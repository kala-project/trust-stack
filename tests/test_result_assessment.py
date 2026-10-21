import hashlib
import json
from pathlib import Path
import unittest

from workbench.answer_artifacts import AnswerArtifactService, ALGORITHM_VERSION, SCHEMA_VERSION
from workbench.evidence_checks import ReceiptBackedEvent, TrustedEvidenceAssembler, VerifiedRecordSource


ROOT = Path(__file__).resolve().parents[1]


def sealed_input(answer, snapshot, events, indexes):
    company_id, run_id = answer.get("company_id", "ACME"), answer.get("run_id", "run-1")
    snapshot = dict(snapshot)
    snapshot.setdefault("evidence", {"company_id": company_id})
    receipts = []
    for index, event in enumerate(events):
        payload = {"type": "tool_result", "authoritative": True,
                   "truncated": False, **event}
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        receipts.append(ReceiptBackedEvent(str(index), hashlib.sha256(raw).hexdigest(), raw))
    source = VerifiedRecordSource(
        run_id=run_id, company_id=company_id, company_ids=[company_id],
        snapshot_bytes=json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode(),
        event_receipts=receipts)
    return TrustedEvidenceAssembler.assemble(source, run_id=run_id, company_id=company_id)


def frozen_fixture():
    source = {"source_id": "src-1", "sha256": "a" * 64, "title": "Filed source", "url": "https://example.invalid/filing"}
    evidence = [
        {"evidence_id": "ev-1", "source_id": "src-1", "source_sha256": source["sha256"], "quote": "Revenue was 100.", "locator": "p1"},
        {"evidence_id": "ev-2", "source_id": "src-1", "source_sha256": source["sha256"], "quote": "Revenue was 80.", "locator": "p2"},
    ]
    facts = []
    for identifier, value, evidence_id, period in (
        ("fact-current", "100", "ev-1", "2025-06-30"),
        ("fact-prior", "80", "ev-2", "2024-06-30"),
    ):
        facts.append({
            "fact_id": identifier, "company_id": "ACME", "metric": "revenue",
            "value": value, "unit": "USD millions", "period_kind": "quarter",
            "period_start": None, "period_end": period, "basis": "GAAP",
            "evidence_id": evidence_id, "activity_id": f"activity-{identifier}",
            "source_id": "src-1",
        })
    indexes = {
        "sources": {source["source_id"]: source},
        "evidence": {item["evidence_id"]: item for item in evidence},
        "claims": {}, "activities": {}, "facts": {fact["fact_id"]: fact for fact in facts},
    }
    snapshot = {"data_version": {"build_version": "fixture-v1", "input_hashes": {"src-1": "a" * 64}}, "evidence": {"company_id": "ACME", "sources": [source], "evidence": evidence}}
    answer = {"company_id": "ACME", "message_id": "message-1", "run_id": "run-1", "question": "收入同比增长是多少？", "answer": "收入同比增长 25%。"}
    calculation = {
        "calculation_id": "calc-1", "schema_version": "calculation/1.0",
        "operation": "percent_change", "formula": "(current-prior)/abs(prior)*100",
        "value": "25.00", "unit": "%", "inputs": facts,
        "evidence_ids": ["ev-1", "ev-2"], "data_version": {"build_version": "fixture-v1", "input_hashes": {"src-1": "a" * 64}},
        "company_id": "ACME", "comparison_mode": "comparable", "limitations": [],
    }
    return answer, snapshot, indexes, calculation


class ResultAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.service = AnswerArtifactService(None)

    def finding(self, text, calculations=None):
        return self.service._findings(text, {}, {}, {}, calculations or [])[0]

    def test_schema_is_additive_but_projection_algorithm_is_bumped(self):
        self.assertEqual(SCHEMA_VERSION, "answer-artifact/1.0")
        self.assertEqual(ALGORITHM_VERSION, "answer-artifact-projection/2.10.0")

    def test_valid_reference_is_not_a_semantic_trusted_flag(self):
        finding = self.service._findings(
            "管理层称收入改善 [evidence:ev-1]",
            {("evidence", "ev-1"): {"valid": True}},
            {("evidence", "ev-1"): "trace-1"},
            {("evidence", "ev-1"): {"quote": "收入为100"}},
        )[0]
        self.assertEqual(finding["classification"], "source_statement")
        self.assertEqual(finding["status"], "source_linked")
        self.assertNotIn("trusted", finding)
        self.assertIn("尚未审核", finding["explanation"])

    def test_non_disclosure_is_flagged_and_scoped_search_is_distinguished(self):
        unsupported = self.finding("公司没有披露该变化的原因。")
        scoped = self.finding("在本次检索到的资料中，没有披露该变化的原因。")
        self.assertEqual(unsupported["classification"], "evidence_gap")
        self.assertEqual(unsupported["status"], "needs_review")
        self.assertEqual(unsupported["review_flags"][0]["code"], "unsupported_non_disclosure")
        self.assertEqual(unsupported["review_flags"][0]["severity"], "high")
        self.assertEqual(scoped["review_flags"][0]["code"], "source_coverage_required")
        self.assertIn("不会自动改写", scoped["review_flags"][0]["action"])

    def test_text_only_calculation_is_not_promoted_to_recorded_calculation(self):
        finding = self.finding("收入同比增长 25%。")
        self.assertEqual(finding["status"], "needs_review")
        self.assertEqual(finding["classification"], "interpretation")
        self.assertEqual(finding["review_flags"][0]["code"], "missing_calculation_record")

    def test_literal_output_matches_actual_record_without_inventing_operation(self):
        _, _, _, calculation = frozen_fixture()
        calculation = {**calculation, "status": "recorded"}
        uncited = self.finding("收入同比增长 25%。", [calculation])
        self.assertEqual(uncited["review_flags"][0]["code"], "missing_calculation_record")
        finding = self.service._findings("收入同比增长 25%。 [calculation:calc-1]", {("calculation", "calc-1"): {"valid": True}}, {}, {}, [calculation])[0]
        self.assertEqual(finding["classification"], "recorded_calculation")
        self.assertFalse(finding["review_flags"])

    def test_unitless_evidence_quote_uses_only_matching_returned_frozen_facts(self):
        source = {"source_id": "src-cashflow", "sha256": "b" * 64}
        evidence = {"evidence_id": "ev-cashflow", "source_id": source["source_id"],
                    "source_sha256": source["sha256"],
                    "quote": "Common stock repurchased (4,546); cash dividends paid (6,169)."}
        def fact(identifier, value, metric):
            return {
                "fact_id": identifier, "company_id": "ACME", "metric": metric,
                "value": value, "unit": "USD millions", "period_kind": "quarter",
                "period_start": "2025-04-01", "period_end": "2025-06-30",
                "basis": "cash flow statement", "evidence_id": evidence["evidence_id"],
                "activity_id": "activity-" + identifier,
            }
        facts = [fact("fact-buyback", "4546", "repurchases"),
                 fact("fact-dividends", "6169", "dividends")]
        indexes = {
            "sources": {source["source_id"]: source},
            "evidence": {evidence["evidence_id"]: evidence},
            "facts": {item["fact_id"]: item for item in facts},
        }
        version = {"build_version": "frozen-v1"}
        snapshot = {"data_version": version}
        answer = {"company_id": "ACME"}
        event = {"authoritative": True, "name": "get_company_facts", "result": {
            "company_id": "ACME", "data_version": version,
            "facts": [{**item, "source_id": source["source_id"]} for item in facts],
        }}
        amounts = self.service._frozen_fact_amounts(
            answer, snapshot, [event], indexes,
            check_input=sealed_input(answer, snapshot, [event], indexes))
        refs = {("evidence", evidence["evidence_id"]): {"valid": True}}
        sources = {("evidence", evidence["evidence_id"]): evidence}
        def findings(text, available=amounts):
            return self.service._findings(text, refs, {}, sources, [], available)

        supported = findings("回购付款45.46亿美元、现金股息61.69亿美元。[evidence:ev-cashflow]")[0]
        self.assertEqual(supported["status"], "source_linked")
        self.assertNotIn("金额字面核对", supported["explanation"])
        for text in ("回购付款45.47亿美元。[evidence:ev-cashflow]",
                     "回购付款45.46亿元人民币。[evidence:ev-cashflow]",
                     "回购付款-45.46亿美元。[evidence:ev-cashflow]"):
            self.assertEqual(findings(text)[0]["status"], "needs_review", text)

        neighboring = findings("回购付款45.46亿美元。[evidence:ev-cashflow]\n\n现金股息61.69亿美元。")
        self.assertEqual(neighboring[0]["status"], "source_linked")
        self.assertEqual(neighboring[1]["review_flags"][0]["code"], "missing_numeric_reference")

        altered = {**event, "result": {**event["result"], "facts": [
            {**event["result"]["facts"][0], "value": "4547"}
        ]}}
        self.assertEqual(self.service._frozen_fact_amounts(
            answer, snapshot, [altered], indexes,
            check_input=sealed_input(answer, snapshot, [altered], indexes)), {})
        for change in ({'source_id': 'wrong-source'}, {'unit': 'CNY millions'}):
            wrong = {**event, 'result': {**event['result'], 'facts': [{**event['result']['facts'][0], **change}]}}
            self.assertEqual(self.service._frozen_fact_amounts(
                answer, snapshot, [wrong], indexes,
                check_input=sealed_input(answer, snapshot, [wrong], indexes)), {})
        wrong_version = {**event, 'result': {**event['result'], 'data_version': {'build_version': 'another'}}}
        self.assertEqual(self.service._frozen_fact_amounts(
            answer, snapshot, [wrong_version], indexes,
            check_input=sealed_input(answer, snapshot, [wrong_version], indexes)), {})
        untrusted = [{**event, "authoritative": False}]
        with self.assertRaisesRegex(Exception, "intact tool results"):
            sealed_input(answer, snapshot, untrusted, indexes)
        self.assertEqual(findings("回购付款45.46亿美元。[evidence:ev-cashflow]", {})[0]["status"],
                         "needs_review")

    def test_authoritative_calculation_validation_rejects_scope_dimension_and_sources(self):
        answer, snapshot, indexes, calculation = frozen_fixture()
        check_input = sealed_input(answer, snapshot, [], indexes)
        valid, reason = self.service._valid_calculation(
            calculation, answer, snapshot, indexes, check_input=check_input)
        self.assertTrue(valid, reason)

        wrong_scope = {**calculation, "company_id": "OTHER"}
        self.assertFalse(self.service._valid_calculation(wrong_scope, answer, snapshot, indexes, check_input=check_input)[0])
        wrong_version = {**calculation, "data_version": "latest"}
        self.assertFalse(self.service._valid_calculation(wrong_version, answer, snapshot, indexes, check_input=check_input)[0])
        wrong_dimension = {**calculation, "inputs": [calculation["inputs"][0], {**calculation["inputs"][1], "basis": "non-GAAP"}]}
        self.assertFalse(self.service._valid_calculation(wrong_dimension, answer, snapshot, indexes, check_input=check_input)[0])
        missing_source = {key: dict(value) for key, value in indexes.items()}
        missing_source["sources"] = {}
        self.assertFalse(self.service._valid_calculation(calculation, answer, snapshot, missing_source, check_input=check_input)[0])

    def test_actual_event_yields_record_and_legacy_yoy_has_no_fake_id(self):
        answer, snapshot, indexes, calculation = frozen_fixture()
        event = {"seq": 7, "name": "calculate_facts", "call_id": "call-7", "result": calculation}
        check_input = sealed_input(answer, snapshot, [event], indexes)
        records, event_map, limitations = self.service._calculation_records(
            answer, snapshot, [event], indexes, check_input=check_input)
        self.assertEqual(records[0]["calculation_id"], "calc-1")
        self.assertEqual(event_map["calc-1"]["seq"], event["seq"])
        self.assertEqual(event_map["calc-1"]["result"], event["result"])
        self.assertEqual(event_map["calc-1"]["type"], "tool_result")
        self.assertFalse(limitations)

        legacy_result = {
            "company_id": "ACME", "calculation": "year_over_year_percent_change",
            "value": "25.00", "unit": "%", "current_fact": calculation["inputs"][0],
            "prior_fact": calculation["inputs"][1], "evidence_ids": calculation["evidence_ids"],
            "data_version": {"build_version": "fixture-v1", "input_hashes": {"src-1": "a" * 64}}, "limitations": [],
        }
        legacy_events = [{"name": "calculate_yoy", "result": legacy_result}]
        records, event_map, limitations = self.service._calculation_records(
            answer, snapshot, legacy_events, indexes,
            check_input=sealed_input(answer, snapshot, legacy_events, indexes))
        self.assertIsNone(records[0]["calculation_id"])
        self.assertEqual(records[0]["status"], "legacy")
        self.assertIsNone(records[0]["reference"])
        self.assertEqual(event_map, {})
        self.assertTrue(limitations)

    def test_calculation_trace_uses_actual_four_layers_and_source_list(self):
        answer, snapshot, indexes, calculation = frozen_fixture()
        indexes["calculations"] = {"calc-1": calculation}
        source, limitations = self.service._source_for_reference("calculation", "calc-1", calculation, indexes)
        event = {
            "seq": 7, "name": "calculate_facts", "call_id": "call-7",
            "observed_arguments": {"operation": "percent_change", "fact_ids": ["fact-current", "fact-prior"]},
        }
        trace = self.service._calculation_trace(
            answer, calculation, event, source, True, True, limitations, indexes
        )
        self.assertEqual([step["layer"] for step in trace["steps"]], ["L4", "L3", "L2", "L1"])
        self.assertEqual(trace["steps"][1]["inputs"]["call_id"], "call-7")
        self.assertEqual(trace["steps"][1]["outputs"]["decimal_value"], "25.00")
        self.assertEqual(len(trace["source"]["sources"]), 2)
        self.assertNotIn("quote", trace["source"], "calculation source must not masquerade as one quote")

    def test_presentation_uses_exact_first_conclusion_and_keeps_every_dataset_auditable(self):
        findings = [self.finding("第一项具体结论。"), self.finding("第二项具体结论。")]
        datasets = [
            {"id": "other", "title": "利润", "description": "年度", "rows": []},
            {"id": "revenue", "title": "revenue", "description": "quarterly trend", "rows": [{"value": "1"}]},
        ]
        result = self.service._presentation("Compare revenue trend", findings, datasets)
        self.assertEqual(result["概览"], "第一项具体结论。")
        self.assertEqual(result["recommended_dataset_id"], "revenue")
        self.assertEqual(result["audit_accessible_dataset_ids"], ["other", "revenue"])
        self.assertIn("期间、指标、口径及所问年份一致", result["recommended_dataset_reason"])

    def test_readonly_four_case_capture_is_available_without_running_models(self):
        capture = ROOT / "var/fintech-redesign/live-4/results.json"
        if not capture.exists():
            self.skipTest("readonly four-case capture is not present")
        payload = json.loads(capture.read_text())
        self.assertEqual(payload["fixed_denominator"], 4)
        self.assertEqual(payload["selected_case_ids"], [
            "msft-revenue-gaap-3y", "msft-segments-fy24-original-v-recast",
            "aapl-eps-adjusted-nuance", "msft-ai-roi-insufficient",
        ])
        self.assertEqual([case["case_id"] for case in payload["cases"]], payload["selected_case_ids"])


if __name__ == "__main__":
    unittest.main()
