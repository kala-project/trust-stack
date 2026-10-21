"""Durable, fenced jobs stored in the chat SQLite database.

The queue provides at-least-once execution only.  A lease prevents stale attempts
from binding results; it cannot make an external model call exactly once.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
import socket
import sqlite3
import time
import uuid
from typing import Protocol


JOB_STATES = (
    "queued", "running", "retry_wait", "blocked", "succeeded", "failed",
    "cancelled", "superseded",
)
TERMINAL_JOB_STATES = ("succeeded", "failed", "cancelled", "superseded")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
 job_id TEXT PRIMARY KEY,
 job_type TEXT NOT NULL CHECK(job_type IN ('research','verify')),
 run_id TEXT UNIQUE REFERENCES runs(run_id),
 conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
 request_identity TEXT NOT NULL UNIQUE,
 input_hash TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('queued','running','retry_wait','blocked','succeeded','failed','cancelled','superseded')),
 available_at REAL NOT NULL,
 deadline_at REAL,
 max_attempts INTEGER NOT NULL CHECK(max_attempts BETWEEN 1 AND 20),
 attempt_count INTEGER NOT NULL DEFAULT 0,
 cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
 lease_owner TEXT,
 lease_token TEXT,
 lease_expires_at REAL,
 current_attempt_id TEXT,
 error_json TEXT,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 finished_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_claim_idx ON jobs(status,available_at,created_at);
CREATE UNIQUE INDEX IF NOT EXISTS one_running_model_job
 ON jobs((1)) WHERE status='running' AND job_type IN ('research','verify');
CREATE UNIQUE INDEX IF NOT EXISTS one_mutating_job_per_conversation
 ON jobs(conversation_id) WHERE status IN ('queued','running','retry_wait');
CREATE TABLE IF NOT EXISTS job_attempts (
 attempt_id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 attempt_number INTEGER NOT NULL,
 worker_id TEXT NOT NULL,
 worker_host TEXT NOT NULL,
 worker_pid INTEGER NOT NULL,
 executor_pgid INTEGER,
 lease_token TEXT NOT NULL UNIQUE,
 claimed_lease_expires_at REAL NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','cancelled','blocked','stale','retry_wait')),
 started_at TEXT NOT NULL,
 heartbeat_at TEXT NOT NULL,
 finished_at TEXT,
 error_json TEXT,
 UNIQUE(job_id,attempt_number)
);
CREATE TABLE IF NOT EXISTS job_checkpoints (
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 checkpoint_key TEXT NOT NULL,
 attempt_id TEXT NOT NULL REFERENCES job_attempts(attempt_id),
 value_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(job_id,checkpoint_key)
);
CREATE TABLE IF NOT EXISTS job_events (
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 seq INTEGER NOT NULL,
 event_type TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(job_id,seq)
);
CREATE TABLE IF NOT EXISTS model_runtime_slot (
 slot_id INTEGER PRIMARY KEY CHECK(slot_id=1), attempt_id TEXT NOT NULL,
 lease_token TEXT NOT NULL, worker_host TEXT NOT NULL, worker_pid INTEGER NOT NULL,
 executor_pgid INTEGER, lease_expires_at REAL NOT NULL
);
"""


class JobQueueError(RuntimeError):
    pass


class LeaseLost(JobQueueError):
    pass


class LifecycleWrites(Protocol):
    def expire_run(self, connection: sqlite3.Connection, run_id: str, *, retrying: bool,
                   error: dict) -> None: ...

    def fail_deadline_run(self, connection: sqlite3.Connection, run_id: str,
                          error: dict) -> None: ...


@dataclass(frozen=True)
class JobClaim:
    job_id: str
    job_type: str
    run_id: str | None
    conversation_id: str
    attempt_id: str
    attempt_number: int
    lease_token: str
    worker_id: str
    lease_expires_at: float


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class JobQueue:
    """Short-transaction queue operations over a supplied SQLite connection factory."""

    def __init__(self, connect, *, lease_seconds: float = 90.0,
                 retry_delays=(30.0, 120.0), clock=time.time,
                 lifecycle_writes: LifecycleWrites):
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._connect = connect
        self.lease_seconds = float(lease_seconds)
        self.retry_delays = tuple(float(value) for value in retry_delays)
        self._clock = clock
        self.__lifecycle_writes = lifecycle_writes

    def initialize(self, connection) -> None:
        connection.executescript(_SCHEMA)
        attempt_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(job_attempts)")
        }
        if "claimed_lease_expires_at" not in attempt_columns:
            connection.execute(
                "ALTER TABLE job_attempts ADD COLUMN claimed_lease_expires_at REAL")
            connection.execute(
                "UPDATE job_attempts SET claimed_lease_expires_at=("
                "SELECT lease_expires_at FROM jobs WHERE jobs.job_id=job_attempts.job_id) "
                "WHERE claimed_lease_expires_at IS NULL")
            connection.commit()
        if "executor_pgid" not in attempt_columns:
            connection.execute("ALTER TABLE job_attempts ADD COLUMN executor_pgid INTEGER")
        slot_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(model_runtime_slot)")
        }
        if "executor_pgid" not in slot_columns:
            connection.execute("ALTER TABLE model_runtime_slot ADD COLUMN executor_pgid INTEGER")
        connection.commit()

    @staticmethod
    def _event(connection, job_id: str, event_type: str, payload: dict) -> None:
        seq = connection.execute(
            "SELECT coalesce(max(seq),0)+1 FROM job_events WHERE job_id=?", (job_id,)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO job_events VALUES(?,?,?,?,?)",
            (job_id, seq, event_type, _json(payload), _now()),
        )

    def enqueue_research(self, connection, *, run_id: str, conversation_id: str,
                         request_identity: str, input_hash: str, max_attempts: int = 3,
                         deadline_at: float | None = None) -> dict:
        """Register a research job inside the caller's run-registration transaction."""
        existing = connection.execute(
            "SELECT * FROM jobs WHERE request_identity=?", (request_identity,)
        ).fetchone()
        if existing is not None:
            if (existing["run_id"] != run_id or existing["input_hash"] != input_hash
                    or existing["job_type"] != "research"):
                raise JobQueueError("request identity is already bound to another job input")
            return dict(existing)
        created = _now()
        job_id = _id("job")
        connection.execute(
            "INSERT INTO jobs(job_id,job_type,run_id,conversation_id,request_identity,input_hash,"
            "status,available_at,deadline_at,max_attempts,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,'queued',?,?,?,?,?)",
            (job_id, "research", run_id, conversation_id, request_identity, input_hash,
             self._clock(), deadline_at, max_attempts, created, created),
        )
        self._event(connection, job_id, "queued", {"job_type": "research", "run_id": run_id})
        return dict(connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def enqueue_verify(self, connection, *, conversation_id: str, request_identity: str,
                       input_hash: str, max_attempts: int = 3,
                       deadline_at: float | None = None) -> dict:
        """Reserve the durable contract for a future verifier; no verifier is implemented."""
        existing = connection.execute(
            "SELECT * FROM jobs WHERE request_identity=?", (request_identity,)
        ).fetchone()
        if existing is not None:
            if existing["input_hash"] != input_hash or existing["job_type"] != "verify":
                raise JobQueueError("request identity is already bound to another job input")
            return dict(existing)
        created = _now()
        job_id = _id("job")
        connection.execute(
            "INSERT INTO jobs(job_id,job_type,run_id,conversation_id,request_identity,input_hash,"
            "status,available_at,deadline_at,max_attempts,created_at,updated_at) "
            "VALUES(?,?,NULL,?,?,?,'queued',?,?,?,?,?)",
            (job_id, "verify", conversation_id, request_identity, input_hash,
             self._clock(), deadline_at, max_attempts, created, created),
        )
        self._event(connection, job_id, "queued", {"job_type": "verify"})
        return dict(connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    @staticmethod
    def _process_is_gone(host: str, pid: int) -> bool:
        if host != socket.gethostname() or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except (OSError, ValueError):
            # Lack of permission or an indeterminate probe cannot prove exit.
            return False
        return False

    @classmethod
    def _executor_is_gone(cls, host: str, pid: int, process_group_id: int | None) -> bool:
        """Prove a same-host executor, including descendants in its owned group, exited."""
        if process_group_id is None:
            return cls._process_is_gone(host, pid)
        if host != socket.gethostname() or process_group_id <= 0 or os.name != "posix":
            return False
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return True
        except (OSError, ValueError):
            # Permission and other indeterminate failures are not proof of exit.
            return False
        return False

    def _expire_one(self, connection, row, now: float) -> None:
        attempt = connection.execute(
            "SELECT * FROM job_attempts WHERE attempt_id=?", (row["current_attempt_id"],)
        ).fetchone()
        if attempt is None:
            safe_to_retry = False
        else:
            safe_to_retry = self._executor_is_gone(
                attempt["worker_host"], attempt["worker_pid"], attempt["executor_pgid"])
            connection.execute(
                "UPDATE job_attempts SET status='stale',finished_at=?,error_json=? "
                "WHERE attempt_id=? AND status='running'",
                (_now(), _json({"code": "lease_expired"}), attempt["attempt_id"]),
            )
        can_retry = safe_to_retry and row["attempt_count"] < row["max_attempts"] \
            and (row["deadline_at"] is None or row["deadline_at"] > now)
        if can_retry:
            status, available, error = "retry_wait", now, {"code": "worker_exited"}
        else:
            status, available = "failed", now
            error = {"code": "recovery_unconfirmed", "message": "expired worker could not be confirmed stopped"}
        changed = connection.execute(
            "UPDATE jobs SET status=?,available_at=?,lease_owner=NULL,lease_token=NULL,"
            "lease_expires_at=NULL,current_attempt_id=NULL,error_json=?,updated_at=?,finished_at=? "
            "WHERE job_id=? AND status='running' AND lease_token=?",
            (status, available, _json(error), _now(), _now() if status == "failed" else None,
             row["job_id"], row["lease_token"]),
        ).rowcount
        if changed:
            if row["run_id"] is not None:
                self.__lifecycle_writes.expire_run(
                    connection, row["run_id"], retrying=status == "retry_wait", error=error)
            self._event(connection, row["job_id"], status, {"error": error, "expired": True})

    def reap_expired(self, connection, *, now: float | None = None) -> int:
        now = self._clock() if now is None else now
        rows = connection.execute(
            "SELECT * FROM jobs WHERE status='running' AND lease_expires_at<=?", (now,)
        ).fetchall()
        for row in rows:
            self._expire_one(connection, row, now)
        return len(rows)

    def fail_expired_deadlines(self, connection, *, now: float | None = None) -> int:
        """Fail overdue work atomically while retaining any live executor's runtime slot."""
        now = self._clock() if now is None else now
        rows = connection.execute(
            "SELECT * FROM jobs WHERE status IN ('queued','retry_wait','running') "
            "AND deadline_at IS NOT NULL AND deadline_at<=?", (now,)
        ).fetchall()
        error = {"code": "deadline_exceeded", "message": "job deadline expired"}
        encoded_error = _json(error)
        failed = 0
        for row in rows:
            changed = connection.execute(
                "UPDATE jobs SET status='failed',lease_owner=NULL,lease_token=NULL,"
                "lease_expires_at=NULL,error_json=?,updated_at=?,finished_at=? "
                "WHERE job_id=? AND status=? AND deadline_at IS NOT NULL AND deadline_at<=?",
                (encoded_error, _now(), _now(), row["job_id"], row["status"], now),
            ).rowcount
            if not changed:
                continue
            failed += 1
            if row["current_attempt_id"]:
                connection.execute(
                    "UPDATE job_attempts SET status='failed',finished_at=?,error_json=? "
                    "WHERE attempt_id=? AND status='running'",
                    (_now(), encoded_error, row["current_attempt_id"]),
                )
            if row["run_id"] is not None:
                self.__lifecycle_writes.fail_deadline_run(connection, row["run_id"], error)
            self._event(connection, row["job_id"], "failed", {
                "error": error, "deadline_expired": True,
            })
        return failed

    def claim(self, worker_id: str, *, job_types=("research",), worker_pid: int | None = None) -> JobClaim | None:
        if not worker_id:
            raise ValueError("worker_id is required")
        allowed = tuple(dict.fromkeys(job_types))
        if not allowed or any(value not in {"research", "verify"} for value in allowed):
            raise ValueError("unsupported job type")
        now = self._clock()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self.fail_expired_deadlines(connection, now=now)
            self.reap_expired(connection, now=now)
            slot = connection.execute("SELECT * FROM model_runtime_slot WHERE slot_id=1").fetchone()
            if (slot is not None and slot["lease_expires_at"] <= now
                    and self._executor_is_gone(
                        slot["worker_host"], slot["worker_pid"], slot["executor_pgid"])):
                connection.execute("DELETE FROM model_runtime_slot WHERE slot_id=1 AND lease_token=?",
                                   (slot["lease_token"],))
                slot = None
            if slot is not None:
                return None
            placeholders = ",".join("?" for _ in allowed)
            row = connection.execute(
                f"SELECT * FROM jobs WHERE status IN ('queued','retry_wait') "
                f"AND available_at<=? AND cancel_requested=0 AND job_type IN ({placeholders}) "
                "AND (deadline_at IS NULL OR deadline_at>?) "
                "AND NOT EXISTS (SELECT 1 FROM jobs active WHERE active.status='running' "
                "AND active.job_type IN ('research','verify')) "
                "ORDER BY available_at,created_at,job_id LIMIT 1",
                (now, *allowed, now),
            ).fetchone()
            if row is None:
                return None
            attempt_number = row["attempt_count"] + 1
            attempt_id, token = _id("attempt"), uuid.uuid4().hex
            expires = now + self.lease_seconds
            changed = connection.execute(
                "UPDATE jobs SET status='running',attempt_count=?,lease_owner=?,lease_token=?,"
                "lease_expires_at=?,current_attempt_id=?,error_json=NULL,updated_at=? "
                "WHERE job_id=? AND status IN ('queued','retry_wait') AND cancel_requested=0",
                (attempt_number, worker_id, token, expires, attempt_id, _now(), row["job_id"]),
            ).rowcount
            if changed != 1:
                return None
            connection.execute(
                "INSERT INTO job_attempts("
                "attempt_id,job_id,attempt_number,worker_id,worker_host,worker_pid,executor_pgid,lease_token,"
                "claimed_lease_expires_at,status,started_at,heartbeat_at,finished_at,error_json) "
                "VALUES(?,?,?,?,?,?,NULL,?,?,'running',?,?,NULL,NULL)",
                (attempt_id, row["job_id"], attempt_number, worker_id, socket.gethostname(),
                 os.getpid() if worker_pid is None else worker_pid, token, expires, _now(), _now()),
            )
            connection.execute(
                "INSERT INTO model_runtime_slot("
                "slot_id,attempt_id,lease_token,worker_host,worker_pid,executor_pgid,lease_expires_at) "
                "VALUES(1,?,?,?,?,NULL,?)",
                (attempt_id, token, socket.gethostname(),
                 os.getpid() if worker_pid is None else worker_pid, expires),
            )
            self._event(connection, row["job_id"], "running", {
                "attempt_id": attempt_id, "attempt_number": attempt_number,
                "worker_id": worker_id,
            })
            return JobClaim(row["job_id"], row["job_type"], row["run_id"],
                            row["conversation_id"], attempt_id, attempt_number,
                            token, worker_id, expires)

    def bind_executor_process(self, claim: JobClaim, executor_pid: int,
                              process_group_id: int | None) -> None:
        """Atomically hand a claimed slot from this supervisor to its safe child identity."""
        if type(executor_pid) is not int or executor_pid <= 0:
            raise ValueError("executor_pid must be a positive integer")
        if process_group_id is not None and (
                type(process_group_id) is not int or process_group_id <= 0):
            raise ValueError("process_group_id must be a positive integer or None")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self.assert_claim(connection, claim)
            host, supervisor_pid = socket.gethostname(), os.getpid()
            slot_changed = connection.execute(
                "UPDATE model_runtime_slot SET worker_pid=?,executor_pgid=? "
                "WHERE slot_id=1 AND attempt_id=? AND lease_token=? "
                "AND worker_host=? AND worker_pid=? AND executor_pgid IS NULL",
                (executor_pid, process_group_id, claim.attempt_id, claim.lease_token,
                 host, supervisor_pid),
            ).rowcount
            attempt_changed = connection.execute(
                "UPDATE job_attempts SET worker_pid=?,executor_pgid=? "
                "WHERE attempt_id=? AND lease_token=? AND status='running' "
                "AND worker_host=? AND worker_pid=? AND executor_pgid IS NULL",
                (executor_pid, process_group_id, claim.attempt_id, claim.lease_token,
                 host, supervisor_pid),
            ).rowcount
            if slot_changed != 1 or attempt_changed != 1:
                raise LeaseLost("durable executor process could not be bound")

    def assert_claim(self, connection, claim: JobClaim, *, allow_blocked: bool = False) -> JobClaim:
        if type(claim) is not JobClaim:
            raise TypeError("claim must be a JobClaim")
        string_fields = (
            claim.job_id, claim.job_type, claim.conversation_id, claim.attempt_id,
            claim.lease_token, claim.worker_id,
        )
        if any(type(value) is not str or not value for value in string_fields):
            raise TypeError("claim string fields must be nonempty strings")
        if claim.run_id is not None and (type(claim.run_id) is not str or not claim.run_id):
            raise TypeError("claim run_id must be a nonempty string or None")
        if type(claim.attempt_number) is not int or claim.attempt_number < 1:
            raise TypeError("claim attempt_number must be a positive integer")
        if type(claim.lease_expires_at) is not float or not math.isfinite(claim.lease_expires_at):
            raise TypeError("claim lease_expires_at must be a finite float")
        states = ("running", "blocked") if allow_blocked else ("running",)
        now = self._clock()
        placeholders = ",".join("?" for _ in states)
        row = connection.execute(
            f"SELECT j.job_id,j.job_type,j.run_id,j.conversation_id,j.attempt_count,"
            "j.lease_expires_at,a.attempt_id,a.attempt_number,a.lease_token,a.worker_id,"
            "a.claimed_lease_expires_at "
            "FROM jobs j JOIN job_attempts a ON a.attempt_id=j.current_attempt_id "
            f"WHERE j.job_id=? AND j.current_attempt_id=? AND j.lease_token=? "
            "AND a.job_id=j.job_id AND a.lease_token=j.lease_token AND a.status='running' "
            f"AND j.cancel_requested=0 AND j.status IN ({placeholders}) AND j.lease_expires_at>? "
            "AND (j.deadline_at IS NULL OR j.deadline_at>?)",
            (claim.job_id, claim.attempt_id, claim.lease_token, *states, now, now),
        ).fetchone()
        if row is None:
            raise LeaseLost("durable job lease is no longer current")
        persisted_identity = (
            row["job_id"], row["job_type"], row["run_id"], row["conversation_id"],
            row["attempt_id"], row["attempt_number"], row["lease_token"], row["worker_id"],
        )
        claim_identity = (
            claim.job_id, claim.job_type, claim.run_id, claim.conversation_id,
            claim.attempt_id, claim.attempt_number, claim.lease_token, claim.worker_id,
        )
        if claim_identity != persisted_identity or claim.attempt_number != row["attempt_count"]:
            raise LeaseLost("durable job claim identity does not match persisted attempt")
        if claim.lease_expires_at != row["claimed_lease_expires_at"]:
            raise LeaseLost("durable job claim lease boundary does not match persisted attempt")
        return JobClaim(*persisted_identity, row["claimed_lease_expires_at"])

    def heartbeat(self, claim: JobClaim) -> tuple[bool, bool]:
        """Return (slot_owned, job_active), retaining the slot after cancellation."""
        now = self._clock()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self.fail_expired_deadlines(connection, now=now)
            expires = now + self.lease_seconds
            changed = connection.execute(
                "UPDATE jobs SET lease_expires_at=?,updated_at=? WHERE job_id=? AND status='running' "
                "AND current_attempt_id=? AND lease_token=? AND cancel_requested=0 AND lease_expires_at>? "
                "AND (deadline_at IS NULL OR deadline_at>?) "
                "AND EXISTS (SELECT 1 FROM model_runtime_slot WHERE slot_id=1 AND attempt_id=? "
                "AND lease_token=? AND lease_expires_at>?)",
                (expires, _now(), claim.job_id, claim.attempt_id, claim.lease_token, now, now,
                 claim.attempt_id, claim.lease_token, now),
            ).rowcount
            slot_owned = False
            if changed:
                slot_owned = connection.execute(
                    "UPDATE model_runtime_slot SET lease_expires_at=? WHERE slot_id=1 "
                    "AND attempt_id=? AND lease_token=? AND lease_expires_at>?",
                    (expires, claim.attempt_id, claim.lease_token, now),
                ).rowcount == 1
                connection.execute(
                    "UPDATE job_attempts SET heartbeat_at=? WHERE attempt_id=? AND status='running'",
                    (_now(), claim.attempt_id),
                )
            return slot_owned, changed == 1

    def release_runtime_slot(self, claim: JobClaim) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM model_runtime_slot WHERE slot_id=1 AND attempt_id=? AND lease_token=?",
                (claim.attempt_id, claim.lease_token),
            )

    def checkpoint(self, claim: JobClaim, key: str, value) -> None:
        if not key or len(key) > 100:
            raise ValueError("invalid checkpoint key")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self.assert_claim(connection, claim)
            connection.execute(
                "INSERT INTO job_checkpoints VALUES(?,?,?,?,?) ON CONFLICT(job_id,checkpoint_key) "
                "DO UPDATE SET attempt_id=excluded.attempt_id,value_json=excluded.value_json,created_at=excluded.created_at",
                (claim.job_id, key, claim.attempt_id, _json(value), _now()),
            )
            self._event(connection, claim.job_id, "checkpoint", {"key": key})

    def mark_blocked_in_transaction(self, connection, claim: JobClaim) -> None:
        self.assert_claim(connection, claim)
        changed = connection.execute(
            "UPDATE jobs SET status='blocked',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,"
            "updated_at=? WHERE job_id=? AND current_attempt_id=? AND lease_token=? AND status='running'",
            (_now(), claim.job_id, claim.attempt_id, claim.lease_token),
        ).rowcount
        if changed != 1:
            raise LeaseLost("durable job could not be blocked")
        connection.execute(
            "UPDATE job_attempts SET status='blocked',finished_at=? WHERE attempt_id=? AND status='running'",
            (_now(), claim.attempt_id),
        )
        self._event(connection, claim.job_id, "blocked", {})

    def succeed_in_transaction(self, connection, claim: JobClaim) -> None:
        self.assert_claim(connection, claim)
        changed = connection.execute(
            "UPDATE jobs SET status='succeeded',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,"
            "updated_at=?,finished_at=? WHERE job_id=? AND current_attempt_id=? AND lease_token=? "
            "AND status='running' AND cancel_requested=0",
            (_now(), _now(), claim.job_id, claim.attempt_id, claim.lease_token),
        ).rowcount
        if changed != 1:
            raise LeaseLost("durable job completion lost its lease")
        connection.execute(
            "UPDATE job_attempts SET status='succeeded',finished_at=? WHERE attempt_id=? AND status='running'",
            (_now(), claim.attempt_id),
        )
        self._event(connection, claim.job_id, "succeeded", {})

    def fail_in_transaction(self, connection, claim: JobClaim, error: dict, *, retryable: bool) -> str:
        self.assert_claim(connection, claim)
        row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (claim.job_id,)).fetchone()
        now = self._clock()
        delay_index = max(0, row["attempt_count"] - 1)
        delay = self.retry_delays[min(delay_index, len(self.retry_delays) - 1)] if self.retry_delays else 0.0
        can_retry = (retryable and row["attempt_count"] < row["max_attempts"]
                     and (row["deadline_at"] is None or now + delay < row["deadline_at"]))
        status = "retry_wait" if can_retry else "failed"
        available = now + delay if can_retry else now
        changed = connection.execute(
            "UPDATE jobs SET status=?,available_at=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,"
            "error_json=?,updated_at=?,finished_at=? WHERE job_id=? AND current_attempt_id=? AND lease_token=? "
            "AND status='running'",
            (status, available, _json(error), _now(), None if can_retry else _now(),
             claim.job_id, claim.attempt_id, claim.lease_token),
        ).rowcount
        if changed != 1:
            raise LeaseLost("durable job failure lost its lease")
        connection.execute(
            "UPDATE job_attempts SET status=?,finished_at=?,error_json=? WHERE attempt_id=? AND status='running'",
            (status, _now(), _json(error), claim.attempt_id),
        )
        self._event(connection, claim.job_id, status, {"error": error, "retryable": can_retry})
        return status

    def cancel_in_transaction(self, connection, job_id: str) -> str:
        row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise JobQueueError("job does not exist")
        if row["status"] in TERMINAL_JOB_STATES:
            return row["status"]
        changed = connection.execute(
            "UPDATE jobs SET status='cancelled',cancel_requested=1,lease_owner=NULL,lease_token=NULL,"
            "lease_expires_at=NULL,error_json=?,updated_at=?,finished_at=? WHERE job_id=? "
            "AND status IN ('queued','running','retry_wait','blocked')",
            (_json({"code": "user_cancelled"}), _now(), _now(), job_id),
        ).rowcount
        if changed and row["current_attempt_id"]:
            connection.execute(
                "UPDATE job_attempts SET status='cancelled',finished_at=?,error_json=? "
                "WHERE attempt_id=? AND status='running'",
                (_now(), _json({"code": "user_cancelled"}), row["current_attempt_id"]),
            )
        if changed:
            self._event(connection, job_id, "cancelled", {})
            return "cancelled"
        return connection.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()[0]

    def for_run(self, connection, run_id: str):
        return connection.execute("SELECT * FROM jobs WHERE run_id=?", (run_id,)).fetchone()
