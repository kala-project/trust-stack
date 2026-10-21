import copy
import json
from pathlib import Path
import tempfile
import unittest

from tests.test_recorded_calculations import wait
from tests.test_research_tasks import CountingProvider
from workbench.answer_artifacts import AnswerArtifactService
from workbench.build import build_database
from workbench.chat import ChatService
from workbench.report_reader import build_reader_payload
from workbench.research_intent import validate_intent
from workbench.research_tasks import ResearchTaskService


class _RecordedRuns:
    def __init__(self, runs):
        self.runs = runs

    def get_run(self, run_id):
        return copy.deepcopy(self.runs[run_id])


class QuestionSnapshotContractTests(unittest.TestCase):
    def test_new_task_artifact_freezes_its_exact_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            evidence_db = Path(directory) / "evidence.sqlite3"
            build_database(db_path=evidence_db)
            chat = ChatService(evidence_db, Path(directory) / "chat.sqlite3", CountingProvider())
            self.addCleanup(chat.close)
            tasks = ResearchTaskService(chat)
            task = tasks.create("微软收入是多少", None, "question-snapshot-contract")
            bridge = tasks.start(task["task_id"], task["current_scope_version"])
            run = wait(chat, bridge["run_id"])
            self.assertEqual(run["status"], "completed")
            message = chat.get_conversation(bridge["conversation_id"])["messages"][-1]
            artifact = AnswerArtifactService(chat).get(
                bridge["conversation_id"], message["message_id"]
            )

            snapshot = artifact["question_snapshot"]
            self.assertEqual(snapshot["original_question"], "微软收入是多少")
            self.assertEqual(snapshot["recorded_scope"]["directions"], ["test follow-up"])
            self.assertEqual(snapshot["recorded_assumptions"], [])
            self.assertIsNone(snapshot["normalized_question"])
            self.assertEqual(len([event for event in run["events"]
                                  if event["type"] == "question_context"]), 1)

    def test_optional_normalized_question_is_a_real_model_field(self):
        intent = {
            "kind": "company_research",
            "subjects": [{"kind": "company", "name": "Microsoft", "candidate_id": "MSFT"}],
            "directions": ["分析资本开支趋势"],
            "metrics": ["资本开支"],
            "period_text": "最近十个财年",
            "basis": ["现金流量表口径"],
            "unit": None,
            "assumptions": ["财年按公司披露口径"],
            "uncertainties": ["融资租赁是否纳入尚未确认"],
            "excluded_entities": [],
            "is_continuation": False,
            "identity_ambiguity": False,
            "summary": "微软资本开支趋势",
            "normalized_question": "微软最近十个财年的资本开支如何变化？",
        }
        self.assertEqual(validate_intent(intent)["normalized_question"],
                         "微软最近十个财年的资本开支如何变化？")
        del intent["normalized_question"]
        self.assertIsNone(validate_intent(intent)["normalized_question"])

    def test_confirmed_time_choice_is_carried_from_the_real_event_chain(self):
        resolution = {
            "status": "ready", "code": "scope_ready", "message": "ready", "options": [],
            "scope": {
                "subjects": [{"kind": "company", "id": "MSFT", "label": "Microsoft"}],
                "period_text": "FY2020-FY2025", "metrics": ["资本开支"],
                "basis": ["现金流量表口径"], "unit": "USD million",
                "directions": ["比较年度变化"], "assumptions": ["沿用披露财年"],
                "uncertainties": ["融资租赁是否纳入尚未确认"], "ambiguities": [], "gaps": [],
                "normalized_question": "微软FY2020至FY2025的资本开支如何变化？",
            },
        }
        context_event = {
            "type": "question_context", "authoritative": True,
            "original_question": "请继续看资本开支", "resolution": resolution,
        }
        time_context = {
            "decision": "这次追问采用哪个时间点的资料？",
            "explanation": "资料可能已经更新；确认后再继续。", "kind": "time",
            "selected": [{
                "id": "retain", "label": "沿用上一版资料",
                "benefit": "方便比较", "limitation": "不纳入后来数据",
            }],
            "user_notes": "保持上一版截至日",
        }
        runs = _RecordedRuns({
            "continued": {"events": [{
                "type": "confirmed_decision", "authoritative": True,
                "decision_id": "decision-time", "kind": "time", "source_run_id": "blocked",
                "context": time_context,
            }]},
            "blocked": {"events": [context_event]},
        })
        snapshot = ChatService.question_snapshot_for(runs, {
            "run_id": "continued", "question": "继续研究。已确认：沿用上一版资料。",
        })

        self.assertEqual(snapshot["original_question"], "请继续看资本开支")
        self.assertEqual(snapshot["latest_user_question"], "请继续看资本开支")
        self.assertEqual(snapshot["normalized_question"],
                         "微软FY2020至FY2025的资本开支如何变化？")
        self.assertEqual(snapshot["recorded_assumptions"], ["沿用披露财年"])
        self.assertEqual(snapshot["open_uncertainties"], ["融资租赁是否纳入尚未确认"])
        self.assertEqual(snapshot["time_choice"]["selected"][0]["id"], "retain")
        self.assertEqual(snapshot["time_choice"]["user_notes"], "保持上一版截至日")

    def test_reader_uses_snapshot_and_legacy_artifact_remains_unmodified(self):
        legacy = {
            "schema_version": "answer-artifact/1.0", "algorithm_version": "legacy",
            "conversation_id": "conv", "message_id": "msg", "run_id": "run",
            "artifact_id": "artifact-legacy", "question": "原始用户问题",
            "findings": [], "anchors": [], "traces": [], "calculations": [],
            "datasets": [], "limitations": [],
        }
        before = json.dumps(legacy, ensure_ascii=False, sort_keys=True)
        old_payload = build_reader_payload(legacy)
        self.assertEqual(old_payload["question"]["original"], "原始用户问题")
        self.assertEqual(old_payload["question"]["normalized"], "")
        self.assertEqual(old_payload["question"]["normalization"], [])
        self.assertEqual(json.dumps(legacy, ensure_ascii=False, sort_keys=True), before)

        current = copy.deepcopy(legacy)
        current["artifact_id"] = "artifact-current"
        current["question_snapshot"] = {
            "schema_version": "question-snapshot/1",
            "original_question": "原始用户问题",
            "latest_user_question": "请继续看资本开支",
            "normalized_question": "微软FY2020至FY2025的资本开支如何变化？",
            "recorded_scope": {
                "subjects": [{"kind": "company", "id": "MSFT", "label": "Microsoft"}],
                "period_text": "FY2020-FY2025", "metrics": ["资本开支"],
                "basis": ["现金流量表口径"], "directions": ["比较年度变化"],
            },
            "recorded_assumptions": ["沿用披露财年"],
            "open_uncertainties": ["融资租赁是否纳入尚未确认"],
            "scope_gaps": [],
            "clarifications": [{
                "decision_id": "decision-time", "kind": "time",
                "question": "这次追问采用哪个时间点的资料？",
                "explanation": "资料可能已经更新；确认后再继续。",
                "selected": [{"id": "retain", "label": "沿用上一版资料"}],
                "user_notes": "保持上一版截至日",
            }],
            "time_choice": {
                "decision_id": "decision-time", "kind": "time",
                "selected": [{"id": "retain", "label": "沿用上一版资料"}],
                "user_notes": "保持上一版截至日",
            },
        }
        question = build_reader_payload(current)["question"]
        self.assertEqual(question["normalized"],
                         "微软FY2020至FY2025的资本开支如何变化？")
        self.assertEqual(question["scope"]["period_text"], "FY2020-FY2025")
        self.assertEqual(question["assumptions"], ["沿用披露财年"])
        self.assertEqual(question["open_uncertainties"], ["融资租赁是否纳入尚未确认"])
        self.assertEqual(question["time_choice"]["selected"][0]["id"], "retain")
        texts = [item["text"] for item in question["normalization"]]
        self.assertIn("沿用上一版资料", texts)
        self.assertIn("保持上一版截至日", texts)
        self.assertNotIn("原始用户问题", texts)


if __name__ == "__main__":
    unittest.main()
