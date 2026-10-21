import ast
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from workbench.data_contracts import SourceVersion
from workbench.documents import DocumentError, DocumentService
from workbench.material_parser import MaterialDocumentService
from workbench.raw_storage import RawStorageError
from workbench.research_tools import ResearchTools
from workbench.service import EvidenceService


RAW = b"<html><body><p>Exact provider bytes for boundary testing.</p></body></html>"


class MemoryObjectStore:
    def __init__(self, *, fail=False):
        self.objects = {}
        self.fail = fail

    def put_if_absent(self, digest, raw):
        if self.fail:
            raise RawStorageError("object_store_unavailable", "storage unavailable")
        self.objects.setdefault(digest, raw)

    def get_verified(self, digest, expected_size=None):
        raw = self.objects[digest]
        if expected_size is not None and len(raw) != expected_size:
            raise RawStorageError("object_tampered", "unexpected size")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise RawStorageError("object_tampered", "digest mismatch")
        return raw


class FakeEvidenceReader:
    def __init__(self):
        self.calls = []

    def report(self, company_id):
        self.calls.append(company_id)
        return {
            "company_id": company_id,
            "generated_at": "2026-10-02T00:00:00Z",
            "manifest": {"schema_version": "fake/1", "build_version": "fake"},
            "input_hashes": {},
            "sources": [], "evidence": [], "claims": [], "facts": [],
            "evidence_chains": [], "activities": [],
        }


class SourceBoundaryTests(unittest.TestCase):
    def metadata(self):
        return {"company_id": "ACME", "source_key": "provider/report",
                "title": "Provider report", "media_type": "text/html"}

    def test_local_and_external_store_keep_bytes_identity_and_query_shape(self):
        with tempfile.TemporaryDirectory() as local_root, tempfile.TemporaryDirectory() as remote_root:
            local = DocumentService(local_root)
            external = DocumentService(remote_root, object_store=MemoryObjectStore())
            local_record = local.register_bytes(RAW, **self.metadata())
            remote_record = external.register_bytes(RAW, **self.metadata())
            self.assertEqual(
                {k: v for k, v in local_record.items() if k != "registered_at"},
                {k: v for k, v in remote_record.items() if k != "registered_at"},
            )
            self.assertEqual(local.read_raw_version(
                company_id="ACME", document_version_id=local_record["document_version_id"]), RAW)
            self.assertEqual(external.read_raw_version(
                company_id="ACME", document_version_id=remote_record["document_version_id"]), RAW)
            local_result = local.search_documents(company_id="ACME", query="provider")["results"][0]
            remote_result = external.search_documents(company_id="ACME", query="provider")["results"][0]
            self.assertEqual(local_result, remote_result)
            self.assertEqual(local_result["evidence_ref"]["source"]["version_id"],
                             local_record["document_version_id"])
            self.assertEqual(local_result["evidence_ref"]["locator"]["type"], "text_span")
            self.assertEqual(local_result["evidence_ref"]["processing_status"], "parsed")

    def test_storage_failure_never_records_source_success(self):
        with tempfile.TemporaryDirectory() as root:
            service = DocumentService(root, object_store=MemoryObjectStore(fail=True))
            with self.assertRaises(DocumentError) as caught:
                service.register_bytes(RAW, **self.metadata())
            self.assertEqual(caught.exception.code, "object_store_unavailable")
            self.assertEqual(service.get_document_coverage(company_id="ACME")["enrolled"], 0)

    def test_public_and_private_source_versions_cannot_cross_registry(self):
        with tempfile.TemporaryDirectory() as public_root, tempfile.TemporaryDirectory() as private_root:
            public = DocumentService(public_root)
            private = MaterialDocumentService(private_root)
            public_record = public.register_bytes(RAW, **self.metadata())
            private_record = private.register_bytes(RAW, **self.metadata())
            typed_public = SourceVersion(
                public_record["source_key"], public_record["document_version_id"],
                public_record["source_version"], public_record["source_sha256"], len(RAW), "public")
            self.assertEqual(public.source_registry.read(typed_public), RAW)
            with self.assertRaises(DocumentError) as caught:
                private.source_registry.read(typed_public)
            self.assertEqual(caught.exception.code, "source_scope_mismatch")
            self.assertEqual(private.source_registry.scope, "private")
            self.assertEqual(private_record["document_version_id"], public_record["document_version_id"])

    def test_sql_evidence_query_has_typed_version_locator_and_status(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "evidence.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.executescript("""
                    CREATE TABLE sources (
                        source_id TEXT, source_version_id TEXT, title TEXT, url TEXT,
                        published_at TEXT, published_at_basis TEXT, collected_at TEXT,
                        sha256 TEXT, rights_status TEXT, bytes INTEGER,
                        parser_version TEXT, text TEXT, tables_json TEXT);
                    CREATE TABLE evidence (
                        evidence_id TEXT, source_id TEXT, quote TEXT, start INTEGER, end INTEGER,
                        table_index INTEGER, row_index INTEGER, source_sha256 TEXT,
                        parser_version TEXT);
                """)
                digest = hashlib.sha256(b"provider").hexdigest()
                connection.execute(
                    "INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("source-1", "srcv-stable", "Source", None, None, None, None,
                     digest, "local_only", 8, "parser/1", "provider", "[]"))
                connection.execute(
                    "INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?)",
                    ("evidence-1", "source-1", "provider", 0, 8, 2, 3,
                     digest, "parser/1"))
            result = EvidenceService(path).evidence_reference("evidence-1")
            self.assertEqual(result["evidence"]["locator"], {
                "type": "text_span", "start": 0, "end": 8,
                "table_index": 2, "row_index": 3})
            self.assertEqual(result["evidence_ref"]["source"]["version_id"], "srcv-stable")
            self.assertIsNone(result["evidence_ref"]["source"]["version"])
            self.assertEqual(result["evidence_ref"]["locator"]["type"], "table_text_span")
            self.assertEqual(result["evidence_ref"]["processing_status"], "parsed")

    def test_injected_fake_reader_runs_research_without_database(self):
        reader = FakeEvidenceReader()
        research = ResearchTools(None, "ACME", evidence_reader=reader)
        catalog = research.discover_research_catalog()
        self.assertEqual(reader.calls, ["ACME"])
        self.assertEqual(catalog["company_id"], "ACME")
        self.assertEqual(catalog["metrics"], [])

    def test_l1_and_contract_import_directions(self):
        root = Path(__file__).resolve().parents[1] / "workbench"
        imports = {}
        for name in ("source_registry.py", "raw_storage.py", "data_contracts.py"):
            tree = ast.parse((root / name).read_text(encoding="utf-8"))
            imports[name] = {
                alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names
            } | {
                (node.module or "") for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            }
        forbidden = ("parser", "research", "service", "http", "chat")
        self.assertFalse(any(any(token in module for token in forbidden)
                             for module in imports["source_registry.py"] | imports["raw_storage.py"]))
        self.assertFalse(any(module.startswith("workbench") or module.startswith(".")
                             for module in imports["data_contracts.py"]))


if __name__ == "__main__":
    unittest.main()
