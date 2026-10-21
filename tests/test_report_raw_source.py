import hashlib
import http.client
import json
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from tests.test_recorded_calculations import wait
from tests.test_report_publications import ReportProvider
from workbench.build import build_database
from workbench.documents import DocumentService
from workbench.owner_auth import OwnerAuth
from workbench.server import EvidenceHTTPServer


ROOT = Path(__file__).resolve().parents[1]


class ReportRawSourceHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "evidence.sqlite3"
        build_database(db_path=self.db)
        self.documents = DocumentService(self.root / "documents")

        self.auth_patch = patch("workbench.owner_auth.PBKDF2_ITERATIONS", 1_000)
        self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.password = secrets.token_urlsafe(24)
        self.auth = OwnerAuth(self.root / "auth" / "owner.sqlite3")
        nonce = self.auth.bootstrap_path.read_text(encoding="ascii").strip()
        self.auth.initialize(nonce, self.password)

        self.server = EvidenceHTTPServer(
            0,
            self.db,
            chat_db=self.root / "chat.sqlite3",
            documents_root=self.documents.root,
            provider=ReportProvider(),
            auth=self.auth,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close_server)
        self.origin = f"http://127.0.0.1:{self.server.server_port}"
        self.cookie = self._login()

    def _close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(self, path, *, method="GET", body=None, authenticated=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {}
        encoded = None
        if authenticated:
            headers["Cookie"] = self.cookie
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
            headers.update({
                "Content-Type": "application/json",
                "Content-Length": str(len(encoded)),
                "Origin": self.origin,
            })
        try:
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def _login(self):
        status, headers, _body = self._request(
            "/api/auth/login", method="POST", body={"password": self.password}, authenticated=False,
        )
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0]

    def _get_json(self, path, *, authenticated=True):
        status, headers, body = self._request(path, authenticated=authenticated)
        return status, headers, json.loads(body)

    def _publish_report(self, expected_sha256):
        submitted = self.server.chat.create_and_submit("MSFT", "微软收入相对以前的累计变化")
        run = wait(self.server.chat, submitted["run_id"])
        self.assertEqual(run["status"], "completed", run.get("error"))
        message = self.server.chat.get_conversation(submitted["conversation_id"])["messages"][-1]

        status, _headers, index = self._get_json(
            "/api/reports?" + urlencode({"conversation_id": submitted["conversation_id"]})
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(index["reports"]), 1)
        report_id = index["reports"][0]["report_id"]
        status, _headers, reader = self._get_json(
            "/api/reports/reader?" + urlencode({"report_id": report_id})
        )
        self.assertEqual(status, 200)
        source = next(item for item in reader["sources"] if item["source_sha256"] == expected_sha256)
        self.assertEqual(reader["report"]["message_id"], message["message_id"])
        return {"report_id": report_id, "source_id": source["id"], "source": source}

    @staticmethod
    def _raw_path(report_id, source_id):
        return "/api/reports/raw-source?" + urlencode({"report_id": report_id, "source_id": source_id})

    def test_legacy_source_reuses_only_same_report_frozen_bytes(self):
        raw_path = ROOT / "data/raw/microsoft-fy2025-q4-press-release.html"
        raw = raw_path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        self.documents.register_file(raw_path, company_id="MSFT", source_key="release", title="Release")
        report = self._publish_report(sha)
        reader_path = "/api/reports/reader?" + urlencode({"report_id": report["report_id"]})
        _, _, before = self._get_json(reader_path)
        legacy = next(source for source in before["sources"]
                      if source.get("source_sha256") == sha and not source.get("document_version_id"))
        status, _, detail = self._get_json("/api/reports/source?" + urlencode({
            "report_id": report["report_id"], "source_id": legacy["id"]}))
        self.assertEqual(status, 200)
        self.assertTrue(detail["raw_download_available"])
        self.assertEqual(detail["source"], legacy)
        status, headers, body = self._request(detail["raw_download_href"])
        self.assertEqual(status, 200)
        self.assertEqual(body, raw)
        self.assertEqual(headers["X-Verified-Source-SHA256"], sha)
        self.assertEqual(self._get_json(reader_path)[2], before)
        status, _, _ = self._request(detail["raw_download_href"], authenticated=False)
        self.assertEqual(status, 401)
        frozen = self.documents.objects_path / sha
        frozen.chmod(0o644)
        frozen.write_bytes(b"tampered legacy source")
        self.assertEqual(self._request(detail["raw_download_href"])[0], 503)

    def test_legacy_source_does_not_match_url_or_absent_document(self):
        from copy import deepcopy
        from workbench.report_reader import build_reader_source_from_payload
        legacy = {"id": "legacy", "source_sha256": "a" * 64, "url": "https://example.org/report"}
        document = {"id": "doc", "source_sha256": "b" * 64,
                    "document_version_id": "doc", "url": legacy["url"]}
        payload = {"schema_version": "test", "report": {"id": "report-test"},
                   "sources": [legacy, document], "evidence": {}}
        before = deepcopy(payload)
        result = build_reader_source_from_payload(payload, "legacy")
        self.assertFalse(result["raw_download_available"])
        self.assertIsNone(result["raw_download_href"])
        self.assertEqual(payload, before)
        payload["sources"] = [legacy]
        self.assertFalse(build_reader_source_from_payload(payload, "legacy")["raw_download_available"])

    def test_formal_report_raw_bytes_are_scoped_frozen_verified_and_owner_protected(self):
        first_path = ROOT / "data/raw/microsoft-fy2025-q4-press-release.html"
        first_raw = first_path.read_bytes()
        first_sha = hashlib.sha256(first_raw).hexdigest()
        first_version = self.documents.register_file(
            first_path, company_id="MSFT", source_key="release", title="FY2025 release",
        )
        first_report = self._publish_report(first_sha)
        self.assertEqual(first_report["source_id"], first_version["document_version_id"])

        first_url = self._raw_path(first_report["report_id"], first_report["source_id"])
        status, headers, body = self._request(first_url)
        self.assertEqual(status, 200)
        self.assertEqual(body, first_raw)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(headers["X-Verified-Source-SHA256"], first_sha)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(
            headers["Content-Disposition"], f'attachment; filename="original-{first_sha[:12]}.html"',
        )

        status, _headers, unauthenticated = self._request(first_url, authenticated=False)
        self.assertEqual(status, 401)
        self.assertNotEqual(unauthenticated, first_raw)
        self.assertEqual(json.loads(unauthenticated)["error"], "authentication_required")

        second_path = ROOT / "data/raw/microsoft-fy2024-q4-press-release.html"
        second_raw = second_path.read_bytes()
        second_sha = hashlib.sha256(second_raw).hexdigest()
        second_version = self.documents.register_file(
            second_path, company_id="MSFT", source_key="release", title="FY2024 release",
        )
        second_report = self._publish_report(second_sha)
        self.assertEqual(second_report["source_id"], second_version["document_version_id"])
        self.assertNotEqual(first_report["source_id"], second_report["source_id"])
        self.assertNotEqual(first_sha, second_sha)

        for report, foreign_source_id in (
            (first_report, second_report["source_id"]),
            (second_report, first_report["source_id"]),
        ):
            status, _headers, rejected = self._get_json(
                self._raw_path(report["report_id"], foreign_source_id)
            )
            self.assertEqual(status, 404)
            self.assertEqual(rejected["error"], "report_source_not_found")

        newer_path = self.root / "registered-newer.html"
        newer_path.write_bytes(second_raw + b"\n<!-- registered after both frozen reports -->\n")
        newer_version = self.documents.register_file(
            newer_path, company_id="MSFT", source_key="release", title="Registered newer edition",
        )
        self.assertNotIn(
            newer_version["document_version_id"],
            {first_report["source_id"], second_report["source_id"]},
        )
        for report in (first_report, second_report):
            status, _headers, rejected = self._get_json(
                self._raw_path(report["report_id"], newer_version["document_version_id"])
            )
            self.assertEqual(status, 404)
            self.assertEqual(rejected["error"], "report_source_not_found")

        frozen_object = self.documents.objects_path / first_sha
        frozen_object.chmod(0o644)
        frozen_object.write_bytes(b"tampered report source")
        status, _headers, rejected_body = self._request(first_url)
        self.assertEqual(status, 503)
        self.assertNotEqual(rejected_body, b"tampered report source")


if __name__ == "__main__":
    unittest.main()
