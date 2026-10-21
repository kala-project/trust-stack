from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from workbench.build import build_database
from workbench.chat import ChatError, ChatService
from workbench.copilot_provider import CopilotProvider
from workbench.research_tools import ResearchTools, ToolError
from workbench.worker import DurableWorker
from tests.test_chat import StructuredTestProvider


OPTIONS = [
    {"id": "cash", "label": "现金口径", "benefit": "观察当期支付", "limitation": "不含未来租赁支付"},
    {"id": "investment", "label": "总投入口径", "benefit": "观察整体投入", "limitation": "不是当期现金流"},
]
GROUPED_OPTIONS = OPTIONS + [
    {"id": "drivers", "label": "业务驱动模型", "benefit": "连接经营因素", "limitation": "需确认更多参数"},
    {"id": "trend", "label": "趋势模型", "benefit": "假设简单透明", "limitation": "不能解释结构变化"},
]
GROUPS = [
    {"id": "basis", "label": "研究口径", "option_ids": ["cash", "investment"], "minimum": 1, "maximum": 2},
    {"id": "method", "label": "研究模型", "option_ids": ["drivers", "trend"], "minimum": 1, "maximum": 1},
]


class DecisionProvider:
    def __init__(self):
        self.calls = 0

    def status(self):
        return {"available": True, "provider": "explicit-protocol-test"}

    def stream(self, messages, tools, cancel_event):
        self.calls += 1
        handlers = {item["name"]: item["handler"] for item in tools}
        if self.calls == 1:
            handlers["request_confirmation"](
                kind="basis", question="使用哪些口径？", explanation="两个口径回答不同的问题。",
                options=OPTIONS, multiple=True,
            )
            yield {"type": "delta", "text": "不得作为最终报告发布的未确认文本"}
            return
        result = handlers["search_evidence"](query="revenue")
        evidence_id = result["results"][0]["evidence_id"]
        yield {"type": "delta", "text": f"仅展示已检索的来源，尚不能确定资本投入趋势。[evidence:{evidence_id}]"}


class GroupedDecisionProvider(DecisionProvider):
    def stream(self, messages, tools, cancel_event):
        if self.calls:
            yield from super().stream(messages, tools, cancel_event)
            return
        self.calls += 1
        handler = next(item["handler"] for item in tools if item["name"] == "request_confirmation")
        handler(kind="model", question="选择研究口径与模型", explanation="可并行核对口径，模型另行选择。",
                options=GROUPED_OPTIONS, multiple=True, groups=GROUPS)
        yield {"type": "delta", "text": "未确认前不应发布"}


class ResearchDecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.db = Path(cls.fixture.name) / "evidence.sqlite3"
        build_database(db_path=cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = DecisionProvider()
        self.chat = ChatService(
            self.db, Path(self.temp.name) / "chat.sqlite3",
            StructuredTestProvider(self.provider, preserve_references=False))
        self.conv = self.chat.create_conversation("MSFT")["conversation_id"]

    def tearDown(self):
        self.chat.close()
        self.temp.cleanup()

    def pending(self, multiple=True):
        run_id = "run_pending"
        with closing(self.chat._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO runs(run_id,conversation_id,status,created_at) VALUES(?,?,?,?)",
                (run_id, self.conv, "queued", "2026-10-03T00:00:00Z"))
            connection.execute(
                "INSERT INTO messages(message_id,conversation_id,role,content,created_at,run_id,status) "
                "VALUES(?,?,?,?,?,?,?)",
                ("msg_pending", self.conv, "user", "研究资本投入", "2026-10-03T00:00:00Z",
                 run_id, "completed"))
        return self.chat.decisions.request(
            run_id, kind="basis", question="选择口径", explanation="明确口径差异。",
            options=OPTIONS, multiple=multiple,
        )

    def wait(self, run_id):
        worker = DurableWorker(
            self.chat, worker_id=f"decision-pump-{run_id}", heartbeat_seconds=.05)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            worker.run_once()
            run = self.chat.get_run(run_id)
            if run["status"] not in {"queued", "running"}:
                with closing(self.chat._connect()) as connection:
                    pending = connection.execute(
                        "SELECT count(*) FROM jobs WHERE status IN ('queued','retry_wait','running')"
                    ).fetchone()[0]
                if not pending:
                    return run
            time.sleep(.02)
        self.fail("run did not terminate")

    def block_intake_decision_job(self, run_id):
        claim = self.chat.jobs.claim("intake-decision-fixture")
        self.assertIsNotNone(claim)
        self.assertEqual(claim.run_id, run_id)
        with closing(self.chat._connect()) as connection, connection:
            self.chat.jobs.mark_blocked_in_transaction(connection, claim)
        self.chat.jobs.release_runtime_slot(claim)

    def test_tool_pauses_without_publishing_answer(self):
        run = self.chat.create_and_submit("MSFT", "研究资本投入")
        result = self.wait(run["run_id"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error"]["code"], "confirmation_required")
        self.assertEqual(result["decision"]["options"], OPTIONS)
        messages = self.chat.get_conversation(run["conversation_id"])["messages"]
        self.assertFalse(any(item["role"] == "assistant" for item in messages))

    def test_multi_select_resumes_once_and_preserves_original_question(self):
        decision = self.pending()
        first = self.chat.decisions.answer(
            decision["decision_id"], ["cash", "investment"], "", "accepted", "request-1")
        with closing(self.chat._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM jobs WHERE run_id=?", (first["run_id"],)).fetchone()[0], 1)
        self.assertEqual(self.provider.calls, 0)
        current = self.chat.decisions.for_run(decision["run_id"])
        self.assertEqual(current["status"], "accepted")
        self.assertEqual(current["selected_ids"], ["cash", "investment"])
        self.assertEqual(self.chat.get_conversation(self.conv)["messages"][0]["content"], "研究资本投入")

    def test_confirmation_question_snapshot_uses_real_intake_not_synthetic_message(self):
        question = "微软FY2025资本投入采用什么口径？"
        run_id = "run_question_snapshot"
        with closing(self.chat._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO runs(run_id,conversation_id,status,created_at) VALUES(?,?,?,?)",
                (run_id, self.conv, "queued", "2026-10-03T00:00:00Z"))
            connection.execute(
                "INSERT INTO messages(message_id,conversation_id,role,content,created_at,run_id,status) "
                "VALUES(?,?,?,?,?,?,?)",
                ("msg_question", self.conv, "user", question, "2026-10-03T00:00:00Z",
                 run_id, "completed"))
            self.chat._insert_intent_context(connection, run_id, {
                "status": "ready", "scope": {"subjects": [], "metrics": ["资本投入"]},
            }, question)
        decision = self.chat.decisions.request(
            run_id, kind="basis", question="选择口径", explanation="明确口径差异。",
            options=OPTIONS, multiple=False,
        )
        continued = self.chat.decisions.answer(
            decision["decision_id"], ["cash"], "保留实际现金口径", "accepted",
            "question-snapshot-real-confirm")
        synthetic = self.chat.get_conversation(self.conv)["messages"][-1]["content"]
        self.assertTrue(synthetic.startswith("继续研究。已确认："))
        snapshot = self.chat.question_snapshot_for({"run_id": continued["run_id"], "question": synthetic})
        self.assertEqual(snapshot["original_question"], question)
        self.assertIsNone(snapshot["normalized_question"])
        self.assertEqual(snapshot["clarifications"][0]["selected"][0]["id"], "cash")
        self.assertEqual(snapshot["clarifications"][0]["user_notes"], "保留实际现金口径")
        from workbench.report_reader import build_reader_payload
        artifact = {"question": synthetic, "question_snapshot": snapshot,
                    "findings": [], "anchors": [], "traces": [], "calculations": [],
                    "datasets": [], "limitations": []}
        self.assertEqual(build_reader_payload(artifact)["question"]["original"], question)

    def test_concurrent_confirmation_is_idempotent(self):
        decision = self.pending()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.chat.decisions.answer(
                decision["decision_id"], ["cash"], "", "accepted", "same-request"), range(4)))
        self.assertEqual(len({item["run_id"] for item in results}), 1)
        with closing(self.chat._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM jobs WHERE run_id=?", (results[0]["run_id"],)).fetchone()[0], 1)

    def test_invalid_choices_do_not_start_a_run(self):
        decision = self.pending(multiple=False)
        for choices in ([], ["cash", "cash"], ["unknown"], ["cash", "investment"]):
            with self.assertRaises(ChatError):
                self.chat.decisions.answer(decision["decision_id"], choices, "", "accepted", "r")
        self.assertEqual(self.chat.decisions.for_run(decision["run_id"])["status"], "pending")

    def grouped_pending(self):
        run_id = "run_grouped_pending"
        with closing(self.chat._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO runs(run_id,conversation_id,status,created_at) VALUES(?,?,?,?)",
                (run_id, self.conv, "queued", "2026-10-03T00:00:00Z"))
            connection.execute(
                "INSERT INTO messages(message_id,conversation_id,role,content,created_at,run_id,status) "
                "VALUES(?,?,?,?,?,?,?)",
                ("msg_grouped", self.conv, "user", "确认模型与口径", "2026-10-03T00:00:00Z",
                 run_id, "completed"))
        return self.chat.decisions.request(
            run_id, kind="model", question="选择研究方案", explanation="口径可并行，模型单选。",
            options=GROUPED_OPTIONS, multiple=True, groups=GROUPS,
        )

    def test_groups_require_each_category_and_allow_parallel_bases(self):
        decision = self.grouped_pending()
        for selected in (["cash"], ["drivers"], ["cash", "drivers", "trend"]):
            with self.assertRaises(ChatError):
                self.chat.decisions.answer(decision["decision_id"], selected, "", "accepted", "r-groups")
        result = self.chat.decisions.answer(
            decision["decision_id"], ["cash", "investment", "drivers"], "", "accepted", "r-groups")
        with closing(self.chat._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM jobs WHERE run_id=?", (result["run_id"],)).fetchone()[0], 1)
        event = next(event for event in self.chat.get_run(result["run_id"])["events"]
                     if event["type"] == "confirmed_decision")
        self.assertEqual(event["context"]["groups"], GROUPS)
        self.assertEqual({item["id"] for item in event["context"]["selected"]},
                         {"cash", "investment", "drivers"})

    def test_group_definitions_must_partition_options_with_valid_limits(self):
        from workbench.research_decisions import validate_request
        invalid = [
            [GROUPS[0]],
            [GROUPS[0], {**GROUPS[1], "option_ids": ["cash", "trend"]}],
            [GROUPS[0], {**GROUPS[1], "id": "basis"}],
            [GROUPS[0], {**GROUPS[1], "minimum": True}],
            [GROUPS[0], {**GROUPS[1], "maximum": 3}],
            [GROUPS[0], {**GROUPS[1], "option_ids": ["drivers", "drivers", "trend"]}],
        ]
        for groups in invalid:
            with self.subTest(groups=groups), self.assertRaises(ToolError):
                validate_request("model", "Question", "Explanation", GROUPED_OPTIONS, True, groups)
        with self.assertRaises(ToolError):
            validate_request("model", "Question", "Explanation", GROUPED_OPTIONS, False, GROUPS)

    def test_groups_survive_reopening_and_cannot_be_rewritten(self):
        import sqlite3
        decision = self.grouped_pending()
        self.chat.close()
        self.chat = ChatService(
            self.db, Path(self.temp.name) / "chat.sqlite3",
            StructuredTestProvider(self.provider, preserve_references=False))
        self.assertEqual(self.chat.decisions.for_run(decision["run_id"])["groups"], GROUPS)
        with closing(self.chat._connect()) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE research_decision_groups SET groups_json='[]'")
        result = self.chat.decisions.answer(decision["decision_id"], [], "", "declined", "decline-groups")
        self.assertIsNone(result["run_id"])

    def test_grouped_model_tool_pauses_with_persisted_constraints(self):
        self.chat.provider = StructuredTestProvider(GroupedDecisionProvider(), preserve_references=False)
        submitted = self.chat.create_and_submit("MSFT", "研究口径和模型")
        run = self.wait(submitted["run_id"])
        self.assertEqual(run["status"], "blocked")
        self.assertEqual(run["decision"]["groups"], GROUPS)
        self.assertFalse(any(message["role"] == "assistant" for message in
                             self.chat.get_conversation(submitted["conversation_id"])["messages"]))

    def test_pending_decision_cannot_be_bypassed_by_normal_followup(self):
        self.pending()
        with self.assertRaises(ChatError) as caught:
            self.chat.submit(self.conv, "继续")
        self.assertEqual(caught.exception.code, "confirmation_required")
        self.assertEqual(self.provider.calls, 0)

    def test_decision_created_during_intake_cannot_be_bypassed(self):
        def intake(*args):
            self.pending()
            return {"status": "ready", "scope": {"subjects": [{"kind": "company", "id": "MSFT"}],
                                               "relationship_to_context": "same_company_followup"}}
        with patch("workbench.research_tasks.ResearchTaskService.intake", side_effect=intake):
            with self.assertRaises(ChatError) as caught:
                self.chat.submit(self.conv, "继续研究")
        self.assertEqual(caught.exception.code, "confirmation_required")
        with closing(self.chat._connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
        self.assertEqual(self.provider.calls, 0)

    def test_decline_and_conflicting_replay(self):
        decision = self.pending()
        result = self.chat.decisions.answer(decision["decision_id"], [], "不缩小范围", "declined", "decline")
        self.assertIsNone(result["run_id"])
        with self.assertRaises(ChatError) as caught:
            self.chat.decisions.answer(decision["decision_id"], ["cash"], "", "accepted", "new")
        self.assertEqual(caught.exception.code, "decision_conflict")

    def test_confirmed_choices_survive_history_trimming(self):
        decision = self.pending()
        self.chat.decisions.answer(
            decision["decision_id"], ["cash"], "", "accepted", "r-history")
        with closing(self.chat._connect()) as connection, connection:
            for identifier, role, content in (
                ("long-report", "assistant", "历史报告" * 2000),
                ("followup", "user", "继续完善整合报告"),
            ):
                connection.execute(
                    "INSERT INTO messages(message_id,conversation_id,role,content,created_at,status) VALUES(?,?,?,?,?,?)",
                    (identifier, self.conv, role, content, "2099-01-01T00:00:00Z", "completed"))
            self.chat.max_history_chars = 1200
            history = self.chat._history(connection, self.conv)
            self.assertEqual(len(history), 1)
            self.assertIn("继续完善整合报告", history[0]["content"])
            self.assertIn("现金口径", history[0]["content"])
            self.chat.max_history_chars = 10
            with self.assertRaises(ChatError) as caught:
                self.chat._history(connection, self.conv)
            self.assertEqual(caught.exception.code, "history_limit")

    def test_decision_survives_reopening_and_is_immutable(self):
        decision = self.pending()
        self.chat.close()
        self.chat = ChatService(
            self.db, Path(self.temp.name) / "chat.sqlite3",
            StructuredTestProvider(self.provider, preserve_references=False))
        self.assertEqual(self.chat.get_run(decision["run_id"])["decision"]["status"], "pending")
        import sqlite3
        with closing(self.chat._connect()) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE research_decisions SET question='overwrite'")

    def test_missing_explanations_rejected(self):
        run, _ = self.chat._persist_submission(self.conv, None, "研究")
        with self.assertRaises(ToolError):
            self.chat.decisions.request(
                run["run_id"], kind="model", question="选模型", explanation="",
                options=OPTIONS, multiple=False)

    def test_runtime_reports_actual_pinned_version_or_fails_closed(self):
        with patch("workbench.copilot_provider.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout="GitHub Copilot CLI 1.0.88.")):
            with self.assertRaisesRegex(ValueError, "reports 1.0.88, expected 1.0.79"):
                CopilotProvider._verify_cli_version("/fixed/copilot", "1.0.79")
        with patch("workbench.copilot_provider.subprocess.run", return_value=SimpleNamespace(
                returncode=0, stdout="GitHub Copilot CLI 1.0.79.")):
            CopilotProvider._verify_cli_version("/fixed/copilot", "1.0.79")

    def test_cross_day_followup_asks_before_model_dispatch(self):
        with closing(self.chat._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO messages(message_id,conversation_id,role,content,created_at,status) VALUES(?,?,?,?,?,?)",
                ("historical", self.conv, "assistant", "以前的报告", "2020-01-01T00:00:00Z", "completed"))
        with patch("workbench.research_tasks.ResearchTaskService.intake", return_value={
                "status": "ready", "scope": {"subjects": [{"kind": "company", "id": "MSFT"}],
                                           "relationship_to_context": "same_company_followup"}}):
            result = self.chat.submit(self.conv, "继续分析")
        decision = self.chat.get_run(result["run_id"])["decision"]
        self.assertEqual(decision["kind"], "time")
        self.assertEqual(self.provider.calls, 0)

    def test_cross_day_confirmation_cannot_authorize_another_company(self):
        with closing(self.chat._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO messages(message_id,conversation_id,role,content,created_at,status) VALUES(?,?,?,?,?,?)",
                ("historical", self.conv, "assistant", "以前的报告", "2020-01-01T00:00:00Z", "completed"))
        with patch("workbench.research_tasks.ResearchTaskService.intake", return_value={
                "status": "ready", "scope": {"subjects": [{"id": "AAPL"}],
                                           "relationship_to_context": "different_company_new_research"}}):
            with self.assertRaises(ChatError) as caught:
                self.chat.submit(self.conv, "改为研究苹果")
        self.assertEqual(caught.exception.code, "scope_mismatch")
        with closing(self.chat._connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM research_decisions").fetchone()[0], 0)
        self.assertEqual(self.provider.calls, 0)

    def test_frozen_research_does_not_refresh_facts(self):
        tools = ResearchTools(self.db, "MSFT")
        evidence = {key: tools._company[key] for key in (
            "sources", "evidence", "claims", "facts", "evidence_chains", "activities")}
        snapshot = {"evidence": {"company_id": "MSFT", **evidence},
                    "data_version": tools.data_version,
                    "documents": {"coverage": tools.document_coverage, "document_version_ids": []}}
        restored = ResearchTools(self.db, "MSFT", frozen_snapshot=snapshot)
        self.assertEqual(restored._facts, tools._facts)
        self.assertEqual(restored.data_version, tools.data_version)
        snapshot["evidence"]["company_id"] = "AAPL"
        with self.assertRaises(ToolError):
            ResearchTools(self.db, "MSFT", frozen_snapshot=snapshot)

    def test_model_confirmation_resumes_the_proposals_frozen_snapshot(self):
        first = self.chat.create_and_submit("MSFT", "研究资本投入")
        paused = self.wait(first["run_id"])
        expected = self.chat.effective_snapshot(first["run_id"], "MSFT")

        def restored(*args, **kwargs):
            self.assertEqual(kwargs.get("frozen_snapshot"), expected)
            return ResearchTools(*args, **kwargs)

        with patch("workbench.scoped_tools.ResearchTools", side_effect=restored) as factory:
            continuation = self.chat.decisions.answer(
                paused["decision"]["decision_id"], ["cash"], "", "accepted", "frozen-choice")
            self.assertEqual(self.wait(continuation["run_id"])["status"], "completed")
            self.assertEqual(factory.call_count, 2)
            self.assertTrue(all(call.kwargs["frozen_snapshot"] == expected
                                for call in factory.call_args_list))
        self.assertEqual(self.chat.effective_snapshot(continuation["run_id"], "MSFT")["data_version"],
                         expected["data_version"])

    def test_retained_data_survives_a_subsequent_basis_confirmation(self):
        class ChainedProvider(DecisionProvider):
            def stream(self, messages, tools, cancel_event):
                handlers = {item["name"]: item["handler"] for item in tools}
                self.calls += 1
                if self.calls == 2:
                    handlers["request_confirmation"](
                        kind="basis", question="选择口径", explanation="沿用已选资料。",
                        options=OPTIONS, multiple=True)
                    return
                result = handlers["search_evidence"](query="revenue")
                yield {"type": "final_answer", "text":
                       f"来源仍需核查。[evidence:{result['results'][0]['evidence_id']}]"}

        self.chat.provider = StructuredTestProvider(ChainedProvider(), preserve_references=False)
        initial = self.chat.create_and_submit("MSFT", "研究微软")
        self.assertEqual(self.wait(initial["run_id"])["status"], "completed")
        conversation = initial["conversation_id"]
        expected = self.chat.effective_snapshot(initial["run_id"], "MSFT")
        with closing(self.chat._connect()) as connection, connection:
            connection.execute("UPDATE messages SET created_at='2020-01-01T00:00:00Z' "
                               "WHERE conversation_id=? AND role='assistant'", (conversation,))
        with patch("workbench.research_tasks.ResearchTaskService.intake", return_value={
                "status": "ready", "scope": {"subjects": [{"kind": "company", "id": "MSFT"}],
                                           "relationship_to_context": "same_company_followup"}}):
            time_run = self.chat.submit(conversation, "继续研究")
        time_decision = self.chat.get_run(time_run["run_id"])["decision"]
        self.block_intake_decision_job(time_run["run_id"])

        def restored(*args, **kwargs):
            self.assertEqual(kwargs.get("frozen_snapshot"), expected)
            return ResearchTools(*args, **kwargs)

        with patch("workbench.scoped_tools.ResearchTools", side_effect=restored) as factory:
            retained = self.chat.decisions.answer(
                time_decision["decision_id"], ["retain"], "", "accepted", "retain-chain")
            paused = self.wait(retained["run_id"])
            self.assertEqual(paused["status"], "blocked")
            continued = self.chat.decisions.answer(
                paused["decision"]["decision_id"], ["cash"], "", "accepted", "basis-chain")
            self.assertEqual(self.wait(continued["run_id"])["status"], "completed")
            self.assertEqual(factory.call_count, 3)

    def test_model_period_choice_does_not_refresh_data(self):
        class PeriodProvider(DecisionProvider):
            def stream(self, messages, tools, cancel_event):
                if self.calls:
                    yield from super().stream(messages, tools, cancel_event)
                    return
                self.calls += 1
                handler = next(item["handler"] for item in tools if item["name"] == "request_confirmation")
                handler(kind="time", question="选择预测期间", explanation="选择预测覆盖的财年。",
                        options=[
                            {"id": "fy2027", "label": "FY2027", "benefit": "较近期间", "limitation": "仍是假设"},
                            {"id": "fy2028", "label": "FY2028", "benefit": "较远期间", "limitation": "不确定性更高"},
                        ], multiple=False)

        self.chat.provider = StructuredTestProvider(PeriodProvider(), preserve_references=False)
        initial = self.chat.create_and_submit("MSFT", "预测收入")
        paused = self.wait(initial["run_id"])
        expected = self.chat.effective_snapshot(initial["run_id"], "MSFT")
        with patch("workbench.scoped_tools.ResearchTools", wraps=ResearchTools) as factory:
            continued = self.chat.decisions.answer(
                paused["decision"]["decision_id"], ["fy2027"], "", "accepted", "period-choice")
            self.assertEqual(self.wait(continued["run_id"])["status"], "completed")
            self.assertEqual(factory.call_args.kwargs["frozen_snapshot"], expected)

    def test_explicit_data_refresh_still_reads_current_data(self):
        self.provider.calls = 1
        initial = self.chat.create_and_submit("MSFT", "研究微软")
        self.assertEqual(self.wait(initial["run_id"])["status"], "completed")
        conversation = initial["conversation_id"]
        with closing(self.chat._connect()) as connection, connection:
            connection.execute("UPDATE messages SET created_at='2020-01-01T00:00:00Z' "
                               "WHERE conversation_id=? AND role='assistant'", (conversation,))
        with patch("workbench.research_tasks.ResearchTaskService.intake", return_value={
                "status": "ready", "scope": {"subjects": [{"kind": "company", "id": "MSFT"}],
                                           "relationship_to_context": "same_company_followup"}}):
            time_run = self.chat.submit(conversation, "更新研究")
        decision = self.chat.get_run(time_run["run_id"])["decision"]
        self.block_intake_decision_job(time_run["run_id"])
        with patch("workbench.scoped_tools.ResearchTools", wraps=ResearchTools) as factory:
            continued = self.chat.decisions.answer(
                decision["decision_id"], ["refresh"], "", "accepted", "explicit-refresh")
            self.assertEqual(self.wait(continued["run_id"])["status"], "completed")
            self.assertEqual(factory.call_count, 2)
            self.assertIsNone(factory.call_args_list[0].kwargs["frozen_snapshot"])
            self.assertIsNotNone(factory.call_args_list[1].kwargs["frozen_snapshot"])

    def test_missing_proposal_snapshot_fails_without_live_fallback(self):
        decision = self.pending()
        with patch("workbench.scoped_tools.ResearchTools", wraps=ResearchTools) as factory:
            continued = self.chat.decisions.answer(
                decision["decision_id"], ["cash"], "", "accepted", "missing-snapshot")
            result = self.wait(continued["run_id"])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "research_record_missing")
            factory.assert_not_called()
