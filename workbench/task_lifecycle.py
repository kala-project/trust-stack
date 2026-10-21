"""Transactional persistence for durable task lifecycle transitions.

This module owns the application transaction that keeps a research run event,
the fenced queue attempt, and the run terminal state atomic.  Callers receive a
narrow capability; they do not receive a database connection or unrestricted
access to the chat application service.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import re
import sqlite3
import unicodedata
from typing import Callable, Protocol

from .job_queue import JobClaim, LeaseLost


@dataclass(frozen=True)
class TaskFailure:
    """Stable, serializable reason for a terminal task failure."""

    code: str
    message: str

    def __post_init__(self) -> None:
        if type(self.code) is not str or type(self.message) is not str:
            raise TypeError("task failure code and message must be strings")
        if not 1 <= len(self.code) <= 64 or self.code != self.code.strip():
            raise ValueError("task failure code must be a trimmed string of 1 to 64 characters")
        if re.fullmatch(r"[a-z][a-z0-9_]*", self.code, flags=re.ASCII) is None:
            raise ValueError("task failure code must use lowercase ASCII letters, digits, and underscores")
        if not 1 <= len(self.message) <= 1024 or self.message != self.message.strip():
            raise ValueError("task failure message must be a trimmed string of 1 to 1024 characters")
        if any(unicodedata.category(character) == "Cc" for character in self.message):
            raise ValueError("task failure message must not contain control characters")

    def payload(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class TaskTransitionOutcome(Enum):
    """Result of an idempotent, claim-fenced terminal transition."""

    APPLIED = "applied"
    CLAIM_NOT_CURRENT = "claim_not_current"


class TaskQueueTransitions(Protocol):
    """Restricted queue operations needed by the lifecycle transaction."""

    def assert_claim(self, connection: sqlite3.Connection, claim: JobClaim,
                     *, allow_blocked: bool = False) -> JobClaim: ...

    def fail_in_transaction(self, connection: sqlite3.Connection, claim: JobClaim,
                            error: dict, *, retryable: bool) -> str: ...


Connect = Callable[[], sqlite3.Connection]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class TaskLifecycleWrites:
    """Run/event writes injected into queue-owned SQLite transactions."""

    @staticmethod
    def expire_run(connection: sqlite3.Connection, run_id: str, *, retrying: bool,
                   error: dict) -> None:
        if retrying:
            connection.execute(
                "UPDATE runs SET status='queued',started_at=NULL,finished_at=NULL,error_json=NULL "
                "WHERE run_id=? AND status='running'", (run_id,))
            return
        connection.execute(
            "UPDATE runs SET status='failed',finished_at=?,error_json=? "
            "WHERE run_id=? AND status IN ('queued','running')",
            (_now(), _json(error), run_id),
        )

    @staticmethod
    def fail_deadline_run(connection: sqlite3.Connection, run_id: str, error: dict) -> None:
        changed = connection.execute(
            "UPDATE runs SET status='failed',error_json=?,finished_at=? "
            "WHERE run_id=? AND status IN ('queued','running')",
            (_json(error), _now(), run_id),
        ).rowcount
        if changed:
            TaskLifecycleRepository._insert_run_event(
                connection, run_id, "failed", {"error": error})


class TaskLifecycleRepository:
    """Public task-transition capability with exclusive transaction ownership."""

    def __init__(self, connect: Connect, queue: TaskQueueTransitions):
        self.__connect = connect
        self.__queue = queue

    @staticmethod
    def _insert_run_event(connection: sqlite3.Connection, run_id: str,
                          event_type: str, payload: dict) -> None:
        seq = connection.execute(
            "SELECT coalesce(max(seq),0)+1 FROM events WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?)",
            (run_id, seq, event_type, _json(payload), _now()),
        )

    def fail_claim(self, claim: JobClaim, failure: TaskFailure) -> TaskTransitionOutcome:
        """Atomically fail the current attempt and its run, or ignore a lost claim.

        Claim validation occurs after acquiring the SQLite writer lock.  A stale,
        cancelled, expired, or already-terminal claim is an idempotent no-op.
        Any event, queue, or run write exception rolls back the whole transition.
        Runtime-slot release remains the worker's responsibility because the
        executor may still be unwinding when this transaction completes.
        """
        if type(claim) is not JobClaim:
            raise TypeError("claim must be a JobClaim")
        if type(failure) is not TaskFailure:
            raise TypeError("failure must be a TaskFailure")
        error = failure.payload()
        try:
            with closing(self.__connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                persisted_claim = self.__queue.assert_claim(connection, claim)
                if persisted_claim.run_id is not None:
                    self._insert_run_event(
                        connection, persisted_claim.run_id, "failed", {"error": error})
                result = self.__queue.fail_in_transaction(
                    connection, persisted_claim, error, retryable=False)
                if persisted_claim.run_id is not None and result == "failed":
                    changed = connection.execute(
                        "UPDATE runs SET status='failed',error_json=?,finished_at=? "
                        "WHERE run_id=? AND status IN ('queued','running')",
                        (_json(error), _now(), persisted_claim.run_id),
                    ).rowcount
                    if changed != 1:
                        raise LeaseLost("claimed run is no longer transitionable")
        except LeaseLost:
            return TaskTransitionOutcome.CLAIM_NOT_CURRENT
        return TaskTransitionOutcome.APPLIED
