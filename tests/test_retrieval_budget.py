from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
import unittest

from workbench.build import build_database
from workbench.chat import ChatService
from workbench.documents import DocumentService
from workbench.research_tools import ToolError
from workbench.retrieval_budget import POLICY_VERSION, RetrievalBudget
from tests.support_chat_provider import ExplicitMSFTIntentAdapter


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/source-catalog.json"
TERMINAL = {"completed", "failed", "cancelled", "blocked"}


def budget(**overrides):
    values = {
        "company_id": "MSFT",
        "data_version": "frozen-data-v1",
        "document_version_ids": ["doc-v1"],
    }
    values.update(overrides)
    return RetrievalBudget(**values)


def passage_result(identifier):
    return {
        "company_id": "MSFT",
        "results": [{
            "passage_id": identifier,
            "document_version_id": "doc-v1",
            "company_id": "MSFT",
        }],
    }


class RetrievalBudgetUnitTests(unittest.TestCase):
    def test_exact_scoped_duplicate_is_cached_without_original_execution(self):
        policy = budget()
        calls = []

        def original():
            calls.append("called")
            return passage_result("passage-1")

        first = policy.execute("search_documents", {"query": "Revenue", "limit": 8}, original)
        second = policy.execute("search_documents", {"limit": 8, "query": "Revenue"}, original)
        self.assertEqual(first.action, "accepted")
        self.assertEqual(second.action, "cached")
        self.assertEqual(calls, ["called"])
        self.assertEqual(second.result["_retrieval_policy"]["cache_marker"], "exact_scoped_arguments")
        self.assertEqual(second.result["results"], first.result["results"])

    def test_argument_and_frozen_scope_differences_do_not_share_cache_keys(self):
        policy = budget()
        calls = []

        def original():
            calls.append(1)
            return passage_result(f"passage-{len(calls)}")

        policy.execute("search_documents", {"query": "Revenue", "limit": 8}, original)
        changed = policy.execute("search_documents", {"query": "Revenue", "limit": 9}, original)
        self.assertEqual(changed.action, "accepted")
        self.assertEqual(len(calls), 2)

        other_company = budget(company_id="AAPL")
        other_version = budget(document_version_ids=["doc-v2"])
        arguments = {"query": "Revenue", "limit": 8}
        self.assertNotEqual(
            policy._cache_key("search_documents", arguments),
            other_company._cache_key("search_documents", arguments),
        )
        self.assertNotEqual(
            policy._cache_key("search_documents", arguments),
            other_version._cache_key("search_documents", arguments),
        )

    def test_search_cap_prevents_original_calls_and_fifth_denial_terminates(self):
        policy = budget(search_limit=6, saturation_limit=3, denial_limit=5)
        calls = []
        for index in range(6):
            outcome = policy.execute(
                "search_documents",
                {"query": f"query-{index}"},
                lambda index=index: calls.append(index) or passage_result(f"passage-{index}"),
            )
            self.assertEqual(outcome.action, "accepted")
        self.assertEqual(len(calls), 6)

        denials = []
        for index in range(5):
            outcome = policy.execute(
                "search_documents",
                {"query": f"blocked-{index}"},
                lambda: self.fail("closed retrieval executed an original search"),
            )
            denials.append(outcome)
        self.assertTrue(all(item.action == "denied" for item in denials))
        self.assertFalse(any(item.terminate for item in denials[:-1]))
        self.assertTrue(denials[-1].terminate)
        self.assertEqual(denials[0].result["status"], "retrieval_closed")
        self.assertEqual(denials[0].result["error"]["code"], "retrieval_closed")
        self.assertIn("unresolved", denials[0].result["instruction"])
        self.assertEqual(policy.progress()["accepted"], 6)
        self.assertEqual(policy.progress()["denied"], 5)

    def test_repeated_ids_warn_without_deciding_semantic_coverage_or_closing_search(self):
        policy = budget(saturation_limit=3)
        for index in range(4):
            outcome = policy.execute(
                "search_documents",
                {"query": f"different wording {index}"},
                lambda: passage_result("same-passage"),
            )
            self.assertEqual(outcome.action, "accepted")
        progress = policy.progress()
        self.assertFalse(progress["closed"])
        self.assertIsNone(progress["closed_reason"])
        self.assertTrue(progress["repetition_warning"])
        self.assertEqual(progress["coverage_status"], "not_assessed")
        self.assertEqual(progress["consecutive_no_new_result_ids"], 3)
        self.assertEqual(progress["known_refs"]["passage_ids"], ["same-passage"])
        self.assertEqual(policy.metadata_for_tool()["decision"], "non_search_allowed")

        accepted = policy.execute(
            "search_documents", {"query": "another query"},
            lambda: passage_result("new-relevant-passage"),
        )
        self.assertEqual(accepted.action, "accepted")
        self.assertFalse(policy.progress()["repetition_warning"])

    def test_default_policy_does_not_close_after_six_useful_searches(self):
        policy = budget()
        for index in range(20):
            result = policy.execute("search_documents", {"query": str(index)},
                                    lambda index=index: passage_result(f"passage-{index}"))
            self.assertEqual(result.action, "accepted")
        self.assertFalse(policy.progress()["closed"])
        self.assertEqual(policy.progress()["accepted"], 20)

    def test_new_result_id_resets_saturation_streak(self):
        policy = budget(search_limit=7, saturation_limit=3)
        identifiers = ["a", "a", "a", "b", "b", "b"]
        for index, identifier in enumerate(identifiers):
            outcome = policy.execute(
                "search_documents", {"query": str(index)},
                lambda identifier=identifier: passage_result(identifier),
            )
            self.assertEqual(outcome.action, "accepted")
        self.assertFalse(policy.progress()["closed"])
        self.assertEqual(policy.progress()["consecutive_no_new_result_ids"], 2)

    def test_known_passages_keep_search_rank_and_bounded_informative_quotes(self):
        policy = budget(search_limit=1)
        result = {
            "results": [
                {"passage_id": "z-ranked-first", "document_version_id": "doc-v1",
                 "title": "Release", "locator": {"start": 10, "end": 20},
                 "quote": "The exact disclosed amount is $9.4 billion."},
                {"passage_id": "a-ranked-second", "document_version_id": "doc-v1",
                 "quote": "Less relevant text."},
            ]
        }
        accepted = policy.execute("search_documents", {"query": "amount"}, lambda: result)
        known = accepted.result["_retrieval_policy"]["known_refs"]
        self.assertEqual(known["passage_ids"], ["z-ranked-first", "a-ranked-second"])
        self.assertEqual(known["passage_candidates"][0]["passage_id"], "z-ranked-first")
        self.assertIn("$9.4 billion", known["passage_candidates"][0]["quote"])

        denied = policy.execute(
            "search_documents", {"query": "more"},
            lambda: self.fail("closed retrieval executed an original search"),
        )
        self.assertEqual(
            denied.result["known_candidate_passages"], known["passage_candidates"]
        )

    def test_concurrent_exact_duplicates_have_one_original_execution(self):
        policy = budget()
        calls = 0
        calls_lock = threading.Lock()

        def original():
            nonlocal calls
            with calls_lock:
                calls += 1
            time.sleep(0.02)
            return passage_result("thread-safe")

        with ThreadPoolExecutor(max_workers=12) as pool:
            outcomes = list(pool.map(
                lambda _: policy.execute("search_documents", {"query": "same"}, original),
                range(20),
            ))
        self.assertEqual(calls, 1)
        self.assertEqual(sum(item.action == "accepted" for item in outcomes), 1)
        self.assertEqual(sum(item.action == "cached" for item in outcomes), 19)


class StagedSearchProvider(ExplicitMSFTIntentAdapter):
    def __init__(self, *, ignore_closure=False):
        self.ignore_closure = ignore_closure
        self.read_result = None

    def status(self):
        return {"available": True, "reason": "", "provider": "retrieval-test"}

    def stream(self, messages, tools, cancel_event):
        by_name = {item["name"]: item["handler"] for item in tools}
        search = by_name["search_documents"]
        first = search(query="UniqueRetrievalToken", limit=1)
        passage_id = first["results"][0]["passage_id"]
        search(query="UniqueRetrievalToken", limit=1)  # exact cache hit
        for limit in (2, 3, 4):
            search(query="UniqueRetrievalToken", limit=limit)

        subsequent = search(query="UniqueRetrievalToken", limit=5)
        if self.ignore_closure:
            for limit in (6, 7, 8, 9):
                try:
                    search(query="UniqueRetrievalToken", limit=limit)
                except ToolError:
                    return
            return

        self.read_result = by_name["read_passage"](passage_id=passage_id, context_chars=1200)
        assert subsequent["_retrieval_policy"]["repetition_warning"]
        yield {"type": "delta", "text": f"Supported disclosure [passage:{passage_id}]"}


class RetrievalBudgetIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.evidence_db = Path(cls.fixture.name) / "evidence.sqlite3"
        build_database(CATALOG, cls.evidence_db, ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.services = []
        self.addCleanup(self._close_services)
        self.documents = DocumentService(Path(self.temp.name) / "documents")
        source = Path(self.temp.name) / "source.html"
        source.write_text(
            "<html><body>UniqueRetrievalToken original quotation remains intact.</body></html>",
            encoding="utf-8",
        )
        self.documents.register_file(source, company_id="MSFT", source_key="retrieval", title="Source")

    def _close_services(self):
        for service in self.services:
            service.close()

    def run_provider(self, provider):
        service = ChatService(
            self.evidence_db,
            Path(self.temp.name) / f"chat-{len(self.services)}.sqlite3",
            provider,
            document_service=self.documents,
        )
        service.max_tool_calls = 8
        self.services.append(service)
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "Find the original disclosure")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = service.get_run(submitted["run_id"])
            if result["status"] in TERMINAL:
                return service, conversation, result
            time.sleep(0.01)
        self.fail("run did not terminate")

    def test_cache_and_repetition_signals_preserve_agent_choice_and_original_reads(self):
        provider = StagedSearchProvider()
        service, conversation, result = self.run_provider(provider)
        self.assertEqual(result["status"], "completed")
        self.assertIn("original quotation remains intact", provider.read_result["context"])

        policy_event = next(item for item in result["events"] if item["type"] == "retrieval_policy")
        self.assertEqual(policy_event["version"], POLICY_VERSION)
        self.assertIsNone(policy_event["search_limit"])
        self.assertEqual(policy_event["saturation_limit"], 3)
        self.assertEqual(policy_event["denial_limit"], 5)
        self.assertTrue(policy_event["authoritative"])
        decisions = [item for item in result["events"] if item["type"] == "policy_decision"]
        self.assertEqual([item["decision"] for item in decisions],
                         ["accepted", "cached", "accepted", "accepted", "accepted", "accepted"])
        final_progress = decisions[-1]["progress"]
        self.assertEqual(
            {key: final_progress[key] for key in ("accepted", "cached", "denied")},
            {"accepted": 5, "cached": 1, "denied": 0},
        )
        self.assertEqual(len(final_progress["known_refs"]["passage_ids"]), 1)
        actual_searches = [
            item for item in result["events"]
            if item["type"] == "tool_call" and item["name"] == "search_documents"
        ]
        self.assertEqual(len(actual_searches), 5)
        self.assertTrue(any(
            item["type"] == "tool_call" and item["name"] == "read_passage"
            for item in result["events"]
        ))
        assistant = service.get_conversation(conversation["conversation_id"])["messages"][-1]
        self.assertTrue(assistant["citations"][0]["valid"])

    def test_ignoring_progress_still_cannot_bypass_shared_execution_budget(self):
        _, _, result = self.run_provider(StagedSearchProvider(ignore_closure=True))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["termination_reason"], "tool_call_limit")
        self.assertFalse(result["cancel_requested"])
        actual_searches = [
            item for item in result["events"]
            if item["type"] == "tool_call" and item["name"] == "search_documents"
        ]
        self.assertEqual(len(actual_searches), 7)
        denied = [
            item for item in result["events"]
            if item["type"] == "policy_decision" and item["decision"] == "denied"
        ]
        self.assertEqual(len(denied), 0)


if __name__ == "__main__":
    unittest.main()
