from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from workbench.build import build_database
from workbench.research_tools import ResearchTools, ToolError
from workbench.scoped_tools import ScopedResearchTools, snapshot_for, tools_for_scope


class ScopedToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.temp.name) / "evidence.sqlite3"
        build_database(db_path=cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def tools(self):
        return tools_for_scope(self.db, "MSFT", ["MSFT", "AAPL"])

    def test_each_data_tool_requires_explicit_authorized_subject(self):
        research = self.tools()
        self.assertIsInstance(research, ScopedResearchTools)
        for tool in research.definitions():
            if tool["name"] == "calculate_facts":
                continue
            self.assertIn("company_id", tool["parameters"]["required"])
            self.assertEqual(tool["parameters"]["properties"]["company_id"]["enum"], ["AAPL", "MSFT"])
        catalog = next(item["handler"] for item in research.definitions()
                       if item["name"] == "discover_research_catalog")
        for identifier in ("MSFT", "AAPL"):
            self.assertEqual(catalog(company_id=identifier)["company_id"], identifier)
            self.assertTrue(all(fact["company_id"] == identifier
                                for fact in research.for_company(identifier)._facts.values()))
        for arguments in ({}, {"company_id": "FOREIGN"}):
            with self.assertRaises(ToolError):
                catalog(**arguments)
        self.assertEqual({fact["company_id"] for fact in research._facts.values()}, {"MSFT", "AAPL"})

    def test_frozen_scope_round_trip_and_tampering(self):
        research = self.tools()
        snapshot = snapshot_for(research)
        reopened = tools_for_scope(self.db, "MSFT", ["AAPL", "MSFT"], frozen_snapshot=snapshot)
        self.assertEqual(snapshot_for(reopened), snapshot)
        for change in ("subject", "membership", "aggregate"):
            changed = deepcopy(snapshot)
            if change == "subject":
                del changed["subjects"]["AAPL"]
            elif change == "membership":
                changed["execution_scope"]["company_ids"] = ["MSFT"]
            else:
                changed["evidence"]["facts"][0]["value"] = "999"
            with self.subTest(change=change), self.assertRaises(ToolError):
                tools_for_scope(self.db, "MSFT", ["AAPL", "MSFT"], frozen_snapshot=changed)
        with self.assertRaises(ToolError):
            tools_for_scope(self.db, "MSFT", ["MSFT"], frozen_snapshot=snapshot)

    def test_cross_subject_arithmetic_is_explicit_exact_and_preserves_periods(self):
        research = self.tools()
        template = next(iter(research._facts.values()))
        for identifier, value, start, end in (
                ("MSFT", "1", "2025-01-01", "2025-12-31"),
                ("AAPL", "3", "2024-10-01", "2025-09-30")):
            research._facts[identifier + "-synthetic"] = {
                **template, "fact_id": identifier + "-synthetic", "company_id": identifier,
                "metric": "unseen_load", "value": value, "unit": "service units",
                "basis": "synthetic protocol fixture", "period_kind": "year",
                "period_start": start, "period_end": end,
            }
        refs = ["MSFT-synthetic", "AAPL-synthetic"]
        with self.assertRaises(ToolError):
            research.calculate_facts(operation="ratio", fact_ids=refs)
        result = research.calculate_facts(operation="ratio", fact_ids=refs, comparison_mode="cross_subject")
        self.assertEqual(result["arithmetic"]["exact_value"], {"numerator": "1", "denominator": "3"})
        self.assertEqual(result["execution_scope"]["company_ids"], ["AAPL", "MSFT"])
        context = result["arithmetic"]["output_context"]
        self.assertIsNone(context["company_id"])
        self.assertEqual(context["company_ids"], ["AAPL", "MSFT"])
        self.assertEqual(context["periods"], [["2025-01-01", "2025-12-31"], ["2024-10-01", "2025-09-30"]])
        total = research.calculate_facts(operation="sum", fact_ids=[result["calculation_id"]] * 3)
        self.assertEqual(total["value"], "1")
        self.assertEqual(research.replay_arithmetic(total), total)
        for field, value in (("company_id", "FOREIGN"), ("unit", "other unit"), ("basis", "other basis")):
            original = research._facts["AAPL-synthetic"][field]
            research._facts["AAPL-synthetic"][field] = value
            with self.subTest(field=field), self.assertRaises(ToolError):
                research.calculate_facts(operation="ratio", fact_ids=refs, comparison_mode="cross_subject")
            research._facts["AAPL-synthetic"][field] = original

    def test_single_subject_factory_preserves_existing_protocol(self):
        original = ResearchTools(self.db, "MSFT")
        compatible = tools_for_scope(self.db, "MSFT", ["MSFT"])
        self.assertIs(type(compatible), ResearchTools)
        self.assertEqual(snapshot_for(compatible), snapshot_for(original))
        self.assertNotIn("execution_scope", snapshot_for(compatible))
        self.assertNotIn("company_id", compatible.definitions()[0]["parameters"]["properties"])


if __name__ == "__main__":
    unittest.main()
