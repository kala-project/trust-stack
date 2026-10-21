from contextlib import closing
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import socket

from workbench.build import build_database
from workbench.chat import ChatError, ChatService
from workbench.job_queue import JobQueue, LeaseLost
from workbench.worker import DurableWorker
from tests.test_chat import FakeProvider


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/source-catalog.json"


class DurableJobTests(unittest.TestCase):
    def test_pid_probe_only_proven_exit_allows_reclaim(self):
        for error, gone in ((ProcessLookupError(), True), (PermissionError(), False),
                            (OSError("indeterminate"), False), (ValueError(), False)):
            with self.subTest(error=type(error).__name__), patch(
                    "workbench.job_queue.os.kill", side_effect=error):
                self.assertEqual(JobQueue._process_is_gone(socket.gethostname(), 12345), gone)
                self.assertEqual(JobQueue._executor_is_gone(socket.gethostname(), 12345, None), gone)

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
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.close()
        self.temp.cleanup()

    def service(self, provider=None, **kwargs):
        service = ChatService(
            self.evidence_db, self.chat_db, provider or FakeProvider(),
            job_lease_seconds=1.0,
            job_retry_delays=(0.0, 0.0), **kwargs)
        self.services.append(service)
        return service

    def queued_submission(self, service, *, request_id="durable-request-1", message="research this"):
        conversation = service.create_conversation("MSFT")
        result = service.submit(conversation["conversation_id"], message, request_id=request_id)
        self.assertEqual(result["status"], "queued")
        return result

    def test_submit_worker_requires_structured_candidate_and_is_idempotent(self):
        api = self.service(FakeProvider())
        submitted = self.queued_submission(api)
        replay = api.submit(
            submitted["conversation_id"], "research this", request_id="durable-request-1")
        self.assertEqual(replay, submitted)
        with closing(api._connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE run_id=?", (submitted["run_id"],)).fetchone()[0], "queued")

        worker_service = self.service(FakeProvider("unstructured output"))
        worker = DurableWorker(worker_service, worker_id="test-worker", heartbeat_seconds=0.1)
        self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())

        run = api.get_run(submitted["run_id"])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"]["code"], "structured_candidate_required")
        with closing(api._connect()) as connection:
            job = connection.execute(
                "SELECT status,attempt_count FROM jobs WHERE run_id=?", (submitted["run_id"],)).fetchone()
            self.assertEqual((job["status"], job["attempt_count"]), ("failed", 1))
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE run_id=? AND role='assistant'",
                (submitted["run_id"],)).fetchone()[0], 0)

    def test_competing_claims_observe_global_single_model_slot(self):
        service = self.service()
        first = self.queued_submission(service, request_id="claim-one", message="first research")
        second_conversation = service.create_conversation("MSFT")
        second = service.submit(
            second_conversation["conversation_id"], "second research", request_id="claim-two")

        claim = service.jobs.claim("worker-one")
        self.assertEqual(claim.run_id, first["run_id"])
        self.assertIsNone(service.jobs.claim("worker-two"))
        with closing(service._connect()) as connection, connection:
            service.jobs.assert_claim(connection, claim)
            service.jobs.fail_in_transaction(
                connection, claim, {"code": "test_terminal"}, retryable=False)
            connection.execute(
                "UPDATE runs SET status='failed',finished_at='test' WHERE run_id=?", (first["run_id"],))
        service.jobs.release_runtime_slot(claim)
        next_claim = service.jobs.claim("worker-two")
        self.assertEqual(next_claim.run_id, second["run_id"])

    def test_dead_orphan_is_reclaimed_and_old_attempt_is_fenced(self):
        service = self.service()
        submitted = self.queued_submission(service, request_id="orphan-request")
        now = [10_000_000_000.0]
        service.jobs._clock = lambda: now[0]
        old = service.jobs.claim("dead-worker", worker_pid=99999999)
        self.assertIsNotNone(old)
        now[0] += 2.0
        current = service.jobs.claim("replacement-worker")
        self.assertEqual(current.job_id, old.job_id)
        self.assertEqual(current.attempt_number, 2)
        with closing(service._connect()) as connection, connection:
            with self.assertRaises(LeaseLost):
                service.jobs.succeed_in_transaction(connection, old)
            states = connection.execute(
                "SELECT status FROM job_attempts WHERE job_id=? ORDER BY attempt_number",
                (old.job_id,)).fetchall()
            self.assertEqual([row["status"] for row in states], ["stale", "running"])
        self.assertEqual(service.get_run(submitted["run_id"])["status"], "queued")

    def test_expired_live_worker_is_not_killed_or_unsafely_retried(self):
        service = self.service()
        submitted = self.queued_submission(service, request_id="live-expiry")
        now = [10_000_000_000.0]
        service.jobs._clock = lambda: now[0]
        claim = service.jobs.claim("unconfirmed-worker", worker_pid=os.getpid())
        now[0] += 2.0
        self.assertIsNone(service.jobs.claim("other-worker"))
        with closing(service._connect()) as connection:
            job = connection.execute("SELECT status,error_json FROM jobs WHERE job_id=?", (claim.job_id,)).fetchone()
        self.assertEqual(job["status"], "failed")
        self.assertIn("recovery_unconfirmed", job["error_json"])
        self.assertEqual(service.get_run(submitted["run_id"])["status"], "failed")

    def test_cancel_wins_against_stale_attempt_commit(self):
        service = self.service()
        submitted = self.queued_submission(service, request_id="cancel-request")
        other_conversation = service.create_conversation("MSFT")
        other = service.submit(
            other_conversation["conversation_id"], "queued behind cancellation",
            request_id="cancel-following-request")
        claim = service.jobs.claim("cancelled-worker")
        cancelled = service.cancel(submitted["run_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        with closing(service._connect()) as connection, connection:
            with self.assertRaises(LeaseLost):
                service.jobs.succeed_in_transaction(connection, claim)
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (claim.job_id,)).fetchone()[0], "cancelled")
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE run_id=? AND role='assistant'",
                (submitted["run_id"],)).fetchone()[0], 0)
        self.assertIsNone(service.jobs.claim("too-early-worker"))
        service.jobs.release_runtime_slot(claim)
        self.assertEqual(service.jobs.claim("following-worker").run_id, other["run_id"])

    def test_same_service_management_cancel_does_not_use_active_claim(self):
        service = self.service()
        submitted = self.queued_submission(service, request_id="same-service-cancel")
        claim = service.jobs.claim("same-service-worker")
        service._durable_claim = claim
        try:
            result = service.cancel(submitted["run_id"])
        finally:
            service._durable_claim = None
        self.assertEqual(result["status"], "cancelled")
        with closing(service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (claim.job_id,)).fetchone()[0],
                "cancelled")
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM events WHERE run_id=? AND type='cancelled'",
                (submitted["run_id"],)).fetchone()[0], 1)

    def test_late_callback_keeps_original_claim_after_new_attempt_starts(self):
        service = self.service()
        submitted = self.queued_submission(service, request_id="late-callback")
        old = service.jobs.claim("attempt-one")
        late_callback = lambda: service._record_tool_completion(
            submitted["run_id"], call_id="late-call", tool_name="late",
            arguments={}, result={"value": "stale"}, claim=old,
        )
        with closing(service._connect()) as connection, connection:
            service.jobs.fail_in_transaction(
                connection, old, {"code": "provider_error"}, retryable=True)
        service.jobs.release_runtime_slot(old)
        current = service.jobs.claim("attempt-two")
        self.assertIsNotNone(current)
        service._durable_claim = current
        try:
            with self.assertRaises(LeaseLost):
                late_callback()
        finally:
            service._durable_claim = None
        with closing(service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM events WHERE run_id=? AND type='tool_result'",
                (submitted["run_id"],)).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT current_attempt_id FROM jobs WHERE job_id=?", (old.job_id,)
            ).fetchone()[0], current.attempt_id)

    def test_confirmation_transition_blocks_job_and_releases_model_slot(self):
        service = self.service()
        submitted = self.queued_submission(service, request_id="blocked-request")
        claim = service.jobs.claim("decision-worker")
        service._durable_claim = claim
        try:
            with closing(service._connect()) as connection, connection:
                service.jobs.assert_claim(connection, claim)
                connection.execute(
                    "UPDATE runs SET status='running',started_at='test' WHERE run_id=? AND status='queued'",
                    (submitted["run_id"],))
                service._insert_event(
                    connection, submitted["run_id"], "running", {}, claim=claim)
            with closing(service._connect()) as connection, connection:
                connection.execute(
                    "UPDATE runs SET status='blocked',error_json='{}',finished_at='test' "
                    "WHERE run_id=?", (submitted["run_id"],))
                service._insert_event(
                    connection, submitted["run_id"], "confirmation_required",
                    {"decision_id": "decision_test", "authoritative": True}, claim=claim)

        finally:
            service._durable_claim = None
        self.assertEqual(service.get_run(submitted["run_id"])["status"], "blocked")
        with closing(service._connect()) as connection:
            job = connection.execute(
                "SELECT status,lease_token FROM jobs WHERE job_id=?", (claim.job_id,)).fetchone()
            attempt = connection.execute(
                "SELECT status FROM job_attempts WHERE attempt_id=?", (claim.attempt_id,)).fetchone()
        self.assertEqual((job["status"], job["lease_token"]), ("blocked", None))
        self.assertEqual(attempt["status"], "blocked")

    def test_retryable_provider_failure_is_bounded_and_can_succeed_once(self):
        api = self.service(FakeProvider())
        submitted = self.queued_submission(api, request_id="retry-request")
        failing = self.service(FakeProvider(fail=True))
        first_worker = DurableWorker(failing, worker_id="retry-one", heartbeat_seconds=0.1)
        self.assertTrue(first_worker.run_once())
        with closing(api._connect()) as connection:
            first = connection.execute(
                "SELECT status,attempt_count FROM jobs WHERE run_id=?", (submitted["run_id"],)).fetchone()
        self.assertEqual((first["status"], first["attempt_count"]), ("retry_wait", 1))
        self.assertEqual(api.get_run(submitted["run_id"])["status"], "queued")

        succeeding = self.service(FakeProvider("report after retry"))
        second_worker = DurableWorker(succeeding, worker_id="retry-two", heartbeat_seconds=0.1)
        self.assertTrue(second_worker.run_once())
        with closing(api._connect()) as connection:
            job = connection.execute(
                "SELECT status,attempt_count FROM jobs WHERE run_id=?", (submitted["run_id"],)).fetchone()
            attempts = connection.execute(
                "SELECT status FROM job_attempts WHERE job_id=(SELECT job_id FROM jobs WHERE run_id=?) "
                "ORDER BY attempt_number", (submitted["run_id"],)).fetchall()
        self.assertEqual((job["status"], job["attempt_count"]), ("failed", 2))
        self.assertEqual([row["status"] for row in attempts], ["retry_wait", "failed"])
        self.assertEqual(api.get_run(submitted["run_id"])["status"], "failed")
        self.assertEqual(
            api.get_run(submitted["run_id"])["error"]["code"],
            "structured_candidate_required",
        )

    def test_expired_queued_deadline_is_failed_before_claim_and_releases_conversation(self):
        service = self.service()
        submitted = self.queued_submission(service, request_id="queued-deadline")
        now = [1000.0]
        service.jobs._clock = lambda: now[0]
        with closing(service._connect()) as connection, connection:
            connection.execute(
                "UPDATE jobs SET deadline_at=? WHERE run_id=?", (now[0] - 1, submitted["run_id"]))
        self.assertIsNone(service.jobs.claim("deadline-worker"))
        with closing(service._connect()) as connection:
            job = connection.execute(
                "SELECT status,error_json FROM jobs WHERE run_id=?", (submitted["run_id"],)
            ).fetchone()
            failed_events = connection.execute(
                "SELECT count(*) FROM events WHERE run_id=? AND type='failed'",
                (submitted["run_id"],),
            ).fetchone()[0]
        self.assertEqual(job["status"], "failed")
        self.assertIn("deadline_exceeded", job["error_json"])
        self.assertEqual(service.get_run(submitted["run_id"])["status"], "failed")
        self.assertEqual(failed_events, 1)
        replay = service.submit(
            submitted["conversation_id"], "after deadline", request_id="after-deadline")
        self.assertEqual(replay["status"], "queued")

    def test_retry_delay_crossing_deadline_fails_in_writer_transaction(self):
        service = self.service()
        service.jobs.retry_delays = (10.0,)
        now = [2000.0]
        service.jobs._clock = lambda: now[0]
        submitted = self.queued_submission(service, request_id="retry-deadline")
        with closing(service._connect()) as connection, connection:
            connection.execute(
                "UPDATE jobs SET deadline_at=? WHERE run_id=?", (now[0] + 5, submitted["run_id"]))
        claim = service.jobs.claim("retry-deadline-worker")
        with closing(service._connect()) as connection, connection:
            connection.execute(
                "UPDATE runs SET status='running' WHERE run_id=?", (submitted["run_id"],))
        service._finish(
            submitted["run_id"], "failed", {"code": "provider_error", "message": "failed"},
            claim=claim,
        )
        with closing(service._connect()) as connection:
            job = connection.execute(
                "SELECT status,available_at FROM jobs WHERE job_id=?", (claim.job_id,)
            ).fetchone()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["available_at"], now[0])
        self.assertEqual(service.get_run(submitted["run_id"])["status"], "failed")

    def test_heartbeat_crossing_deadline_fails_job_without_releasing_or_reviving_slot(self):
        service = self.service()
        now = [3000.0]
        service.jobs._clock = lambda: now[0]
        submitted = self.queued_submission(service, request_id="heartbeat-deadline")
        with closing(service._connect()) as connection, connection:
            connection.execute(
                "UPDATE jobs SET deadline_at=? WHERE run_id=?", (now[0] + 0.5, submitted["run_id"]))
        claim = service.jobs.claim("deadline-heartbeat-worker")
        now[0] += 0.25
        self.assertEqual(service.jobs.heartbeat(claim), (True, True))
        with closing(service._connect()) as connection:
            extended = connection.execute(
                "SELECT lease_expires_at FROM model_runtime_slot WHERE slot_id=1").fetchone()[0]
        now[0] += 0.5
        self.assertEqual(service.jobs.heartbeat(claim), (False, False))
        with closing(service._connect()) as connection:
            slot = connection.execute(
                "SELECT lease_expires_at FROM model_runtime_slot WHERE slot_id=1").fetchone()
            job = connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (claim.job_id,)).fetchone()[0]
        self.assertIsNotNone(slot)
        self.assertEqual(slot["lease_expires_at"], extended)
        self.assertEqual(job, "failed")
        self.assertEqual(service.get_run(submitted["run_id"])["status"], "failed")

    def test_worker_heartbeat_stops_when_job_is_cancelled_and_does_not_renew_slot(self):
        service = self.service()
        submitted = self.queued_submission(service, request_id="heartbeat-cancel")
        claim = service.jobs.claim("heartbeat-cancel-worker")
        with closing(service._connect()) as connection:
            before = connection.execute(
                "SELECT lease_expires_at FROM model_runtime_slot WHERE slot_id=1").fetchone()[0]
        with closing(service._connect()) as connection, connection:
            service.jobs.cancel_in_transaction(connection, claim.job_id)
        worker = DurableWorker(service, worker_id="heartbeat-test", heartbeat_seconds=0.01)
        done_event = threading.Event()
        cancel_event = threading.Event()
        thread = threading.Thread(
            target=worker._heartbeat, args=(claim, done_event, cancel_event), daemon=True)
        thread.start()
        thread.join(0.5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(cancel_event.is_set())
        with closing(service._connect()) as connection:
            after = connection.execute(
                "SELECT lease_expires_at FROM model_runtime_slot WHERE slot_id=1").fetchone()[0]
        self.assertEqual(after, before)

    def test_durable_constructor_does_not_fail_another_workers_job(self):
        first = self.service()
        submitted = self.queued_submission(first, request_id="constructor-request")
        second = self.service()
        self.assertEqual(second.get_run(submitted["run_id"])["status"], "queued")
        with closing(second._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE run_id=?", (submitted["run_id"],)).fetchone()[0], "queued")

    def test_worker_claims_required_verification_jobs(self):
        service = self.service()
        conversation = service.create_conversation("MSFT")
        with closing(service._connect()) as connection, connection:
            job = service.jobs.enqueue_verify(
                connection, conversation_id=conversation["conversation_id"],
                request_identity="verify-future-1", input_hash="candidate-hash")
        self.assertEqual(job["job_type"], "verify")
        worker = DurableWorker(service, worker_id="required-verifier", heartbeat_seconds=0.1)
        self.assertTrue(worker.run_once())
        with closing(service._connect()) as connection:
            stored = connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (job["job_id"],)
            ).fetchone()[0]
        self.assertEqual(stored, "failed")


if __name__ == "__main__":
    unittest.main()
