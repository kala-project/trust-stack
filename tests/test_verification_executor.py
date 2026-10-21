"""Controlled-provider tests for the durable verifier; never real model acceptance."""
from workbench.candidate_contracts import AuthorInputs
from workbench.execution_scope import ExecutionScope
from workbench.publication_contracts import CanonicalPublication
from workbench.research_records import freeze_object
from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

from workbench.answer_artifacts import AnswerArtifactService
from workbench.answer_review import validated_document_facts_from_receipts
from workbench.build import build_database
from workbench.chat import ChatError, ChatService, _now
from workbench.documents import DocumentService
from workbench.report_publications import prepare
from workbench.research_tools import ResearchTools
from workbench.tool_errors import ToolError
from workbench.verification_executor import VerificationApplication, VerificationExecutionConfig
from workbench.verification_store import (
    ArtifactRef, CandidateSubmission, VerificationBudgetExceeded,
    VerificationIntegrityError,
)
from workbench.worker import DurableWorker
from tests.test_chat import FakeProvider


ROOT = Path(__file__).resolve().parents[1]


def report():
    return prepare(title="Candidate", blocks=[{
        "classification": "evidence_gap", "parts": [{"kind": "text", "text": "Limited."}],
        "support": [], "reasoning": "", "limitations": ["Current material is limited."],
    }], refs={}, facts={}, calculations={}, max_chars=10000)


class DummyResearch:
    def definitions(self):
        return [{
            "name": "search_evidence", "description": "controlled read",
            "parameters": {"type": "object"},
            "handler": lambda **_arguments: {"results": [], "searched_scope": "frozen"},
        }]


class ControlledVerifier:
    """Explicit test double that drives runner handlers, not an AI surrogate."""
    def __init__(self, *, skip_read=False):
        self.skip_read = skip_read
        self.messages = None

    def stream_verification(self, messages, tools, cancel_event):
        self.messages = messages
        self.tools = tools
        packet = json.loads(messages[0]["content"])
        handlers = {tool["name"]: tool["handler"] for tool in tools}
        if not self.skip_read:
            handlers["search_evidence"](query="candidate limitation")
        for node_id in packet["content_nodes"]:
            for check_kind in packet["verification_checks"][node_id]["required"]:
                handlers["record_verification_finding"](
                    version_id=packet["version_id"], node_id=node_id, check_kind=check_kind,
                    outcome="insufficient_material_disclosed", explanation="Searched frozen scope.")
        handlers["finalize_verification"](
            version_id=packet["version_id"], outcome="verified_with_limitations",
            limitations=["Authorized frozen material does not answer the core question."],
            semantic_review={"original_question_coverage": "Controlled assessment of the original question."},
            verification={"method": "controlled-provider-test", "semantic_truth_guaranteed": False})
        yield {"type": "usage", "tokens": 1}


class ControlledAuthorVerifier(ControlledVerifier):
    """Controlled structured author plus clean verifier for queue integration only."""
    requires_structured_reports = True

    def status(self):
        return {"available": True, "reason": "", "provider": "controlled-test"}

    def resolve_research_intent(self, prompt, timeout_seconds):
        return FakeProvider().resolve_research_intent(prompt, timeout_seconds)

    def stream(self, messages, tools, cancel_event):
        publish = next(tool for tool in tools if tool["name"] == "publish_research_report")
        publish["handler"](title="Candidate", blocks=[{
            "classification": "evidence_gap",
            "parts": [{"kind": "text", "text": "Limited."}],
            "support": [], "reasoning": "",
            "limitations": ["Current material is limited."],
        }])
        yield {"type": "usage", "tokens": 1}


class VerificationExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.evidence = root / "evidence.sqlite3"
        self.chat = root / "chat.sqlite3"
        build_database(db_path=self.evidence)

    def candidate_service(self, provider, *, publication=None, input_snapshot=None,
                          research_factory=None, evidence_refs=(), calculation_refs=(),
                          author_inputs=None, author_receipts=(), verification_config=None,
                          document_service=None):
        service = ChatService(
            self.evidence, self.chat, FakeProvider(), job_lease_seconds=30,
            job_retry_delays=(0, 0), document_service=document_service)
        self.addCleanup(service.close)
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "ordinary question", request_id="author")
        author = service.jobs.claim("author")
        with closing(service._connect()) as connection, connection:
            connection.execute("UPDATE runs SET status='running',started_at=? WHERE run_id=?",
                               (_now(), author.run_id))
        frozen = deepcopy(input_snapshot or {
            "authoritative": True, "truncated": False,
            "data_version": {}, "evidence": {"company_id": "MSFT"}})
        frozen.setdefault("authoritative", True)
        frozen.setdefault("truncated", False)
        exact_inputs = {"claim": {}, "evidence": {}, "passage": {}, "calculation": {}, "query": {},
                        **({} if author_inputs is None else author_inputs)}
        with closing(service._connect()) as connection, connection:
            service.records.append_snapshot(
                connection, run_id=author.run_id, snapshot=frozen, created_at=_now())
            if evidence_refs or calculation_refs:
                service.records.append_receipt(
                    connection, run_id=author.run_id, call_id="candidate-fixture-call",
                    tool_name="candidate_fixture", arguments={}, result={"catalogs": exact_inputs},
                    created_at=_now())
            for index, receipt in enumerate(author_receipts):
                if receipt["tool_name"] == "extract_document_table_fact":
                    service.inputs.record(connection, author.run_id, receipt["result"])
                service.records.append_receipt(
                    connection, run_id=author.run_id, call_id=f"author-fixture-call-{index}",
                    tool_name=receipt["tool_name"], arguments=receipt.get("arguments", {}),
                    result=receipt["result"], created_at=_now())
            service._insert_event(
                connection, submitted["run_id"], "data_snapshot", frozen, claim=author)
        saved = service.verification_store.save_candidate_and_enqueue(author, CandidateSubmission(
            question="ordinary question", scope=ExecutionScope.create(["MSFT"]),
            input_snapshot=freeze_object(frozen, "snapshot"), base_message_id=None,
            publication=CanonicalPublication.decode(publication or report()), evidence_refs=evidence_refs,
            calculation_refs=calculation_refs, request_identity="verify",
            author_inputs=AuthorInputs.decode(exact_inputs)))
        service.jobs.release_runtime_slot(author)
        service.provider = provider
        def document_fact_factory(packet, receipts):
            company_id = packet["input_snapshot"].get("evidence", {}).get("company_id")
            return validated_document_facts_from_receipts(
                service, packet["author_run_id"], company_id, receipts,
                expected_snapshot=packet["input_snapshot"])
        service.verification_executor = VerificationApplication(
            service.verification_store, provider, research_factory or (lambda _packet: DummyResearch()),
            document_fact_factory=document_fact_factory if document_service is not None else None,
            research_result_adapter=(
                (lambda research, _name, result:
                 service._authoritative_document_fact_result(research, result))
                if document_service is not None else None),
            config=verification_config)
        return service, conversation, submitted, saved

    def document_research_fixture(self):
        root = Path(self.temp.name)
        documents = DocumentService(root / "documents")
        source = root / "annual.html"
        source.write_text(
            "<!doctype html><html><body><p>Annual filing confirms cloud revenue.</p><table>"
            "<tr><th>USD millions</th><th>2025</th></tr>"
            "<tr><td>Revenue</td><td>$240</td></tr>"
            "<tr><td>Capital expenditures</td><td>$120</td></tr>"
            "</table></body></html>", encoding="utf-8")
        version = documents.register_file(
            source, company_id="MSFT", source_key="annual", title="Annual filing",
            url="https://example.test/annual", published_at="2025-07-01",
            rights_status="public")
        research = ResearchTools(self.evidence, "MSFT", documents)
        snapshot = {"authoritative": True, "truncated": False,
                    "data_version": deepcopy(research.data_version), "evidence": {
            "company_id": "MSFT", **{key: deepcopy(research._company[key]) for key in (
                "sources", "evidence", "claims", "activities", "facts", "evidence_chains")},
        }, "documents": {"coverage": deepcopy(research.document_coverage),
                          "document_version_ids": list(research.document_version_ids)}}
        passage = research.search_documents(query="cloud revenue", limit=1)["results"][0]
        return documents, research, snapshot, version, passage

    def test_candidate_has_no_formal_then_verify_reads_and_publishes_limitations(self):
        provider = ControlledVerifier()
        service, conversation, submitted, saved = self.candidate_service(provider)
        with closing(service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (saved["verify_job_id"],)).fetchone()[0], "queued")
        worker = DurableWorker(service, worker_id="controlled-verify", heartbeat_seconds=.1)
        self.assertTrue(worker.run_once())
        candidate = service.verification_store.get_candidate(
            conversation["conversation_id"], saved["candidate_id"])
        self.assertEqual(candidate["status"], "verified_with_limitations")
        self.assertIsNotNone(candidate["formal_message_id"])
        packet = json.loads(provider.messages[0]["content"])
        self.assertNotIn("history", packet)
        self.assertNotIn("reasoning_trace", provider.messages[0]["content"])
        with closing(service._connect()) as connection:
            self.assertGreater(connection.execute(
                "SELECT count(*) FROM verification_tool_receipts").fetchone()[0], 0)
            findings = connection.execute(
                "SELECT count(*) FROM verification_checkpoints").fetchone()[0]
            self.assertGreater(findings, 0)
            tool_calls = connection.execute(
                "SELECT tool_calls FROM verification_usage WHERE job_id=?",
                (saved["verify_job_id"],)).fetchone()[0]
            self.assertEqual(tool_calls, 1 + findings + 1)  # read + findings + finalize

    def test_acquired_source_is_in_final_candidate_snapshot_and_verifier_reader(self):
        from tests.test_research_acquisition import ControlledAcquisition, AcquisitionReportProvider
        from workbench.answer_review import PersistedEvidenceSource
        from workbench.evidence_checks import EvidenceCheckError
        outer = self

        class Provider(AcquisitionReportProvider):
            def stream_verification(self, messages, tools, cancel_event):
                packet = json.loads(messages[0]["content"])
                handlers = {tool["name"]: tool["handler"] for tool in tools}
                reference = next(ref for ref in packet["evidence_refs"] if ref["kind"] == "passage")
                read = handlers["get_candidate_reference"](
                    kind="passage", identifier=reference["id"])
                outer.assertEqual(read["state"], "available")
                for node in packet["content_nodes"]:
                    for check_kind in packet["verification_checks"][node]["required"]:
                        handlers["record_verification_finding"](
                            version_id=packet["version_id"], node_id=node,
                            check_kind=check_kind, outcome="supported",
                            explanation="Controlled read of the newly archived original.")
                handlers["finalize_verification"](
                    version_id=packet["version_id"], outcome="verified", limitations=[],
                    semantic_review={"original_question_coverage": "Controlled acquisition scope."},
                    verification={"method": "controlled-test"})
                yield {"type": "usage", "tokens": 1}

        root = Path(self.temp.name)
        documents = DocumentService(root / "documents")
        provider = Provider()
        service = ChatService(
            self.evidence, self.chat, provider, document_service=documents,
            source_acquisition=ControlledAcquisition(documents, root))
        self.addCleanup(service.close)
        submitted = service.create_and_submit("MSFT", "Acquire the requested annual original.")
        worker = DurableWorker(service, worker_id="acquisition", heartbeat_seconds=.1)
        self.assertTrue(worker.run_once())
        candidate = service.list_verification_candidates(submitted["conversation_id"])[0]
        detail = service.get_verification_candidate(submitted["conversation_id"], candidate["candidate_id"])
        effective = service.effective_snapshot(submitted["run_id"], "MSFT")
        self.assertEqual(detail["input_snapshot"], effective)
        run = service.get_run(submitted["run_id"])
        initial_event = next(event for event in run["events"] if event["type"] == "data_snapshot")
        self.assertNotIn(provider.available["document"]["document_version_id"],
                         initial_event["documents"]["document_version_ids"])
        self.assertIn(provider.available["document"]["document_version_id"],
                      effective["documents"]["document_version_ids"])
        stale = deepcopy(effective)
        stale["documents"]["document_version_ids"] = initial_event["documents"]["document_version_ids"]
        with self.assertRaisesRegex(EvidenceCheckError, "expected snapshot"):
            PersistedEvidenceSource(service, submitted["run_id"], "MSFT", expected_snapshot=stale)
        self.assertTrue(worker.run_once())
        final = service.get_verification_candidate(submitted["conversation_id"], candidate["candidate_id"])
        self.assertIsNotNone(final["formal_message_id"], final)
        published = service.published_verification_evidence(
            submitted["conversation_id"], final["formal_message_id"])
        self.assertEqual(published["input_snapshot"], effective)
        artifact = AnswerArtifactService(service).get(
            submitted["conversation_id"], final["formal_message_id"])
        self.assertEqual(artifact["message_id"], final["formal_message_id"])

    def test_ordinary_submit_runs_author_candidate_then_verify_formal(self):
        provider = ControlledAuthorVerifier()
        service = ChatService(
            self.evidence, self.chat, provider, job_lease_seconds=30,
            job_retry_delays=(0, 0))
        self.addCleanup(service.close)
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(
            conversation["conversation_id"], "ordinary request", request_id="ordinary")
        worker = DurableWorker(service, worker_id="two-stage", heartbeat_seconds=.1)
        self.assertTrue(worker.run_once())
        with closing(service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM verification_candidates").fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM jobs WHERE job_type='verify' AND status='queued'").fetchone()[0], 1)
        self.assertTrue(worker.run_once())
        with closing(service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0], 1)
            record = connection.execute(
                "SELECT formal_message_id FROM verification_records WHERE formal_message_id IS NOT NULL").fetchone()
            self.assertIsNotNone(record)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM report_reader_versions").fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM report_artifact_snapshots").fetchone()[0], 1)
            before = tuple(connection.execute(
                "SELECT (SELECT count(*) FROM report_reader_versions),"
                "(SELECT count(*) FROM report_artifact_snapshots),"
                "(SELECT payload_sha256 FROM report_reader_versions),"
                "(SELECT artifact_sha256 FROM report_artifact_snapshots)").fetchone())
        artifact = AnswerArtifactService(service).get(
            conversation["conversation_id"], record["formal_message_id"])
        again = AnswerArtifactService(service).get(
            conversation["conversation_id"], record["formal_message_id"])
        self.assertEqual(artifact, again)
        with closing(service._connect()) as connection:
            after = tuple(connection.execute(
                "SELECT (SELECT count(*) FROM report_reader_versions),"
                "(SELECT count(*) FROM report_artifact_snapshots),"
                "(SELECT payload_sha256 FROM report_reader_versions),"
                "(SELECT artifact_sha256 FROM report_artifact_snapshots)").fetchone())
        self.assertEqual(after, before)
        self.assertEqual(artifact["message_id"], record["formal_message_id"])
        projected = artifact["required_verifier_findings"]
        with closing(service._connect()) as connection:
            reader = json.loads(connection.execute(
                "SELECT payload_json FROM report_reader_versions WHERE message_id=?",
                (record["formal_message_id"],)).fetchone()[0])
        self.assertEqual({item["node_id"] for item in reader["required_verifier_findings"]},
                         {item["node_id"] for item in projected})
        self.assertEqual(reader["report"]["status"], "needs_review")
        self.assertEqual({item["node_id"] for item in projected},
                         {"report-title", "report-block-1"})
        self.assertEqual({item["outcome"] for item in projected},
                         {"insufficient_material_disclosed"})
        self.assertTrue(all(item["receipts"] for item in projected))
        self.assertTrue(all(set(item) == {
            "node_id", "check_kind", "outcome", "explanation", "receipts"}
            for item in projected))
        self.assertTrue(all(set(receipt) == {"receipt_id", "sha256"}
                            for item in projected for receipt in item["receipts"]))
        self.assertFalse(artifact["verification"]["semantic_verified"])
        qualitative = next(item for item in artifact["findings"]
                           if item["id"] == "report-block-1")
        self.assertEqual(qualitative["classification"], "evidence_gap")
        self.assertEqual(qualitative["semantic_status"], "not_assessed")
        whole = next(item for item in artifact["anchors"]
                     if item["finding_id"] == "report-block-1"
                     and item["text"] == qualitative["text"])
        self.assertEqual(whole["support_status"], "unresolved")
        self.assertEqual(whole["required_verifier_findings"][0]["outcome"],
                         "insufficient_material_disclosed")
        contract = service.published_verification_evidence(
            conversation["conversation_id"], record["formal_message_id"])
        self.assertEqual(contract["formal_message_id"], record["formal_message_id"])
        self.assertGreater(len(contract["receipts"]), 0)

    def test_model_facing_verification_tools_have_complete_required_schemas(self):
        provider = ControlledVerifier()
        service, conversation, submitted, saved = self.candidate_service(provider)
        worker = DurableWorker(service, worker_id="schema-check", heartbeat_seconds=.1)
        self.assertTrue(worker.run_once())
        schemas = {tool["name"]: tool["parameters"] for tool in provider.tools}
        self.assertEqual(set(schemas["preview_verification_revision"]["required"]),
                         {"version_id", "mode", "revision_summary"})
        self.assertEqual(set(schemas["revise_verification_candidate"]["required"]),
                         {"version_id", "preview_id"})
        self.assertEqual(set(schemas["record_verification_finding"]["required"]),
                         {"version_id", "node_id", "check_kind", "outcome",
                          "explanation"})
        receipt_description = schemas["record_verification_finding"]["properties"][
            "receipt_ids"]["description"]
        self.assertIn("Normally omit", receipt_description)
        self.assertIn("strictly validated", receipt_description)
        self.assertEqual(set(schemas["finalize_verification"]["required"]),
                         {"version_id", "outcome", "limitations",
                          "semantic_review", "verification"})
        self.assertNotIn("node_hash", schemas["record_verification_finding"]["properties"])
        self.assertNotIn("dependency_hash", schemas["record_verification_finding"]["properties"])
        for name in ("preview_verification_revision", "revise_verification_candidate",
                     "record_verification_finding", "finalize_verification"):
            self.assertFalse(schemas[name].get("additionalProperties", True))

    def test_large_sealed_catalog_entry_can_receive_a_current_verifier_receipt(self):
        def artifact(kind, identifier, value):
            raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
            return ArtifactRef(kind, identifier, hashlib.sha256(raw.encode()).hexdigest())

        passages = {
            f"passage-{index}": {
                "passage_id": f"passage-{index}", "quote": f"Generic source text {index}",
                "source_sha256": "a" * 64, "locator": {"page": index + 1},
            }
            for index in range(151)
        }
        calculations = {
            f"calculation-{index}": {
                "calculation_id": f"calculation-{index}", "operation": "difference",
                "formula": "2 - 1", "value": "1", "unit": "count", "inputs": [],
            }
            for index in range(21)
        }
        catalogs = {"claim": {}, "evidence": {}, "passage": passages,
                    "query": {}, "calculation": calculations}
        evidence_refs = tuple(artifact("passage", key, value)
                              for key, value in passages.items())
        calculation_refs = tuple(artifact("calculation", key, value)
                                 for key, value in calculations.items())
        candidate = prepare(title="Catalog-shaped candidate", blocks=[{
            "classification": "source_statement",
            "parts": [{"kind": "text", "text": "Bound source."}],
            "support": [{"kind": "passage", "id": "passage-150"}],
            "reasoning": "", "limitations": [],
        }], refs=catalogs, facts={}, calculations=calculations, max_chars=10000)

        class CatalogVerifier:
            receipt = None
            terminal_rejected = False

            def stream_verification(inner_self, messages, tools, cancel_event):
                packet = json.loads(messages[0]["content"])
                self.assertNotIn("author_inputs", packet)
                handlers = {tool["name"]: tool["handler"] for tool in tools}
                result = handlers["get_candidate_reference"](
                    kind="passage", identifier="passage-150")
                self.assertEqual(result["state"], "incomplete")
                self.assertNotIn("record", result)
                self.assertNotIn("passage-149", json.dumps(result))
                inner_self.receipt = result["_verification_receipt"]
                try:
                    handlers["record_verification_finding"](
                        version_id=packet["version_id"], node_id=packet["content_nodes"][-1],
                        check_kind="semantic_support", outcome="supported",
                        explanation="An incomplete descriptor must not support a terminal finding.")
                except VerificationIntegrityError:
                    inner_self.terminal_rejected = True
                for node_id in packet["content_nodes"]:
                    handlers["record_verification_finding"](
                        version_id=packet["version_id"], node_id=node_id,
                        check_kind="semantic_support", outcome="unresolved",
                        explanation="The descriptor has no persisted complete record.",
                        receipt_ids=[inner_self.receipt["identifier"]])
                handlers["finalize_verification"](
                    version_id=packet["version_id"], outcome="unresolved",
                    limitations=["Semantic review remains unresolved."],
                    semantic_review={"original_question_coverage": "Controlled assessment of the original question."},
                    verification={"method": "controlled-catalog-contract-test"})
                yield {"type": "usage", "tokens": 1}

        provider = CatalogVerifier()
        snapshot = {"authoritative": True, "truncated": False, "data_version": {},
                    "evidence": {"company_id": "MSFT"}, "catalog_padding": "x" * 165000}
        service, _, _, saved = self.candidate_service(
            provider, publication=candidate, input_snapshot=snapshot,
            evidence_refs=evidence_refs, calculation_refs=calculation_refs,
            author_inputs=catalogs)
        worker = DurableWorker(service, worker_id="large-catalog", heartbeat_seconds=.1)
        self.assertTrue(worker.run_once())
        self.assertTrue(provider.terminal_rejected)
        with closing(service._connect()) as connection:
            receipt = connection.execute(
                "SELECT tool_name,result_json FROM verification_tool_receipts WHERE job_id=?",
                (saved["verify_job_id"],)).fetchone()
            self.assertEqual(receipt["tool_name"], "get_candidate_reference")
            result = json.loads(receipt["result_json"])
            self.assertEqual(result["state"], "incomplete")
            self.assertIn("candidate_reference_sha256", result)
            self.assertNotIn("record", result)
            self.assertNotIn("passage-149", receipt["result_json"])

    def test_candidate_lookup_reads_full_replayed_calculation_not_id_descriptor(self):
        research = ResearchTools(self.evidence, "MSFT")
        fact_id = next(iter(research._facts))
        calculation = research.calculate_facts(operation="sum", fact_ids=[fact_id, fact_id])
        identifier = calculation["calculation_id"]
        snapshot = {"authoritative": True, "truncated": False,
                    "data_version": deepcopy(research.data_version), "evidence": {
            "company_id": "MSFT", **{key: deepcopy(research._company[key]) for key in (
                "sources", "evidence", "claims", "activities", "facts", "evidence_chains")},
        }, "documents": {"coverage": deepcopy(research.document_coverage),
                          "document_version_ids": []}}
        descriptor = {"calculation_id": identifier, "company_id": "MSFT",
                      "schema_version": calculation["schema_version"]}
        descriptor_raw = json.dumps(descriptor, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"))
        reference = ArtifactRef(
            "calculation", identifier, hashlib.sha256(descriptor_raw.encode()).hexdigest())
        publication = prepare(title="Recorded calculation", blocks=[{
            "classification": "recorded_calculation",
            "parts": [{"kind": "text", "text": "Recorded value: "},
                      {"kind": "calculation", "id": identifier,
                       "value_formation": "derived", "attribution": None}],
            "support": [], "reasoning": "", "limitations": [],
        }], refs={"calculation": {identifier: calculation}}, facts={},
           calculations={identifier: calculation}, max_chars=10000)

        class FullRecordVerifier:
            result = None

            def stream_verification(inner_self, messages, tools, cancel_event):
                packet = json.loads(messages[0]["content"])
                handlers = {tool["name"]: tool["handler"] for tool in tools}
                inner_self.result = handlers["get_candidate_reference"](
                    kind="calculation", identifier=identifier)
                self.assertEqual(inner_self.result["state"], "available")
                self.assertEqual(inner_self.result["record"]["inputs"], calculation["inputs"])
                self.assertEqual(inner_self.result["record"]["formula"], calculation["formula"])
                self.assertEqual(inner_self.result["record"]["value"], calculation["value"])
                self.assertNotEqual(inner_self.result["record_sha256"],
                                    inner_self.result["candidate_reference_sha256"])
                receipt = inner_self.result["_verification_receipt"]
                for node_id in packet["content_nodes"]:
                    handlers["record_verification_finding"](
                        version_id=packet["version_id"], node_id=node_id,
                        check_kind="semantic_support", outcome="unresolved",
                        explanation="Contract read exercised without semantic acceptance.",
                        receipt_ids=[receipt["identifier"]])
                handlers["finalize_verification"](
                    version_id=packet["version_id"], outcome="unresolved",
                    limitations=["Semantic review intentionally not performed."],
                    semantic_review={"original_question_coverage": "Controlled assessment of the original question."},
                    verification={"method": "controlled-full-record-contract"})
                yield {"type": "usage", "tokens": 1}

        provider = FullRecordVerifier()
        service, _conversation, submitted, saved = self.candidate_service(
            provider, publication=publication, input_snapshot=snapshot,
            research_factory=lambda _packet: research,
            calculation_refs=(reference,),
            author_inputs={"claim": {}, "evidence": {}, "passage": {}, "query": {},
                           "calculation": {identifier: descriptor}},
            author_receipts=({
                "tool_name": "calculate_facts",
                "arguments": {"operation": "sum", "fact_ids": [fact_id, fact_id]},
                "result": calculation,
            },))
        self.assertTrue(DurableWorker(
            service, worker_id="full-calculation-read", heartbeat_seconds=.1).run_once())
        self.assertEqual(provider.result["record_type"], "calculation")
        with closing(service._connect()) as connection:
            state = connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (saved["verify_job_id"],)).fetchone()[0]
        self.assertEqual(state, "succeeded")

    def test_provider_output_time_and_tool_usage_survive_dead_attempt_reclaim(self):
        service, conversation, submitted, saved = self.candidate_service(ControlledVerifier())
        dead = service.jobs.claim("dead-attempt", job_types=("verify",), worker_pid=99999999)
        limits = {"tool_calls": 4, "provider_events": 8,
                  "output_chars": 400, "payload_bytes": 1_000,
                  "elapsed_seconds": 30}
        service.verification_store.begin_usage(dead)
        first = service.verification_store.consume_usage(
            dead, provider_events=2, output_chars=90, limits=limits)
        service.verification_store.consume_usage(dead, tool_calls=1, limits=limits)
        # Simulate abrupt process death: no end_usage call. Queue reclamation must
        # prove the recorded PID is gone, then the next attempt settles that clock.
        with closing(service._connect()) as connection, connection:
            connection.execute("UPDATE jobs SET lease_expires_at=0 WHERE job_id=?", (dead.job_id,))
            connection.execute("UPDATE model_runtime_slot SET lease_expires_at=0 WHERE attempt_id=?",
                               (dead.attempt_id,))
        reclaimed = service.jobs.claim("reclaimed", job_types=("verify",))
        self.assertIsNotNone(reclaimed)
        resumed = service.verification_store.begin_usage(reclaimed)
        self.assertEqual(resumed["provider_events"], 2)
        self.assertEqual(resumed["output_chars"], 90)
        self.assertEqual(resumed["payload_bytes"], 0)
        self.assertEqual(resumed["tool_calls"], 1)
        self.assertGreaterEqual(resumed["elapsed_seconds"], first["elapsed_seconds"])
        with self.assertRaises(VerificationBudgetExceeded) as caught:
            service.verification_store.consume_usage(
                reclaimed, payload_bytes=1_001, limits=limits, stage="tool_result")
        self.assertEqual(caught.exception.code, "payload_bytes")
        service.verification_store.end_usage(reclaimed)
        blocked = service.verification_store.begin_usage(reclaimed)
        self.assertEqual(blocked["payload_bytes"], 1_001)
        self.assertEqual(blocked["exhaustion_stage"], "tool_result")
        self.assertEqual(blocked["exhaustion_code"], "payload_bytes")
        self.assertEqual(blocked["exhaustion_requested_size"], 1_001)
        service.verification_store.end_usage(reclaimed)
        with self.assertRaises(VerificationBudgetExceeded) as terminal:
            service.verification_executor.execute(reclaimed, threading.Event())
        self.assertEqual(terminal.exception.code, "payload_bytes")

    def test_large_source_result_uses_payload_budget_not_model_output_budget(self):
        class LargeResearch:
            def definitions(self):
                return [{
                    "name": "search_evidence", "description": "large controlled read",
                    "parameters": {"type": "object"},
                    "handler": lambda **_arguments: {"source": "x" * 20_000},
                }]

        config = VerificationExecutionConfig(
            max_output_chars=10, max_event_payload_bytes=100_000,
            max_tool_result_bytes=50_000)
        service, _conversation, _submitted, saved = self.candidate_service(
            ControlledVerifier(), research_factory=lambda _packet: LargeResearch(),
            verification_config=config)
        claim = service.jobs.claim("large-source", job_types=("verify",))
        service.verification_executor.execute(claim, threading.Event())
        with closing(service._connect()) as connection:
            usage = connection.execute(
                "SELECT output_chars,payload_bytes FROM verification_usage WHERE job_id=?",
                (saved["verify_job_id"],)).fetchone()
        self.assertEqual(usage["output_chars"], 0)
        self.assertGreater(usage["payload_bytes"], 20_000)

    def test_oversized_result_is_recoverable_and_narrower_query_can_continue(self):
        class SizedResearch:
            def definitions(self):
                return [{
                    "name": "search_evidence", "description": "sized controlled read",
                    "parameters": {"type": "object"},
                    "handler": lambda query: {
                        "source": "x" * (1_000 if query == "broad" else 10)},
                }]

        class NarrowingVerifier(ControlledVerifier):
            def stream_verification(inner_self, messages, tools, cancel_event):
                handlers = {tool["name"]: tool["handler"] for tool in tools}
                with self.assertRaises(ToolError) as caught:
                    handlers["search_evidence"](query="broad")
                inner_self.error_code = caught.exception.code
                receipt = handlers["search_evidence"](query="narrow")["_verification_receipt"]
                packet = json.loads(messages[0]["content"])
                for node_id in packet["content_nodes"]:
                    for check_kind in packet["verification_checks"][node_id]["required"]:
                        handlers["record_verification_finding"](
                            version_id=packet["version_id"], node_id=node_id, check_kind=check_kind,
                            outcome="insufficient_material_disclosed", explanation="Narrowed query.",
                            receipt_ids=[receipt["identifier"]])
                handlers["finalize_verification"](
                    version_id=packet["version_id"], outcome="verified_with_limitations",
                    limitations=["Evidence remained limited."],
                    semantic_review={"original_question_coverage": "Controlled assessment of the original question."}, verification={})
                yield {"type": "usage", "tokens": 1}

        provider = NarrowingVerifier()
        config = VerificationExecutionConfig(
            max_event_payload_bytes=10_000, max_tool_result_bytes=100)
        service, _conversation, _submitted, saved = self.candidate_service(
            provider, research_factory=lambda _packet: SizedResearch(),
            verification_config=config)
        claim = service.jobs.claim("narrow-result", job_types=("verify",))
        service.verification_executor.execute(claim, threading.Event())
        self.assertEqual(provider.error_code, "tool_result_too_large")
        with closing(service._connect()) as connection:
            usage = connection.execute(
                "SELECT tool_calls,payload_bytes FROM verification_usage WHERE job_id=?",
                (saved["verify_job_id"],)).fetchone()
            rejected = json.loads(connection.execute(
                "SELECT result_json FROM verification_tool_receipts WHERE job_id=? "
                "AND instr(result_json,'tool_result_too_large')>0",
                (saved["verify_job_id"],)).fetchone()[0])
        self.assertGreaterEqual(usage["tool_calls"], 2)
        self.assertGreater(usage["payload_bytes"], 1_000)
        self.assertEqual(rejected["stage"], "tool_result")
        self.assertGreater(rejected["requested_size"], 1_000)

    def test_model_delta_budget_is_bounded(self):
        class TextVerifier:
            def stream_verification(self, messages, tools, cancel_event):
                yield {"type": "delta", "text": "123456"}
                yield {"type": "assistant_message", "text": "123456"}
                yield {"type": "final_answer", "text": "123456"}

        config = VerificationExecutionConfig(max_output_chars=5)
        service, _conversation, _submitted, saved = self.candidate_service(
            TextVerifier(), verification_config=config)
        claim = service.jobs.claim("model-output", job_types=("verify",))
        with self.assertRaises(VerificationBudgetExceeded) as caught:
            service.verification_executor.execute(claim, threading.Event())
        self.assertEqual(caught.exception.code, "output_chars")
        with closing(service._connect()) as connection:
            usage = connection.execute(
                "SELECT output_chars,exhaustion_stage,exhaustion_code,"
                "exhaustion_requested_size FROM verification_usage WHERE job_id=?",
                (saved["verify_job_id"],)).fetchone()
        self.assertEqual(usage["output_chars"], 6)
        self.assertEqual(usage["exhaustion_stage"], "model_output")
        self.assertEqual(usage["exhaustion_code"], "output_chars")
        self.assertEqual(usage["exhaustion_requested_size"], 6)

    def test_repeated_complete_answer_events_do_not_triple_count_delta_text(self):
        class RepeatingVerifier:
            def stream_verification(self, messages, tools, cancel_event):
                yield {"type": "delta", "text": "answer"}
                yield {"type": "assistant_message", "text": "answer"}
                yield {"type": "final_answer", "text": "answer"}

        service, _conversation, _submitted, saved = self.candidate_service(RepeatingVerifier())
        claim = service.jobs.claim("repeated-answer", job_types=("verify",))
        with self.assertRaisesRegex(RuntimeError, "without strict finalization"):
            service.verification_executor.execute(claim, threading.Event())
        with closing(service._connect()) as connection:
            output_chars = connection.execute(
                "SELECT output_chars FROM verification_usage WHERE job_id=?",
                (saved["verify_job_id"],)).fetchone()[0]
        self.assertEqual(output_chars, len("answer"))

    def test_failed_revision_and_finding_handlers_are_charged(self):
        class FailingControls:
            def stream_verification(inner_self, messages, tools, cancel_event):
                handlers = {tool["name"]: tool["handler"] for tool in tools}
                packet = json.loads(messages[0]["content"])
                for name, arguments in (
                    ("preview_verification_revision", {
                        "version_id": packet["version_id"], "mode": "replace",
                        "title": "", "blocks": [], "revision_summary": "invalid"}),
                    ("record_verification_finding", {
                        "version_id": packet["version_id"], "node_id": "missing-node",
                        "check_kind": "semantic_support", "outcome": "unchecked",
                        "explanation": "invalid", "receipt_ids": []}),
                ):
                    try:
                        handlers[name](**arguments)
                    except Exception:
                        pass
                yield {"type": "usage", "tokens": 1}

        service, _conversation, _submitted, saved = self.candidate_service(FailingControls())
        claim = service.jobs.claim("failed-controls", job_types=("verify",))
        with self.assertRaisesRegex(RuntimeError, "without strict finalization"):
            service.verification_executor.execute(claim, threading.Event())
        with closing(service._connect()) as connection:
            calls = connection.execute(
                "SELECT tool_calls FROM verification_usage WHERE job_id=?",
                (saved["verify_job_id"],)).fetchone()[0]
        self.assertEqual(calls, 2)

    def test_invalid_preview_does_not_append_or_consume_revision_round_and_stale_handles_reject(self):
        class PreviewVerifier:
            stale_handle_code = None
            stale_version_code = None

            def stream_verification(inner_self, messages, tools, cancel_event):
                packet = json.loads(messages[0]["content"])
                handlers = {tool["name"]: tool["handler"] for tool in tools}
                block = {"classification": "evidence_gap",
                         "parts": [{"kind": "text", "text": "Corrected but unresolved."}],
                         "support": [], "reasoning": "", "limitations": ["Still limited."]}
                with self.assertRaises(ToolError):
                    handlers["preview_verification_revision"](
                        version_id=packet["version_id"], mode="patch",
                        replacements=[{"block_id": "unknown", "block": block}],
                        revision_summary="Malformed target must not append.")
                first = handlers["preview_verification_revision"](
                    version_id=packet["version_id"], mode="patch",
                    replacements=[{"block_id": "report-block-1", "block": block}],
                    revision_summary="First valid transient preview.")
                replacement = deepcopy(block)
                replacement["parts"][0]["text"] = "Latest corrected but unresolved."
                second = handlers["preview_verification_revision"](
                    version_id=packet["version_id"], mode="patch",
                    replacements=[{"block_id": "report-block-1", "block": replacement}],
                    revision_summary="Latest valid preview wins.")
                try:
                    handlers["revise_verification_candidate"](
                        version_id=packet["version_id"], preview_id=first["preview_id"])
                except ToolError as exc:
                    inner_self.stale_handle_code = exc.code
                revised = handlers["revise_verification_candidate"](
                    version_id=packet["version_id"], preview_id=second["preview_id"])
                try:
                    handlers["preview_verification_revision"](
                        version_id=packet["version_id"], mode="patch", title="stale",
                        revision_summary="Stale base must reject.")
                except ToolError as exc:
                    inner_self.stale_version_code = exc.code
                for node_id in revised["content_nodes"]:
                    handlers["record_verification_finding"](
                        version_id=revised["version_id"], node_id=node_id,
                        check_kind="semantic_support", outcome="unresolved",
                        explanation="Controlled unresolved lifecycle check.", receipt_ids=[])
                handlers["finalize_verification"](
                    version_id=revised["version_id"], outcome="unresolved",
                    limitations=["Controlled unresolved lifecycle check."],
                    semantic_review={"original_question_coverage": "Coverage remains unresolved."},
                    verification={"method": "preview-contract"})
                yield {"type": "usage", "tokens": 1}

        provider = PreviewVerifier()
        service, _conversation, _submitted, saved = self.candidate_service(
            provider, verification_config=VerificationExecutionConfig(max_revision_rounds=1))
        claim = service.jobs.claim("preview-lifecycle", job_types=("verify",))
        service.verification_executor.execute(claim, threading.Event())
        with closing(service._connect()) as connection:
            versions = connection.execute(
                "SELECT version_number,revision_summary FROM candidate_versions ORDER BY version_number").fetchall()
        self.assertEqual([(row[0], row[1]) for row in versions],
                         [(1, "author candidate"), (2, "Latest valid preview wins.")])
        self.assertEqual(provider.stale_handle_code, "stale_preview")
        self.assertEqual(provider.stale_version_code, "stale_version")

    def test_explicit_replacement_reports_removed_nodes_and_requires_scope_assessment(self):
        initial = prepare(title="Two sections", blocks=[
            {"classification": "evidence_gap", "parts": [{"kind": "text", "text": text}],
             "support": [], "reasoning": "", "limitations": ["limited"]}
            for text in ("First.", "Second.")
        ], refs={}, facts={}, calculations={}, max_chars=10000)

        class ReplacementDiffVerifier:
            removed = None

            def stream_verification(inner_self, messages, tools, cancel_event):
                packet = json.loads(messages[0]["content"])
                handlers = {tool["name"]: tool["handler"] for tool in tools}
                arguments = dict(
                    version_id=packet["version_id"], mode="replace", title="One section",
                    blocks=[{"classification": "evidence_gap",
                             "parts": [{"kind": "text", "text": "Only first remains."}],
                             "support": [], "reasoning": "", "limitations": ["limited"]}],
                    revision_summary="Explicit restructuring removes one section.")
                with self.assertRaises(ToolError):
                    handlers["preview_verification_revision"](**arguments)
                preview = handlers["preview_verification_revision"](
                    **arguments,
                    scope_coverage="The verifier must assess whether removing the second section still answers the original question.")
                inner_self.removed = preview["diff"]["removed_node_ids"]
                self.assertEqual(preview["original_question"], "ordinary question")
                for node_id in packet["content_nodes"]:
                    handlers["record_verification_finding"](
                        version_id=packet["version_id"], node_id=node_id,
                        check_kind="semantic_support", outcome="unresolved",
                        explanation="Replacement was previewed but not committed.", receipt_ids=[])
                handlers["finalize_verification"](
                    version_id=packet["version_id"], outcome="unresolved",
                    limitations=["Replacement was not committed."],
                    semantic_review={"original_question_coverage": "Original candidate remains unresolved."},
                    verification={"method": "replacement-diff"})
                yield {"type": "usage", "tokens": 1}

        provider = ReplacementDiffVerifier()
        service, _conversation, _submitted, saved = self.candidate_service(provider, publication=initial)
        claim = service.jobs.claim("replacement-diff", job_types=("verify",))
        service.verification_executor.execute(claim, threading.Event())
        self.assertEqual(provider.removed, ["report-block-2"])
        with closing(service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM candidate_versions WHERE candidate_id=?",
                (saved["candidate_id"],)).fetchone()[0], 1)

    def test_provider_event_stream_is_cumulatively_bounded(self):
        class EndlessVerifier:
            def stream_verification(self, messages, tools, cancel_event):
                while True:
                    yield {"type": "usage", "tokens": 1}

        config = VerificationExecutionConfig(max_provider_events=3)
        service, _conversation, _submitted, saved = self.candidate_service(
            EndlessVerifier(), verification_config=config)
        claim = service.jobs.claim("bounded-events", job_types=("verify",))
        with self.assertRaises(VerificationBudgetExceeded) as caught:
            service.verification_executor.execute(claim, threading.Event())
        self.assertEqual(caught.exception.code, "provider_events")
        with closing(service._connect()) as connection:
            usage = connection.execute(
                "SELECT provider_events,exhaustion_requested_size FROM verification_usage "
                "WHERE job_id=?", (saved["verify_job_id"],)).fetchone()
        self.assertEqual(usage["provider_events"], 4)
        self.assertEqual(usage["exhaustion_requested_size"], 1)

    def test_staged_finalize_does_not_publish_before_queued_delta_is_charged(self):
        class FinalizeThenOversize(ControlledVerifier):
            def __init__(inner_self):
                super().__init__()
                inner_self.finalize_result = None

            def stream_verification(inner_self, messages, tools, cancel_event):
                inner_self.messages = messages
                packet = json.loads(messages[0]["content"])
                handlers = {tool["name"]: tool["handler"] for tool in tools}
                receipt = handlers["search_evidence"](query="candidate limitation")
                ref = receipt["_verification_receipt"]
                for node_id in packet["content_nodes"]:
                    for check_kind in packet["verification_checks"][node_id]["required"]:
                        handlers["record_verification_finding"](
                            version_id=packet["version_id"], node_id=node_id, check_kind=check_kind,
                            outcome="insufficient_material_disclosed", explanation="Searched frozen scope.",
                            receipt_ids=[ref["identifier"]])
                inner_self.finalize_result = handlers["finalize_verification"](
                    version_id=packet["version_id"], outcome="verified_with_limitations",
                    limitations=["Authorized frozen material does not answer the core question."],
                    semantic_review={"original_question_coverage": "Controlled assessment of the original question."},
                    verification={"method": "controlled-provider-test"})
                yield {"type": "delta", "text": "too-large-terminal-delta"}

        provider = FinalizeThenOversize()
        config = VerificationExecutionConfig(max_output_chars=5)
        service, conversation, _submitted, saved = self.candidate_service(
            provider, verification_config=config)
        claim = service.jobs.claim("staged-budget", job_types=("verify",))
        with self.assertRaises(VerificationBudgetExceeded) as caught:
            service.verification_executor.execute(claim, threading.Event())
        self.assertEqual(caught.exception.code, "output_chars")
        self.assertEqual(provider.finalize_result["staged"], True)
        self.assertNotIn("formal_message_id", provider.finalize_result)
        with closing(service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM verification_records WHERE job_id=?",
                (saved["verify_job_id"],)).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0], 0)
            self.assertIsNone(connection.execute(
                "SELECT message_id FROM report_current WHERE conversation_id=?",
                (conversation["conversation_id"],)).fetchone()[0])
            usage = connection.execute(
                "SELECT output_chars,exhaustion_code FROM verification_usage WHERE job_id=?",
                (saved["verify_job_id"],)).fetchone()
        self.assertEqual((usage["output_chars"], usage["exhaustion_code"]),
                         (config.max_output_chars + 1, "output_chars"))

    def test_receipt_backed_passage_derives_frozen_company_and_version(self):
        documents, research, snapshot, version, passage = self.document_research_fixture()
        passage_id = passage["passage_id"]
        refs = {"claim": {}, "evidence": {}, "passage": {passage_id: passage},
                "query": {}, "calculation": {}}
        publication = prepare(title="Passage-backed report", blocks=[{
            "classification": "source_statement",
            "parts": [{"kind": "text", "text": "The annual filing discusses cloud revenue."}],
            "support": [{"kind": "passage", "id": passage_id}],
            "reasoning": "", "limitations": [],
        }], refs=refs, facts={}, calculations={}, max_chars=10000)
        raw = json.dumps(passage, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
        evidence_refs = (ArtifactRef(
            "passage", passage_id, hashlib.sha256(raw.encode()).hexdigest()),)

        class PassageVerifier:
            receipt = None

            def stream_verification(inner_self, messages, tools, cancel_event):
                packet = json.loads(messages[0]["content"])
                handlers = {tool["name"]: tool["handler"] for tool in tools}
                result = handlers["read_passage"](passage_id=passage_id, context_chars=500)
                inner_self.receipt = result["_verification_receipt"]
                for node_id in packet["content_nodes"]:
                    for check_kind in packet["verification_checks"][node_id]["required"]:
                        handlers["record_verification_finding"](
                            version_id=packet["version_id"], node_id=node_id, check_kind=check_kind,
                            outcome="supported", explanation="Exact frozen passage was re-read.",
                            receipt_ids=[inner_self.receipt["identifier"]])
                handlers["finalize_verification"](
                    version_id=packet["version_id"], outcome="verified", limitations=[],
                    semantic_review={"original_question_coverage": "The report addresses the original question."},
                    verification={"method": "controlled-passage-test"})
                yield {"type": "usage", "tokens": 1}

        provider = PassageVerifier()
        service, conversation, _submitted, saved = self.candidate_service(
            provider, publication=publication, input_snapshot=snapshot,
            research_factory=lambda _packet: research, evidence_refs=evidence_refs,
            author_inputs=refs, document_service=documents)
        self.assertTrue(DurableWorker(
            service, worker_id="passage-closure", heartbeat_seconds=.1).run_once())
        candidate = service.verification_store.get_candidate(
            conversation["conversation_id"], saved["candidate_id"])
        self.assertEqual(candidate["status"], "verified")
        formal_id = candidate["formal_message_id"]
        answer = service.answer_context(conversation["conversation_id"], formal_id)
        citation = next(item for item in answer["citations"]
                        if item["kind"] == "passage" and item["id"] == passage_id)
        self.assertEqual(citation["company_id"], "MSFT")
        self.assertEqual(citation["document_version_id"], version["document_version_id"])
        self.assertNotEqual(citation["document_version_id"], "forged-version")
        clicked = service.read_citation(
            conversation["conversation_id"], formal_id, "passage", passage_id)
        self.assertEqual(clicked["document_version_id"], version["document_version_id"])
        self.assertEqual(clicked["source_company_id"], "MSFT")
        artifact = AnswerArtifactService(service).get(conversation["conversation_id"], formal_id)
        trace = next(item for item in artifact["traces"]
                     if item.get("reference") == {"kind": "passage", "id": passage_id})
        self.assertEqual(trace["source"]["document_version_id"], version["document_version_id"])

    def test_receipt_backed_100_minus_40_revision_publishes_and_reads_citation(self):
        # Add controlled facts to the temporary evidence fixture before freezing it.
        # Their quotes, source hash, locators, and current evidence-store rows remain
        # reader-verifiable; only the fake provider supplies semantic judgement.
        with closing(sqlite3.connect(self.evidence)) as connection, connection:
            source = connection.execute(
                "SELECT source_id,sha256,parser_version,text FROM sources "
                "WHERE instr(text,'100')>0 AND instr(text,'40')>0 LIMIT 1").fetchone()
            activity_id = connection.execute(
                "SELECT activity_id FROM activities ORDER BY activity_id LIMIT 1").fetchone()[0]
            facts = []
            for fact_id, metric, value, evidence_id in (
                    ("revenue-100", "revenue", "100", "revenue-evidence"),
                    ("cost-40", "cost", "40", "cost-evidence")):
                start = source[3].index(value)
                connection.execute(
                    "INSERT INTO evidence(evidence_id,source_id,quote,start,end,table_index,row_index,source_sha256,parser_version) "
                    "VALUES(?,?,?,?,?,NULL,NULL,?,?)",
                    (evidence_id, source[0], value, start, start + len(value), source[1], source[2]))
                connection.execute(
                    "INSERT INTO facts(fact_id,company_id,metric,value,unit,period_start,period_end,period_kind,basis,evidence_id,activity_id) "
                    "VALUES(?,'MSFT',?,?, 'USD millions','2025-01-01','2025-12-31','year','controlled fixture',?,?)",
                    (fact_id, metric, value, evidence_id, activity_id))
        research = ResearchTools(self.evidence, "MSFT")
        facts = [research._facts[identifier] for identifier in ("revenue-100", "cost-40")]
        snapshot = {"authoritative": True, "truncated": False,
                    "data_version": deepcopy(research.data_version), "evidence": {
            "company_id": "MSFT", **{key: deepcopy(research._company[key]) for key in (
                "sources", "evidence", "claims", "activities", "facts", "evidence_chains")},
        }, "documents": {"coverage": deepcopy(research.document_coverage),
                          "document_version_ids": []}}
        initial = prepare(title="Uncorrected candidate", blocks=[{
            "classification": "evidence_gap",
            "parts": [{"kind": "text", "text": "Difference: 899 (unverified)."}],
            "support": [], "reasoning": "", "limitations": ["Numeric claim needs correction."],
        }], refs={}, facts={}, calculations={}, max_chars=10000)

        class PositiveVerifier:
            def __init__(self):
                self.revision = None
                self.calculation_id = None
                self.current_receipt = None
                self.partial_finalize_code = None
                self.custom_kind_rejected = False

            def stream_verification(inner_self, messages, tools, cancel_event):
                handlers = {tool["name"]: tool["handler"] for tool in tools}
                calculated = handlers["calculate_facts"](
                    operation="difference", fact_ids=["revenue-100", "cost-40"])
                self.assertEqual(calculated["value"], "60")
                inner_self.calculation_id = calculated["calculation_id"]
                preview = handlers["preview_verification_revision"](
                    version_id=json.loads(messages[0]["content"])["version_id"], mode="replace",
                    title="Corrected numeric candidate",
                    blocks=[{"classification": "recorded_calculation", "parts": [
                        {"kind": "text", "text": "Revenue minus cost: "},
                        {"kind": "calculation", "id": inner_self.calculation_id,
                         "value_formation": "derived", "attribution": None},
                    ], "support": [], "reasoning": "Subtract cost from revenue.",
                        "limitations": []}],
                    revision_summary="Replaced unsupported 899 with replayed 100 minus 40.")
                revised = handlers["revise_verification_candidate"](
                    version_id=preview["base_version_id"], preview_id=preview["preview_id"])
                inner_self.revision = revised
                self.assertIn("publication", revised)
                self.assertIn("content_nodes", revised)
                self.assertRegex(revised["publication_sha256"], "^[0-9a-f]{64}$")
                current = handlers["calculate_facts"](
                    operation="difference", fact_ids=["revenue-100", "cost-40"])
                inner_self.current_receipt = current["_verification_receipt"]
                numeric_node = "report-block-1"
                self.assertEqual(revised["verification_checks"][numeric_node], {
                    "required": ["source_identity", "temporal_alignment",
                                 "numeric_reproducibility", "semantic_support"],
                    "not_applicable": ["limitation_disclosure"],
                })
                definition = next(tool for tool in tools
                                  if tool["name"] == "record_verification_finding")
                self.assertEqual(definition["parameters"]["properties"]["check_kind"]["enum"], [
                    "source_identity", "temporal_alignment", "numeric_reproducibility",
                    "semantic_support", "limitation_disclosure",
                ])
                try:
                    handlers["record_verification_finding"](
                        version_id=revised["version_id"], node_id=numeric_node,
                        check_kind="invented_check", outcome="supported",
                        explanation="An invented dimension must be rejected at the tool boundary.")
                except ValueError:
                    inner_self.custom_kind_rejected = True
                handlers["record_verification_finding"](
                    version_id=revised["version_id"], node_id=numeric_node,
                    check_kind="source_identity", outcome="corrected_and_rechecked",
                    explanation="The replayed calculation has a current source identity.",
                    receipt_ids=[inner_self.current_receipt["identifier"]])
                try:
                    handlers["finalize_verification"](
                        version_id=revised["version_id"], outcome="verified", limitations=[],
                        semantic_review={"original_question_coverage": "Partial check must not publish."},
                        verification={"method": "partial-check-negative"})
                except ToolError as exc:
                    inner_self.partial_finalize_code = exc.code
                for node_id in revised["content_nodes"]:
                    for check_kind in revised["verification_checks"][node_id]["required"]:
                        if node_id == numeric_node and check_kind == "source_identity":
                            continue
                        handlers["record_verification_finding"](
                            version_id=revised["version_id"], node_id=node_id,
                            check_kind=check_kind, outcome="corrected_and_rechecked",
                            explanation="Frozen facts 100 and 40 deterministically replay to 60.",
                            receipt_ids=[inner_self.current_receipt["identifier"]])
                handlers["finalize_verification"](
                    version_id=revised["version_id"], outcome="verified", limitations=[],
                    semantic_review={"original_question_coverage": "The corrected report addresses the original question."},
                    verification={"method": "controlled-provider-positive-test",
                                  "semantic_truth_guaranteed": False})
                yield {"type": "usage", "tokens": 1}

        provider = PositiveVerifier()
        service, conversation, submitted, saved = self.candidate_service(
            provider, publication=initial, input_snapshot=snapshot,
            research_factory=lambda _packet: research)
        worker = DurableWorker(service, worker_id="positive-60", heartbeat_seconds=.1)
        self.assertTrue(worker.run_once())
        candidate = service.verification_store.get_candidate(
            conversation["conversation_id"], saved["candidate_id"])
        self.assertEqual(candidate["status"], "verified", provider.__dict__)
        self.assertEqual(provider.partial_finalize_code, "missing_terminal_findings")
        self.assertTrue(provider.custom_kind_rejected)
        self.assertEqual(len(candidate["versions"]), 2)
        formal_id = candidate["formal_message_id"]
        self.assertIsNotNone(formal_id)
        artifact = AnswerArtifactService(service).get(conversation["conversation_id"], formal_id)
        self.assertIn("Revenue minus cost: 60 USD millions", artifact["answer"])
        calculation = next(item for item in artifact["calculations"]
                           if item.get("calculation_id") == provider.calculation_id)
        self.assertEqual(calculation["value"], "60")
        trace = next(item for item in artifact["traces"]
                     if item.get("reference") == {
                         "kind": "calculation", "id": provider.calculation_id})
        receipt_input = next(step["inputs"] for step in trace["steps"]
                             if step["operation"] == "authoritative_calculation_event")
        self.assertEqual(receipt_input["verification_receipt_id"],
                         provider.current_receipt["identifier"])
        self.assertEqual(receipt_input["verification_receipt_sha256"],
                         provider.current_receipt["sha256"])
        published = service.published_verification_evidence(
            conversation["conversation_id"], formal_id)
        self.assertEqual(published["version_id"], provider.revision["version_id"])
        self.assertEqual(published["publication_sha256"], provider.revision["publication_sha256"])
        self.assertTrue(any(item.get("name") == "calculate_facts"
                            and item.get("verification_receipt_id") ==
                            provider.current_receipt["identifier"]
                            and item.get("verification_receipt_sha256") ==
                            provider.current_receipt["sha256"]
                            for item in published["receipts"]))
        citation = service.read_citation(
            conversation["conversation_id"], formal_id, "calculation", provider.calculation_id)
        self.assertEqual(citation["calculation"]["value"], "60")
        self.assertEqual(citation["calculation"]["formula"], "operand[0] - operand[1]")
        self.assertEqual([item["value"] for item in citation["calculation"]["inputs"]],
                         ["100", "40"])

    def test_author_document_calculation_replays_in_a_fresh_verifier(self):
        documents, author_research, snapshot, version, _passage = self.document_research_fixture()
        common = {
            "document_version_id": version["document_version_id"], "table_index": 0,
            "header_row_indices": [0], "row_label_column_index": 0, "column_index": 1,
            "unit": "USD millions", "period_start": "2024-07-01", "period_end": "2025-06-30",
            "period_kind": "year", "basis": "issuer table presentation",
            "metadata_mode": "explicit_interpretation",
        }
        results = [author_research.extract_document_table_fact(
            **common, row_index=row, expected_value=value, metric=metric)
            for row, value, metric in ((1, "240", "revenue"), (2, "120", "capital_expenditures"))]
        results = [ChatService._authoritative_document_fact_result(author_research, item) for item in results]
        ids = [item["fact"]["fact_id"] for item in results]
        calculation = author_research.calculate_facts(operation="difference", fact_ids=ids)
        calculation_id = calculation["calculation_id"]
        evidence_id = results[0]["fact"]["evidence_id"]
        evidence_catalog = {evidence_id: results[0]["fact"]}
        publication = prepare(title="Author document calculation", blocks=[{
            "classification": "recorded_calculation",
            "parts": [{"kind": "text", "text": "Difference: "},
                      {"kind": "calculation", "id": calculation_id,
                       "value_formation": "derived", "attribution": None}],
            "support": [], "reasoning": "Subtract the exact source cells.", "limitations": [],
        }, {
            "classification": "source_statement",
            "parts": [{"kind": "text", "text": "Revenue: "},
                      {"kind": "fact", "id": ids[0],
                       "value_formation": "observed", "attribution": None}],
            "support": [], "reasoning": "Read the original revenue cell.", "limitations": [],
        }], refs={"calculation": {calculation_id: calculation}, "evidence": evidence_catalog},
           facts=author_research._facts, calculations={calculation_id: calculation}, max_chars=10000)
        catalog = AuthorInputs.decode({"claim": {}, "evidence": evidence_catalog, "passage": {}, "query": {},
                                      "calculation": {calculation_id: calculation}})
        fresh = ResearchTools(self.evidence, "MSFT", documents, frozen_snapshot=snapshot)
        self.assertFalse(set(ids) & set(fresh._facts))
        case = self

        class FreshVerifier:
            def stream_verification(self, messages, tools, cancel_event):
                packet = json.loads(messages[0]["content"])
                handlers = {tool["name"]: tool["handler"] for tool in tools}
                fresh._facts[ids[0]] = {**results[0]["fact"], "value": "999"}
                with case.assertRaisesRegex(VerificationIntegrityError, "conflicts with verifier input"):
                    handlers["get_candidate_reference"](kind="calculation", identifier=calculation_id)
                case.assertEqual(fresh._facts.pop(ids[0])["value"], "999")
                read = handlers["get_candidate_reference"](kind="calculation", identifier=calculation_id)
                case.assertEqual(read["state"], "available", read)
                case.assertEqual(read["record"]["value"], "120")
                unrelated = handlers["search_documents"](query="Revenue", limit=1)
                with case.assertRaisesRegex(VerificationIntegrityError, calculation_id):
                    handlers["record_verification_finding"](
                        version_id=packet["version_id"], node_id="report-block-1",
                        check_kind="semantic_support", outcome="supported",
                        explanation="An unrelated read must not cover a numeric binding.",
                        receipt_ids=[unrelated["_verification_receipt"]["identifier"]])
                replay = handlers["calculate_facts"](operation="difference", fact_ids=ids)
                case.assertEqual(replay["calculation_id"], calculation_id)
                direct = handlers["get_candidate_reference"](kind="evidence", identifier=evidence_id)
                case.assertEqual(direct["state"], "available")
                for node_id in packet["content_nodes"]:
                    for check_kind in packet["verification_checks"][node_id]["required"]:
                        handlers["record_verification_finding"](
                            version_id=packet["version_id"], node_id=node_id, check_kind=check_kind,
                            outcome="supported", explanation="Replayed source-backed author inputs in a fresh verifier.")
                handlers["finalize_verification"](
                    version_id=packet["version_id"], outcome="verified", limitations=[],
                    semantic_review={"original_question_coverage": "The exact source-cell difference was checked."},
                    verification={"method": "fresh-verifier-document-calculation"})
                yield {"type": "usage"}

        service, conversation, _submitted, saved = self.candidate_service(
            FreshVerifier(), publication=publication, input_snapshot=snapshot,
            calculation_refs=catalog.references(calculations=True),
            evidence_refs=catalog.references(calculations=False), author_inputs=catalog.to_json(),
            author_receipts=tuple({"tool_name": "extract_document_table_fact", "result": item} for item in results)
                + ({"tool_name": "calculate_facts", "result": calculation},),
            research_factory=lambda _packet: fresh, document_service=documents)
        claim = service.jobs.claim("fresh-verifier-replay", job_types=("verify",))
        service.verification_executor.execute(claim, threading.Event())
        candidate = service.verification_store.get_candidate(conversation["conversation_id"], saved["candidate_id"])
        self.assertEqual(candidate["status"], "verified")

    def test_storage_validated_dynamic_document_facts_support_verified_calculation(self):
        documents, research, snapshot, version, _passage = self.document_research_fixture()
        initial = prepare(title="Uncorrected document calculation", blocks=[{
            "classification": "evidence_gap",
            "parts": [{"kind": "text", "text": "The table calculation is pending."}],
            "support": [], "reasoning": "", "limitations": ["Document cells need replay."],
        }], refs={}, facts={}, calculations={}, max_chars=10000)

        class DynamicFactVerifier:
            calculation_id = None
            extraction_receipts = []

            def stream_verification(inner_self, messages, tools, cancel_event):
                handlers = {tool["name"]: tool["handler"] for tool in tools}
                common = {
                    "document_version_id": version["document_version_id"], "table_index": 0,
                    "header_row_indices": [0], "row_label_column_index": 0,
                    "column_index": 1, "unit": "USD millions",
                    "period_start": "2024-07-01", "period_end": "2025-06-30",
                    "period_kind": "year", "basis": "issuer table presentation",
                    "metadata_mode": "explicit_interpretation",
                }
                revenue = handlers["extract_document_table_fact"](
                    **common, row_index=1, expected_value="240", metric="revenue")
                capex = handlers["extract_document_table_fact"](
                    **common, row_index=2, expected_value="120", metric="capital_expenditures")
                inner_self.extraction_receipts = [
                    revenue["_verification_receipt"], capex["_verification_receipt"]]
                calculated = handlers["calculate_facts"](
                    operation="difference",
                    fact_ids=[revenue["fact"]["fact_id"], capex["fact"]["fact_id"]])
                self.assertEqual(calculated["value"], "120")
                inner_self.calculation_id = calculated["calculation_id"]
                preview = handlers["preview_verification_revision"](
                    version_id=json.loads(messages[0]["content"])["version_id"], mode="replace",
                    title="Storage-validated document calculation",
                    blocks=[{"classification": "recorded_calculation", "parts": [
                        {"kind": "text", "text": "Revenue: "},
                        {"kind": "fact", "id": revenue["fact"]["fact_id"],
                         "value_formation": "observed", "attribution": None},
                        {"kind": "text", "text": "; capital expenditures: "},
                        {"kind": "fact", "id": capex["fact"]["fact_id"],
                         "value_formation": "observed", "attribution": None},
                        {"kind": "text", "text": "; difference: "},
                        {"kind": "calculation", "id": inner_self.calculation_id,
                         "value_formation": "derived", "attribution": None},
                    ], "support": [], "reasoning": "Subtract the exact table cells.",
                        "limitations": []}],
                    datasets=[{"title": "Exact table inputs and calculation",
                               "description": "Two extracted inputs and their difference.",
                               "display": "table", "points": [
                                   {"label": "Revenue", "kind": "fact", "id": revenue["fact"]["fact_id"],
                                    "value_formation": "observed", "attribution": None},
                                   {"label": "Capital expenditures", "kind": "fact", "id": capex["fact"]["fact_id"],
                                    "value_formation": "observed", "attribution": None},
                                   {"label": "Difference", "kind": "calculation", "id": inner_self.calculation_id,
                                    "value_formation": "derived", "attribution": None}]}],
                    revision_summary="Replaced the gap with two extracted cells and exact arithmetic.")
                revised = handlers["revise_verification_candidate"](
                    version_id=preview["base_version_id"], preview_id=preview["preview_id"])
                current_revenue = handlers["extract_document_table_fact"](
                    **common, row_index=1, expected_value="240", metric="revenue")
                current_capex = handlers["extract_document_table_fact"](
                    **common, row_index=2, expected_value="120", metric="capital_expenditures")
                current = handlers["calculate_facts"](
                    operation="difference", fact_ids=[
                        current_revenue["fact"]["fact_id"], current_capex["fact"]["fact_id"]])
                receipt = current["_verification_receipt"]
                for node_id in revised["content_nodes"]:
                    for check_kind in revised["verification_checks"][node_id]["required"]:
                        handlers["record_verification_finding"](
                            version_id=revised["version_id"], node_id=node_id,
                            check_kind=check_kind, outcome="corrected_and_rechecked",
                            explanation="Storage validated both receipt-backed table cells and arithmetic.",
                            receipt_ids=[receipt["identifier"],
                                         current_revenue["_verification_receipt"]["identifier"],
                                         current_capex["_verification_receipt"]["identifier"]])
                handlers["finalize_verification"](
                    version_id=revised["version_id"], outcome="verified", limitations=[],
                    semantic_review={"original_question_coverage": "The corrected report addresses the original question."},
                    verification={"method": "storage-validated-document-facts"})
                yield {"type": "usage", "tokens": 1}

        provider = DynamicFactVerifier()
        service, conversation, _submitted, saved = self.candidate_service(
            provider, publication=initial, input_snapshot=snapshot,
            research_factory=lambda _packet: research, document_service=documents)
        claim = service.jobs.claim("dynamic-document-facts", job_types=("verify",))
        service.verification_executor.execute(claim, threading.Event())
        candidate = service.verification_store.get_candidate(
            conversation["conversation_id"], saved["candidate_id"])
        with closing(service._connect()) as connection:
            job_state = dict(connection.execute(
                "SELECT status,error_json FROM jobs WHERE job_id=?",
                (saved["verify_job_id"],)).fetchone())
        self.assertEqual(candidate["status"], "verified", job_state)
        formal_id = candidate["formal_message_id"]
        detail = service.read_citation(
            conversation["conversation_id"], formal_id,
            "calculation", provider.calculation_id)
        from workbench.api_contracts import validate_response
        validate_response("GET", "/api/chat/citation", 200, detail)
        self.assertEqual((detail["value"], detail["unit"]), ("120", "USD millions"))
        self.assertEqual([item["table"]["target_cell"]["value"]
                          for item in detail["input_sources"]], ["240", "120"])
        evidence_ids = [item["evidence_id"] for item in detail["inputs"]]
        fact_details = [service.read_citation(
            conversation["conversation_id"], formal_id, "evidence", evidence_id)
                        for evidence_id in evidence_ids]
        self.assertEqual([item["fact"]["value"] for item in fact_details], ["240", "120"])
        self.assertEqual([(item["locator"]["row_index"], item["locator"]["column_index"])
                          for item in fact_details], [(1, 1), (2, 1)])
        self.assertTrue(all(item["source_sha256"] == version["source_sha256"]
                            for item in fact_details))

        published = service.published_verification_evidence(
            conversation["conversation_id"], formal_id)
        rejected = deepcopy(published)
        cited_receipt = next(item["verification_receipt"] for item in
                             service.answer_context(conversation["conversation_id"], formal_id)["citations"]
                             if item["kind"] == "evidence" and item["id"] == evidence_ids[0])
        event = next(item for item in rejected["receipts"]
                     if item.get("verification_receipt_id") == cited_receipt["identifier"])
        event["verification_receipt_sha256"] = "0" * 64
        original_published_reader = service.published_verification_evidence
        service.published_verification_evidence = lambda *_args: rejected
        try:
            with self.assertRaises(ChatError) as rejected_receipt:
                service.read_citation(
                    conversation["conversation_id"], formal_id, "evidence", evidence_ids[0])
            self.assertEqual(rejected_receipt.exception.code, "citation_source_mismatch")
        finally:
            service.published_verification_evidence = original_published_reader

        artifact = AnswerArtifactService(service).get(conversation["conversation_id"], formal_id)
        calculation = next(item for item in artifact["calculations"]
                           if item["calculation_id"] == provider.calculation_id)
        self.assertEqual(calculation["status"], "recorded")
        with closing(service._connect()) as connection:
            reader = json.loads(connection.execute(
                "SELECT payload_json FROM report_reader_versions WHERE message_id=?",
                (formal_id,)).fetchone()[0])
        self.assertEqual({item["node_id"] for item in reader["required_verifier_findings"]},
                         {item["node_id"] for item in artifact["required_verifier_findings"]})
        dataset = next(item for item in artifact["datasets"] if item["id"] == "report-dataset-1")
        self.assertEqual(dataset["required_verifier_findings"][0]["outcome"], "corrected_and_rechecked")
        block = next(block for chapter in reader["chapters"] for block in chapter["blocks"]
                     if block["id"] == "report-dataset-1")
        self.assertEqual(block["required_verifier_findings"][0]["outcome"], "corrected_and_rechecked")
        cells = [anchor for anchor in reader["anchors"] if anchor["kind"] == "table_cell"]
        self.assertTrue(cells)
        self.assertTrue(all(not anchor.get("required_verifier_findings") for anchor in cells))
        self.assertEqual(reader["report"]["status"], "needs_review")

    def test_right_arithmetic_with_forged_input_cannot_publish(self):
        sha = "a" * 64
        source = {"source_id": "s1", "sha256": sha, "title": "Filing", "url": "https://example.test"}
        evidence = {"evidence_id": "e1", "source_id": "s1", "source_sha256": sha,
                    "quote": "Revenue 100", "locator": {"page": 1}}
        fact = {"fact_id": "f1", "company_id": "MSFT", "metric": "revenue", "value": "100",
                "unit": "USD millions", "period_kind": "year", "basis": "reported",
                "evidence_id": "e1", "activity_id": "a1"}
        snapshot = {"data_version": {"fixture": "v1"}, "evidence": {"company_id": "MSFT",
                    "sources": [source], "evidence": [evidence], "claims": [],
                    "activities": [], "facts": [fact]}}
        forged = {"calculation_id": "c1", "schema_version": "recorded-calculation-v1",
                  "operation": "difference", "formula": "999 - 100", "value": "899",
                  "unit": "USD millions", "inputs": [{**fact, "value": "999"}, fact],
                  "evidence_ids": ["e1"], "data_version": snapshot["data_version"],
                  "company_id": "MSFT", "comparison_mode": "comparable", "limitations": []}
        publication = prepare(title="Forged numeric candidate", blocks=[{
            "classification": "recorded_calculation",
            "parts": [{"kind": "text", "text": "Difference: "},
                      {"kind": "calculation", "id": "c1",
                       "value_formation": "derived", "attribution": None}],
            "support": [], "reasoning": "", "limitations": [],
        }], refs={"calculation": {"c1": forged}}, facts={},
           calculations={"c1": forged}, max_chars=10000)

        class CalculationResearch:
            def definitions(self):
                return [{"name": "calculate_facts", "description": "controlled calculation",
                         "parameters": {"type": "object", "additionalProperties": False,
                                        "properties": {}},
                         "handler": lambda **_arguments: forged}]

        class ForgedVerifier:
            def stream_verification(self, messages, tools, cancel_event):
                packet = json.loads(messages[0]["content"])
                handlers = {tool["name"]: tool["handler"] for tool in tools}
                result = handlers["calculate_facts"]()
                receipt = result["_verification_receipt"]
                for node_id in packet["content_nodes"]:
                    handlers["record_verification_finding"](
                        version_id=packet["version_id"], node_id=node_id, check_kind="semantic_support",
                        outcome="supported", explanation="Model claims arithmetic is right.",
                        receipt_ids=[receipt["identifier"]])
                handlers["finalize_verification"](
                    version_id=packet["version_id"], outcome="verified", limitations=[],
                    semantic_review={"original_question_coverage": "Controlled assessment of the original question."}, verification={"method": "controlled numeric replay"})
                yield {"type": "usage"}

        service, conversation, submitted, saved = self.candidate_service(
            ForgedVerifier(), publication=publication, input_snapshot=snapshot,
            research_factory=lambda _packet: CalculationResearch())
        worker = DurableWorker(service, worker_id="forged-numeric", heartbeat_seconds=.1)
        self.assertTrue(worker.run_once())
        with closing(service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM verification_records").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0], 0)

    def test_invalid_finalization_is_rejected_before_missing_evidence_or_replay(self):
        case = self
        rejected = []

        class InvalidFinalizer:
            def stream_verification(self, messages, tools, cancel_event):
                packet = json.loads(messages[0]["content"])
                definition = next(tool for tool in tools if tool["name"] == "finalize_verification")
                schema = definition["parameters"]["properties"]["verification"]
                case.assertFalse(schema["additionalProperties"])
                case.assertNotIn("verification_outcome", schema["properties"])
                for changes in (
                    {"verification": {"verification_outcome": "verified"}},
                    {"verification": {"semantic_truth_guaranteed": True}},
                    {"verification": {"deterministic_calculation_limitations": []}},
                    {"limitations": "not an array"}, {"limitations": [False]},
                    {"outcome": "made-up"}, {"outcome": "verified_with_limitations"},
                ):
                    arguments = {
                        "version_id": packet["version_id"], "outcome": "verified", "limitations": [],
                        "semantic_review": {"original_question_coverage": "Controlled validation only."},
                        "verification": {}, **changes,
                    }
                    with case.assertRaises(ToolError) as caught:
                        definition["handler"](**arguments)
                    # No findings exist: reaching evidence checks would produce a different error.
                    case.assertEqual(caught.exception.code, "invalid_verification")
                    rejected.append(changes)
                yield {"type": "usage"}

        service, conversation, submitted, saved = self.candidate_service(InvalidFinalizer())
        self.assertTrue(DurableWorker(service, worker_id="invalid-finalization").run_once())
        self.assertEqual(len(rejected), 7)
        with closing(service._connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM verification_records").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0], 0)

    def test_fake_approvals_without_persisted_read_receipts_never_publish(self):
        service, conversation, submitted, saved = self.candidate_service(ControlledVerifier(skip_read=True))
        worker = DurableWorker(service, worker_id="controlled-reject", heartbeat_seconds=.1)
        self.assertTrue(worker.run_once())
        with closing(service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0], 0)
            self.assertNotEqual(connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (saved["verify_job_id"],)).fetchone()[0], "succeeded")

    def test_failed_read_alone_cannot_support_omitted_receipts_for_gap_node(self):
        class FailingResearch:
            @staticmethod
            def fail(**_arguments):
                raise ToolError("controlled_failure", "Controlled read failure.")

            def definitions(self):
                return [{"name": "search_evidence", "description": "controlled failure",
                         "parameters": {"type": "object"}, "handler": self.fail}]

        class FailureOnlyVerifier:
            def stream_verification(self, messages, tools, cancel_event):
                packet = json.loads(messages[0]["content"])
                handlers = {tool["name"]: tool["handler"] for tool in tools}
                try:
                    handlers["search_evidence"](query="must fail")
                except ToolError:
                    pass
                handlers["record_verification_finding"](
                    version_id=packet["version_id"], node_id=packet["content_nodes"][0],
                    check_kind="semantic_support", outcome="insufficient_material_disclosed",
                    explanation="A failed tool call is not evidence.")
                yield {"type": "usage", "tokens": 1}

        service, _conversation, _submitted, saved = self.candidate_service(
            FailureOnlyVerifier(), research_factory=lambda _packet: FailingResearch())
        self.assertTrue(DurableWorker(
            service, worker_id="failed-read-only", heartbeat_seconds=.1).run_once())
        with closing(service._connect()) as connection:
            receipt = json.loads(connection.execute(
                "SELECT result_json FROM verification_tool_receipts WHERE job_id=?",
                (saved["verify_job_id"],)).fetchone()[0])
            self.assertFalse(receipt["completed"])
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM verification_findings WHERE job_id=?",
                (saved["verify_job_id"],)).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM verification_records WHERE job_id=?",
                (saved["verify_job_id"],)).fetchone()[0], 0)


    def test_stale_version_and_missing_terminal_coverage_are_bounded_diagnostics(self):
        class RejectedControlsThenUnresolved:
            stale_code = None
            coverage_code = None
            coverage_message = None

            def stream_verification(inner_self, messages, tools, cancel_event):
                packet = json.loads(messages[0]["content"])
                handlers = {tool["name"]: tool["handler"] for tool in tools}
                receipt = handlers["search_evidence"](query="bounded diagnostics")
                receipt_id = receipt["_verification_receipt"]["identifier"]
                first, *remaining = packet["content_nodes"]
                try:
                    handlers["record_verification_finding"](
                        version_id="stale-version", node_id=first, check_kind="semantic_support",
                        outcome="supported", explanation="Must be rejected.",
                        receipt_ids=[receipt_id])
                except ToolError as exc:
                    inner_self.stale_code = exc.code
                handlers["record_verification_finding"](
                    version_id=packet["version_id"], node_id=first,
                    check_kind="semantic_support", outcome="insufficient_material_disclosed",
                    explanation="Current receipt was inspected.", receipt_ids=[receipt_id])
                try:
                    handlers["finalize_verification"](
                        version_id=packet["version_id"], outcome="verified_with_limitations",
                        limitations=["Coverage is incomplete."],
                        semantic_review={"original_question_coverage": "Coverage remains incomplete."},
                        verification={})
                except ToolError as exc:
                    inner_self.coverage_code = exc.code
                    inner_self.coverage_message = str(exc)
                for node_id in remaining:
                    handlers["record_verification_finding"](
                        version_id=packet["version_id"], node_id=node_id,
                        check_kind="semantic_support", outcome="unresolved",
                        explanation="No terminal semantic check was completed.", receipt_ids=[])
                handlers["finalize_verification"](
                    version_id=packet["version_id"], outcome="unresolved",
                    limitations=["Coverage is incomplete."],
                    semantic_review={"original_question_coverage": "Coverage remains incomplete."},
                    verification={})
                yield {"type": "usage", "tokens": 1}

        provider = RejectedControlsThenUnresolved()
        service, conversation, _submitted, saved = self.candidate_service(provider)
        claim = service.jobs.claim("bounded-control-rejections", job_types=("verify",))
        result = service.verification_executor.execute(claim, threading.Event())
        self.assertEqual(result["outcome"], "unresolved")
        self.assertIsNone(result["formal_message_id"])
        self.assertEqual(provider.stale_code, "stale_version")
        self.assertEqual(provider.coverage_code, "missing_terminal_findings")
        self.assertIn("report-", provider.coverage_message)
        with closing(service._connect()) as connection:
            diagnostics = [tuple(row) for row in connection.execute(
                "SELECT operation,error_code FROM verification_control_diagnostics "
                "WHERE job_id=? ORDER BY created_at,rowid", (saved["verify_job_id"],))]
            formal = connection.execute(
                "SELECT count(*) FROM messages WHERE conversation_id=? AND role='assistant'",
                (conversation["conversation_id"],)).fetchone()[0]
        self.assertEqual(diagnostics, [
            ("record_verification_finding", "stale_version"),
            ("finalize_verification", "missing_terminal_findings"),
        ])
        self.assertEqual(formal, 0)


if __name__ == "__main__":
    unittest.main()
