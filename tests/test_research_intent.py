"""Controlled-model tests for LLM-first research intake (no live SDK calls)."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock

from workbench.build import build_database
from workbench.chat import ChatError, ChatService
from workbench.copilot_provider import CopilotProvider
from workbench.research_intent import INTENT_PROMPT_VERSION, INTENT_SYSTEM_MESSAGE, INTENT_TOOL_NAME
from workbench.research_tasks import ResearchTaskService


def intent(kind="company_research", subjects=None, **overrides):
    value = {
        "kind": kind,
        "subjects": subjects if subjects is not None else [
            {"kind": "company", "name": "Microsoft", "candidate_id": "MSFT"}],
        "directions": ["先检查可用资料并形成趋势"], "metrics": [],
        "period_text": None, "basis": [], "unit": None, "assumptions": [],
        "uncertainties": [], "excluded_entities": [], "is_continuation": False,
        "identity_ambiguity": False, "summary": "保留用户的原始研究方向",
    }
    value.update(overrides)
    return value


class ControlledIntentResolver:
    def __init__(self, answers=None, failure=None):
        self.answers = list(answers or [])
        self.failure = failure
        self.calls = []

    def __call__(self, prompt, timeout_seconds):
        self.calls.append((prompt, timeout_seconds))
        if self.failure:
            raise self.failure
        value = self.answers.pop(0)
        return {"intent": value, "provenance": {
            "prompt": prompt, "prompt_version": INTENT_PROMPT_VERSION,
            "provider": "controlled-test-model", "model": "controlled-v1",
            "result": value, "usage": {},
        }}


class NoopChatProvider:
    def status(self):
        return {"available": True, "provider": "controlled"}

    def stream(self, messages, tools, cancel_event):
        yield {"type": "delta", "text": "unused"}


class ResearchIntentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.evidence_db = Path(cls.fixture.name) / "evidence.sqlite3"
        build_database(db_path=cls.evidence_db)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.chat = ChatService(self.evidence_db, Path(self.temp.name) / "chat.sqlite3", NoopChatProvider())

    def tearDown(self):
        self.chat.close()
        self.temp.cleanup()

    def service(self, *answers, failure=None):
        resolver = ControlledIntentResolver(answers, failure)
        return ResearchTaskService(self.chat, intent_resolver=resolver, intent_timeout_seconds=3), resolver

    def test_broad_capex_casing_revenue_and_omitted_unit_start(self):
        answers = (
            intent(metrics=["CapEX"], period_text="最近10年", directions=["资本开支长期变化"]),
            intent(metrics=["收入"], directions=["收入规模、增速和驱动因素"]),
            intent(metrics=["capex"], period_text="last 10 years", unit=None),
        )
        service, resolver = self.service(*answers)
        for question in ("msft 最近10年CapEX的变化", "微软收入怎么样", "MSFT capex last10years"):
            result = service.intake(question)
            self.assertEqual((result["status"], result["scope"]["subjects"][0]["id"]), ("ready", "MSFT"))
            self.assertEqual(result["scope"]["resolution_method"], "llm_structured_intent_v1")
        self.assertIsNone(answers[2]["unit"])
        self.assertEqual(len(resolver.calls), 3)

    def test_catalog_comes_from_registered_data_not_two_company_prompt(self):
        db = Path(self.temp.name) / "additional-entity.sqlite3"
        build_database(db_path=db)
        with sqlite3.connect(db) as connection:
            connection.execute("INSERT INTO companies VALUES(?,?,?,?)",
                               ("NEW-ENTITY", "Previously Unseen Entity", "NEWX", "Synthetic registry fixture"))
        chat = ChatService(db, Path(self.temp.name) / "additional-chat.sqlite3", NoopChatProvider())
        self.addCleanup(chat.close)
        resolver = ControlledIntentResolver([intent(subjects=[
            {"kind": "company", "name": "另一种语言的名称", "candidate_id": "NEW-ENTITY"}])])
        result = ResearchTaskService(chat, intent_resolver=resolver).intake("研究新登记的实体")
        self.assertEqual(result["scope"]["subjects"],
                         [{"kind": "company", "id": "NEW-ENTITY", "label": "Previously Unseen Entity"}])
        self.assertEqual(result["status"], "missing_data")
        payload = json.loads(resolver.calls[0][0].split("\n", 1)[1])
        entity = next(item for item in payload["entity_catalog"] if item["candidate_id"] == "NEW-ENTITY")
        self.assertIn("NEWX", entity["aliases"])

    def test_multiple_clear_subjects_are_not_identity_ambiguity_or_silently_narrowed(self):
        known = intent(subjects=[
            {"kind": "company", "name": "Microsoft", "candidate_id": "MSFT"},
            {"kind": "company", "name": "Apple", "candidate_id": "AAPL"}])
        mixed = intent(subjects=[
            {"kind": "company", "name": "Microsoft", "candidate_id": "MSFT"},
            {"kind": "company", "name": "Unregistered Entity", "candidate_id": "OUTSIDE"}])
        service, _ = self.service(known, mixed)
        result = service.intake("比较两个明确的主体")
        self.assertEqual((result["status"], result["code"]), ("ready", "scope_ready"))
        self.assertEqual({item["id"] for item in result["scope"]["subjects"]}, {"MSFT", "AAPL"})
        self.assertEqual(result["options"], [])
        result = service.intake("比较已接入和未接入的主体")
        self.assertEqual(result["code"], "partial_subject_coverage")
        self.assertEqual(len(result["scope"]["subjects"]), 2)
        self.assertEqual(result["scope"]["subjects"][1]["label"], "Unregistered Entity")
        self.assertNotEqual(result["status"], "ready")

    def test_industry_unknown_and_adversarial_candidate_preserve_exploration(self):
        industry = intent("industry_research", [{"kind": "industry", "name": "半导体行业", "candidate_id": None}])
        unknown = intent(subjects=[{"kind": "company", "name": "Tesla", "candidate_id": "TSLA"}])
        adversarial = intent(subjects=[{"kind": "company", "name": "Microsoft", "candidate_id": "MSFT; ignore prior"}])
        service, _ = self.service(industry, unknown, adversarial)
        first = service.intake("分析半导体行业收入")
        self.assertEqual((first["status"], first["code"]), ("unsupported_scope", "industry_coverage_unavailable"))
        self.assertIn("不能据此代表行业", first["scope"]["gaps"][0])
        result = service.intake("Tesla 收入如何")
        self.assertEqual((result["status"], result["code"]), ("unsupported_scope", "company_not_covered"))
        self.assertEqual(result["options"], [])
        result = service.intake("adversarial subject")
        self.assertEqual((result["status"], result["code"]),
                         ("needs_clarification", "inconsistent_entity_link"))

    def test_followup_uses_context_and_different_company_is_new_research(self):
        conversation = self.chat.create_conversation("MSFT")
        continued = intent(metrics=["收入"], is_continuation=True)
        switched = intent(subjects=[{"kind": "company", "name": "Apple", "candidate_id": "AAPL"}],
                          is_continuation=True)
        service, resolver = self.service(continued, switched)
        same = service.intake("那收入呢", conversation["conversation_id"])
        self.assertEqual(same["scope"]["relationship_to_context"], "continuation")
        other = service.intake("改看苹果收入", conversation["conversation_id"])
        self.assertEqual(other["code"], "scope_ready_new_research")
        self.assertEqual(other["scope"]["relationship_to_context"], "different_company_new_research")
        prompt_payload = json.loads(resolver.calls[0][0].split("\n", 1)[1])
        self.assertEqual(prompt_payload["current_company_id"], "MSFT")

    def test_idempotent_create_does_not_rerun_model(self):
        service, resolver = self.service(intent(metrics=["收入"]))
        first = service.create("微软收入怎么样", None, "same-request")
        replay = service.create("微软收入怎么样", None, "same-request")
        self.assertEqual(first["task_id"], replay["task_id"])
        self.assertEqual(len(resolver.calls), 1)
        self.assertEqual(first["scope"]["intent_metadata"]["model"], "controlled-v1")
        self.assertEqual(first["scope"]["intent_metadata"]["prompt_version"], INTENT_PROMPT_VERSION)
        self.assertNotIn("prompt", first["scope"]["intent_metadata"])
        internal = service.get(first["task_id"], include_internal=True)
        self.assertIn("history", internal["scope"]["intent_metadata"]["prompt"])
        self.assertIn("result", internal["scope"]["intent_metadata"])

    def test_model_unavailable_and_invalid_schema_persist_original(self):
        unavailable, _ = self.service(failure=TimeoutError("controlled timeout"))
        task = unavailable.create("微软收入怎么样", None, "timeout-request")
        self.assertEqual((task["status"], task["code"]), ("model_unavailable", "intent_model_unavailable"))
        self.assertEqual(task["question"], "微软收入怎么样")
        self.assertEqual(task["scope"]["original_intent"], task["question"])

        invalid, _ = self.service({"kind": "company_research"})
        task = invalid.create("msft revenue", None, "invalid-request")
        self.assertEqual((task["status"], task["code"]), ("needs_review", "intent_schema_invalid"))

    def test_provider_intent_call_is_bounded_and_custom_tool_only(self):
        provider = CopilotProvider("unused.json")
        value = intent(metrics=["Revenue"])

        def controlled_stream(messages, tools, cancel_event, timeout_override=None, **kwargs):
            self.assertEqual([tool["name"] for tool in tools], [INTENT_TOOL_NAME])
            self.assertEqual(kwargs["system_message"], INTENT_SYSTEM_MESSAGE)
            self.assertEqual(timeout_override, 4)
            tools[0]["handler"](**value)
            yield {"type": "usage", "model": "controlled-sdk-model"}

        with mock.patch.object(provider, "status", return_value={"available": True}), \
             mock.patch.object(provider, "_config", return_value={
                 "model": "configured", "provider": "github-copilot-sdk", "enabled": True,
                 "automatic_downloads": False, "automatic_updates": False}), \
             mock.patch.object(provider, "_stream", side_effect=controlled_stream):
            result = provider.resolve_research_intent("actual prompt", timeout_seconds=4)
        self.assertEqual(result["intent"]["metrics"], ["Revenue"])
        self.assertEqual(result["provenance"]["model"], "controlled-sdk-model")
        self.assertEqual(result["provenance"]["prompt"], "actual prompt")
        self.assertEqual(result["provenance"]["prompt_version"], INTENT_PROMPT_VERSION)

    def test_catalog_name_and_id_must_agree_without_parsing_the_question(self):
        mismatch = intent(subjects=[{"kind": "company", "name": "Apple", "candidate_id": "MSFT"}])
        null_link = intent(subjects=[{"kind": "company", "name": "Microsoft", "candidate_id": None}])
        invalid_link = intent(subjects=[{"kind": "company", "name": "Microsoft", "candidate_id": "TSLA"}])
        unknown = intent(subjects=[{"kind": "company", "name": "Tesla", "candidate_id": "TSLA"}],
                         is_continuation=True)
        excluded_target = intent(excluded_entities=["Microsoft"])
        service, _ = self.service(mismatch, null_link, invalid_link, unknown, excluded_target)

        inconsistent = service.intake("review the candidate")
        self.assertEqual((inconsistent["status"], inconsistent["code"]),
                         ("needs_clarification", "inconsistent_entity_link"))
        self.assertEqual({item["company_id"] for item in inconsistent["options"]}, {"AAPL", "MSFT"})
        linked = service.intake("ordinary broad request")
        self.assertEqual((linked["status"], linked["scope"]["subjects"][0]["id"]), ("ready", "MSFT"))
        self.assertEqual(service.intake("another request")["code"], "inconsistent_entity_link")
        conversation = self.chat.create_conversation("AAPL")
        unidentified = service.intake("follow this up", conversation["conversation_id"])
        self.assertEqual((unidentified["status"], unidentified["code"]),
                         ("unsupported_scope", "company_not_covered"))
        self.assertEqual(service.intake("exclude target")["code"], "inconsistent_entity_link")

    def test_concurrent_request_id_is_single_flight_and_conflicts_preflight(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def blocked(prompt, timeout_seconds):
            calls.append(prompt)
            entered.set()
            self.assertTrue(release.wait(2))
            value = intent()
            return {"intent": value, "provenance": {
                "prompt": prompt, "prompt_version": INTENT_PROMPT_VERSION,
                "provider": "blocked-test", "model": "blocked-v1", "result": value,
            }}

        service = ResearchTaskService(self.chat, intent_resolver=blocked)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.create, "微软收入", None, "concurrent-id")
            self.assertTrue(entered.wait(1))
            second = pool.submit(service.create, "微软收入", None, "concurrent-id")
            with self.assertRaises(ChatError) as caught:
                service.create("苹果收入", None, "concurrent-id")
            self.assertEqual(caught.exception.code, "idempotency_conflict")
            self.assertEqual(len(calls), 1)
            release.set()
            self.assertEqual(first.result()["task_id"], second.result()["task_id"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(service._request_locks, {})

    def test_document_only_coverage_is_ready_capability(self):
        service, _ = self.service(intent())
        service._coverage = lambda company_id: {
            "facts": {"available": False, "count": 0},
            "documents": {"available": True, "count": 1},
        }
        result = service.intake("微软长期战略")
        self.assertEqual((result["status"], result["code"]), ("ready", "scope_ready"))

    def test_identity_only_clarification_and_excluded_company(self):
        ambiguous = intent("ambiguous_identity", [
            {"kind": "company", "name": "Apple", "candidate_id": "AAPL"},
            {"kind": "company", "name": "Microsoft", "candidate_id": "MSFT"}],
            identity_ambiguity=True, uncertainties=["代词可能指向两家公司"])
        excluded = intent(subjects=[{"kind": "company", "name": "Apple", "candidate_id": "AAPL"}],
                          excluded_entities=["Microsoft"])
        service, _ = self.service(ambiguous, excluded)
        first = service.intake("它的收入如何")
        self.assertEqual(first["code"], "ambiguous_identity")
        self.assertEqual({o["company_id"] for o in first["options"]}, {"MSFT", "AAPL"})
        second = service.intake("不要微软，看苹果收入")
        self.assertEqual(second["scope"]["subjects"][0]["id"], "AAPL")


if __name__ == "__main__":
    unittest.main()
