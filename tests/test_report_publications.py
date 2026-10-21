from contextlib import closing
from copy import deepcopy
from decimal import localcontext
import hashlib
import json
from types import SimpleNamespace
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, Mock

from tests.test_recorded_calculations import CalculationProvider, wait
from workbench.answer_artifacts import AnswerArtifactService, _dataset_semantic_claim_ids
from workbench.build import build_database
from workbench.chat import ChatError, ChatService
from workbench.evidence_checks import EvidenceRecordRead
from workbench.report_publications import (
    VERSION, prepare, project, project_datasets, validate_bindings, _review_claims_within, _value_text,
)
from workbench.research_tools import ToolError


def block(parts, *, support=None, classification="recorded_calculation"):
    return {"classification": classification, "parts": parts, "support": support or [],
            "reasoning": "比较已选择的冻结输入，不作因果推断。",
            "limitations": ["计算准确不等于经济解释正确。"]}


def numeric(kind, identifier):
    return {"kind": kind, "id": identifier,
            "value_formation": "derived" if kind == "calculation" else "observed",
            "attribution": None}


class ReportProvider(CalculationProvider):
    requires_structured_reports = True

    def stream(self, messages, tools, cancel_event):
        yield from super().stream(messages, tools, cancel_event)
        publish = next(item["handler"] for item in tools if item["name"] == "publish_research_report")
        self.blocks = [block([{"kind": "text", "text": "明确绑定的差额为😀 "},
                             numeric("calculation", self.calculation_id)])]
        publish(title="完整研究报告", blocks=self.blocks, value_style=getattr(self, "value_style", "recorded"))
        yield {"type": "delta", "text": "\nThis extra streamed prose is not the published report."}


class PublicationTests(unittest.TestCase):
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

    def publish(self):
        initial = self.chat.create_and_submit("MSFT", "微软收入相对以前的累计变化")
        run = wait(self.chat, initial["run_id"])
        self.assertEqual(run["status"], "completed", run.get("error"))
        message = self.chat.get_conversation(initial["conversation_id"])["messages"][-1]
        answer = self.chat.answer_context(initial["conversation_id"], message["message_id"])
        return initial, answer

    def test_chinese_display_is_exact_bounded_and_unknown_units_are_not_guessed(self):
        cases = [("77673", "USD millions", "77,673 百万美元"),
                 ("-17079.0000", "USD million", "-17,079.0000 百万美元"),
                 ("5.94", "percentage points", "5.94 个百分点"),
                 ("22.40", "%", "22.40%"), ("1.25", "percent", "1.25%"),
                 ("-0.00", "USD", "-0.00 美元"), ("1E+3", "USD billions", "1,000 十亿美元"),
                 ("1.00000000000000000001", "USD/share", "1.00000000000000000001 美元/股"),
                 ("0.500", "x", "0.500 倍"), ("1234.50", "unknown unit", "1,234.50 unknown unit")]
        with localcontext() as context:
            context.prec = 2
            for value, unit, shown in cases:
                with self.subTest(value=value, unit=unit):
                    self.assertEqual(_value_text(value, unit, "zh-CN"), shown)
                    self.assertEqual(_value_text(value, unit), f"{value} {unit}")
        for value in ("NaN", "Infinity", "-Infinity", "1E+4000", "1E-4001", "not a number"):
            with self.subTest(value=value), self.assertRaises(ToolError):
                _value_text(value, "USD", "zh-CN")

    def test_chinese_datasets_validate_their_own_frozen_bindings(self):
        facts = {"f": {"value": "77673.00", "unit": "USD millions", "evidence_id": "e",
                        "basis": "GAAP", "period_kind": "quarter"}}
        args = dict(title="测试格式而非真实研究", blocks=[block([numeric("fact", "f")])],
                    facts=facts, calculations={}, refs={"evidence": {"e": {}}}, max_chars=10000,
                    datasets=[{"title": "金额", "description": "合成单元测试数据", "display": "table",
                               "points": [{"label": "收入", **numeric("fact", "f")}]}])
        original = deepcopy(args)
        recorded = prepare(**args)
        self.assertEqual(recorded, prepare(**args, value_style="recorded"))
        self.assertNotIn("value_style", recorded)
        self.assertNotIn("display_text", project_datasets(recorded, {})[0]["rows"][0])
        localized = prepare(**args, value_style="zh-CN")
        self.assertEqual(args, original)
        self.assertIn("77,673.00 百万美元", localized["answer"])
        row = project_datasets(localized, {})[0]["rows"][0]
        self.assertEqual((row["value"], row["unit"], row["display_text"], row["display_unit"]),
                         ("77673.00", "USD millions", "77,673.00 百万美元", "百万美元"))
        reference_read = EvidenceRecordRead(
            "available", "evidence", "e", record_type="structured_fact",
            _record_bytes=json.dumps(facts["f"]).encode("utf-8"),
        )
        reviewer = SimpleNamespace(
            indexes={"facts": facts, "calculations": {}},
            read_reference_record=Mock(return_value=reference_read),
        )
        validate_bindings(localized, reviewer)
        reviewer.read_reference_record.return_value = EvidenceRecordRead(
            "incomplete", "evidence", "e", reason="matching record is incomplete")
        with self.assertRaises(ToolError) as raised:
            validate_bindings(localized, reviewer)
        self.assertEqual(raised.exception.code, "invalid_report_reference")
        reviewer.read_reference_record.return_value = reference_read
        for field, changed in (("value", "999"), ("unit", "USD billions"), ("text", "999 百万美元"),
                               ("display_unit", "十亿美元"), ("reference", {"kind": "evidence", "id": "other"}),
                               ("dimensions", {})):
            for location in ("dataset", "span"):
                corrupt = deepcopy(localized)
                binding = (corrupt["datasets"][0]["points"][0]["binding"] if location == "dataset"
                           else corrupt["blocks"][0]["spans"][0]["binding"])
                binding[field] = changed
                with self.subTest(field=field, location=location), self.assertRaises(ToolError):
                    validate_bindings(corrupt, reviewer)
        for style in (None, {}, [], True, "auto", "en-US"):
            with self.subTest(style=style), self.assertRaises(ToolError):
                prepare(**args, value_style=style)

    def test_cross_period_dataset_keeps_exact_operand_ranges_without_inventing_one_period(self):
        periods = [["2024-01-29", "2025-01-26"], ["2023-01-30", "2024-01-28"]]
        calculations = {"c": {"value": "69575", "unit": "USD million", "arithmetic": {
            "output_context": {"periods": periods, "period_start": None, "period_end": None,
                               "period_kind": "year", "basis": "GAAP"}}}}
        prepared = prepare(title="合成期间测试", blocks=[block([numeric("calculation", "c")])],
            refs={"calculation": {"c": {}}}, facts={}, calculations=calculations, max_chars=10000,
            datasets=[{"title": "年度增量", "description": "两个实际财年之差", "display": "table",
                       "points": [{"label": "NVIDIA", **numeric("calculation", "c")}]}])
        period = project_datasets(prepared, {})[0]["rows"][0]["period"]
        self.assertEqual(period, {"start": None, "end": None, "kind": "year", "ranges": periods})
        periods[0][0] = "changed after preparation"
        self.assertEqual(project_datasets(prepared, {})[0]["rows"][0]["period"]["ranges"][0][0], "2024-01-29")

    def test_new_chinese_publication_preserves_old_publication_and_exact_anchor_spans(self):
        initial, old_answer = self.publish()
        old = self.chat.publications.read(old_answer)
        self.provider.value_style = "zh-CN"
        submitted = self.chat.submit(initial["conversation_id"], "仅生成新的中文显示版本，不改旧版")
        self.assertEqual(wait(self.chat, submitted["run_id"])["status"], "completed")
        message = self.chat.get_conversation(initial["conversation_id"])["messages"][-1]
        artifact = AnswerArtifactService(self.chat).get(initial["conversation_id"], message["message_id"])
        numeric = next(anchor for anchor in artifact["anchors"] if anchor["kind"] == "number")
        self.assertEqual(numeric["text"], "20,252 百万美元")
        self.assertEqual(artifact["answer"][numeric["start"]:numeric["end"]], numeric["text"])
        self.assertGreater(numeric["utf16_start"], numeric["start"])
        self.assertEqual(self.chat.publications.read(old_answer), old)

    def test_descriptive_titles_are_optional_explicit_and_do_not_shift_saved_spans(self):
        narrative = block([{"kind": "text", "text": "资料不足，不能作出增长归因。"}], classification="evidence_gap")
        args = dict(title="研究结论", refs={}, facts={}, calculations={}, max_chars=10000)
        legacy = prepare(blocks=[narrative], **args)
        titled = prepare(blocks=[{**narrative, "title": "增长归因的证据缺口"}], **args)
        self.assertEqual(legacy["answer"], titled["answer"])
        self.assertEqual(legacy["blocks"][0]["spans"], titled["blocks"][0]["spans"])
        findings, _ = project(titled, {}, {})
        self.assertEqual(findings[0]["title"], "增长归因的证据缺口")
        self.assertNotIn("title", project(legacy, {}, {})[0][0])
        for invalid in ("", "x" * 121, "标题\n换行", None):
            with self.subTest(title=invalid), self.assertRaises(ToolError):
                prepare(blocks=[{**narrative, "title": invalid}], **args)

    def test_report_is_explicit_persisted_and_independent_of_legacy_text_inference(self):
        initial, answer = self.publish()
        self.assertEqual(answer["publication_schema"], VERSION)
        self.assertNotIn("extra streamed", answer["answer"])
        self.assertIn("20252 USD millions", answer["answer"])
        self.assertIn("推理说明", answer["answer"])
        with patch.object(AnswerArtifactService, "_findings", side_effect=AssertionError("legacy guessing")), \
             patch.object(AnswerArtifactService, "_atomic_anchors", side_effect=AssertionError("legacy anchor guessing")), \
             patch.object(AnswerArtifactService, "_presentation", side_effect=AssertionError("keyword presentation")):
            artifact = AnswerArtifactService(self.chat).get(initial["conversation_id"], answer["message_id"])
        self.assertTrue(artifact["publication"]["immutable"])
        numeric = next(item for item in artifact["anchors"] if item["kind"] == "number")
        self.assertEqual(numeric["text"], "20252 USD millions")
        self.assertEqual(numeric["calculation_id"], self.provider.calculation_id)
        self.assertEqual(numeric["support_status"], "calculated")
        statements = [item for item in artifact["anchors"] if item["kind"] == "assertion"]
        self.assertEqual(len(statements), len(artifact["findings"]))
        self.assertEqual(statements[0]["text"], artifact["findings"][0]["text"])
        self.assertGreater(numeric["utf16_start"], numeric["start"])
        self.assertEqual(artifact["findings"][0]["reasoning"], self.provider.blocks[0]["reasoning"])
        self.assertIsNone(artifact["presentation"]["recommended_dataset_id"])
        before = self.chat.publications.read(answer)
        self.chat.close()
        reopened = ChatService(self.db, self.chat.chat_db, self.provider)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.publications.read(answer), before)

    def test_saved_contained_claims_attach_to_whole_assertion_without_promoting_it(self):
        initial, answer = self.publish()
        publication = self.chat.publications.read(answer)
        block = publication["blocks"][0]

        def claim(identifier, exact, status, reason):
            start = publication["answer"].index(exact, block["start"], block["end"])
            return {"id": identifier, "start": start, "end": start + len(exact), "exact_text": exact,
                    "support_status": status, "evidence_refs": [], "reason": reason}

        valid = [
            claim("supported", "明确绑定", "supported_by_evidence", "受控支持理由"),
            claim("inference", "😀", "inference", "受控推断理由"),
            claim("negative", "差额", "unsupported", "受控不支持理由"),
        ]
        invalid = [
            {**valid[0], "id": "wrong-text", "exact_text": "错误原文"},
            {**valid[0], "id": "bool-boundary", "start": True},
            {**valid[0], "id": "crossing", "start": block["start"] - 1,
             "exact_text": publication["answer"][block["start"] - 1:valid[0]["end"]]},
            {**valid[0], "id": "out-of-bounds", "end": len(publication["answer"]) + 1},
        ]
        publication["semantic_review"] = {"status": "assessed", "semantic_verified": False,
                                          "claims": valid + invalid}
        with patch.object(self.chat.publications, "read", return_value=publication):
            artifact = AnswerArtifactService(self.chat).get(initial["conversation_id"], answer["message_id"])

        anchor = next(item for item in artifact["anchors"] if item["text"] == block["text"])
        review = anchor["semantic_review"]
        self.assertEqual(review["scope"], "contained_claims")
        self.assertEqual(review["review_scope"], "partial")
        self.assertEqual([item["id"] for item in review["claims"]],
                         ["supported", "inference", "negative"])
        self.assertEqual(review["status"], "mixed")
        self.assertFalse(review["semantic_verified"])
        self.assertEqual(anchor["support_status"], "unresolved")
        self.assertFalse(artifact["verification"]["semantic_verified"])
        self.assertTrue(any("未与公开锚点" in item for item in artifact["limitations"]))
        self.assertEqual(_review_claims_within(publication["answer"], block["start"], block["end"],
                                               valid + invalid), valid)

        all_supported = [{**item, "support_status": "supported_by_evidence"} for item in valid]
        publication["semantic_review"] = {"status": "assessed", "semantic_verified": False,
                                          "claims": all_supported}
        with patch.object(self.chat.publications, "read", return_value=publication):
            supported_artifact = AnswerArtifactService(self.chat).get(
                initial["conversation_id"], answer["message_id"])
        supported_anchor = next(item for item in supported_artifact["anchors"] if item["text"] == block["text"])
        self.assertEqual(supported_anchor["semantic_review"]["status"], "supported")
        self.assertEqual(supported_anchor["support_status"], "unresolved")
        self.assertFalse(supported_anchor["semantic_review"]["semantic_verified"])
        self.assertEqual(supported_anchor["semantic_review"]["support_status"], "partial_support")

        publication["semantic_review"]["claims"] = all_supported[:1]
        with patch.object(self.chat.publications, "read", return_value=publication):
            one_claim_artifact = AnswerArtifactService(self.chat).get(
                initial["conversation_id"], answer["message_id"])
        one_claim_anchor = next(item for item in one_claim_artifact["anchors"] if item["text"] == block["text"])
        self.assertEqual(one_claim_anchor["semantic_review"]["scope"], "contained_claims")
        self.assertEqual(one_claim_anchor["semantic_review"]["support_status"], "partial_support")
        self.assertEqual(one_claim_anchor["support_status"], "unresolved")

    def test_dataset_claim_ids_count_only_reviews_actually_carried_by_projection(self):
        facts = {"f": {"value": "1", "unit": "CNY", "basis": "reported",
                       "period_kind": "year", "evidence_id": "e"}}
        publication = prepare(
            title="审核统计", blocks=[block([{"kind": "text", "text": "普通正文"}])],
            refs={"evidence": {"e": {}}}, facts=facts, calculations={}, max_chars=4000,
            datasets=[{"title": "受控表", "description": "受控😀说明", "display": "table",
                       "points": [{"label": "甲", **numeric("fact", "f")}]}])
        generated = publication["blocks"][-1]
        exact = "受控😀说明"
        start = publication["answer"].index(exact, generated["start"], generated["end"])
        attached = {"id": "dataset-attached", "start": start, "end": start + len(exact),
                    "exact_text": exact, "support_status": "supported_by_evidence",
                    "evidence_refs": [], "reason": "受控理由"}
        publication["semantic_review"] = {"status": "assessed", "claims": [attached,
            {**attached, "id": "wrong-text", "exact_text": "不匹配"}]}
        datasets = project_datasets(publication, {})
        self.assertEqual(_dataset_semantic_claim_ids(datasets), {"dataset-attached"})

    def test_history_is_append_only_and_frozen_review_does_not_follow_changed_events(self):
        initial, answer = self.publish()
        saved = self.chat.publications.read(answer)
        with closing(self.chat._connect()) as connection, connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM report_publications")
            connection.execute("UPDATE events SET payload_json=? WHERE run_id=? AND type='semantic_review'",
                               ('{"authoritative":true,"review":{"status":"forged"}}', initial["run_id"]))
        artifact = AnswerArtifactService(self.chat).get(initial["conversation_id"], answer["message_id"])
        self.assertEqual(artifact["verification"]["semantic_review"], saved["semantic_review"])
        second = self.chat.submit(initial["conversation_id"], "请整理完整新版")
        self.assertEqual(wait(self.chat, second["run_id"])["status"], "completed")
        self.assertEqual(self.chat.publications.read(answer), saved)

    def test_missing_or_corrupt_publication_never_falls_back_to_prose(self):
        initial, answer = self.publish()
        with closing(self.chat._connect()) as connection, connection:
            connection.execute("DROP TRIGGER report_publications_no_update")
            connection.execute("UPDATE report_publications SET payload_sha256='changed'")
        with self.assertRaises(ChatError) as caught:
            AnswerArtifactService(self.chat).get(initial["conversation_id"], answer["message_id"])
        self.assertEqual(caught.exception.code, "publication_integrity_mismatch")
        with closing(self.chat._connect()) as connection, connection:
            connection.execute("DROP TRIGGER report_publications_no_delete")
            connection.execute("DELETE FROM report_publications")
        with self.assertRaises(ChatError) as caught:
            AnswerArtifactService(self.chat).get(initial["conversation_id"], answer["message_id"])
        self.assertEqual(caught.exception.code, "publication_missing")

    def test_failure_to_commit_publication_rolls_back_assistant_and_completed_status(self):
        with patch.object(self.chat.publications, "save", side_effect=RuntimeError("injected failure")):
            initial = self.chat.create_and_submit("MSFT", "微软收入变化")
            run = wait(self.chat, initial["run_id"])
        self.assertEqual(run["status"], "failed")
        with closing(self.chat._connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM report_publications").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0], 0)

    def test_required_provider_cannot_publish_free_text_but_legacy_provider_can(self):
        self.provider.stream = CalculationProvider.stream.__get__(self.provider, ReportProvider)
        initial = self.chat.create_and_submit("MSFT", "微软收入变化")
        result = wait(self.chat, initial["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "structured_report_required")
        self.provider.requires_structured_reports = False
        initial = self.chat.create_and_submit("MSFT", "微软收入变化")
        self.assertEqual(wait(self.chat, initial["run_id"])["status"], "completed")
        answer = self.chat.get_conversation(initial["conversation_id"])["messages"][-1]
        artifact = AnswerArtifactService(self.chat).get(initial["conversation_id"], answer["message_id"])
        self.assertNotIn("publication", artifact)

    def test_typed_values_cannot_be_invented_or_cross_run_and_bad_requests_are_atomic(self):
        refs = {"evidence": {"e": {}}, "calculation": {"c": {}}}
        facts = {"f": {"fact_id": "f", "evidence_id": "e", "value": "1", "unit": "CNY", "company_id": "MSFT"}}
        base = block([numeric("fact", "f")], classification="source_statement")
        expected = prepare(title="有来源的值", blocks=[base], refs=refs, facts=facts, calculations={}, max_chars=4000)
        for parts in ([{"kind": "fact", "id": "missing"}], [{"kind": "fact", "id": "f", "value": "999"}],
                      [{"kind": "text", "text": "[evidence:e] 999"}]):
            bad = {**base, "parts": parts}
            with self.subTest(parts=parts), self.assertRaises(ToolError):
                prepare(title="Bad", blocks=[bad], refs=refs, facts=facts, calculations={}, max_chars=4000)
        with self.assertRaises(ToolError):
            prepare(title="Bad", blocks=[base], refs={}, facts=facts, calculations={}, max_chars=4000)
        broken = deepcopy(expected)
        broken["blocks"][0]["spans"][0]["end"] += 1
        with self.assertRaises(ToolError):
            project(broken, {}, {})

    def test_explicit_datasets_use_exact_records_and_reject_incompatible_charts(self):
        facts = {
            "a": {"value": "1", "unit": "CNY", "basis": "reported", "period_kind": "year", "evidence_id": "ea"},
            "b": {"value": "2", "unit": "CNY", "basis": "reported", "period_kind": "year", "evidence_id": "eb"},
        }
        refs = {"evidence": {"ea": {}, "eb": {}}}
        datasets = [{"title": "自主选择的比较", "description": "仅比较已选两个项目", "display": "bar",
                     "points": [{"label": "甲", **numeric("fact", "a")},
                                {"label": "乙", **numeric("fact", "b")}]}]
        kwargs = {"title": "Report", "blocks": [block([{"kind": "text", "text": "说明"}])],
                  "refs": refs, "facts": facts, "calculations": {}, "max_chars": 4000, "datasets": datasets}
        publication = prepare(**kwargs)
        self.assertIn("| 甲 | 1 CNY [evidence:ea] |", publication["answer"])
        findings, anchors = project(publication, {("evidence", "ea"): {"valid": True}, ("evidence", "eb"): {"valid": True}},
                                    {("evidence", "ea"): "trace-a", ("evidence", "eb"): "trace-b"})
        self.assertEqual(len(findings), 1)
        self.assertIn("说明", findings[0]["text"])
        self.assertNotIn("| 项目 |", findings[0]["text"])
        self.assertFalse(any(anchor["text"].startswith("###") for anchor in anchors))
        projected = project_datasets(publication, {("evidence", "ea"): "trace-a", ("evidence", "eb"): "trace-b"})
        self.assertEqual([row["value"] for row in projected[0]["rows"]], ["1", "2"])
        self.assertEqual(projected[0]["rows"][1]["trace_id"], "trace-b")
        self.assertEqual(
            [span["binding"] for span in publication["blocks"][-1]["spans"] if span["binding"] is not None],
            [point["binding"] for point in publication["datasets"][0]["points"]],
        )
        datasets[0]["display"] = "line"
        line = project_datasets(prepare(**kwargs), {})
        self.assertEqual(line[0]["chart"], {"type": "line", "x": "label", "y": "value"})
        datasets[0]["display"] = "scatter"
        scatter = project_datasets(prepare(**kwargs), {})
        self.assertEqual(scatter[0]["chart"], {"type": "scatter", "x": "label", "y": "value"})
        datasets[0]["display"] = "bar"
        facts["b"]["unit"] = "USD"
        with self.assertRaises(ToolError) as caught:
            prepare(**kwargs)
        self.assertEqual(caught.exception.code, "incompatible_chart")
        datasets[0]["display"] = "table"
        mixed = project_datasets(prepare(**kwargs), {})
        self.assertIsNone(mixed[0]["chart"])
        self.assertEqual([row["unit"] for row in mixed[0]["rows"]], ["CNY", "USD"])

    def test_dataset_projection_preserves_exact_review_outcomes_without_restoring_markdown_finding(self):
        facts = {
            "a": {"value": "1", "unit": "CNY", "basis": "reported", "period_kind": "year", "evidence_id": "ea"},
            "b": {"value": "2", "unit": "CNY", "basis": "reported", "period_kind": "year", "evidence_id": "eb"},
        }
        publication = prepare(
            title="审核传播", blocks=[block([{"kind": "text", "text": "| 普通 | Markdown表 |\n|---|---|\n| 保留 | 正文 |"}])],
            refs={"evidence": {"ea": {}, "eb": {}}}, facts=facts, calculations={}, max_chars=4000,
            datasets=[{"title": "受控审核表", "description": "受控审核说明", "display": "table",
                       "points": [{"label": "甲", **numeric("fact", "a")},
                                  {"label": "乙", **numeric("fact", "b")}]}])
        generated = publication["blocks"][-1]
        value_span = next(span for span in generated["spans"] if span["binding"] is not None)

        def claim(identifier, text, status, reason):
            start = publication["answer"].index(text, generated["start"], generated["end"])
            return {"id": identifier, "start": start, "end": start + len(text), "exact_text": text,
                    "support_status": status, "evidence_refs": [], "reason": reason}

        cases = [
            ("supported_by_evidence", "supported", False),
            ("unsupported", "unsupported", True),
            ("conflicting", "conflicting", True),
        ]
        for verdict, expected, negative in cases:
            with self.subTest(verdict=verdict):
                publication["semantic_review"] = {
                    "status": "assessed", "claims": [claim("table", value_span["text"], verdict, "受控原因")],
                }
                findings, _anchors = project(publication, {}, {})
                projected = project_datasets(publication, {})[0]
                self.assertEqual([finding["id"] for finding in findings], ["report-block-1"])
                self.assertIn("普通 | Markdown表", findings[0]["text"])
                self.assertEqual(projected["semantic_review"]["status"], expected)
                self.assertEqual(bool(projected["review_flags"]), negative)
                self.assertEqual(projected["rows"][0]["semantic_review"]["support_status"], verdict)
                self.assertNotIn("semantic_review", projected["rows"][1])

        publication["semantic_review"] = {"status": "assessed", "claims": [
            claim("supported", "受控审核表", "supported_by_evidence", "标题有依据"),
            claim("unsupported", "受控审核说明", "unsupported", "说明无依据"),
        ]}
        mixed = project_datasets(publication, {})[0]
        self.assertEqual(mixed["semantic_review"]["status"], "mixed")
        self.assertEqual(mixed["review_flags"][0]["code"], "semantic_unsupported")

        publication["semantic_review"] = {"status": "not_assessed", "claims": []}
        not_assessed = project_datasets(publication, {})[0]
        self.assertEqual(not_assessed["semantic_review"]["status"], "not_assessed")
        self.assertFalse(not_assessed["review_flags"])

    def test_matrix_presentation_maps_every_bound_point_once_and_reuses_anchors(self):
        facts = {
            key: {"value": str(index), "unit": "CNY", "basis": "reported", "period_kind": "year",
                  "period_start": f"20{23 + (index % 2)}-01-01", "period_end": f"20{23 + (index % 2)}-12-31",
                  "evidence_id": f"e-{key}"}
            for index, key in enumerate(("a", "b", "c", "d"), 1)
        }
        refs = {"evidence": {f"e-{key}": {} for key in facts}}
        presentation = {
            "after_block_id": "report-block-2", "unit": "CNY", "note": "按已保存披露口径并列，不代表经济可比性。",
            "columns": [{"key": "p1", "label": "期间一"}, {"key": "p2", "label": "期间二"}],
            "rows": [
                {"key": "subject-a", "label": "主体甲", "cells": [{"point_index": 2}, {"point_index": 0}]},
                {"key": "subject-b", "label": "主体乙", "cells": [{"point_index": 3}, {"point_index": 1}]},
            ],
        }
        datasets = [{"title": "显式比较矩阵", "description": "仅用于工程契约验证", "display": "table",
                     "points": [{"label": key, **numeric("fact", key)} for key in facts],
                     "presentation": presentation}]
        similar_matrix_prose = ("### 显式比较矩阵\n\n仅用于工程契约验证\n\n"
                                "| 项目 | 期间一 | 期间二 |\n|---|---|---|\n"
                                "这是同标题且含 Markdown 的真正分析正文。")
        publication = prepare(
            title="矩阵契约测试", blocks=[block([{"kind": "text", "text": similar_matrix_prose}]),
                                      block([{"kind": "text", "text": "比较解释"}])],
            refs=refs, facts=facts, calculations={}, max_chars=10000, datasets=datasets)
        self.assertIn("单位：CNY", publication["answer"])
        self.assertIn("注：按已保存披露口径并列，不代表经济可比性。", publication["answer"])
        self.assertIn("| 项目 | 期间一 | 期间二 |", publication["answer"])
        self.assertIn("| 主体甲 | 3 CNY [evidence:e-c] | 1 CNY [evidence:e-a] |", publication["answer"])
        self.assertIn("| 主体乙 | 4 CNY [evidence:e-d] | 2 CNY [evidence:e-b] |", publication["answer"])
        dataset_bindings = [span["binding"]["id"] for span in publication["blocks"][-1]["spans"]
                            if span["binding"] is not None]
        self.assertEqual(dataset_bindings, ["c", "a", "d", "b"])
        self.assertEqual(sorted(dataset_bindings), sorted(facts))
        findings, anchors = project(publication, {}, {})
        self.assertEqual([finding["id"] for finding in findings], ["report-block-1", "report-block-2"])
        self.assertTrue(findings[0]["text"].startswith(similar_matrix_prose))
        self.assertNotIn("report-block-3", {anchor["finding_id"] for anchor in anchors})
        projected_rows = project_datasets(publication, {})[0]["rows"]
        self.assertEqual([row["value"] for row in projected_rows], ["1", "2", "3", "4"])
        self.assertEqual([row["reference"]["id"] for row in projected_rows], ["e-a", "e-b", "e-c", "e-d"])
        from workbench.semantic_review import publication_review_scopes
        scopes = publication_review_scopes(publication)
        self.assertEqual(scopes[0]['start'], 0)
        self.assertEqual(scopes[-1]['end'], len(publication['answer']))
        invalid_order = deepcopy(publication)
        invalid_order['datasets'][0]['presentation']['rows'][0]['cells'][0]['point_index'] = 0
        with self.assertRaisesRegex(ValueError, 'every bound point exactly once'):
            publication_review_scopes(invalid_order)
        matrix = project_datasets(publication, {})[0]["presentation"]
        self.assertEqual(matrix["after_block_id"], "report-block-2")
        self.assertEqual([[cell["point_id"] for cell in row["cells"]] for row in matrix["rows"]], [
            ["report-dataset-1-point-3", "report-dataset-1-point-1"],
            ["report-dataset-1-point-4", "report-dataset-1-point-2"],
        ])
        self.assertTrue(all(cell["anchor_id"] == cell["point_id"] + "-cell-value"
                            for row in matrix["rows"] for cell in row["cells"]))
        self.assertNotIn("value", matrix["rows"][0]["cells"][0])

        bad_presentations = []
        duplicate = deepcopy(presentation)
        duplicate["rows"][1]["cells"][0]["point_index"] = 0
        bad_presentations.append(duplicate)
        missing = deepcopy(presentation)
        missing["rows"] = missing["rows"][:1]
        bad_presentations.append(missing)
        outside = deepcopy(presentation)
        outside["rows"][1]["cells"][1]["point_index"] = 99
        bad_presentations.append(outside)
        unknown_block = deepcopy(presentation)
        unknown_block["after_block_id"] = "report-block-99"
        bad_presentations.append(unknown_block)
        wrong_unit = deepcopy(presentation)
        wrong_unit["unit"] = "USD"
        bad_presentations.append(wrong_unit)
        for invalid in bad_presentations:
            bad = deepcopy(datasets)
            bad[0]["presentation"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(ToolError) as caught:
                prepare(title="拒绝坏矩阵", blocks=[block([{"kind": "text", "text": "一"}]),
                                                   block([{"kind": "text", "text": "二"}])], refs=refs,
                        facts=facts, calculations={}, max_chars=10000, datasets=bad)
            self.assertEqual(caught.exception.code, "invalid_report_dataset")
        chart = deepcopy(datasets)
        chart[0]["display"] = "bar"
        with self.assertRaises(ToolError):
            prepare(title="拒绝兼容性误标", blocks=[block([{"kind": "text", "text": "一"}]),
                                                   block([{"kind": "text", "text": "二"}])], refs=refs,
                    facts=facts, calculations={}, max_chars=10000, datasets=chart)

    def test_explicit_real_time_axis_is_frozen_and_rejects_incompatible_facts(self):
        facts = {
            "late": {"value": "20.00", "unit": "CNY", "company_id": "subject-1", "metric": "revenue",
                     "basis": "reported", "period_kind": "quarter", "period_start": "2025-04-01",
                     "period_end": "2025-06-30", "evidence_id": "e-late"},
            "early": {"value": "10.00", "unit": "CNY", "company_id": "subject-1", "metric": "revenue",
                      "basis": "reported", "period_kind": "quarter", "period_start": "2025-01-01",
                      "period_end": "2025-03-31", "evidence_id": "e-early"},
        }
        refs = {"evidence": {"e-late": {}, "e-early": {}}}
        dataset = {"title": "真实事实趋势", "description": "受控契约数据", "display": "line",
                   "time_axis": "period_end", "points": [
                       {"label": "后期", **numeric("fact", "late")},
                       {"label": "前期", **numeric("fact", "early")}]}
        args = dict(title="时间轴契约", blocks=[block([{"kind": "text", "text": "说明"}])], refs=refs,
                    facts=facts, calculations={}, max_chars=10000, datasets=[dataset])
        publication = prepare(**args)
        self.assertEqual(publication["datasets"][0]["time_axis"], {
            "version": "utc-period-axis/1", "field": "period_end", "timezone": "UTC", "gap_policy": "break"})
        self.assertIn("UTC真实日期距离", publication["answer"])
        findings, _ = project(publication, {}, {})
        self.assertEqual([finding["id"] for finding in findings], ["report-block-1"])
        projected = project_datasets(publication, {})[0]
        self.assertEqual(projected["chart"]["x"], "period.end")
        self.assertEqual([row["id"] for row in projected["rows"]],
                         ["report-dataset-1-point-1", "report-dataset-1-point-2"])
        self.assertEqual(projected["rows"][0]["company_id"], "subject-1")

        mutations = []
        for field, value in (("company_id", "subject-2"), ("metric", "profit"), ("unit", "USD"),
                             ("basis", "adjusted"), ("period_kind", "month"),
                             ("period_start", "2025-02-30"), ("period_end", "2025-03-31"),
                             ("value", "9007199254740993"), ("value", "1e3"),
                             ("value", "0." + "0" * 350 + "1")):
            changed = deepcopy(facts); changed["late"][field] = value; mutations.append((field, changed))
        overlap = deepcopy(facts); overlap["late"]["period_start"] = "2025-03-31"; mutations.append(("overlap", overlap))
        for name, changed in mutations:
            with self.subTest(name=name), self.assertRaises(ToolError) as caught:
                prepare(**{**args, "facts": changed})
            self.assertIn(caught.exception.code, {"incompatible_time_axis", "incompatible_chart"})

        calculation_dataset = deepcopy(dataset)
        calculation_dataset["points"][0] = {"label": "派生", **numeric("calculation", "c")}
        calculation = {"c": {**facts["late"], "arithmetic": {"output_context": facts["late"]}}}
        with self.assertRaises(ToolError) as caught:
            prepare(**{**args, "datasets": [calculation_dataset], "calculations": calculation,
                       "refs": {**refs, "calculation": {"c": {}}}})
        self.assertEqual(caught.exception.code, "incompatible_time_axis")

    def test_dataset_description_support_is_explicit_scoped_and_clickable(self):
        facts = {"f": {"value": "1", "unit": "CNY", "basis": "reported", "period_kind": "year", "evidence_id": "e"}}
        reference = {"kind": "passage", "id": "footnote"}
        dataset = {"title": "受控😀数据", "description": "本列为重列比较数，仅用于测试。",
                   "display": "table", "points": [{"label": "值", **numeric("fact", "f")}],
                   "support": [reference]}
        args = dict(title="受控报告", blocks=[block([{"kind": "text", "text": "正文"}])],
                    refs={"evidence": {"e": {}}, "passage": {"footnote": {}}}, facts=facts,
                    calculations={}, max_chars=4000, datasets=[dataset])
        publication = prepare(**args)
        self.assertEqual(publication["datasets"][0]["support"], [reference])
        self.assertIn(reference, publication["blocks"][-1]["support"])
        self.assertIn("[passage:footnote]", publication["blocks"][-1]["text"])
        start = publication["answer"].index(dataset["description"])
        publication["semantic_review"] = {"status": "assessed", "claims": [{
            "id": "description", "start": start, "end": start + len(dataset["description"]),
            "exact_text": dataset["description"], "support_status": "supported_by_evidence",
            "evidence_refs": [reference], "reason": "受控脚注支持"}]}
        projected = project_datasets(publication, {("passage", "footnote"): "trace-footnote"})[0]
        anchor = projected["description_anchor"]
        self.assertEqual(anchor["references"], [reference])
        self.assertEqual(anchor["trace_ids"], ["trace-footnote"])
        self.assertEqual(publication["answer"][anchor["start"]:anchor["end"]], anchor["text"])
        self.assertEqual(anchor["utf16_start"], anchor["start"])
        self.assertEqual(anchor["utf16_end"], anchor["end"] + 1)
        self.assertEqual(anchor["text"], f'{dataset["title"]}\n\n{dataset["description"]}')
        self.assertEqual(anchor["semantic_review"]["status"], "supported")
        self.assertEqual([item["id"] for item in project(publication, {}, {})[0]], ["report-block-1"])
        for bad in ([{"kind": "passage", "id": "not-obtained"}], [reference] * 17, None):
            with self.subTest(support=bad), self.assertRaises(ToolError):
                prepare(**{**args, "datasets": [{**dataset, "support": bad}]})

    def test_optional_presentation_keeps_base_dataset_shape_deterministic(self):
        facts = {"f": {"value": "1", "unit": "CNY", "basis": "reported", "period_kind": "year",
                       "evidence_id": "e"}}
        base_publication = prepare(
            title="Base", blocks=[block([{"kind": "text", "text": "说明"}])],
            refs={"evidence": {"e": {}}}, facts=facts, calculations={}, max_chars=4000,
            datasets=[{"title": "基础表", "description": "当前两列表", "display": "table",
                       "points": [{"label": "甲", **numeric("fact", "f")}]}])
        self.assertNotIn("presentation", base_publication["datasets"][0])
        raw = json.dumps(base_publication, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(), "9574a94fc73e1d9df4c1556b5429e40368874835bacb598b1137d06a4b3ae836")
