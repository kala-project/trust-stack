from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.test_chat import FakeProvider
from tests.test_native_data import ORIGIN
from workbench.answer_artifacts import AnswerArtifactService
from workbench.build import build_database
from workbench.chat import ChatService
from workbench.evidence_checks import EvidenceRecordRead
from workbench.native_data import NativeDataService
from workbench.native_sql import NativeSqlError, digest
from workbench.report_publications import prepare, validate_bindings
from workbench.tool_errors import ToolError
from workbench.worker import DurableWorker


class NativeVerificationProvider(FakeProvider):
    """Controlled protocol driver; it makes no claim about model research quality."""
    requires_structured_reports = True

    def __init__(self):
        super().__init__()
        self.query = None
        self.revision = None
        self.receipt = None

    @staticmethod
    def _query(handlers):
        source = handlers["list_native_sources"]()["sources"][0]
        return handlers["execute_native_sql"](
            sources=[{"snapshot_id": source["snapshot_id"], "alias": "operations"}],
            sql="SELECT SUM(units) AS total FROM operations.records")

    def stream(self, messages, tools, cancel_event):
        handlers = {item["name"]: item["handler"] for item in tools}
        self.query = self._query(handlers)
        handlers["publish_research_report"](
            title="Initial synthetic inventory",
            blocks=[{
                "classification": "recorded_calculation",
                "parts": [
                    {"kind": "text", "text": "Initial total: "},
                    {"kind": "query_cell", "id": self.query["query_id"], "row": 0, "column": 0,
                     "value_formation": "observed", "attribution": None},
                ],
                "support": [], "reasoning": "SQL aggregation over the frozen synthetic fixture.",
                "limitations": ["Synthetic fixture; not a real-company result."],
            }])
        yield {"type": "usage", "tokens": 1}

    def stream_verification(self, messages, tools, cancel_event):
        handlers = {item["name"]: item["handler"] for item in tools}
        packet = json.loads(messages[0]["content"])
        current = self._query(handlers)
        preview = handlers["preview_verification_revision"](
            version_id=packet["version_id"], mode="replace",
            title="Verified synthetic inventory",
            blocks=[{
                "classification": "recorded_calculation",
                "parts": [
                    {"kind": "text", "text": "Replayed total: "},
                    {"kind": "query_cell", "id": current["query_id"], "row": 0, "column": 0,
                     "value_formation": "observed", "attribution": None},
                ],
                "support": [], "reasoning": "Exact replay of the frozen native query.",
                "limitations": ["Synthetic fixture; not a real-company result."],
            }],
            revision_summary="Replayed the exact native query and clarified the bounded result.")
        self.revision = handlers["revise_verification_candidate"](
            version_id=preview["base_version_id"], preview_id=preview["preview_id"])
        current = handlers["get_candidate_reference"](
            kind="query", identifier=current["query_id"])
        if current["state"] != "available":
            raise AssertionError(current)
        self.receipt = current["_verification_receipt"]
        for node_id in self.revision["content_nodes"]:
            for check_kind in self.revision["verification_checks"][node_id]["required"]:
                handlers["record_verification_finding"](
                    version_id=self.revision["version_id"], node_id=node_id,
                    check_kind=check_kind, outcome="corrected_and_rechecked",
                    explanation="The persisted query replayed to its exact recorded result.",
                    receipt_ids=[self.receipt["identifier"]])
        handlers["finalize_verification"](
            version_id=self.revision["version_id"], outcome="verified", limitations=[],
            semantic_review={"original_question_coverage": "The bounded synthetic inventory question is answered."},
            verification={"method": "controlled-native-service-test", "semantic_truth_guaranteed": False})
        yield {"type": "usage", "tokens": 1}


class NativeVerificationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.evidence = self.root / "evidence.sqlite3"
        build_database(db_path=self.evidence)
        source_path = self.root / "operations.sqlite"
        with closing(sqlite3.connect(source_path)) as connection, connection:
            connection.executescript(
                "CREATE TABLE records(name TEXT,units INTEGER);"
                "INSERT INTO records VALUES('Alpha',7),('Beta',13);")
        self.native = NativeDataService(self.root / "native")
        self.source = self.native.register_snapshot(
            source_path, source_key="synthetic-operations", title="Synthetic operations",
            origin=ORIGIN)

    def service(self, provider=None, *, native=True):
        service = ChatService(
            self.evidence, self.root / ("chat-native.sqlite" if native else "chat-no-native.sqlite"),
            provider or FakeProvider(),
            native_data_service=self.native if native else None,
            job_lease_seconds=30, job_retry_delays=(0, 0))
        self.addCleanup(service.close)
        return service

    def test_real_native_query_preview_commit_finalize_and_citation_read(self):
        provider = NativeVerificationProvider()
        service = self.service(provider)
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(
            conversation["conversation_id"], "Total the registered synthetic operation units.",
            request_id="native-author")
        worker = DurableWorker(service, worker_id="native-verification", heartbeat_seconds=.1)
        self.assertTrue(worker.run_once())
        self.assertTrue(worker.run_once())

        listed = service.verification_store.list_candidates(conversation["conversation_id"])
        candidate = service.verification_store.get_candidate(
            conversation["conversation_id"], listed[0]["candidate_id"])
        self.assertEqual(candidate["author_run_id"], submitted["run_id"])
        self.assertEqual(candidate["status"], "verified", candidate["status"])
        self.assertEqual(len(candidate["versions"]), 2)
        self.assertEqual(provider.query["rows"], [[20]])
        self.assertEqual(provider.query["query_id"], provider.revision["publication"]["blocks"][0]["support"][0]["id"])

        formal_id = candidate["formal_message_id"]
        answer = service.answer_context(conversation["conversation_id"], formal_id)
        self.assertIn("Replayed total: 20", answer["answer"])
        citation = service.read_citation(
            conversation["conversation_id"], formal_id, "query", provider.query["query_id"])
        self.assertTrue(citation["replay_matches"])
        self.assertEqual(citation["query"]["rows"], [[20]])
        artifact = AnswerArtifactService(service).get(conversation["conversation_id"], formal_id)
        self.assertTrue(any(trace.get("reference") == {
            "kind": "query", "id": provider.query["query_id"]} for trace in artifact["traces"]))
        published = service.published_verification_evidence(conversation["conversation_id"], formal_id)
        self.assertTrue(any(item.get("verification_receipt_id") == provider.receipt["identifier"]
                            for item in published["receipts"]))

    def test_scoped_callback_rejects_cross_owner_run_snapshot_and_altered_record(self):
        service = self.service()
        conversation = service.create_conversation("MSFT")
        owner = conversation["conversation_id"]
        run_id = service.submit(owner, "Native scope test", request_id="scope")["run_id"]
        scope = self.native.capture_scope(owner=owner)
        query = self.native.query(
            owner=owner, run_id=run_id, scope=scope,
            sources=[{"snapshot_id": self.source["snapshot_id"], "alias": "operations"}],
            sql="SELECT SUM(units) FROM operations.records")
        replay = service.verification_executor.native_query_replay
        self.assertEqual(replay(query["query_id"], owner=owner, run_id=run_id, scope=scope)["query"], query)
        cases = [
            {"owner": "other-owner", "run_id": run_id,
             "scope": {**scope, "owner": "other-owner"}},
            {"owner": owner, "run_id": "other-run", "scope": scope},
            {"owner": owner, "run_id": run_id, "scope": {**scope, "snapshot_ids": []}},
        ]
        for arguments in cases:
            with self.subTest(arguments=arguments), self.assertRaises(NativeSqlError):
                replay(query["query_id"], **arguments)

        with closing(sqlite3.connect(self.native.database)) as connection, connection:
            connection.execute("DROP TRIGGER native_queries_no_update")
            row = connection.execute(
                "SELECT payload_json FROM native_queries WHERE owner=? AND query_id=?",
                (owner, query["query_id"])).fetchone()
            altered = json.loads(row[0])
            altered["sql"] = "SELECT 0"
            raw = json.dumps(altered, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            connection.execute(
                "UPDATE native_queries SET payload_json=?,payload_sha256=? WHERE owner=? AND query_id=?",
                (raw, digest(altered), owner, query["query_id"]))
        with self.assertRaises(NativeSqlError) as raised:
            replay(query["query_id"], owner=owner, run_id=run_id, scope=scope)
        self.assertEqual(raised.exception.code, "native_record_integrity")

    def test_missing_public_replay_capability_is_a_clear_tool_error(self):
        query = self.native.query(
            owner="owner-a", run_id="run-a", scope=self.native.capture_scope(owner="owner-a"),
            sources=[{"snapshot_id": self.source["snapshot_id"], "alias": "operations"}],
            sql="SELECT SUM(units) FROM operations.records")
        publication = prepare(
            title="Synthetic query", blocks=[{
                "classification": "recorded_calculation",
                "parts": [{"kind": "query_cell", "id": query["query_id"], "row": 0, "column": 0,
                           "value_formation": "observed", "attribution": None}],
                "support": [], "reasoning": "Synthetic test.", "limitations": [],
            }], refs={"query": {query["query_id"]: {"record": query}}},
            facts={}, calculations={}, max_chars=4000)

        class Reviewer:
            indexes = {"facts": {}, "calculations": {}}

            @staticmethod
            def read_reference_record(kind, identifier):
                return EvidenceRecordRead(
                    "available", kind, identifier, record_type="sql_query",
                    _record_bytes=json.dumps(query).encode("utf-8"),
                )

        with self.assertRaises(ToolError) as raised:
            validate_bindings(publication, Reviewer())
        self.assertEqual(raised.exception.code, "native_source_unavailable")
        self.assertIn("replay capability", raised.exception.message)

    def test_scoped_callback_rejects_altered_registered_source_hash(self):
        service = self.service()
        conversation = service.create_conversation("MSFT")
        owner = conversation["conversation_id"]
        run_id = service.submit(owner, "Source hash test", request_id="source-hash")["run_id"]
        scope = self.native.capture_scope(owner=owner)
        query = self.native.query(
            owner=owner, run_id=run_id, scope=scope,
            sources=[{"snapshot_id": self.source["snapshot_id"], "alias": "operations"}],
            sql="SELECT SUM(units) FROM operations.records")
        with closing(sqlite3.connect(self.native.database)) as connection, connection:
            connection.execute("DROP TRIGGER native_sources_no_update")
            row = connection.execute(
                "SELECT payload_json FROM native_sources WHERE snapshot_id=?",
                (self.source["snapshot_id"],)).fetchone()
            altered = json.loads(row[0])
            altered["sha256"] = "0" * 64
            raw = json.dumps(
                altered, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False)
            connection.execute(
                "UPDATE native_sources SET payload_json=?,payload_sha256=? WHERE snapshot_id=?",
                (raw, digest(altered), self.source["snapshot_id"]))
        with self.assertRaises(NativeSqlError) as raised:
            service.verification_executor.native_query_replay(
                query["query_id"], owner=owner, run_id=run_id, scope=scope)
        self.assertEqual(raised.exception.code, "native_record_integrity")


if __name__ == "__main__":
    unittest.main()
