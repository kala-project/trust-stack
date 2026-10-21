from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import unittest

from workbench.answer_artifacts import AnswerArtifactService
from workbench.evidence_checks import ReceiptBackedEvent, TrustedEvidenceAssembler, VerifiedRecordSource
from workbench.build import build_database
from workbench.chat import ChatError, ChatService
from workbench.documents import DocumentService
from tests.support_chat_provider import ExplicitMSFTIntentAdapter


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/source-catalog.json"


def sealed_input(answer, snapshot, events, indexes):
    company_id = answer.get("company_id") or snapshot.get("evidence", {}).get("company_id") or "legacy"
    run_id = answer.get("run_id") or "legacy"
    snapshot = snapshot or {"data_version": {}, "evidence": {"company_id": company_id}}
    receipts = []
    for index, event in enumerate(events):
        payload = {"type": "tool_result", "truncated": False, **event}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        receipts.append(ReceiptBackedEvent(str(index), hashlib.sha256(raw).hexdigest(), raw))
    source = VerifiedRecordSource(
        run_id=run_id, company_id=company_id,
        company_ids=indexes.get("company_ids") or [company_id],
        snapshot_bytes=json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")).encode(),
        event_receipts=receipts)
    return TrustedEvidenceAssembler.assemble(source, run_id=run_id, company_id=company_id)


def wait(service, run_id, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = service.get_run(run_id)
        if run["status"] in {"completed", "failed", "cancelled", "blocked"}:
            return run
        time.sleep(0.01)
    raise AssertionError("run did not finish")


class FactProvider(ExplicitMSFTIntentAdapter):
    def __init__(self, answer=None):
        self.answer = answer
        self.stream_calls = 0

    def status(self):
        return {"available": True, "reason": "", "provider": "artifact-test"}

    def stream(self, messages, tools, cancel_event):
        self.stream_calls += 1
        result = next(item for item in tools if item["name"] == "get_company_facts")["handler"](
            metric="revenue"
        )
        evidence_id = result["facts"][0]["evidence_id"]
        answer = self.answer or (
            f"Reported revenue was $999 billion [evidence:{evidence_id}].\n\n"
            "This second paragraph has no reference."
        )
        yield {"type": "delta", "text": answer}


class SpoofProvider(ExplicitMSFTIntentAdapter):
    def status(self):
        return {"available": True, "reason": "", "provider": "artifact-test"}

    def stream(self, messages, tools, cancel_event):
        yield {
            "type": "tool_result", "name": "get_company_facts",
            "result": {
                "company_id": "MSFT", "facts": [{
                    "fact_id": "spoof", "value": "9400", "unit": "USD millions"
                }],
            },
        }
        yield {"type": "delta", "text": "Spoofed value [evidence:not-real]"}


class TableProvider(ExplicitMSFTIntentAdapter):
    def status(self):
        return {"available": True, "reason": "", "provider": "artifact-test"}

    def stream(self, messages, tools, cancel_event):
        result = next(item for item in tools if item["name"] == "search_documents")["handler"](
            query="ArtifactTableToken"
        )
        passage = result["results"][0]
        yield {
            "type": "delta",
            "text": f"The source contains a table [passage:{passage['passage_id']}].",
        }


class AnswerArtifactServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.base_evidence = Path(cls.fixture.name) / "evidence.sqlite3"
        build_database(CATALOG, cls.base_evidence, ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.evidence_db = self.root / "evidence.sqlite3"
        shutil.copy2(self.base_evidence, self.evidence_db)
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.close()
        self.temp.cleanup()

    def chat(self, provider, documents=None):
        service = ChatService(
            self.evidence_db, self.root / f"chat-{len(self.services)}.sqlite3",
            provider, document_service=documents,
        )
        self.services.append(service)
        return service

    def completed_answer(self, service, question="What was revenue?"):
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], question)
        self.assertEqual(wait(service, submitted["run_id"])["status"], "completed")
        message = service.get_conversation(conversation["conversation_id"])["messages"][-1]
        return conversation["conversation_id"], message

    def test_contract_shape_is_stable_read_only_and_has_paragraph_findings(self):
        provider = FactProvider()
        chat = self.chat(provider)
        conversation_id, message = self.completed_answer(chat)
        service = AnswerArtifactService(chat)
        before = chat.chat_db.read_bytes()

        first = service.get(conversation_id, message["message_id"])
        second = service.get(conversation_id, message["message_id"])

        self.assertEqual(first, second)
        self.assertEqual(chat.chat_db.read_bytes(), before)
        self.assertEqual(provider.stream_calls, 1, "artifact reads must not invoke the provider")
        self.assertEqual(first["schema_version"], "answer-artifact/1.0")
        self.assertEqual(first["algorithm_version"], "answer-artifact-projection/2.10.0")
        self.assertTrue(first["artifact_id"].startswith("artifact_"))
        self.assertEqual(first["anchor_contract"]["schema_version"], "atomic-provenance/1.0")
        self.assertEqual(first["anchor_contract"]["offset_unit"], "unicode_code_points")
        self.assertTrue(first["anchors"])
        self.assertTrue(all(first["answer"][item["start"]:item["end"]] == item["text"]
                            for item in first["anchors"]))
        self.assertEqual(first["verification"]["status"], "needs_review")
        self.assertEqual(first["verification"]["label"], "待核查")
        self.assertEqual(len(first["findings"]), 2)
        self.assertEqual(first["findings"][0]["status"], "needs_review")
        self.assertIn("金额字面核对", first["findings"][0]["explanation"])
        self.assertEqual(first["findings"][1]["status"], "needs_review")
        self.assertEqual(first["findings"][1]["review_flags"][0]["code"], "missing_local_reference")

    def test_exact_semantic_span_is_attached_to_matching_public_anchor(self):
        class SemanticFactProvider(FactProvider):
            def review_research_answer(self, request, cancel_event, timeout_seconds):
                exact = "999"
                start = request["draft"].index(exact)
                return {
                    "claims": [{
                        "id": "amount-claim", "start": start, "end": start + len(exact),
                        "exact_text": exact, "support_status": "unsupported",
                        "evidence_refs": [], "reason": "The returned evidence does not support this amount.",
                    }],
                    "coverage": {"status": "partial", "specific_gaps": ["A supported revenue amount is missing."]},
                }

        chat = self.chat(SemanticFactProvider())
        conversation_id, message = self.completed_answer(chat)
        artifact = AnswerArtifactService(chat).get(conversation_id, message["message_id"])
        anchor = next(item for item in artifact["anchors"] if item["text"] == "999")
        self.assertEqual(anchor["semantic_review"]["claim_id"], "amount-claim")
        self.assertEqual(anchor["semantic_review"]["support_status"], "unsupported")
        self.assertFalse(anchor["semantic_review"]["semantic_verified"])
        self.assertEqual(artifact["verification"]["semantic_review"]["coverage"]["status"], "partial")
        self.assertTrue(any("不构成人工专业核准" in item for item in artifact["limitations"]))
        self.assertFalse(any("所有要点都尚未经逐项语义审核" in item for item in artifact["limitations"]))
        finding = next(item for item in artifact["findings"] if "999" in item["text"])
        self.assertEqual(finding["semantic_status"], "unsupported")
        self.assertEqual(finding["status"], "needs_review")
        self.assertIn("semantic_unsupported", [flag["code"] for flag in finding["review_flags"]])
        self.assertEqual(artifact["answer"], message["content"])

    def test_number_exposes_containing_statement_review_without_claiming_exact_review(self):
        class StatementReviewProvider(FactProvider):
            def review_research_answer(self, request, cancel_event, timeout_seconds):
                exact = request["draft"].split("\n\n")[0]
                return {
                    "claims": [{"id": "statement", "start": 0, "end": len(exact), "exact_text": exact,
                                "support_status": "unsupported", "evidence_refs": [],
                                "reason": "The source does not support this statement."}],
                    "coverage": {"status": "partial", "specific_gaps": ["Correct amount needed."]},
                }
        chat = self.chat(StatementReviewProvider())
        conversation_id, message = self.completed_answer(chat)
        artifact = AnswerArtifactService(chat).get(conversation_id, message["message_id"])
        number = next(item for item in artifact["anchors"] if item["text"] == "999")
        self.assertEqual(number["semantic_review"]["scope"], "containing_statement")
        self.assertIn("999", number["semantic_review"]["reviewed_text"])
        self.assertFalse(number["semantic_review"]["semantic_verified"])

    def test_frozen_typed_facts_make_comparable_charts_and_complete_trace_shapes(self):
        chat = self.chat(FactProvider())
        conversation_id, message = self.completed_answer(chat)
        artifact = AnswerArtifactService(chat).get(conversation_id, message["message_id"])

        fact_sets = [item for item in artifact["datasets"] if item["rows"]]
        self.assertTrue(fact_sets)
        self.assertTrue(any(item["chart"] == {"type": "bar", "x": "label", "y": "value"}
                            for item in fact_sets if len(item["rows"]) >= 2))
        for dataset in fact_sets:
            for row in dataset["rows"]:
                self.assertIsInstance(row["value"], str)
                self.assertEqual(row["unit"], dataset["unit"])
                self.assertEqual(row["reference"]["kind"], "fact")
                trace = next(item for item in artifact["traces"] if item["id"] == row["trace_id"])
                self.assertEqual([step["layer"] for step in trace["steps"]], ["L4", "L3", "L2", "L1"])
                self.assertEqual(trace["source"]["source_sha256"],
                                 trace["steps"][3]["outputs"]["source_sha256"])
            self.assertIn("不自动证明", dataset["limitations"][0])

        citation_trace = next(item for item in artifact["traces"]
                              if item["reference"]["kind"] == "evidence")
        self.assertEqual([step["layer"] for step in citation_trace["steps"]], ["L4", "L3", "L2", "L1"])
        self.assertEqual(citation_trace["source"]["quote"],
                         next(item for item in chat.answer_context(conversation_id, message["message_id"])
                              ["source_snapshot"]["evidence"]["evidence"]
                              if item["evidence_id"] == citation_trace["reference"]["id"])["quote"])

    def test_projection_uses_frozen_fact_events_not_changed_evidence_database(self):
        chat = self.chat(FactProvider())
        conversation_id, message = self.completed_answer(chat)
        service = AnswerArtifactService(chat)
        before = service.get(conversation_id, message["message_id"])
        before_values = [(row["id"], row["value"]) for data in before["datasets"] for row in data["rows"]]

        with closing(sqlite3.connect(self.evidence_db)) as connection, connection:
            connection.execute("UPDATE facts SET value='123456789' WHERE company_id='MSFT'")
        after = service.get(conversation_id, message["message_id"])
        after_values = [(row["id"], row["value"]) for data in after["datasets"] for row in data["rows"]]

        self.assertEqual(after, before)
        self.assertEqual(after_values, before_values)
        self.assertNotIn("123456789", [value for _, value in after_values])

    def test_sdk_spoof_is_not_a_dataset_or_available_source_trace(self):
        chat = self.chat(SpoofProvider())
        conversation_id, message = self.completed_answer(chat, "Accept provider event")
        artifact = AnswerArtifactService(chat).get(conversation_id, message["message_id"])

        self.assertEqual(artifact["datasets"], [])
        self.assertEqual(artifact["findings"][0]["status"], "reference_invalid")
        trace = artifact["traces"][0]
        self.assertEqual(trace["status"], "unavailable")
        self.assertIsNone(trace["source"])
        self.assertEqual(trace["steps"][1]["operation"], "gap")
        self.assertFalse(any(item["reference"].get("id") == "spoof" for item in artifact["traces"]))

    def test_truncated_authoritative_result_fails_closed_for_valid_citation(self):
        chat = self.chat(FactProvider())
        conversation_id, message = self.completed_answer(chat)
        with closing(sqlite3.connect(chat.chat_db)) as connection, connection:
            row = connection.execute(
                "SELECT run_id,seq FROM events WHERE run_id=? AND type='tool_result' ORDER BY seq LIMIT 1",
                (message["run_id"],),
            ).fetchone()
            connection.execute(
                "UPDATE events SET payload_json=? WHERE run_id=? AND seq=?",
                (json.dumps({"truncated": True, "message": "event payload exceeded storage budget"}),
                 row[0], row[1]),
            )
        with self.assertRaises(ChatError) as caught:
            AnswerArtifactService(chat).get(conversation_id, message["message_id"])
        self.assertEqual(caught.exception.code, "citation_source_mismatch")

    def test_passage_table_is_raw_uncharted_and_corruption_fails_closed(self):
        documents = DocumentService(self.root / "documents")
        source = self.root / "table.html"
        source.write_text(
            "<!doctype html><html><body>ArtifactTableToken"
            "<table><tr><th>Period</th><th>Amount</th></tr>"
            "<tr><td>2025</td><td>9.4</td></tr></table></body></html>"
        )
        registered = documents.register_file(
            source, company_id="MSFT", source_key="artifact-table", title="Artifact table"
        )
        chat = self.chat(TableProvider(), documents)
        conversation_id, message = self.completed_answer(chat, "Read the table")
        service = AnswerArtifactService(chat)

        artifact = service.get(conversation_id, message["message_id"])
        table = next(item for item in artifact["datasets"] if "raw_rows" in item)
        self.assertEqual(table["raw_rows"][1], ["2025", "9.4"])
        self.assertEqual(table["rows"], [])
        self.assertIsNone(table["chart"])
        self.assertIsNone(table["unit"])
        self.assertIn("没有推测", table["limitations"][0])
        trace = next(item for item in artifact["traces"] if item["id"] == table["trace_id"])
        self.assertEqual(trace["source"]["document_version_id"], registered["document_version_id"])
        self.assertEqual(trace["source"]["source_sha256"], registered["source_sha256"])

        (documents.objects_path / registered["source_sha256"]).write_bytes(b"corrupt")
        with self.assertRaises(ChatError) as caught:
            service.get(conversation_id, message["message_id"])
        self.assertEqual(caught.exception.code, "citation_source_mismatch")

    def test_list_items_do_not_borrow_neighboring_citations_and_metadata_is_not_evidence(self):
        service = AnswerArtifactService(None)
        found = service._findings('**注意：**\n\n- 报告金额 [passage:p1]\n- 这说明没有披露其他原因',
                                  {('passage', 'p1'): {'valid': True}}, {('passage', 'p1'): 't'}, {})
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0]['status'], 'source_linked')
        self.assertEqual(found[1]['status'], 'needs_review')
        self.assertEqual(found[1]['references'], [])
        self.assertEqual(found[1]['review_flags'][0]['code'], 'unsupported_non_disclosure')
        event, item = service._event_reference([{'result': {'_retrieval_policy': {'passage_id': 'p1', 'quote': 'not a new source'}}}], 'passage', 'p1')
        self.assertIsNone(event)
        self.assertIsNone(item)

    def test_authoritative_event_budget_preserves_large_sources_but_is_bounded(self):
        chat = self.chat(FactProvider())
        conversation_id, message = self.completed_answer(chat)
        run_id = message['run_id']
        with closing(chat._connect()) as connection, connection:
            connection.execute("UPDATE runs SET status='running' WHERE run_id=?", (run_id,))
        chat._event(run_id, 'tool_result', {'authoritative': True, 'name': 'large_source', 'call_id': 'a', 'result': {'quote': 'x' * 90000}})
        chat._event(run_id, 'telemetry', {'text': 'x' * 90000})
        chat._event(run_id, 'tool_result', {'authoritative': True, 'name': 'too_large', 'call_id': 'b', 'result': {'quote': 'x' * 1000001}})
        events = chat.get_run(run_id)['events'][-3:]
        self.assertEqual(len(events[0]['result']['quote']), 90000)
        self.assertTrue(events[1]['truncated'])
        self.assertTrue(events[2]['truncated'])
        self.assertEqual(events[2]['name'], 'too_large')
        self.assertEqual(events[2]['call_id'], 'b')
        with closing(chat._connect()) as connection, connection:
            connection.execute("UPDATE runs SET status='completed' WHERE run_id=?", (run_id,))

    def test_wrong_conversation_and_non_assistant_message_fail_closed(self):
        chat = self.chat(FactProvider())
        conversation_id, message = self.completed_answer(chat)
        other = chat.create_conversation("MSFT")
        with self.assertRaises(ChatError) as caught:
            AnswerArtifactService(chat).get(other["conversation_id"], message["message_id"])
        self.assertEqual(caught.exception.code, "message_not_found")

        user_message = chat.get_conversation(conversation_id)["messages"][0]
        with self.assertRaises(ChatError) as caught:
            AnswerArtifactService(chat).get(conversation_id, user_message["message_id"])
        self.assertEqual(caught.exception.code, "message_not_eligible")


    def test_metric_period_same_amount_and_unrelated_scope_are_checked_locally(self):
        service = AnswerArtifactService(None)
        citations = {(kind, identifier): {"valid": True} for kind, identifier in
                     (("passage", "wrong"), ("passage", "right"), ("passage", "note"))}

        wrong_metric = service._findings(
            "单位为百万美元。2022财年营业收入为7,321。[passage:wrong]",
            citations, {}, {("passage", "wrong"): {
                "quote": "Year ended June 30 2022 Revenue 9,876 Operating income 7,321"
            }}, question="该公司2022财年营业收入是多少？")[0]
        self.assertEqual([flag["code"] for flag in wrong_metric["review_flags"]],
                         ["metric_identity_mismatch"])
        self.assertEqual(wrong_metric["review_flags"][0]["message"],
                         "指标不一致：回答写营业收入，所引原文数值对应营业利润；这组数不能用于回答当前问题。")

        wrong_period = service._findings(
            "单位为百万美元。2022财年营业收入为7,321。[passage:wrong]",
            citations, {}, {("passage", "wrong"): {
                "quote": "Quarter ended June 30 2022 Revenue 7,321"
            }}, question="该公司2022财年营业收入是多少？")[0]
        self.assertIn("period_identity_mismatch",
                      [flag["code"] for flag in wrong_period["review_flags"]])

        collision = service._findings(
            "单位为百万美元。2022财年营业收入为7,321。[passage:wrong] [passage:right]",
            citations, {}, {
                ("passage", "wrong"): {"quote": "Year ended 2022 Operating income 7,321"},
                ("passage", "right"): {"quote": "Year ended 2022 Revenue 7,321"},
            }, question="该公司2022财年营业收入是多少？")[0]
        self.assertNotIn("metric_identity_mismatch",
                         [flag["code"] for flag in collision["review_flags"]])

        unrelated = service._findings(
            "单位为百万美元。\n\n| 财年 | 营业收入 |\n|---|---:|\n| 2022 | 7,321 |\n\n数据来自所引资料。[passage:right] [passage:note]",
            citations, {}, {
                ("passage", "right"): {"quote": "Year ended 2022 Revenue 7,321"},
                ("passage", "note"): {"quote": "Prior periods were recast for internal reporting."},
            }, question="该公司2022财年营业收入是多少？")[0]
        self.assertIn("citation_scope_unrelated",
                      [flag["code"] for flag in unrelated["review_flags"]])

    def test_direct_disclosed_values_and_qualitative_compare_are_not_invented_calculation(self):
        service = AnswerArtifactService(None)
        finding = service._findings(
            "单位为百万美元。年度营业收入：2022年7,321，2021年6,400；2022年高于2021年。[passage:p]",
            {("passage", "p"): {"valid": True}}, {},
            {("passage", "p"): {"quote": "Year ended Revenue 2022 7,321 2021 6,400"}},
            question="按年度比较营业收入")[0]
        self.assertNotIn("missing_calculation_record",
                         [flag["code"] for flag in finding["review_flags"]])

    def test_metric_rules_do_not_embed_blocking_company_or_amount(self):
        source = Path(__file__).parents[1].joinpath("workbench", "answer_artifacts.py").read_text()
        self.assertNotIn("88,523", source)
        self.assertNotIn("Microsoft", source)

    def test_report_versions_and_fiscal_ranges_are_not_calculated_amounts(self):
        service = AnswerArtifactService(None)
        for text in (
            "第1版报告的增长方向不变。",
            "2021—2024财年连续增长，来源为 FY2024 Q4 新闻稿中的年度表。",
            "报告版本2的年度比较已更正。",
            "Report version 3 preserves the growth comparison.",
        ):
            with self.subTest(text=text):
                finding = service._findings(text, {}, {}, {})[0]
                codes = [flag["code"] for flag in finding["review_flags"]]
                self.assertNotIn("missing_calculation_record", codes)
                self.assertIn("missing_local_reference", codes)
        finding = service._findings("第1版报告：收入增长2.5%。", {}, {}, {})[0]
        self.assertIn("missing_calculation_record", [flag["code"] for flag in finding["review_flags"]])

    def test_increase_columns_bind_exact_currency_calculations(self):
        from workbench.answer_artifacts import _table_calculation_matches
        calculation = {"calculation_id": "calc-test", "value": "33207", "unit": "USD millions"}
        for label in ("较上年增加", "Annual increase", "变动"):
            text = f"| 财年 | {label}（百万美元） |\n|---|---:|\n| 2024 | 33,207 [calculation:calc-test] |"
            self.assertTrue(_table_calculation_matches(text, calculation))
            self.assertFalse(_table_calculation_matches(text.replace("33,207", "33,208"), calculation))
            self.assertFalse(_table_calculation_matches(text.replace("百万美元", "百万人民币"), calculation))

    def test_conclusion_with_source_clause_does_not_become_a_table_footnote(self):
        from workbench.answer_artifacts import _join_table_context
        table = "| 年份 | 收入 |\n|---|---:|\n| 2024 | 100 |"
        conclusion = "**结论：**收入连续增长。来源为年度报告。[passage:source]"
        self.assertEqual(_join_table_context([table, conclusion]), [table, conclusion])
        footer = "来源为年度报告。[passage:source]"
        self.assertEqual(_join_table_context([table, footer]), [table + "\n\n" + footer])

    def test_qualitative_growth_does_not_borrow_numbers_from_another_sentence(self):
        service = AnswerArtifactService(None)
        for separator in ("。", "；", ". ", "; "):
            text = f"旧版的123,456是营业利润{separator}收入连续增长的方向不变。"
            finding = service._findings(text, {}, {}, {})[0]
            self.assertNotIn("missing_calculation_record",
                             [flag["code"] for flag in finding["review_flags"]])
            self.assertIn("missing_local_reference", [flag["code"] for flag in finding["review_flags"]])
        finding = service._findings("收入增长3.5%。", {}, {}, {})[0]
        self.assertIn("missing_calculation_record", [flag["code"] for flag in finding["review_flags"]])

    def test_period_and_numeric_claims_ending_with_colon_are_not_hidden_as_titles(self):
        service = AnswerArtifactService(None)
        for text in ("微软财年截至每年6月30日，以下为GAAP营业收入：",
                     "# Revenue was USD 120 million"):
            finding = service._findings(text, {}, {}, {})[0]
            self.assertTrue(finding["review_flags"])
            self.assertEqual(finding["text"], text)

    def test_calculation_trace_subject_comes_from_output_not_run_owner(self):
        from workbench.answer_artifacts import _calculation_input_amounts
        cases = [({}, {"company_id": "IBM"}),
                 ({"arithmetic": {"output_context": {"company_id": "CRM"}}}, {"company_id": "CRM"}),
                 ({"arithmetic": {"output_context": {"company_id": None, "company_ids": ["CRM", "IBM"]}}},
                  {"company_id": None, "company_ids": ["CRM", "IBM"]}),
                 ({"arithmetic": {}}, {"company_id": None})]
        for extra, expected in cases:
            with self.subTest(extra=extra):
                fact = {"fact_id": "f", "evidence_id": "e", "value": "100", "unit": "USD millions"}
                calculation = {"calculation_id": "c", "company_id": "IBM", "formula": "example",
                               "value": "100", "unit": "USD millions", "operation": "sum",
                               "comparison_mode": "comparable", "data_version": {"test": "frozen"},
                               "inputs": [fact], "evidence_ids": ["e"], **extra}
                frozen = json.dumps(calculation, sort_keys=True)
                indexes = {"calculations": {"c": calculation}, "activities": {},
                           "document_sources": {"e": {"fact_id": "f", "evidence_id": "e",
                             "source_sha256": "1" * 64, "quote": "Revenue 100"}}}
                service = AnswerArtifactService(None)
                source, limitations = service._source_for_reference("calculation", "c", calculation, indexes)
                for key, value in expected.items():
                    self.assertEqual(source[key], value)
                trace = service._calculation_trace({"message_id": "m"}, calculation, None, source,
                                                   True, True, limitations, indexes)
                level = next(step for step in trace["steps"] if step["layer"] == "L2")
                for key, value in expected.items():
                    self.assertEqual(level["inputs"][key], value)
                self.assertTrue(_calculation_input_amounts(calculation, source))
                wrong = {**source, "company_id": "UNRELATED"}
                self.assertEqual(_calculation_input_amounts(calculation, wrong), set())
                if "company_ids" in source:
                    source["company_ids"].append("UNRELATED")
                    self.assertEqual(_calculation_input_amounts(calculation, source), set())
                self.assertEqual(json.dumps(calculation, sort_keys=True), frozen)

    def test_failed_calculation_result_is_not_a_legacy_calculation(self):
        service = AnswerArtifactService(None)
        legacy = {"calculation": "year_over_year_percent_change", "value": "10", "unit": "%",
                  "current_fact": {"value": "110"}, "prior_fact": {"value": "100"}}
        error = {"error": "not_found_or_out_of_scope",
                 "message": "both facts must belong to the selected company"}
        events = [{"name": name, "result": result, "authoritative": True}
                  for name in ("calculate_yoy", "calculate_facts", "calculate_regression", "calculate_forecast")
                  for result in (error, {"calculation": error}, {"calculations": [error]})]
        events.append({"name": "calculate_yoy", "result": legacy, "authoritative": True})
        records, event_map, limitations = service._calculation_records(
            {}, {}, events, {}, check_input=sealed_input({}, {}, events, {}))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["status"], "legacy")
        self.assertEqual(records[0]["value"], "10")
        self.assertEqual(event_map, {})
        self.assertTrue(any("失败响应" in item for item in limitations))
        self.assertEqual(events[0]["result"], error)
        _, _, fact_limits = service._fact_datasets({"company_id": "IBM"}, {},
            [{"name": "calculate_yoy", "result": error}], {}, {})
        self.assertTrue(any("失败的取数或计算调用" in item for item in fact_limits))
        self.assertFalse(any("company or data version" in item for item in fact_limits))

    def test_historical_financing_answer_replay_uses_only_cited_calculation_inputs(self):
        """Replay relevant msg_57c4... fields without depending on the production DB."""
        sha = "169c062bba9ab8140a2b7633048e890400111f533cf2ec56a671f2374668b91c"
        version = {"build_version": "deterministic-financial-evidence/1.1.0",
                   "input_hashes": {"issuer-filing": sha}}
        facts = [
            {"activity_id": "act-repurchase", "basis": "cash flow statement; financing cash outflow",
             "company_id": "MSFT", "evidence_id": "ev-repurchase", "fact_id": "fact-repurchase",
             "metric": "payments_for_repurchase_of_common_stock", "period_end": "2025-06-30",
             "period_kind": "quarter", "period_start": "2025-04-01", "unit": "USD millions", "value": "4546"},
            {"activity_id": "act-dividend", "basis": "cash flow statement; financing cash outflow",
             "company_id": "MSFT", "evidence_id": "ev-dividend", "fact_id": "fact-dividend",
             "metric": "payments_of_dividends", "period_end": "2025-06-30", "period_kind": "quarter",
             "period_start": "2025-04-01", "unit": "USD millions", "value": "6169"},
        ]
        evidence = [
            {"evidence_id": "ev-repurchase", "source_id": "issuer-filing", "source_sha256": sha,
             "quote": "Common stock repurchased (4,546) (4,210) (18,420) (17,254)", "locator": {"row_index": 25}},
            {"evidence_id": "ev-dividend", "source_id": "issuer-filing", "source_sha256": sha,
             "quote": "Common stock cash dividends paid (6,169) (5,574) (24,082) (21,771)", "locator": {"row_index": 26}},
        ]
        source = {"source_id": "issuer-filing", "sha256": sha, "title": "Issuer quarterly filing",
                  "url": "https://issuer.example/filing"}
        calculation = {
            "calculation_id": "calc-history", "schema_version": "recorded-calculation-v1",
            "operation": "sum", "formula": "fact[0] + fact[1]", "value": "10715",
            "unit": "USD millions", "inputs": facts, "evidence_ids": ["ev-repurchase", "ev-dividend"],
            "data_version": version, "company_id": "MSFT", "comparison_mode": "comparable",
            "limitations": ["This sum includes only the selected financing line items."],
        }
        frozen = {"company_id": "MSFT", "sources": [source], "evidence": evidence,
                  "claims": [], "activities": [], "facts": facts}
        indexes = {
            "sources": {source["source_id"]: source},
            "evidence": {item["evidence_id"]: item for item in evidence},
            "claims": {}, "activities": {},
            "facts": {item["fact_id"]: item for item in facts},
        }
        service = AnswerArtifactService(None)
        answer = {"company_id": "MSFT"}
        snapshot = {"data_version": version, "evidence": frozen}
        events = [{"authoritative": True, "truncated": False, "name": "calculate_facts",
                   "result": calculation}]
        check_input = sealed_input(answer, snapshot, events, indexes)
        records, _, _ = service._calculation_records(
            answer, snapshot, events, indexes, check_input=check_input)
        self.assertEqual(len(records), 1)
        calc_source, limitations = service._source_for_reference(
            "calculation", "calc-history", calculation,
            {**indexes, "calculations": {"calc-history": records[0]}})
        self.assertEqual(limitations, [])

        p1, p2, calc = "issuer-return", "cash-flow-table", "calc-history"
        text = (
            f"发行方披露返还94亿美元。[passage:{p1}]\n\n"
            "现金流量表列示回购现金支出45.46亿美元、现金分红61.69亿美元，"
            f"两项合计107.15亿美元；这只是两项融资现金流之和。[passage:{p2}] [calculation:{calc}]\n\n"
            f"若分析实际融资现金流，则采用107.15亿美元。[calculation:{calc}]"
        )
        citation_map = {(kind, identifier): {"valid": True} for kind, identifier in
                        (("passage", p1), ("passage", p2), ("calculation", calc))}
        citation_sources = {
            ("passage", p1): {"quote": "The issuer returned $9.4 billion to shareholders."},
            ("passage", p2): {"company_id": "MSFT", "source_sha256": sha,
                              "quote": "CASH FLOWS STATEMENTS (In millions) Common stock repurchased (4,546) "
                                       "Common stock cash dividends paid (6,169) Net cash used in financing (10,844)"},
            ("calculation", calc): calc_source,
        }
        findings = service._findings(text, citation_map, {}, citation_sources, records, {})
        self.assertNotIn("金额字面核对", findings[1]["explanation"])
        self.assertEqual(findings[1]["review_flags"], [])
        self.assertEqual([flag["code"] for flag in findings[2]["review_flags"]],
                         ["calculation_scope_mislabel"])

        cases = {}
        wrong = deepcopy(calculation); wrong["inputs"][0]["value"] = "9999"; cases["input"] = wrong
        wrong = deepcopy(calculation); wrong["inputs"][0]["evidence_id"] = "ev-other"; cases["evidence"] = wrong
        wrong = deepcopy(calculation); wrong["data_version"] = {"build_version": "other"}; cases["version"] = wrong
        wrong_indexes = deepcopy(indexes); wrong_indexes["sources"]["issuer-filing"]["sha256"] = "0" * 64
        for label, candidate in cases.items():
            with self.subTest(label=label):
                self.assertFalse(service._valid_calculation(
                    candidate, answer, snapshot, indexes, check_input=check_input)[0])
        self.assertFalse(service._valid_calculation(
            calculation, answer, snapshot, wrong_indexes, check_input=check_input)[0])
        forged_source = deepcopy(calc_source); forged_source["sources"][0]["quote"] = "unrelated row 999"
        forged_findings = service._findings(text.split("\n\n")[1], citation_map, {},
                                            {("passage", p2): citation_sources[("passage", p2)],
                                             ("calculation", calc): forged_source}, records, {})
        self.assertIn("金额字面核对", forged_findings[0]["explanation"])
        self.assertIn("45.46亿美元", forged_findings[0]["explanation"])
        self.assertNotIn("USD 4546000000", forged_findings[0]["explanation"])


if __name__ == "__main__":
    unittest.main()
