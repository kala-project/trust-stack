from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from workbench.arithmetic import MAX_STEPS
from workbench.build import build_database
from workbench.research_tools import ResearchTools, ToolError


class ArithmeticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.db = Path(cls.fixture.name) / "evidence.sqlite3"
        build_database(db_path=cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def tools(self, company="MSFT", values=("1", "2", "3"), metrics=("staff_cost", "lease_cost", "service_cost")):
        tools = ResearchTools(self.db, company)
        template = next(iter(tools._facts.values()))
        tools._facts = {f"synthetic-{index}": {
            **template, "fact_id": f"synthetic-{index}", "metric": metric, "value": value,
            "basis": "explicit synthetic fixture", "unit": "USD millions",
            "period_start": "2025-01-01", "period_end": "2025-12-31", "period_kind": "year",
        } for index, (value, metric) in enumerate(zip(values, metrics))}
        return tools

    def test_unseen_metrics_and_companies_need_no_whitelist(self):
        for company in ("MSFT", "AAPL"):
            for metrics in (("staff_cost", "lease_cost", "service_cost"), ("east", "west", "other")):
                with self.subTest(company=company, metrics=metrics):
                    tools = self.tools(company, metrics=metrics)
                    result = tools.calculate_facts(operation="sum", fact_ids=list(tools._facts))
                    self.assertEqual(result["value"], "6")
                    result = tools.calculate_facts(operation="difference", fact_ids=["synthetic-2", "synthetic-0"])
                    self.assertEqual(result["value"], "2")

    def test_repeating_fractions_compose_without_rounding_loss(self):
        tools = self.tools()
        first = tools.calculate_facts(operation="ratio", fact_ids=["synthetic-0", "synthetic-2"], decimal_places=2)
        second = tools.calculate_facts(operation="ratio", fact_ids=["synthetic-1", "synthetic-2"], decimal_places=2)
        self.assertEqual((first["value"], second["value"]), ("0.33", "0.67"))
        result = tools.calculate_facts(operation="sum", fact_ids=[first["calculation_id"], second["calculation_id"]])
        self.assertEqual(result["value"], "1")
        self.assertEqual(result["arithmetic"]["exact_value"], {"numerator": "1", "denominator": "1"})
        self.assertEqual(len(result["parameters"]["dependencies"]), 2)
        self.assertEqual(len(result["inputs"]), 3)
        result_again = tools.calculate_facts(operation="sum", fact_ids=[first["calculation_id"]] * 3)
        self.assertEqual(result_again["value"], "1")
        self.assertEqual(len(result_again["inputs"]), 2)
        self.assertEqual(tools.replay_arithmetic(result_again), result_again)
        tools._calculations.clear()
        self.assertEqual(tools.replay_arithmetic(result), result)
        self.assertEqual(tools._calculations, {})

    def test_finite_precision_and_signed_half_rounding(self):
        tiny = "0." + "0" * 150 + "1"
        tools = self.tools(values=(tiny, "0", "3"))
        exact = tools.calculate_facts(operation="sum", fact_ids=["synthetic-0", "synthetic-1"])
        self.assertEqual(exact["value"], tiny)
        self.assertFalse(exact["arithmetic"]["rounding"]["applied"])
        tools = self.tools(values=("-1", "8", "3"))
        ratio = tools.calculate_facts(operation="ratio", fact_ids=["synthetic-0", "synthetic-1"], decimal_places=0)
        self.assertEqual(ratio["value"], "-0.125")
        ratio = tools.calculate_facts(operation="ratio", fact_ids=["synthetic-0", "synthetic-2"], decimal_places=2)
        self.assertEqual(ratio["value"], "-0.33")

    def test_scope_units_dates_zero_and_arity_remain_enforced(self):
        for key, value in (("unit", "CNY"), ("basis", "other"), ("segment", "other"),
                           ("company_id", "AAPL"), ("period_end", "2026-12-31"), ("value", "NaN")):
            with self.subTest(key=key):
                tools = self.tools()
                tools._facts["synthetic-1"][key] = value
                with self.assertRaises(ToolError):
                    tools.calculate_facts(operation="sum", fact_ids=["synthetic-0", "synthetic-1"])
        tools = self.tools(values=("1", "0", "3"))
        for arguments in (
            {"operation": "ratio", "fact_ids": ["synthetic-0", "synthetic-1"]},
            {"operation": "ratio", "fact_ids": list(tools._facts)},
            {"operation": "sum", "fact_ids": ["synthetic-0"] * 51},
            {"operation": "sum", "fact_ids": ["synthetic-0", "missing"]},
            {"operation": "sum", "fact_ids": ["synthetic-0", "synthetic-1"], "decimal_places": True},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ToolError):
                tools.calculate_facts(**arguments)

    def test_dependency_tampering_and_resource_limit(self):
        tools = self.tools()
        prior = tools.calculate_facts(operation="sum", fact_ids=["synthetic-0", "synthetic-1"])
        result = tools.calculate_facts(operation="ratio", fact_ids=["synthetic-0", prior["calculation_id"]])
        changed = deepcopy(result)
        changed["parameters"]["dependencies"][0]["operation"] = "difference"
        with self.assertRaises(ToolError):
            tools.replay_arithmetic(changed)
        changed = deepcopy(result)
        changed["parameters"]["execution_version"] = "unknown"
        with self.assertRaises(ToolError):
            tools.replay_arithmetic(changed)
        for _ in range(MAX_STEPS - 1):
            prior = tools.calculate_facts(operation="sum", fact_ids=[prior["calculation_id"], "synthetic-0"])
        before = deepcopy(tools._calculations)
        with self.assertRaises(ToolError) as caught:
            tools.calculate_facts(operation="sum", fact_ids=[prior["calculation_id"], "synthetic-0"])
        self.assertEqual(caught.exception.code, "calculation_size_limit")
        self.assertEqual(tools._calculations, before)

    def test_old_records_replay_with_their_original_algorithm(self):
        tools = self.tools(metrics=("payments_of_dividends", "payments_for_repurchase_of_common_stock", "other"))
        old = tools._legacy_calculate_facts(operation="sum", fact_ids=["synthetic-0", "synthetic-1"])
        new = tools.calculate_facts(operation="sum", fact_ids=["synthetic-0", "synthetic-1"])
        self.assertEqual(old["value"], new["value"])
        self.assertNotEqual(old["calculation_id"], new["calculation_id"])
        self.assertEqual(tools.replay_arithmetic(old), old)
