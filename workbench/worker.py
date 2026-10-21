"""Single-slot dispatcher for durable ChatService research jobs.

This module never searches for or terminates unrelated processes; an expired lease is only
retried when the queue can prove its same-host worker PID has exited.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import math
import multiprocessing
import os
import signal
import socket
import threading
import time
import uuid
from typing import Protocol

from .job_queue import JobClaim, JobQueue
from .task_lifecycle import TaskFailure, TaskLifecycleRepository
from .worker_process import (
    WorkerProcessConfig, claim_payload, execute_claim_process,
)


class DurableExecutionService(Protocol):
    """Restricted capabilities required by the durable dispatcher."""

    jobs: JobQueue
    task_lifecycle: TaskLifecycleRepository

    def execute_durable_claim(self, claim: JobClaim, cancel_event: threading.Event) -> None: ...

    def close(self) -> None: ...


class WorkerJobsCapability:
    """Queue operations visible to a durable worker."""

    __slots__ = ("lease_seconds", "__claim", "__heartbeat", "__bind", "__release")

    def __init__(self, jobs: JobQueue):
        self.lease_seconds = jobs.lease_seconds
        self.__claim = jobs.claim
        self.__heartbeat = jobs.heartbeat
        self.__bind = jobs.bind_executor_process
        self.__release = jobs.release_runtime_slot

    def claim(self, worker_id: str, *, job_types=("research", "verify")) -> JobClaim | None:
        return self.__claim(worker_id, job_types=job_types)

    def heartbeat(self, claim: JobClaim) -> tuple[bool, bool]:
        return self.__heartbeat(claim)

    def bind_executor_process(self, claim: JobClaim, executor_pid: int,
                              process_group_id: int | None) -> None:
        self.__bind(claim, executor_pid, process_group_id)

    def release_runtime_slot(self, claim: JobClaim) -> None:
        self.__release(claim)


class WorkerLifecycleCapability:
    """Terminal lifecycle operation visible to a durable worker."""

    __slots__ = ("__fail_claim",)

    def __init__(self, lifecycle: TaskLifecycleRepository):
        self.__fail_claim = lifecycle.fail_claim

    def fail_claim(self, claim: JobClaim, failure: TaskFailure):
        return self.__fail_claim(claim, failure)


class WorkerExecutionCapability:
    """Claim execution operation visible to a durable worker."""

    __slots__ = ("__execute",)

    def __init__(self, execute):
        self.__execute = execute

    def execute(self, claim: JobClaim, cancel_event: threading.Event) -> None:
        self.__execute(claim, cancel_event)


class WorkerServiceCapability:
    """Narrow facade bound immediately from a compatible durable service."""

    __slots__ = ("jobs", "lifecycle", "execution", "job_types", "__close")

    def __init__(self, service: DurableExecutionService):
        self.jobs = WorkerJobsCapability(service.jobs)
        self.lifecycle = WorkerLifecycleCapability(service.task_lifecycle)
        self.execution = WorkerExecutionCapability(service.execute_durable_claim)
        self.job_types = ("research", "verify")
        self.__close = service.close

    def close(self) -> None:
        self.__close()


@dataclass(frozen=True)
class SupervisionConfig:
    """Bounded hard-stop policy used by production process execution."""

    hard_timeout_seconds: float = 1800.0
    cancel_grace_seconds: float = 10.0
    terminate_grace_seconds: float = 5.0
    kill_grace_seconds: float = 5.0
    startup_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive finite number")
            if value > 604800:
                raise ValueError(f"{name} must not exceed 604800 seconds")
            object.__setattr__(self, name, float(value))


class DurableWorker:
    """Claim and execute at most one model-backed job at a time.

    ``process_config=None`` preserves the controlled injected-provider unit-test
    path. Production CLI construction always supplies it and therefore never runs
    a model provider in the supervisor process.
    """

    def __init__(self, service: DurableExecutionService, *, worker_id: str | None = None,
                 heartbeat_seconds: float = 10.0, poll_seconds: float = 1.0,
                 process_config: WorkerProcessConfig | None = None,
                 supervision: SupervisionConfig | None = None,
                 process_target=execute_claim_process):
        capabilities = WorkerServiceCapability(service)
        if heartbeat_seconds <= 0 or heartbeat_seconds >= capabilities.jobs.lease_seconds:
            raise ValueError("heartbeat_seconds must be positive and shorter than the lease")
        self.capabilities = capabilities
        self.jobs = capabilities.jobs
        self.task_lifecycle = capabilities.lifecycle
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.poll_seconds = max(0.01, float(poll_seconds))
        self.stop_event = threading.Event()
        if process_config is not None and not isinstance(process_config, WorkerProcessConfig):
            raise TypeError("process_config must be WorkerProcessConfig or None")
        if supervision is not None and not isinstance(supervision, SupervisionConfig):
            raise TypeError("supervision must be SupervisionConfig or None")
        self.process_config = process_config
        self.supervision = supervision or SupervisionConfig()
        self._process_target = process_target

    def _heartbeat(self, claim: JobClaim, done_event: threading.Event,
                   cancel_event: threading.Event) -> None:
        while not done_event.wait(self.heartbeat_seconds):
            slot_owned, job_active = self.jobs.heartbeat(claim)
            if self.stop_event.is_set() or not slot_owned or not job_active:
                cancel_event.set()
                return

    def _fail_unfinished_claim(self, claim: JobClaim, failure: TaskFailure) -> None:
        """Delegate setup failure to the claim-fenced transactional capability."""
        self.task_lifecycle.fail_claim(claim, failure)

    @staticmethod
    def _receive(control, timeout: float = 0.0):
        try:
            return control.recv() if control.poll(timeout) else None
        except (EOFError, OSError):
            return None

    @staticmethod
    def _group_alive(process_group_id: int | None) -> bool:
        if process_group_id is None or os.name != "posix":
            return False
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except (OSError, ValueError):
            # Indeterminate liveness must retain the slot, never prove cleanup.
            return True
        return True

    @staticmethod
    def _signal_owned_group(process, process_group_id: int | None, signum: int) -> None:
        """Signal only the child object or the group whose leader is that exact child."""
        if os.name == "posix" and process_group_id == process.pid:
            try:
                os.killpg(process_group_id, signum)
            except ProcessLookupError:
                pass
            return
        if signum == signal.SIGTERM:
            process.terminate()
        else:
            process.kill()

    def _prove_executor_stopped(self, process, process_group_id: int | None) -> bool:
        """TERM/KILL the owned child group and return only after liveness is disproved."""
        policy = self.supervision
        if process.is_alive() or self._group_alive(process_group_id):
            self._signal_owned_group(process, process_group_id, signal.SIGTERM)
            process.join(policy.terminate_grace_seconds)
            deadline = time.monotonic() + policy.terminate_grace_seconds
            while self._group_alive(process_group_id) and time.monotonic() < deadline:
                time.sleep(0.01)
        if process.is_alive() or self._group_alive(process_group_id):
            self._signal_owned_group(process, process_group_id, signal.SIGKILL)
            process.join(policy.kill_grace_seconds)
            deadline = time.monotonic() + policy.kill_grace_seconds
            while self._group_alive(process_group_id) and time.monotonic() < deadline:
                time.sleep(0.01)
        process.join(0)
        return not process.is_alive() and not self._group_alive(process_group_id)

    def _run_supervised(self, claim: JobClaim) -> None:
        policy = self.supervision
        context = multiprocessing.get_context("spawn")
        cancel_event = context.Event()
        start_event = context.Event()
        parent_control, child_control = context.Pipe(duplex=False)
        process = context.Process(
            target=self._process_target,
            args=(self.process_config.payload(), claim_payload(claim), cancel_event,
                  start_event, child_control, os.getpid()),
            name=f"durable-executor-{claim.attempt_number}",
        )
        process_group_id = None
        process_started = False
        failure = None
        result = None
        launched = time.monotonic()
        try:
            process.start()
            process_started = True
            child_control.close()
            startup_deadline = min(
                launched + policy.hard_timeout_seconds,
                launched + policy.startup_timeout_seconds,
            )
            ready = None
            while time.monotonic() < startup_deadline and process.is_alive():
                ready = self._receive(
                    parent_control,
                    min(0.05, max(0.0, startup_deadline - time.monotonic())),
                )
                if ready is not None:
                    break
                if self.stop_event.is_set():
                    break
            if (type(ready) is not dict or ready.get("kind") != "ready"
                    or ready.get("pid") != process.pid
                    or ready.get("pgid") not in (None, process.pid)):
                cancel_event.set()
                failure = TaskFailure(
                    "executor_startup_timeout" if process.is_alive() else "executor_startup_error",
                    "durable executor did not complete its safe startup handshake",
                )
            else:
                process_group_id = ready["pgid"]
                self.jobs.bind_executor_process(claim, process.pid, process_group_id)
                start_event.set()
                next_heartbeat = time.monotonic() + self.heartbeat_seconds
                cancel_started = None
                terminated_at = None
                killed_at = None
                cancel_code = None
                while process.is_alive():
                    now = time.monotonic()
                    received = self._receive(parent_control)
                    if received is not None:
                        result = received
                    if cancel_started is None and now >= next_heartbeat:
                        slot_owned, job_active = self.jobs.heartbeat(claim)
                        next_heartbeat = now + self.heartbeat_seconds
                        if not slot_owned or not job_active:
                            cancel_code = "executor_lease_lost"
                            cancel_started = now
                            cancel_event.set()
                    if cancel_started is None and self.stop_event.is_set():
                        cancel_code = "executor_stopped"
                        cancel_started = now
                        cancel_event.set()
                    if cancel_started is None and now - launched >= policy.hard_timeout_seconds:
                        cancel_code = "executor_timeout"
                        cancel_started = now
                        cancel_event.set()
                    if (cancel_started is not None and terminated_at is None
                            and now - cancel_started >= policy.cancel_grace_seconds):
                        self._signal_owned_group(process, process_group_id, signal.SIGTERM)
                        terminated_at = now
                    if (terminated_at is not None and killed_at is None
                            and now - terminated_at >= policy.terminate_grace_seconds
                            and (process.is_alive() or self._group_alive(process_group_id))):
                        self._signal_owned_group(process, process_group_id, signal.SIGKILL)
                        killed_at = now
                    if (killed_at is not None and now - killed_at >= policy.kill_grace_seconds
                            and (process.is_alive() or self._group_alive(process_group_id))):
                        raise RuntimeError("owned durable executor could not be proven stopped")
                    time.sleep(0.01)
                process.join(0)
                received = self._receive(parent_control)
                if received is not None:
                    result = received
                if cancel_code is not None:
                    messages = {
                        "executor_timeout": "durable executor exceeded its hard time limit",
                        "executor_stopped": "durable executor stopped with its supervisor",
                        "executor_lease_lost": "durable executor lost its current lease",
                    }
                    failure = TaskFailure(cancel_code, messages[cancel_code])
                elif type(result) is dict and result.get("kind") == "failed":
                    item = result.get("failure") or {}
                    failure = TaskFailure(item.get("code", "worker_execution_error"),
                                          item.get("message", "durable executor failed"))
                elif type(result) is not dict or result.get("kind") != "completed":
                    failure = TaskFailure(
                        "executor_abnormal_exit", "durable executor exited without a valid outcome")
                else:
                    failure = TaskFailure(
                        "executor_incomplete", "durable executor returned without a terminal job state")
        except Exception:
            if failure is None:
                failure = TaskFailure(
                    "worker_execution_error", "durable executor supervision failed")
        finally:
            cancel_event.set()
            start_event.set()
            stopped = (self._prove_executor_stopped(process, process_group_id)
                       if process_started else True)
            parent_control.close()
            try:
                child_control.close()
            except Exception:
                pass
            if not stopped:
                # Keep the slot bound to the child identity; lease expiry also refuses
                # recovery while that PID/process group may still host model work.
                raise RuntimeError("owned durable executor remains alive")
            if failure is not None:
                self._fail_unfinished_claim(claim, failure)
            # Release only after the child and every process in its owned group are gone.
            self.jobs.release_runtime_slot(claim)

    def run_once(self) -> bool:
        """Fairly execute one research or verify claim through the shared model slot."""
        claim = self.jobs.claim(self.worker_id, job_types=self.capabilities.job_types)
        if claim is None:
            return False
        if self.process_config is not None:
            self._run_supervised(claim)
            return True
        cancel_event = threading.Event()
        done_event = threading.Event()
        heartbeat = threading.Thread(
            target=self._heartbeat, args=(claim, done_event, cancel_event),
            name=f"job-heartbeat-{claim.attempt_number}", daemon=True,
        )
        heartbeat.start()
        try:
            self.capabilities.execution.execute(claim, cancel_event)
        except Exception:
            self._fail_unfinished_claim(claim, TaskFailure(
                code="worker_execution_error",
                message="durable worker could not execute the claimed research job",
            ))
        finally:
            done_event.set()
            cancel_event.set()
            heartbeat.join(self.heartbeat_seconds + 0.5)
            self.jobs.release_runtime_slot(claim)
        return True

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            if not self.run_once():
                self.stop_event.wait(self.poll_seconds)

    def stop(self) -> None:
        self.stop_event.set()


def create_worker_service(evidence_db, chat_db, *, documents_root=None,
                          enable_public_acquisition=False, provider=None, storage_config=None,
                          verification_config=None):
    """Build the shared runtime without importing HTTP; caller closes the chat service.

    Shared optional data services have no separate lifetime.
    """
    from .runtime import create_runtime_services
    from .runtime_config import RuntimeConfig, RuntimePaths

    config = RuntimeConfig(
        RuntimePaths.resolve(evidence_db, chat_db=chat_db, documents_root=documents_root),
        verification_config=verification_config,
        enable_public_acquisition=enable_public_acquisition, storage_config=storage_config,
    )
    return create_runtime_services(config, provider=provider).chat


class _SupervisorOnlyProvider:
    """Inert marker: production model construction occurs only in the spawned child."""


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Run the single-slot durable research worker")
    parser.add_argument("--evidence-db", required=True)
    parser.add_argument("--chat-db", required=True)
    parser.add_argument("--documents-root")
    parser.add_argument("--storage-config")
    parser.add_argument("--enable-public-acquisition", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=10.0)
    parser.add_argument("--hard-timeout-seconds", type=float, default=1800.0)
    parser.add_argument("--cancel-grace-seconds", type=float, default=10.0)
    parser.add_argument("--terminate-grace-seconds", type=float, default=5.0)
    parser.add_argument("--kill-grace-seconds", type=float, default=5.0)
    parser.add_argument("--startup-timeout-seconds", type=float, default=30.0)
    args = parser.parse_args(argv)

    process_config = WorkerProcessConfig(
        evidence_db=args.evidence_db, chat_db=args.chat_db,
        documents_root=args.documents_root, storage_config=args.storage_config,
        enable_public_acquisition=args.enable_public_acquisition,
    )
    supervision = SupervisionConfig(
        hard_timeout_seconds=args.hard_timeout_seconds,
        cancel_grace_seconds=args.cancel_grace_seconds,
        terminate_grace_seconds=args.terminate_grace_seconds,
        kill_grace_seconds=args.kill_grace_seconds,
        startup_timeout_seconds=args.startup_timeout_seconds,
    )
    # The supervisor opens only queue/lifecycle capabilities with an inert provider.
    # Every claimed job creates the real fixed provider in a fresh spawned child.
    service = create_worker_service(
        args.evidence_db, args.chat_db, documents_root=args.documents_root,
        enable_public_acquisition=args.enable_public_acquisition,
        provider=_SupervisorOnlyProvider(), storage_config=args.storage_config)
    worker = DurableWorker(
        service, heartbeat_seconds=args.heartbeat_seconds, poll_seconds=args.poll_seconds,
        process_config=process_config, supervision=supervision)

    def request_stop(_signum, _frame):
        worker.stop()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        if args.once:
            worker.run_once()
        else:
            worker.run_forever()
    finally:
        worker.stop()
        service.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by deployment integration
    raise SystemExit(main())
