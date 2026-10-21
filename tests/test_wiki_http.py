"""Real loopback HTTP tests for the local Wiki change lifecycle."""
import hashlib
import http.client
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from workbench.build import build_database
from workbench.server import EvidenceHTTPServer


class WikiHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.evidence_db = cls.root / "evidence.sqlite3"
        build_database(db_path=cls.evidence_db)
        with closing(sqlite3.connect(cls.evidence_db)) as connection:
            cls.msft_claim = connection.execute(
                "SELECT claim_id,text FROM claims WHERE company_id='MSFT' ORDER BY claim_id LIMIT 1"
            ).fetchone()
            cls.apple_claim = connection.execute(
                "SELECT claim_id FROM claims WHERE company_id='AAPL' ORDER BY claim_id LIMIT 1"
            ).fetchone()[0]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        name = self.id().rsplit(".", 1)[-1]
        self.server = EvidenceHTTPServer(
            0, self.evidence_db,
            chat_db=self.root / f"{name}-chat.sqlite3",
            wiki_db=self.root / f"{name}-wiki.sqlite3",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    @property
    def statements(self):
        return [
            {"kind": "evidence_summary", "text": self.msft_claim[1],
             "claim_ids": [self.msft_claim[0]]},
            {"kind": "hypothesis", "text": "ROI may improve; this is unverified.", "claim_ids": []},
        ]

    def request(self, path, data=None, *, raw=None, headers=None, method=None):
        method = method or ("POST" if data is not None or raw is not None else "GET")
        request_headers = {"Origin": self.origin, "Content-Type": "application/json"} if method == "POST" else {}
        request_headers.update(headers or {})
        body = json.dumps(data, ensure_ascii=False).encode() if data is not None else raw
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=6)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def create(self, title="Microsoft notes"):
        code, body = self.request("/api/wiki/changes", {
            "company_id": "MSFT", "base_version": 0, "title": title,
            "statements": self.statements,
        })
        self.assertEqual(code, 201, body)
        return body

    def test_create_review_publish_history_over_real_http(self):
        evidence_hash = hashlib.sha256(self.evidence_db.read_bytes()).hexdigest()
        self.assertEqual(self.request("/api/wiki/page?company=MSFT")[1]["version"], 0)
        change = self.create()
        code, reviewed = self.request("/api/wiki/review", {
            "change_id": change["change_id"], "expected_hash": change["content_hash"],
            "decision": "approve",
        })
        self.assertEqual((code, reviewed["status"]), (200, "approved"))
        code, page = self.request("/api/wiki/publish", {
            "change_id": change["change_id"], "expected_hash": change["content_hash"],
        })
        self.assertEqual((code, page["version"], page["semantic_status"]), (200, 1, "unreviewed"))
        self.assertEqual(self.request("/api/wiki/page?company=MSFT&version=1")[1]["title"], "Microsoft notes")
        history = self.request("/api/wiki/changes?company=MSFT")[1]
        self.assertEqual(history["changes"][0]["status"], "applied")
        self.assertEqual(self.request("/api/wiki/change?id=" + change["change_id"])[1]["events"][-1]["type"], "published")
        self.assertEqual(hashlib.sha256(self.evidence_db.read_bytes()).hexdigest(), evidence_hash)

    def test_stale_competing_publish_and_wiki_error_status(self):
        first, second = self.create("First"), self.create("Second")
        for change in (first, second):
            self.assertEqual(self.request("/api/wiki/review", {
                "change_id": change["change_id"], "expected_hash": change["content_hash"],
                "decision": "approve",
            })[0], 200)
        self.assertEqual(self.request("/api/wiki/publish", {
            "change_id": first["change_id"], "expected_hash": first["content_hash"]})[0], 200)
        code, error = self.request("/api/wiki/publish", {
            "change_id": second["change_id"], "expected_hash": second["content_hash"]})
        self.assertEqual((code, error["error"]), (409, "stale_change"))
        self.assertEqual(self.request("/api/wiki/change?id=missing")[0], 404)

    def test_closed_schema_structured_types_and_cross_company_claim(self):
        base = {"company_id": "MSFT", "base_version": 0, "title": "Bad", "statements": self.statements}
        for mutation in (
            {**base, "author": "admin"},
            {**base, "base_version": "0"},
            {**base, "statements": [{"kind": "hypothesis", "text": "x", "claim_ids": [], "role": "admin"}]},
        ):
            self.assertEqual(self.request("/api/wiki/changes", mutation)[0], 400)
        foreign = [{"kind": "evidence_summary", "text": "foreign", "claim_ids": [self.apple_claim]}]
        code, body = self.request("/api/wiki/changes", {**base, "statements": foreign})
        self.assertEqual((code, body["error"]), (400, "foreign_claim_reference"))

    def test_same_origin_is_mandatory_and_evidence_routes_remain_read_only(self):
        payload = {"company_id": "MSFT", "base_version": 0, "title": "No", "statements": []}
        self.assertEqual(self.request("/api/wiki/changes", payload, headers={"Origin": "http://evil.test"})[0], 403)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            body = json.dumps(payload).encode()
            connection.request("POST", "/api/wiki/changes", body=body,
                               headers={"Content-Type": "application/json"})
            self.assertEqual(connection.getresponse().status, 403)
        finally:
            connection.close()
        self.assertEqual(self.request("/api/company?id=MSFT", raw=b"{}", method="POST")[0], 405)

    def test_integrity_gate_and_dynamic_agent_disclosure(self):
        self.assertEqual(self.request("/api/health")[1], {"status": "ok"})
        overview = self.request("/api/overview")[1]
        self.assertEqual(overview["agent"]["live_verification"], "not_performed")
        self.assertTrue(overview["agent"]["chat_implemented"])
        self.assertEqual(overview["agent"]["enabled"], self.request("/api/chat/status")[1]["available"])
        with patch.object(self.server.service, "validate_store", side_effect=RuntimeError("private")):
            code, body = self.request("/api/wiki/changes", {
                "company_id": "MSFT", "base_version": 0, "title": "Blocked", "statements": []})
            self.assertEqual(code, 503)
            self.assertNotIn("private", json.dumps(body))


if __name__ == "__main__":
    unittest.main()
