import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.parse import quote

from workbench.build import build_database
from workbench.server import EvidenceHTTPServer


class Provider:
    def __init__(self):
        self.histories = []
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        return {"available": True, "provider": "scope-http-test", "reason": ""}

    def resolve_research_intent(self, prompt, timeout_seconds):
        """Explicit controlled model adapter; production has no keyword fallback."""
        del timeout_seconds
        question = json.loads(prompt.splitlines()[-1])["question"]
        base = {
            "kind": "company_research", "subjects": [], "directions": [question],
            "metrics": [], "period_text": None, "basis": [], "unit": None,
            "assumptions": [], "uncertainties": [], "excluded_entities": [],
            "is_continuation": False, "identity_ambiguity": False, "summary": question,
        }
        if "行业" in question:
            base.update(kind="industry_research", subjects=[
                {"kind": "industry", "name": "云计算行业", "candidate_id": None}])
        elif "Google" in question:
            base.update(subjects=[{"kind": "company", "name": "Google", "candidate_id": "GOOGL"}],
                        excluded_entities=["Microsoft"])
        elif question == "收入如何":
            base.update(kind="ambiguous_identity", identity_ambiguity=True,
                        subjects=[
                            {"kind": "company", "name": "Microsoft", "candidate_id": "MSFT"},
                            {"kind": "company", "name": "Apple", "candidate_id": "AAPL"},
                        ], uncertainties=["研究对象未明确"])
        else:
            company_id = "AAPL" if "苹果" in question else "MSFT"
            name = "Apple" if company_id == "AAPL" else "Microsoft"
            base["subjects"] = [{"kind": "company", "name": name, "candidate_id": company_id}]
            if "2025财年第四季度" in question:
                base["period_text"] = "2025财年第四季度"
        return {"intent": base, "provenance": {
            "prompt": prompt, "prompt_version": "research-intent/1.0",
            "provider": "controlled-scope-http-model", "model": "controlled-v1",
            "result": base, "usage": {},
        }}

    def stream(self, messages, tools, cancel_event):
        self.histories.append(messages)
        yield {"type": "delta", "text": "test-only answer"}


class ScopeHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cls.evidence = root / "evidence.sqlite3"
        build_database(db_path=cls.evidence)
        cls.provider = Provider()
        cls.server = EvidenceHTTPServer(0, cls.evidence, provider=cls.provider)
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

    def request(self, path, data=None, *, raw=None, headers=None):
        method = "POST" if data is not None or raw is not None else "GET"
        body = json.dumps(data, ensure_ascii=False).encode() if data is not None else raw
        request_headers = {"Origin": self.origin, "Content-Type": "application/json"} if method == "POST" else {}
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=6)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_intake_and_saved_noncompany_task_have_closed_origin_boundary(self):
        exact = "微软2025财年第四季度通过分红和回购向股东返还了多少钱？有什么口径需要注意"
        code, intake = self.request("/api/research/intake", {
            "question": exact, "context_conversation_id": None})
        self.assertEqual(code, 200)
        self.assertEqual((intake["status"], intake["scope"]["subjects"][0]["id"]), ("ready", "MSFT"))
        self.assertEqual(intake["scope"]["period_text"], "2025财年第四季度")

        code, industry = self.request("/api/research/tasks", {
            "question": "分析云计算行业", "context_conversation_id": None, "request_id": "http-industry"})
        self.assertEqual(code, 201)
        self.assertEqual(industry["status"], "unsupported_scope")
        self.assertIsNone(industry["scope"]["subjects"][0]["id"])
        self.assertEqual(self.request("/api/research/tasks/start", {
            "task_id": industry["task_id"], "expected_scope_version": 1})[0], 409)

        self.assertEqual(self.request("/api/research/intake", {
            "question": "微软", "context_conversation_id": None}, headers={"Origin": "http://evil.test"})[0], 403)
        self.assertEqual(self.request("/api/research/intake", raw=b'{"question":"x","question":"y","context_conversation_id":null}')[0], 400)
        self.assertEqual(self.request("/api/research/intake", {
            "question": "微软", "context_conversation_id": None, "company_id": "AAPL"})[0], 400)

    def test_task_create_get_list_idempotency_and_start_schema(self):
        payload = {"question": "微软收入是多少", "context_conversation_id": None, "request_id": "http-msft"}
        code, task = self.request("/api/research/tasks", payload)
        self.assertEqual(code, 201)
        code, replay = self.request("/api/research/tasks", payload)
        self.assertEqual((code, replay["task_id"]), (201, task["task_id"]))
        code, conflict = self.request("/api/research/tasks", {
            **payload, "question": "苹果收入是多少"})
        self.assertEqual((code, conflict["error"]), (409, "idempotency_conflict"))

        code, detail = self.request("/api/research/task?id=" + quote(task["task_id"]))
        self.assertEqual((code, detail["question"], detail["runs"]), (200, payload["question"], []))
        code, listing = self.request("/api/research/tasks")
        self.assertEqual(code, 200)
        self.assertTrue(any(item["task_id"] == task["task_id"] for item in listing["tasks"]))

        code, started = self.request("/api/research/tasks/start", {
            "task_id": task["task_id"], "expected_scope_version": 1})
        self.assertEqual(code, 202)
        self.assertEqual(set(started), {"task_id", "scope_version", "company_id", "company_ids", "conversation_id", "run_id"})
        self.assertEqual(started["company_ids"], ["MSFT"])
        self.assertEqual(started["company_id"], "MSFT")
        code, repeated = self.request("/api/research/tasks/start", {
            "task_id": task["task_id"], "expected_scope_version": 1})
        self.assertEqual((code, repeated), (202, started))

        for _ in range(100):
            _, run = self.request("/api/chat/run?id=" + quote(started["run_id"]))
            if run["status"] not in {"queued", "running"}:
                break
            time.sleep(.01)
        self.assertEqual(run["status"], "completed")
        _, detail = self.request("/api/research/task?id=" + quote(task["task_id"]))
        self.assertEqual(detail["runs"][0]["status"], "completed")

    def test_origin_filters_lists_but_keeps_machine_details_auditable(self):
        user_payload = {
            "question": "苹果收入是多少", "context_conversation_id": None,
            "request_id": "http-origin-user",
        }
        code, user = self.request("/api/research/tasks", user_payload)
        self.assertEqual((code, user["origin"]), (201, "user"))
        machine_payload = {
            "question": "微软收入是多少", "context_conversation_id": None,
            "request_id": "research-matrix-http-origin", "origin": "machine_acceptance",
        }
        code, machine = self.request("/api/research/tasks", machine_payload)
        self.assertEqual((code, machine["origin"]), (201, "machine_acceptance"))
        code, conflict = self.request("/api/research/tasks", {**machine_payload, "origin": "user"})
        self.assertEqual((code, conflict["error"]), (409, "idempotency_conflict"))
        code, started = self.request("/api/research/tasks/start", {
            "task_id": machine["task_id"], "expected_scope_version": 1})
        self.assertEqual(code, 202)

        default_tasks = self.request("/api/research/tasks")[1]["tasks"]
        internal_tasks = self.request("/api/research/tasks?include_internal=true")[1]["tasks"]
        self.assertTrue(any(item["task_id"] == user["task_id"] for item in default_tasks))
        self.assertFalse(any(item["task_id"] == machine["task_id"] for item in default_tasks))
        self.assertTrue(any(item["task_id"] == machine["task_id"] for item in internal_tasks))
        default_conversations = self.request("/api/chat/conversations?company=MSFT")[1]["conversations"]
        internal_conversations = self.request(
            "/api/chat/conversations?company=MSFT&include_internal=true")[1]["conversations"]
        self.assertFalse(any(item["conversation_id"] == started["conversation_id"]
                             for item in default_conversations))
        self.assertTrue(any(item["conversation_id"] == started["conversation_id"]
                            for item in internal_conversations))
        detail = self.request("/api/research/task?id=" + quote(machine["task_id"]))[1]
        original = self.request("/api/chat/conversation?id=" + quote(started["conversation_id"]))[1]
        self.assertEqual((detail["origin"], original["conversation_id"]),
                         ("machine_acceptance", started["conversation_id"]))
        self.assertEqual(self.request("/api/research/tasks?include_internal=1")[0], 400)
        self.assertEqual(self.request("/api/chat/conversations?include_internal=yes")[0], 400)

    def test_retry_intent_route_is_strict_and_updates_same_task(self):
        original = self.server.research_tasks._intent_resolver
        try:
            self.server.research_tasks._intent_resolver = (
                lambda prompt, timeout_seconds: (_ for _ in ()).throw(RuntimeError("controlled")))
            code, failed = self.request("/api/research/tasks", {
                "question": "原问题必须保留", "context_conversation_id": None,
                "request_id": "http-retry-intent",
            })
            self.assertEqual((code, failed["status"], failed["current_scope_version"]),
                             (201, "model_unavailable", 1))
            self.assertEqual(self.request("/api/research/tasks/retry-intent", {
                "task_id": failed["task_id"]})[0], 400)
            self.assertEqual(self.request("/api/research/tasks/retry-intent", {
                "task_id": failed["task_id"], "expected_scope_version": 1,
                "request_id": "not-accepted"})[0], 400)
            self.server.research_tasks._intent_resolver = original
            code, retried = self.request("/api/research/tasks/retry-intent", {
                "task_id": failed["task_id"], "expected_scope_version": 1})
            self.assertEqual((code, retried["task_id"], retried["question"],
                              retried["current_scope_version"]),
                             (200, failed["task_id"], "原问题必须保留", 2))
            replay = self.request("/api/research/tasks/retry-intent", {
                "task_id": failed["task_id"], "expected_scope_version": 1})
            self.assertEqual((replay[0], replay[1]["current_scope_version"]), (200, 2))
            self.assertEqual(retried["runs"], [])
        finally:
            self.server.research_tasks._intent_resolver = original

    def test_resolve_cas_and_legacy_cross_company_guard(self):
        code, ambiguous = self.request("/api/research/tasks", {
            "question": "收入如何", "context_conversation_id": None, "request_id": "http-ambiguous"})
        self.assertEqual((code, ambiguous["status"]), (201, "needs_clarification"))
        code, resolved = self.request("/api/research/tasks/resolve", {
            "task_id": ambiguous["task_id"], "expected_scope_version": 1, "company_id": "AAPL"})
        self.assertEqual((code, resolved["current_scope_version"], resolved["status"]), (200, 2, "ready"))
        self.assertEqual(self.request("/api/research/tasks/resolve", {
            "task_id": ambiguous["task_id"], "expected_scope_version": 1, "company_id": "MSFT"})[0], 409)

        _, conversation = self.request("/api/chat/conversations", {"company_id": "AAPL"})
        before = self.provider.status_calls
        code, error = self.request("/api/chat/messages", {
            "conversation_id": conversation["conversation_id"], "message": "微软收入是多少"})
        self.assertEqual((code, error["error"]), (409, "scope_mismatch"))
        self.assertEqual(self.provider.status_calls, before)
        _, stored = self.request("/api/chat/conversation?id=" + quote(conversation["conversation_id"]))
        self.assertEqual(stored["messages"], [])

        exclusion_question = "不要研究微软，请分析 Google 的收入趋势"
        code, excluded = self.request("/api/research/tasks", {
            "question": exclusion_question, "context_conversation_id": conversation["conversation_id"],
            "request_id": "http-excluded-msft",
        })
        self.assertEqual((code, excluded["status"], excluded["code"]),
                         (201, "unsupported_scope", "company_not_covered"))
        before = self.provider.status_calls
        code, error = self.request("/api/research/tasks/start", {
            "task_id": excluded["task_id"], "expected_scope_version": 1})
        self.assertEqual((code, error["error"]), (409, "task_not_ready"))
        self.assertEqual(self.provider.status_calls, before)


if __name__ == "__main__":
    unittest.main()
