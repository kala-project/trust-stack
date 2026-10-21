from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from workbench.api_contracts import validate_response
from workbench.build import build_database
from workbench.chat import ChatError, ChatService
from workbench.documents import DocumentService
from tests.support_chat_provider import ExplicitMSFTIntentAdapter


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/source-catalog.json"


def wait(service, run_id, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = service.get_run(run_id)
        if run["status"] not in {"queued", "running"}:
            return run
        time.sleep(0.01)
    raise AssertionError("run did not finish")


class DocumentCalculationProvider(ExplicitMSFTIntentAdapter):
    def __init__(self, version_id):
        self.version_id = version_id
        self.fact_ids = []
        self.evidence_ids = []
        self.calculation_id = None

    def status(self):
        return {"available": True, "reason": "", "provider": "fake"}

    def stream(self, messages, tools, cancel_event):
        handlers = {item["name"]: item["handler"] for item in tools}
        common = {
            "document_version_id": self.version_id,
            "table_index": 0,
            "header_row_indices": [0],
            "row_label_column_index": 0,
            "unit": "USD millions",
            "period_start": "2024-07-01",
            "period_end": "2025-06-30",
            "period_kind": "year",
            "basis": "issuer table presentation",
            "metadata_mode": "explicit_interpretation",
        }
        capex = handlers["extract_document_table_fact"](
            **common, row_index=1, column_index=1, expected_value="120",
            metric="capital_expenditures",
        )["fact"]
        revenue = handlers["extract_document_table_fact"](
            **common, row_index=2, column_index=1, expected_value="240",
            metric="revenue_from_document",
        )["fact"]
        self.fact_ids = [capex["fact_id"], revenue["fact_id"]]
        self.evidence_ids = [capex["evidence_id"], revenue["evidence_id"]]
        calculation = handlers["calculate_facts"](
            operation="ratio", fact_ids=self.fact_ids,
        )
        self.calculation_id = calculation["calculation_id"]
        yield {"type": "delta", "text": (
            f'Capex was {capex["value"]} [evidence:{capex["evidence_id"]}] and '
            f'the ratio was {calculation["value"]} '
            f'[calculation:{calculation["calculation_id"]}]'
        )}


class DynamicDocumentProvenanceTests(unittest.TestCase):
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
        root = Path(self.temp.name)
        self.documents = DocumentService(root / "documents")
        source = root / "annual.html"
        source.write_text(
            "<!doctype html><html><body><table>"
            "<tr><th>USD millions</th><th>2025</th></tr>"
            "<tr><td>Capital expenditures</td><td>$120</td></tr>"
            "<tr><td>Revenue</td><td>$240</td></tr>"
            "</table></body></html>", encoding="utf-8",
        )
        self.version = self.documents.register_file(
            source, company_id="MSFT", source_key="annual", title="Annual filing",
            url="https://www.microsoft.com/investor/example", published_at="2025-07-01",
            rights_status="public",
        )
        self.provider = DocumentCalculationProvider(self.version["document_version_id"])
        self.service = ChatService(
            self.evidence_db, root / "chat.sqlite3", self.provider,
            document_service=self.documents,
        )
        conversation = self.service.create_conversation("MSFT")
        self.conversation_id = conversation["conversation_id"]
        submitted = self.service.submit(self.conversation_id, "For Microsoft, calculate capex as a share of revenue")
        self.run_id = submitted["run_id"]
        self.assertEqual(wait(self.service, self.run_id)["status"], "completed")
        self.answer = self.service.get_conversation(self.conversation_id)["messages"][-1]

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def read(self, kind, identifier):
        return self.service.read_citation(
            self.conversation_id, self.answer["message_id"], kind, identifier,
        )

    def extraction_event(self):
        with closing(sqlite3.connect(self.service.chat_db)) as connection:
            row = connection.execute(
                "SELECT seq,payload_json FROM events WHERE run_id=? AND type='tool_result' "
                "AND json_extract(payload_json,'$.name')='extract_document_table_fact' ORDER BY seq LIMIT 1",
                (self.run_id,),
            ).fetchone()
        return row[0], json.loads(row[1])

    def rewrite_receipt_result(self, tool_name, mutate, *, occurrence=0, run_id=None):
        """Simulate storage tamper past append-only enforcement, retaining envelope validity."""
        run_id = run_id or self.run_id
        with closing(sqlite3.connect(self.service.chat_db)) as connection, connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM author_tool_receipts WHERE run_id=? AND tool_name=? ORDER BY revision",
                (run_id, tool_name),
            ).fetchall()
            row = rows[occurrence]
            result = json.loads(row["result_json"])
            mutate(result)
            result_raw = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            envelope = json.dumps({
                "arguments_sha256": hashlib.sha256(row["arguments_json"].encode()).hexdigest(),
                "call_id": row["call_id"],
                "record_version": "author-tool-receipt/1",
                "result_sha256": hashlib.sha256(result_raw.encode()).hexdigest(),
                "revision": row["revision"],
                "run_id": row["run_id"],
                "tool_name": row["tool_name"],
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            receipt_sha256 = hashlib.sha256(envelope.encode()).hexdigest()
            connection.execute("DROP TRIGGER IF EXISTS author_tool_receipts_no_update")
            connection.execute(
                "UPDATE author_tool_receipts SET result_json=?,receipt_sha256=? "
                "WHERE run_id=? AND revision=?",
                (result_raw, receipt_sha256, run_id, row["revision"]),
            )
        return result

    def test_extraction_calculation_and_evidence_replay_exact_frozen_table(self):
        self.assertTrue(all(item["valid"] for item in self.answer["citations"]))
        self.assertTrue(any("not semantically approved" in warning
                            for warning in self.answer["verification"]["warnings"]))
        context = self.service.answer_context(self.conversation_id, self.answer["message_id"])
        self.assertFalse(any(item.get("fact_id", "").startswith("docfact-")
                             for item in context["source_snapshot"]["evidence"]["facts"]))

        _, event = self.extraction_event()
        provenance = event["result"]["historical_provenance"]
        self.assertEqual(provenance["source"]["published_at"], "2025-07-01")
        self.assertFalse(provenance["interpretation"]["semantic_verified"])

        evidence = self.read("evidence", self.provider.evidence_ids[0])
        validate_response("GET", "/api/chat/citation", 200, evidence)
        self.assertEqual(evidence["source_visibility"], "public")
        self.assertEqual(evidence["quote"], "$120")
        self.assertEqual(evidence["tables"][0]["rows"][1][1], "$120")
        self.assertEqual(evidence["tables"][0]["target_cell"]["value"], "120")
        self.assertEqual(evidence["tables"][0]["target_cell"]["row_header"], "Capital expenditures")
        self.assertEqual(evidence["tables"][0]["target_cell"]["column_headers"], ["2025"])
        self.assertIsNone(evidence["tables"][0]["target_cell"]["unit_header"])
        self.assertFalse(evidence["semantic_verified"])
        self.assertEqual(evidence["interpretation"]["state"], "explicit_interpretation")
        self.assertIn("not issuer-proven", evidence["interpretation"]["statement"])

        calculation = self.read("calculation", self.provider.calculation_id)
        self.assertEqual((calculation["value"], calculation["unit"]), ("0.5", "x"))
        self.assertEqual([item["table"]["target_cell"]["raw_value"]
                          for item in calculation["input_sources"]], ["$120", "$240"])
        self.assertTrue(all(item["interpretation"]["semantic_verified"] is False
                            for item in calculation["input_sources"]))
        self.assertEqual(calculation["interpretation"], "unverified")

    def test_composed_calculation_publishes_replays_and_exposes_all_source_cells(self):
        original_stream = self.provider.stream
        calculated = {}
        def stream(messages, tools, cancel_event):
            yield from original_stream(messages, tools, cancel_event)
            handlers = {item["name"]: item["handler"] for item in tools}
            total = handlers["calculate_facts"](operation="sum", fact_ids=self.provider.fact_ids)
            ratio = handlers["calculate_facts"](
                operation="ratio", fact_ids=[self.provider.fact_ids[0], total["calculation_id"]])
            calculated.update(ratio)
            yield {"type": "delta", "text": f'\n\nThe ratio is {ratio["value"]} x [calculation:{ratio["calculation_id"]}].'}
        with patch.object(self.provider, "stream", side_effect=stream):
            submitted = self.service.submit(self.conversation_id, "For Microsoft, calculate the ratio to the sum")
            self.assertEqual(wait(self.service, submitted["run_id"])["status"], "completed")
        answer = self.service.get_conversation(self.conversation_id)["messages"][-1]
        detail = self.service.read_citation(
            self.conversation_id, answer["message_id"], "calculation", calculated["calculation_id"])
        self.assertEqual(detail["value"], "0.333333333333")
        self.assertEqual(len(detail["input_sources"]), 2)
        arithmetic = detail["calculation"]["arithmetic"]
        self.assertEqual(arithmetic["exact_value"], {"numerator": "1", "denominator": "3"})
        self.assertEqual(arithmetic["steps"][0]["value"], "360")
        from workbench.answer_artifacts import AnswerArtifactService
        artifact = AnswerArtifactService(self.service).get(self.conversation_id, answer["message_id"])
        record = next(item for item in artifact["calculations"] if item["calculation_id"] == calculated["calculation_id"])
        self.assertEqual(record["arithmetic"], arithmetic)
        with closing(self.service._connect()) as connection, connection:
            row = connection.execute(
                "SELECT seq,payload_json FROM events WHERE run_id=? AND type='tool_result' "
                "AND json_extract(payload_json,'$.result.calculation_id')=?",
                (submitted["run_id"], calculated["calculation_id"])).fetchone()
            changed = json.loads(row["payload_json"])
            changed["result"]["parameters"]["dependencies"][0]["operation"] = "difference"
            connection.execute("UPDATE events SET payload_json=? WHERE run_id=? AND seq=?",
                               (json.dumps(changed), submitted["run_id"], row["seq"]))
        unchanged = self.service.read_citation(
            self.conversation_id, answer["message_id"], "calculation", calculated["calculation_id"])
        self.assertEqual(unchanged["calculation"]["arithmetic"], arithmetic)
        self.rewrite_receipt_result(
            "calculate_facts",
            lambda result: result["parameters"]["dependencies"][0].__setitem__(
                "operation", "difference"),
            occurrence=-1,
            run_id=submitted["run_id"],
        )
        with self.assertRaises(ChatError):
            self.service.read_citation(
                self.conversation_id, answer["message_id"], "calculation", calculated["calculation_id"])

    def test_followup_can_discover_and_reuse_registered_inputs_without_model_confirmation(self):
        def stream(messages, tools, cancel_event):
            handlers = {item["name"]: item["handler"] for item in tools}
            catalog = handlers["list_research_inputs"]()
            self.assertEqual({item["input_id"] for item in catalog["inputs"]}, set(self.provider.fact_ids))
            self.assertTrue(all(item["in_frozen_snapshot"] for item in catalog["inputs"]))
            result = handlers["read_research_inputs"](input_ids=self.provider.fact_ids)
            self.assertEqual(len(result["restored_document_facts"]), 2)
            calculation = handlers["calculate_facts"](operation="difference", fact_ids=self.provider.fact_ids)
            self.provider.calculation_id = calculation["calculation_id"]
            yield {"type": "delta", "text": f'The difference is {calculation["value"]} {calculation["unit"]} [calculation:{calculation["calculation_id"]}].'}
        with patch.object(self.provider, "stream", side_effect=stream):
            submitted = self.service.submit(self.conversation_id, "For Microsoft, subtract the same-period inputs")
            run = wait(self.service, submitted["run_id"])
        self.assertEqual(run["status"], "completed", run.get("error"))
        self.assertFalse(any(event.get("name") == "extract_document_table_fact" for event in run["events"]))
        message = self.service.get_conversation(self.conversation_id)["messages"][-1]
        detail = self.service.read_citation(
            self.conversation_id, message["message_id"], "calculation", self.provider.calculation_id)
        self.assertEqual(detail["value"], "-120")
        self.assertTrue(all(item["reused_from"]["run_id"] == self.run_id for item in detail["input_sources"]))
        from workbench.answer_artifacts import AnswerArtifactService
        artifact = AnswerArtifactService(self.service).get(self.conversation_id, message["message_id"])
        self.assertTrue(any(item["calculation_id"] == self.provider.calculation_id for item in artifact["calculations"]))

    def test_registered_restore_rejects_changed_snapshot_atomically(self):
        from workbench.research_tools import ResearchTools, ToolError
        snapshot = self.service.effective_snapshot(self.run_id, "MSFT")
        research = ResearchTools(self.evidence_db, "MSFT", self.documents, frozen_snapshot=snapshot)
        before = deepcopy(research._facts)
        changed = deepcopy(snapshot)
        changed["documents"]["document_version_ids"] = []
        def validate(result, source_run_id):
            self.service._validated_document_fact(
                {"run_id": self.run_id, "company_id": "MSFT", "source_snapshot": changed},
                result, source_run_id=source_run_id)
        read = next(item["handler"] for item in research.input_definitions(
            self.service.inputs, self.run_id, validate) if item["name"] == "read_research_inputs")
        with self.assertRaises(ChatError):
            read(self.provider.fact_ids)
        self.assertEqual(research._facts, before)
        changed["documents"] = snapshot["documents"]
        with self.assertRaises(ToolError):
            read([self.provider.fact_ids[0], "missing-input"])
        self.assertEqual(research._facts, before)

    def test_answer_artifact_preserves_dynamic_ratio_and_each_input_source(self):
        from workbench.answer_artifacts import AnswerArtifactService
        artifact = AnswerArtifactService(self.service).get(self.conversation_id, self.answer["message_id"])
        calculation = next(item for item in artifact["calculations"] if item["calculation_id"] == self.provider.calculation_id)
        self.assertEqual((calculation["value"], calculation["unit"]), ("0.5", "x"))
        self.assertEqual([item["fact_id"] for item in calculation["inputs"]], self.provider.fact_ids)
        trace = next(item for item in artifact["traces"] if item["reference"] == {"kind": "calculation", "id": self.provider.calculation_id})
        self.assertEqual([item["table"]["target_cell"]["raw_value"] for item in trace["source"]["sources"]], ["$120", "$240"])
        self.assertFalse(any("已排除" in text for text in artifact["limitations"]))
        snapshot = self.service.answer_context(self.conversation_id, self.answer["message_id"])["source_snapshot"]
        self.assertFalse(any(fact["fact_id"].startswith("docfact-") for fact in snapshot["evidence"]["facts"]))
        seq, event = self.extraction_event()
        event["result"]["fact"]["value"] = "999"
        with closing(sqlite3.connect(self.service.chat_db)) as connection, connection:
            connection.execute("UPDATE events SET payload_json=? WHERE run_id=? AND seq=?", (json.dumps(event), self.run_id, seq))
        unchanged = AnswerArtifactService(self.service).get(
            self.conversation_id, self.answer["message_id"])
        unchanged_calculation = next(
            item for item in unchanged["calculations"]
            if item["calculation_id"] == self.provider.calculation_id)
        self.assertEqual(unchanged_calculation["value"], "0.5")
        self.rewrite_receipt_result(
            "extract_document_table_fact",
            lambda result: result["fact"].__setitem__("value", "999"),
        )
        with self.assertRaises(ChatError):
            AnswerArtifactService(self.service).get(self.conversation_id, self.answer["message_id"])

    def reviewer(self):
        from workbench.answer_review import AnswerReview
        refs = {kind: {} for kind in ("claim", "evidence", "passage", "calculation")}
        for citation in self.answer["citations"]:
            refs[citation["kind"]][citation["id"]] = {}
        for evidence_id in self.provider.evidence_ids:
            refs["evidence"][evidence_id] = {}
        return AnswerReview(self.service, self.run_id, "MSFT", "Capex / revenue", refs)

    def test_publication_review_keeps_document_calculations_and_source_cells(self):
        reviewer = self.reviewer()
        self.assertEqual([item["calculation_id"] for item in reviewer.calculations],
                         [self.provider.calculation_id])
        self.assertEqual(set(reviewer.indexes["document_facts"]), set(self.provider.fact_ids))
        draft = f'The ratio was 0.5x [calculation:{self.provider.calculation_id}].'
        checked = reviewer.check(draft)
        self.assertEqual(checked["flags"], [])
        self.assertEqual(len(checked["calculation_bindings"]), 1)
        request = reviewer.repair_request(draft, checked)
        self.assertEqual(request["allowed_context"]["calculations"][0]["value"], "0.5")
        self.assertTrue(set(self.provider.evidence_ids) <= {
            item["id"] for item in request["allowed_context"]["sources"]
        })

    def test_repair_cannot_trade_a_valid_calculation_binding_for_fewer_flags(self):
        reviewer = self.reviewer()
        citation = f"[calculation:{self.provider.calculation_id}]"
        evidence = f"[evidence:{self.provider.evidence_ids[0]}]"
        original = reviewer.check(
            f"The ratio was 0.5x {citation}.\n\n"
            "The ratio was 0.5x.\n\nUnits: USD millions."
        )
        candidate = reviewer.check(
            f"The ratio was 0.5x.\n\n"
            f"The ratio was 0.5x {citation}.\n\nUnits: USD millions {evidence}."
        )
        self.assertLess(candidate["score"], original["score"])
        self.assertFalse(reviewer.is_better(original, candidate))
        repaired = reviewer.check(
            f"The ratio was 0.5x {citation}.\n\n"
            f"The ratio was 0.5x {citation}.\n\nUnits: USD millions {evidence}."
        )
        self.assertTrue(reviewer.is_better(original, repaired))

    def test_publication_review_rejects_tampered_document_derivation(self):
        seq, event = self.extraction_event()
        event["result"]["fact"]["value"] = "999"
        with closing(sqlite3.connect(self.service.chat_db)) as connection, connection:
            connection.execute("UPDATE events SET payload_json=? WHERE run_id=? AND seq=?",
                               (json.dumps(event), self.run_id, seq))
        self.assertEqual([item["calculation_id"] for item in self.reviewer().calculations],
                         [self.provider.calculation_id])
        self.rewrite_receipt_result(
            "extract_document_table_fact",
            lambda result: result["fact"].__setitem__("value", "999"),
        )
        with self.assertRaises(ChatError):
            self.reviewer()

    def test_tampered_authoritative_extraction_is_rejected(self):
        seq, event = self.extraction_event()
        event["result"]["fact"]["raw_value"] = "$121"
        with closing(sqlite3.connect(self.service.chat_db)) as connection, connection:
            connection.execute(
                "UPDATE events SET payload_json=? WHERE run_id=? AND seq=?",
                (json.dumps(event), self.run_id, seq),
            )
        self.assertEqual(self.read("calculation", self.provider.calculation_id)["value"], "0.5")
        self.assertEqual(self.read("evidence", self.provider.evidence_ids[0])["quote"], "$120")
        self.rewrite_receipt_result(
            "extract_document_table_fact",
            lambda result: result["fact"].__setitem__("raw_value", "$121"),
        )
        with self.assertRaisesRegex(ChatError, "derivation|cell|identity"):
            self.read("calculation", self.provider.calculation_id)
        with self.assertRaisesRegex(ChatError, "cell|identity"):
            self.read("evidence", self.provider.evidence_ids[0])

    def test_immutable_source_archive_tamper_is_rejected(self):
        object_path = self.documents.objects_path / self.version["source_sha256"]
        object_path.write_bytes(b"tampered")
        with self.assertRaisesRegex(ChatError, "unavailable"):
            self.read("calculation", self.provider.calculation_id)
        with self.assertRaisesRegex(ChatError, "unavailable"):
            self.read("evidence", self.provider.evidence_ids[0])

    def test_foreign_or_later_version_cannot_replace_run_frozen_source(self):
        for company_id, source_key in (("AAPL", "foreign"), ("MSFT", "later")):
            with self.subTest(company_id=company_id):
                source = Path(self.temp.name) / f"{source_key}.html"
                source.write_text("<html><body><table><tr><th>X</th></tr></table></body></html>")
                replacement = self.documents.register_file(
                    source, company_id=company_id, source_key=source_key, title=source_key,
                )
                seq, event = self.extraction_event()
                changed = deepcopy(event)
                changed["result"]["fact"]["locator"]["document_version_id"] = replacement["document_version_id"]
                changed["result"]["fact"]["source_id"] = replacement["document_version_id"]
                changed["result"]["historical_provenance"]["source"]["document_version_id"] = replacement["document_version_id"]
                with closing(sqlite3.connect(self.service.chat_db)) as connection, connection:
                    connection.execute(
                        "UPDATE events SET payload_json=? WHERE run_id=? AND seq=?",
                        (json.dumps(changed), self.run_id, seq),
                    )
                self.assertEqual(
                    self.read("calculation", self.provider.calculation_id)["value"], "0.5")

                def replace_owner(result):
                    result["fact"]["locator"]["document_version_id"] = replacement["document_version_id"]
                    result["fact"]["source_id"] = replacement["document_version_id"]
                    result["historical_provenance"]["source"]["document_version_id"] = replacement["document_version_id"]

                self.rewrite_receipt_result("extract_document_table_fact", replace_owner)
                with self.assertRaisesRegex(ChatError, "ownership|snapshot|derivation"):
                    self.read("calculation", self.provider.calculation_id)
                self.rewrite_receipt_result(
                    "extract_document_table_fact",
                    lambda result: (result.clear(), result.update(deepcopy(event["result"]))),
                )
                with closing(sqlite3.connect(self.service.chat_db)) as connection, connection:
                    connection.execute(
                        "UPDATE events SET payload_json=? WHERE run_id=? AND seq=?",
                        (json.dumps(event), self.run_id, seq),
                    )


if __name__ == "__main__":
    unittest.main()
