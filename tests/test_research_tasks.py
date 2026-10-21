from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from workbench.build import build_database
from workbench.chat import ChatError, ChatService
from workbench.research_scope import resolve_scope
from workbench.research_tasks import ResearchTaskService, _SCHEMA


def provider_intent(company_id, *, ambiguous=False):
    return {
        "kind": "ambiguous_identity" if ambiguous else "company_research",
        "subjects": ([{"kind": "company", "name": "unresolved", "candidate_id": None}]
                     if ambiguous else [{"kind": "company", "name": company_id, "candidate_id": company_id}]),
        "directions": ["test follow-up"], "metrics": [], "period_text": None, "basis": [],
        "unit": None, "assumptions": [], "uncertainties": ["identity unclear"] if ambiguous else [],
        "excluded_entities": [], "is_continuation": True, "identity_ambiguity": ambiguous,
        "summary": "preserved test follow-up",
    }


class CountingProvider:
    def __init__(self):
        self.available = True
        self.status_calls = 0
        self.stream_calls = 0
        self.intent_calls = 0
        self.release = threading.Event()
        self.release.set()

    def status(self):
        self.status_calls += 1
        return {"available": self.available, "provider": "test", "reason": "offline" if not self.available else ""}

    def resolve_research_intent(self, prompt, timeout_seconds):
        self.intent_calls += 1
        payload = json.loads(prompt.split("\n", 1)[1])
        question = payload["question"]
        current = payload.get("current_company_id") or "MSFT"
        if "微软收入是多少" in question:
            value = provider_intent("MSFT")
        elif any(token in question for token in ("英伟达", "宁德时代", "谷歌", "Google", "分析业务")):
            value = provider_intent(current, ambiguous=True)
        else:
            value = provider_intent(current)
        return {"intent": value, "provenance": {
            "prompt": prompt, "prompt_version": "research-intent/1.0",
            "provider": "explicit-test-adapter", "model": "test-intent-v1", "result": value,
        }}

    def stream(self, messages, tools, cancel_event):
        self.stream_calls += 1
        self.release.wait(2)
        yield {"type": "delta", "text": "test answer"}


class ResearchTaskTests(unittest.TestCase):
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
        self.chat_db = Path(self.temp.name) / "chat.sqlite3"
        self.provider = CountingProvider()
        self.chat = ChatService(self.evidence_db, self.chat_db, self.provider)
        # Explicit legacy adapter: these tests cover the old deterministic scope
        # mechanics, never the production LLM-first path.
        self.tasks = ResearchTaskService(self.chat, deterministic_scope_resolver=resolve_scope)

    def tearDown(self):
        self.provider.release.set()
        self.chat.close()
        self.temp.cleanup()

    def test_bounded_scope_preserves_period_and_noncompany_kinds(self):
        exact = "微软2025财年第四季度通过分红和回购向股东返还了多少钱？有什么口径需要注意"
        result = self.tasks.intake(exact)
        self.assertEqual((result["status"], result["scope"]["subjects"][0]["id"]), ("ready", "MSFT"))
        self.assertEqual(result["scope"]["period_text"], "2025财年第四季度")
        self.assertEqual(result["scope"]["resolution_method"], "bounded_alias_rules_v1")
        self.assertTrue(result["coverage"]["facts"]["available"])
        self.assertFalse(result["coverage"]["documents"]["available"])

        industry = self.tasks.intake("分析半导体行业利润")
        self.assertEqual(industry["status"], "unsupported_scope")
        self.assertEqual(industry["scope"]["subjects"][0]["kind"], "industry")
        self.assertIsNone(industry["scope"]["subjects"][0]["id"])
        unknown = self.tasks.intake("Tesla 的收入如何")
        self.assertEqual(unknown["status"], "needs_clarification")
        self.assertEqual(unknown["options"], [])

    def test_context_only_applies_to_generic_followup_and_legacy_mismatch_preflights_provider(self):
        conversation = self.chat.create_conversation("AAPL")
        generic = self.tasks.intake("继续说明来源限制", conversation["conversation_id"])
        self.assertEqual(generic["scope"]["subjects"][0]["id"], "AAPL")
        explicit = self.tasks.intake("微软收入是多少", conversation["conversation_id"])
        self.assertEqual(explicit["scope"]["subjects"][0]["id"], "MSFT")

        before = self.provider.status_calls
        with self.assertRaises(ChatError) as caught:
            self.chat.submit(conversation["conversation_id"], "微软收入是多少")
        self.assertEqual(caught.exception.code, "scope_mismatch")
        self.assertEqual(self.provider.status_calls, before)
        self.assertEqual(self.chat.get_conversation(conversation["conversation_id"])["messages"], [])

    def test_context_rejects_unregistered_entities_and_accepts_only_bounded_followups(self):
        conversation = self.chat.create_conversation("AAPL")
        for question in ("Google 最近一季收入如何？", "英伟达最近一季收入如何？"):
            result = self.tasks.intake(question, conversation["conversation_id"])
            self.assertEqual((result["status"], result["code"]), ("needs_clarification", "unknown_subject"))
            self.assertIsNone(result["scope"]["subjects"][0]["id"])
        metric = self.tasks.intake("收入增长了多少", conversation["conversation_id"])
        self.assertEqual((metric["status"], metric["scope"]["subjects"][0]["id"]), ("ready", "AAPL"))
        greeting = self.tasks.intake("hello", conversation["conversation_id"])
        self.assertEqual(greeting["status"], "needs_clarification")
        self.assertIsNone(greeting["scope"]["subjects"][0]["id"])

    def test_context_never_overrides_structurally_explicit_unknown_subjects(self):
        aapl = self.chat.create_conversation("AAPL")
        msft = self.chat.create_conversation("MSFT")
        cases = (
            (aapl, "英伟达2025年收入如何？"),
            (aapl, "宁德时代利润率"),
            (aapl, "分析谷歌业务"),
            (msft, "谷歌的业务与微软有什么不同"),
        )
        before = self.provider.status_calls
        for conversation, question in cases:
            result = self.tasks.intake(question, conversation["conversation_id"])
            self.assertEqual((result["status"], result["code"]),
                             ("needs_clarification", "unknown_subject"), question)
            self.assertIsNone(result["scope"]["subjects"][0]["id"], question)
            with self.assertRaises(ChatError, msg=question) as caught:
                self.chat.submit(conversation["conversation_id"], question)
            self.assertEqual(caught.exception.code, "scope_clarification", question)
        self.assertEqual(self.provider.status_calls, before)

    def test_context_followups_have_no_unresolved_tokens_and_english_exclusions_block(self):
        conversation = self.chat.create_conversation("MSFT")
        exact = self.tasks.intake(
            "微软2025财年第四季度通过分红和回购向股东返还了多少钱？有什么口径需要注意",
            conversation["conversation_id"],
        )
        self.assertEqual((exact["status"], exact["scope"]["subjects"][0]["id"]), ("ready", "MSFT"))
        uncommon_metric = self.tasks.intake("Microsoft customer retention", conversation["conversation_id"])
        self.assertEqual((uncommon_metric["status"], uncommon_metric["scope"]["subjects"][0]["id"]),
                         ("ready", "MSFT"))

        unresolved = self.tasks.intake("收入如何，顺便看看英伟达", conversation["conversation_id"])
        self.assertEqual(unresolved["status"], "needs_clarification")
        for question in ("not Microsoft revenue", "except Microsoft profit", "instead of Microsoft earnings"):
            result = self.tasks.intake(question, conversation["conversation_id"])
            self.assertEqual((result["status"], result["code"]),
                             ("needs_clarification", "unknown_subject"), question)

        before = self.provider.status_calls
        with self.assertRaises(ChatError) as caught:
            self.chat.submit(conversation["conversation_id"], "分析业务")
        self.assertEqual(caught.exception.code, "scope_clarification")
        self.assertEqual(self.provider.status_calls, before)
        greeting = self.chat.submit(conversation["conversation_id"], "hello")
        self.assertEqual(greeting["status"], "queued")

    def test_explicit_exclusions_never_select_the_excluded_company(self):
        conversation = self.chat.create_conversation("MSFT")
        question = "不要研究微软，请分析 Google 的收入趋势"
        result = self.tasks.intake(question, conversation["conversation_id"])
        self.assertEqual((result["status"], result["code"]), ("needs_clarification", "unknown_subject"))
        task = self.tasks.create(question, conversation["conversation_id"], "request-excluded")
        before = self.provider.status_calls
        with self.assertRaises(ChatError) as caught:
            self.tasks.start(task["task_id"], 1)
        self.assertEqual(caught.exception.code, "task_not_ready")
        with self.assertRaises(ChatError) as caught:
            self.chat.submit(conversation["conversation_id"], question)
        self.assertEqual(caught.exception.code, "scope_clarification")
        self.assertEqual(self.provider.status_calls, before)

        positive = self.tasks.intake("不要研究微软，请分析苹果的收入趋势")
        self.assertEqual((positive["status"], positive["scope"]["subjects"][0]["id"]), ("ready", "AAPL"))
        alternatives = self.tasks.intake("不要研究微软或苹果的收入")
        self.assertEqual(alternatives["status"], "needs_clarification")
        ordinary_negation = self.tasks.intake("微软收入趋势不包括非金融损失")
        self.assertEqual((ordinary_negation["status"], ordinary_negation["scope"]["subjects"][0]["id"]),
                         ("ready", "MSFT"))

    def test_content_bound_idempotency_and_immutable_cas_scope_versions(self):
        first = self.tasks.create("收入如何", None, "request-1")
        replay = self.tasks.create("收入如何", None, "request-1")
        self.assertEqual((first["task_id"], first["origin"]), (replay["task_id"], "user"))
        with self.assertRaises(ChatError) as caught:
            self.tasks.create("不同问题", None, "request-1")
        self.assertEqual((caught.exception.code, caught.exception.status_code), ("idempotency_conflict", 409))
        with self.assertRaises(ChatError) as caught:
            self.tasks.create("收入如何", None, "request-1", "machine_acceptance")
        self.assertEqual((caught.exception.code, caught.exception.status_code), ("idempotency_conflict", 409))

        resolved = self.tasks.resolve(first["task_id"], 1, "MSFT")
        self.assertEqual((resolved["current_scope_version"], resolved["status"]), (2, "ready"))
        with self.assertRaises(ChatError) as caught:
            self.tasks.resolve(first["task_id"], 1, "AAPL")
        self.assertEqual(caught.exception.code, "scope_version_conflict")
        with closing(sqlite3.connect(self.chat_db)) as connection:
            versions = connection.execute(
                "SELECT scope_version FROM research_scope_versions WHERE task_id=? ORDER BY scope_version",
                (first["task_id"],),
            ).fetchall()
            self.assertEqual(versions, [(1,), (2,)])
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE research_scope_versions SET resolution_json='{}' WHERE task_id=? AND scope_version=1",
                    (first["task_id"],),
                )

        industry = self.tasks.create("分析软件行业", None, "request-industry")
        with self.assertRaises(ChatError) as caught:
            self.tasks.resolve(industry["task_id"], 1, "MSFT")
        self.assertEqual(caught.exception.code, "invalid_scope_choice")

    def test_machine_tasks_and_linked_conversations_are_hidden_only_from_default_lists(self):
        user = self.tasks.create("苹果收入是多少", None, "request-user-default")
        machine = self.tasks.create(
            "微软收入是多少", None, "research-matrix-new-explicit", "machine_acceptance")
        bridge = self.tasks.start(machine["task_id"], 1)

        self.assertEqual([item["task_id"] for item in self.tasks.list()["tasks"]], [user["task_id"]])
        all_tasks = self.tasks.list(include_internal=True)["tasks"]
        self.assertEqual({item["task_id"] for item in all_tasks}, {user["task_id"], machine["task_id"]})
        self.assertEqual({item["origin"] for item in all_tasks}, {"user", "machine_acceptance"})
        self.assertEqual(self.chat.list_conversations()["conversations"], [])
        internal = self.chat.list_conversations(include_internal=True)["conversations"]
        self.assertEqual([item["conversation_id"] for item in internal], [bridge["conversation_id"]])
        self.assertEqual(self.tasks.get(machine["task_id"])["origin"], "machine_acceptance")
        self.assertEqual(
            self.chat.get_conversation(bridge["conversation_id"])["conversation_id"], bridge["conversation_id"])

    def test_origin_migration_backfills_only_certain_prefix_without_deleting_history(self):
        legacy_db = Path(self.temp.name) / "legacy-chat.sqlite3"
        legacy_chat = ChatService(self.evidence_db, legacy_db, self.provider)
        resolution = self.tasks.intake("微软收入是多少")
        try:
            with closing(legacy_chat._connect()) as connection:
                connection.executescript(_SCHEMA)
                for task_id, request_id in (("task_matrix", "research-matrix-old"),
                                            ("task_user", "ordinary-old")):
                    connection.execute(
                        "INSERT INTO research_tasks VALUES(?,?,?,?,?,?,?)",
                        (task_id, "微软收入是多少", None, request_id, "legacy-fingerprint", 1, "2025-01-01T00:00:00Z"),
                    )
                    connection.execute(
                        "INSERT INTO research_scope_versions VALUES(?,?,?,?)",
                        (task_id, 1, json.dumps(resolution), "2025-01-01T00:00:00Z"),
                    )
                connection.commit()
            migrated = ResearchTaskService(legacy_chat)
            all_tasks = migrated.list(include_internal=True)["tasks"]
            self.assertEqual(len(all_tasks), 2)
            self.assertEqual({item["task_id"]: item["origin"] for item in all_tasks}, {
                "task_matrix": "machine_acceptance", "task_user": "user"})
            self.assertEqual([item["task_id"] for item in migrated.list()["tasks"]], ["task_user"])
            self.assertEqual(migrated.get("task_matrix")["origin"], "machine_acceptance")
            with closing(sqlite3.connect(legacy_db)) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM research_tasks").fetchone()[0], 2)
        finally:
            legacy_chat.close()

    def test_list_returns_lightweight_persisted_user_summary_without_artifact_reads(self):
        task = self.tasks.create("微软2025财年第四季度收入是多少", None, "request-list-summary")
        bridge = self.tasks.start(task["task_id"], 1)
        for _ in range(100):
            run = self.chat.get_run(bridge["run_id"])
            if run["status"] not in {"queued", "running"}:
                break
            time.sleep(.01)
        self.assertEqual(run["status"], "completed")

        with mock.patch("workbench.answer_artifacts.AnswerArtifactService.get",
                        side_effect=AssertionError("list must not read artifacts")):
            listed = self.tasks.list()["tasks"][0]
        self.assertEqual(listed["answer_status"], "answered")
        self.assertEqual(listed["answer_summary"], "test answer")
        self.assertEqual(listed["period_text"], "2025财年第四季度")
        self.assertIsNotNone(listed["finished_at"])
        self.assertEqual(listed["failure_reason"], "")
        self.assertIn(listed["risk_status"], {"needs_review", "not_checked"})
        self.assertNotEqual(listed["risk_status"], "no_risk")
        self.assertNotIn("reader_report_id", listed)
        self.assertNotIn("reader_href", listed)
        detail = self.tasks.get(task["task_id"])
        self.assertIsNone(detail["latest_report"])

    def test_list_and_detail_expose_current_persisted_decision_without_events(self):
        from tests.test_research_decisions import OPTIONS

        for kind in ("coverage", "time", "basis", "model", "other"):
            with self.subTest(kind=kind):
                def stream(messages, tools, cancel_event):
                    handler = next(tool["handler"] for tool in tools if tool["name"] == "request_confirmation")
                    handler(kind=kind, question="需要选择的实际问题", explanation="保留范围与方案差异。",
                            options=OPTIONS, multiple=False)
                    yield {"type": "delta", "text": "未确认不能发布"}

                with mock.patch.object(self.provider, "stream", stream):
                    task = self.tasks.create("微软收入是多少", None, "pending-" + kind)
                    bridge = self.tasks.start(task["task_id"], 1)
                    for _ in range(200):
                        run = self.chat.get_run(bridge["run_id"])
                        if run["status"] not in {"queued", "running"}:
                            break
                        time.sleep(.01)
                self.assertEqual(run["status"], "blocked")
                with mock.patch.object(self.chat, "get_run", side_effect=AssertionError("do not load event history")):
                    listed = next(t for t in self.tasks.list()["tasks"] if t["task_id"] == task["task_id"])
                    detailed = self.tasks.get(task["task_id"])
                summary = listed["latest_run"]
                self.assertEqual(summary, detailed["latest_run"])
                self.assertEqual(summary["run_id"], bridge["run_id"])
                self.assertEqual(summary["termination_reason"], "confirmation_required")
                self.assertEqual(summary["decision"]["kind"], kind)
                self.assertEqual(summary["decision"]["status"], "pending")
                self.assertEqual(summary["decision"]["question"], "需要选择的实际问题")
                self.assertNotIn("events", summary)
                self.assertNotIn("options", summary["decision"])
                self.assertNotIn("reader_href", listed)
                self.assertEqual(listed["answer_status"], "unanswered")
                self.chat.decisions.answer(summary["decision"]["decision_id"], [], "", "declined", "decline-" + kind)
                current = next(t for t in self.tasks.list()["tasks"] if t["task_id"] == task["task_id"])
                self.assertEqual(current["latest_run"]["decision"]["status"], "declined")
                newer, _ = self.chat._persist_submission(bridge["conversation_id"], None, "新的研究要求")
                current = next(t for t in self.tasks.list()["tasks"] if t["task_id"] == task["task_id"])
                self.assertEqual(current["latest_run"]["run_id"], newer["run_id"])
                self.assertNotIn("decision", current["latest_run"], "old decisions must not leak onto a new run")

    def test_published_task_has_reader_link(self):
        from tests.test_report_publications import ReportProvider

        class TaskReportProvider(ReportProvider):
            resolve_research_intent = CountingProvider.resolve_research_intent

        provider = TaskReportProvider()
        provider.intent_calls = 0
        published_chat = ChatService(self.evidence_db, Path(self.temp.name) / "published.sqlite3", provider)
        self.addCleanup(published_chat.close)
        tasks = ResearchTaskService(published_chat)
        task = tasks.create("微软收入相对以前的累计变化", None, "request-published")
        bridge = tasks.start(task["task_id"], 1)
        for _ in range(200):
            run = published_chat.get_run(bridge["run_id"])
            if run["status"] not in {"queued", "running"}:
                break
            time.sleep(.01)
        self.assertEqual(run["status"], "completed", run.get("error"))
        listed = tasks.list()["tasks"][0]
        self.assertRegex(listed["reader_report_id"], r"^report_[0-9a-f]{16}$")
        self.assertEqual(listed["reader_href"], f'/report-reader.html?report_id={listed["reader_report_id"]}')
        detail = tasks.get(task["task_id"])
        self.assertEqual(detail["latest_report"]["reader_href"], listed["reader_href"])
        from workbench.report_reader import load_required_reader_snapshots, resolve_report

        identity = resolve_report(published_chat, report_id=listed["reader_report_id"])
        payload, _artifact = load_required_reader_snapshots(
            published_chat, report_id=identity["report_id"])
        self.assertEqual(payload["report"]["id"], listed["reader_report_id"])
        refreshed = tasks.list()["tasks"][0]
        self.assertEqual(refreshed["reader_title"], payload["report"]["title"])

    def test_repeat_and_concurrent_start_return_one_atomic_run(self):
        task = self.tasks.create("微软收入是多少", None, "request-start")
        self.provider.release.clear()
        barrier = threading.Barrier(8)

        def start(_):
            barrier.wait(timeout=3)
            return self.tasks.start(task["task_id"], 1)

        with ThreadPoolExecutor(max_workers=8) as pool:
            starts = list(pool.map(start, range(8)))
        self.assertEqual(len({item["run_id"] for item in starts}), 1)
        self.assertEqual(len({item["conversation_id"] for item in starts}), 1)
        self.assertEqual(set(starts[0]), {"task_id", "scope_version", "company_id", "company_ids", "conversation_id", "run_id"})
        self.assertEqual(starts[0]["company_ids"], ["MSFT"])
        self.provider.available = False
        self.assertEqual(self.tasks.start(task["task_id"], 1), starts[0])
        with closing(sqlite3.connect(self.chat_db)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM research_task_runs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM conversations").fetchone()[0], 1)
        self.provider.release.set()

    def test_cross_instance_contenders_preflight_provider_only_after_atomic_election(self):
        task = self.tasks.create("微软收入是多少", None, "request-provider-election")
        second_chat = ChatService(self.evidence_db, self.chat_db, self.provider)
        second_tasks = ResearchTaskService(second_chat)
        original_status = self.provider.status
        status_lock = threading.Lock()

        def available_once():
            with status_lock:
                result = original_status()
                self.provider.available = False
                return result

        self.provider.status = available_once
        barrier = threading.Barrier(8)

        def start(index):
            barrier.wait(timeout=3)
            service = self.tasks if index % 2 == 0 else second_tasks
            return service.start(task["task_id"], 1)

        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                starts = list(pool.map(start, range(8)))
            self.assertEqual(len({item["run_id"] for item in starts}), 1)
            self.assertEqual(self.provider.status_calls, 1)
            self.assertEqual(self.tasks.start(task["task_id"], 1), starts[0])
        finally:
            self.provider.status = original_status
            self.provider.available = True
            second_chat.close()

    def test_worker_thread_start_failure_retries_same_association_once(self):
        task = self.tasks.create("微软收入是多少", None, "request-worker-retry")
        original_start = threading.Thread.start
        with mock.patch("workbench.chat.threading.Thread.start", side_effect=RuntimeError("cannot start")):
            with self.assertRaises(ChatError) as caught:
                self.tasks.start(task["task_id"], 1)
        self.assertEqual(caught.exception.code, "worker_start_failed")
        failed = self.tasks.get(task["task_id"])
        self.assertEqual((len(failed["runs"]), failed["runs"][0]["status"]), (1, "failed"))

        with mock.patch("workbench.chat.threading.Thread.start", autospec=True,
                        side_effect=lambda thread: original_start(thread)):
            retried = self.tasks.start(task["task_id"], 1)
        self.assertEqual(retried["run_id"], failed["runs"][0]["run_id"])
        for _ in range(100):
            run = self.chat.get_run(retried["run_id"])
            if run["status"] not in {"queued", "running"}:
                break
            time.sleep(.01)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(self.provider.stream_calls, 1)
        with closing(sqlite3.connect(self.chat_db)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM research_task_runs").fetchone()[0], 1)

    def test_retry_intent_singleflight_lost_ack_and_original_input(self):
        question = "保留这个原始研究问题"
        prompts = []
        write_lock_checks = []
        calls_lock = threading.Lock()

        def resolver(prompt, timeout_seconds):
            del timeout_seconds
            with calls_lock:
                prompts.append(prompt)
                call = len(prompts)
            if call == 1:
                raise RuntimeError("controlled unavailable detail must not escape")
            with closing(sqlite3.connect(self.chat_db, timeout=.1)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
                write_lock_checks.append(True)
            time.sleep(.03)
            value = provider_intent("MSFT")
            return {"intent": value, "provenance": {
                "prompt": prompt, "prompt_version": "research-intent/1.0",
                "provider": "retry-test", "model": "test", "result": value,
            }}

        service = ResearchTaskService(self.chat, intent_resolver=resolver)
        failed = service.create(question, None, "request-retry-intent")
        self.assertEqual((failed["status"], failed["code"]),
                         ("model_unavailable", "intent_model_unavailable"))
        barrier = threading.Barrier(8)

        def retry(_):
            barrier.wait(timeout=3)
            return service.retry_intent(failed["task_id"], 1)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(retry, range(8)))
        self.assertEqual(len(prompts), 2)
        self.assertEqual(write_lock_checks, [True], "model wait must not hold a SQLite writer lock")
        self.assertEqual({item["current_scope_version"] for item in results}, {2})
        self.assertEqual({item["task_id"] for item in results}, {failed["task_id"]})
        self.assertTrue(all(item["question"] == question for item in results))
        self.assertIn(question, prompts[1])
        self.assertEqual(service.retry_intent(failed["task_id"], 1)["current_scope_version"], 2)
        self.assertEqual(len(prompts), 2, "lost-ack replay must not call intent again")
        with self.assertRaises(ChatError) as caught:
            service.retry_intent(failed["task_id"], 2)
        self.assertEqual(caught.exception.code, "intent_retry_not_allowed")
        with self.assertRaises(ChatError) as caught:
            service.retry_intent(failed["task_id"], 99)
        self.assertEqual(caught.exception.code, "scope_version_conflict")
        with closing(sqlite3.connect(self.chat_db)) as connection:
            rows = connection.execute(
                "SELECT scope_version,resolution_json FROM research_scope_versions "
                "WHERE task_id=? ORDER BY scope_version", (failed["task_id"],)).fetchall()
        self.assertEqual([row[0] for row in rows], [1, 2])
        self.assertEqual(json.loads(rows[0][1])["code"], "intent_model_unavailable")

    def test_retry_intent_schema_failure_and_repeated_failure_append_history(self):
        outcomes = [
            {},
            {"not": "an intent"},
        ]

        def resolver(prompt, timeout_seconds):
            del prompt, timeout_seconds
            return outcomes.pop(0)

        service = ResearchTaskService(self.chat, intent_resolver=resolver)
        first = service.create("结构失败也保留问题", None, "request-retry-schema")
        self.assertEqual((first["status"], first["code"]), ("needs_review", "intent_schema_invalid"))
        second = service.retry_intent(first["task_id"], 1)
        self.assertEqual((second["current_scope_version"], second["status"], second["code"]),
                         (2, "needs_review", "intent_schema_invalid"))
        self.assertEqual(second["question"], first["question"])
        self.assertEqual(second["runs"], [])
        self.assertEqual(service.retry_intent(first["task_id"], 1)["current_scope_version"], 2)

    def test_provider_unavailable_creates_no_conversation_run_or_link(self):
        task = self.tasks.create("苹果收入是多少", None, "request-offline")
        self.provider.available = False
        with self.assertRaises(ChatError) as caught:
            self.tasks.start(task["task_id"], 1)
        self.assertEqual(caught.exception.code, "provider_unavailable")
        with closing(sqlite3.connect(self.chat_db)) as connection:
            for table in ("conversations", "runs", "research_task_runs"):
                self.assertEqual(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_multi_company_and_false_positive_comparison(self):
        multi = self.tasks.create("比较微软和苹果的收入", None, "request-multi")
        self.assertEqual(multi["status"], "needs_clarification")
        self.assertEqual(multi["scope"]["subjects"][0]["kind"], "comparison")
        with self.assertRaises(ChatError):
            self.tasks.start(multi["task_id"], 1)
        single = self.tasks.intake("Microsoft comparison to earlier quarter")
        self.assertEqual(single["status"], "ready")
        self.assertEqual(single["scope"]["subjects"][0]["id"], "MSFT")


if __name__ == "__main__":
    unittest.main()
