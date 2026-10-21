from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from workbench.build import build_database
from workbench.chat import ChatService
from workbench.research_tools import ToolError
from tests.support_chat_provider import ExplicitMSFTIntentAdapter


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/source-catalog.json"
TERMINAL = {"completed", "failed", "cancelled", "blocked"}


class BaseProvider(ExplicitMSFTIntentAdapter):
    def status(self):
        return {"available": True, "reason": "", "provider": "termination-test"}


class SwallowingLimitProvider(BaseProvider):
    """Reproduces an SDK loop that catches ToolError and then ends its stream."""

    def stream(self, messages, tools, cancel_event):
        handler = next(item["handler"] for item in tools if item["name"] == "get_company_facts")
        while True:
            try:
                handler(metric="revenue")
            except ToolError:
                return
        yield  # pragma: no cover


class ConcurrentLimitProvider(BaseProvider):
    def __init__(self):
        self.results = []
        self.errors = []
        self.unexpected = []
        self.finished = threading.Event()

    def stream(self, messages, tools, cancel_event):
        handler = next(item["handler"] for item in tools if item["name"] == "get_company_facts")

        def invoke(_):
            try:
                result = handler(metric="revenue")
                return "result", result["_tool_budget"]
            except ToolError as exc:
                return "error", exc.code
            except Exception as exc:  # surfaced explicitly by the assertion below
                return "unexpected", repr(exc)

        with ThreadPoolExecutor(max_workers=12) as pool:
            outcomes = list(pool.map(invoke, range(20)))
        self.results = [value for kind, value in outcomes if kind == "result"]
        self.errors = [value for kind, value in outcomes if kind == "error"]
        self.unexpected = [value for kind, value in outcomes if kind == "unexpected"]
        self.finished.set()
        return
        yield  # pragma: no cover


class BlockingProvider(BaseProvider):
    def __init__(self):
        self.started = threading.Event()

    def stream(self, messages, tools, cancel_event):
        self.started.set()
        cancel_event.wait(2)
        return
        yield  # pragma: no cover


class InterruptingProvider(BaseProvider):
    def stream(self, messages, tools, cancel_event):
        cancel_event.set()
        return
        yield  # pragma: no cover


class RunTerminationTests(unittest.TestCase):
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
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.close()
        self.temp.cleanup()

    def service(self, provider):
        service = ChatService(
            self.evidence_db, Path(self.temp.name) / "chat.sqlite3", provider
        )
        self.services.append(service)
        return service

    def wait(self, service, run_id, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = service.get_run(run_id)
            if result["status"] in TERMINAL:
                return result
            time.sleep(0.01)
        self.fail("run did not terminate")

    def submit(self, service):
        conversation = service.create_conversation("MSFT")
        return service.submit(conversation["conversation_id"], "What was Microsoft revenue?")

    def test_swallowed_tool_error_preserves_originating_limit_failure(self):
        service = self.service(SwallowingLimitProvider())
        service.max_tool_calls = 3
        submitted = self.submit(service)
        result = self.wait(service, submitted["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "tool_call_limit")
        self.assertEqual(result["termination_reason"], "tool_call_limit")
        self.assertFalse(result["cancel_requested"])
        self.assertEqual(service.max_tool_calls, 3)
        summaries = service.get_conversation(submitted["conversation_id"])["research_scope"]["runs"]
        summary = next(item for item in summaries if item["run_id"] == submitted["run_id"])
        self.assertEqual(summary["termination_reason"], "tool_call_limit")
        self.assertFalse(summary["cancel_requested"])

    def test_concurrent_tool_calls_use_one_thread_safe_shared_counter(self):
        provider = ConcurrentLimitProvider()
        service = self.service(provider)
        service.max_tool_calls = 4
        result = self.wait(service, self.submit(service)["run_id"])
        self.assertEqual(result["termination_reason"], "tool_call_limit")
        self.assertTrue(provider.finished.wait(2))
        used = [item["used"] for item in provider.results]
        self.assertEqual(len(used), len(set(used)))
        self.assertTrue(all(1 <= count <= service.max_tool_calls for count in used))
        self.assertEqual(provider.unexpected, [])
        self.assertEqual(len(provider.results) + len(provider.errors), 20)
        self.assertIn("tool_call_limit", provider.errors)
        self.assertTrue(all(code == "tool_call_limit" for code in provider.errors))

    def test_explicit_user_cancel_is_attributed_and_never_rewritten(self):
        provider = BlockingProvider()
        service = self.service(provider)
        run = self.submit(service)
        self.assertTrue(provider.started.wait(1))
        service.cancel(run["run_id"])
        result = self.wait(service, run["run_id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["termination_reason"], "user_cancelled")
        self.assertTrue(result["cancel_requested"])

    def test_timeout_and_other_failure_race_keeps_one_complete_reason(self):
        provider = BlockingProvider()
        service = self.service(provider)
        run = self.submit(service)
        self.assertTrue(provider.started.wait(1))
        event = service._cancel_events[run["run_id"]]
        barrier = threading.Barrier(3)

        def timeout():
            barrier.wait()
            service._timeout_run(run["run_id"], event)

        def limit():
            barrier.wait()
            service._fail_and_stop(run["run_id"], event, "tool_call_limit", "tool call limit reached")

        threads = [threading.Thread(target=timeout), threading.Thread(target=limit)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        result = self.wait(service, run["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn(result["termination_reason"], {"timeout", "tool_call_limit"})
        self.assertEqual(result["termination_reason"], result["error"]["code"])
        terminal_events = [event for event in result["events"] if event["type"] == "failed"]
        self.assertEqual(len(terminal_events), 1)

    def test_unattributed_transport_stop_is_not_reported_as_user_cancel(self):
        service = self.service(InterruptingProvider())
        result = self.wait(service, self.submit(service)["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["termination_reason"], "provider_interrupted")
        self.assertFalse(result["cancel_requested"])

    def test_legacy_cancelled_row_does_not_invent_a_budget_origin(self):
        service = self.service(BaseProvider())
        conversation = service.create_conversation("MSFT")
        with closing(sqlite3.connect(service.chat_db)) as connection, connection:
            connection.execute(
                "INSERT INTO runs(run_id,conversation_id,status,created_at,cancel_requested) "
                "VALUES(?,?,?,?,0)",
                ("run_legacy_cancel", conversation["conversation_id"], "cancelled", "2020-01-01T00:00:00Z"),
            )
        result = service.get_run("run_legacy_cancel")
        self.assertEqual(result["termination_reason"], "unattributed_interruption")
        self.assertIn("旧记录未保存中断来源", result["termination_message"])
        self.assertFalse(result["cancel_requested"])


if __name__ == "__main__":
    unittest.main()
