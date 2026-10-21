"""Temporary HTTP coverage for durable candidate identities and the shared reader."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import urlencode

from workbench.build import build_database
from workbench.server import EvidenceHTTPServer
from workbench.worker import DurableWorker
from tests.test_verification_executor import ControlledAuthorVerifier


class HTTPControlledProvider(ControlledAuthorVerifier):
    """Deterministic verifier that records positive checks after a controlled read."""
    def stream_verification(self, messages, tools, cancel_event):
        packet = json.loads(messages[0]["content"])
        handlers = {tool["name"]: tool["handler"] for tool in tools}
        result = handlers["search_evidence"](query="candidate content")
        receipt = result["_verification_receipt"]
        for node_id in packet["content_nodes"]:
            for check_kind in packet["verification_checks"][node_id]["required"]:
                handlers["record_verification_finding"](
                    version_id=packet["version_id"], node_id=node_id,
                    check_kind=check_kind,
                    outcome="supported", explanation="Checked in controlled frozen scope.",
                    receipt_ids=[receipt["identifier"]])
        handlers["finalize_verification"](
            version_id=packet["version_id"], outcome="verified", limitations=[],
            semantic_review={"original_question_coverage": "Controlled content covers the test question."},
            verification={"method": "controlled-http-test", "semantic_truth_guaranteed": False})
        yield {"type": "usage", "tokens": 1}


class VerificationHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cls.evidence = root / "evidence.sqlite3"
        build_database(db_path=cls.evidence)
        cls.provider = HTTPControlledProvider()
        cls.server = EvidenceHTTPServer(
            0, cls.evidence, chat_db=root / "chat.sqlite3", wiki_db=root / "wiki.sqlite3",
            sample_db=root / "sample.sqlite3", documents_root=root / "documents",
            provider=cls.provider)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port
        cls.origin = f"http://127.0.0.1:{cls.port}"
        cls.worker = DurableWorker(cls.server.chat, worker_id="http-controlled", heartbeat_seconds=.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)
        cls.temp.cleanup()

    def request(self, method, path, body=None):
        raw = json.dumps(body).encode() if body is not None else None
        headers = ({"Content-Type": "application/json", "Origin": self.origin}
                   if body is not None else {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def create_submission(self, suffix):
        status, conversation = self.request("POST", "/api/chat/conversations", {"company_id": "MSFT"})
        self.assertEqual(status, 201)
        status, submitted = self.request("POST", "/api/chat/messages", {
            "conversation_id": conversation["conversation_id"], "message": "controlled request",
            "request_id": f"http-{suffix}",
        })
        self.assertEqual(status, 202)
        return conversation, submitted

    def candidate_detail(self, conversation_id):
        status, listed = self.request("GET", "/api/verifications?" + urlencode({
            "conversation_id": conversation_id, "limit": 50,
        }))
        self.assertEqual(status, 200)
        self.assertEqual(listed["conversation_id"], conversation_id)
        self.assertEqual(len(listed["candidates"]), 1)
        candidate_id = listed["candidates"][0]["candidate_id"]
        status, candidate = self.request("GET", "/api/verification/candidate?" + urlencode({
            "conversation_id": conversation_id, "candidate_id": candidate_id,
        }))
        self.assertEqual(status, 200)
        return candidate

    def test_submit_candidate_reader_verify_formal_and_strict_ownership(self):
        conversation, submitted = self.create_submission("formal")
        self.assertTrue(self.worker.run_once())
        candidate = self.candidate_detail(conversation["conversation_id"])
        self.assertEqual(candidate["status"], "queued")
        self.assertIsNone(candidate["formal_message_id"])
        self.assertEqual(candidate["author_run_id"], submitted["run_id"])
        version = candidate["versions"][-1]
        identity = {"conversation_id": conversation["conversation_id"],
                    "candidate_id": candidate["candidate_id"], "version_id": version["version_id"]}
        status, canonical = self.request("GET", "/api/verification/version?" + urlencode(identity))
        self.assertEqual(status, 200)
        self.assertEqual(canonical["publication"]["title"], "Candidate")
        status, reader = self.request("GET", "/api/verification/reader?" + urlencode(identity))
        self.assertEqual(status, 200)
        self.assertEqual(reader["schema_version"], "report-reader-payload/1")
        self.assertEqual(reader["report"]["kind"], "verification_candidate")
        self.assertEqual(reader["report"]["version_id"], version["version_id"])
        self.assertIn("Limited.", json.dumps(reader["chapters"], ensure_ascii=False))
        self.assertEqual(self.request("GET", "/api/verification/reader?" + urlencode({**identity, "extra": "x"}))[0], 400)
        other_status, other = self.request("POST", "/api/chat/conversations", {"company_id": "MSFT"})
        self.assertEqual(other_status, 201)
        self.assertEqual(self.request("GET", "/api/verification/candidate?" + urlencode({
            "conversation_id": other["conversation_id"], "candidate_id": candidate["candidate_id"],
        }))[0], 404)

        self.assertTrue(self.worker.run_once())
        refreshed = self.candidate_detail(conversation["conversation_id"])
        self.assertEqual(refreshed["status"], "verified")
        self.assertIsNotNone(refreshed["formal_message_id"])
        status, persisted = self.request("GET", f"/api/chat/conversation?id={conversation['conversation_id']}")
        self.assertEqual(status, 200)
        self.assertEqual([item["message_id"] for item in persisted["messages"] if item["role"] == "assistant"],
                         [refreshed["formal_message_id"]])

    def test_budget_failure_can_continue_once_from_latest_version(self):
        conversation, _ = self.create_submission("continue")
        self.assertTrue(self.worker.run_once())
        candidate = self.candidate_detail(conversation["conversation_id"])
        old_job_id = candidate["verify_job_id"]
        failed_claim = self.server.chat.jobs.claim("forced-budget", job_types=("verify",))
        self.assertEqual(failed_claim.job_id, old_job_id)
        with closing(self.server.chat._connect()) as connection, connection:
            self.server.chat.jobs.fail_in_transaction(
                connection, failed_claim,
                {"code": "verification_budget_exhausted", "message": "controlled budget"},
                retryable=False)
        self.server.chat.jobs.release_runtime_slot(failed_claim)

        body = {
            "conversation_id": conversation["conversation_id"],
            "candidate_id": candidate["candidate_id"],
            "expected_version_id": candidate["versions"][-1]["version_id"],
            "request_id": "continue-http-budget",
        }
        status, continued = self.request("POST", "/api/verification/continue", body)
        self.assertEqual(status, 202)
        self.assertEqual(continued["previous_verify_job_id"], old_job_id)
        self.assertNotEqual(continued["verify_job_id"], old_job_id)
        self.assertEqual(self.request("POST", "/api/verification/continue", body),
                         (status, continued))
        refreshed = self.candidate_detail(conversation["conversation_id"])
        self.assertEqual(refreshed["verify_job_id"], continued["verify_job_id"])
        self.assertEqual(refreshed["status"], "queued")

        self.assertTrue(self.worker.run_once())
        published = self.candidate_detail(conversation["conversation_id"])
        self.assertEqual(published["status"], "verified")
        self.assertIsNotNone(published["formal_message_id"])

    def test_cancel_is_persisted_and_visible_after_reconnect(self):
        conversation, _ = self.create_submission("cancel")
        self.assertTrue(self.worker.run_once())
        candidate = self.candidate_detail(conversation["conversation_id"])
        status, cancelled = self.request("POST", "/api/verification/cancel", {
            "conversation_id": conversation["conversation_id"],
            "candidate_id": candidate["candidate_id"],
        })
        self.assertEqual(status, 200)
        self.assertEqual(cancelled["candidate_id"], candidate["candidate_id"])
        reconnected = self.candidate_detail(conversation["conversation_id"])
        self.assertEqual(reconnected["status"], "unresolved")
        status, persisted = self.request("GET", f"/api/chat/conversation?id={conversation['conversation_id']}")
        self.assertEqual(status, 200)
        self.assertFalse(any(item["role"] == "assistant" for item in persisted["messages"]))


if __name__ == "__main__":
    unittest.main()
