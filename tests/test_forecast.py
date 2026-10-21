from contextlib import closing
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from tests.test_recorded_calculations import CalculationProvider, wait
from workbench import forecast
from workbench.answer_artifacts import AnswerArtifactService, _calculation_output_matches
from workbench.build import build_database
from workbench.chat import ChatError, ChatService
from workbench.documents import DocumentService
from workbench.research_tools import ResearchTools, ToolError


REFERENCE = "2026-09-29"


def plan(cfo="cfo", capex="capex"):
    return {"method": "annual_cash_flow_growth", "operating_cash_flow_fact_id": cfo,
            "capital_expenditures_fact_id": capex, "capex_sign": "negative_cash_outflow",
            "periods": [
                {"period_start": "2025-07-01", "period_end": "2026-06-30",
                 "cfo_growth_percent": "10", "capex_growth_percent": "20"},
                {"period_start": "2026-07-01", "period_end": "2027-06-30",
                 "cfo_growth_percent": "10", "capex_growth_percent": "0"},
            ]}


class ForecastProvider(CalculationProvider):
    """Protocol substitute: fixture amounts are not financial acceptance evidence."""
    def __init__(self, version_id):
        super().__init__()
        self.version_id = version_id
        self.result = None
        self.decision = None

    def stream(self, messages, tools, cancel_event):
        handlers = {tool["name"]: tool["handler"] for tool in tools}
        if self.decision is None:
            ids = []
            for row, metric, value in ((1, "operating_cash_flow", "100"), (2, "capital_expenditures", "-40")):
                result = handlers["extract_document_table_fact"](
                    document_version_id=self.version_id, table_index=0, row_index=row, column_index=1,
                    expected_value=value, metric=metric, unit="USD millions", basis="synthetic annual cash flow",
                    period_kind="year", period_start="2024-07-01", period_end="2025-06-30",
                    header_row_indices=[0], row_label_column_index=0, metadata_mode="explicit_interpretation")
                ids.append(result["fact"]["fact_id"])
            self.decision = handlers["request_confirmation"](
                kind="model", question="确认现金流情景的基线与逐年假设？",
                explanation="先估计已结束的桥接年度，再计算下一年度条件情景；均不是实绩。",
                options=[{"id": "cash-flow", "label": "分别增长",
                          "benefit": "可复算每期现金流压力", "limitation": "增长率是人为假设，非管理层指引",
                          "forecast": plan(*ids)}], multiple=False)["decision_id"]
            return
        marker = "(context, not source evidence):\n"
        contexts = [json.loads(message["content"].split(marker, 1)[1])
                    for message in messages if marker in message["content"]]
        decision_id = contexts[-1][-1]["decision_id"]
        self.result = handlers["calculate_forecast"](decision_id=decision_id, option_id="cash-flow")
        text = []
        for entry in self.result["report_values"]:
            period = entry["period"]
            label = "桥接估计" if period["classification"] == "bridge_estimate" else "条件预测"
            text.append(f"{period['period_start']} 至 {period['period_end']} {label}，"
                        f"{entry['label']}为 {entry['value_text']} {entry['citation']}。")
        yield {"type": "delta", "text": "\n\n".join(text)}


def register_fixture(documents, root):
    source = root / "cash-flow-fixture.html"
    source.write_text(
        "<!doctype html><html><body><table><tr><th>USD millions</th><th>FY2025</th></tr>"
        "<tr><td>Net cash from operations</td><td>100</td></tr>"
        "<tr><td>Additions to property and equipment</td><td>(40)</td></tr></table></body></html>")
    return documents.register_file(source, company_id="MSFT", source_key="forecast-protocol-fixture",
                                   title="Synthetic cash-flow protocol fixture")


class ForecastTests(unittest.TestCase):
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
        base = next(iter(self.tools._facts.values()))
        for key, metric, value in (("cfo", "operating_cash_flow", "100"), ("capex", "capital_expenditures", "-40")):
            self.tools._facts[key] = {
                **base, "fact_id": key, "metric": metric, "value": value, "unit": "USD millions",
                "basis": "synthetic", "period_kind": "year", "period_start": "2024-07-01", "period_end": "2025-06-30"}
        self.authorization = {"decision_id": "decision", "option_id": "cash-flow", "source_run_id": "run"}

    def calculate(self, selected=None):
        return forecast.calculate(self.tools, selected or plan(), self.authorization, as_of_date=REFERENCE)

    def test_exact_recursion_sign_conversion_and_explicit_bridge(self):
        result = self.calculate()
        self.assertEqual([item["value"] for item in result["calculations"]],
                         ["110.000000", "48.000000", "62.000000", "121.000000", "48.000000", "73.000000"])
        self.assertEqual([period["classification"] for period in result["model"]["periods"]],
                         ["bridge_estimate", "conditional_forecast"])
        self.assertEqual(result, self.calculate())
        records = result["calculations"]
        self.assertEqual(records[2]["forecast"]["depends_on_calculation_ids"],
                         [records[0]["calculation_id"], records[1]["calculation_id"]])
        self.assertEqual(records[3]["forecast"]["depends_on_calculation_ids"], [records[0]["calculation_id"]])
        self.assertEqual(self.tools._facts["capex"]["value"], "-40")
        self.assertFalse(any(key.startswith("calc-") for key in self.tools._facts))

    def test_precision_does_not_feed_rounded_intermediates_back_into_model(self):
        self.tools._facts["cfo"]["value"] = "1.00000049"
        chosen = plan()
        for period in chosen["periods"]:
            period["cfo_growth_percent"] = "1.123456789"
        result = self.calculate(chosen)
        with localcontext() as context:
            context.prec = 80
            value = Decimal("1.00000049")
            gold = []
            for _ in chosen["periods"]:
                value *= 1 + Decimal("1.123456789") / 100
                gold.append(format(value.quantize(Decimal(".000001"), rounding="ROUND_HALF_UP"), "f"))
        self.assertEqual([step["cfo"] for step in result["steps"]], gold)
        self.tools._facts["capex"]["value"] = "40"
        chosen["capex_sign"] = "positive_payment_amount"
        positive = self.calculate(chosen)
        self.assertEqual([{key: value for key, value in step.items() if key != "calculation_ids"}
                          for step in positive["steps"]],
                         [{key: value for key, value in step.items() if key != "calculation_ids"}
                          for step in result["steps"]])
        self.assertNotEqual(positive["calculations"][0]["calculation_id"],
                            result["calculations"][0]["calculation_id"])

    def test_invalid_assumptions_and_incompatible_baselines_are_rejected(self):
        variants = [
            {**plan(), "capex_sign": "guess"},
            {**plan(), "periods": []},
            {**plan(), "periods": plan()["periods"][1:]},
            {**plan(), "periods": [dict(plan()["periods"][0], cfo_growth_percent=0.1)]},
            {**plan(), "periods": [dict(plan()["periods"][0], capex_growth_percent="-100.01")]},
            {**plan(), "periods": [dict(plan()["periods"][0], cfo_growth_percent="NaN")]},
            {**plan(), "periods": [dict(plan()["periods"][0], period_start="2025-07-02")]},
            {**plan(), "periods": [dict(plan()["periods"][0], period_end="2026-09-30")]},
            {**plan(), "periods": [dict(plan()["periods"][0], period_end="2026-06-15")]},
        ]
        for selected in variants:
            with self.subTest(selected=selected), self.assertRaises(ToolError):
                self.calculate(selected)
        for days in (350, 363, 364, 365, 366, 367, 370, 371, 372, 378):
            selected = plan()
            selected["periods"] = [dict(selected["periods"][0],
                                       period_end=(date(2025, 7, 1) + timedelta(days=days - 1)).isoformat())]
            with self.subTest(annual_days=days):
                if days in {364, 365, 366, 371}:
                    self.assertEqual(len(self.calculate(selected)["calculations"]), 3)
                else:
                    with self.assertRaises(ToolError):
                        self.calculate(selected)
        for key, value in (("company_id", "AAPL"), ("metric", "revenue"), ("unit", "widgets"),
                           ("period_kind", "quarter"), ("period_end", "2025-07-01"),
                           ("basis", "other"), ("value", "40"), ("segment", "cloud")):
            original = deepcopy(self.tools._facts["capex"])
            self.tools._facts["capex"][key] = value
            with self.subTest(key=key), self.assertRaises(ToolError):
                self.calculate()
            self.tools._facts["capex"] = original
        actual = {**self.tools._facts["cfo"], "fact_id": "new-actual",
                  "period_start": "2025-07-01", "period_end": "2026-06-30"}
        self.tools._facts["new-actual"] = actual
        with self.assertRaises(ToolError) as caught:
            self.calculate()
        self.assertEqual(caught.exception.code, "forecast_actual_available")

    def test_compact_sdk_view_retains_every_period_value_and_reference(self):
        result = self.calculate()
        original = deepcopy(result)
        view = forecast.model_result(result)
        self.assertEqual(result, original)
        self.assertEqual(view["report_values"], result["report_values"])
        self.assertEqual([item["citation"] for item in view["baseline_sources"]],
                         [f"[evidence:{fact['evidence_id']}]" for fact in result["calculations"][0]["inputs"]])
        self.assertEqual(view["baseline_sources"][1]["value"], "-40")
        self.assertLess(len(json.dumps(view)), 12_000)
        self.assertNotIn("calculations", view)
        self.assertEqual(forecast.model_result({"error": "denied"}), {"error": "denied"})
        longest = plan()
        longest["periods"] = [
            {"period_start": f"{2025 + index}-07-01", "period_end": f"{2026 + index}-06-30",
             "cfo_growth_percent": "10", "capex_growth_percent": "20"} for index in range(10)]
        maximum = self.calculate(longest)
        self.assertEqual(len(maximum["calculations"]), 30)
        self.assertEqual(len(forecast.model_result(maximum)["report_values"]), 30)
        self.assertLess(len(json.dumps(maximum)), 1_000_000)
        self.assertLess(len(json.dumps(forecast.model_result(maximum))), 16_000)

    def test_binding_requires_exact_period_metric_and_conditional_status(self):
        calc = self.calculate()["calculations"][2]
        reference = f"[calculation:{calc['calculation_id']}]"
        text = f"2025-07-01 至 2026-06-30 桥接估计，自由现金流62百万美元 {reference}"
        self.assertTrue(_calculation_output_matches(text, calc))
        for wrong in (text.replace("62", "63", 1), text.replace("桥接估计", "实际"),
                      text.replace("2026-06-30", "2027-06-30"), text.replace("自由现金流", "经营现金流"),
                      text.replace("百万美元", "十亿美元"), text.replace("62", "-62", 1)):
            with self.subTest(wrong=wrong):
                self.assertFalse(_calculation_output_matches(wrong, calc))
        flags = AnswerArtifactService(None)._findings(
            text.replace("桥接估计", "实际"), {("calculation", calc["calculation_id"]): {"valid": True}},
            {}, {}, [{**calc, "status": "recorded"}])[0]["review_flags"]
        self.assertIn("forecast_output_context_mismatch", {item["code"] for item in flags})

    def test_comparison_table_binding_uses_own_metric_column_and_period_row(self):
        records = self.calculate()["calculations"]
        header = "| 期间与性质 | 经营现金流 | 资本开支支付金额 | 自由现金流 |\n|---|---:|---:|---:|"
        rows = []
        for index in range(2):
            period = records[index * 3]["forecast"]["period"]
            nature = "桥接估计" if index == 0 else "条件预测"
            cells = [f"{period['period_start']} 至 {period['period_end']} {nature}"]
            cells += [f"**{item['value']} USD millions** [calculation:{item['calculation_id']}]"
                      for item in records[index * 3:index * 3 + 3]]
            rows.append("| " + " | ".join(cells) + " |")
        text = "\n".join([header] + rows)
        for record in records:
            self.assertTrue(_calculation_output_matches(text, record))
        for wrong in (text.replace("桥接估计", "已披露实绩"),
                      text.replace("2026-06-30", "2028-06-30"),
                      text.replace("经营现金流 |", "营业利润 |"),
                      text.replace("110.000000", "111.000000"),
                      text.replace("110.000000 USD millions", "110.000000 CNY millions"),
                      text.replace(records[0]["calculation_id"], records[2]["calculation_id"])):
            with self.subTest(wrong=wrong):
                self.assertFalse(_calculation_output_matches(wrong, records[0]))
        swapped = text.replace(records[1]["calculation_id"], "temporary").replace(
            records[4]["calculation_id"], records[1]["calculation_id"]).replace("temporary", records[4]["calculation_id"])
        self.assertFalse(_calculation_output_matches(swapped, records[1]))
        self.assertFalse(_calculation_output_matches(swapped, records[4]))

    def test_worker_confirmation_restoration_review_and_full_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = DocumentService(root / "documents")
            version = register_fixture(documents, root)
            provider = ForecastProvider(version["document_version_id"])
            chat = ChatService(self.db, root / "chat.sqlite3", provider, document_service=documents)
            try:
                with patch("workbench.research_decisions._now", return_value=REFERENCE + "T00:00:00Z"):
                    first = chat.create_and_submit("MSFT", "经营现金流情景")
                    self.assertEqual(wait(chat, first["run_id"])["status"], "blocked")
                self.assertIsNone(provider.result)
                decision = chat.decisions.for_run(first["run_id"])
                self.assertEqual(decision["options"][0]["forecast_details"]["baseline"]["capital_expenditures"]["value"], "-40")
                with self.assertRaises(ToolError):
                    chat.decisions.model_authorization("forecast", first["run_id"], provider.decision, "cash-flow")
                resumed = chat.decisions.answer(provider.decision, ["cash-flow"], "", "accepted", "forecast")
                run = wait(chat, resumed["run_id"], timeout=10)
                self.assertEqual(run["status"], "completed", run)
                self.assertFalse(any(e.get("name") == "extract_document_table_fact" for e in run["events"]))
                message = chat.get_conversation(first["conversation_id"])["messages"][-1]
                artifact = AnswerArtifactService(chat).get(first["conversation_id"], message["message_id"])
                self.assertEqual(len(artifact["calculations"]), 6, artifact["limitations"])
                self.assertFalse(artifact["review_flags"], artifact["review_flags"])
                from workbench.answer_review import AnswerReview
                refs = {"claim": {}, "evidence": {}, "passage": {}, "calculation": {}}
                ChatService._record_refs(provider.result, refs)
                ChatService._record_calculation_ref(provider.result, refs)
                reviewer = AnswerReview(chat, resumed["run_id"], "MSFT", "现金流情景", refs)
                fact = provider.result["calculations"][0]["inputs"][0]
                draft = f"实际经营现金流基线为100百万美元 [fact:{fact['fact_id']}]"
                replacement = draft.replace(f"[fact:{fact['fact_id']}]", f"[evidence:{fact['evidence_id']}]")
                original_check, fixed_check = reviewer.check(draft), reviewer.check(replacement)
                self.assertIn("invalid_reference", {flag["code"] for flag in original_check["flags"]})
                self.assertEqual(original_check["body_signature"], fixed_check["body_signature"])
                self.assertEqual(original_check["numeric_token_count"], fixed_check["numeric_token_count"])
                self.assertFalse(fixed_check["flags"], fixed_check["flags"])
                altered = reviewer.check(replacement.replace("100", "999"))
                self.assertNotEqual(original_check["body_signature"], altered["body_signature"])
                with patch.object(chat, "_launch"):
                    unrelated = chat.create_and_submit("MSFT", "独立研究")
                with self.assertRaises(ToolError) as caught:
                    chat.decisions.model_authorization("forecast", unrelated["run_id"], provider.decision, "cash-flow")
                self.assertEqual(caught.exception.code, "forecast_confirmation_required")
                for record in provider.result["calculations"]:
                    with patch("workbench.research_decisions._now", return_value="2030-01-01T00:00:00Z"):
                        detail = chat.read_citation(first["conversation_id"], message["message_id"], "calculation",
                                                    record["calculation_id"])
                    self.assertEqual(len(detail["forecast_steps"]), 2)
                    self.assertEqual(detail["forecast_model"]["as_of_date"], REFERENCE)
                    self.assertEqual(len(detail["input_sources"]), 2)
                    self.assertEqual(detail["input_sources"][0]["reused_from"]["run_id"], first["run_id"])
                with closing(chat._connect()) as connection, connection:
                    event = connection.execute(
                        "SELECT seq,payload_json FROM events WHERE run_id=? AND type='tool_result' "
                        "AND json_extract(payload_json,'$.name')='calculate_forecast' "
                        "AND json_extract(payload_json,'$.authoritative')=1", (resumed["run_id"],)).fetchone()
                    changed = json.loads(event["payload_json"])
                    changed["result"]["calculations"][5]["forecast"]["depends_on_calculation_ids"] = []
                    connection.execute("UPDATE events SET payload_json=? WHERE run_id=? AND seq=?",
                                       (json.dumps(changed), resumed["run_id"], event["seq"]))
                with self.assertRaises(ChatError):
                    chat.read_citation(first["conversation_id"], message["message_id"], "calculation",
                                       provider.result["calculations"][0]["calculation_id"])
            finally:
                chat.close()

    def test_user_added_requirements_cannot_be_guessed_into_numeric_parameters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = DocumentService(root / "documents")
            provider = ForecastProvider(register_fixture(documents, root)["document_version_id"])
            chat = ChatService(self.db, root / "chat.sqlite3", provider, document_service=documents)
            try:
                with patch("workbench.research_decisions._now", return_value=REFERENCE + "T00:00:00Z"):
                    first = chat.create_and_submit("MSFT", "现金流情景")
                    self.assertEqual(wait(chat, first["run_id"])["status"], "blocked")
                with patch.object(chat, "_launch"):
                    resumed = chat.decisions.answer(provider.decision, ["cash-flow"], "资本开支再高一些", "accepted", "revise")
                with self.assertRaises(ToolError) as caught:
                    chat.decisions.model_definition("forecast", resumed["run_id"], self.tools)["handler"](
                        decision_id=provider.decision, option_id="cash-flow")
                self.assertEqual(caught.exception.code, "forecast_reconfirmation_required")
            finally:
                chat.close()
