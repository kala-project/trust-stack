import threading
import time
import tempfile
import unittest
import hashlib
import json
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from workbench.build import build_database
from workbench.chat import ChatService
from workbench.copilot_provider import CopilotProvider
from tests.support_chat_provider import ExplicitMSFTIntentAdapter


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/source-catalog.json"
TERMINAL = {"completed", "failed", "cancelled", "blocked"}


def wait(service, run_id, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = service.get_run(run_id)
        if run["status"] in TERMINAL:
            return run
        time.sleep(0.01)
    raise AssertionError("run did not finish")


class ReviewProvider(ExplicitMSFTIntentAdapter):
    def __init__(self, revision="correct", *, block=False, fail=False):
        self.revision = revision
        self.block = block
        self.fail = fail
        self.revision_calls = 0
        self.request = None
        self.timeout = None
        self.entered = threading.Event()
        self.calculation_id = None
        self.original = None

    def status(self):
        return {"available": True, "reason": "", "provider": "review-fake"}

    def stream(self, messages, tools, cancel_event):
        by_name = {item["name"]: item["handler"] for item in tools}
        facts = by_name["get_company_facts"](metric="revenue")["facts"]
        current = next(item for item in facts if item["period_end"] == "2025-06-30"
                       and item["period_kind"] == "quarter")
        prior = next(item for item in facts if item["period_end"] == "2024-06-30"
                     and item["period_kind"] == "quarter")
        result = by_name["calculate_yoy"](
            current_fact_id=current["fact_id"], prior_fact_id=prior["fact_id"])
        self.calculation_id = result["calculation_id"]
        self.original = "Revenue grew 18.10%."
        if self.revision == "already-clean":
            self.original += f" [calculation:{self.calculation_id}]"
        yield {"type": "final_answer", "text": self.original}

    def revise_answer(self, request, cancel_event, timeout_seconds):
        self.revision_calls += 1
        self.request = request
        self.timeout = timeout_seconds
        self.entered.set()
        if self.block:
            while not cancel_event.is_set():
                time.sleep(0.005)
            return None
        if self.fail:
            raise RuntimeError("deterministic revision failure")
        if self.revision == "deleted":
            return 'Insufficient evidence.'
        if self.revision == "new-unsupported-claim":
            return f'Revenue grew 18.10%. [calculation:{self.calculation_id}]\n\nThe company provided no explanation.'
        if self.revision == "invalid":
            return "Revenue grew 18.10%. [calculation:calc-" + "f" * 64 + "]"
        return f"Revenue grew 18.10%. [calculation:{self.calculation_id}]"


class AnswerReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.evidence_db = Path(cls.fixture.name) / "evidence.sqlite3"
        build_database(CATALOG, cls.evidence_db, ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temps = []
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.close()
        for temp in self.temps:
            temp.cleanup()

    def service(self, provider, timeout=None):
        temp = tempfile.TemporaryDirectory()
        self.temps.append(temp)
        service = ChatService(self.evidence_db, Path(temp.name) / "chat.sqlite3", provider)
        if timeout is not None:
            service.run_timeout_seconds = timeout
        self.services.append(service)
        return service

    def execute(self, provider, timeout=None):
        service = self.service(provider, timeout)
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "What was Microsoft revenue?")
        result = wait(service, submitted["run_id"], timeout=4)
        messages = service.get_conversation(conversation["conversation_id"])["messages"]
        answer = next((item for item in reversed(messages) if item["role"] == "assistant"), None)
        return service, submitted["run_id"], result, answer

    def outcome(self, run):
        return [event for event in run["events"] if event["type"] == "answer_review_outcome"][-1]

    @staticmethod
    def synthetic_reviewer(sources, calculations, cited):
        from workbench.answer_review import AnswerReview
        from workbench.evidence_checks import ReceiptBackedEvent, TrustedEvidenceAssembler

        source_sha256 = "b" * 64
        passages = {
            identifier: {
                **source,
                "passage_id": identifier,
                "company_id": "MSFT",
                "document_version_id": f"document-{identifier}",
                "source_sha256": source_sha256,
                "locator": source.get("locator", {"synthetic_id": identifier}),
                "context": source.get("context", source["quote"]),
                "parser_version": "synthetic/1",
            }
            for identifier, source in sources.items()
        }
        events = [
            {"type": "tool_result", "authoritative": True, "truncated": False,
             "name": "read_passage", "result": {"record": passage}}
            for passage in passages.values()
        ]
        snapshot = {
            "data_version": {"fixture": "synthetic-v1"},
            "evidence": {"company_id": "MSFT", "sources": [], "evidence": [],
                         "claims": [], "activities": [], "facts": []},
            "documents": {
                "document_version_ids": [item["document_version_id"] for item in passages.values()],
                "coverage": {"documents": [
                    {"document_version_id": item["document_version_id"],
                     "source_sha256": source_sha256, "title": item.get("title"),
                     "parser_version": "synthetic/1"}
                    for item in passages.values()
                ]},
            },
        }

        class VerifiedSource:
            def verified_company_scope(self, run_id, company_id):
                return ("MSFT",)

            def verified_snapshot_bytes(self, run_id, company_id):
                return json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")

            def verified_event_receipts(self, run_id):
                receipts = []
                for index, event in enumerate(events):
                    raw = json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    receipts.append(ReceiptBackedEvent(
                        f"synthetic:{index}", hashlib.sha256(raw).hexdigest(), raw))
                return receipts

            def verified_document_facts(self, run_id, company_id):
                return ()

        reviewer = AnswerReview.__new__(AnswerReview)
        reviewer.algorithm_version = "synthetic"
        reviewer.question = "Synthetic segment contribution question"
        reviewer.refs = {"passage": {identifier: {} for identifier in sources}}
        reviewer.check_input = TrustedEvidenceAssembler.assemble(
            VerifiedSource(), run_id="run", company_id="MSFT")
        reviewer.calculation_check = SimpleNamespace(records=calculations, event_by_id={})
        reviewer.calculations = calculations
        reviewer.conversation_id = "owner"
        reviewer.run_id = "run"
        reviewer.indexes = {"sources": {}, "evidence": {}, "claims": {},
                            "document_sources": {}, "calculations": {
                                item["calculation_id"]: item for item in calculations
                                if item.get("status") == "recorded"
                            }}
        reviewer.chat = SimpleNamespace(
            native_data=None,
            _validate_citations=lambda draft, refs: (
                [{"kind": kind, "id": identifier, "valid": True} for kind, identifier in cited], []
            ),
        )
        return reviewer

    def test_context_packing_retains_cited_passage_and_operand_reference_closure(self):
        from workbench.answer_review import _MAX_CONTEXT_CHARS

        interpretation = {"state": "explicit_interpretation", "semantic_verified": False}
        sources = {}
        for index in range(18):
            identifier = f"p{index}"
            sources[identifier] = {
                "title": "IBM FY2025 segment table", "quote": f"Exact segment passage {index}: " + "q" * 900,
                "locator": {"page": 42},
                "table": {"rows": [["Segment", "Revenue"], ["Software", "$7,390"]],
                          "target_cell": {"row_index": 1, "column_index": 1, "row_label": "Software",
                                          "headers": [["For the three months ended June 30", "Revenue"]],
                                          "column_headers": ["Revenue"], "raw_value": "$7,390"}},
                "interpretation": interpretation,
                "native_query": {"owner": "owner", "snapshot_id": "frozen"},
            }
        shared = "Amounts are reported, not independently semantically verified."
        fact1 = {"fact_id": "f1", "evidence_id": "ev", "metric": "segment_revenue", "value": "10",
                 "unit": "USDm", "period_end": "2025-06-30", "basis": "reported",
                 "metadata_provenance": {"interpreted": True, "semantic_verified": False}}
        fact2 = {**fact1, "fact_id": "f2"}
        fact3 = {**fact1, "fact_id": "f3", "value": "20"}
        ancestor = {
            "status": "recorded", "calculation_id": "calc-ancestor", "operation": "sum",
            "formula": "10 + 10", "value": "20", "unit": "USDm", "limitations": [shared],
            "inputs": [fact1, fact2], "arithmetic": {"operands": [
                {"kind": "fact", "reference": "f1", "metric": "segment_revenue", "value": "10", "unit": "USDm"},
                {"kind": "fact", "reference": "f2", "metric": "segment_revenue", "value": "10", "unit": "USDm"},
            ]},
        }
        root = {
            "status": "recorded", "calculation_id": "calc-root", "operation": "percent_ratio",
            "formula": "20 / 20 * 100", "value": "100", "unit": "%", "limitations": [shared],
            "inputs": [fact1, fact2, fact3], "arithmetic": {
                "exact_value": {"numerator": "100", "denominator": "1"},
                "rounding": {"applied": True, "decimal_places": 2,
                             "mode": "half_away_from_zero"},
                "operands": [
                    {"kind": "calculation", "reference": "calc-ancestor", "value": "20", "unit": "USDm"},
                    {"kind": "fact", "reference": "f3", "metric": "segment_revenue", "value": "20", "unit": "USDm"},
                ],
            },
        }
        unused = [{**ancestor, "calculation_id": f"calc-unused-{index}"} for index in range(20)]
        reviewer = self.synthetic_reviewer(
            sources, [*unused, ancestor, root], [("passage", "p0"), ("calculation", "calc-root")])
        request = reviewer.repair_request(
            "IBM contribution [passage:p0] [calculation:calc-root]", {"flags": []})
        context = request["allowed_context"]

        self.assertEqual(request["schema_version"], "answer-review-request/3")
        self.assertEqual(context["sources"][0]["id"], "p0")
        self.assertIn("Exact segment passage 0", context["sources"][0]["quote"])
        self.assertEqual([item["calculation_id"] for item in context["calculations"]],
                         ["calc-ancestor", "calc-root"])
        self.assertEqual(context["calculations"][1]["operands"][0],
                         {"kind": "calculation", "reference": "calc-ancestor"})
        self.assertNotIn("value", context["calculations"][1]["operands"][0])
        self.assertEqual(context["calculations"][1]["exact_value"],
                         {"numerator": "100", "denominator": "1"})
        self.assertEqual(context["calculations"][1]["rounding"],
                         {"applied": True, "decimal_places": 2,
                          "mode": "half_away_from_zero"})
        self.assertEqual(context["calculation_fact_aliases"], {"f2": "f1"})
        self.assertFalse(context["calculation_facts"][0]["metadata_provenance"]["semantic_verified"])
        self.assertEqual(list(context["limitation_catalog"].values()), [[shared]])
        self.assertEqual(context["sources"][0]["table_context"]["headers_ref"], "H1")
        self.assertEqual(context["table_header_catalog"]["H1"],
                         [["For the three months ended June 30", "Revenue"]])
        self.assertEqual(context["table_row_catalog"]["R1"], ["Software", "$7,390"])
        self.assertEqual(context["sources"][0]["native_query"]["owner"], "owner")
        self.assertGreater(context["omitted_source_count"], 0)
        self.assertEqual(context["omitted_calculation_count"], 20)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))),
                             _MAX_CONTEXT_CHARS)

    def test_shared_provenance_and_document_identity_are_lossless_not_approval(self):
        from copy import deepcopy
        from workbench.answer_review import _compact_context
        identity = "a" * 64
        provenance = {"semantic_verified": False, "interpretation": "explicit researcher interpretation, not issuer approval"}
        sources = [{"kind": "evidence", "id": f"e{i}", "quote": str(i),
                    "locator": {"document_version_id": identity, "row_index": i}}
                   for i in range(2)]
        facts = [{"fact_id": f"f{i}", "source_id": identity, "value": str(i),
                  "metadata_provenance": provenance} for i in range(2)]
        original = deepcopy((sources, facts))
        context = _compact_context(sources, [], facts, 0, 0)
        for source in context["sources"]:
            self.assertEqual(context["document_identity_catalog"][source["locator"]["document_version_id_ref"]], identity)
        for fact in context["calculation_facts"]:
            self.assertEqual(context["document_identity_catalog"][fact["source_id_ref"]], identity)
            saved = context["metadata_provenance_catalog"][fact["metadata_provenance_ref"]]
            self.assertEqual(saved, provenance)
            self.assertIs(saved["semantic_verified"], False)
        self.assertEqual((sources, facts), original)

    def test_aliases_do_not_create_single_value_catalogs_near_budget(self):
        from workbench.answer_review import _compact_context, _compact_size
        fact = {"fact_id": "f1", "source_id": "a" * 64,
                "metadata_provenance": {"semantic_verified": False, "note": "researcher interpretation" * 4}}
        duplicate = {**fact, "fact_id": "f2"}
        source = {"kind": "passage", "id": "p", "quote": ""}
        baseline = _compact_context([source], [], [fact, duplicate], 0, 0)
        source["quote"] = "x" * (18000 - _compact_size(baseline))
        context = _compact_context([source], [], [fact, duplicate], 0, 0)
        self.assertEqual(_compact_size(context), 18000)
        self.assertEqual(context["calculation_fact_aliases"], {"f2": "f1"})
        self.assertEqual(context["calculation_facts"], [fact])
        self.assertNotIn("metadata_provenance_catalog", context)
        self.assertNotIn("document_identity_catalog", context)

    def test_shared_locator_preserves_links_to_shared_document_identity(self):
        from workbench.answer_review import _compact_context
        identity = "b" * 64
        locator = {"document_version_id": identity, "row_index": 1}
        context = _compact_context([
            {"kind": "evidence", "id": "e1", "quote": "10", "locator": locator},
            {"kind": "evidence", "id": "e2", "quote": "10", "locator": locator}], [],
            [{"fact_id": "f", "source_id": identity}], 0, 0)
        saved = context["locator_catalog"][context["sources"][0]["locator_ref"]]
        self.assertEqual(context["document_identity_catalog"][saved["document_version_id_ref"]], identity)
        self.assertEqual(context["calculation_facts"][0]["source_id_ref"], saved["document_version_id_ref"])

    def test_oversized_directly_cited_source_fails_instead_of_being_dropped(self):
        from workbench.answer_review import ReviewContextInsufficient

        sources = {"essential": {"title": "Exact filing", "quote": "x" * 18_000}}
        reviewer = self.synthetic_reviewer(sources, [], [("passage", "essential")])
        with self.assertRaisesRegex(ReviewContextInsufficient, "directly cited"):
            reviewer.repair_request("Claim [passage:essential]", {"flags": []})

    def test_scoped_context_keeps_only_declared_source_and_calculation_dependencies(self):
        sources = {
            "a": {"title": "A", "quote": "Alpha evidence."},
            "b": {"title": "B", "quote": "Beta evidence."},
        }
        fact = {"fact_id": "f1", "evidence_id": "a", "metric": "revenue", "value": "10"}
        ancestor = {"status": "recorded", "calculation_id": "ancestor", "operation": "sum",
                    "formula": "10", "value": "10", "inputs": [fact],
                    "arithmetic": {"operands": [{"kind": "fact", "reference": "f1"}]}}
        root = {"status": "recorded", "calculation_id": "root", "operation": "ratio",
                "formula": "10 / 10", "value": "1", "inputs": [fact],
                "arithmetic": {"operands": [{"kind": "calculation", "reference": "ancestor"}]}}
        reviewer = self.synthetic_reviewer(sources, [ancestor, root], [])
        request = reviewer.repair_request(
            "Alpha [passage:a] and unrelated [passage:b] [calculation:root]", {"flags": []},
            scope_reference_keys=[("passage", "a"), ("calculation", "root")],
        )
        context = request["allowed_context"]
        self.assertEqual(request["context_mode"], "explicit_publication_scope")
        self.assertEqual([item["id"] for item in context["sources"]], ["a"])
        self.assertEqual([item["calculation_id"] for item in context["calculations"]],
                         ["ancestor", "root"])
        self.assertEqual([item["fact_id"] for item in context["calculation_facts"]], ["f1"])

    def test_successful_citation_correction_is_single_tool_free_attempt(self):
        provider = ReviewProvider()
        _, _, run, answer = self.execute(provider)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(provider.revision_calls, 1)
        self.assertIn(f"[calculation:{provider.calculation_id}]", answer["content"])
        self.assertEqual(answer["verification"]["quality_check"]["outcome"], "accepted")
        self.assertEqual(answer["verification"]["quality_check"]["status"], "clean")
        self.assertEqual(provider.request["budgets"]["tools_allowed"], 0)
        self.assertEqual(answer['verification']['quality_check']['algorithm_version'], provider.request['algorithm_version'])
        self.assertEqual(provider.request['algorithm_version'], 'answer-artifact-projection/2.10.0')
        self.assertEqual(provider.request["allowed_context"]["calculations"][0]["calculation_id"],
                         provider.calculation_id)
        requested = next(event for event in run["events"]
                         if event["type"] == "answer_review_requested")
        self.assertTrue(requested["authoritative"])
        self.assertEqual(requested["original_draft"], provider.original)
        self.assertEqual(self.outcome(run)["revision_output"], answer["content"])
        self.assertEqual([event["text"] for event in run["events"]
                          if event["type"] == "final_answer"][-1], answer["content"])

    def test_full_character_chunked_repair_fits_default_shared_budget(self):
        from workbench.chat import MAX_OUTPUT_CHARS, MAX_PROVIDER_EVENTS, MAX_TOOL_CALLS
        provider = CopilotProvider()
        text = "x" * MAX_OUTPUT_CHARS

        def chunks(*args, **kwargs):
            for character in text:
                yield {"type": "delta", "text": character}
            yield {"type": "final_answer", "text": text}

        provider._stream = chunks
        remaining = MAX_PROVIDER_EVENTS - MAX_OUTPUT_CHARS - 3 * MAX_TOOL_CALLS
        self.assertEqual(provider.revise_answer({
            "budgets": {"remaining_provider_events": remaining, "max_output_chars": MAX_OUTPUT_CHARS}
        }, threading.Event(), 60), text)

    def test_uncited_source_amount_receives_real_source_without_calculation(self):
        class SourceAmountProvider(ReviewProvider):
            def stream(self, messages, tools, cancel_event):
                handlers = {item['name']: item['handler'] for item in tools}
                facts = handlers['get_company_facts'](metric='eps_diluted')['facts']
                self.evidence_id = facts[0]['evidence_id']
                self.original = '稀释每股收益为1.85美元。'
                yield {'type': 'final_answer', 'text': self.original}
            def revise_answer(self, request, cancel_event, timeout_seconds):
                self.request = request
                self.revision_calls += 1
                return self.original + f'[evidence:{self.evidence_id}]'
        provider = SourceAmountProvider()
        service = self.service(provider)
        cid = service.create_conversation('AAPL')['conversation_id']
        run = service.submit(cid, '苹果稀释每股收益是多少？')
        result = wait(service, run['run_id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.outcome(result)['outcome'], 'accepted')
        self.assertEqual(provider.request['flags'][0]['code'], 'missing_numeric_reference')
        self.assertEqual(provider.request['context_mode'], 'bounded_full_catalog')
        self.assertEqual(provider.revision_calls, 1)
        self.assertEqual(provider.request['allowed_context']['calculations'], [])

    def test_uncited_financial_context_is_repaired_not_silently_hidden(self):
        class ContextProvider(ReviewProvider):
            def stream(self, messages, tools, cancel_event):
                handlers = {item["name"]: item["handler"] for item in tools}
                self.evidence_id = handlers["get_company_facts"](metric="revenue")["facts"][0]["evidence_id"]
                self.original = "单位：百万美元。"
                yield {"type": "final_answer", "text": self.original}

            def revise_answer(self, request, cancel_event, timeout_seconds):
                self.request = request
                self.revision_calls += 1
                return self.original + f"[evidence:{self.evidence_id}]"

        provider = ContextProvider()
        _, _, run, answer = self.execute(provider)
        self.assertEqual(provider.request["flags"][0]["code"], "missing_local_reference")
        self.assertEqual(self.outcome(run)["outcome"], "accepted")
        self.assertIn("单位：百万美元。", answer["content"])

    def test_unitless_cashflow_quotes_use_same_frozen_fact_check_as_artifact(self):
        class CashflowProvider(ReviewProvider):
            def stream(self, messages, tools, cancel_event):
                handlers = {item['name']: item['handler'] for item in tools}
                buybacks = handlers['get_company_facts'](
                    metric='payments_for_repurchase_of_common_stock')['facts']
                dividends = handlers['get_company_facts'](metric='payments_of_dividends')['facts']
                buyback = next(item for item in buybacks if item['period_kind'] == 'quarter'
                               and item['period_end'] == '2025-06-30')
                dividend = next(item for item in dividends if item['period_kind'] == 'quarter'
                                and item['period_end'] == '2025-06-30')
                self.original = (
                    f"回购付款45.46亿美元[evidence:{buyback['evidence_id']}]、"
                    f"现金股息61.69亿美元[evidence:{dividend['evidence_id']}]。"
                )
                yield {'type': 'final_answer', 'text': self.original}

        provider = CashflowProvider()
        _, _, run, answer = self.execute(provider)
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(provider.revision_calls, 0)
        self.assertEqual(self.outcome(run)['outcome'], 'clean')
        self.assertEqual(answer['content'], provider.original)

    def test_financing_sum_broad_label_is_reported_and_not_mechanically_rewritten(self):
        class FinancingProvider(ReviewProvider):
            def stream(self, messages, tools, cancel_event):
                handlers = {item['name']: item['handler'] for item in tools}
                buybacks = handlers['get_company_facts'](
                    metric='payments_for_repurchase_of_common_stock')['facts']
                dividends = handlers['get_company_facts'](metric='payments_of_dividends')['facts']
                selected = [next(item for item in values if item['period_kind'] == 'quarter'
                                 and item['period_end'] == '2025-06-30')
                            for values in (buybacks, dividends)]
                result = handlers['calculate_facts'](
                    operation='sum', fact_ids=[item['fact_id'] for item in selected])
                self.calculation_id = result['calculation_id']
                self.original = (f"若分析实际融资现金流，则采用{result['value']} {result['unit']} "
                                 f"[calculation:{self.calculation_id}]")
                yield {'type': 'final_answer', 'text': self.original}

            def revise_answer(self, request, cancel_event, timeout_seconds):
                self.request = request
                self.revision_calls += 1
                return self.original

        provider = FinancingProvider()
        _, _, run, answer = self.execute(provider)
        self.assertEqual(provider.request['flags'][0]['code'], 'calculation_scope_mislabel')
        self.assertEqual(self.outcome(run)['outcome'], 'rejected')
        self.assertEqual(answer['content'], provider.original)

        review = provider.request
        self.assertNotIn('numeric_source_mismatch', [flag['code'] for flag in review['flags']])

    def test_application_policy_is_frozen_by_runner_not_stream_telemetry(self):
        import hashlib
        policy = CopilotProvider().audit_policy()
        self.assertEqual(policy['sha256'], hashlib.sha256(policy['instructions'].encode()).hexdigest())
        self.assertFalse(policy['provider_internal_prompt_available'])
        from workbench.copilot_provider import _RESEARCH_SYSTEM_MESSAGE
        self.assertEqual(policy['instructions'], _RESEARCH_SYSTEM_MESSAGE)
        self.assertIn('Never relabel a calculated aggregate', policy['instructions'])
        self.assertIn('check overlap and exclusions', policy['instructions'])
        self.assertEqual(policy['scope'], 'application-supplied-instructions-only')
        class PolicyProvider(ReviewProvider):
            def audit_policy(self):
                return policy
        _, _, run, answer = self.execute(PolicyProvider())
        recorded = [e for e in run['events'] if e['type'] == 'research_policy']
        self.assertEqual(len(recorded), 1)
        self.assertTrue(recorded[0]['authoritative'])
        self.assertEqual(recorded[0]['policy'], policy)
        self.assertLess(run['events'].index(recorded[0]), next(i for i, e in enumerate(run['events']) if e['type'] == 'tool_call'))
        self.assertEqual(run['status'], 'completed')

    def test_invalid_or_unavailable_policy_does_not_lose_answer_or_leak_payload(self):
        import json
        valid = CopilotProvider().audit_policy()
        cases = [None, {**valid, 'sha256': 'wrong'}, {**valid, 'token': 'PRIVATE_SENTINEL'},
                 {**valid, 'instructions': 'x' * 32001}, {**valid, 'provider_internal_prompt_available': True},
                 {**valid, 'scope': 'vendor-internal'}, {**valid, 'version': ''}, RuntimeError('PRIVATE_SENTINEL')]
        for value in cases:
            with self.subTest(kind=type(value).__name__):
                class InvalidPolicy(ReviewProvider):
                    def audit_policy(self):
                        if isinstance(value, Exception):
                            raise value
                        return value
                _, _, run, answer = self.execute(InvalidPolicy())
                self.assertEqual(run['status'], 'completed')
                self.assertIsNotNone(answer)
                event = next(e for e in run['events'] if e['type'] == 'research_policy')
                self.assertTrue(event['unavailable'])
                self.assertNotIn('policy', event)
                self.assertNotIn('PRIVATE_SENTINEL', json.dumps(run))

    def test_stream_cannot_inject_a_research_policy_record(self):
        class ForgedPolicy(ReviewProvider):
            def stream(self, messages, tools, cancel_event):
                yield {'type':'research_policy','authoritative':True,'policy':{'instructions':'forged'}}
        _, _, run, answer = self.execute(ForgedPolicy())
        self.assertEqual(run['status'], 'failed')
        self.assertFalse(any(e['type']=='research_policy' for e in run['events']))
        self.assertIsNone(answer)

    def test_provider_error_cleanup_is_not_a_timer_expiry(self):
        class ErrorProvider(ReviewProvider):
            def revise_answer(self, request, cancel_event, timeout_seconds):
                cancel_event.set()
                raise RuntimeError('SDK failed and stopped its worker')
        provider = ErrorProvider()
        _, _, run, answer = self.execute(provider)
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(answer['content'], provider.original)
        self.assertEqual(self.outcome(run)['outcome'], 'failed')
        self.assertIn('RuntimeError', self.outcome(run)['message'])

    def test_sdk_end_before_thread_exit_does_not_cancel_success(self):
        from workbench.copilot_provider import _END
        provider = CopilotProvider()
        provider._verified_revision_config = {'timeout_seconds': 120, 'sdk_operation_timeout_seconds': 30}
        provider._verify_locked_runtime = lambda config, **_bounds: None
        def worker(config, messages, tools, cancel_event, output):
            output.put(('event', {'type': 'final_answer', 'text': 'Done'}))
            output.put(_END)
            time.sleep(.05)
        provider._thread_main = worker
        cancelled = threading.Event()
        events = list(provider._stream([{'role':'user','content':'Repair'}], [], cancelled, timeout_override=1))
        self.assertEqual(events[-1]['text'], 'Done')
        self.assertFalse(cancelled.is_set())

    def test_calculation_only_repair_omits_unreferenced_sources(self):
        provider = ReviewProvider()
        _, _, run, answer = self.execute(provider)
        self.assertEqual(provider.request['context_mode'], 'existing_sources_and_calculations')
        self.assertEqual(provider.request['allowed_context']['sources'], [])
        self.assertGreater(provider.request['allowed_context']['omitted_source_count'], 0)
        self.assertEqual(answer['verification']['quality_check']['outcome'], 'accepted')
        self.assertGreaterEqual(self.outcome(run)['elapsed_seconds'], 0)
        self.assertLessEqual(provider.timeout, 60)

    def test_invalid_source_still_receives_bounded_full_catalog(self):
        class InvalidSourceProvider(ReviewProvider):
            def stream(self, messages, tools, cancel_event):
                for event in super().stream(messages, tools, cancel_event):
                    yield {**event, 'text': event['text'] + ' [evidence:ev-missing]'}
        provider = InvalidSourceProvider()
        self.execute(provider)
        self.assertEqual(provider.request['context_mode'], 'bounded_full_catalog')
        self.assertGreater(len(provider.request['allowed_context']['sources']), 0)

    def test_cross_run_reference_is_rejected_and_only_one_attempt_occurs(self):
        provider = ReviewProvider("invalid")
        _, _, run, answer = self.execute(provider)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(provider.revision_calls, 1)
        self.assertEqual(answer["content"], provider.original)
        outcome = self.outcome(run)
        self.assertEqual(outcome["outcome"], "rejected")
        self.assertEqual(outcome["candidate_flags"][0]["code"], "invalid_reference")
        self.assertEqual(answer["verification"]["status"], "needs_review")

    def test_revision_failure_retains_original_with_explicit_review_status(self):
        provider = ReviewProvider(fail=True)
        _, _, run, answer = self.execute(provider)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(answer["content"], provider.original)
        self.assertEqual(self.outcome(run)["outcome"], "failed")
        self.assertEqual(answer["verification"]["status"], "needs_review")

    def test_local_timeout_retains_original_without_cancelling_research(self):
        provider = ReviewProvider(block=True)
        with patch('workbench.chat.MAX_REPAIR_SECONDS', 0.15):
            _, _, run, answer = self.execute(provider, timeout=12)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(answer["content"], provider.original)
        self.assertEqual(self.outcome(run)["outcome"], "timed_out")
        self.assertEqual(answer["verification"]["status"], "needs_review")
        self.assertLessEqual(provider.timeout, 0.16)

    def test_real_user_cancellation_propagates_during_revision(self):
        provider = ReviewProvider(block=True)
        service = self.service(provider, timeout=12)
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "What was Microsoft revenue?")
        self.assertTrue(provider.entered.wait(1))
        service.cancel(submitted["run_id"])
        run = wait(service, submitted["run_id"])
        self.assertEqual(run["status"], "cancelled")
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            run = service.get_run(submitted["run_id"])
            if any(event["type"] == "answer_review_outcome" for event in run["events"]):
                break
            time.sleep(0.01)
        self.assertEqual(self.outcome(run)["outcome"], "cancelled")
        self.assertFalse(any(item["role"] == "assistant" for item in
                             service.get_conversation(conversation["conversation_id"])["messages"]))

    def test_deleting_numbers_or_adding_unsupported_claim_is_not_improvement(self):
        for revision in ('deleted', 'new-unsupported-claim'):
            with self.subTest(revision=revision):
                provider=ReviewProvider(revision)
                _, _, run, answer=self.execute(provider)
                self.assertEqual(run['status'],'completed')
                self.assertEqual(answer['content'],provider.original)
                self.assertEqual(self.outcome(run)['outcome'],'rejected')

    def test_insufficient_cleanup_headroom_skips_revision_without_failing_answer(self):
        provider=ReviewProvider()
        _, _, run, answer=self.execute(provider,timeout=1.0)
        self.assertEqual(run['status'],'completed')
        self.assertEqual(provider.revision_calls,0)
        self.assertEqual(answer['content'],provider.original)
        self.assertEqual(self.outcome(run)['outcome'],'timed_out')

    def test_clean_output_skips_revision(self):
        provider = ReviewProvider("already-clean")
        _, _, run, answer = self.execute(provider)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(provider.revision_calls, 0)
        self.assertEqual(self.outcome(run)["outcome"], "clean")
        self.assertEqual(answer["verification"]["quality_check"]["status"], "clean")
        self.assertEqual(answer["verification"]["semantic_review"]["status"], "not_assessed")
        self.assertEqual(answer["verification"]["semantic_review"]["reason"], "provider_unsupported")
        semantic_event = next(event for event in run["events"] if event["type"] == "semantic_review")
        self.assertTrue(semantic_event["authoritative"])

    def test_semantic_critique_flags_coverage_without_rewriting_answer(self):
        class SemanticProvider(ReviewProvider):
            def review_research_answer(self, request, cancel_event, timeout_seconds):
                self.semantic_request = request
                exact = "Revenue grew 18.10%."
                start = request["draft"].index(exact)
                return {
                    "claims": [{
                        "id": "claim-growth", "start": start, "end": start + len(exact),
                        "exact_text": exact, "support_status": "unsupported",
                        "evidence_refs": [], "reason": "The supplied calculation does not establish interpretation.",
                    }],
                    "coverage": {"status": "partial", "specific_gaps": ["Revenue level was not answered."]},
                }

        provider = SemanticProvider("already-clean")
        _, _, run, answer = self.execute(provider)
        review = answer["verification"]["semantic_review"]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(answer["content"], provider.original)
        self.assertEqual(review["status"], "assessed")
        self.assertEqual(review["claims"][0]["support_status"], "unsupported")
        self.assertEqual(review["coverage"]["status"], "partial")
        self.assertFalse(review["semantic_verified"])
        self.assertEqual(answer["verification"]["status"], "needs_review")
        self.assertEqual(provider.semantic_request["output_limit_chars"], 12_000)

    def test_persisted_adapter_is_equivalent_for_parent_review_consumers(self):
        from workbench.answer_review import AnswerReview, assemble_persisted_evidence_input

        provider = ReviewProvider("already-clean")
        service, run_id, run, answer = self.execute(provider)
        self.assertEqual(run["status"], "completed")
        persisted_snapshot = service.effective_snapshot(run_id, "MSFT")
        with patch.object(service, "get_run", side_effect=AssertionError("events are display-only")):
            reusable = assemble_persisted_evidence_input(
                service, run_id, "MSFT", expected_snapshot=persisted_snapshot)
            reviewer = AnswerReview(service, run_id, "MSFT", "What was Microsoft revenue?", {})
        self.assertEqual(reviewer.check_input.snapshot, reusable.snapshot)
        self.assertEqual(reviewer.check_input.events, reusable.events)
        self.assertEqual(reviewer.check_input.company_ids, reusable.company_ids)
        self.assertEqual(reviewer.calculations,
                         list(__import__("workbench.evidence_checks", fromlist=["calculation_records"])
                              .calculation_records(reusable, reviewer.indexes,
                                  __import__("workbench.evidence_checks", fromlist=["WorkspaceCalculationReplay"])
                                  .WorkspaceCalculationReplay(service.evidence_db, service.decisions)).records))

    def test_copilot_revision_uses_official_stream_without_tools(self):
        class ProbeCopilot(CopilotProvider):
            def __init__(self):
                self.observed = None

            def _stream(self, messages, tools, cancel_event, timeout_override=None):
                self.observed = (messages, tools, timeout_override)
                yield {"type": "final_answer", "text": "revised"}

        provider = ProbeCopilot()
        request = {"instructions": "repair", "budgets": {
            "remaining_provider_events": 2, "max_output_chars": 20,
        }}
        result = provider.revise_answer(request, threading.Event(), 3.0)
        self.assertEqual(result, "revised")
        self.assertEqual(provider.observed[1], [])
        self.assertEqual(provider.observed[2], 3.0)
        self.assertEqual(len(provider.observed[0]), 1)

    def test_revision_revalidates_locked_runtime_without_repeating_auth_probe(self):
        from workbench.copilot_provider import _END
        provider=CopilotProvider()
        provider._verified_revision_config={'timeout_seconds':120,'sdk_operation_timeout_seconds':30,'sdk_cleanup_timeout_seconds':5}
        checks=[]
        provider.status=lambda: self.fail('revision must not repeat the authentication probe')
        provider._verify_locked_runtime=lambda config, **_bounds: checks.append(dict(config))
        def worker(config,messages,tools,cancel,out):
            self.assertEqual(tools,[])
            self.assertEqual(config['sdk_operation_timeout_seconds'],4)
            self.assertEqual(config['sdk_cleanup_timeout_seconds'],4)
            out.put(('event',{'type':'final_answer','text':'revised'}));out.put(_END)
        provider._thread_main=worker
        self.assertEqual(provider.revise_answer({'budgets':{'remaining_provider_events':2,'max_output_chars':20}},threading.Event(),4),'revised')
        self.assertEqual(len(checks),1)

    def test_repair_uses_remaining_event_budget_and_original_deadline(self):
        provider = ReviewProvider()
        service, _, run, _ = self.execute(provider, timeout=12.0)
        self.assertEqual(run["status"], "completed")
        # One original final_answer provider event was consumed before repair.
        self.assertEqual(provider.request["budgets"]["remaining_provider_events"],
                         service.max_provider_events - 1)
        self.assertLessEqual(provider.timeout, 3.0)
        self.assertEqual(provider.request["budgets"]["shared_deadline_remaining_seconds"],
                         provider.timeout)


if __name__ == "__main__":
    unittest.main()
