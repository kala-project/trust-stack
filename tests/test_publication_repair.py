import json
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests.test_recorded_calculations import wait
from tests.test_report_publications import ReportProvider
from workbench.build import build_database
from workbench.chat import ChatService
from workbench.copilot_provider import CopilotProvider
from workbench.publication_repair import (
    MAX_RESPONSE_CHARS, PublicationRepairError, TransportBudget, TransportBudgetExceeded,
    apply_response, build_request, has_conflicting_claims, review_is_acceptable,
)


def arguments():
    return {
        "title": "报告 2025",
        "blocks": [{
            "title": "2025 年增长结论",
            "classification": "recorded_calculation",
            "parts": [
                {"kind": "text", "text": "按报表标题口径，增长率约为 18.10%。"},
                {"kind": "calculation", "id": "calc-fixed"},
            ],
            "support": [{"kind": "calculation", "id": "calc-fixed"}],
            "reasoning": "18.10% 是精确计算，但“约”属于不受支持的舍入限定。",
            "limitations": ["不改变 2025 年数据或来源。"],
        }],
        "datasets": [{
            "title": "冻结数据", "description": "不可编辑", "display": "table",
            "support": [{"kind": "passage", "id": "frozen-footnote"}],
            "points": [{"label": "差额", "kind": "calculation", "id": "calc-fixed"}],
        }],
        "value_style": "zh-CN",
    }


def edit(**updates):
    value = {
        "target": "part_text", "block_index": 0, "part_index": 0,
        "old_text": "按报表标题口径，增长率约为 18.10%。",
        "new_text": "增长率为 18.10%。",
    }
    value.update(updates)
    return json.dumps({"edits": [value]}, ensure_ascii=False)


class PublicationRepairHelperTests(unittest.TestCase):
    def test_allowed_edit_preserves_every_non_text_structure(self):
        original = arguments()
        candidate, edits = apply_response(original, edit())
        self.assertEqual(candidate["blocks"][0]["parts"][0]["text"], "增长率为 18.10%。")
        self.assertEqual(len(edits), 1)
        self.assertEqual(original, arguments())
        for key in ("title", "datasets", "value_style"):
            self.assertEqual(candidate[key], original[key])
        block, prior = candidate["blocks"][0], original["blocks"][0]
        for key in ("classification", "support"):
            self.assertEqual(block[key], prior[key])
        self.assertEqual(block["parts"][1], prior["parts"][1])

    def test_numeric_binding_reference_and_dataset_mutations_are_rejected(self):
        cases = [
            edit(new_text="增长率为 18.11%。"),
            edit(target="part_text", part_index=1,
                 old_text="calc-fixed", new_text="calc-other"),
            edit(target="dataset", old_text="冻结数据", new_text="别的数据"),
            json.dumps({"edits": [{"target": "block_title", "block_index": 0,
                                  "old_text": arguments()["blocks"][0]["title"],
                                  "new_text": "未经正文跨度审核的新标题"}]}, ensure_ascii=False),
            json.dumps({"edits": [{"target": "reasoning", "block_index": 0,
                                    "old_text": arguments()["blocks"][0]["reasoning"],
                                    "new_text": "19% 是精确计算。", "support": []}]}, ensure_ascii=False),
        ]
        for response in cases:
            with self.subTest(response=response), self.assertRaises(PublicationRepairError):
                apply_response(arguments(), response)

    def test_signed_exponent_and_embedded_numeric_literal_changes_are_rejected(self):
        old = arguments()
        old["blocks"][0]["parts"][0]["text"] = "FY2024 values are -1.0e-3 and -5."
        cases = [
            "FY2024 values are +1.0e3 and -5.",
            "FY2024 values are -1.0e-3 and 5.",
            "FY2025 values are -1.0e-3 and -5.",
        ]
        for new_text in cases:
            response = json.dumps({"edits": [{
                "target": "part_text", "block_index": 0, "part_index": 0,
                "old_text": "FY2024 values are -1.0e-3 and -5.", "new_text": new_text,
            }]})
            with self.subTest(new_text=new_text), self.assertRaises(PublicationRepairError):
                apply_response(old, response)

    def test_duplicate_json_keys_are_rejected_at_every_depth(self):
        packets = [
            '{"edits":[],"edits":[]}',
            '{"edits":[{"target":"part_text","target":"part_text","block_index":0,'
            '"part_index":0,"old_text":"x","new_text":"y"}]}',
        ]
        for response in packets:
            with self.subTest(response=response), self.assertRaisesRegex(
                    PublicationRepairError, "duplicate JSON key"):
                apply_response(arguments(), response)

    def test_malformed_ambiguous_empty_and_oversized_packets_are_rejected(self):
        duplicate = json.loads(edit())["edits"] * 2
        for response in ("```json\n{}\n```", "", json.dumps({"edits": []}),
                         json.dumps({"edits": duplicate}, ensure_ascii=False),
                         "x" * (MAX_RESPONSE_CHARS + 1)):
            with self.subTest(size=len(response)), self.assertRaises(PublicationRepairError):
                apply_response(arguments(), response)

    def test_request_contains_bounded_original_render_and_actual_critique(self):
        review = {"status": "assessed", "claims": [{"support_status": "unsupported"}],
                  "coverage": {"status": "answered", "specific_gaps": []}}
        request = build_request(arguments(), "rendered", review, {"tools_allowed": 0})
        self.assertEqual(request["original_publish_arguments"], arguments())
        self.assertEqual(request["rendered_draft"], "rendered")
        self.assertEqual(request["semantic_critique"], review)
        self.assertEqual(request["budgets"]["tools_allowed"], 0)

    def test_dedicated_provider_revision_is_tool_free_and_has_explicit_contract(self):
        provider = CopilotProvider()
        observed = {}

        def stream(messages, tools, cancel_event, timeout_override=None, **kwargs):
            observed.update(messages=messages, tools=tools, timeout=timeout_override, **kwargs)
            yield {"type": "final_answer", "text": '{"edits":[]}'}

        request = {"budgets": {"remaining_provider_events": 2, "max_output_chars": 100}}
        with patch.object(provider, "_stream", side_effect=stream):
            result = provider.revise_publication(request, threading.Event(), 5)
        self.assertEqual(result, '{"edits":[]}')
        self.assertEqual(observed["tools"], [])
        self.assertIn("structured-publication text editor", observed["system_message"])
        self.assertNotIn("publish_research_report", observed["system_message"])

    def test_provider_counts_oversized_non_delta_and_delta_plus_final_payloads(self):
        provider = CopilotProvider()
        request = {"budgets": {"max_output_chars": 100}}
        cases = [
            ([{"type": "usage", "details": "x" * 200}], 100),
            ([{"type": "delta", "text": "x" * 40},
              {"type": "final_answer", "text": "y" * 40}], 100),
        ]
        for events, cap in cases:
            budget = TransportBudget(max_events=10, max_payload_chars=cap)
            with self.subTest(events=events), patch.object(provider, "_stream", return_value=iter(events)):
                with self.assertRaises(TransportBudgetExceeded):
                    provider.revise_publication(request, threading.Event(), 5, budget)

    def test_request_reservations_are_atomic_and_separate_from_output_limit(self):
        payload = {"request": "x" * 20}
        size = TransportBudget._serialized_chars(payload)
        budget = TransportBudget(10, 100, max_request_chars=2 * size)
        barrier = threading.Barrier(5)
        outcomes = []
        def reserve():
            barrier.wait(1)
            try:
                budget.consume_request(payload)
                outcomes.append(True)
            except TransportBudgetExceeded:
                outcomes.append(False)
        workers = [threading.Thread(target=reserve) for _ in range(5)]
        for worker in workers: worker.start()
        for worker in workers: worker.join(2)
        self.assertEqual(outcomes.count(True), 2)
        self.assertEqual(budget.snapshot()["request_chars_reserved"], 2 * size)
        self.assertEqual(budget.snapshot()["remaining_request_chars"], 0)
        self.assertEqual(budget.snapshot()["payload_chars_used"], 0)

    def test_outbound_request_limit_rejects_before_provider_stream(self):
        provider = CopilotProvider()
        for method, request in ((provider.review_research_answer, {"draft": "x", "authoritative_context": {}}),
                                (provider.revise_publication, {"budgets": {"max_output_chars": 100}})):
            with self.subTest(method=method.__name__), patch.object(provider, "_stream") as stream:
                with self.assertRaises(TransportBudgetExceeded):
                    method(request, threading.Event(), 5, TransportBudget(10,1000,max_request_chars=1))
                stream.assert_not_called()

    def test_large_review_submission_is_debited_even_without_yielded_tool_event(self):
        provider = CopilotProvider()
        budget = TransportBudget(max_events=10, max_payload_chars=100)

        def stream(messages, tools, cancel_event, **kwargs):
            tools[0]["handler"](
                claims=[{"id": "c1", "start": 0, "end": 1, "exact_text": "x",
                         "support_status": "unsupported", "evidence_refs": [],
                         "reason": "x" * 200}],
                coverage={"status": "answered", "specific_gaps": []},
            )
            if False:
                yield None

        with patch.object(provider, "_stream", stream), self.assertRaises(TransportBudgetExceeded):
            provider.review_research_answer({"draft": "x", "authoritative_context": {}}, threading.Event(), 5, budget)

    def test_direct_semantic_call_has_bounded_local_transport(self):
        provider = CopilotProvider()

        def stream(messages, tools, cancel_event, **kwargs):
            yield {"type": "usage", "details": "x" * 50_000}

        with patch.object(provider, "_stream", stream), self.assertRaises(TransportBudgetExceeded):
            provider.review_research_answer({"draft": "x", "authoritative_context": {}}, threading.Event(), 5)

    def test_review_gate_skips_clean_or_unassessed_and_requires_non_regression(self):
        unsupported = {"status": "assessed", "claims": [{"support_status": "unsupported"}],
                       "coverage": {"status": "answered"}}
        clean = {"status": "assessed", "claims": [{"support_status": "inference"}],
                 "coverage": {"status": "answered"}}
        regressed = deepcopy(clean)
        regressed["coverage"] = {"status": "partial"}
        self.assertTrue(has_conflicting_claims(unsupported))
        self.assertFalse(has_conflicting_claims(clean))
        self.assertFalse(has_conflicting_claims({"status": "not_assessed"}))
        self.assertTrue(review_is_acceptable(unsupported, clean))
        self.assertFalse(review_is_acceptable(unsupported, regressed))
        self.assertFalse(review_is_acceptable(unsupported, {"status": "not_assessed"}))


class RepairingReportProvider(ReportProvider):
    supports_shared_transport_budget = True

    def __init__(self, *, second="clean"):
        super().__init__()
        self.review_calls = 0
        self.repair_calls = 0
        self.second = second

    def review_research_answer(self, request, cancel_event, timeout_seconds, transport_budget):
        self.review_calls += 1
        if self.review_calls == 1:
            exact, status, coverage = "明确绑定的差额为😀", "unsupported", "answered"
        else:
            exact, status = "冻结记录的差额为😀", "inference"
            coverage = "partial" if self.second == "regressed" else "answered"
        evidence_refs = ([] if self.review_calls == 1 else
                         [{"kind": "calculation", "id": self.calculation_id}])
        result = {"claims": [{"id": "c1", "start": request["draft"].index(exact),
                              "end": request["draft"].index(exact) + len(exact),
                              "exact_text": exact, "support_status": status,
                              "evidence_refs": evidence_refs, "reason": "bounded test critique"}],
                  "coverage": {"status": coverage,
                               "specific_gaps": (["candidate omitted requested scope"]
                                                 if coverage == "partial" else [])}}
        transport_budget.consume_submission(result)
        transport_budget.consume_event({"type": "tool_call", "name": "submit_semantic_review"})
        return result

    def revise_publication(self, request, cancel_event, timeout_seconds, transport_budget):
        self.repair_calls += 1
        result = json.dumps({"edits": [{
            "target": "part_text", "block_index": 0, "part_index": 0,
            "old_text": "明确绑定的差额为😀 ", "new_text": "冻结记录的差额为😀 ",
        }]}, ensure_ascii=False)
        transport_budget.consume_event({"type": "final_answer", "text": result})
        return result


class PublicationRepairIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.TemporaryDirectory()
        cls.db = Path(cls.root.name) / "evidence.sqlite3"
        build_database(db_path=cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.root.cleanup()

    def run_report(self, provider, *, run_timeout=None, max_output_chars=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        chat = ChatService(self.db, Path(temp.name) / "chat.sqlite3", provider)
        if run_timeout is not None:
            chat.run_timeout_seconds = run_timeout
        if max_output_chars is not None:
            chat.max_output_chars = max_output_chars
        self.addCleanup(chat.close)
        submitted = chat.create_and_submit("MSFT", "微软收入相对以前的累计变化")
        run = wait(chat, submitted["run_id"])
        self.assertEqual(run["status"], "completed", run.get("error"))
        conversation = chat.get_conversation(submitted["conversation_id"])
        return run, conversation["messages"][-1]

    def test_accepted_qualifier_repair_is_committed_once_with_consistent_final_review(self):
        provider = RepairingReportProvider()
        original_blocks = None
        run, message = self.run_report(provider)
        original_blocks = provider.blocks
        self.assertEqual(provider.repair_calls, 1)
        self.assertEqual(provider.review_calls, 2)
        self.assertIn("冻结记录的差额为😀", message["content"])
        self.assertEqual(original_blocks[0]["parts"][0]["text"], "明确绑定的差额为😀 ")
        outcome = [e for e in run["events"] if e["type"] == "publication_repair_outcome"][-1]
        self.assertEqual(outcome["outcome"], "accepted")
        self.assertEqual(outcome["transport_budget"]["events_used"], 3)
        self.assertGreater(outcome["transport_budget"]["payload_chars_used"], 0)
        authoritative = [e["review"] for e in run["events"]
                         if e["type"] == "semantic_review" and e.get("authoritative")]
        self.assertEqual(message["verification"]["semantic_review"], authoritative[-1])
        self.assertFalse(message["verification"]["quality_check"]["semantic_verified"])

    def test_candidate_rereview_uses_its_recompiled_publication_contract(self):
        class DatasetRepairProvider(RepairingReportProvider):
            def __init__(self):
                super().__init__()
                self.review_requests = []

            def stream(self, messages, tools, cancel_event):
                yield from super(ReportProvider, self).stream(messages, tools, cancel_event)
                publish = next(item["handler"] for item in tools
                               if item["name"] == "publish_research_report")
                self.blocks = [{
                    "classification": "recorded_calculation",
                    "parts": [{"kind": "text", "text": "明确绑定的差额为😀 "},
                              {"kind": "calculation", "id": self.calculation_id}],
                    "support": [], "reasoning": "", "limitations": [],
                }]
                publish(title="完整研究报告", blocks=self.blocks, datasets=[{
                    "title": "冻结差额比较", "description": "编译展示契约", "display": "line",
                    "points": [{"label": "点一", "kind": "calculation", "id": self.calculation_id},
                               {"label": "点二", "kind": "calculation", "id": self.calculation_id}],
                }])

            def review_research_answer(self, request, cancel_event, timeout_seconds,
                                       transport_budget):
                self.review_requests.append(deepcopy(request))
                return super().review_research_answer(
                    request, cancel_event, timeout_seconds, transport_budget)

        provider = DatasetRepairProvider()
        run, message = self.run_report(provider)
        self.assertEqual(len(provider.review_requests), 2)
        original, candidate = provider.review_requests
        self.assertIn("明确绑定的差额为😀", original["draft"])
        self.assertIn("冻结记录的差额为😀", candidate["draft"])
        for request in provider.review_requests:
            contract = request["authoritative_context"]["compiled_presentation_contracts"][0]
            self.assertEqual(contract["provenance"], "program_compiled_publication")
            self.assertFalse(contract["chart"]["real_time_axis"])
            self.assertEqual(contract["chart"]["x"], "label")
        outcome = [event for event in run["events"]
                   if event["type"] == "publication_repair_outcome"][-1]
        self.assertEqual(outcome["outcome"], "accepted")
        self.assertIn("冻结记录的差额为😀", message["content"])

    def test_regressed_second_review_rejects_candidate_and_retains_original_review(self):
        provider = RepairingReportProvider(second="regressed")
        run, message = self.run_report(provider)
        self.assertIn("明确绑定的差额为😀", message["content"])
        self.assertNotIn("冻结记录的差额为😀", message["content"])
        outcome = [e for e in run["events"] if e["type"] == "publication_repair_outcome"][-1]
        self.assertEqual(outcome["outcome"], "rejected")
        self.assertEqual(message["verification"]["semantic_review"], outcome["original_review"])

    def test_provider_without_dedicated_support_does_not_attempt_repair(self):
        class CritiqueOnlyProvider(ReportProvider):
            def review_research_answer(self, request, cancel_event, timeout_seconds):
                exact = "明确绑定的差额为😀"
                start = request["draft"].index(exact)
                return {"claims": [{"id": "c1", "start": start, "end": start + len(exact),
                                    "exact_text": exact, "support_status": "unsupported",
                                    "evidence_refs": [], "reason": "unsupported qualifier"}],
                        "coverage": {"status": "answered", "specific_gaps": []}}

        provider = CritiqueOnlyProvider()
        run, _message = self.run_report(provider)
        outcome = [e for e in run["events"] if e["type"] == "publication_repair_outcome"][-1]
        self.assertFalse(outcome["attempted"])
        self.assertEqual(outcome["outcome"], "provider_unsupported")

    def test_legacy_provider_with_revision_method_is_fail_safe_unsupported(self):
        class LegacyProvider(ReportProvider):
            def review_research_answer(self, request, cancel_event, timeout_seconds):
                exact = "明确绑定的差额为😀"
                start = request["draft"].index(exact)
                return {"claims": [{"id": "c1", "start": start, "end": start + len(exact),
                                    "exact_text": exact, "support_status": "unsupported",
                                    "evidence_refs": [], "reason": "legacy critique"}],
                        "coverage": {"status": "answered", "specific_gaps": []}}

            def revise_publication(self, request, cancel_event, timeout_seconds):
                raise AssertionError("legacy revision must not be called without shared accounting")

        run, _message = self.run_report(LegacyProvider())
        outcome = [e for e in run["events"] if e["type"] == "publication_repair_outcome"][-1]
        self.assertEqual(outcome["outcome"], "provider_unsupported")

    def test_clean_review_skips_repair(self):
        class CleanProvider(RepairingReportProvider):
            def review_research_answer(self, request, cancel_event, timeout_seconds, transport_budget):
                self.review_calls += 1
                exact = "明确绑定的差额为😀"
                start = request["draft"].index(exact)
                result = {"claims": [{"id": "c1", "start": start, "end": start + len(exact),
                                      "exact_text": exact, "support_status": "inference",
                                      "evidence_refs": [{"kind": "calculation", "id": self.calculation_id}],
                                      "reason": "bounded calculation inference"}],
                          "coverage": {"status": "answered", "specific_gaps": []}}
                transport_budget.consume_submission(result)
                transport_budget.consume_event({"type": "tool_call"})
                return result

        provider = CleanProvider()
        run, _message = self.run_report(provider)
        outcome = [e for e in run["events"] if e["type"] == "publication_repair_outcome"][-1]
        self.assertEqual(outcome["outcome"], "not_needed")
        self.assertEqual(provider.repair_calls, 0)
        self.assertEqual(provider.review_calls, 1)

    def test_insufficient_shared_deadline_skips_repair_and_retains_original(self):
        provider = RepairingReportProvider()
        run, message = self.run_report(provider, run_timeout=125)
        outcome = [e for e in run["events"] if e["type"] == "publication_repair_outcome"][-1]
        self.assertEqual(outcome["outcome"], "timed_out")
        self.assertFalse(outcome["attempted"])
        self.assertEqual(provider.repair_calls, 0)
        self.assertIn("明确绑定的差额为😀", message["content"])

    def test_initial_semantic_review_transport_exhaustion_fails_closed(self):
        class ExhaustingInitial(RepairingReportProvider):
            def review_research_answer(self, request, cancel_event, timeout_seconds, transport_budget):
                self.review_calls += 1
                transport_budget.consume_event({"type": "usage", "details": "x" * 200_000})

        provider = ExhaustingInitial()
        run, message = self.run_report(provider)
        semantic = [e for e in run["events"] if e["type"] == "semantic_review"][-1]
        self.assertEqual(semantic["review"]["reason"], "transport_budget_exhausted")
        self.assertEqual(provider.repair_calls, 0)
        self.assertIn("明确绑定的差额为😀", message["content"])

    def test_candidate_review_uses_remaining_budget_and_exhaustion_rejects_candidate(self):
        class ExhaustingCandidate(RepairingReportProvider):
            def review_research_answer(self, request, cancel_event, timeout_seconds, transport_budget):
                if self.review_calls == 0:
                    return super().review_research_answer(
                        request, cancel_event, timeout_seconds, transport_budget)
                self.review_calls += 1
                transport_budget.consume_event({"type": "usage", "details": "x" * 200_000})

        provider = ExhaustingCandidate()
        run, message = self.run_report(provider)
        outcome = [e for e in run["events"] if e["type"] == "publication_repair_outcome"][-1]
        self.assertEqual(outcome["outcome"], "budget_exhausted")
        self.assertGreater(outcome["transport_budget"]["events_used"], 0)
        self.assertIn("明确绑定的差额为😀", message["content"])
        self.assertNotIn("冻结记录的差额为😀", message["content"])

    def test_candidate_review_timeout_records_outcome_and_retains_original(self):
        class TimedOutCandidateReview(RepairingReportProvider):
            def review_research_answer(self, request, cancel_event, timeout_seconds, transport_budget):
                if self.review_calls == 0:
                    return super().review_research_answer(
                        request, cancel_event, timeout_seconds, transport_budget)
                self.review_calls += 1
                raise TimeoutError("controlled candidate review timeout")

        provider = TimedOutCandidateReview()
        run, message = self.run_report(provider)
        outcome = [e for e in run["events"] if e["type"] == "publication_repair_outcome"][-1]
        self.assertEqual(provider.review_calls, 2)
        self.assertTrue(outcome["attempted"])
        self.assertEqual(outcome["candidate_review"]["reason"], "timed_out")
        self.assertEqual(outcome["outcome"], "timed_out")
        self.assertIn("明确绑定的差额为😀", message["content"])
        self.assertNotIn("冻结记录的差额为😀", message["content"])

    def test_user_cancellation_during_candidate_review_records_outcome_and_does_not_commit(self):
        class BlockingCandidateReview(RepairingReportProvider):
            def __init__(self):
                super().__init__()
                self.entered = threading.Event()

            def review_research_answer(self, request, cancel_event, timeout_seconds, transport_budget):
                if self.review_calls == 0:
                    return super().review_research_answer(
                        request, cancel_event, timeout_seconds, transport_budget)
                self.review_calls += 1
                self.entered.set()
                while not cancel_event.is_set():
                    time.sleep(0.005)
                return None

        provider = BlockingCandidateReview()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        chat = ChatService(self.db, Path(temp.name) / "chat.sqlite3", provider)
        self.addCleanup(chat.close)
        submitted = chat.create_and_submit("MSFT", "微软收入相对以前的累计变化")
        self.assertTrue(provider.entered.wait(2))
        chat.cancel(submitted["run_id"])
        run = wait(chat, submitted["run_id"])
        self.assertEqual(run["status"], "cancelled")
        deadline = time.monotonic() + 1
        while (not any(e["type"] == "publication_repair_outcome" for e in run["events"])
               and time.monotonic() < deadline):
            time.sleep(0.01)
            run = chat.get_run(submitted["run_id"])
        outcome = [e for e in run["events"] if e["type"] == "publication_repair_outcome"][-1]
        self.assertEqual(outcome["outcome"], "cancelled")
        self.assertTrue(outcome["attempted"])
        messages = chat.get_conversation(submitted["conversation_id"])["messages"]
        self.assertFalse(any(item["role"] == "assistant" for item in messages))

    def test_user_cancellation_during_repair_does_not_commit_candidate(self):
        class BlockingProvider(RepairingReportProvider):
            def __init__(self):
                super().__init__()
                self.entered = threading.Event()

            def revise_publication(self, request, cancel_event, timeout_seconds, transport_budget):
                self.repair_calls += 1
                self.entered.set()
                while not cancel_event.is_set():
                    time.sleep(0.005)
                return None

        provider = BlockingProvider()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        chat = ChatService(self.db, Path(temp.name) / "chat.sqlite3", provider)
        self.addCleanup(chat.close)
        submitted = chat.create_and_submit("MSFT", "微软收入相对以前的累计变化")
        self.assertTrue(provider.entered.wait(2))
        chat.cancel(submitted["run_id"])
        run = wait(chat, submitted["run_id"])
        self.assertEqual(run["status"], "cancelled")
        deadline = time.monotonic() + 1
        while (not any(e["type"] == "publication_repair_outcome" for e in run["events"])
               and time.monotonic() < deadline):
            time.sleep(0.01)
            run = chat.get_run(submitted["run_id"])
        outcome = [e for e in run["events"] if e["type"] == "publication_repair_outcome"][-1]
        self.assertEqual(outcome["outcome"], "cancelled")
        messages = chat.get_conversation(submitted["conversation_id"])["messages"]
        self.assertFalse(any(item["role"] == "assistant" for item in messages))


if __name__ == "__main__":
    unittest.main()
