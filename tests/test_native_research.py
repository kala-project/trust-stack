from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from tests.test_native_data import ORIGIN
from tests.test_recorded_calculations import wait
from tests.test_report_publications import block
from tests.test_research_intent import ControlledIntentResolver, intent
from workbench.answer_artifacts import AnswerArtifactService
from workbench.answer_review import AnswerReview
from workbench.api_contracts import validate_response
from workbench.build import build_database
from workbench.chat import ChatService
from workbench.native_data import NativeDataService
from workbench.native_sql import NativeSqlError
from workbench.report_publications import prepare
from workbench.research_tools import ToolError
from workbench.semantic_review import build_review_request
from workbench.server import EvidenceHTTPServer


class NativeProvider:
    requires_structured_reports = True

    def status(self):
        return {"available": True, "provider": "native-sql-protocol"}

    def stream(self, messages, tools, cancel_event):
        handlers = {item["name"]: item["handler"] for item in tools}
        sources = handlers["list_native_sources"]()["sources"]
        self.source = handlers["describe_native_source"](snapshot_id=sources[0]["snapshot_id"])
        self.query = handlers["execute_native_sql"](
            sources=[{"snapshot_id": self.source["snapshot_id"], "alias": "provider"}],
            sql="SELECT SUM(workload),COUNT(*),NULL,?,9007199254740993 AS large_integer,"
                "1.0 AS native_real,-0.0 AS signed_zero FROM provider.operations",
            parameters=["[claim:forged] **source text**"])
        parts = [
            {"kind": "text", "text": "Total synthetic workload across the registered source, not a company financial result: "},
            {"kind": "query_cell", "id": self.query["query_id"], "row": 0, "column": 0},
            {"kind": "text", "text": ". Record count: "},
            {"kind": "query_cell", "id": self.query["query_id"], "row": 0, "column": 1},
            {"kind": "text", "text": ". Missing value: "},
            {"kind": "query_cell", "id": self.query["query_id"], "row": 0, "column": 2},
            {"kind": "text", "text": ". Literal parameter: "},
            {"kind": "query_cell", "id": self.query["query_id"], "row": 0, "column": 3},
            {"kind": "text", "text": ". Exact large integer control: "},
            {"kind": "query_cell", "id": self.query["query_id"], "row": 0, "column": 4},
        ]
        handlers["publish_research_report"](
            title="Synthetic native data inventory, not issuer disclosure", blocks=[block(parts)])
        yield {"type": "delta", "text": "Native report prepared."}


class NativeResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.db = Path(cls.directory.name) / "evidence.sqlite3"
        build_database(db_path=cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def fixture(self, root):
        source = root / "provided.sqlite"
        with closing(sqlite3.connect(source)) as db, db:
            db.executescript("CREATE TABLE operations(subject TEXT,workload INTEGER);"
                             "INSERT INTO operations VALUES('MSFT',7),('AAPL',11),('OTHER',13);")
        native = NativeDataService(root / "native-data")
        snapshot = native.register_snapshot(source, source_key="synthetic", title="Synthetic operations", origin=ORIGIN)
        provider = NativeProvider()
        chat = ChatService(self.db, root / "chat.sqlite3", provider, native_data_service=native)
        self.addCleanup(chat.close)
        return source, native, snapshot, provider, chat

    @staticmethod
    def update_source(source, native):
        with closing(sqlite3.connect(source)) as db, db:
            db.execute("INSERT INTO operations VALUES('NEW',17)")
        return native.register_snapshot(source, source_key="synthetic", title="Synthetic operations", origin=ORIGIN)

    def test_native_query_to_published_binding_trace_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, native, snapshot, provider, chat = self.fixture(root)
            initial = chat.create_and_submit("MSFT", "Inspect all records in the registered synthetic source as research context")
            run = wait(chat, initial["run_id"])
            self.assertEqual(run["status"], "completed", run.get("error"))
            conversation = chat.get_conversation(initial["conversation_id"])
            message = conversation["messages"][-1]
            self.assertEqual(provider.query["rows"][0][:3], [31, 3, None])
            self.assertEqual(conversation["execution_scope"]["company_ids"], ["MSFT"])
            answer = chat.answer_context(initial["conversation_id"], message["message_id"])
            self.assertEqual(len(answer["citations"]), 1)
            self.assertEqual(answer["citations"][0]["kind"], "query")
            self.assertNotIn("[claim:forged]", answer["answer"])
            self.assertIn("\\u005bclaim:forged\\u005d", answer["answer"])
            self.assertIn("9007199254740993", answer["answer"])
            self.assertEqual(answer["source_snapshot"]["native_sources"]["snapshot_ids"], [snapshot["snapshot_id"]])
            citation = chat.read_citation(initial["conversation_id"], message["message_id"], "query", provider.query["query_id"])
            validate_response("GET", "/api/chat/citation", 200, citation)
            self.assertEqual(citation["detail_schema_version"], "citation-detail/1")
            self.assertNotIn("source_visibility", citation)
            self.assertTrue(citation["replay_matches"])
            self.assertEqual(citation["query"]["rows"][0][0], 31)
            artifact = AnswerArtifactService(chat).get(initial["conversation_id"], message["message_id"])
            trace = next(item for item in artifact["traces"] if item["reference"]["kind"] == "query")
            self.assertEqual(trace["status"], "available")
            self.assertEqual([item["layer"] for item in trace["steps"]], ["L4", "L3", "L2", "L1"])
            bound = [item for item in artifact["anchors"] if item["evidence"]]
            self.assertEqual(len(bound), 5)
            self.assertTrue(all(item["trace_ids"] for item in bound))
            self.assertEqual([item["kind"] for item in bound], ["number", "number", "assertion", "assertion", "number"])
            with self.assertRaises(NativeSqlError):
                native.read_query(provider.query["query_id"], owner="other-conversation", run_id=initial["run_id"])
            chat.close()
            reopened = ChatService(self.db, root / "chat.sqlite3", NativeProvider(),
                                   native_data_service=NativeDataService(native.root))
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.answer_context(initial["conversation_id"], message["message_id"]), answer)
            again = AnswerArtifactService(reopened).get(initial["conversation_id"], message["message_id"])
            self.assertEqual(again["findings"], artifact["findings"])
            reviewer = AnswerReview(reopened, initial["run_id"], "MSFT", answer["question"],
                                    {"query": {provider.query["query_id"]: {"record": provider.query}}})
            context = build_review_request(reviewer, answer["answer"])["authoritative_context"]
            native_source = next(item for item in context["sources"] if item["kind"] == "query")["native_sources"][0]
            self.assertEqual(native_source["origin"]["kind"], "synthetic_fixture")
            self.assertEqual(native_source["snapshot_id"], snapshot["snapshot_id"])
            self.assertEqual(native_source["schema"]["rows"][0][0], "operations")
            with self.assertRaises(ToolError):
                prepare(title="Invalid cell", blocks=[block([{
                    "kind": "query_cell", "id": provider.query["query_id"], "row": 99, "column": 0}])],
                    refs={"query": {provider.query["query_id"]: {"record": provider.query}}},
                    facts={}, calculations={}, max_chars=4000)

    def test_source_update_while_queued_does_not_change_selected_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            source, native, snapshot, provider, chat = self.fixture(Path(directory))
            with patch.object(chat, "_launch"):
                initial = chat.create_and_submit("MSFT", "Inspect registered synthetic source")
            newer = self.update_source(source, native)
            self.assertNotEqual(snapshot["snapshot_id"], newer["snapshot_id"])
            chat._launch(initial["run_id"])
            run = wait(chat, initial["run_id"])
            self.assertEqual(run["status"], "completed", run.get("error"))
            self.assertEqual(provider.query["rows"][0][:2], [31, 3])
            self.assertEqual(provider.source["snapshot_id"], snapshot["snapshot_id"])

    def test_native_updates_require_time_choice_and_preserve_old_report(self):
        with tempfile.TemporaryDirectory() as directory:
            source, native, snapshot, provider, chat = self.fixture(Path(directory))
            provider.resolve_research_intent = ControlledIntentResolver([
                intent(is_continuation=True), intent(is_continuation=True)])
            initial = chat.create_and_submit("MSFT", "Inspect registered synthetic source")
            self.assertEqual(wait(chat, initial["run_id"])["status"], "completed")
            message = chat.get_conversation(initial["conversation_id"])["messages"][-1]
            before = chat.answer_context(initial["conversation_id"], message["message_id"])
            newer = self.update_source(source, native)
            for choice, expected in (("retain", [31, 3]), ("refresh", [48, 4])):
                paused = chat.submit(initial["conversation_id"], "继续核查微软研究的上下文合成数据")
                state = chat.get_run(paused["run_id"])
                self.assertEqual(state["status"], "blocked")
                self.assertEqual(state["decision"]["kind"], "time")
                resumed = chat.decisions.answer(
                    state["decision"]["decision_id"], [choice], "", "accepted", "native-time-" + choice)
                finished = wait(chat, resumed["run_id"])
                self.assertEqual(finished["status"], "completed", finished.get("error"))
                self.assertEqual(provider.query["rows"][0][:2], expected)
                self.assertEqual(provider.source["snapshot_id"],
                                 snapshot["snapshot_id"] if choice == "retain" else newer["snapshot_id"])
            self.assertEqual(chat.answer_context(initial["conversation_id"], message["message_id"]), before)
            self.assertTrue(chat.read_citation(
                initial["conversation_id"], message["message_id"], "query", before["citations"][0]["id"])["replay_matches"])

    def test_native_source_download_requires_answer_frozen_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence.sqlite3"
            build_database(db_path=evidence)
            source = root / "provided.sqlite"
            with closing(sqlite3.connect(source)) as db, db:
                db.executescript("CREATE TABLE operations(subject TEXT,workload INTEGER);"
                                 "INSERT INTO operations VALUES('MSFT',7);")
            native = NativeDataService(root / "native-data")
            snapshot = native.register_snapshot(source, source_key="synthetic",
                                                title="Synthetic operations", origin=ORIGIN)
            server = EvidenceHTTPServer(0, evidence, chat_db=root / "chat.sqlite3",
                                        provider=NativeProvider())
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                initial = server.chat.create_and_submit(
                    "MSFT", "Inspect all records in the registered synthetic source")
                self.assertEqual(wait(server.chat, initial["run_id"])["status"], "completed")
                message = server.chat.get_conversation(initial["conversation_id"])["messages"][-1]
                answer = server.chat.answer_context(initial["conversation_id"], message["message_id"])
                values = urlencode({
                    "conversation_id": initial["conversation_id"],
                    "message_id": message["message_id"],
                    "snapshot_id": snapshot["snapshot_id"],
                })
                url = f"http://127.0.0.1:{server.server_port}/api/research/native-source?{values}"
                with urlopen(url, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["X-Verified-Source-SHA256"], snapshot["sha256"])
                missing_scope = dict(answer)
                missing_scope["source_snapshot"] = {}
                with patch.object(server.chat, "answer_context", return_value=missing_scope):
                    with self.assertRaises(HTTPError) as raised:
                        urlopen(url, timeout=5)
                    self.assertEqual(raised.exception.code, 409)
                    raised.exception.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)


if __name__ == "__main__":
    unittest.main()
