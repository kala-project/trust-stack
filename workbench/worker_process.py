"""Spawn-safe isolated executor entry point for the durable CLI worker.

Only primitive runtime configuration and immutable claim metadata cross the spawn
boundary.  The child creates and closes its own runtime; it never receives the
parent's provider, SQLite connections, or queue capabilities.

Linux uses both a fresh process group and ``PR_SET_PDEATHSIG``. Other POSIX
platforms retain process-group TERM/KILL and slot fencing but cannot close the
small parent-crash-before-child-start window with PDEATHSIG. Non-POSIX fallback
can supervise the exact spawned process only; it cannot prove descendant-group
cleanup, so Linux is the supported production hard-crash guarantee.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import ctypes
import json
import math
import os
import re
import signal
import sys

from .job_queue import JobClaim


@dataclass(frozen=True)
class WorkerProcessConfig:
    evidence_db: str
    chat_db: str
    documents_root: str | None = None
    storage_config: str | None = None
    enable_public_acquisition: bool = False

    def __post_init__(self) -> None:
        for name in ("evidence_db", "chat_db"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip():
                raise ValueError(f"{name} must be a nonempty path")
        for name in ("documents_root", "storage_config"):
            value = getattr(self, name)
            if value is not None and (type(value) is not str or not value.strip()):
                raise ValueError(f"{name} must be a nonempty path or None")
        if type(self.enable_public_acquisition) is not bool:
            raise ValueError("enable_public_acquisition must be a boolean")

    def payload(self) -> dict:
        return asdict(self)


_CLAIM_FIELDS = tuple(JobClaim.__dataclass_fields__)
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z", re.ASCII)


def claim_payload(claim: JobClaim) -> dict:
    if type(claim) is not JobClaim:
        raise TypeError("claim must be a JobClaim")
    return {name: getattr(claim, name) for name in _CLAIM_FIELDS}


def _claim_from_payload(payload: dict) -> JobClaim:
    if type(payload) is not dict or tuple(payload) != _CLAIM_FIELDS:
        raise ValueError("invalid claim payload fields")
    claim = JobClaim(**payload)
    strings = (claim.job_id, claim.job_type, claim.conversation_id, claim.attempt_id,
               claim.lease_token, claim.worker_id)
    if any(type(value) is not str or not value for value in strings):
        raise ValueError("invalid claim payload")
    if claim.run_id is not None and (type(claim.run_id) is not str or not claim.run_id):
        raise ValueError("invalid claim run identity")
    if type(claim.attempt_number) is not int or claim.attempt_number < 1:
        raise ValueError("invalid claim attempt number")
    if type(claim.lease_expires_at) is not float or not math.isfinite(claim.lease_expires_at):
        raise ValueError("invalid claim lease boundary")
    return claim


def _install_parent_death_signal(expected_parent_pid: int) -> None:
    """On Linux, close the spawn-before-bind crash window with PDEATHSIG."""
    if not sys.platform.startswith("linux"):
        return
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(), "could not install parent death signal")
    if os.getppid() != expected_parent_pid:
        os.kill(os.getpid(), signal.SIGKILL)


def _safe_failure(exc: BaseException, *, startup: bool) -> dict[str, str]:
    """Retain known domain codes but never serialize arbitrary exception text."""
    try:
        from .research_tools import ToolError
        from .verification_store import VerificationBudgetExceeded, VerificationIntegrityError
    except Exception:  # Imports must not make sanitization itself fail.
        ToolError = ()
        VerificationBudgetExceeded = ()
        VerificationIntegrityError = ()
    if ToolError and isinstance(exc, ToolError):
        code = exc.code if type(exc.code) is str and _SAFE_CODE.fullmatch(exc.code) else "tool_error"
        return {"code": code, "message": "durable executor reported a tool error"}
    if VerificationBudgetExceeded and isinstance(exc, VerificationBudgetExceeded):
        return {"code": "verification_budget_exhausted",
                "message": "durable verification resource budget was exhausted"}
    if VerificationIntegrityError and isinstance(exc, VerificationIntegrityError):
        return {"code": "verification_integrity_error",
                "message": "durable verification integrity check failed"}
    return {
        "code": "executor_startup_error" if startup else "worker_execution_error",
        "message": ("durable executor could not start" if startup
                    else "durable executor could not complete the claimed job"),
    }


def execute_claim_process(config_payload: dict, serialized_claim: dict, cancel_event,
                          start_event, control, expected_parent_pid: int) -> None:
    """Spawn target. It joins no inherited runtime and owns a fresh process group."""
    service = None
    started = False
    try:
        process_group_id = None
        if os.name == "posix":
            os.setsid()
            process_group_id = os.getpid()
        _install_parent_death_signal(expected_parent_pid)
        control.send({"kind": "ready", "pid": os.getpid(), "pgid": process_group_id})
        while not start_event.wait(0.1):
            if os.getppid() != expected_parent_pid:
                return
        config = WorkerProcessConfig(**config_payload)
        claim = _claim_from_payload(serialized_claim)
        from .worker import create_worker_service
        service = create_worker_service(
            config.evidence_db, config.chat_db,
            documents_root=config.documents_root,
            enable_public_acquisition=config.enable_public_acquisition,
            storage_config=config.storage_config,
        )
        started = True
        service.execute_durable_claim(claim, cancel_event)
        control.send({"kind": "completed"})
    except BaseException as exc:
        # Log code locations only, never exception text, source lines, locals or payloads.
        try:
            frames = []
            frame = exc.__traceback__
            while frame is not None:
                code = frame.tb_frame.f_code
                if os.path.dirname(code.co_filename) == os.path.dirname(__file__):
                    frames.append({"module": os.path.basename(code.co_filename),
                                   "function": code.co_name, "line": frame.tb_lineno})
                frame = frame.tb_next
            print(json.dumps({"event": "executor_failure", "exception_type": type(exc).__name__,
                              "frames": frames}), file=sys.stderr, flush=True)
        except Exception:
            pass  # A broken diagnostic sink must not prevent terminal failure reporting.
        try:
            control.send({"kind": "failed", "failure": _safe_failure(exc, startup=not started)})
        except Exception:
            pass
    finally:
        if service is not None:
            try:
                service.close()
            except Exception:
                pass
        try:
            control.close()
        except Exception:
            pass
