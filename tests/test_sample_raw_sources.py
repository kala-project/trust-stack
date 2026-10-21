from __future__ import annotations

import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlencode

from workbench.build import build_database
from workbench.documents import DocumentError, DocumentService
from workbench.sample_research import SampleResearchError, SampleResearchService
from workbench.server import EvidenceHTTPServer


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data/raw/microsoft-fy2025-q4-press-release.html"
SCOPE = {
    "company_ids": ["MSFT", "AAPL"],
    "metric": "revenue",
    "period_kind": "quarter",
    "start_date": "2025-04-01",
    "end_date": "2025-09-27",
}


class NoModelProvider:
    def status(self):
        return {"available": False, "provider": "sample-raw-test", "reason": "model disabled"}

    def stream(self, *_args):
        raise AssertionError("sample raw-source acceptance must not call a model")


def register_msft(documents: DocumentService) -> tuple[bytes, str]:
    raw = SOURCE.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    documents.register_file(
        SOURCE,
        company_id="MSFT",
        source_key="fy2025-q4-press-release",
        title="Microsoft FY2025 Q4 original",
        expected_sha256=digest,
        expected_byte_length=len(raw),
    )
    return raw, digest


class SampleRawSourceServiceTests(unittest.TestCase):
    def test_snapshot_version_owns_exact_bytes_and_tampering_can_be_retried_after_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence, results = root / "evidence.sqlite3", root / "sample.sqlite3"
            build_database(db_path=evidence)
            documents = DocumentService(root / "documents")
            service = SampleResearchService(evidence, results, document_service=documents)

            old = service.create(SCOPE)
            self.assertEqual((old["version"], old["frozen_raw_sources"]), (1, []))
            raw, digest = register_msft(documents)
            current = service.create(SCOPE)
            self.assertEqual(current["version"], 2)
            self.assertEqual(current["frozen_raw_sources"], [{
                "company_id": "MSFT",
                "source_sha256": digest,
                "document_version_id": current["frozen_raw_sources"][0]["document_version_id"],
            }])
            self.assertEqual(service.get(old["result_id"], 1)["frozen_raw_sources"], [])
            downloaded = service.read_raw_source(current["result_id"], 2, "MSFT", digest)
            self.assertEqual(downloaded, raw)
            self.assertEqual(hashlib.sha256(downloaded).hexdigest(), digest)

            for version, company_id, requested_digest in (
                (2, "AAPL", digest),
                (2, "MSFT", "0" * 64),
                (1, "MSFT", digest),
            ):
                with self.subTest(version=version, company_id=company_id, digest=requested_digest):
                    with self.assertRaises(SampleResearchError) as caught:
                        service.read_raw_source(old["result_id"], version, company_id, requested_digest)
                    self.assertEqual((caught.exception.code, caught.exception.status_code),
                                     ("source_not_in_snapshot", 404))

            stored = documents.objects_path / digest
            stored.chmod(0o600)
            stored.write_bytes(b"tampered")
            with self.assertRaises(DocumentError) as caught:
                service.read_raw_source(current["result_id"], 2, "MSFT", digest)
            self.assertEqual(caught.exception.code, "object_tampered")
            stored.write_bytes(raw)
            stored.chmod(0o400)
            self.assertEqual(service.read_raw_source(current["result_id"], 2, "MSFT", digest), raw)


class SampleRawSourceHTTPTests(unittest.TestCase):
    def test_http_requires_exact_version_and_returns_only_its_frozen_attachment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence.sqlite3"
            build_database(db_path=evidence)
            documents = DocumentService(root / "documents")
            server = EvidenceHTTPServer(
                0,
                evidence,
                sample_db=root / "sample.sqlite3",
                documents_root=root / "documents",
                provider=NoModelProvider(),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                def request(path: str, data=None):
                    body = json.dumps(data).encode() if data is not None else None
                    headers = ({"Content-Type": "application/json",
                                "Origin": f"http://127.0.0.1:{server.server_port}"}
                               if data is not None else {})
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=8)
                    try:
                        connection.request("POST" if data is not None else "GET", path,
                                           body=body, headers=headers)
                        response = connection.getresponse()
                        return response.status, dict(response.getheaders()), response.read()
                    finally:
                        connection.close()

                status, _, body = request("/api/sample-research/results", SCOPE)
                self.assertEqual(status, 201)
                old = json.loads(body)
                raw, digest = register_msft(documents)
                status, _, body = request("/api/sample-research/results", SCOPE)
                self.assertEqual(status, 201)
                current = json.loads(body)
                self.assertEqual((old["version"], current["version"]), (1, 2))
                self.assertEqual(old["frozen_raw_sources"], [])
                self.assertTrue(any(item["company_id"] == "MSFT" and item["source_sha256"] == digest
                                    for item in current["frozen_raw_sources"]))

                def source_path(version=None, company="MSFT", source_digest=digest):
                    values = {"id": current["result_id"], "company_id": company,
                              "source_sha256": source_digest}
                    if version is not None:
                        values["version"] = str(version)
                    return "/api/sample-research/raw-source?" + urlencode(values)

                status, headers, downloaded = request(source_path(2))
                self.assertEqual(status, 200)
                self.assertEqual(downloaded, raw)
                self.assertEqual(hashlib.sha256(downloaded).hexdigest(), digest)
                self.assertTrue(headers["Content-Type"].startswith("text/html"))
                self.assertIn(f'attachment; filename="original-{digest[:12]}.html"', headers["Content-Disposition"])

                self.assertEqual(request(source_path())[0], 400, "HTTP must not resolve latest")
                self.assertEqual(request(source_path(2, company="AAPL"))[0], 404)
                self.assertEqual(request(source_path(2, source_digest="0" * 64))[0], 404)
                self.assertEqual(request(source_path(1))[0], 404)

                stored = documents.objects_path / digest
                stored.chmod(0o600)
                stored.write_bytes(b"tampered")
                self.assertEqual(request(source_path(2))[0], 503)
                stored.write_bytes(raw)
                stored.chmod(0o400)
                self.assertEqual(request(source_path(2))[2], raw)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)


if __name__ == "__main__":
    unittest.main()
