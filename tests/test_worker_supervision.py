"""Focused process-lifetime tests for production durable worker supervision."""
from contextlib import closing
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
import unittest

from tests.test_chat import FakeProvider
from workbench.build import build_database
from workbench.chat import ChatService
from workbench.worker import DurableWorker, SupervisionConfig
from workbench.verification_store import VerificationBudgetExceeded
from workbench.worker_process import WorkerProcessConfig, _safe_failure


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/source-catalog.json"


def _ready(control, parent_pid):
    if os.name == "posix":
        os.setsid()
        pgid = os.getpid()
    else:
        pgid = None
    control.send({"kind": "ready", "pid": os.getpid(), "pgid": pgid})


def hanging_executor(config, claim, cancel_event, start_event, control, parent_pid):
    _ready(control, parent_pid)
    start_event.wait(5)
    marker = config.get("documents_root")
    if marker:
        Path(marker).write_text(str(os.getpid()), encoding="ascii")
    if os.name == "posix":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.05)


def startup_crash_executor(config, claim, cancel_event, start_event, control, parent_pid):
    os._exit(17)


def abnormal_executor(config, claim, cancel_event, start_event, control, parent_pid):
    _ready(control, parent_pid)
    start_event.wait(5)
    os._exit(23)


@unittest.skipUnless(os.name == "posix", "process-group cleanup proof requires POSIX")
class WorkerSupervisionTests(unittest.TestCase):
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
        self.marker = Path(self.temp.name) / "executor.pid"
        self.service = ChatService(
            self.evidence_db, self.chat_db, FakeProvider(),
            job_lease_seconds=1.0, job_retry_delays=(0.0,),
        )
        conversation = self.service.create_conversation("MSFT")
        self.submitted = self.service.submit(
            conversation["conversation_id"], "supervise this", request_id="supervision-test")

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def worker(self, target, **policy):
        config = WorkerProcessConfig(
            str(self.evidence_db), str(self.chat_db), documents_root=str(self.marker))
        defaults = dict(
            hard_timeout_seconds=0.25, cancel_grace_seconds=0.1,
            terminate_grace_seconds=0.1, kill_grace_seconds=0.5,
            startup_timeout_seconds=0.5,
        )
        defaults.update(policy)
        return DurableWorker(
            self.service, heartbeat_seconds=0.05, process_config=config,
            supervision=SupervisionConfig(**defaults), process_target=target,
        )

    @staticmethod
    def assert_pid_gone(testcase, pid):
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.01)
        testcase.fail(f"executor PID {pid} is still alive")

    def test_verification_budget_failure_has_distinct_sanitized_code(self):
        failure = _safe_failure(
            VerificationBudgetExceeded("tool_result", "payload_bytes", 123), startup=False)
        self.assertEqual(failure, {
            "code": "verification_budget_exhausted",
            "message": "durable verification resource budget was exhausted",
        })

    def test_hung_executor_ignoring_cancel_and_term_is_killed_before_slot_release(self):
        started = time.monotonic()
        self.assertTrue(self.worker(hanging_executor).run_once())
        self.assertLess(time.monotonic() - started, 2.0)
        pid = int(self.marker.read_text(encoding="ascii"))
        self.assert_pid_gone(self, pid)
        with closing(self.service._connect()) as connection:
            job = connection.execute(
                "SELECT status,error_json FROM jobs WHERE run_id=?", (self.submitted["run_id"],)
            ).fetchone()
            slots = connection.execute("SELECT count(*) FROM model_runtime_slot").fetchone()[0]
            events = connection.execute(
                "SELECT count(*) FROM events WHERE run_id=? AND type='failed'",
                (self.submitted["run_id"],),
            ).fetchone()[0]
        self.assertEqual(job["status"], "failed")
        self.assertIn("executor_timeout", job["error_json"])
        self.assertEqual(slots, 0)
        self.assertEqual(events, 1)

    def test_cancel_converges_to_kill_and_second_worker_cannot_overlap(self):
        worker = self.worker(hanging_executor, hard_timeout_seconds=5.0)
        thread = threading.Thread(target=worker.run_once)
        thread.start()
        deadline = time.monotonic() + 2.0
        while not self.marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.marker.exists())
        pid = int(self.marker.read_text(encoding="ascii"))
        self.assertFalse(self.worker(abnormal_executor).run_once())
        self.assertEqual(self.service.cancel(self.submitted["run_id"])["status"], "cancelled")
        thread.join(2.0)
        self.assertFalse(thread.is_alive())
        self.assert_pid_gone(self, pid)
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM model_runtime_slot").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM events WHERE run_id=? AND type='failed'",
                (self.submitted["run_id"],)).fetchone()[0], 0)

    def test_child_startup_failure_is_sanitized_and_releases_slot(self):
        self.assertTrue(self.worker(startup_crash_executor).run_once())
        with closing(self.service._connect()) as connection:
            job = connection.execute(
                "SELECT status,error_json FROM jobs WHERE run_id=?", (self.submitted["run_id"],)
            ).fetchone()
            slots = connection.execute("SELECT count(*) FROM model_runtime_slot").fetchone()[0]
        self.assertEqual(job["status"], "failed")
        self.assertIn("executor_startup_error", job["error_json"])
        self.assertNotIn("17", job["error_json"])
        self.assertEqual(slots, 0)

    def test_abnormal_child_exit_has_one_terminal_transition_and_no_slot(self):
        self.assertTrue(self.worker(abnormal_executor).run_once())
        with closing(self.service._connect()) as connection:
            job = connection.execute(
                "SELECT status,error_json FROM jobs WHERE run_id=?", (self.submitted["run_id"],)
            ).fetchone()
            events = connection.execute(
                "SELECT count(*) FROM events WHERE run_id=? AND type='failed'",
                (self.submitted["run_id"],),
            ).fetchone()[0]
            slots = connection.execute("SELECT count(*) FROM model_runtime_slot").fetchone()[0]
        self.assertEqual(job["status"], "failed")
        self.assertIn("executor_abnormal_exit", job["error_json"])
        self.assertEqual(events, 1)
        self.assertEqual(slots, 0)

    def test_expired_supervisor_claim_cannot_reopen_slot_while_executor_group_lives(self):
        other_conversation = self.service.create_conversation("MSFT")
        other = self.service.submit(
            other_conversation["conversation_id"], "queued second", request_id="second-job")
        now = [10_000_000_000.0]
        self.service.jobs._clock = lambda: now[0]
        claim = self.service.jobs.claim("supervisor-owner")
        ready_read, ready_write = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(ready_read)
            os.setsid()
            os.write(ready_write, b"1")
            os.close(ready_write)
            while True:
                time.sleep(1)
        os.close(ready_write)
        try:
            self.assertEqual(os.read(ready_read, 1), b"1")
            self.service.jobs.bind_executor_process(claim, child_pid, child_pid)
            now[0] += 2.0
            self.assertIsNone(self.service.jobs.claim("replacement"))
            with closing(self.service._connect()) as connection:
                slot = connection.execute(
                    "SELECT worker_pid,executor_pgid FROM model_runtime_slot WHERE slot_id=1"
                ).fetchone()
            self.assertEqual((slot["worker_pid"], slot["executor_pgid"]), (child_pid, child_pid))
        finally:
            os.close(ready_read)
            try:
                os.killpg(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child_pid, 0)
        replacement = self.service.jobs.claim("replacement")
        self.assertIsNotNone(replacement)
        self.assertEqual(replacement.run_id, other["run_id"])


if __name__ == "__main__":
    unittest.main()
