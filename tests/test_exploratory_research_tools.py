from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from workbench.build import build_database
from workbench.documents import DocumentService
from workbench.research_tools import ResearchTools, ToolError


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/source-catalog.json"


class ExploratoryResearchToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.db = Path(cls.fixture.name) / "evidence.sqlite3"
        build_database(CATALOG, cls.db, ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.documents = DocumentService(base / "documents")
        source = base / "annual.html"
        source.write_text(
            "<!doctype html><html><body>"
            "<table><tr><th>USD millions</th><th>2025</th><th>2024</th></tr>"
            "<tr><td>Capital expenditures</td><td>$120</td><td>$100</td></tr>"
            "<tr><td>Revenue</td><td>$240</td><td>$200</td></tr>"
            "<tr><td>Malformed unit</td><td>12 million</td><td>10 million</td></tr>"
            "</table></body></html>",
            encoding="utf-8",
        )
        self.version = self.documents.register_file(
            source, company_id="MSFT", source_key="annual", title="Annual filing",
            url="https://www.microsoft.com/investor/example", published_at="2025-07-01",
            rights_status="public",
        )
        self.tools = ResearchTools(self.db, "MSFT", self.documents)

    def tearDown(self):
        self.temporary.cleanup()

    def extract(self, row, column, metric, value, start, end, basis="issuer table presentation", **overrides):
        arguments = dict(
            document_version_id=self.version["document_version_id"], table_index=0,
            row_index=row, column_index=column, header_row_indices=[0],
            row_label_column_index=0, expected_value=value, metric=metric,
            unit="USD millions", period_start=start, period_end=end,
            period_kind="year", basis=basis, metadata_mode="explicit_interpretation",
        )
        arguments.update(overrides)
        return self.tools.extract_document_table_fact(**arguments)["fact"]

    def test_catalog_is_question_independent_bounded_and_has_exact_table_version(self):
        catalog = self.tools.discover_research_catalog()
        self.assertIn("revenue", {item["metric"] for item in catalog["metrics"]})
        table = catalog["documents"][0]["tables"][0]
        self.assertEqual(table["header_preview"][0], ["USD millions", "2025", "2024"])
        self.assertRegex(table["table_sha256"], r"^[0-9a-f]{64}$")
        self.assertIn("not a statement", catalog["interpretation"])
        rows = self.tools.read_document_table(
            document_version_id=self.version["document_version_id"], table_index=0,
            row_offset=1, row_limit=1,
        )
        candidate = rows["rows"][0]["cells"][1]
        self.assertEqual(candidate["numeric_candidate"]["value"], "120")
        self.assertEqual(candidate["locator"]["row_index"], 1)

    def test_exact_raw_value_and_signed_decimal_produce_identical_frozen_fact(self):
        plain = self.extract(1, 1, "capital_expenditures", "120", "2024-07-01", "2025-06-30")
        raw = self.extract(1, 1, "capital_expenditures", "$120", "2024-07-01", "2025-06-30")
        self.assertEqual(plain["fact_id"], raw["fact_id"])
        self.assertEqual(
            plain["fact_id"],
            "docfact-fba078c4a03d116e41c5033c17b528828f97c2acc3ae35fc79ddde62611f20af",
        )
        self.assertNotIn("pdf_provenance", plain["locator"])
        with self.assertRaises(ToolError):
            self.extract(1, 1, "capital_expenditures", "$100", "2024-07-01", "2025-06-30")
        with self.assertRaises(ToolError):
            self.extract(1, 1, "capital_expenditures", "(120)", "2024-07-01", "2025-06-30")
        definition = next(d for d in self.tools.definitions() if d["name"] == "extract_document_table_fact")
        self.assertIn("all three", definition["description"])
        self.assertIn("numeric_candidate.value", definition["parameters"]["properties"]["expected_value"]["description"])

    def test_doc_cells_create_source_tied_facts_and_support_exact_difference_and_ratio(self):
        capex_2025 = self.extract(1, 1, "capital_expenditures", "120", "2024-07-01", "2025-06-30")
        capex_2024 = self.extract(1, 2, "capital_expenditures", "100", "2023-07-01", "2024-06-30")
        revenue_2025 = self.extract(2, 1, "revenue_from_document", "240", "2024-07-01", "2025-06-30")
        difference = self.tools.calculate_facts(
            operation="difference", fact_ids=[capex_2025["fact_id"], capex_2024["fact_id"]]
        )
        ratio = self.tools.calculate_facts(
            operation="ratio", fact_ids=[capex_2025["fact_id"], revenue_2025["fact_id"]]
        )
        self.assertEqual((difference["value"], difference["unit"]), ("20", "USD millions"))
        self.assertEqual((ratio["value"], ratio["unit"]), ("0.5", "x"))
        self.assertEqual(difference["inputs"][0]["raw_value"], "$120")
        self.assertEqual(difference["inputs"][0]["locator"]["table_sha256"], self.tools.read_document_table(
            document_version_id=self.version["document_version_id"], table_index=0
        )["table_sha256"])

    def test_wrong_cell_raw_unit_missing_period_and_unproven_metadata_are_refused(self):
        with self.assertRaisesRegex(ToolError, "does not equal"):
            self.extract(1, 1, "capital_expenditures", "121", "2024-07-01", "2025-06-30")
        with self.assertRaisesRegex(ToolError, "embedded unit words"):
            self.extract(3, 1, "capital_expenditures", "12", "2024-07-01", "2025-06-30")
        with self.assertRaises(ToolError) as missing:
            self.extract(1, 1, "capital_expenditures", "120", "2024-07-01", "")
        self.assertEqual(missing.exception.code, "missing_period")
        with self.assertRaises(ToolError) as unresolved:
            self.extract(
                1, 1, "capital_expenditures", "120", "2024-07-01", "2025-06-30",
                metadata_mode="table_proven", unit_evidence_text="USD millions",
                period_evidence_text="2025", basis_evidence_text="GAAP",
            )
        self.assertEqual(unresolved.exception.code, "unresolved_metadata")

    def test_snapshot_ownership_blocks_later_and_foreign_versions(self):
        later_path = Path(self.temporary.name) / "later.html"
        later_path.write_text("<!doctype html><html><body><table><tr><th>X</th></tr></table></body></html>")
        later = self.documents.register_file(
            later_path, company_id="MSFT", source_key="later", title="Later"
        )
        foreign_path = Path(self.temporary.name) / "foreign.html"
        foreign_path.write_text("<!doctype html><html><body><table><tr><th>X</th></tr></table></body></html>")
        foreign = self.documents.register_file(
            foreign_path, company_id="AAPL", source_key="foreign", title="Foreign"
        )
        for identifier in (later["document_version_id"], foreign["document_version_id"]):
            with self.subTest(identifier=identifier), self.assertRaisesRegex(ToolError, "snapshot"):
                self.tools.read_document_table(document_version_id=identifier, table_index=0)

    def test_unseen_metric_and_per_period_coverage_keep_bases_separate(self):
        first = self.extract(1, 1, "ten_year_capex", "120", "2024-07-01", "2025-06-30")
        second = self.extract(1, 1, "ten_year_capex", "120", "2024-07-01", "2025-06-30", basis="analyst interpretation")
        metrics = {item["metric"]: item for item in self.tools.discover_research_catalog()["metrics"]}
        self.assertIn("ten_year_capex", metrics)
        self.assertEqual(metrics["ten_year_capex"]["origins"], ["frozen_document_table_cell-v1"])
        coverage = self.tools.analyze_period_coverage(
            metric="ten_year_capex", period_ends=["2025-06-30", "2024-06-30"]
        )
        present, missing = coverage["periods"]
        self.assertEqual(present["status"], "present")
        self.assertTrue(present["multiple_bases_or_scopes"])
        self.assertEqual({item["dimensions"]["basis"] for item in present["basis_groups"]},
                         {first["basis"], second["basis"]})
        self.assertEqual(missing["status"], "missing")
        self.assertIn("not issuer non-disclosure", missing["meaning"])
        self.assertFalse(coverage["complete_for_requested_periods"])

    def test_existing_calculation_id_is_unchanged_after_document_fact_registration(self):
        facts = self.tools.get_company_facts(metric="revenue")["facts"]
        current = next(item for item in facts if item["period_end"] == "2025-06-30" and item["period_kind"] == "quarter")
        prior = next(item for item in facts if item["period_end"] == "2024-06-30" and item["period_kind"] == "quarter")
        before = self.tools.calculate_facts(
            operation="difference", fact_ids=[current["fact_id"], prior["fact_id"]]
        )["calculation_id"]
        self.extract(1, 1, "capital_expenditures", "120", "2024-07-01", "2025-06-30")
        after = self.tools.calculate_facts(
            operation="difference", fact_ids=[current["fact_id"], prior["fact_id"]]
        )["calculation_id"]
        self.assertEqual(before, after)

    def test_ratio_retains_nonterminating_exact_value_with_explicit_display_rounding(self):
        numerator = self.extract(1, 1, "numerator", "120", "2024-07-01", "2025-06-30")
        denominator = deepcopy(self.extract(2, 1, "denominator", "240", "2024-07-01", "2025-06-30"))
        denominator["fact_id"] = "docfact-denominator-three"
        denominator["value"] = "3"
        self.tools._facts[denominator["fact_id"]] = denominator
        # Synthetic values exercise 1/3; they are not financial source evidence.
        numerator = deepcopy(numerator)
        numerator["fact_id"] = "docfact-numerator-one"
        numerator["value"] = "1"
        self.tools._facts[numerator["fact_id"]] = numerator
        result = self.tools.calculate_facts(
            operation="ratio", fact_ids=[numerator["fact_id"], denominator["fact_id"]])
        self.assertEqual(result["value"], "0.333333333333")
        self.assertEqual(result["arithmetic"]["exact_value"], {"numerator": "1", "denominator": "3"})
        self.assertTrue(result["arithmetic"]["rounding"]["applied"])


if __name__ == "__main__":
    unittest.main()
