from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from workbench.build import build_database
from workbench.documents import DocumentService
from workbench.research_tools import ResearchTools, ToolError
from tests.test_documents import pdf_with_text


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/source-catalog.json"


class ResearchToolsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.temp.name) / "evidence.sqlite3"
        build_database(CATALOG, cls.db, ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_facts_are_company_scoped_and_have_real_evidence(self):
        tools = ResearchTools(self.db, "MSFT")
        result = tools.get_company_facts(metric="revenue", period="2025-06-30")
        self.assertTrue(result["facts"])
        self.assertTrue(all(item["company_id"] == "MSFT" for item in result["facts"]))
        self.assertTrue(all(item["evidence_id"] and item["source_id"] for item in result["facts"]))

    def test_claim_from_another_company_is_rejected(self):
        apple = ResearchTools(self.db, "AAPL")
        apple_claim = next(iter(apple._claims))
        with self.assertRaisesRegex(ToolError, "selected company"):
            ResearchTools(self.db, "MSFT").get_claim_evidence(claim_id=apple_claim)

    def test_search_returns_native_locator_and_exact_ids(self):
        result = ResearchTools(self.db, "MSFT").search_evidence(query="revenue", limit=3)
        self.assertTrue(result["results"])
        for item in result["results"]:
            self.assertTrue(item["evidence_id"])
            self.assertEqual(item["locator"]["type"], "text_span")
            self.assertTrue(item["snippet"])

    def test_closed_schema_and_runtime_reject_unknown_arguments(self):
        definition = ResearchTools(self.db, "MSFT").definitions()[0]
        self.assertFalse(definition["parameters"]["additionalProperties"])
        with self.assertRaises(TypeError):
            definition["handler"](query="revenue", sql="select * from facts")

    def test_decimal_yoy_checks_compatibility(self):
        tools = ResearchTools(self.db, "MSFT")
        facts = tools.get_company_facts(metric="revenue")["facts"]
        current = next(item for item in facts if item["period_end"] == "2025-06-30" and item["period_kind"] == "quarter")
        prior = next(item for item in facts if item["period_end"] == "2024-06-30" and item["period_kind"] == "quarter")
        result = tools.calculate_yoy(current_fact_id=current["fact_id"], prior_fact_id=prior["fact_id"])
        self.assertEqual(result["value"], "18.10")
        incompatible = next(item for item in tools.get_company_facts(metric="eps_diluted")["facts"] if item["period_end"] == "2024-06-30")
        with self.assertRaisesRegex(ToolError, "different metric"):
            tools.calculate_yoy(current_fact_id=current["fact_id"], prior_fact_id=incompatible["fact_id"])

    def test_yoy_requires_corresponding_one_year_calendar_periods(self):
        tools = ResearchTools(self.db, "MSFT")
        facts = tools.get_company_facts(metric="revenue")["facts"]
        current = next(item for item in facts if item["period_end"] == "2025-06-30" and item["period_kind"] == "quarter")
        prior = next(item for item in facts if item["period_end"] == "2024-06-30" and item["period_kind"] == "quarter")
        altered = deepcopy(prior)
        altered["fact_id"] = "wrong-quarter"
        altered["period_end"] = "2024-03-31"
        tools._facts[altered["fact_id"]] = altered
        with self.assertRaisesRegex(ToolError, "corresponding periods"):
            tools.calculate_yoy(current_fact_id=current["fact_id"], prior_fact_id=altered["fact_id"])

    def test_yoy_rejects_company_and_segment_scope_mix(self):
        tools = ResearchTools(self.db, "MSFT")
        facts = tools.get_company_facts(metric="revenue")["facts"]
        current = deepcopy(next(item for item in facts if item["period_end"] == "2025-06-30" and item["period_kind"] == "quarter"))
        prior = deepcopy(next(item for item in facts if item["period_end"] == "2024-06-30" and item["period_kind"] == "quarter"))
        current["fact_id"], prior["fact_id"] = "scoped-current", "scoped-prior"
        current["segment"] = "example_segment"
        tools._facts.update({current["fact_id"]: current, prior["fact_id"]: prior})
        with self.assertRaisesRegex(ToolError, "different segment"):
            tools.calculate_yoy(current_fact_id=current["fact_id"], prior_fact_id=prior["fact_id"])

    def test_yoy_is_finite_half_up_and_uses_absolute_negative_base(self):
        tools = ResearchTools(self.db, "MSFT")
        facts = tools.get_company_facts(metric="revenue")["facts"]
        current = deepcopy(next(item for item in facts if item["period_end"] == "2025-06-30" and item["period_kind"] == "quarter"))
        prior = deepcopy(next(item for item in facts if item["period_end"] == "2024-06-30" and item["period_kind"] == "quarter"))
        current["fact_id"], prior["fact_id"] = "decimal-current", "decimal-prior"
        current["value"], prior["value"] = "-50", "-100"
        tools._facts.update({current["fact_id"]: current, prior["fact_id"]: prior})
        self.assertEqual(tools.calculate_yoy(current_fact_id=current["fact_id"], prior_fact_id=prior["fact_id"])["value"], "50.00")
        current["value"], prior["value"] = "101.005", "100"
        self.assertEqual(tools.calculate_yoy(current_fact_id=current["fact_id"], prior_fact_id=prior["fact_id"])["value"], "1.01")
        current["value"] = "NaN"
        with self.assertRaisesRegex(ToolError, "finite"):
            tools.calculate_yoy(current_fact_id=current["fact_id"], prior_fact_id=prior["fact_id"])
        current["value"], prior["value"] = "1", "-0"
        with self.assertRaisesRegex(ToolError, "zero"):
            tools.calculate_yoy(current_fact_id=current["fact_id"], prior_fact_id=prior["fact_id"])

    def test_results_record_frozen_data_version(self):
        tools = ResearchTools(self.db, "MSFT")
        version = tools.get_company_facts(metric="revenue")["data_version"]
        self.assertEqual(version, tools.search_evidence(query="revenue", limit=1)["data_version"])
        self.assertTrue(version["generated_at"])
        self.assertTrue(version["input_hashes"])

    def test_document_tools_are_conditional_and_absence_is_explicit(self):
        tools = ResearchTools(self.db, "MSFT")
        self.assertEqual(len(tools.definitions()), 7)
        self.assertFalse(tools.get_document_coverage()["available"])
        with self.assertRaisesRegex(ToolError, "unavailable"):
            tools.search_documents(query="revenue")

    def test_document_search_is_frozen_and_passages_are_company_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            documents = DocumentService(base / "documents")
            old_path = base / "old.html"
            old_path.write_text("<!doctype html><html><body><div>ArbitraryOldToken 😀 revenue disclosure.</div></body></html>")
            old = documents.register_file(
                old_path, company_id="MSFT", source_key="filing", title="Old filing",
                url="https://example.test/old", published_at="2025-01-01", rights_status="public",
            )
            other_path = base / "other.html"
            other_path.write_text("<!doctype html><html><body><div>OtherCompanyToken.</div></body></html>")
            documents.register_file(other_path, company_id="AAPL", source_key="filing", title="Other")

            tools = ResearchTools(self.db, "MSFT", documents)
            self.assertEqual(len(tools.definitions()), 12)
            self.assertEqual(tools.document_version_ids, (old["document_version_id"],))
            result = tools.search_documents(query="ArbitraryOldToken")
            passage = result["results"][0]
            self.assertEqual(result["version_scope"], "explicit")
            self.assertEqual(passage["company_id"], "MSFT")
            self.assertEqual(tools.read_passage(passage_id=passage["passage_id"])["quote"], passage["quote"])
            read_schema = next(
                item for item in tools.definitions() if item["name"] == "read_passage"
            )["parameters"]
            self.assertIn("exact_quote", read_schema["properties"])
            selected = tools.read_passage(
                passage_id=passage["passage_id"], exact_quote="revenue disclosure"
            )["exact_citation_selection"]
            self.assertEqual(selected["status"], "matched")
            self.assertTrue(selected["occurrences"][0]["supporting_passages"])

            new_path = base / "new.html"
            new_path.write_text("<!doctype html><html><body><div>CurrentOnlyToken replaces the prior text.</div></body></html>")
            new = documents.register_file(new_path, company_id="MSFT", source_key="filing", title="New filing")
            self.assertTrue(tools.search_documents(query="ArbitraryOldToken")["results"])
            with self.assertRaisesRegex(ToolError, "snapshot"):
                tools.search_documents(query="CurrentOnlyToken", document_version_ids=[new["document_version_id"]])
            foreign = documents.search_documents(company_id="AAPL", query="OtherCompanyToken")["results"][0]
            with self.assertRaisesRegex(ToolError, "this company"):
                tools.read_passage(passage_id=foreign["passage_id"])

    def test_pdf_document_tools_preserve_page_and_extraction_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            documents = DocumentService(base / "documents")
            path = base / "report.pdf"
            path.write_bytes(pdf_with_text(["First page", "ResearchPdfPageToken"]))
            version = documents.register_file(path, company_id="MSFT", source_key="pdf", title="PDF filing")
            tools = ResearchTools(self.db, "MSFT", documents)
            self.assertIn(version["document_version_id"], tools.document_version_ids)
            hit = tools.search_documents(query="ResearchPdfPageToken")["results"][0]
            self.assertEqual(hit["media_type"], "application/pdf")
            self.assertEqual(hit["locator"]["page"], 2)
            passage = tools.read_passage(passage_id=hit["passage_id"])
            self.assertEqual(passage["locator"]["page"], 2)
            self.assertEqual(passage["tables"], [])


if __name__ == "__main__":
    unittest.main()
