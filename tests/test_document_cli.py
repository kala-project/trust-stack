from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from workbench.build import build_database
from workbench.document_cli import OFFICIAL_SOURCES, bootstrap, checked_date, main, ROOT
from workbench.documents import DocumentError, DocumentService
from workbench.research_tools import ResearchTools
from tests.test_documents import pdf_with_text


class DocumentCLITests(unittest.TestCase):
    def test_cli_register_idempotent_and_pdf_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "store"
            source = Path(temp) / "source.html"
            source.write_text("<!doctype html><html><body>Repurchased shares; dividends paid.</body></html>")
            args = ["--root", str(root), "register", str(source), "--company", "MSFT",
                    "--source-key", "new-report", "--title", "New report", "--published-at", "2025-07-30"]
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(args), 0)
            first = json.loads(out.getvalue())
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(args), 0)
            self.assertEqual(first, json.loads(out.getvalue()))
            self.assertIsNone(first["collected_at"])
            source.write_text("<!doctype html><html><body>Revised disclosure</body></html>")
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(args), 0)
            self.assertEqual(json.loads(out.getvalue())["source_version"], 2)
            self.assertIn(b"Repurchased", DocumentService(root).read_raw_version(company_id="MSFT", document_version_id=first["document_version_id"]))
            pdf = Path(temp) / "report.pdf"
            pdf.write_bytes(pdf_with_text(["CliPdfAnchor disclosure"]))
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(["--root", str(root), "register", str(pdf), "--company", "MSFT", "--source-key", "pdf", "--title", "PDF"]), 0)
            self.assertEqual(json.loads(out.getvalue())["parse_status"], "parsed")
            service = DocumentService(root)
            self.assertEqual(service.get_document_coverage(company_id="MSFT")["parsed"], 2)
            self.assertTrue(service.search_documents(company_id="MSFT", query="CliPdfAnchor")["results"])

    def test_dates_and_missing_coverage_fail_without_creating_catalog(self):
        for invalid in ["yesterday", "2025-02-29", "2025-10-01T12:00:00"]:
            with self.assertRaises(DocumentError):
                checked_date(invalid)
        with self.assertRaises(DocumentError):
            checked_date("2025-10-01", timestamp=True)
        self.assertEqual(checked_date("2025-10-01T12:00:00Z", timestamp=True), "2025-10-01T12:00:00Z")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "missing"
            with redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--root", str(root), "coverage", "--company", "MSFT"]), 2)
            self.assertFalse(root.exists())

    def test_bound_manifest_hash_rejects_before_object_commit(self):
        with tempfile.TemporaryDirectory() as temp:
            service = DocumentService(Path(temp) / "store")
            source = Path(temp) / "source.html"
            source.write_bytes(b"<html>New bytes</html>")
            values = dict(company_id="MSFT", source_key="report", title="Report")
            with self.assertRaises(DocumentError) as caught:
                service.register_file(source, expected_sha256="0" * 64, **values)
            self.assertEqual(caught.exception.code, "source_hash_mismatch")
            with self.assertRaises(DocumentError) as caught:
                service.register_file(source, expected_byte_length=1, **values)
            self.assertEqual(caught.exception.code, "source_size_mismatch")
            self.assertEqual(service.get_document_coverage(company_id="MSFT")["enrolled"], 0)
            self.assertEqual(list(service.objects_path.iterdir()), [])

    def test_real_official_bootstrap_and_queries_independent_of_claims(self):
        catalog = json.loads((ROOT / "data/source-catalog.json").read_text(encoding="utf-8"))
        by_id = {item["id"]: item for item in catalog["sources"]}
        required_paths = [
            ROOT / by_id[source_id]["artifact"]["path"]
            for source_id in OFFICIAL_SOURCES
            if source_id in by_id and by_id[source_id].get("artifact")
        ]
        if not all(path.exists() for path in required_paths):
            self.skipTest("Official local artifacts not present; never download automatically")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "store"
            original = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT / "data/raw").glob("*.html")}
            first = bootstrap(root)
            second = bootstrap(root)
            self.assertEqual(first, second)
            self.assertEqual(len(first["records"]), len(OFFICIAL_SOURCES))
            self.assertEqual(first["coverage"]["MSFT"]["parsed"], 4)
            self.assertEqual(first["coverage"]["AAPL"]["parsed"], 1)
            self.assertEqual(first["coverage"]["NVDA"]["parsed"], 2)
            self.assertEqual(first["coverage"]["CRM"]["parsed"], 2)
            self.assertEqual(first["coverage"]["IBM"]["parsed"], 2)
            self.assertEqual(len([s for s in first["excluded"] if s["reason"] == "terms_not_research_evidence"]), 2)
            service = DocumentService(root)
            for query in ["repurchased", "dividends", "回购", "分红"]:
                with self.subTest(query=query):
                    result = service.search_documents(company_id="MSFT", query=query)
                    self.assertTrue(result["results"])
                    self.assertTrue(any(any(term in r["quote"].lower() for term in ["repurchas", "dividend"]) for r in result["results"]))
                    for hit in result["results"]:
                        passage = service.read_passage(company_id="MSFT", passage_id=hit["passage_id"])
                        offset = passage["offset"]
                        self.assertEqual(passage["context"][offset:offset + len(hit["quote"])], hit["quote"])
            # The shareholder-payment extraction now indexes repurchased amounts.
            # CEO attribution remains unstructured: fulltext must not depend on claims.
            # Build an isolated database rather than depending on the deployed version.
            db = Path(temp) / "evidence.sqlite3"
            build_database(db_path=db)
            tools = ResearchTools(db, "MSFT", document_service=service)
            self.assertFalse(tools.search_evidence(query="Satya")["results"])
            self.assertTrue(tools.search_documents(query="Satya")["results"])
            self.assertEqual(original, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in original})

    def test_bad_bootstrap_manifest_does_not_create_store(self):
        with tempfile.TemporaryDirectory() as temp:
            catalog = Path(temp) / "catalog.json"
            payload = json.loads((ROOT / "data/source-catalog.json").read_text())
            payload["sources"][0]["artifact"]["sha256"] = "0" * 64
            catalog.write_text(json.dumps(payload))
            root = Path(temp) / "store"
            with self.assertRaises(DocumentError):
                bootstrap(root, catalog=catalog)
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
