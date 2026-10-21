"""Read-only document HTTP integration and boundary tests."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import quote

from workbench.build import build_database
from workbench.documents import DocumentService
from workbench.server import EvidenceHTTPServer


class DocumentHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.evidence = self.base / "evidence.sqlite3"
        build_database(db_path=self.evidence)
        self.documents_root = self.base / "documents"
        documents = DocumentService(self.documents_root)
        source = self.base / "arbitrary.html"
        source.write_text(
            "<!doctype html><html><body><div>Prefix 😀 ArbitraryHTTPToken supports the stated result.</div>"
            "<div>Second searchable division.</div></body></html>"
        )
        self.record = documents.register_file(
            source, company_id="MSFT", source_key="arbitrary", title="Arbitrary filing",
            url="https://example.test/report", published_at="2026-01-02", rights_status="public",
        )
        foreign = self.base / "foreign.html"
        foreign.write_text("<!doctype html><html><body><div>ForeignHTTPToken only.</div></body></html>")
        documents.register_file(foreign, company_id="AAPL", source_key="foreign", title="Foreign")
        self.server = EvidenceHTTPServer(0, self.evidence, documents_root=self.documents_root)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)
        self.temporary.cleanup()

    def request(self, path, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_coverage_search_and_unicode_passage_context(self):
        status, coverage = self.request("/api/documents/coverage?company=MSFT")
        self.assertEqual(status, 200)
        self.assertEqual(coverage["enrolled"], 1)
        self.assertEqual(coverage["documents"][0]["document_version_id"], self.record["document_version_id"])

        status, search = self.request("/api/documents/search?company=MSFT&query=ArbitraryHTTPToken&limit=3")
        self.assertEqual(status, 200)
        passage = search["results"][0]
        self.assertEqual(passage["company_id"], "MSFT")
        status, detail = self.request(
            f'/api/documents/passage?company=MSFT&id={passage["passage_id"]}&context=20'
        )
        self.assertEqual(status, 200)
        chars = list(detail["context"])
        quote_chars = list(detail["quote"])
        self.assertEqual("".join(chars[detail["offset"]:detail["offset"] + len(quote_chars)]), detail["quote"])
        self.assertNotIn("path", detail)

    def test_cross_company_missing_and_invalid_requests(self):
        foreign = self.request("/api/documents/search?company=AAPL&query=ForeignHTTPToken")[1]["results"][0]
        self.assertEqual(
            self.request(f'/api/documents/passage?company=MSFT&id={foreign["passage_id"]}')[0], 404
        )
        self.assertEqual(self.request("/api/documents/passage?company=MSFT&id=" + "f" * 64)[0], 404)
        invalid = [
            "/api/documents/search?company=MSFT",
            "/api/documents/search?company=msft&query=x",
            "/api/documents/search?company=MSFT&query=x&limit=0",
            "/api/documents/search?company=MSFT&query=x&path=/etc/passwd",
            "/api/documents/passage?company=MSFT&id=bad",
            "/api/documents/passage?company=MSFT&id=" + "a" * 64 + "&context=-1",
            "/api/documents/coverage?company=MSFT&company=AAPL",
        ]
        for path in invalid:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 400)
        self.assertEqual(self.request("/api/documents/raw?path=" + quote("/etc/passwd"))[0], 404)
        self.assertEqual(self.request("/api/documents/search?company=MSFT&query=x", "POST")[0], 405)

    def test_missing_catalog_is_503_and_is_not_created(self):
        root = self.base / "not-installed"
        server = EvidenceHTTPServer(
            0, self.evidence, chat_db=self.base / "missing-chat.sqlite3",
            wiki_db=self.base / "missing-wiki.sqlite3", documents_root=root,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request("GET", "/api/documents/coverage?company=MSFT")
            response = connection.getresponse()
            body = json.loads(response.read())
            connection.close()
            self.assertEqual(response.status, 503)
            self.assertEqual(body["error"], "document_service_unavailable")
            self.assertFalse(root.exists())
        finally:
            server.shutdown(); server.server_close(); thread.join(3)

    def test_document_catalog_collision_is_preflighted_before_writes(self):
        collision_root = self.base / "collision"
        collision_root.mkdir()
        collision_evidence = collision_root / "catalog.sqlite3"
        build_database(db_path=collision_evidence)
        chat = collision_root / "chat-new.sqlite3"
        wiki = collision_root / "wiki-new.sqlite3"
        with self.assertRaisesRegex(RuntimeError, "evidence and documents_catalog"):
            EvidenceHTTPServer(
                0, collision_evidence, chat_db=chat, wiki_db=wiki,
                documents_root=collision_root,
            )
        self.assertFalse(chat.exists())
        self.assertFalse(wiki.exists())


if __name__ == "__main__":
    unittest.main()
