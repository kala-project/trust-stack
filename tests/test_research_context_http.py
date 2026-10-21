import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from urllib.parse import quote

from workbench.build import build_database
from workbench.server import EvidenceHTTPServer
from tests.support_chat_provider import ExplicitMSFTIntentAdapter


class Provider(ExplicitMSFTIntentAdapter):
    def __init__(self):
        self.fail_next = False

    def status(self):
        return {"available": True, "provider": "test", "reason": ""}

    def stream(self, messages, tools, cancel_event):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("later run failed")
        result = next(item for item in tools if item["name"] == "get_company_facts")["handler"](
            metric="revenue")
        yield {"type": "delta", "text": f'Supported [evidence:{result["facts"][0]["evidence_id"]}]'}


class ResearchContextHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cls.evidence = root / "evidence.sqlite3"
        build_database(db_path=cls.evidence)
        cls.server = EvidenceHTTPServer(0, cls.evidence, provider=Provider())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port
        cls.origin = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)
        cls.temp.cleanup()

    def request(self, path, data=None):
        headers = {"Origin": self.origin, "Content-Type": "application/json"} if data is not None else {}
        body = json.dumps(data).encode() if data is not None else None
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=6)
        try:
            connection.request("POST" if data is not None else "GET", path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read().decode()
            return response.status, response.getheader("Content-Type"), raw
        finally:
            connection.close()

    def json_request(self, path, data=None):
        status, content_type, raw = self.request(path, data)
        return status, json.loads(raw)

    def answer(self, company="MSFT"):
        status, conversation = self.json_request("/api/chat/conversations", {"company_id": company})
        self.assertEqual(status, 201)
        conversation_id = conversation["conversation_id"]
        status, submitted = self.json_request("/api/chat/messages", {
            "conversation_id": conversation_id, "message": "Question from server context"})
        self.assertEqual(status, 202)
        for _ in range(200):
            status, run = self.json_request("/api/chat/run?id=" + quote(submitted["run_id"]))
            if run["status"] not in {"queued", "running"}:
                break
            time.sleep(.01)
        self.assertEqual(run["status"], "completed")
        _, conversation = self.json_request("/api/chat/conversation?id=" + quote(conversation_id))
        return conversation_id, conversation["messages"][-1], conversation

    def test_citation_reader_and_scope_are_answer_bound(self):
        conversation_id, message, conversation = self.answer()
        self.assertEqual(conversation["research_scope"]["scope_type"], "frozen_per_run")
        self.assertEqual(len(conversation["research_scope"]["runs"]), 1)
        self.assertEqual(conversation["research_scope"]["latest_snapshot"]["message_id"],
                         message["message_id"])
        citation = message["citations"][0]
        path = ("/api/chat/citation?conversation=" + quote(conversation_id) + "&message=" +
                quote(message["message_id"]) + "&kind=" + citation["kind"] + "&id=" + quote(citation["id"]))
        status, result = self.json_request(path)
        self.assertEqual(status, 200)
        self.assertEqual(result["detail_schema_version"], "citation-detail/1")
        for field in ("company_id", "conversation_id", "message_id", "kind", "id", "title",
                      "source_url", "published_at", "collected_at", "source_sha256", "document_version_id",
                      "quote", "context", "offset", "locator", "parser_version", "rights_status", "tables", "limitations"):
            self.assertIn(field, result)
        self.assertEqual(result["message_id"], message["message_id"])

        other_id, other_message, _ = self.answer("AAPL")
        foreign = path.replace(quote(conversation_id), quote(other_id), 1)
        status, error = self.json_request(foreign)
        self.assertEqual(status, 404)
        self.assertNotIn("MSFT", json.dumps(error))
        swapped = path.rsplit("=", 1)[0] + "=" + quote(other_message["citations"][0]["id"])
        self.assertEqual(self.json_request(swapped)[0], 404)
        forged = path.rsplit("=", 1)[0] + "=forged"
        self.assertEqual(self.json_request(forged)[0], 404)

    def test_artifact_http_is_readonly_exact_answer_and_closed_parameters(self):
        conversation_id, message, _ = self.answer()
        path = ('/api/research/artifact?conversation_id=' + conversation_id
                + '&message_id=' + message['message_id'])
        status, artifact = self.json_request(path)
        self.assertEqual(status, 200)
        self.assertEqual(artifact['message_id'], message['message_id'])
        self.assertEqual(artifact['conversation_id'], conversation_id)
        self.assertEqual(artifact['schema_version'], 'answer-artifact/1.0')
        self.assertEqual(artifact['verification']['status'], 'needs_review')
        self.assertTrue(artifact['datasets'])
        self.assertTrue(any(d['chart'] for d in artifact['datasets']))
        self.assertEqual(self.json_request(path)[1], artifact)
        self.assertEqual(self.json_request(path + '&company_id=AAPL')[0], 400)
        self.assertEqual(self.json_request(path + '&message_id=forged')[0], 400)
        self.assertEqual(self.json_request('/api/research/artifact')[0], 400)
        other_id, _, _ = self.answer('AAPL')
        self.assertEqual(self.json_request(path.replace(conversation_id, other_id))[0], 404)
        status, saved = self.json_request('/api/research/results', {
            'conversation_id': conversation_id, 'message_id': message['message_id'],
            'title': 'Freeze artifact', 'body': message['content'], 'limitations': []})
        self.assertEqual(status, 201)
        self.assertEqual(saved['artifact'], artifact)

    def test_result_http_lifecycle_and_exports_are_exact_versions(self):
        conversation_id, message, _ = self.answer()
        payload = {"conversation_id": conversation_id, "message_id": message["message_id"],
                   "title": "<script>alert(1)</script>", "body": "=cmd|' /C calc'!A0", "limitations": ["<img src=x>"]}
        status, first = self.json_request("/api/research/results", payload)
        self.assertEqual(status, 201)
        self.assertEqual(first["human_review_status"], "unreviewed")
        scope = {"conversation_id": conversation_id, "company_id": "MSFT"}
        status, second = self.json_request("/api/research/results/revise", {
            "result_id": first["result_id"], **scope, "expected_version": 1,
            "title": "second", "body": "changed", "limitations": []})
        self.assertEqual((status, second["version"]), (200, 2))
        self.assertEqual(self.json_request("/api/research/results/revise", {
            "result_id": first["result_id"], **scope, "expected_version": 1,
            "title": "race", "body": "race", "limitations": []})[0], 409)
        status, listed = self.json_request("/api/research/results?conversation=" + quote(conversation_id))
        self.assertEqual((status, listed["results"][0]["version"]), (200, 2))

        result_scope = ("&conversation_id=" + quote(conversation_id) +
                        "&company_id=MSFT")
        base = "/api/research/export?id=" + quote(first["result_id"]) + result_scope + "&version=1&format="
        status, content_type, html = self.request(base + "html")
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("text/html"))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertNotIn("changed", html)
        status, _, csv_body = self.request(base + "csv")
        self.assertEqual(status, 200)
        self.assertIn("'=cmd", csv_body)
        status, _, json_body = self.request(base + "json")
        self.assertEqual(json.loads(json_body)["version"], 1)
        self.assertEqual(json.loads(json_body)["body"], payload["body"])
        status, stored = self.json_request(
            "/api/research/result?id=" + quote(first["result_id"]) + result_scope + "&version=1")
        self.assertEqual((status, stored["version"], stored["body"]), (200, 1, payload["body"]))

    def test_result_scope_is_required_and_cross_scope_is_uniformly_not_found(self):
        conversation_id, message, _ = self.answer()
        status, created = self.json_request("/api/research/results", {
            "conversation_id": conversation_id, "message_id": message["message_id"],
            "title": "original", "body": "stored", "limitations": []})
        self.assertEqual(status, 201)
        other_conversation, _, _ = self.answer("MSFT")
        result_id = quote(created["result_id"])
        good_scope = {"conversation_id": conversation_id, "company_id": "MSFT"}
        bad_scopes = (
            {"conversation_id": other_conversation, "company_id": "MSFT"},
            {"conversation_id": conversation_id, "company_id": "AAPL"},
        )
        for scope in bad_scopes:
            query = ("&conversation_id=" + quote(scope["conversation_id"]) +
                     "&company_id=" + quote(scope["company_id"]))
            with self.subTest(scope=scope, operation="get"):
                self.assertEqual(self.json_request("/api/research/result?id=" + result_id + query)[0], 404)
            with self.subTest(scope=scope, operation="export"):
                self.assertEqual(self.request(
                    "/api/research/export?id=" + result_id + query + "&version=1&format=json")[0], 404)
            with self.subTest(scope=scope, operation="revise"):
                self.assertEqual(self.json_request("/api/research/results/revise", {
                    "result_id": created["result_id"], **scope, "expected_version": 1,
                    "title": "wrong scope", "body": "must not persist", "limitations": []})[0], 404)

        self.assertEqual(self.json_request("/api/research/result?id=" + result_id)[0], 400)
        self.assertEqual(self.request(
            "/api/research/export?id=" + result_id + "&version=1&format=json")[0], 400)
        missing_scope_revision = {
            "result_id": created["result_id"], "expected_version": 1,
            "title": "missing", "body": "missing", "limitations": []}
        self.assertEqual(self.json_request(
            "/api/research/results/revise", missing_scope_revision)[0], 400)

        query = ("&conversation_id=" + quote(good_scope["conversation_id"]) +
                 "&company_id=" + quote(good_scope["company_id"]))
        status, stored = self.json_request("/api/research/result?id=" + result_id + query)
        self.assertEqual((status, stored["version"], stored["body"]), (200, 1, "stored"))

    def test_latest_snapshot_remains_bound_to_completed_answer_after_later_failure(self):
        conversation_id, answer, _ = self.answer()
        first_run_id = answer["run_id"]
        self.server.chat.provider.fail_next = True
        status, submitted = self.json_request("/api/chat/messages", {
            "conversation_id": conversation_id, "message": "This later run must fail"})
        self.assertEqual(status, 202)
        for _ in range(200):
            status, run = self.json_request("/api/chat/run?id=" + quote(submitted["run_id"]))
            if run["status"] not in {"queued", "running"}:
                break
            time.sleep(.01)
        self.assertEqual(run["status"], "failed")
        status, conversation = self.json_request(
            "/api/chat/conversation?id=" + quote(conversation_id))
        self.assertEqual(status, 200)
        scope = conversation["research_scope"]
        self.assertEqual(scope["latest_snapshot"]["run_id"], first_run_id)
        self.assertEqual(scope["latest_snapshot"]["message_id"], answer["message_id"])
        self.assertEqual([item["status"] for item in scope["runs"]], ["completed", "failed"])

    def test_task_conversation_exposes_user_question_not_execution_prompt(self):
        user_question = "MSFT FY2025 Q4 AI基础设施投入会不会持续挤压云厂商现金流？"
        execution_prompt = (
            "研究主体固定为MSFT。请只使用已登记文档库中的内部执行范围，"
            "再执行工具并发布结构化报告。"
        )
        status, task = self.json_request("/api/research/tasks", {
            "question": user_question, "context_conversation_id": None, "request_id": "display-question-test"})
        self.assertEqual(status, 201)
        status, bridge = self.json_request("/api/research/tasks/start", {
            "task_id": task["task_id"], "expected_scope_version": task["current_scope_version"]})
        self.assertEqual(status, 202)
        for _ in range(200):
            status, run = self.json_request("/api/chat/run?id=" + quote(bridge["run_id"]))
            if run["status"] not in {"queued", "running"}:
                break
            time.sleep(.01)
        self.assertEqual(run["status"], "completed")

        with sqlite3.connect(self.server.chat.chat_db) as connection:
            connection.execute(
                "UPDATE messages SET content=? WHERE conversation_id=? AND run_id=? AND role='user'",
                (execution_prompt, bridge["conversation_id"], bridge["run_id"]),
            )

        status, conversation = self.json_request(
            "/api/chat/conversation?id=" + quote(bridge["conversation_id"]))
        self.assertEqual(status, 200)
        self.assertEqual(conversation["display_question"], user_question)
        self.assertEqual(conversation["messages"][0]["content"], execution_prompt)
        answer = conversation["messages"][-1]
        status, artifact = self.json_request(
            "/api/research/artifact?conversation_id=" + quote(bridge["conversation_id"])
            + "&message_id=" + quote(answer["message_id"]))
        self.assertEqual(status, 200)
        self.assertEqual(artifact["question"], user_question)
        self.assertNotEqual(artifact["question"], execution_prompt)

    def test_result_posts_require_exact_shape_and_same_origin(self):
        conversation_id, message, _ = self.answer()
        payload = {"conversation_id": conversation_id, "message_id": message["message_id"],
                   "title": "x", "body": "x", "limitations": [], "company_id": "AAPL"}
        self.assertEqual(self.json_request("/api/research/results", payload)[0], 400)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            body = json.dumps({key: value for key, value in payload.items() if key != "company_id"})
            connection.request("POST", "/api/research/results", body=body,
                               headers={"Content-Type": "application/json"})
            self.assertEqual(connection.getresponse().status, 403)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
