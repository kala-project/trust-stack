import json
import hashlib
from pathlib import Path
import subprocess
import sys
import unittest

from workbench.evidence_checks import (
    EvidenceCheckError, EvidenceCheckInput, ReceiptBackedEvent, ReferenceDescriptor,
    TrustedEvidenceAssembler, VerifiedDocumentFact, calculation_records, findings,
    projection_indexes, read_evidence_record, resolve_event_reference, resolve_source,
)


class _VerifiedFixtureSource:
    def __init__(self, snapshot, events=(), *, scope=("ACME",), document_facts=()):
        self.snapshot = snapshot
        self.scope = scope
        self.receipts = []
        for index, event in enumerate(events, 1):
            raw = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
            self.receipts.append(ReceiptBackedEvent(
                f"r:{index}", hashlib.sha256(raw).hexdigest(), raw))
        self.document_facts = document_facts

    def verified_company_scope(self, run_id, company_id): return self.scope
    def verified_snapshot_bytes(self, run_id, company_id):
        return json.dumps(self.snapshot, sort_keys=True, separators=(",", ":")).encode()
    def verified_event_receipts(self, run_id): return self.receipts
    def verified_document_facts(self, run_id, company_id): return self.document_facts


def _assemble(snapshot, events=(), *, scope=("ACME",), document_facts=()):
    return TrustedEvidenceAssembler.assemble(
        _VerifiedFixtureSource(snapshot, events, scope=scope, document_facts=document_facts),
        run_id="r", company_id="ACME")


class _NoReplay:
    def replay_arithmetic(self, *args):
        raise AssertionError("legacy comparable records do not require arithmetic replay")

    def replay_model(self, *args):
        raise AssertionError("model replay was not requested")


class EvidenceChecksTests(unittest.TestCase):
    def test_source_metadata_cannot_shadow_or_supply_an_evidence_record(self):
        fact = {"evidence_id": "e1", "fact_id": "f1", "company_id": "ACME",
                "metric": "revenue", "value": "100", "unit": "USD millions"}
        metadata = {"evidence_id": "e1", "quote": "metadata is not an evidence record",
                    "locator": {"page": 1}, "source_id": "s1"}
        for key in ("source_provenance", "historical_provenance"):
            with self.subTest(key=key):
                event = {"name": "get_candidate_reference", "result": {"record": fact, key: metadata}}
                self.assertEqual(resolve_event_reference([event], "evidence", "e1"), (event, fact))
                self.assertEqual(resolve_event_reference([
                    {"name": "get_candidate_reference", "result": {key: metadata}}], "evidence", "e1"),
                    (None, None))

    def fixture(self):
        sha = "a" * 64
        version = {"fixture": "v1"}
        source = {"source_id": "s1", "sha256": sha, "title": "Issuer filing", "url": "https://example.test"}
        evidence = {"evidence_id": "e1", "source_id": "s1", "source_sha256": sha,
                    "quote": "Revenue was USD 100 million.", "locator": {"page": 1}}
        fact = {"fact_id": "f1", "company_id": "ACME", "metric": "revenue",
                "value": "100", "unit": "USD millions", "period_kind": "year",
                "basis": "reported", "evidence_id": "e1", "activity_id": "a1"}
        snapshot = {"data_version": version, "evidence": {"company_id": "ACME",
                    "sources": [source], "evidence": [evidence], "claims": [],
                    "activities": [], "facts": [fact]}}
        calculation = {"calculation_id": "c1", "schema_version": "recorded-calculation-v1",
                       "operation": "difference", "formula": "100 - 100", "value": "0",
                       "unit": "USD millions", "inputs": [fact, fact], "evidence_ids": ["e1"],
                       "data_version": version, "company_id": "ACME",
                       "comparison_mode": "comparable", "limitations": []}
        event = {"type": "tool_result", "authoritative": True, "truncated": False,
                 "name": "calculate_facts", "result": calculation}
        return snapshot, fact, calculation, event

    def test_strict_input_rejects_malformed_and_truncated_results(self):
        snapshot, _, _, event = self.fixture()
        with self.assertRaisesRegex(EvidenceCheckError, "raw dictionaries"):
            EvidenceCheckInput.create(run_id="r", company_id="ACME", company_ids=["ACME"],
                                      snapshot=snapshot, events=[event])
        with self.assertRaisesRegex(EvidenceCheckError, "intact tool results"):
            _assemble(snapshot, [{**event, "truncated": True}])
        with self.assertRaisesRegex(EvidenceCheckError, "missing or outside"):
            _assemble({"evidence": snapshot["evidence"]})

    def test_same_binding_input_matches_artifact_compatibility_projection(self):
        from workbench.answer_artifacts import AnswerArtifactService
        text = "Revenue was USD 100 million. [evidence:e1]"
        citation_map = {("evidence", "e1"): {"valid": True}}
        sources = {("evidence", "e1"): {"quote": "Revenue was USD 100 million."}}
        direct = findings(text, citation_map, {}, sources)
        projected = AnswerArtifactService(None)._findings(text, citation_map, {}, sources)
        self.assertEqual(direct, projected)

    def test_changed_calculation_input_is_excluded(self):
        snapshot, fact, calculation, event = self.fixture()
        check_input = _assemble(snapshot, [event])
        indexes = projection_indexes(check_input)
        accepted = calculation_records(check_input, indexes, _NoReplay())
        self.assertEqual([item["calculation_id"] for item in accepted.records], ["c1"])

        changed = json.loads(json.dumps(calculation))
        changed["inputs"][0]["value"] = "999"
        changed_event = {**event, "result": changed}
        changed_input = _assemble(snapshot, [changed_event])
        rejected = calculation_records(changed_input, projection_indexes(changed_input), _NoReplay())
        self.assertEqual(rejected.records, ())
        self.assertTrue(any("冻结事实不完全一致" in value for value in rejected.limitations))

    def test_native_and_document_references_resolve_from_explicit_results(self):
        snapshot, _, _, _ = self.fixture()
        doc_fact = {"fact_id": "doc-f", "evidence_id": "doc-e", "company_id": "ACME",
                    "value": "7", "unit": "USD", "metric": "revenue"}
        doc_source = {"source_id": "doc-s", "table": {"target_cell": {"raw_value": "7"}},
                      "quote": "Revenue 7", "source_sha256": "b" * 64}
        doc_result = {"fact": doc_fact, "evidence_id": "doc-e"}
        doc_event = {"type": "tool_result", "authoritative": True, "truncated": False,
                     "name": "extract_document_table_fact", "result": doc_result}
        source = _VerifiedFixtureSource(snapshot, [doc_event])
        result_sha = hashlib.sha256(json.dumps(
            doc_result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        source.document_facts = [VerifiedDocumentFact(
            "r:1", source.receipts[0].payload_sha256, result_sha, doc_fact, doc_source)]
        check_input = TrustedEvidenceAssembler.assemble(
            source, run_id="r", company_id="ACME")
        indexes = projection_indexes(check_input)
        resolved, limitations = resolve_source("evidence", "doc-e", {"evidence_id": "doc-e"}, indexes)
        self.assertEqual(resolved["fact"], doc_fact)
        self.assertEqual(limitations, [])

        query = {"query_id": "q1", "sql": "select 7", "parameters": [],
                 "columns": ["value"], "rows": [[7]], "sources": [],
                 "numeric_semantics": "raw integer"}
        query_event = {"type": "tool_result", "authoritative": True, "truncated": False,
                       "name": "query_native", "result": query}
        event, original = resolve_event_reference([query_event], "query", "q1")
        self.assertIs(event, query_event)
        native, native_limits = resolve_source("query", "q1", original, indexes)
        self.assertEqual(native["native_query"]["sql"], "select 7")
        self.assertEqual(native_limits, ["raw integer"])

    def test_full_record_reader_preserves_actual_shapes_and_rejects_descriptors(self):
        snapshot, fact, calculation, calculation_event = self.fixture()
        document = {"document_version_id": "doc-v1", "source_sha256": "b" * 64,
                    "title": "Annual report", "parser_version": "html/1"}
        snapshot["documents"] = {"document_version_ids": ["doc-v1"],
                                 "coverage": {"documents": [document]}}
        passage = {"passage_id": "p1", "company_id": "ACME",
                   "document_version_id": "doc-v1", "source_sha256": "b" * 64,
                   "quote": "Exact natural passage.", "locator": {"start": 2, "end": 24},
                   "context": "An Exact natural passage. End", "parser_version": "html/1"}
        query = {"query_id": "q1", "sql": "select amount from provider.facts",
                 "parameters": [], "columns": ["amount"], "rows": [["100"]],
                 "sources": [{"source_id": "sql-snapshot", "sha256": "c" * 64}]}
        structured = dict(fact)
        events = [
            {"type": "tool_result", "authoritative": True, "truncated": False,
             "name": "read_passage", "result": {"reference": {"passage_id": "p1"},
                                                    "record": passage}},
            {"type": "tool_result", "authoritative": True, "truncated": False,
             "name": "get_company_facts", "result": {"company_id": "ACME", "facts": [structured]}},
            {"type": "tool_result", "authoritative": True, "truncated": False,
             "name": "query_native", "result": query},
            calculation_event,
        ]
        check_input = _assemble(snapshot, events)
        checked = calculation_records(check_input, projection_indexes(check_input), _NoReplay())
        reads = {
            kind: read_evidence_record(
                check_input, kind, identifier, calculations=checked.records,
                calculation_events=checked.event_by_id)
            for kind, identifier in (("passage", "p1"), ("evidence", "e1"),
                                     ("query", "q1"), ("calculation", "c1"))
        }
        self.assertTrue(all(item.state == "available" for item in reads.values()))
        self.assertEqual(reads["passage"].record, passage)
        self.assertEqual(reads["evidence"].record, structured)
        self.assertEqual(reads["query"].record["rows"], [["100"]])
        self.assertEqual(reads["calculation"].record["inputs"], [fact, fact])
        self.assertEqual(reads["calculation"].record["formula"], "100 - 100")
        self.assertEqual(reads["calculation"].record["value"], "0")
        detached = reads["passage"].record
        detached["quote"] = "changed"
        self.assertEqual(reads["passage"].record["quote"], "Exact natural passage.")

        descriptor_only = {"type": "tool_result", "authoritative": True, "truncated": False,
                           "name": "catalog", "result": {"calculation_id": "descriptor"}}
        descriptor_input = _assemble(snapshot, [descriptor_only])
        unavailable = read_evidence_record(descriptor_input, "calculation", "descriptor")
        self.assertEqual(unavailable.state, "incomplete")
        self.assertEqual(resolve_event_reference([descriptor_only], "calculation", "descriptor"),
                         (None, None))
        self.assertEqual(read_evidence_record(check_input, "passage", "absent").state, "missing")
        self.assertEqual(read_evidence_record(check_input, "video", "v1").state, "unsupported")
        bounded = read_evidence_record(check_input, "passage", "p1", max_record_bytes=10)
        self.assertEqual(bounded.state, "unsupported")

    def test_document_fact_receipt_is_not_a_copied_calculation_input(self):
        fact = {"fact_id": "doc-f", "evidence_id": "doc-e", "company_id": "OTHER",
                "origin": "frozen_document_table_cell-v1", "metric": "cost",
                "value": "17", "unit": "EUR"}
        calculation = {"name": "calculate_facts", "result": {"inputs": [fact]}}
        direct = {"name": "extract_document_table_fact", "result": {"fact": fact}}
        wrapped = {"name": "get_candidate_reference", "result": {
            "state": "available", "record_type": "dynamic_document_fact",
            "record": {"fact": fact}, "source_provenance": {"source_id": "doc-v"}}}
        self.assertEqual(resolve_event_reference([calculation], "evidence", "doc-e"), (None, None))
        for source in (direct, wrapped):
            event, record = resolve_event_reference([calculation, source], "evidence", "doc-e")
            self.assertIs(event, source)
            self.assertEqual(record, fact)
        wrapped["result"]["state"] = "incomplete"
        self.assertEqual(resolve_event_reference([wrapped], "evidence", "doc-e"), (None, None))

    def test_descriptor_hash_is_distinct_and_tampering_rejects(self):
        value = {"calculation_id": "c1", "company_id": "ACME", "schema_version": "v1"}
        digest = hashlib.sha256(json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        descriptor = ReferenceDescriptor.validate(
            "calculation", "c1", {"schema_version": "v1", "company_id": "ACME",
                                    "calculation_id": "c1"}, digest)
        self.assertEqual(descriptor.descriptor_sha256, digest)
        with self.assertRaisesRegex(EvidenceCheckError, "digest"):
            ReferenceDescriptor.validate("calculation", "c1", {**value, "company_id": "FORGED"}, digest)

    def test_dynamic_document_fact_read_uses_storage_validated_record(self):
        snapshot, _, _, _ = self.fixture()
        fact = {"fact_id": "doc-f", "evidence_id": "doc-e", "company_id": "ACME",
                "value": "7", "unit": "USD", "metric": "revenue"}
        result = {"fact": fact, "historical_provenance": {"schema_version": "document-table-cell-provenance/1"}}
        event = {"type": "tool_result", "authoritative": True, "truncated": False,
                 "name": "extract_document_table_fact", "result": result}
        source = _VerifiedFixtureSource(snapshot, [event])
        result_sha = hashlib.sha256(json.dumps(
            result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        provenance = {"source_id": "doc-v1", "source_sha256": "d" * 64,
                      "quote": "7", "locator": {"row_index": 1, "column_index": 2}}
        source.document_facts = [VerifiedDocumentFact(
            "r:1", source.receipts[0].payload_sha256, result_sha, fact, provenance)]
        check_input = TrustedEvidenceAssembler.assemble(source, run_id="r", company_id="ACME")
        read = read_evidence_record(check_input, "evidence", "doc-e")
        self.assertEqual((read.state, read.record_type), ("available", "dynamic_document_fact"))
        self.assertEqual(read.record, result)
        self.assertEqual(read.source_provenance, provenance)

    def test_sealed_input_ignores_original_and_detached_graph_mutation(self):
        snapshot, _, _, event = self.fixture()
        check_input = _assemble(snapshot, [event])
        expected = projection_indexes(check_input)
        snapshot["evidence"]["facts"][0]["value"] = "999"
        event["result"]["value"] = "999"
        detached_snapshot = check_input.snapshot
        detached_events = check_input.events
        detached_snapshot["evidence"]["facts"][0]["value"] = "888"
        detached_events[0]["result"]["value"] = "888"
        self.assertEqual(projection_indexes(check_input), expected)
        self.assertEqual(check_input.snapshot["evidence"]["facts"][0]["value"], "100")
        self.assertEqual(check_input.events[0]["result"]["value"], "0")

    def test_forged_flag_bad_receipt_document_mismatch_and_extra_scope_reject(self):
        snapshot, _, _, event = self.fixture()
        source = _VerifiedFixtureSource(snapshot, [event])
        source.receipts[0] = ReceiptBackedEvent(
            "r:1", "0" * 64, source.receipts[0].payload_bytes)
        with self.assertRaisesRegex(EvidenceCheckError, "payload hash"):
            TrustedEvidenceAssembler.assemble(source, run_id="r", company_id="ACME")

        forged = {"type": "tool_result", "authoritative": True, "truncated": False,
                  "name": "extract_document_table_fact",
                  "result": {"fact": {"fact_id": "f", "evidence_id": "e",
                                      "company_id": "ACME"}}}
        source = _VerifiedFixtureSource(snapshot, [forged])
        source.document_facts = [VerifiedDocumentFact(
            "r:1", source.receipts[0].payload_sha256, "f" * 64,
            {"fact_id": "other", "evidence_id": "e", "company_id": "ACME"}, {})]
        with self.assertRaisesRegex(EvidenceCheckError, "exact receipt payload"):
            TrustedEvidenceAssembler.assemble(source, run_id="r", company_id="ACME")

        with self.assertRaisesRegex(EvidenceCheckError, "subjects"):
            _assemble(snapshot, scope=("ACME", "UNTRUSTED"))

    def test_seal_digest_is_checked_at_processing_entry(self):
        snapshot, _, _, event = self.fixture()
        check_input = _assemble(snapshot, [event])
        check_input._canonical_bytes += b" "
        with self.assertRaisesRegex(EvidenceCheckError, "digest mismatch"):
            projection_indexes(check_input)

    def test_core_import_does_not_load_artifact_or_report_reader(self):
        root = Path(__file__).resolve().parents[1]
        code = (
            "import sys; import workbench.evidence_checks; "
            "assert 'workbench.answer_artifacts' not in sys.modules; "
            "assert 'workbench.report_reader' not in sys.modules"
        )
        completed = subprocess.run([sys.executable, "-c", code], cwd=root,
                                   text=True, capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
