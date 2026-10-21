"""Focused transaction and isolation tests for durable task lifecycle persistence."""

from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from tests.test_chat import FakeProvider
from workbench.build import build_database
from workbench.chat import ChatService
from workbench.task_lifecycle import TaskFailure, TaskTransitionOutcome
from workbench.worker import DurableWorker


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/source-catalog.json"
FAILURE = TaskFailure("worker_execution_error", "executor setup failed")


class TaskLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.evidence_db = Path(cls.fixture.name) / "evidence.sqlite3"
        build_database(CATALOG, cls.evidence_db, ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.chat_db = Path(self.temp.name) / "chat.sqlite3"
        self.service = ChatService(
            self.evidence_db, self.chat_db, FakeProvider(),
            job_lease_seconds=1.0, job_retry_delays=(0.0, 0.0),
        )
        conversation = self.service.create_conversation("MSFT")
        self.submitted = self.service.submit(
            conversation["conversation_id"], "research this", request_id="lifecycle-test")

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def claim(self, worker_id="lifecycle-worker", **kwargs):
        claim = self.service.jobs.claim(worker_id, **kwargs)
        self.assertIsNotNone(claim)
        return claim

    def test_worker_setup_failure_uses_atomic_public_terminal_transition(self):
        def fail_setup(_claim, _cancel_event):
            raise RuntimeError("controlled executor setup failure")

        self.service.execute_durable_claim = fail_setup
        worker = DurableWorker(self.service, heartbeat_seconds=0.1)
        self.assertTrue(worker.run_once())

        with closing(self.service._connect()) as connection:
            job = connection.execute(
                "SELECT status,error_json FROM jobs WHERE run_id=?", (self.submitted["run_id"],)
            ).fetchone()
            attempt = connection.execute(
                "SELECT status FROM job_attempts WHERE job_id=(SELECT job_id FROM jobs WHERE run_id=?)",
                (self.submitted["run_id"],),
            ).fetchone()
            failed_events = connection.execute(
                "SELECT count(*) FROM events WHERE run_id=? AND type='failed'",
                (self.submitted["run_id"],),
            ).fetchone()[0]
            slots = connection.execute("SELECT count(*) FROM model_runtime_slot").fetchone()[0]
        self.assertEqual(job["status"], "failed")
        self.assertIn("worker_execution_error", job["error_json"])
        self.assertEqual(attempt["status"], "failed")
        self.assertEqual(failed_events, 1)
        self.assertEqual(slots, 0)
        self.assertEqual(self.service.get_run(self.submitted["run_id"])["status"], "failed")

    def test_stale_attempt_cannot_apply_terminal_transition(self):
        now = [10_000_000_000.0]
        self.service.jobs._clock = lambda: now[0]
        old = self.claim("stale-worker", worker_pid=99_999_999)
        now[0] += 2.0
        current = self.claim("replacement-worker")

        outcome = self.service.task_lifecycle.fail_claim(old, FAILURE)

        self.assertEqual(outcome, TaskTransitionOutcome.CLAIM_NOT_CURRENT)
        with closing(self.service._connect()) as connection:
            job = connection.execute(
                "SELECT status,current_attempt_id FROM jobs WHERE job_id=?", (old.job_id,)
            ).fetchone()
            failed_events = connection.execute(
                "SELECT count(*) FROM events WHERE run_id=? AND type='failed'", (old.run_id,)
            ).fetchone()[0]
        self.assertEqual((job["status"], job["current_attempt_id"]), ("running", current.attempt_id))
        self.assertEqual(failed_events, 0)

    def test_cancelled_job_rejects_late_terminal_transition(self):
        claim = self.claim("cancelled-worker")
        self.assertEqual(self.service.cancel(self.submitted["run_id"])["status"], "cancelled")

        outcome = self.service.task_lifecycle.fail_claim(claim, FAILURE)

        self.assertEqual(outcome, TaskTransitionOutcome.CLAIM_NOT_CURRENT)
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (claim.job_id,)
            ).fetchone()[0], "cancelled")
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM events WHERE run_id=? AND type='failed'", (claim.run_id,)
            ).fetchone()[0], 0)

    def test_duplicate_terminal_calls_are_idempotent(self):
        claim = self.claim("duplicate-worker")

        first = self.service.task_lifecycle.fail_claim(claim, FAILURE)
        second = self.service.task_lifecycle.fail_claim(claim, FAILURE)

        self.assertEqual(first, TaskTransitionOutcome.APPLIED)
        self.assertEqual(second, TaskTransitionOutcome.CLAIM_NOT_CURRENT)
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM events WHERE run_id=? AND type='failed'", (claim.run_id,)
            ).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT status FROM job_attempts WHERE attempt_id=?", (claim.attempt_id,)
            ).fetchone()[0], "failed")

    def test_queue_transition_exception_rolls_back_event_and_state(self):
        claim = self.claim("transition-rollback-worker")
        original = self.service.jobs.fail_in_transaction

        def fail_after_transition(connection, current_claim, error, *, retryable):
            original(connection, current_claim, error, retryable=retryable)
            raise RuntimeError("controlled transition failure")

        self.service.jobs.fail_in_transaction = fail_after_transition
        try:
            with self.assertRaisesRegex(RuntimeError, "controlled transition failure"):
                self.service.task_lifecycle.fail_claim(claim, FAILURE)
        finally:
            self.service.jobs.fail_in_transaction = original

        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (claim.job_id,)
            ).fetchone()[0], "running")
            self.assertEqual(connection.execute(
                "SELECT status FROM job_attempts WHERE attempt_id=?", (claim.attempt_id,)
            ).fetchone()[0], "running")
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM events WHERE run_id=? AND type='failed'", (claim.run_id,)
            ).fetchone()[0], 0)
        self.assertEqual(self.service.get_run(claim.run_id)["status"], "queued")

    def test_event_exception_rolls_back_before_queue_transition(self):
        claim = self.claim("event-rollback-worker")
        with closing(self.service._connect()) as connection, connection:
            connection.execute(
                "CREATE TRIGGER reject_failed_event BEFORE INSERT ON events "
                "WHEN NEW.type='failed' BEGIN SELECT RAISE(ABORT,'controlled event failure'); END"
            )

        with self.assertRaisesRegex(sqlite3.IntegrityError, "controlled event failure"):
            self.service.task_lifecycle.fail_claim(claim, FAILURE)

        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (claim.job_id,)
            ).fetchone()[0], "running")
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM events WHERE run_id=? AND type='failed'", (claim.run_id,)
            ).fetchone()[0], 0)

    def test_every_forged_claim_identity_field_is_rejected_without_writes(self):
        other_conversation = self.service.create_conversation("MSFT")
        other = self.service.submit(
            other_conversation["conversation_id"], "other research", request_id="other-run")
        claim = self.claim("identity-worker")
        variants = {
            "job_id": replace(claim, job_id="job_forged"),
            "job_type": replace(claim, job_type="verify"),
            "run_id": replace(claim, run_id=other["run_id"]),
            "run_id_none": replace(claim, run_id=None),
            "conversation_id": replace(claim, conversation_id=other["conversation_id"]),
            "attempt_id": replace(claim, attempt_id="attempt_forged"),
            "attempt_number": replace(claim, attempt_number=claim.attempt_number + 1),
            "worker_id": replace(claim, worker_id="forged-worker"),
            "lease_expires_at": replace(claim, lease_expires_at=claim.lease_expires_at - .25),
        }

        for field, forged in variants.items():
            with self.subTest(field=field):
                self.assertEqual(
                    self.service.task_lifecycle.fail_claim(forged, FAILURE),
                    TaskTransitionOutcome.CLAIM_NOT_CURRENT,
                )

        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (claim.job_id,)
            ).fetchone()[0], "running")
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM events WHERE run_id=? AND type='failed'", (claim.run_id,)
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT status FROM runs WHERE run_id=?", (other["run_id"],)
            ).fetchone()[0], "queued")

    def test_original_claim_identity_remains_valid_after_heartbeat_extension(self):
        claim = self.claim("heartbeat-identity-worker")
        now = [claim.lease_expires_at - self.service.jobs.lease_seconds + .25]
        self.service.jobs._clock = lambda: now[0]
        self.assertEqual(self.service.jobs.heartbeat(claim), (True, True))

        self.assertEqual(
            self.service.task_lifecycle.fail_claim(claim, FAILURE),
            TaskTransitionOutcome.APPLIED,
        )

    def test_verify_claim_with_persisted_null_run_is_valid(self):
        conversation = self.service.create_conversation("MSFT")
        with closing(self.service._connect()) as connection, connection:
            job = self.service.jobs.enqueue_verify(
                connection, conversation_id=conversation["conversation_id"],
                request_identity="verify-lifecycle", input_hash="verify-input")
        claim = self.claim("verify-worker", job_types=("verify",))
        self.assertIsNone(claim.run_id)

        self.assertEqual(
            self.service.task_lifecycle.fail_claim(claim, FAILURE),
            TaskTransitionOutcome.APPLIED,
        )
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (job["job_id"],)
            ).fetchone()[0], "failed")

    def test_run_transition_mismatch_rolls_back_queue_and_event_writes(self):
        claim = self.claim("run-mismatch-worker")
        with closing(self.service._connect()) as connection, connection:
            connection.execute(
                "UPDATE runs SET status='completed',finished_at='test' WHERE run_id=?",
                (claim.run_id,),
            )

        self.assertEqual(
            self.service.task_lifecycle.fail_claim(claim, FAILURE),
            TaskTransitionOutcome.CLAIM_NOT_CURRENT,
        )
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (claim.job_id,)
            ).fetchone()[0], "running")
            self.assertEqual(connection.execute(
                "SELECT status FROM job_attempts WHERE attempt_id=?", (claim.attempt_id,)
            ).fetchone()[0], "running")
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM events WHERE run_id=? AND type='failed'", (claim.run_id,)
            ).fetchone()[0], 0)

    def test_deadline_run_event_failure_rolls_back_queue_and_run(self):
        claim = self.claim("deadline-rollback-worker")
        now = [claim.lease_expires_at - self.service.jobs.lease_seconds]
        self.service.jobs._clock = lambda: now[0]
        with closing(self.service._connect()) as connection, connection:
            connection.execute(
                "UPDATE jobs SET deadline_at=? WHERE job_id=?", (now[0] + .5, claim.job_id))
            connection.execute(
                "CREATE TRIGGER reject_deadline_event BEFORE INSERT ON events "
                "WHEN NEW.type='failed' BEGIN SELECT RAISE(ABORT,'deadline event failure'); END"
            )
        now[0] += 1.0

        with self.assertRaisesRegex(sqlite3.IntegrityError, "deadline event failure"):
            self.service.jobs.heartbeat(claim)

        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (claim.job_id,)
            ).fetchone()[0], "running")
            self.assertEqual(connection.execute(
                "SELECT status FROM runs WHERE run_id=?", (claim.run_id,)
            ).fetchone()[0], "queued")
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM job_events WHERE job_id=? AND event_type='failed'",
                (claim.job_id,),
            ).fetchone()[0], 0)

    def test_repository_rejects_wrong_argument_types_before_writing(self):
        claim = self.claim("typed-repository-worker")
        with self.assertRaisesRegex(TypeError, "claim must be a JobClaim"):
            self.service.task_lifecycle.fail_claim({"job_id": claim.job_id}, FAILURE)
        with self.assertRaisesRegex(TypeError, "failure must be a TaskFailure"):
            self.service.task_lifecycle.fail_claim(claim, {"code": "unsafe"})
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (claim.job_id,)
            ).fetchone()[0], "running")
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM events WHERE run_id=? AND type='failed'", (claim.run_id,)
            ).fetchone()[0], 0)

    def test_task_failure_requires_bounded_safe_exact_strings(self):
        invalid = (
            ([], "message", TypeError),
            ("code", {}, TypeError),
            ("", "message", ValueError),
            (" bad", "message", ValueError),
            ("BadCode", "message", ValueError),
            ("bad-code", "message", ValueError),
            ("a" * 65, "message", ValueError),
            ("code", " ", ValueError),
            ("code", " padded ", ValueError),
            ("code", "line\nbreak", ValueError),
            ("code", "x" * 1025, ValueError),
        )
        for code, message, exception in invalid:
            with self.subTest(code=code, message_type=type(message).__name__):
                with self.assertRaises(exception):
                    TaskFailure(code, message)
        self.assertEqual(
            TaskFailure("safe_code_2", "Safe public message").payload(),
            {"code": "safe_code_2", "message": "Safe public message"},
        )

    def test_worker_retains_only_narrow_capabilities(self):
        worker = DurableWorker(self.service, heartbeat_seconds=.1)
        self.assertFalse(hasattr(worker, "service"))
        self.assertFalse(hasattr(worker, "executor"))
        self.assertFalse(hasattr(worker.capabilities, "_connect"))
        self.assertFalse(hasattr(worker.jobs, "_connect"))
        self.assertFalse(hasattr(worker.task_lifecycle, "_connect"))
        self.assertFalse(hasattr(worker.capabilities.execution, "_connect"))


class WorkerImportIsolationTests(unittest.TestCase):
    def test_fresh_worker_import_does_not_load_chat_runtime_or_http_server(self):
        script = (
            "import sys; import workbench.worker; "
            "blocked=[name for name in ('workbench.chat','workbench.runtime','workbench.server') "
            "if name in sys.modules]; "
            "assert not blocked, blocked"
        )
        result = subprocess.run(
            [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
