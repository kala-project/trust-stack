import http.client
import json
from copy import deepcopy
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from tests.test_recorded_calculations import wait
from tests.test_report_publications import ReportProvider
from tests.test_verification_executor import ControlledAuthorVerifier
from workbench.answer_artifacts import AnswerArtifactService, _stable_id
from workbench.build import build_database
from workbench.chat import ChatError, ChatService
from workbench.report_reader import (
    CHAPTERS, SCHEMA_VERSION, build_reader_evidence, build_reader_evidence_from_payload,
    build_reader_payload, build_reader_source, list_reader_reports,
    load_reader_artifact_snapshot, load_required_reader_snapshots, report_id_for, resolve_report,
    frozen_trace_details, _insert_artifact_snapshot, _ensure_reader_schema, _source_summary,
)
from workbench.server import EvidenceHTTPServer
from workbench.worker import DurableWorker


class ReportReaderPayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.TemporaryDirectory()
        cls.db = Path(cls.root.name) / "evidence.sqlite3"
        build_database(db_path=cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.root.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = ReportProvider()
        self.chat = ChatService(self.db, Path(self.temp.name) / "chat.sqlite3", self.provider)
        self.addCleanup(self.chat.close)

    def _artifact(self):
        initial = self.chat.create_and_submit("MSFT", "微软收入相对以前的累计变化")
        run = wait(self.chat, initial["run_id"])
        self.assertEqual(run["status"], "completed", run.get("error"))
        message = self.chat.get_conversation(initial["conversation_id"])["messages"][-1]
        return AnswerArtifactService(self.chat).get(initial["conversation_id"], message["message_id"])

    def test_time_axis_identity_survives_publication_to_reader_projection(self):
        from workbench.report_publications import prepare, project_datasets
        facts = {str(year): {"value": str(year), "unit": "CNY", "company_id": "TEST",
                 "metric": "revenue", "basis": "reported", "period_kind": "year",
                 "period_start": f"{year}-01-01", "period_end": f"{year}-12-31",
                 "evidence_id": f"e-{year}"} for year in (2023, 2024)}
        publication = prepare(title="受控时间轴", blocks=[{
            "classification": "evidence_gap", "parts": [{"kind": "text", "text": "仅用于投影测试"}],
            "support": [], "reasoning": "", "limitations": []}],
            refs={"evidence": {fact["evidence_id"]: {} for fact in facts.values()}, "passage": {"footnote": {}}},
            facts=facts, calculations={}, max_chars=4000,
            datasets=[{"title": "受控事实", "description": "仅测试投影，不是真实报告",
                       "support": [{"kind": "passage", "id": "footnote"}], "display": "line", "time_axis": "period_end", "points": [
                           {"label": year, "kind": "fact", "id": year} for year in facts]}])
        artifact = self._artifact()
        artifact["datasets"] = project_datasets(publication, {
            **{("evidence", fact["evidence_id"]): f"trace-{year}" for year, fact in facts.items()},
            ("passage", "footnote"): "trace-footnote"})
        artifact["traces"].append({"id": "trace-footnote", "status": "available",
            "reference": {"kind": "passage", "id": "footnote"},
            "source": {"title": "受控脚注", "quote": "仅测试脚注原文回放，不是真实报告。"}})
        payload = build_reader_payload(artifact)
        dataset = next(block for chapter in payload["chapters"] for block in chapter["blocks"]
                       if block["type"] == "dataset")
        description = next(anchor for anchor in payload["anchors"]
                           if anchor["id"] == dataset["description_anchor_id"])
        self.assertEqual(description["reference_keys"], ["passage:footnote"])
        bundle = payload["evidence"][description["evidence_id"]]
        self.assertIn("仅测试投影，不是真实报告", bundle["claim"])
        self.assertEqual(bundle["traces"][0]["source"]["quote"], "仅测试脚注原文回放，不是真实报告。")
        self.assertFalse(bundle["verification"]["semantic_verified"])
        self.assertEqual(dataset["chart"], artifact["datasets"][0]["chart"])
        self.assertEqual(dataset["chart"]["time_axis"]["version"], "utc-period-axis/1")
        for source, row in zip(artifact["datasets"][0]["rows"], dataset["rows"]):
            for field in ("id", "record_kind", "company_id", "metric", "value", "unit", "basis", "period", "reference", "trace_id"):
                self.assertEqual(row[field], source[field], field)
            self.assertTrue(any(anchor["id"] == row["anchor_id"] for anchor in payload["anchors"]))

    def test_structured_publication_compiles_to_reader_payload(self):
        artifact = self._artifact()
        payload = build_reader_payload(artifact)

        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["report"]["title"], "完整研究报告")
        self.assertEqual(artifact["reader_report_id"], payload["report"]["id"])
        self.assertEqual([chapter["title"] for chapter in payload["chapters"]],
                         [title for _identifier, title in CHAPTERS])
        self.assertEqual(payload["question"]["original"], artifact["question"])
        self.assertEqual(payload["question"]["normalized"], "")
        self.assertEqual(payload, build_reader_payload(artifact))
        self.assertRegex(payload["payload_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotRegex(json.dumps(payload["chapters"], ensure_ascii=False), r"\[(claim|evidence|passage|calculation|query):")
        self.assertNotRegex(json.dumps(payload["anchors"], ensure_ascii=False), r"\[(claim|evidence|passage|calculation|query):")
        self.assertNotRegex(json.dumps(payload["evidence"], ensure_ascii=False), r"\[(claim|evidence|passage|calculation|query):")
        conclusion = payload["chapters"][0]["blocks"][1]
        self.assertIn("明确绑定的差额", conclusion["text"])
        self.assertNotIn("推理说明（分析判断，未独立验证）", conclusion["text"])
        self.assertNotIn("限制：", conclusion["text"])
        self.assertEqual(conclusion["reasoning"], artifact["findings"][0]["reasoning"])
        self.assertIn(artifact["findings"][0]["limitations"][0], payload["chapters"][3]["blocks"][0]["items"])
        self.assertEqual(len(conclusion["anchor_ids"]), 2)
        self.assertNotIn("推理说明（分析判断，未独立验证）", payload["evidence"][
            next(anchor for anchor in payload["anchors"] if anchor["id"] == conclusion["anchor_ids"][0])["evidence_id"]
        ]["claim"])

        numeric = next(anchor for anchor in payload["anchors"] if anchor["kind"] == "number")
        self.assertEqual(numeric["support_status"], "calculated")
        bundle = payload["evidence"][numeric["evidence_id"]]
        self.assertEqual(bundle["claim"], numeric["text"])
        self.assertIn("输入、公式、单位、期间和计算结果", bundle["verification"]["method"])
        self.assertEqual(bundle["verification"]["boundary"], "只核查当前选中的主张或数值，不自动外推到报告其他结论。")
        self.assertTrue(bundle["references"])
        self.assertTrue(bundle["traces"])
        calculation_trace = next(trace for trace in bundle["traces"] if trace.get("calculation"))
        self.assertEqual(calculation_trace["calculation"]["value"], "20252")
        self.assertTrue(calculation_trace["calculation"]["formula"])
        self.assertEqual(len(calculation_trace["source"]["inputs"]), 2)
        self.assertEqual(len(calculation_trace["calculation"]["arithmetic"]["operands"]), 2)
        extracted = build_reader_evidence(artifact, numeric["id"])
        self.assertEqual(extracted["anchor"], numeric)
        self.assertEqual(extracted["evidence"], bundle)

    def test_contained_claim_review_reaches_formal_reader_anchor_without_status_promotion(self):
        artifact = self._artifact()
        anchor = next(item for item in artifact["anchors"] if item["kind"] == "assertion")
        review = {
            "status": "mixed", "scope": "contained_claims", "review_scope": "partial",
            "semantic_verified": False,
            "claims": [
                {"id": "supported", "exact_text": "受控支持句", "support_status": "supported_by_evidence",
                 "evidence_refs": [], "reason": "受控支持理由"},
                {"id": "negative", "exact_text": "受控冲突句", "support_status": "conflicting",
                 "evidence_refs": [], "reason": "受控冲突理由"},
            ],
        }
        original_status = anchor["support_status"]
        anchor["semantic_review"] = review
        payload = build_reader_payload(artifact)
        projected = next(item for item in payload["anchors"] if item["id"] == anchor["id"])
        evidence = build_reader_evidence_from_payload(payload, anchor["id"])
        self.assertEqual(projected["semantic_review"], review)
        self.assertEqual(evidence["anchor"]["semantic_review"], review)
        self.assertEqual(projected["support_status"], original_status)
        self.assertFalse(projected["semantic_review"]["semantic_verified"])

    def test_required_verifier_outcomes_are_distinct_without_promoting_binding_or_human_status(self):
        artifact = self._artifact()
        finding = artifact["findings"][0]
        finding["classification"] = "interpretation"
        outcomes = [
            "supported", "corrected_and_rechecked", "conflict_disclosed",
            "insufficient_material_disclosed",
        ]
        projected = [{
            "node_id": finding["id"], "check_kind": f"check-{index}",
            "outcome": outcome, "explanation": f"public explanation {index}",
            "receipts": [{"receipt_id": f"receipt-{index}", "sha256": "a" * 64}],
        } for index, outcome in enumerate(outcomes)]
        artifact["required_verifier_findings"] = deepcopy(projected)
        finding["required_verifier_findings"] = deepcopy(projected)
        whole = next(anchor for anchor in artifact["anchors"]
                     if anchor["finding_id"] == finding["id"]
                     and anchor["text"] == finding["text"])
        whole["required_verifier_findings"] = deepcopy(projected)

        payload = build_reader_payload(artifact)
        block = next(block for chapter in payload["chapters"] for block in chapter["blocks"]
                     if block["id"] == finding["id"])
        self.assertEqual(block["classification"], "interpretation")
        self.assertEqual([item["outcome"] for item in block["required_verifier_findings"]], outcomes)
        self.assertEqual(len({item["outcome_label"] for item in block["required_verifier_findings"]}), 4)
        self.assertEqual(payload["report"]["status"], "needs_review")
        self.assertEqual(payload["report"]["status_label"], "Pending human approval")
        reader_whole = next(anchor for anchor in payload["anchors"] if anchor["id"] == whole["id"])
        self.assertEqual(reader_whole["support_status"], whole["support_status"])
        self.assertEqual([item["outcome"] for item in reader_whole["required_verifier_findings"]], outcomes)
        self.assertEqual(reader_whole["support_label"], "Reviewed as a whole; evidence not annotated sentence by sentence")
        bundle = payload["evidence"][reader_whole["evidence_id"]]
        self.assertEqual(bundle["verification"]["precise_binding_status"], "Not established")
        self.assertEqual(len(bundle["verification"]["required_verifier_findings"]), 4)
        self.assertFalse(bundle["verification"]["semantic_verified"])
        self.assertTrue(all("required_verifier_findings" not in anchor
                            for anchor in payload["anchors"]
                            if anchor["finding_id"] == finding["id"]
                            and anchor["id"] != whole["id"]))

    def test_frozen_trace_supplement_does_not_rewrite_payload_or_cross_artifacts(self):
        artifact = self._artifact()
        payload = load_required_reader_snapshots(
            self.chat, report_id=artifact["reader_report_id"])[0]
        self.assertNotIn("required_verifier_findings", payload)
        before = json.dumps(payload, sort_keys=True)
        anchor = next(item for item in payload["anchors"] if item["kind"] == "number")
        detail = frozen_trace_details(payload, artifact, anchor["id"])
        self.assertEqual(detail["origin"], "same_frozen_artifact")
        self.assertTrue(any(trace.get("calculation", {}).get("formula") for trace in detail["traces"]))
        self.assertEqual(json.dumps(payload, sort_keys=True), before)
        with self.assertRaises(ValueError):
            frozen_trace_details(payload, {**artifact, "artifact_id": "other"}, anchor["id"])
        with self.assertRaises(KeyError):
            frozen_trace_details(payload, artifact, "not-in-report")

    def test_original_table_excerpt_preserves_selected_coordinates_and_headers(self):
        source = {"title": "Frozen table", "quote": "17", "table": {"rows": [
            ["Revenue", "FY2024", "FY2025"], ["Selected", "13", "17"], ["Other", "2", "3"]]},
            "locator": {"header_row_indices": [0], "row_index": 1, "column_index": 2}}
        result = _source_summary(source)
        excerpt = result["table_excerpt"]
        self.assertEqual(excerpt["rows"], [{"row_index": 0, "cells": source["table"]["rows"][0]},
                                         {"row_index": 1, "cells": source["table"]["rows"][1]}])
        self.assertEqual(excerpt["total_rows"], 3)
        self.assertEqual(excerpt["selected_column"], 2)
        self.assertEqual(excerpt["selection"], "selected_headers_and_source_row")

    def test_unsupported_findings_are_kept_only_in_review_appendix(self):
        artifact = self._artifact()
        for status in ("unsupported", "conflicting"):
            with self.subTest(status=status):
                artifact["findings"][0]["semantic_status"] = status
                payload = build_reader_payload(artifact)
                body = [block for chapter in payload["chapters"][:2] for block in chapter["blocks"]]
                self.assertFalse(any(block["type"] == "finding" for block in body))
                self.assertTrue(any(block["id"] == "unsupported-content-notice" for block in body))
                appendix = payload["chapters"][3]["blocks"][-1]
                self.assertEqual(appendix["type"], "review_finding")
                self.assertEqual(appendix["status"], status)
                self.assertTrue(appendix["status_label"])
                self.assertIn("明确绑定的差额", appendix["text"])
                self.assertTrue(appendix["anchor_ids"])
                for anchor_id in appendix["anchor_ids"]:
                    self.assertTrue(build_reader_evidence(artifact, anchor_id)["evidence"])

    def test_excluded_conclusion_does_not_promote_later_management_comment(self):
        artifact = self._artifact()
        artifact["findings"][0]["semantic_status"] = "unsupported"
        artifact["findings"].append({**artifact["findings"][0], "id": "later-comment",
                                     "title": "管理层说明", "text": "这只是后续说明。",
                                     "semantic_status": "partial_support"})
        payload = build_reader_payload(artifact)
        self.assertFalse(any(block["type"] == "finding" for block in payload["chapters"][0]["blocks"]))
        self.assertTrue(any(block["id"] == "later-comment" for block in payload["chapters"][1]["blocks"]))

    def test_correction_index_is_bidirectional_without_rewriting_frozen_payload(self):
        artifact = self._artifact()
        before = load_required_reader_snapshots(
            self.chat, report_id=artifact["reader_report_id"])[0]

        class CorrectingProvider(ReportProvider):
            def stream(self, messages, tools, cancel_event):
                handlers = {item["name"]: item["handler"] for item in tools}
                source = handlers["search_evidence"](query="revenue")["results"][0]
                handlers["record_report_correction"](
                    version=1, original_text="明确绑定的差额为😀",
                    explanation="协议测试：修正旧版表述，不代表真实财务错误。",
                    impact="仅验证纠错导航与冻结正文保护。",
                    evidence_refs=[{"kind": "evidence", "id": source["evidence_id"]}])
                yield from super().stream(messages, tools, cancel_event)

        self.chat.provider = CorrectingProvider()
        initial = self.chat.submit(artifact["conversation_id"], "核查旧版表述")
        run = wait(self.chat, initial["run_id"])
        self.assertEqual(run["status"], "completed", run.get("error"))
        index = list_reader_reports(self.chat, artifact["conversation_id"])
        old, new = index["reports"]
        relation = old["corrected_by"][0]
        self.assertEqual(new["corrections"], [relation])
        self.assertEqual(relation["old_report_id"], old["report_id"])
        self.assertEqual(relation["new_report_id"], new["report_id"])
        self.assertEqual(relation["new_reader_href"], new["reader_href"])
        self.assertFalse(relation["semantic_verified"])
        self.assertEqual(load_required_reader_snapshots(
            self.chat, report_id=artifact["reader_report_id"])[0], before)
        with closing(self.chat._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT payload_sha256 FROM report_reader_versions WHERE report_id=?",
                (old["report_id"],)).fetchone()[0], before["payload_sha256"])

    def test_unpublished_correction_never_becomes_formal_reader_link(self):
        artifact = self._artifact()
        initial, _ = self.chat._persist_submission(artifact["conversation_id"], None, "纠错协议测试")
        self.chat.report_versions.record(
            initial["run_id"], {"evidence": {"protocol-reference": {}}},
            version=1, original_text="明确绑定的差额为😀", explanation="协议测试纠错。",
            impact="验证失败运行不发布。", evidence_refs=[{"kind": "evidence", "id": "protocol-reference"}])
        for status in ("running", "failed"):
            self.chat._finish(initial["run_id"], status, None)
            index = list_reader_reports(self.chat, artifact["conversation_id"])
            self.assertEqual(len(index["reports"]), 1)
            self.assertEqual(index["reports"][0]["corrected_by"], [])

    def test_report_index_lists_completed_reader_reports(self):
        artifact = self._artifact()
        index = list_reader_reports(self.chat, artifact["conversation_id"])

        self.assertEqual(index["schema_version"], "report-index/1")
        self.assertEqual(len(index["reports"]), 1)
        report = index["reports"][0]
        self.assertEqual(report["message_id"], artifact["message_id"])
        self.assertEqual(report["report_id"], build_reader_payload(artifact)["report"]["id"])
        self.assertEqual(report["reader_href"], f'/report-reader.html?report_id={report["report_id"]}')
        self.assertEqual(report["publication_schema"], artifact["publication"]["schema_version"])
        self.assertRegex(report["payload_sha256"], r"^[0-9a-f]{64}$")
        with closing(self.chat._connect()) as connection:
            stored = connection.execute(
                "SELECT count(*) FROM report_reader_versions WHERE report_id=?",
                (report["report_id"],),
            ).fetchone()[0]
        self.assertEqual(stored, 1)
        self.assertEqual(resolve_report(self.chat, report_id=report["report_id"])["message_id"], artifact["message_id"])
        source = build_reader_source(artifact, build_reader_payload(artifact)["sources"][0]["id"])
        self.assertEqual(source["source"]["id"], build_reader_payload(artifact)["sources"][0]["id"])
        self.assertIn("title", source["source"])
        self.assertIn("status", source["source"])
        self.assertIn("raw_download_available", source)

    def test_report_index_skips_stale_completed_answers(self):
        artifact = self._artifact()
        second = self.chat.submit(artifact["conversation_id"], "请更新研究报告")
        self.assertEqual(wait(self.chat, second["run_id"])["status"], "completed")
        stale_message_id = self.chat.get_conversation(artifact["conversation_id"])["messages"][-1]["message_id"]

        original_get = AnswerArtifactService.get

        def get_or_raise(service, conversation_id, message_id):
            if message_id == stale_message_id:
                raise ChatError("source_snapshot_mismatch", "snapshot is stale", 409)
            return original_get(service, conversation_id, message_id)

        with mock.patch.object(AnswerArtifactService, "get", get_or_raise):
            index = list_reader_reports(self.chat, artifact["conversation_id"])

        self.assertEqual([report["message_id"] for report in index["reports"]], [artifact["message_id"]])
        self.assertEqual(index["skipped_reports"], [{
            "message_id": stale_message_id,
            "reason": "source_snapshot_mismatch",
        }])


    def test_reader_payload_is_persisted_append_only(self):
        artifact = self._artifact()
        first = load_required_reader_snapshots(
            self.chat, report_id=artifact["reader_report_id"])[0]
        changed = dict(artifact)
        changed["question"] = "这句不应覆盖已冻结 reader payload"

        self.assertEqual(load_required_reader_snapshots(
            self.chat, report_id=artifact["reader_report_id"])[0], first)
        self.assertNotEqual(first["question"]["original"], changed["question"])
        with closing(self.chat._connect()) as connection:
            row = connection.execute(
                "SELECT payload_json,payload_sha256 FROM report_reader_versions WHERE report_id=?",
                (first["report"]["id"],),
            ).fetchone()
            self.assertEqual(json.loads(row["payload_json"]), first)
            self.assertEqual(row["payload_sha256"], first["payload_sha256"])
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE report_reader_versions SET title='overwrite' WHERE report_id=?",
                    (first["report"]["id"],),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM report_reader_versions WHERE report_id=?",
                    (first["report"]["id"],),
                )

    def test_source_summary_preserves_cross_subject_identity_without_guessing(self):
        source = {"company_id": None, "company_ids": ["CRM", "IBM"]}
        summary = _source_summary(source)
        self.assertIsNone(summary["company_id"])
        self.assertEqual(summary["company_ids"], ["CRM", "IBM"])
        summary["company_ids"].append("OTHER")
        self.assertEqual(source["company_ids"], ["CRM", "IBM"])
        self.assertNotIn("company_ids", _source_summary({"company_id": "IBM"}))

    def test_schema_creation_preserves_the_callers_transaction(self):
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("BEGIN IMMEDIATE")
            _ensure_reader_schema(connection)
            self.assertTrue(connection.in_transaction)
            self.assertIsNotNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name='report_artifact_snapshots'").fetchone())
            connection.rollback()
            self.assertIsNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE name='report_artifact_snapshots'").fetchone())

    def test_reader_and_complete_artifact_snapshot_remain_bound_and_immutable(self):
        artifact = self._artifact()
        report_id = artifact["reader_report_id"]
        payload, saved = load_required_reader_snapshots(self.chat, report_id=report_id)
        self.assertEqual(saved, artifact)
        with closing(self.chat._connect()) as connection:
            row = connection.execute(
                "SELECT artifact_sha256 FROM report_artifact_snapshots WHERE report_id=?", (report_id,)
            ).fetchone()
            self.assertEqual(len(row["artifact_sha256"]), 64)
            self.assertEqual(_insert_artifact_snapshot(
                connection, artifact, registered_at="ignored-on-idempotent-repeat"), artifact)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE report_artifact_snapshots SET registered_at='changed' WHERE report_id=?",
                    (report_id,))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM report_artifact_snapshots WHERE report_id=?", (report_id,))
        changed = deepcopy(artifact)
        changed["question"] = "new projection must not replace the frozen artifact"
        candidate = deepcopy(changed)
        candidate.pop("artifact_id")
        candidate.pop("reader_report_id")
        changed["artifact_id"] = _stable_id("artifact", candidate)
        with closing(self.chat._connect()) as connection:
            with self.assertRaises(ValueError):
                _insert_artifact_snapshot(connection, changed, registered_at="conflict")
        with mock.patch.object(AnswerArtifactService, "_get", side_effect=AssertionError("reprojected")):
            self.assertEqual(AnswerArtifactService(self.chat).get(
                artifact["conversation_id"], artifact["message_id"]), artifact)
        self.assertEqual(load_required_reader_snapshots(self.chat, report_id=report_id)[0], payload)

    def test_saved_artifact_snapshot_does_not_bypass_authoritative_event_tampering(self):
        artifact = self._artifact()
        load_required_reader_snapshots(
            self.chat, report_id=artifact["reader_report_id"])
        cited_id = next(item["id"] for item in self.chat.answer_context(
            artifact["conversation_id"], artifact["message_id"])["citations"] if item.get("valid") is True)
        with closing(self.chat._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT run_id,seq,payload_json FROM events WHERE run_id=? AND type='tool_result' ORDER BY seq",
                (artifact["run_id"],),
            ).fetchall()
            row = next(item for item in rows if cited_id in item["payload_json"])
            connection.execute(
                "UPDATE events SET payload_json=? WHERE run_id=? AND seq=?",
                (json.dumps({"truncated": True, "message": "tampered after freeze"}), row["run_id"], row["seq"]),
            )
        with self.assertRaises(ChatError) as caught:
            AnswerArtifactService(self.chat).get(artifact["conversation_id"], artifact["message_id"])
        self.assertEqual(caught.exception.code, "citation_source_mismatch")
        # An explicit audit detects the historical-record fault, while the immutable
        # publication-time closure remains readable for correction/version history.
        self.assertEqual(load_required_reader_snapshots(
            self.chat, report_id=artifact["reader_report_id"])[1], artifact)


    def test_reader_payload_keeps_unsupported_content_out_of_primary_conclusion(self):
        artifact = self._artifact()
        risky = {
            "id": "manual-risk",
            "text": "这句缺少依据，不能作为核心结论。",
            "classification": "interpretation",
            "status": "needs_review",
            "semantic_status": "unsupported",
            "references": [],
            "trace_ids": [],
            "review_flags": [{"message": "无支持依据"}],
        }
        artifact["findings"].append(risky)
        payload = build_reader_payload(artifact)

        primary_texts = [block.get("text") for block in payload["chapters"][0]["blocks"]]
        self.assertNotIn(risky["text"], primary_texts)
        analysis_blocks = payload["chapters"][1]["blocks"]
        self.assertFalse(any(block["id"] == "manual-risk" for block in analysis_blocks))
        risky_block = next(block for block in payload["chapters"][3]["blocks"] if block["id"] == "manual-risk")
        self.assertEqual(risky_block["type"], "review_finding")
        self.assertEqual(risky_block["status"], "unsupported")
        self.assertEqual(risky_block["review_flags"][0]["message"], "无支持依据")

    def test_frozen_chinese_dataset_text_preserves_canonical_value_and_old_fallback(self):
        from workbench.report_reader import _dataset_blocks, _dataset_row_anchors
        datasets = [{"id": "test-display", "title": "合成格式测试", "rows": [
            {"id": "r", "label": "收入", "value": "77673.00", "unit": "USD millions",
             "display_text": "77,673.00 百万美元", "reference": {"kind": "evidence", "id": "e"}}]}]
        before = json.dumps(datasets, ensure_ascii=False)
        row = _dataset_blocks(datasets)[0]["rows"][0]
        self.assertEqual((row["value"], row["unit"], row["display_text"]),
                         ("77673.00", "USD millions", "77,673.00 百万美元"))
        self.assertEqual(_dataset_row_anchors(datasets)[0]["text"], "收入：77,673.00 百万美元")
        self.assertEqual(json.dumps(datasets, ensure_ascii=False), before)
        del datasets[0]["rows"][0]["display_text"]
        self.assertNotIn("display_text", _dataset_blocks(datasets)[0]["rows"][0])
        self.assertEqual(_dataset_row_anchors(datasets)[0]["text"], "收入：77673.00 USD millions")

    def test_dataset_rows_become_clickable_table_cell_anchors(self):
        artifact = self._artifact()
        numeric_anchor = next(anchor for anchor in artifact["anchors"] if anchor["kind"] == "number")
        artifact["datasets"] = [{
            "id": "dataset-1",
            "title": "关键数据表",
            "description": "用于正文核查的表格",
            "columns": [{"key": "label", "label": "项目"}, {"key": "value", "label": "数值"}],
            "rows": [{
                "id": "row-1",
                "label": "累计变化",
                "value": "20252",
                "unit": "USD millions",
                "basis": "reported",
                "period": {"end": "2025-06-30"},
                "reference": numeric_anchor["references"][0],
                "trace_id": numeric_anchor["trace_ids"][0],
            }],
            "chart": None,
            "limitations": [],
        }]

        payload = build_reader_payload(artifact)
        dataset_block = next(block for block in payload["chapters"][1]["blocks"] if block["id"] == "dataset-1")
        self.assertEqual(dataset_block["rows"][0]["anchor_id"], "row-1-cell-value")
        table_anchor = next(anchor for anchor in payload["anchors"] if anchor["id"] == "row-1-cell-value")
        self.assertEqual(table_anchor["kind"], "table_cell")
        self.assertEqual(table_anchor["support_status"], "calculated")
        self.assertIn("累计变化", payload["evidence"][table_anchor["evidence_id"]]["claim"])

    def test_matrix_dataset_is_placed_after_exact_body_block_and_reuses_point_anchors(self):
        artifact = self._artifact()
        target = artifact["findings"][0]["id"]
        numeric_anchor = next(anchor for anchor in artifact["anchors"] if anchor["kind"] == "number")
        reference = numeric_anchor["references"][0]
        trace_id = numeric_anchor["trace_ids"][0]
        artifact["datasets"] = [{
            "id": "matrix-1", "title": "期间比较", "description": "显式绑定的工程测试矩阵",
            "columns": [{"key": "label", "label": "项目"}, {"key": "value", "label": "数值"}],
            "rows": [
                {"id": "point-1", "label": "任意标签一", "value": "20252", "unit": "USD millions",
                 "reference": reference, "trace_id": trace_id},
                {"id": "point-2", "label": "任意标签二", "value": "20252", "unit": "USD millions",
                 "reference": reference, "trace_id": trace_id},
            ],
            "chart": None, "limitations": [],
            "presentation": {
                "type": "matrix", "after_block_id": target,
                "columns": [{"key": "p1", "label": "期间一"}, {"key": "p2", "label": "期间二"}],
                "rows": [{"key": "metric", "label": "指标", "cells": [
                    {"point_id": "point-1", "anchor_id": "point-1-cell-value"},
                    {"point_id": "point-2", "anchor_id": "point-2-cell-value"},
                ]}],
            },
        }]

        payload = build_reader_payload(artifact)
        conclusion_ids = [block["id"] for block in payload["chapters"][0]["blocks"]]
        self.assertEqual(conclusion_ids[conclusion_ids.index(target) + 1], "matrix-1")
        matrix = next(block for block in payload["chapters"][0]["blocks"] if block["id"] == "matrix-1")
        self.assertEqual(matrix["presentation"]["rows"][0]["cells"][0]["anchor_id"],
                         matrix["rows"][0]["anchor_id"])
        self.assertEqual({anchor["id"] for anchor in payload["anchors"] if anchor["kind"] == "table_cell"},
                         {"point-1-cell-value", "point-2-cell-value"})

    def test_matrix_targeting_excluded_finding_stays_with_review_appendix(self):
        artifact = self._artifact()
        target = artifact["findings"][0]["id"]
        numeric_anchor = next(anchor for anchor in artifact["anchors"] if anchor["kind"] == "number")
        artifact["datasets"] = [{
            "id": "matrix-excluded", "title": "待核查期间比较", "description": "不能提升为分析结论",
            "columns": [{"key": "label", "label": "项目"}, {"key": "value", "label": "数值"}],
            "rows": [{
                "id": "excluded-point", "label": "待核查值", "value": "20252", "unit": "USD millions",
                "reference": numeric_anchor["references"][0], "trace_id": numeric_anchor["trace_ids"][0],
            }],
            "chart": None, "limitations": [],
            "presentation": {
                "type": "matrix", "after_block_id": target,
                "columns": [{"key": "period", "label": "期间"}],
                "rows": [{"key": "metric", "label": "指标", "cells": [
                    {"point_id": "excluded-point", "anchor_id": "excluded-point-cell-value"},
                ]}],
            },
        }]

        for status in ("unsupported", "conflicting"):
            with self.subTest(status=status):
                artifact["findings"][0]["semantic_status"] = status
                payload = build_reader_payload(artifact)
                body_ids = {block["id"] for chapter in payload["chapters"][:2] for block in chapter["blocks"]}
                self.assertNotIn(target, body_ids)
                self.assertNotIn("matrix-excluded", body_ids)
                appendix = payload["chapters"][3]["blocks"]
                target_index = next(index for index, block in enumerate(appendix) if block["id"] == target)
                self.assertEqual(appendix[target_index]["type"], "review_finding")
                self.assertEqual(appendix[target_index]["status"], status)
                self.assertEqual(appendix[target_index + 1]["id"], "matrix-excluded")
                self.assertEqual(appendix[target_index + 1]["presentation"]["after_block_id"], target)

    def test_dataset_own_negative_review_is_quarantined_once_and_keeps_cell_evidence(self):
        base = self._artifact()
        numeric_anchor = next(anchor for anchor in base["anchors"] if anchor["kind"] == "number")
        base["datasets"] = [{
            "id": "reviewed-dataset", "title": "自身受审核的数据表", "description": "受控说明",
            "columns": [{"key": "label", "label": "项目"}, {"key": "value", "label": "数值"}],
            "rows": [{
                "id": "reviewed-point", "label": "受控值", "value": "20252", "unit": "USD millions",
                "reference": numeric_anchor["references"][0], "trace_id": numeric_anchor["trace_ids"][0],
                "semantic_review": {"status": "unsupported", "support_status": "unsupported",
                                    "reason": "单元格受控负面原因", "claims": []},
            }],
            "chart": None, "limitations": [],
        }]
        cases = [
            ("supported", [], False),
            ("not_assessed", [], False),
            ("unsupported", [], True),
            ("conflicting", [], True),
            ("mixed", [{"support_status": "unsupported", "reason": "说明无依据"}], True),
        ]
        for status, claims, quarantined in cases:
            with self.subTest(status=status):
                artifact = deepcopy(base)
                artifact["datasets"][0]["semantic_review"] = {
                    "status": status, "semantic_verified": False, "claims": claims,
                }
                payload = build_reader_payload(artifact)
                body = [block for chapter in payload["chapters"][:2] for block in chapter["blocks"]]
                appendix = payload["chapters"][3]["blocks"]
                body_matches = [block for block in body if block["id"] == "reviewed-dataset"]
                appendix_matches = [block for block in appendix if block["id"] == "reviewed-dataset"]
                self.assertEqual(len(body_matches), 0 if quarantined else 1)
                self.assertEqual(len(appendix_matches), 1 if quarantined else 0)
                self.assertEqual(len(body_matches) + len(appendix_matches), 1)
                self.assertEqual((body_matches or appendix_matches)[0]["status"], status)
                notices = [block for block in body if block["id"] == "unsupported-content-notice"]
                self.assertEqual(bool(notices), quarantined)
                row_anchor = next(anchor for anchor in payload["anchors"]
                                  if anchor["id"] == "reviewed-point-cell-value")
                self.assertEqual(row_anchor["semantic_review"]["reason"], "单元格受控负面原因")
                self.assertTrue(payload["evidence"][row_anchor["evidence_id"]]["references"])

    def test_line_chart_dataset_reuses_row_anchors(self):
        artifact = self._artifact()
        numeric_anchor = next(anchor for anchor in artifact["anchors"] if anchor["kind"] == "number")
        artifact["datasets"] = [{
            "id": "trend-1",
            "title": "趋势数据",
            "description": "用于趋势图的已记录点位",
            "columns": [],
            "rows": [
                {"id": "trend-row-1", "label": "FY2024", "value": "100", "unit": "USD millions",
                 "reference": numeric_anchor["references"][0], "trace_id": numeric_anchor["trace_ids"][0]},
                {"id": "trend-row-2", "label": "FY2025", "value": "120", "unit": "USD millions",
                 "reference": numeric_anchor["references"][0], "trace_id": numeric_anchor["trace_ids"][0]},
            ],
            "chart": {"type": "line", "x": "label", "y": "value"},
            "limitations": [],
        }]

        payload = build_reader_payload(artifact)
        dataset_block = next(block for block in payload["chapters"][1]["blocks"] if block["id"] == "trend-1")
        self.assertEqual(dataset_block["chart"]["type"], "line")
        self.assertEqual([row["anchor_id"] for row in dataset_block["rows"]],
                         ["trend-row-1-cell-value", "trend-row-2-cell-value"])
        self.assertTrue(all(anchor["kind"] == "table_cell" for anchor in payload["anchors"]
                            if anchor["id"].startswith("trend-row-")))


class ReportReaderApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "evidence.sqlite3"
        build_database(db_path=self.db)
        self.provider = ControlledAuthorVerifier()
        self.server = EvidenceHTTPServer(
            0, self.db, chat_db=Path(self.temp.name) / "chat.sqlite3",
            provider=self.provider,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.worker = DurableWorker(
            self.server.chat, worker_id="reader-http-test", heartbeat_seconds=.1)
        self.addCleanup(self._close_server)

    def _publish(self):
        initial = self.server.chat.create_and_submit("MSFT", "微软收入相对以前的累计变化")
        self.assertTrue(self.worker.run_once())
        self.assertTrue(self.worker.run_once())
        run = self.server.chat.get_run(initial["run_id"])
        self.assertEqual(run["status"], "completed", run.get("error"))
        message = self.server.chat.get_conversation(initial["conversation_id"])["messages"][-1]
        return initial, message

    def _close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _snapshot_counts(self):
        with closing(self.server.chat._connect()) as connection:
            return tuple(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                         for table in ("report_reader_versions", "report_artifact_snapshots"))

    def _get(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_empty_report_list_does_not_create_reader_schema(self):
        conversation = self.server.chat.create_conversation("MSFT")
        with closing(self.server.chat._connect()) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='report_reader_versions'").fetchone())
        status, index = self._get(
            f'/api/reports?conversation_id={conversation["conversation_id"]}')
        self.assertEqual(status, 200)
        self.assertEqual(index["reports"], [])
        self.assertEqual(index["skipped_reports"], [])
        with closing(self.server.chat._connect()) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='report_reader_versions'").fetchone())

    def test_reader_endpoint_reads_publication_time_snapshots(self):
        initial, message = self._publish()
        frozen_counts = self._snapshot_counts()

        status, payload = self._get(
            f'/api/reports/reader?conversation_id={initial["conversation_id"]}&message_id={message["message_id"]}'
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["schema_version"], SCHEMA_VERSION)
        self.assertEqual(payload["report"]["message_id"], message["message_id"])
        self.assertEqual([chapter["title"] for chapter in payload["chapters"]],
                         [title for _identifier, title in CHAPTERS])
        self.assertTrue(payload["anchors"])
        self.assertTrue(payload["evidence"])

        status, index = self._get(f'/api/reports?conversation_id={initial["conversation_id"]}')
        self.assertEqual(status, 200)
        self.assertEqual(index["reports"][0]["message_id"], message["message_id"])
        report_id = index["reports"][0]["report_id"]
        self.assertEqual(index["reports"][0]["reader_href"],
                         f'/report-reader.html?report_id={report_id}')

        status, by_report_id = self._get(f'/api/reports/reader?report_id={report_id}')
        self.assertEqual(status, 200)
        self.assertEqual(by_report_id["report"]["id"], report_id)
        self.assertEqual(by_report_id["report"]["message_id"], message["message_id"])

        anchor = payload["anchors"][0]
        status, evidence = self._get(
            f'/api/reports/evidence?conversation_id={initial["conversation_id"]}'
            f'&message_id={message["message_id"]}&anchor_id={anchor["id"]}'
        )
        self.assertEqual(status, 200)
        self.assertEqual(evidence["anchor"]["id"], anchor["id"])
        self.assertEqual(evidence["evidence"]["anchor_id"], anchor["id"])

        status, report_evidence = self._get(
            f'/api/reports/evidence?report_id={report_id}&anchor_id={anchor["id"]}'
        )
        self.assertEqual(status, 200)
        self.assertEqual(report_evidence["report"]["id"], report_id)
        self.assertEqual(report_evidence["anchor"]["id"], anchor["id"])

        source_id = by_report_id["sources"][0]["id"]
        status, source = self._get(f'/api/reports/source?report_id={report_id}&source_id={source_id}')
        self.assertEqual(status, 200)
        self.assertEqual(source["source"]["id"], source_id)
        status, missing_source = self._get(f'/api/reports/source?report_id={report_id}&source_id=missing')
        self.assertEqual(status, 404)
        self.assertEqual(missing_source["error"], "report_source_not_found")

        status, missing = self._get(
            f'/api/reports/evidence?conversation_id={initial["conversation_id"]}'
            f'&message_id={message["message_id"]}&anchor_id=missing'
        )
        self.assertEqual(status, 404)
        self.assertEqual(missing["error"], "report_anchor_not_found")
        self.assertEqual(self._snapshot_counts(), frozen_counts)

    def test_reader_and_evidence_do_not_replay_all_sources_at_view_time(self):
        initial, message = self._publish()
        report_id = report_id_for(initial["conversation_id"], message["message_id"])
        payload = load_required_reader_snapshots(self.server.chat, report_id=report_id)[0]
        anchor_id = payload["anchors"][0]["id"]

        with mock.patch.object(
                AnswerArtifactService, "get",
                side_effect=AssertionError("view-time artifact replay")), mock.patch.object(
                self.server.chat, "read_citation",
                side_effect=AssertionError("view-time citation replay")):
            status, reader = self._get(f"/api/reports/reader?report_id={report_id}")
            self.assertEqual(status, 200)
            self.assertEqual(reader["payload_sha256"], payload["payload_sha256"])
            status, evidence = self._get(
                f"/api/reports/evidence?report_id={report_id}&anchor_id={anchor_id}")
            self.assertEqual(status, 200)
            self.assertEqual(evidence["anchor"]["id"], anchor_id)
            status, index = self._get(
                f"/api/reports?conversation_id={initial['conversation_id']}")
            self.assertEqual(status, 200)
            self.assertEqual(index["reports"][0]["report_id"], report_id)

    def test_reader_does_not_depend_on_display_snapshot_events(self):
        initial, message = self._publish()
        path = (f'/api/reports/reader?conversation_id={initial["conversation_id"]}'
                f'&message_id={message["message_id"]}')
        status, before = self._get(path)
        self.assertEqual(status, 200)
        with closing(self.server.chat._connect()) as connection, connection:
            connection.execute(
                "DELETE FROM events WHERE run_id=? AND type IN ('data_snapshot','data_snapshot_addition','tool_result')",
                (initial["run_id"],))
        status, after = self._get(path)
        self.assertEqual(status, 200, after)
        self.assertEqual(after, before)

    def test_report_gets_reject_missing_snapshots_without_rebuilding(self):
        initial, message = self._publish()
        message_id = message["message_id"]
        status, payload = self._get(
            f'/api/reports/reader?conversation_id={initial["conversation_id"]}&message_id={message_id}')
        self.assertEqual(status, 200)
        report_id = payload["report"]["id"]
        anchor = payload["anchors"][0]
        source_id = "missing"
        with closing(self.server.chat._connect()) as connection, connection:
            connection.execute("DROP TRIGGER report_artifact_snapshots_no_delete")
            connection.execute("DELETE FROM report_artifact_snapshots WHERE report_id=?", (report_id,))

        for path in (
            f'/api/reports/reader?report_id={report_id}',
            f'/api/reports/evidence?report_id={report_id}&anchor_id={anchor["id"]}',
            f'/api/reports/source?report_id={report_id}&source_id={source_id}',
            f'/api/reports/raw-source?report_id={report_id}&source_id={source_id}',
            f'/api/research/artifact?conversation_id={initial["conversation_id"]}&message_id={message_id}',
        ):
            status, result = self._get(path)
            self.assertEqual((status, result["error"]),
                             (409, "report_artifact_snapshot_unavailable"), path)
        with closing(self.server.chat._connect()) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM report_artifact_snapshots WHERE report_id=?", (report_id,)).fetchone())
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM report_reader_versions WHERE report_id=?", (report_id,)).fetchone())
        status, index = self._get(f'/api/reports?conversation_id={initial["conversation_id"]}')
        self.assertEqual(status, 200)
        self.assertEqual(index["reports"], [])
        self.assertEqual(index["skipped_reports"], [{
            "message_id": message_id, "reason": "report_artifact_snapshot_unavailable"}])

        with closing(self.server.chat._connect()) as connection, connection:
            connection.execute("DROP TRIGGER report_reader_versions_no_delete")
            connection.execute("DELETE FROM report_reader_versions WHERE report_id=?", (report_id,))
        status, result = self._get(f'/api/reports/reader?report_id={report_id}')
        self.assertEqual((status, result["error"]), (409, "report_reader_snapshot_unavailable"))
        with closing(self.server.chat._connect()) as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM report_reader_versions WHERE report_id=?", (report_id,)).fetchone())



if __name__ == "__main__":
    unittest.main()
