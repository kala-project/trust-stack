from contextlib import closing
from copy import deepcopy
from pathlib import Path
import json
import hashlib
import tempfile
import threading
import unittest
from unittest.mock import patch

from tests.test_recorded_calculations import CalculationProvider, wait
from workbench import regression
from workbench.answer_artifacts import AnswerArtifactService
from workbench.build import build_database
from workbench.chat import ChatError, ChatService
from workbench.documents import DocumentService
from workbench.research_tools import ResearchTools, ToolError


def real_plan(tools):
    def series(metric):
        return [next(fact["fact_id"] for fact in tools._facts.values()
                     if fact["metric"] == metric and fact["period_end"] == f"{year}-06-30"
                     and fact["period_kind"] == "quarter" and fact["basis"] == "GAAP issuer reconciliation table")
                for year in (2023, 2024, 2025)]
    return {"method": "ols_with_intercept", "x_fact_ids": series("revenue"),
            "y_fact_ids": series("operating_income"), "y_prior_fact_ids": [],
            "y_transform": "level", "lag_years": 0}


class RegressionProvider(CalculationProvider):
    """Explicit protocol substitute; never classified as a real model evaluation."""
    def __init__(self, plan):
        super().__init__()
        self.plan, self.decision, self.result = plan, None, None
        self.decision_ready = threading.Event()

    def stream(self, messages, tools, cancel_event):
        handlers = {tool["name"]: tool["handler"] for tool in tools}
        if self.decision is None:
            self.decision = handlers["request_confirmation"](
                kind="model", question="确认收入与营业利润的样本线性关联？",
                explanation="仅比较2023至2025年的6月季度，不代表全年，也不推断因果。",
                options=[{"id": "ols", "label": "含截距回归", "benefit": "可复算样本内线性关联",
                          "limitation": "只有三组样本，不能据此判断因果或预测能力",
                          "regression": self.plan}], multiple=False)["decision_id"]
            self.decision_ready.set()
            return
        marker = "(context, not source evidence):\n"
        contexts = [json.loads(message["content"].split(marker, 1)[1])
                    for message in messages if marker in message["content"]]
        confirmed = contexts[-1][-1]
        self.result = handlers["calculate_regression"](decision_id=confirmed["decision_id"], option_id="ols")
        labels = {"regression_slope": "回归斜率", "regression_intercept": "回归截距",
                  "regression_r_squared": "样本内R²"}
        text = "\n\n".join(f"{labels[item['operation']]}为 {item['value']} {item['unit']}。"
                           f"[calculation:{item['calculation_id']}]"
                           for item in self.result["calculations"])
        yield {"type": "delta", "text": text + "\n\n以上仅为样本内线性关联，不证明因果，也不是预测。"}


class DocumentRegressionProvider(RegressionProvider):
    def __init__(self, version_id, *, repeat_extraction=False, reconfirm=False):
        super().__init__(None)
        self.version_id = version_id
        self.repeat_extraction = repeat_extraction
        self.reconfirm = reconfirm

    def extract(self, handlers):
        ids = []
        for metric, row, values in (("capital_expenditures", 1, (1, 2, 3)),
                                    ("revenue", 2, (3, 5, 7))):
            current = []
            for column, (year, value) in enumerate(zip((2023, 2024, 2025), values), 1):
                result = handlers["extract_document_table_fact"](
                    document_version_id=self.version_id, table_index=0,
                    row_index=row, column_index=column, expected_value=str(value),
                    metric=metric, unit="USD millions", basis="synthetic fixture",
                    period_kind="year", period_start=f"{year}-01-01", period_end=f"{year}-12-31",
                    header_row_indices=[0], row_label_column_index=0,
                    metadata_mode="explicit_interpretation")
                current.append(result["fact"]["fact_id"])
            ids.append(current)
        self.plan = {"method": "ols_with_intercept", "x_fact_ids": ids[0],
                     "y_fact_ids": ids[1], "y_prior_fact_ids": [],
                     "y_transform": "level", "lag_years": 0}

    def stream(self, messages, tools, cancel_event):
        handlers = {item["name"]: item["handler"] for item in tools}
        if self.decision is None:
            self.extract(handlers)
        yield from super().stream(messages, tools, cancel_event)
        if self.result is not None:
            if self.repeat_extraction:
                self.extract(handlers)
                self.extract(handlers)
            handlers["calculate_regression"](decision_id=self.decision, option_id="ols")
            if self.reconfirm:
                self.reconfirm = False
                self.decision = handlers["request_confirmation"](
                    kind="model", question="重新确认同一组文档样本？",
                    explanation="协议测试：使用本轮已校验复用的输入建立下一份模型方案。",
                    options=[{"id": "ols", "label": "原样本回归", "benefit": "完整保留来源",
                              "limitation": "合成数据仅验证协议", "regression": self.plan}],
                    multiple=False)["decision_id"]


class RegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.db = Path(cls.fixture.name) / "evidence.sqlite3"
        build_database(db_path=cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.tools = ResearchTools(self.db, "MSFT")
        self.authorization = {"decision_id": "decision_test", "option_id": "ols", "source_run_id": "run_test"}

    def document_research(self, root, **options):
        documents = DocumentService(root / "documents")
        source = root / "fixture.html"
        source.write_text(
            "<!doctype html><html><body><table><tr><th>USD millions</th><th>2023</th><th>2024</th><th>2025</th></tr>"
            "<tr><td>Capital expenditures</td><td>$1</td><td>$2</td><td>$3</td></tr>"
            "<tr><td>Revenue</td><td>$3</td><td>$5</td><td>$7</td></tr></table></body></html>")
        version = documents.register_file(source, company_id="MSFT", source_key="synthetic-test",
                                          title="Explicit synthetic regression fixture")
        provider = DocumentRegressionProvider(version["document_version_id"], **options)
        chat = ChatService(self.db, root / "chat.sqlite3", provider, document_service=documents)
        self.addCleanup(chat.close)
        initial = chat.create_and_submit("MSFT", "使用所选表格做回归")
        self.assertEqual(wait(chat, initial["run_id"])["status"], "blocked")
        # The transaction can expose blocked before this protocol double receives
        # the tool return and assigns its local decision ID. Synchronize that
        # test-only state instead of racing an immediate continuation against it.
        self.assertTrue(provider.decision_ready.wait(5), "test provider did not receive its saved decision")
        return chat, provider, initial, version

    def synthetic(self, x_values, y_values):
        """Controlled arithmetic fixture, not source-backed financial evidence."""
        base = next(iter(self.tools._facts.values()))
        ids = []
        for metric, values in (("test_x", x_values), ("test_y", y_values)):
            current = []
            for index, value in enumerate(values):
                identifier = f"{metric}_{index}"
                self.tools._facts[identifier] = {
                    **base, "fact_id": identifier, "metric": metric, "value": str(value), "unit": "USD millions",
                    "basis": "synthetic", "period_kind": "year", "period_start": f"{2020+index}-01-01",
                    "period_end": f"{2020+index}-12-31",
                }
                current.append(identifier)
            ids.append(current)
        return {"method": "ols_with_intercept", "x_fact_ids": ids[0], "y_fact_ids": ids[1],
                "y_prior_fact_ids": [], "y_transform": "level", "lag_years": 0}

    def test_exact_line_and_deterministic_ids(self):
        plan = self.synthetic([1, 2, 3], [3, 5, 7])
        first = regression.calculate(self.tools, plan, self.authorization)
        self.assertEqual(first, regression.calculate(self.tools, plan, self.authorization))
        self.assertEqual([item["value"] for item in first["calculations"]], ["2.000000", "1.000000", "100.000000"])
        self.assertEqual(len({item["calculation_id"] for item in first["calculations"]}), 3)
        self.assertTrue(all(len(item["inputs"]) == 6 for item in first["calculations"]))

    def test_nonperfect_fit_and_signed_rounding(self):
        plan = self.synthetic([1, 2, 3], [1, 2, 4])
        records = regression.calculate(self.tools, plan, self.authorization)["calculations"]
        self.assertEqual([item["value"] for item in records], ["1.500000", "-0.666667", "96.428571"])

    def test_large_nearby_values_do_not_collapse_to_a_float_constant(self):
        base = 10 ** 40
        plan = self.synthetic([base + 1, base + 2, base + 3], [3, 5, 7])
        records = regression.calculate(self.tools, plan, self.authorization)["calculations"]
        self.assertEqual([item["value"] for item in records],
                         ["2.000000", f"{1 - 2 * base}.000000", "100.000000"])

    def test_model_projection_keeps_all_output_ids_without_mutating_full_provenance(self):
        result = regression.calculate(self.tools, real_plan(self.tools), self.authorization)
        result["_tool_budget"] = {"remaining": 12}
        result["_retrieval_policy"] = {"closed": True, "known_refs": {"passage_candidates": ["x" * 20000]}}
        original = deepcopy(result)
        projected = regression.model_result(result)
        self.assertEqual(result, original)
        self.assertLess(len(json.dumps(projected, ensure_ascii=False).encode()), 8000)
        self.assertEqual(projected["report_values"], result["report_values"])
        self.assertEqual([item["calculation_id"] for item in projected["calculations"]],
                         [item["calculation_id"] for item in result["calculations"]])
        self.assertEqual(projected["_tool_budget"], {"remaining": 12})
        self.assertTrue(projected["_retrieval_policy"]["closed"])
        self.assertNotIn("known_refs", projected["_retrieval_policy"])
        self.assertTrue(all(len(item["inputs"]) == 6 for item in result["calculations"]))
        error = {"error": "regression_confirmation_required", "message": "Confirm first"}
        self.assertEqual(regression.model_result(error), error)

    def test_output_binding_keeps_sign_and_units_but_allows_trailing_zero_removal(self):
        from workbench.answer_artifacts import _calculation_output_matches
        positive = {"operation": "regression_slope", "value": "2.000000", "unit": "x"}
        self.assertTrue(_calculation_output_matches("斜率为 2 x", positive))
        self.assertTrue(_calculation_output_matches("斜率为 **2**；截距另行列示。", positive))
        self.assertFalse(_calculation_output_matches("斜率为 -2 x", positive))
        self.assertFalse(_calculation_output_matches("斜率为 -2；截距另行列示。", positive))
        self.assertFalse(_calculation_output_matches("斜率为 2 百万美元", positive))
        self.assertFalse(_calculation_output_matches("斜率为 2%", positive))
        growth = {"operation": "regression_slope", "value": "-2.000000", "unit": "百分点 / USD millions"}
        self.assertTrue(_calculation_output_matches("斜率为 -2 百分点 / 百万美元", growth))
        self.assertFalse(_calculation_output_matches("斜率为 2 百分点 / 百万美元", growth))
        self.assertFalse(_calculation_output_matches("斜率为 -2 百分点 / 十亿美元", growth))

    def test_dimensional_coefficient_is_not_misread_as_an_absolute_cash_amount(self):
        calc = {"calculation_id": "calc-" + "a" * 64, "operation": "regression_slope",
                "value": "2.000000", "unit": "CNY / USD millions", "status": "recorded"}
        ref = ("calculation", calc["calculation_id"])
        text = f"斜率为 2 CNY / USD millions。[calculation:{calc['calculation_id']}]"
        service = AnswerArtifactService(None)
        findings = service._findings(text, {ref: {"valid": True}}, {}, {}, [calc])
        self.assertEqual(findings[0]["status"], "source_linked")
        self.assertEqual(findings[0]["classification"], "recorded_calculation")
        unrelated = text + " 利润还有5百万美元。"
        findings = service._findings(unrelated, {ref: {"valid": True}}, {}, {}, [calc])
        self.assertEqual(findings[0]["status"], "needs_review")

    def test_growth_transform_has_explicit_prior_inputs_and_lag(self):
        plan = self.synthetic([1, 2, 3], [100, 110, 132, "171.6"])
        plan.update(y_fact_ids=["test_y_1", "test_y_2", "test_y_3"],
                    y_prior_fact_ids=["test_y_0", "test_y_1", "test_y_2"],
                    y_transform="percent_change", lag_years=1)
        records = regression.calculate(self.tools, plan, self.authorization)["calculations"]
        self.assertEqual([item["value"] for item in records], ["10.000000", "0.000000", "100.000000"])
        self.assertEqual(records[0]["unit"], "百分点 / USD millions")
        self.assertEqual(len(records[0]["inputs"]), 7)
        self.assertEqual([row["y"] for row in records[0]["regression"]["observations"]],
                         ["10.000000000000", "20.000000000000", "30.000000000000"])
        self.tools._facts["test_y_0"]["value"] = "0"
        with self.assertRaises(ToolError):
            regression.calculate(self.tools, plan, self.authorization)

    def test_no_silent_sample_deletion_or_scope_conversion(self):
        plan = self.synthetic([1, 2, 3], [3, 5, 7])
        changes = [("unit", "CNY millions"), ("basis", "different"), ("company_id", "AAPL"),
                   ("segment", "cloud"), ("period_end", "2022-09-30"), ("value", "NaN")]
        for key, value in changes:
            with self.subTest(key=key):
                original = deepcopy(self.tools._facts["test_y_2"])
                self.tools._facts["test_y_2"][key] = value
                with self.assertRaises(ToolError):
                    regression.calculate(self.tools, plan, self.authorization)
                self.tools._facts["test_y_2"] = original

    def test_constant_variables_and_invalid_plans_fail_explicitly(self):
        with self.assertRaises(ToolError) as caught:
            regression.calculate(self.tools, self.synthetic([1, 1, 1], [1, 2, 3]), self.authorization)
        self.assertEqual(caught.exception.code, "regression_constant_x")
        plan = self.synthetic([1, 2, 3], [3, 5, 7])
        for invalid in ({**plan, "lag_years": True}, {**plan, "method": "guessed"},
                        {**plan, "x_fact_ids": ["test_x_0"] * 3},
                        {**plan, "y_fact_ids": plan["y_fact_ids"][:2]},
                        {**plan, "y_fact_ids": list(reversed(plan["y_fact_ids"]))}):
            with self.assertRaises(ToolError):
                regression.calculate(self.tools, invalid, self.authorization)

    def test_constant_y_preserves_defined_coefficients_without_fabricating_r_squared(self):
        result = regression.calculate(self.tools, self.synthetic([1, 2, 3], [7, 7, 7]), self.authorization)
        self.assertEqual([(item["operation"], item["value"]) for item in result["calculations"]],
                         [("regression_slope", "0.000000"), ("regression_intercept", "7.000000")])
        self.assertEqual(result["undefined_statistics"][0]["operation"], "regression_r_squared")
        self.assertEqual(result["undefined_statistics"][0]["status"], "undefined")
        self.assertIn("分母为零", result["undefined_statistics"][0]["reason"])

    def test_worker_confirmation_calculation_artifact_and_source_replay(self):
        provider = RegressionProvider(real_plan(self.tools))
        with tempfile.TemporaryDirectory() as directory:
            chat = ChatService(self.db, Path(directory) / "chat.sqlite3", provider)
            try:
                initial = chat.create_and_submit("MSFT", "用收入与营业利润做样本回归")
                paused = wait(chat, initial["run_id"])
                self.assertEqual(paused["status"], "blocked", paused.get("error"))
                self.assertIsNone(provider.result)
                option = paused["decision"]["options"][0]
                self.assertEqual(option["regression_details"]["sample_count"], 3)
                with self.assertRaises(ToolError):
                    chat.decisions.regression_definition(initial["run_id"], self.tools)["handler"](
                        decision_id=provider.decision, option_id="ols")
                resumed = chat.decisions.answer(provider.decision, ["ols"], "", "accepted", "accept-ols")
                completed = wait(chat, resumed["run_id"], timeout=10)
                self.assertEqual(completed["status"], "completed", completed.get("error"))
                answer = chat.get_conversation(initial["conversation_id"])["messages"][-1]
                self.assertEqual(len(answer["citations"]), 3)
                self.assertTrue(all(item["valid"] for item in answer["citations"]))
                artifact = AnswerArtifactService(chat).get(initial["conversation_id"], answer["message_id"])
                self.assertEqual(len(artifact["calculations"]), 3, artifact["limitations"])
                self.assertTrue(all(item["classification"] == "recorded_calculation"
                                    and not item["review_flags"] for item in artifact["findings"][:3]))
                from workbench.answer_review import AnswerReview
                from workbench.semantic_review import build_review_request
                refs = {"claim": {}, "evidence": {}, "passage": {}, "calculation": {}}
                ChatService._record_calculation_ref(provider.result, refs)
                reviewer = AnswerReview(chat, resumed["run_id"], "MSFT", "回归分析", refs)
                self.assertEqual(len(reviewer.calculations), 3)
                self.assertTrue(reviewer.frozen_fact_amounts)
                packet = build_review_request(reviewer, answer["content"])
                self.assertEqual(len(packet["authoritative_context"]["calculations"]), 3)
                self.assertTrue(all(item["regression"]["sample_count"] == 3
                                    for item in packet["authoritative_context"]["calculations"]))
                scope_check = reviewer.check(
                    f"回归使用3组样本。[calculation:{provider.result['calculations'][0]['calculation_id']}]")
                self.assertNotIn("missing_calculation_record", [item["code"] for item in scope_check["flags"]])
                for calc in provider.result["calculations"]:
                    source = chat.read_citation(initial["conversation_id"], answer["message_id"],
                                                "calculation", calc["calculation_id"])
                    self.assertEqual(source["calculation"], calc)
                    self.assertEqual(len(source["regression_outputs"]), 3)
                    self.assertEqual(len(source["input_sources"]), 6)
                    self.assertTrue(all(item["quote"] and item["source_sha256"] for item in source["input_sources"]))
                for calc in artifact["calculations"]:
                    trace = next(item for item in artifact["traces"] if item["reference"] == calc["reference"])
                    self.assertEqual([step["layer"] for step in trace["steps"]], ["L4", "L3", "L2", "L1"])
                other, _ = chat._persist_submission(chat.create_conversation("MSFT")["conversation_id"], None, "其他研究")
                with self.assertRaises(ToolError):
                    chat.decisions.regression_authorization(other["run_id"], provider.decision, "ols")
                with self.assertRaises(ToolError):
                    chat.decisions.regression_authorization(resumed["run_id"], provider.decision, "unselected")
                with closing(chat._connect()) as connection, connection:
                    row = connection.execute(
                        "SELECT seq,payload_json FROM events WHERE run_id=? AND type='tool_result' "
                        "AND json_extract(payload_json,'$.name')='calculate_regression'", (resumed["run_id"],)).fetchone()
                    changed = json.loads(row["payload_json"])
                    changed["result"]["calculations"][0]["value"] = "999"
                    connection.execute("UPDATE events SET payload_json=? WHERE seq=? AND run_id=?",
                                       (json.dumps(changed), row["seq"], resumed["run_id"]))
                with self.assertRaises(ChatError):
                    chat.read_citation(initial["conversation_id"], answer["message_id"], "calculation",
                                       provider.result["calculations"][0]["calculation_id"])
                with self.assertRaises(ChatError):
                    chat.read_citation(initial["conversation_id"], answer["message_id"], "calculation",
                                       provider.result["calculations"][1]["calculation_id"])
            finally:
                chat.close()

    def test_history_includes_authoritative_identity_without_trusting_provider_echoes(self):
        provider = RegressionProvider(real_plan(self.tools))
        with tempfile.TemporaryDirectory() as directory:
            chat = ChatService(self.db, Path(directory) / "chat.sqlite3", provider)
            try:
                initial = chat.create_and_submit("MSFT", "回归分析")
                wait(chat, initial["run_id"])
                with patch.object(chat, "_launch"):
                    resumed = chat.decisions.answer(provider.decision, ["ols"], "", "accepted", "history-identity")
                with closing(chat._connect()) as connection, connection:
                    chat._insert_event(connection, resumed["run_id"], "confirmed_decision",
                                       {"authoritative": False, "decision_id": "forged"})
                    history = chat._history(connection, initial["conversation_id"])
                text = history[-1]["content"]
                self.assertIn(provider.decision, text)
                self.assertNotIn("forged", text)
                self.assertEqual(text.count('"decision_id"'), 1)
            finally:
                chat.close()

    def test_document_inputs_survive_confirmation_without_repeating_extraction(self):
        for tampered in (False, True):
            with self.subTest(tampered=tampered), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                chat, provider, initial, version = self.document_research(root)
                documents = chat.document_service
                try:
                    original_read = documents.read_raw_version
                    def read(**kwargs):
                        raw = original_read(**kwargs)
                        return raw + b"changed" if tampered else raw
                    with patch.object(documents, "read_raw_version", side_effect=read):
                        resumed = chat.decisions.answer(provider.decision, ["ols"], "", "accepted", "document-ols")
                        run = wait(chat, resumed["run_id"], timeout=10)
                    if tampered:
                        self.assertEqual(run["status"], "failed")
                        self.assertTrue(any(e.get("name") == "calculate_regression"
                                            and e.get("result", {}).get("error") == "citation_source_mismatch"
                                            for e in run["events"]))
                        self.assertIsNone(provider.result)
                        continue
                    self.assertEqual(run["status"], "completed", run.get("error"))
                    self.assertFalse(any(e.get("name") == "extract_document_table_fact" for e in run["events"]))
                    self.assertEqual(len(provider.result["restored_document_facts"]), 6)
                    answer = chat.get_conversation(initial["conversation_id"])["messages"][-1]
                    artifact = AnswerArtifactService(chat).get(initial["conversation_id"], answer["message_id"])
                    self.assertEqual(len(artifact["calculations"]), 3, artifact["limitations"])
                    calc = provider.result["calculations"][0]
                    detail = chat.read_citation(initial["conversation_id"], answer["message_id"], "calculation",
                                                calc["calculation_id"])
                    self.assertEqual(len(detail["input_sources"]), 6)
                    self.assertTrue(all(item.get("document_version_id") == version["document_version_id"]
                                        for item in detail["input_sources"]))
                    self.assertTrue(all(item.get("reused_from", {}).get("run_id") == initial["run_id"]
                                        for item in detail["input_sources"]))
                    refs = {"claim": {}, "evidence": {}, "passage": {}, "calculation": {}}
                    ChatService._record_calculation_ref(provider.result, refs)
                    from workbench.answer_review import AnswerReview
                    reviewer = AnswerReview(chat, resumed["run_id"], "MSFT", "回归", refs)
                    self.assertEqual(len(reviewer.calculations), 3)
                finally:
                    chat.close()

    def test_registered_inputs_resume_after_restart_without_loading_original_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chat, provider, initial, version = self.document_research(root)
            documents = chat.document_service
            public_files = [self.db, documents.db_path, documents.objects_path / version["source_sha256"]]
            digests = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in public_files}
            chat.close()
            chat = ChatService(self.db, root / "chat.sqlite3", provider, document_service=documents)
            self.addCleanup(chat.close)
            get_run = chat.get_run
            def guarded(run_id):
                if run_id == initial["run_id"]:
                    raise AssertionError("continuation loaded the original event stream")
                return get_run(run_id)
            with patch.object(chat, "get_run", side_effect=guarded):
                resumed = chat.decisions.answer(provider.decision, ["ols"], "", "accepted", "restart")
                self.assertEqual(wait(chat, resumed["run_id"])["status"], "completed")
            snapshot = chat.effective_snapshot(resumed["run_id"], "MSFT")
            research = ResearchTools(self.db, "MSFT", documents, frozen_snapshot=snapshot)
            with patch.object(chat, "_document_fact_derivation", side_effect=AssertionError("legacy lookup")):
                result = chat.decisions.regression_definition(resumed["run_id"], research)["handler"](
                    decision_id=provider.decision, option_id="ols")
            self.assertEqual(len(result["restored_document_facts"]), 6)
            for identifier in provider.plan["x_fact_ids"] + provider.plan["y_fact_ids"]:
                self.assertEqual(chat.inputs.read(resumed["run_id"], identifier)["reused_from"]["run_id"],
                                 initial["run_id"])
            self.assertEqual(digests, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in public_files})
            chat.close()

    def test_missing_registered_input_fails_without_event_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            chat, provider, initial, _ = self.document_research(Path(directory))
            with closing(chat._connect()) as connection, connection:
                connection.execute("DROP TRIGGER research_input_uses_no_delete")
                connection.execute("DELETE FROM research_input_uses WHERE run_id=?", (initial["run_id"],))
            with patch.object(chat, "_document_fact_derivation", side_effect=AssertionError("legacy lookup")):
                resumed = chat.decisions.answer(provider.decision, ["ols"], "", "accepted", "missing-input")
                run = wait(chat, resumed["run_id"])
            self.assertEqual(run["status"], "failed")
            self.assertTrue(any(event.get("result", {}).get("error") == "research_input_missing"
                                for event in run["events"]))
            self.assertIsNone(provider.result)
            chat.close()

    def test_legacy_confirmation_migrates_only_used_inputs_without_rewriting_history(self):
        with tempfile.TemporaryDirectory() as directory:
            # Absence of a contract is the explicit old-writer compatibility case.
            with patch("workbench.research_inputs.ResearchInputStore.begin_run"):
                chat, provider, initial, _ = self.document_research(Path(directory))
            events = chat.get_run(initial["run_id"])["events"]
            self.assertFalse(chat.inputs.has_contract(initial["run_id"]))
            resumed = chat.decisions.answer(provider.decision, ["ols"], "", "accepted", "legacy")
            self.assertEqual(wait(chat, resumed["run_id"])["status"], "completed")
            self.assertTrue(chat.inputs.has_contract(resumed["run_id"]))
            self.assertFalse(chat.inputs.has_contract(initial["run_id"]))
            self.assertEqual(chat.get_run(initial["run_id"])["events"], events)
            for identifier in provider.plan["x_fact_ids"]:
                self.assertEqual(chat.inputs.read(resumed["run_id"], identifier)["reused_from"]["run_id"],
                                 initial["run_id"])
            message = chat.get_conversation(initial["conversation_id"])["messages"][-1]
            detail = chat.read_citation(initial["conversation_id"], message["message_id"], "calculation",
                                        provider.result["calculations"][0]["calculation_id"])
            self.assertEqual(len(detail["input_sources"]), 6)
            chat.close()

    def test_authoritative_event_and_registry_commit_or_rollback_together(self):
        with tempfile.TemporaryDirectory() as directory:
            chat, provider, initial, _ = self.document_research(Path(directory))
            result = chat.inputs.read(initial["run_id"], provider.plan["x_fact_ids"][0])
            with patch.object(chat, "_launch"):
                resumed = chat.decisions.answer(provider.decision, ["ols"], "", "accepted", "atomic")
            payload = {"authoritative": True, "name": "extract_document_table_fact", "result": result}
            with patch.object(chat, "_insert_event", side_effect=RuntimeError("injected event failure")):
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    chat._event(resumed["run_id"], "tool_result", payload)
            from workbench.research_inputs import InputError
            with self.assertRaises(InputError):
                chat.inputs.read(resumed["run_id"], result["fact"]["fact_id"])
            chat._event(resumed["run_id"], "tool_result", payload)
            self.assertEqual(chat.inputs.read(resumed["run_id"], result["fact"]["fact_id"])["fact"], result["fact"])
            with closing(chat._connect()) as connection:
                count = connection.execute(
                    "SELECT count(*) FROM events WHERE run_id=? AND type='tool_result'",
                    (resumed["run_id"],)).fetchone()[0]
            self.assertEqual(count, 1)
            chat.close()

    def test_repeated_extraction_and_second_confirmation_keep_original_source(self):
        with tempfile.TemporaryDirectory() as directory:
            chat, provider, initial, _ = self.document_research(
                Path(directory), repeat_extraction=True, reconfirm=True)
            try:
                resumed = chat.decisions.answer(provider.decision, ["ols"], "", "accepted", "first")
                middle = wait(chat, resumed["run_id"], timeout=10)
                self.assertEqual(middle["status"], "blocked")
                for fact_id in provider.plan["x_fact_ids"]:
                    result = chat._document_fact_derivation(middle["events"], fact_id=fact_id)
                    self.assertIsNotNone(result)
                    self.assertEqual(result["reused_from"]["run_id"], initial["run_id"])
                final = chat.decisions.answer(provider.decision, ["ols"], "", "accepted", "second")
                self.assertEqual(wait(chat, final["run_id"], timeout=10)["status"], "completed")
                answer = chat.get_conversation(initial["conversation_id"])["messages"][-1]
                artifact = AnswerArtifactService(chat).get(initial["conversation_id"], answer["message_id"])
                self.assertEqual(len(artifact["calculations"]), 3, artifact["limitations"])
                self.assertFalse({item["code"] for item in artifact["review_flags"]} &
                                 {"calculation_output_mismatch", "missing_calculation_record"})
                for calculation in provider.result["calculations"]:
                    detail = chat.read_citation(initial["conversation_id"], answer["message_id"], "calculation",
                                                calculation["calculation_id"])
                    self.assertEqual(len(detail["input_sources"]), 6)
                    self.assertTrue(all(source["reused_from"]["run_id"] == initial["run_id"]
                                        for source in detail["input_sources"]))
            finally:
                chat.close()

    def test_document_derivation_deduplication_never_hides_conflicting_proof(self):
        original = {"fact": {"fact_id": "docfact-test", "evidence_id": "doccell-test"}, "interpretation": "fixture"}
        reused = {**original, "reused_from": {"run_id": "original", "fact_id": "docfact-test"}}
        def event(result, name="extract_document_table_fact"):
            return {"authoritative": True, "name": name,
                    "result": result if name == "extract_document_table_fact" else {"restored_document_facts": [result]}}
        payloads = [event({**original, "_tool_budget": {"remaining": 10}}),
                    event({**original, "_tool_budget": {"remaining": 9}}),
                    event(reused, "calculate_regression")]
        self.assertEqual(ChatService._document_fact_derivation(payloads, fact_id="docfact-test"), reused)
        for conflict in ({**original, "interpretation": "changed"},
                         {**reused, "reused_from": {"run_id": "other", "fact_id": "docfact-test"}}):
            with self.subTest(conflict=conflict):
                self.assertIsNone(ChatService._document_fact_derivation(
                    payloads + [event(conflict, "calculate_regression" if "reused_from" in conflict
                                      else "extract_document_table_fact")], fact_id="docfact-test"))

    def test_registered_reuse_rejects_wrong_owner_and_uses_registry_not_original_events(self):
        with tempfile.TemporaryDirectory() as directory:
            chat, provider, initial, _ = self.document_research(Path(directory))
            try:
                resumed = chat.decisions.answer(provider.decision, ["ols"], "", "accepted", "original")
                self.assertEqual(wait(chat, resumed["run_id"], timeout=10)["status"], "completed")
                message = chat.get_conversation(initial["conversation_id"])["messages"][-1]
                answer = chat.answer_context(initial["conversation_id"], message["message_id"])
                proof = provider.result["restored_document_facts"][0]
                with patch.object(chat, "_launch"):
                    other = chat.create_and_submit("MSFT", "另一个独立会话")
                for run_id in (resumed["run_id"], other["run_id"], "missing-run"):
                    invalid = deepcopy(proof)
                    invalid["reused_from"]["run_id"] = run_id
                    with self.subTest(run_id=run_id), self.assertRaises(ChatError) as caught:
                        chat._validated_document_fact(answer, invalid)
                    self.assertEqual(caught.exception.code, "citation_source_mismatch")
                with closing(chat._connect()) as connection, connection:
                    row = connection.execute(
                        "SELECT seq,payload_json FROM events WHERE run_id=? AND type='tool_result' "
                        "AND json_extract(payload_json,'$.result.fact.fact_id')=?",
                        (initial["run_id"], proof["fact"]["fact_id"])).fetchone()
                    changed = json.loads(row["payload_json"])
                    changed["result"]["historical_provenance"]["source"]["title"] = "changed origin"
                    connection.execute("UPDATE events SET payload_json=? WHERE run_id=? AND seq=?",
                                       (json.dumps(changed), initial["run_id"], row["seq"]))
                for calc in provider.result["calculations"]:
                    detail = chat.read_citation(initial["conversation_id"], message["message_id"],
                                                "calculation", calc["calculation_id"])
                    self.assertEqual(len(detail["input_sources"]), 6)
                original_context = {**answer, "run_id": initial["run_id"],
                                    "source_snapshot": chat.effective_snapshot(initial["run_id"], "MSFT")}
                with self.assertRaises(ChatError):
                    chat._validated_document_fact(original_context, changed["result"])
                with closing(chat._connect()) as connection, connection:
                    connection.execute("DROP TRIGGER research_inputs_no_update")
                    connection.execute(
                        "UPDATE research_inputs SET payload_sha256='changed' WHERE input_id=?",
                        (proof["fact"]["fact_id"],))
                with self.assertRaises(ChatError) as caught:
                    chat.read_citation(initial["conversation_id"], message["message_id"],
                                       "calculation", provider.result["calculations"][0]["calculation_id"])
                self.assertEqual(caught.exception.code, "citation_source_mismatch")
            finally:
                chat.close()

    def test_cached_document_reuse_revalidates_bytes_and_failed_restore_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            chat, provider, initial, _ = self.document_research(Path(directory))
            try:
                resumed = chat.decisions.answer(provider.decision, ["ols"], "", "accepted", "cached")
                self.assertEqual(wait(chat, resumed["run_id"], timeout=10)["status"], "completed")
                snapshot = chat.effective_snapshot(resumed["run_id"], "MSFT")
                research = ResearchTools(self.db, "MSFT", chat.document_service, frozen_snapshot=snapshot)
                handler = chat.decisions.regression_definition(resumed["run_id"], research)["handler"]
                before = deepcopy(research._facts)
                original_read = chat.document_service.read_raw_version
                def changed(**kwargs):
                    return original_read(**kwargs) + b"changed"
                with patch.object(chat.document_service, "read_raw_version", side_effect=changed):
                    with self.assertRaises(ToolError) as caught:
                        handler(decision_id=provider.decision, option_id="ols")
                self.assertEqual(caught.exception.code, "citation_source_mismatch")
                self.assertEqual(research._facts, before)
                result = handler(decision_id=provider.decision, option_id="ols")
                self.assertEqual(len(result["restored_document_facts"]), 6)
                self.assertEqual(handler(decision_id=provider.decision, option_id="ols"), result)
                with patch.object(chat.document_service, "read_raw_version", side_effect=changed):
                    with self.assertRaises(ToolError) as caught:
                        handler(decision_id=provider.decision, option_id="ols")
                self.assertEqual(caught.exception.code, "citation_source_mismatch")
            finally:
                chat.close()

    def test_free_text_notes_require_a_revised_confirmed_plan(self):
        provider = RegressionProvider(real_plan(self.tools))
        with tempfile.TemporaryDirectory() as directory:
            chat = ChatService(self.db, Path(directory) / "chat.sqlite3", provider)
            try:
                initial = chat.create_and_submit("MSFT", "回归分析")
                wait(chat, initial["run_id"])
                with patch.object(chat, "_launch"):
                    resumed = chat.decisions.answer(provider.decision, ["ols"], "请去掉截距", "accepted", "change-method")
                with self.assertRaises(ToolError) as caught:
                    chat.decisions.regression_definition(resumed["run_id"], self.tools)["handler"](
                        decision_id=provider.decision, option_id="ols")
                self.assertEqual(caught.exception.code, "regression_reconfirmation_required")
            finally:
                chat.close()
