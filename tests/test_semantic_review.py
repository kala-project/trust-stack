import json
from copy import deepcopy
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from workbench.chat import ChatService
from workbench.copilot_provider import CopilotProvider
from workbench.semantic_review import (
    aggregate_scoped_reviews, build_review_request, build_scoped_review_requests,
    compiled_publication_review_context, prepare_review_wire_packet, publication_review_scopes,
    resolve_review_ranges,
    resolve_review_wire_ranges, review_wire_packet_audit, validate_review,
)


DRAFT = "Revenue was $10 million. Profit rose in the same period."
REQUEST = {
    "draft": DRAFT,
    "authoritative_context": {
        "sources": [
            {"kind": "evidence", "id": "ev-real", "quote": "Revenue was $10 million."},
            {"kind": "passage", "id": "no-quote", "quote": None},
        ],
        "calculations": [],
    },
}


def claim(identifier, exact, status, refs):
    start = DRAFT.index(exact)
    return {
        "id": identifier, "start": start, "end": start + len(exact),
        "exact_text": exact, "support_status": status,
        "evidence_refs": refs, "reason": "Bounded review result.",
    }


def tool_claim(*args):
    return {key: value for key, value in claim(*args).items() if key not in ("start", "end")}


def compiled_publication(display="line", *, real_time=False, description_support=False):
    from workbench.report_publications import prepare
    facts = {
        "late": {"value": "20.00", "unit": "CNY", "company_id": "subject-1",
                 "metric": "revenue", "basis": "reported", "period_kind": "quarter",
                 "period_start": "2025-04-01", "period_end": "2025-06-30",
                 "evidence_id": "e-late"},
        "early": {"value": "10.00", "unit": "CNY", "company_id": "subject-1",
                  "metric": "revenue", "basis": "reported", "period_kind": "quarter",
                  "period_start": "2025-01-01", "period_end": "2025-03-31",
                  "evidence_id": "e-early"},
    }
    dataset = {"title": "真实事实趋势", "description": "受控契约数据", "display": display,
               "points": [{"label": "后期", "kind": "fact", "id": "late"},
                          {"label": "前期", "kind": "fact", "id": "early"}]}
    if real_time:
        dataset["time_axis"] = "period_end"
    if description_support:
        dataset["support"] = [{"kind": "passage", "id": "footnote"}]
    narrative = {"classification": "source_statement",
                 "parts": [{"kind": "text", "text": "说明"}], "support": [],
                 "reasoning": "", "limitations": []}
    return prepare(title="时间轴契约", blocks=[narrative],
                   refs={"evidence": {"e-late": {}, "e-early": {}}, "passage": {"footnote": {}}},
                   facts=facts, calculations={}, max_chars=10000, datasets=[dataset])


class SemanticReviewValidationTests(unittest.TestCase):
    @staticmethod
    def frozen_metadata_reviewer(base, records, identities):
        class Artifacts:
            @staticmethod
            def _event_reference(events, kind, identifier):
                original = identities.get((kind, identifier))
                return (events[0], original) if original is not None else (None, None)

            @staticmethod
            def _source_for_reference(kind, identifier, original, indexes):
                return original.get("projected_source", original), None

        return SimpleNamespace(
            question="Source timing", events=[{"result": {}}], artifacts=Artifacts(), indexes={},
            refs={"evidence": {source["id"]: {} for source in base["sources"]}},
            snapshot={"documents": {"coverage": {"documents": records}}},
            repair_request=lambda *args, **kwargs: {"allowed_context": deepcopy(base)},
        )

    def test_exact_frozen_document_metadata_survives_full_and_scoped_packets(self):
        ibm_version, crm_version = "ibm-version", "crm-version"
        records = [
            {"document_version_id": ibm_version, "title": "IBM release",
             "published_at": "2026-01-28", "collected_at": "2026-09-29T20:19:49Z"},
            {"document_version_id": crm_version, "title": "Salesforce annual report",
             "published_at": None, "collected_at": "2026-09-30T06:49:45Z"},
        ]
        base = {"sources": [
            {"kind": "evidence", "id": "ibm", "title": "IBM release", "quote": "IBM excerpt"},
            {"kind": "evidence", "id": "crm", "title": "Salesforce annual report", "quote": "CRM excerpt"},
        ], "calculations": []}
        identities = {
            ("evidence", "ibm"): {"document_version_id": ibm_version},
            ("evidence", "crm"): {"source_id": crm_version},
        }
        reviewer = self.frozen_metadata_reviewer(base, records, identities)

        full = build_review_request(reviewer, "AB")["authoritative_context"]
        self.assertEqual(full["sources"][0]["frozen_document_metadata_ref"], ibm_version)
        self.assertEqual(full["sources"][1]["frozen_document_metadata_ref"], crm_version)
        self.assertEqual(full["frozen_document_metadata_catalog"][ibm_version]["published_at"],
                         "2026-01-28")
        self.assertEqual(full["frozen_document_metadata_catalog"][ibm_version]["collected_at"],
                         "2026-09-29T20:19:49Z")
        self.assertNotIn("published_at", full["frozen_document_metadata_catalog"][crm_version])
        self.assertEqual(full["frozen_document_metadata_catalog"][crm_version]["collected_at"],
                         "2026-09-30T06:49:45Z")

        def scoped_repair(*args, scope_reference_keys, **kwargs):
            wanted = set(scope_reference_keys)
            return {"allowed_context": {"sources": [deepcopy(source) for source in base["sources"]
                    if (source["kind"], source["id"]) in wanted], "calculations": []}}
        reviewer.repair_request = scoped_repair
        requests = build_scoped_review_requests(reviewer, "AB", [
            {"id": "s1", "start": 0, "end": 1,
             "references": [{"kind": "evidence", "id": "ibm"}]},
            {"id": "s2", "start": 1, "end": 2,
             "references": [{"kind": "evidence", "id": "crm"}]},
        ])
        self.assertEqual([set(item["authoritative_context"]["frozen_document_metadata_catalog"])
                          for item in requests], [{ibm_version}, {crm_version}])
        self.assertTrue(all(len(json.dumps(item["authoritative_context"], separators=(",", ":")))
                            <= 18_000 for item in requests))

    def test_frozen_document_metadata_omits_mismatched_missing_and_ambiguous_identity(self):
        records = [
            {"document_version_id": "v1", "title": "Right title",
             "published_at": "2026-01-28", "collected_at": "2026-09-29T20:19:49Z"},
            {"document_version_id": "v2", "title": "Other title",
             "collected_at": "2026-09-30T06:49:45Z"},
        ]
        base = {"sources": [
            {"kind": "evidence", "id": "wrong-title", "title": "Wrong title", "quote": "x"},
            {"kind": "evidence", "id": "missing", "title": "Right title", "quote": "x"},
            {"kind": "evidence", "id": "ambiguous", "title": "Right title", "quote": "x"},
        ], "calculations": []}
        identities = {
            ("evidence", "wrong-title"): {"document_version_id": "v1"},
            ("evidence", "missing"): {"document_version_id": "not-in-this-snapshot"},
            ("evidence", "ambiguous"): {
                "document_version_id": "v1", "projected_source": {"source_id": "v2"}},
        }
        context = build_review_request(
            self.frozen_metadata_reviewer(base, records, identities), DRAFT)["authoritative_context"]
        self.assertNotIn("frozen_document_metadata_catalog", context)
        self.assertTrue(all("frozen_document_metadata_ref" not in source
                            for source in context["sources"]))

    def test_frozen_document_metadata_is_retained_and_counted_at_18k_boundary(self):
        from workbench.answer_review import ReviewContextInsufficient

        records = [{"document_version_id": "v1", "title": "Right title",
                    "published_at": "2026-01-28", "collected_at": "2026-09-29T20:19:49Z"}]
        identities = {("evidence", "e"): {"document_version_id": "v1"}}
        def reviewer(quote_size):
            base = {"sources": [{"kind": "evidence", "id": "e", "title": "Right title",
                                  "quote": "x" * quote_size}], "calculations": []}
            return self.frozen_metadata_reviewer(base, records, identities)

        context = build_review_request(reviewer(17_500), DRAFT)["authoritative_context"]
        self.assertEqual(context["sources"][0]["frozen_document_metadata_ref"], "v1")
        self.assertIn("v1", context["frozen_document_metadata_catalog"])
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))), 18_000)
        with self.assertRaises(ReviewContextInsufficient):
            build_review_request(reviewer(17_600), DRAFT)

    def test_pdf_review_projection_round_trips_all_common_and_target_fields(self):
        from workbench.answer_review import _compact_context
        shared = {"schema_version": "pdf-table-cell-locator/1", "physical_page": 69,
                  "unit_line": {"raw_text": "in millions", "original_words": ["exact word"] * 40},
                  "semantic_status": "unapproved", "manifest": {"sha256": "a" * 64}}
        row = {"label": "Revenue", "original_row_words": ["source word"] * 20}
        sources = [{"kind": "evidence", "id": f"e{i}", "locator": {"pdf_provenance": {
            **deepcopy(shared), "target": {"row": deepcopy(row), "cell": {"raw_text": str(i), "bbox": {"x": i}}}}}}
            for i in (1, 2)]
        original = deepcopy(sources)
        headers = [["Revenue", "FY2025", "FY2024"]]
        pdf_text = {"unit_line": "in millions", "fiscal_period_header": "Year ended January 31", "semantic_status": "unapproved"}
        facts = [{"fact_id": f"f{i}", "value": str(i), "headers": headers, "pdf_source_context": pdf_text} for i in (1, 2)]
        packed = _compact_context(sources, [], facts, 0, 0)
        for index, source in enumerate(packed["sources"]):
            pdf = source["locator"]["pdf_provenance"]
            common = deepcopy(packed["pdf_locator_common_catalog"][pdf["common_ref"]])
            target = deepcopy(pdf["target"])
            target["row"] = packed["pdf_row_catalog"][target.pop("row_ref")]
            self.assertEqual({**common, "target": target}, original[index]["locator"]["pdf_provenance"])
        for fact in packed["calculation_facts"]:
            self.assertEqual(packed["table_header_catalog"][fact["headers_ref"]], headers)
            self.assertEqual(packed["pdf_source_context_catalog"][fact["pdf_source_context_ref"]], pdf_text)
        self.assertEqual(sources, original)

    def test_small_structured_dataset_splits_by_exact_saved_binding_sequence(self):
        refs = [{"kind": "evidence", "id": f"e{i}"} for i in range(4)]
        spans = [{"start": i, "end": i + 1, "text": str(i), "binding": {"reference": ref}}
                 for i, ref in enumerate(refs)]
        publication = {"answer": "0123", "blocks": [{"start": 0, "end": 4, "text": "0123", "spans": spans, "support": refs}],
                       "datasets": [{"points": [{"binding": deepcopy(span["binding"])} for span in spans]}]}
        scopes = publication_review_scopes(publication)
        self.assertEqual([(s["start"], s["end"]) for s in scopes],
                         [(0, 1), (1, 2), (2, 3), (3, 4)])
        self.assertEqual([scope["references"] for scope in scopes], [[ref] for ref in refs])
        publication["datasets"][0]["points"][0]["binding"]["reference"]["id"] = "wrong"
        with self.assertRaisesRegex(ValueError, "binding sequence differs"):
            publication_review_scopes(publication)

    def test_compiled_real_time_contract_survives_full_and_scoped_packets(self):
        publication = compiled_publication(real_time=True)
        scopes = publication_review_scopes(publication)
        base = {"sources": [
            {"kind": "evidence", "id": "e-late", "quote": "20"},
            {"kind": "evidence", "id": "e-early", "quote": "10"},
        ], "calculations": []}
        reviewer = SimpleNamespace(
            question="季度收入趋势", events=[],
            repair_request=lambda *args, **kwargs: {"allowed_context": deepcopy(base)})

        full = build_review_request(
            reviewer, publication["answer"], publication=publication,
            review_scopes=scopes)["authoritative_context"]
        contract = full["compiled_presentation_contracts"][0]
        self.assertEqual(contract["type"], "line")
        self.assertEqual(contract["chart"]["time_axis"], {
            "version": "utc-period-axis/1", "field": "period_end",
            "timezone": "UTC", "gap_policy": "break"})
        self.assertTrue(contract["chart"]["real_time_axis"])
        self.assertEqual([point["x"] for point in contract["points"]],
                         ["2025-06-30", "2025-03-31"])
        self.assertTrue(all(point["mapping"]["x"] == "binding.dimensions.period_end"
                            and point["citation_eligible"] is False
                            for point in contract["points"]))
        self.assertFalse(contract["financial_evidence"])
        self.assertFalse(contract["browser_rendering_verified"])
        self.assertFalse(contract["visual_acceptance_verified"])

        requests = build_scoped_review_requests(
            reviewer, publication["answer"], scopes, publication=publication)
        by_scope = {request["review_scope"]["id"]: request["authoritative_context"]
                    for request in requests}
        for scope in scopes:
            has_contract = "compiled_presentation_contracts" in by_scope[scope["id"]]
            self.assertEqual(has_contract, scope["id"] in contract["review_scope_ids"])
            inventory = by_scope[scope["id"]]["compiled_presentation_index"]
            self.assertEqual(inventory, full["compiled_presentation_index"])
            self.assertEqual(inventory[0]["point_count"], 2)
            self.assertTrue(inventory[0]["chart"]["real_time_axis"])
            self.assertFalse(inventory[0]["financial_evidence"])
            self.assertFalse(inventory[0]["browser_rendering_verified"])
            self.assertNotIn("points", inventory[0])

    def test_dataset_description_footnote_reaches_its_review_scope(self):
        publication = compiled_publication(real_time=True, description_support=True)
        scopes = publication_review_scopes(publication)
        base = {"sources": [
            {"kind": "evidence", "id": "e-late", "quote": "20"},
            {"kind": "evidence", "id": "e-early", "quote": "10"},
            {"kind": "passage", "id": "footnote", "quote": "Controlled recast footnote"},
        ], "calculations": []}
        reviewer = SimpleNamespace(question="季度收入趋势", events=[],
            repair_request=lambda *args, **kwargs: {"allowed_context": deepcopy(base)})
        requests = build_scoped_review_requests(reviewer, publication["answer"], scopes,
                                                publication=publication)
        description_start = publication["answer"].index("受控契约数据")
        description_request = next(request for request in requests
            if request["review_scope"]["start"] <= description_start < request["review_scope"]["end"])
        self.assertTrue(any(source.get("id") == "footnote" and source["quote"] == "Controlled recast footnote"
                            for source in description_request["authoritative_context"]["sources"]))
        publication["datasets"][0]["support"] = [{"kind": "passage", "id": "invented"}]
        with self.assertRaisesRegex(ValueError, "support differs"):
            compiled_publication_review_context(publication, publication_review_scopes(publication))

    def test_table_and_legacy_line_never_gain_a_real_time_axis(self):
        table = compiled_publication(display="table")
        table_contract = compiled_publication_review_context(
            table, publication_review_scopes(table))["contracts"][0]
        self.assertIsNone(table_contract["chart"])

        legacy_line = compiled_publication(display="line")
        line_contract = compiled_publication_review_context(
            legacy_line, publication_review_scopes(legacy_line))["contracts"][0]
        self.assertEqual(line_contract["chart"]["x"], "label")
        self.assertFalse(line_contract["chart"]["real_time_axis"])
        self.assertNotIn("time_axis", line_contract["chart"])

    def test_compiled_contract_rejects_scope_draft_and_forged_axis_mismatches(self):
        publication = compiled_publication(real_time=True)
        scopes = publication_review_scopes(publication)
        wrong_scopes = deepcopy(scopes)
        wrong_scopes[0]["end"] += 1
        with self.assertRaisesRegex(ValueError, "does not match"):
            compiled_publication_review_context(publication, wrong_scopes)

        forged = deepcopy(publication)
        forged["datasets"][0]["time_axis"]["version"] = "invented/9"
        with self.assertRaisesRegex(ValueError, "unrecognized time axis"):
            compiled_publication_review_context(forged, publication_review_scopes(forged))

        reviewer = SimpleNamespace(question="q", events=[], repair_request=lambda *args, **kwargs: {
            "allowed_context": {"sources": [], "calculations": []}})
        with self.assertRaisesRegex(ValueError, "differs from the review draft"):
            build_review_request(reviewer, publication["answer"] + "x", publication=publication)

    def test_complete_compiled_contract_counts_against_18k_without_trimming_evidence(self):
        from workbench.answer_review import ReviewContextInsufficient
        publication = compiled_publication(real_time=True)
        source = {"kind": "evidence", "id": "e-late", "quote": "x" * 17_000}
        reviewer = SimpleNamespace(question="q", events=[], repair_request=lambda *args, **kwargs: {
            "allowed_context": {"sources": [deepcopy(source)], "calculations": []}})
        plain = build_review_request(reviewer, publication["answer"])["authoritative_context"]
        self.assertEqual(plain["sources"][0]["quote"], source["quote"])
        with self.assertRaises(ReviewContextInsufficient):
            build_review_request(reviewer, publication["answer"], publication=publication,
                                 review_scopes=publication_review_scopes(publication))

    def test_review_preserves_exact_table_labels_and_deduplicated_calculation_inputs(self):
        from workbench.answer_review import AnswerReview
        source = {"kind": "evidence", "id": "cell", "quote": "$10", "table": {
            "rows": [["FY", "Revenue"], ["2025", "$10"]],
            "target_cell": {"row_index": 1, "column_index": 1, "row_label": "2025",
                            "headers": [["FY", "Revenue"]], "column_headers": ["Revenue"], "raw_value": "$10"},
        }, "interpretation": {"semantic_verified": False, "state": "explicit_interpretation"}}
        projection = {**source, **AnswerReview._source_context(source)}
        facts = [{"fact_id": "fact", "value": "10", "metric": "revenue", "period_end": "2025-06-30"}]
        calc = {"calculation_id": "calc", "value": "100.00", "formula": "a / a * 100", "input_fact_ids": ["fact", "fact"]}
        reviewer = SimpleNamespace(question="Revenue", events=[], repair_request=lambda *args: {
            "allowed_context": {"sources": [projection], "calculations": [calc], "calculation_facts": facts}})
        context = build_review_request(reviewer, DRAFT)["authoritative_context"]
        self.assertEqual(context["sources"][0]["table_context"]["selected_row"], ["2025", "$10"])
        self.assertEqual(context["sources"][0]["table_context"]["column_headers"], ["Revenue"])
        self.assertFalse(context["sources"][0]["table_context"]["interpretation"]["semantic_verified"])
        self.assertEqual(context["calculation_facts"], facts)
        self.assertEqual(context["calculations"][0]["input_fact_ids"], ["fact", "fact"])

    def test_large_report_history_never_displaces_current_financial_evidence(self):
        import json
        from workbench.semantic_review import MAX_CONTEXT_CHARS
        source = {"kind": "evidence", "id": "current-table", "quote": "x" * 15500,
                  "table_context": {"headers": [["2025", "Revenue"]], "selected_row": ["2025", "10"]}}
        facts = [{"fact_id": "input", "value": "10"}]
        calculations = [{"calculation_id": "current-calc", "input_fact_ids": ["input"]}]
        reviewer = SimpleNamespace(question="Current revenue", events=[
            {"name": "read_report_version", "result": {"version": index, "content": "old" * 1000}}
            for index in range(4)], repair_request=lambda *args: {"allowed_context": {
                "sources": [source], "calculations": calculations, "calculation_facts": facts}})
        context = build_review_request(reviewer, DRAFT)["authoritative_context"]
        self.assertEqual([item["id"] for item in context["sources"]], ["current-table"])
        self.assertEqual(context["calculations"], calculations)
        self.assertEqual(context["calculation_facts"], facts)
        self.assertEqual(context["omitted_source_count"], 0)
        self.assertGreater(context["omitted_history_count"], 0)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))), MAX_CONTEXT_CHARS)

    def test_semantic_stage_preserves_compact_catalogs_and_prompt_identity(self):
        base = {
            "sources": [{"kind": "passage", "id": "cited", "quote": "Exact issuer passage.",
                         "table_context": {"headers_ref": "H1", "selected_row_ref": "R1"}}],
            "calculations": [{"calculation_id": "root", "formula": "10 + 20", "value": "30",
                              "operands": [{"kind": "calculation", "reference": "ancestor"}]}],
            "calculation_facts": [{"fact_id": "f1", "value": "10", "unit": "USDm"}],
            "calculation_fact_aliases": {"f2": "f1"},
            "table_header_catalog": {"H1": [["Segment", "Revenue"]]},
            "table_row_catalog": {"R1": ["Software", "$7,390"]},
            "limitation_catalog": {"L1": ["Unverified interpretation remains unverified."]},
            "omitted_source_count": 7, "omitted_calculation_count": 12,
        }
        reviewer = SimpleNamespace(question="IBM segments", events=[], repair_request=lambda *args: {
            "allowed_context": base})
        request = build_review_request(reviewer, DRAFT)
        context = request["authoritative_context"]
        self.assertEqual(request["prompt_version"], "semantic-answer-review-prompt/12")
        self.assertEqual(context["table_header_catalog"], base["table_header_catalog"])
        self.assertEqual(context["table_row_catalog"], base["table_row_catalog"])
        self.assertEqual(context["calculation_fact_aliases"], {"f2": "f1"})
        self.assertEqual(context["calculations"][0]["operands"][0]["reference"], "ancestor")
        self.assertEqual(context["omitted_calculation_count"], 12)

    def test_semantic_stage_fails_when_essential_base_leaves_no_room(self):
        from workbench.answer_review import ReviewContextInsufficient

        base = {"sources": [{"kind": "passage", "id": "essential", "quote": "x" * 17_900}],
                "calculations": [], "calculation_facts": []}
        reviewer = SimpleNamespace(question="Oversized", events=[], repair_request=lambda *args: {
            "allowed_context": base})
        with self.assertRaises(ReviewContextInsufficient):
            build_review_request(reviewer, DRAFT)

    def test_report_history_is_available_but_never_registered_as_financial_proof(self):
        reviewer = SimpleNamespace(
            question="Correct the prior report",
            repair_request=lambda *args: {"allowed_context": {"sources": [], "calculations": []}},
            events=[
                {"name": "read_report_version", "result": {
                    "version": 1, "message_id": "msg-old", "run_id": "run-old",
                    "content": "Old wrong value."}},
                {"name": "record_report_correction", "result": {
                    "status": "recorded", "correction_id": "record",
                    "publication": "Visible only when this run publishes a completed report; not independent verification."},
                 "observed_arguments": {
                     "version": 1, "original_text": "Old wrong value.",
                     "explanation": "The old statement exceeded the retrieved material.",
                     "impact": "The conclusion must be narrowed.",
                     "evidence_refs": [{"kind": "passage", "id": "current-proof"}]}},
            ],
        )
        request = build_review_request(reviewer, DRAFT)
        history = request["authoritative_context"]["report_history"]
        self.assertEqual([item["kind"] for item in history], ["prior_report", "recorded_correction"])
        self.assertTrue(all(item["financial_evidence"] is False for item in history))
        self.assertEqual((history[0]["message_id"], history[0]["run_id"]), ("msg-old", "run-old"))
        self.assertEqual(history[1]["publication"], "pending_completed_report")
        self.assertEqual(history[1]["procedural_status"], "recorded")
        self.assertEqual(history[1]["explanation"], "The old statement exceeded the retrieved material.")
        self.assertEqual(history[1]["impact"], "The conclusion must be narrowed.")
        self.assertEqual(history[1]["evidence_refs"], [{"kind": "passage", "id": "current-proof"}])
        forged = {"claims": [claim("c1", "Revenue was $10 million.", "supported_by_evidence",
                                   [{"kind": "evidence", "id": "record"}])],
                  "coverage": {"status": "answered", "specific_gaps": []}}
        with self.assertRaisesRegex(ValueError, "not returned"):
            validate_review(forged, request)

    def test_quote_resolution_uses_exact_unicode_offsets(self):
        text = "收入增长。"
        request = {**REQUEST, "draft": "前言😀\n" + text}
        value = {"claims": [{
            "id": "unicode", "exact_text": text, "support_status": "unsupported",
            "evidence_refs": [], "reason": "No evidence for this claim.",
        }], "coverage": {"status": "answered", "specific_gaps": []}}
        resolved = resolve_review_ranges(value, request)
        self.assertEqual((resolved["claims"][0]["start"], resolved["claims"][0]["end"]), (4, 9))
        self.assertEqual(validate_review(resolved, request)["status"], "assessed")
        self.assertNotIn("start", value["claims"][0])

    def test_duplicate_quote_requires_explicit_valid_occurrence(self):
        value = {"claims": [tool_claim("c1", "Revenue was $10 million.", "unsupported", [])],
                 "coverage": {"status": "answered", "specific_gaps": []}}
        request = {**REQUEST, "draft": DRAFT + "\n" + DRAFT}
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            resolve_review_ranges(value, request)
        for occurrence in (0, 3, True, "2", None):
            with self.subTest(occurrence=occurrence), self.assertRaises(ValueError):
                resolve_review_ranges({**value, "claims": [{**value["claims"][0], "occurrence": occurrence}]}, request)
        resolved = resolve_review_ranges({
            **value, "claims": [{**value["claims"][0], "occurrence": 2}]}, request)
        self.assertEqual(resolved["claims"][0]["start"], len(DRAFT) + 1)
        self.assertEqual(validate_review(resolved, request)["status"], "assessed")

    def test_quote_resolution_rejects_fuzzy_text_and_model_offsets(self):
        value = {"claims": [tool_claim("c1", "Revenue was $10 million.", "unsupported", [])],
                 "coverage": {"status": "answered", "specific_gaps": []}}
        for changed in (
            {**value["claims"][0], "exact_text": "Revenue was $11 million."},
            {**value["claims"][0], "start": 0, "end": 24},
        ):
            with self.assertRaises(ValueError):
                resolve_review_ranges({**value, "claims": [changed]}, REQUEST)

    def test_full_bounded_claim_set_fits_review_output_budget(self):
        value = {
            "claims": [claim(f"c{index}", "Revenue was $10 million.", "supported_by_evidence",
                             [{"kind": "evidence", "id": "ev-real"}]) for index in range(12)],
            "coverage": {"status": "answered", "specific_gaps": []},
        }
        self.assertEqual(len(validate_review(value, REQUEST)["claims"]), 12)

    def test_rejects_forged_reference(self):
        value = {
            "claims": [claim("c1", "Revenue was $10 million.", "supported_by_evidence",
                             [{"kind": "evidence", "id": "forged"}])],
            "coverage": {"status": "answered", "specific_gaps": []},
        }
        with self.assertRaisesRegex(ValueError, "not returned"):
            validate_review(value, REQUEST)

    def test_rejects_wrong_range_even_when_text_occurs_elsewhere(self):
        item = claim("c1", "Revenue was $10 million.", "unsupported", [])
        item["start"] += 1
        item["end"] += 1
        value = {"claims": [item],
                 "coverage": {"status": "answered", "specific_gaps": []}}
        with self.assertRaisesRegex(ValueError, "exactly match"):
            validate_review(value, REQUEST)

    def test_no_quote_cannot_be_accepted_as_evidence_support(self):
        value = {
            "claims": [claim("c1", "Profit rose in the same period.", "supported_by_evidence",
                             [{"kind": "passage", "id": "no-quote"}])],
            "coverage": {"status": "answered", "specific_gaps": []},
        }
        with self.assertRaisesRegex(ValueError, "lacks exact"):
            validate_review(value, REQUEST)

    def test_publication_scopes_are_exact_complete_and_use_declared_support(self):
        draft = "# Title\n\nFirst [evidence:a]\n\nSecond [evidence:b]"
        second = draft.index("Second")
        publication = {"answer": draft, "blocks": [
            {"start": draft.index("First"), "end": second - 2,
             "text": "First [evidence:a]", "support": [{"kind": "evidence", "id": "a"}]},
            {"start": second, "end": len(draft), "text": draft[second:],
             "support": [{"kind": "evidence", "id": "b"}]},
        ]}
        scopes = publication_review_scopes(publication)
        self.assertEqual([(item["start"], item["end"]) for item in scopes],
                         [(0, second), (second, len(draft))])
        self.assertEqual("".join(draft[item["start"]:item["end"]] for item in scopes), draft)
        self.assertEqual(scopes[0]["references"], [{"kind": "evidence", "id": "a"}])

    def test_large_structured_block_splits_only_on_typed_binding_spans(self):
        parts, support, answer = [], [], ""
        for index in range(15):
            prefix = f"row {index}: "
            start = len(answer)
            answer += prefix
            parts.append({"start": start, "end": len(answer), "text": prefix, "binding": None})
            ref = {"kind": "evidence", "id": f"e{index}"}
            start = len(answer)
            answer += str(index)
            parts.append({"start": start, "end": len(answer), "text": str(index),
                          "binding": {"reference": ref}})
            support.append(ref)
        publication = {"answer": answer, "blocks": [{"start": 0, "end": len(answer),
                        "text": answer, "support": support, "spans": parts}]}
        scopes = publication_review_scopes(publication)
        self.assertEqual(len(scopes), 15)
        self.assertTrue(all(len(item["references"]) == 1 for item in scopes))
        self.assertEqual("".join(answer[item["start"]:item["end"]] for item in scopes), answer)
        self.assertEqual([ref["id"] for ref in scopes[1]["references"]], ["e1"])
        hostile = {"answer": answer, "blocks": [{"start": 0, "end": len(answer),
                   "text": answer, "support": support}]}
        with self.assertRaisesRegex(ValueError, "explicit part spans"):
            publication_review_scopes(hostile)

    def test_four_typed_prose_bindings_split_despite_small_reference_count(self):
        answer, spans, support = "", [], []
        extra = {"kind": "passage", "id": "shared-methodology"}
        support.append(extra)
        for index in range(4):
            prefix = f"value {index}: "
            start = len(answer)
            answer += prefix
            spans.append({"start": start, "end": len(answer), "text": prefix,
                          "binding": None})
            ref = {"kind": "evidence", "id": f"pdf-cell-{index}"}
            start = len(answer)
            answer += str(index)
            spans.append({"start": start, "end": len(answer), "text": str(index),
                          "binding": {"reference": ref}})
            support.append(ref)
        publication = {"answer": answer, "blocks": [{"start": 0, "end": len(answer),
                       "text": answer, "support": support, "spans": spans}]}

        scopes = publication_review_scopes(publication)

        self.assertEqual(len(scopes), 4)
        self.assertEqual([[ref["id"] for ref in scope["references"]] for scope in scopes], [
            ["shared-methodology", f"pdf-cell-{index}"] for index in range(4)
        ])
        self.assertEqual("".join(answer[item["start"]:item["end"]] for item in scopes), answer)
        self.assertEqual(scopes[0]["end"], spans[2]["start"])

    def test_adjacent_bindings_keep_value_and_citation_in_own_scope(self):
        answer, spans, support = "", [], []
        for index in range(15):
            ref = {"kind": "evidence", "id": f"e{index}"}
            start = len(answer)
            text = str(100 + index)
            answer += text
            spans.append({"start": start, "end": len(answer), "text": text,
                          "binding": {"reference": ref}})
            answer += f" [evidence:e{index}]"
            support.append(ref)
        publication = {"answer": answer, "blocks": [{"start": 0, "end": len(answer),
                       "text": answer, "support": support, "spans": spans}]}
        scopes = publication_review_scopes(publication)
        for index, span in enumerate(spans):
            owner = next(scope for scope in scopes
                         if scope["start"] <= span["start"] < scope["end"])
            self.assertIn(support[index], owner["references"])
            citation_end = spans[index + 1]["start"] if index + 1 < len(spans) else len(answer)
            self.assertLessEqual(citation_end, owner["end"])

    def test_scoped_requests_isolate_references_and_retain_full_draft_for_offsets(self):
        draft = "Alpha claim. Beta claim."
        calls = []
        contexts = {
            ("evidence", "a"): {"kind": "evidence", "id": "a", "quote": "Alpha claim."},
            ("evidence", "b"): {"kind": "evidence", "id": "b", "quote": "Beta claim."},
        }
        def repair(text, review, *, scope_reference_keys):
            calls.append(scope_reference_keys)
            return {"allowed_context": {"sources": [contexts[key] for key in scope_reference_keys],
                                         "calculations": [], "calculation_facts": []}}
        reviewer = SimpleNamespace(question="Both?", repair_request=repair)
        scopes = [
            {"id": "s1", "start": 0, "end": 13,
             "references": [{"kind": "evidence", "id": "a"}]},
            {"id": "s2", "start": 13, "end": len(draft),
             "references": [{"kind": "evidence", "id": "b"}]},
        ]
        requests = build_scoped_review_requests(reviewer, draft, scopes)
        self.assertEqual(calls, [[("evidence", "a")], [("evidence", "b")]])
        self.assertTrue(all(item["draft"] == draft for item in requests))
        self.assertEqual([item["authoritative_context"]["sources"][0]["id"] for item in requests], ["a", "b"])
        self.assertTrue(all(item["authoritative_context"]["report_history"] == [] for item in requests))
        self.assertTrue(all(item["authoritative_context"]["omitted_history_count"] == 0 for item in requests))

    def test_scoped_requests_include_old_report_and_two_complete_recorded_corrections(self):
        draft = "AB"
        scopes = [
            {"id": "s1", "start": 0, "end": 1,
             "references": [{"kind": "evidence", "id": "a"}]},
            {"id": "s2", "start": 1, "end": 2,
             "references": [{"kind": "evidence", "id": "b"}]},
        ]
        events = [{"name": "read_report_version", "result": {
            "version": 2, "message_id": "report_test_previous", "run_id": "run-old",
            "content": "The complete prior report text."}}]
        for index in (1, 2):
            events.append({
                "name": "record_report_correction",
                "result": {"status": "recorded", "correction_id": f"correction-{index}",
                           "publication": "Visible only when this run publishes a completed report; not independent verification."},
                "observed_arguments": {
                    "version": 2, "original_text": f"Original sentence {index}.",
                    "explanation": f"Explanation {index} preserves the evidence boundary.",
                    "impact": f"Impact {index} narrows the conclusion.",
                    "evidence_refs": [{"kind": "passage", "id": f"passage-{index}"}],
                },
            })
        def repair(text, review, *, scope_reference_keys):
            return {"allowed_context": {"sources": [
                {"kind": kind, "id": identifier, "quote": identifier}
                for kind, identifier in scope_reference_keys], "calculations": []}}
        reviewer = SimpleNamespace(question="Corrections?", events=events, repair_request=repair)
        requests = build_scoped_review_requests(reviewer, draft, scopes)
        for request in requests:
            history = request["authoritative_context"]["report_history"]
            self.assertEqual([item["kind"] for item in history],
                             ["prior_report", "recorded_correction", "recorded_correction"])
            self.assertEqual(history[0]["message_id"], "report_test_previous")
            self.assertEqual([item["correction_id"] for item in history[1:]],
                             ["correction-1", "correction-2"])
            self.assertEqual(history[2]["explanation"],
                             "Explanation 2 preserves the evidence boundary.")
            self.assertEqual(history[2]["impact"], "Impact 2 narrows the conclusion.")
            self.assertEqual(history[2]["evidence_refs"],
                             [{"kind": "passage", "id": "passage-2"}])
            self.assertTrue(all(item["financial_evidence"] is False for item in history))
            self.assertEqual(request["authoritative_context"]["omitted_history_count"], 0)

    def test_unsplittable_atomic_scope_names_its_evidence_source(self):
        from workbench.answer_review import ReviewContextInsufficient

        def repair(text, review, *, scope_reference_keys):
            raise ReviewContextInsufficient("directly cited excerpt exceeds budget")

        with self.assertRaisesRegex(
                ReviewContextInsufficient,
                r"scope s1 references \[evidence:pdf-cell-heavy\].*directly cited excerpt"):
            build_scoped_review_requests(
                SimpleNamespace(question="One value?", repair_request=repair), "x", [{
                    "id": "s1", "start": 0, "end": 1,
                    "references": [{"kind": "evidence", "id": "pdf-cell-heavy"}],
                }])

    def test_adjacent_scopes_merge_only_when_complete_evidence_fits(self):
        draft = "ABC"
        scopes = [{"id": f"s{i+1}", "start": i, "end": i+1,
                   "references": [{"kind": "evidence", "id": letter}]}
                  for i, letter in enumerate(draft)]
        def repair(text, review, *, scope_reference_keys):
            return {"allowed_context": {"sources": [
                {"kind": kind, "id": identifier, "quote": identifier * (16_000 if identifier == "C" else 1_000)}
                for kind, identifier in scope_reference_keys], "calculations": []}}
        requests = build_scoped_review_requests(
            SimpleNamespace(question="all?", repair_request=repair), draft, scopes, merge_adjacent=True)
        self.assertEqual([(r["review_scope"]["start"], r["review_scope"]["end"]) for r in requests], [(0, 2), (2, 3)])
        self.assertEqual([s["id"] for s in requests[0]["authoritative_context"]["sources"]], ["A", "B"])
        self.assertEqual([s["id"] for s in requests[1]["authoritative_context"]["sources"]], ["C"])
        self.assertTrue(all(len(json.dumps(r["authoritative_context"], separators=(",", ":"))) <= 18000 for r in requests))

    def test_group_merge_budget_includes_recorded_correction_history(self):
        draft = "AB"
        scopes = [
            {"id": "s1", "start": 0, "end": 1,
             "references": [{"kind": "evidence", "id": "a"}]},
            {"id": "s2", "start": 1, "end": 2,
             "references": [{"kind": "evidence", "id": "b"}]},
        ]
        events = [{
            "name": "record_report_correction",
            "result": {"status": "recorded", "correction_id": "correction-large"},
            "observed_arguments": {
                "version": 2, "original_text": "o" * 1000,
                "explanation": "e" * 1000, "impact": "i" * 1000,
                "evidence_refs": [{"kind": "passage", "id": "history-proof"}],
            },
        }]
        def repair(text, review, *, scope_reference_keys):
            return {"allowed_context": {"sources": [
                {"kind": kind, "id": identifier, "quote": identifier * 7500}
                for kind, identifier in scope_reference_keys], "calculations": []}}
        requests = build_scoped_review_requests(
            SimpleNamespace(question="All?", events=events, repair_request=repair),
            draft, scopes, merge_adjacent=True)
        self.assertEqual(len(requests), 2)
        self.assertEqual([request["review_scope"]["id"] for request in requests], ["s1", "s2"])
        self.assertTrue(all(request["authoritative_context"]["report_history"][0]["correction_id"]
                            == "correction-large" for request in requests))
        self.assertTrue(all(len(json.dumps(request["authoritative_context"], separators=(",", ":")))
                            <= 18_000 for request in requests))

    def test_supplement_overflow_falls_back_to_typed_atomic_scopes_without_dropping_metadata(self):
        from workbench.answer_review import ReviewContextInsufficient

        records = [
            {"document_version_id": "va", "title": "Source A",
             "published_at": "2026-01-01", "collected_at": "2026-09-29T00:00:00Z"},
            {"document_version_id": "vb", "title": "Source B",
             "published_at": "2026-02-01", "collected_at": "2026-09-30T00:00:00Z"},
        ]
        identities = {
            ("evidence", "a"): {"document_version_id": "va"},
            ("evidence", "b"): {"document_version_id": "vb"},
        }
        sources = [
            {"kind": "evidence", "id": "a", "title": "Source A", "quote": "a" * 7200},
            {"kind": "evidence", "id": "b", "title": "Source B", "quote": "b" * 7200},
        ]
        reviewer = self.frozen_metadata_reviewer(
            {"sources": sources, "calculations": []}, records, identities)
        reviewer.events = [
            {"name": "period_record", "result": {
                "period_start": "2025-01-01", "period_end": "2025-12-31",
                "period_kind": "fiscal_year", "metric": "revenue", "unit": "USDm"}},
            {"name": "record_report_correction",
             "result": {"status": "recorded", "correction_id": "required-correction"},
             "observed_arguments": {
                 "version": 2, "original_text": "o" * 1200,
                 "explanation": "e" * 1200, "impact": "i" * 1200,
                 "evidence_refs": [{"kind": "evidence", "id": "a"}],
             }},
        ]

        def repair(*args, scope_reference_keys=None, **kwargs):
            wanted = set(scope_reference_keys) if scope_reference_keys is not None else None
            return {"allowed_context": {
                "sources": [deepcopy(source) for source in sources
                            if wanted is None or (source["kind"], source["id"]) in wanted],
                "calculations": [],
            }}
        reviewer.repair_request = repair

        with self.assertRaisesRegex(ReviewContextInsufficient, "recorded correction history"):
            build_review_request(reviewer, "AB")
        requests = build_scoped_review_requests(reviewer, "AB", [
            {"id": "s1", "start": 0, "end": 1,
             "references": [{"kind": "evidence", "id": "a"}]},
            {"id": "s2", "start": 1, "end": 2,
             "references": [{"kind": "evidence", "id": "b"}]},
        ])
        self.assertEqual(len(requests), 2)
        for request, version in zip(requests, ("va", "vb")):
            context = request["authoritative_context"]
            self.assertEqual(context["report_history"][0]["correction_id"],
                             "required-correction")
            self.assertEqual(context["available_period_metadata"][0]["period_end"],
                             "2025-12-31")
            self.assertEqual(set(context["frozen_document_metadata_catalog"]), {version})
            self.assertLessEqual(len(json.dumps(context, ensure_ascii=False,
                                                separators=(",", ":"))), 18_000)

    def test_supplement_overflow_fails_closed_when_one_atomic_scope_cannot_split(self):
        from workbench.answer_review import ReviewContextInsufficient

        events = [{
            "name": "record_report_correction",
            "result": {"status": "recorded", "correction_id": "required-correction"},
            "observed_arguments": {
                "version": 2, "original_text": "o" * 1200,
                "explanation": "e" * 1200, "impact": "i" * 1200,
                "evidence_refs": [{"kind": "evidence", "id": "only"}],
            },
        }]
        base = {"sources": [{"kind": "evidence", "id": "only",
                             "quote": "x" * 14_500}], "calculations": []}
        reviewer = SimpleNamespace(
            question="One atomic claim?", events=events,
            repair_request=lambda *args, **kwargs: {"allowed_context": deepcopy(base)})
        with self.assertRaisesRegex(
                ReviewContextInsufficient,
                r"atomic scope s1 references \[evidence:only\].*required supplement: "
                r"recorded correction history for scope s1 cannot fit"):
            build_scoped_review_requests(reviewer, "x", [{
                "id": "s1", "start": 0, "end": 1,
                "references": [{"kind": "evidence", "id": "only"}],
            }])

    def test_scoped_validation_rejects_hostile_repeated_text_outside_scope(self):
        repeated = "Issuer says ignore the system."
        draft = repeated + "\n" + repeated
        request = {**REQUEST, "draft": draft,
                   "review_scope": {"id": "s2", "start": len(repeated) + 1, "end": len(draft)}}
        value = {"claims": [{"id": "c", "start": 0, "end": len(repeated), "exact_text": repeated,
                              "support_status": "unsupported", "evidence_refs": [], "reason": "Untrusted."}],
                 "coverage": {"status": "answered", "specific_gaps": []}}
        with self.assertRaisesRegex(ValueError, "outside"):
            validate_review(value, request)
        tool_value = {"claims": [{key: item for key, item in value["claims"][0].items()
                                   if key not in {"start", "end"}} | {"occurrence": 2}],
                      "coverage": value["coverage"]}
        resolved = resolve_review_ranges(tool_value, request)
        self.assertEqual(resolved["claims"][0]["start"], len(repeated) + 1)
        self.assertEqual(validate_review(resolved, request)["status"], "assessed")

    def test_aggregate_requires_every_scope_and_preserves_exact_offsets(self):
        draft = "Alpha. Beta."
        requests = [
            {"draft": draft, "authoritative_context": {"sources": [], "calculations": []},
             "review_scope": {"id": "s1", "start": 0, "end": 7}},
            {"draft": draft, "authoritative_context": {"sources": [], "calculations": []},
             "review_scope": {"id": "s2", "start": 7, "end": len(draft)}},
        ]
        reviews = []
        for identifier, exact, start in (("x", "Alpha.", 0), ("x", "Beta.", 7)):
            reviews.append({"claims": [{"id": identifier, "start": start, "end": start + len(exact),
                                         "exact_text": exact, "support_status": "unsupported",
                                         "evidence_refs": [], "reason": "No scoped evidence."}],
                            "coverage": {"status": "answered", "specific_gaps": []}})
        with self.assertRaisesRegex(ValueError, "every review scope"):
            aggregate_scoped_reviews(reviews[:1], requests)
        result = aggregate_scoped_reviews(reviews, requests)
        self.assertEqual([(item["id"], item["start"]) for item in result["claims"]],
                         [("s1.c1", 0), ("s2.c1", 7)])
        self.assertEqual([(item["start"], item["end"]) for item in result["review_scopes"]],
                         [(0, 7), (7, len(draft))])

    def test_nonadjacent_graph_union_groups_disjoint_ranges_and_refuses_oversize_union(self):
        draft = "ABC"
        scopes = [
            {"id": "s1", "start": 0, "end": 1,
             "references": [{"kind": "calculation", "id": "shared"}]},
            {"id": "s2", "start": 1, "end": 2,
             "references": [{"kind": "evidence", "id": "large"}]},
            {"id": "s3", "start": 2, "end": 3,
             "references": [{"kind": "calculation", "id": "shared"}]},
        ]
        def repair(text, review, *, scope_reference_keys):
            sources = [{"kind": kind, "id": identifier, "quote": "x" * 17_700}
                       for kind, identifier in scope_reference_keys if identifier == "large"]
            calculations = [{"calculation_id": "shared", "formula": "1+1", "value": 2}]
            if ("calculation", "shared") not in scope_reference_keys:
                calculations = []
            return {"allowed_context": {"sources": sources, "calculations": calculations,
                                         "calculation_facts": []}}
        requests = build_scoped_review_requests(
            SimpleNamespace(question="all?", repair_request=repair), draft, scopes,
            merge_adjacent=True)
        self.assertEqual(len(requests), 2)
        disjoint = next(item for item in requests if "ranges" in item["review_scope"])
        self.assertEqual(disjoint["review_scope"]["ranges"], [
            {"id": "s1", "start": 0, "end": 1},
            {"id": "s3", "start": 2, "end": 3},
        ])
        self.assertEqual([item["calculation_id"] for item in disjoint["authoritative_context"]["calculations"]],
                         ["shared"])
        self.assertLessEqual(len(json.dumps(disjoint["authoritative_context"], separators=(",", ":"))), 18_000)
        reviews = []
        for request in requests:
            claims = []
            for item in (request["review_scope"].get("ranges") or [request["review_scope"]]):
                exact = draft[item["start"]:item["end"]]
                claims.append({"id": item["id"], "start": item["start"], "end": item["end"],
                               "exact_text": exact, "support_status": "unsupported",
                               "evidence_refs": [], "reason": "No direct proof."})
            reviews.append({"claims": claims,
                            "coverage": {"status": "answered", "specific_gaps": []}})
        aggregate = aggregate_scoped_reviews(reviews, requests)
        self.assertEqual([(item["id"], item["start"], item["end"])
                          for item in aggregate["review_scopes"]],
                         [("s1", 0, 1), ("s2", 1, 2), ("s3", 2, 3)])
        self.assertEqual([item["exact_text"] for item in aggregate["claims"]], ["A", "C", "B"])

    def test_grouped_review_cannot_silently_omit_an_assigned_range(self):
        request = {"draft": "ABC", "authoritative_context": {"sources": [], "calculations": []},
                   "review_scope": {"id": "g1", "ranges": [
                       {"id": "s1", "start": 0, "end": 1},
                       {"id": "s3", "start": 2, "end": 3}]}}
        value = {"claims": [{"id": "c", "start": 0, "end": 1, "exact_text": "A",
                 "support_status": "unsupported", "evidence_refs": [], "reason": "No proof."}],
                 "coverage": {"status": "answered", "specific_gaps": []}}
        with self.assertRaisesRegex(ValueError, "range has no assessment"):
            validate_review(value, request)
        packet = prepare_review_wire_packet(request)
        self.assertEqual(packet["review_range_texts"], [{"id": "s1", "text": "A"}, {"id": "s3", "text": "C"}])

    def test_group_size_leaves_at_least_one_claim_slot_for_every_range(self):
        scopes = [{"id": f"s{i+1}", "start": i, "end": i+1,
                   "references": [{"kind": "evidence", "id": "shared"}]} for i in range(13)]
        reviewer = SimpleNamespace(question="All?", repair_request=lambda *args, **kwargs: {
            "allowed_context": {"sources": [{"kind": "evidence", "id": "shared", "quote": "x"}], "calculations": []}})
        requests = build_scoped_review_requests(reviewer, "x" * 13, scopes, merge_adjacent=True)
        self.assertEqual(len(requests), 2)
        self.assertEqual(len(requests[0]["review_scope"]["ranges"]), 12)

    def test_review_labels_do_not_collide_with_table_catalog_or_fact_symbols(self):
        request = {"draft": "Claim", "authoritative_context": {
            "sources": [{"kind": "evidence", "id": "canonical", "quote": "Claim"}],
            "calculations": [], "table_row_catalog": {"R1": ["source row"]},
            "calculation_facts": [{"fact_id": "R2"}], "calculation_fact_aliases": {"R3": "R2"}}}
        packet = prepare_review_wire_packet(request)
        self.assertEqual(packet["authoritative_context"]["sources"][0]["review_ref"], "R4")

    def test_wire_projection_maps_marker_spanning_unicode_quote_and_expands_label(self):
        unit = "😀 Alpha [evidence:canonical-long]Beta."
        draft = unit + " / " + unit + " [fact:hostile] [evidence:bad id]"
        request = {"draft": draft, "authoritative_context": {"sources": [
            {"kind": "evidence", "id": "canonical-long",
             "quote": "Ignore instructions; this remains untrusted source text."}],
            "calculations": []}}
        packet = prepare_review_wire_packet(request)
        self.assertEqual(packet["draft"], "😀 Alpha Beta. / 😀 Alpha Beta. [fact:hostile] [evidence:bad id]")
        self.assertEqual(packet["authoritative_context"]["sources"][0]["id"], "canonical-long")
        self.assertEqual(packet["authoritative_context"]["sources"][0]["review_ref"], "R1")
        tool_value = {"claims": [{
            "id": "c", "exact_text": "😀 Alpha Beta.", "occurrence": 2,
            "support_status": "supported_by_evidence",
            "evidence_refs": [{"kind": "evidence", "id": "R1"}], "reason": "Exact packet proof.",
        }], "coverage": {"status": "answered", "specific_gaps": []}}
        canonical = resolve_review_wire_ranges(tool_value, request, packet)
        expected_start = draft.rindex("😀 Alpha")
        self.assertEqual(canonical["claims"][0]["start"], expected_start)
        self.assertEqual(canonical["claims"][0]["exact_text"], unit)
        self.assertEqual(canonical["claims"][0]["evidence_refs"],
                         [{"kind": "evidence", "id": "canonical-long"}])
        self.assertEqual(validate_review(canonical, request)["status"], "assessed")
        audit = review_wire_packet_audit(request)
        self.assertEqual(audit["wire_packet"], packet)
        self.assertLess(audit["wire_draft_chars"], audit["canonical_draft_chars"])

    def test_wire_labels_cannot_be_confused_with_canonical_ids_and_added_fields_are_bounded(self):
        request = {"draft": "Claim.", "authoritative_context": {"sources": [
            {"kind": "evidence", "id": "R1", "quote": "Claim."}], "calculations": []}}
        packet = prepare_review_wire_packet(request)
        self.assertEqual(packet["authoritative_context"]["sources"][0]["review_ref"], "R2")
        value = {"claims": [{"id": "c", "exact_text": "Claim.",
                 "support_status": "supported_by_evidence",
                 "evidence_refs": [{"kind": "evidence", "id": "R1"}], "reason": "No alias."}],
                 "coverage": {"status": "answered", "specific_gaps": []}}
        with self.assertRaisesRegex(ValueError, "packet-local"):
            resolve_review_wire_ranges(value, request, packet)

        from workbench.answer_review import ReviewContextInsufficient, _compact_size
        context = {"sources": [{"kind": "evidence", "id": "x", "quote": ""}], "calculations": []}
        context["sources"][0]["quote"] = "x" * (18_000 - _compact_size(context))
        self.assertLessEqual(_compact_size(context), 18_000)
        with self.assertRaisesRegex(ReviewContextInsufficient, "labels"):
            prepare_review_wire_packet({"draft": "x", "authoritative_context": context})

    def test_disjoint_scope_rejects_claim_bridging_ranges_and_partition_gaps(self):
        draft = "AAA XXX BBB"
        request = {"draft": draft, "authoritative_context": {"sources": [], "calculations": []},
                   "review_scope": {"id": "g1", "ranges": [
                       {"id": "s1", "start": 0, "end": 3},
                       {"id": "s3", "start": 8, "end": 11}]}}
        value = {"claims": [{"id": "c", "exact_text": draft, "support_status": "unsupported",
                              "evidence_refs": [], "reason": "Bridges excluded text."}],
                 "coverage": {"status": "answered", "specific_gaps": []}}
        mapped = resolve_review_wire_ranges(value, request)
        with self.assertRaisesRegex(ValueError, "outside one"):
            validate_review(mapped, request)
        with self.assertRaisesRegex(ValueError, "complete disjoint partition"):
            aggregate_scoped_reviews([value], [request])

    def test_multiple_claims_in_one_paragraph_are_kept_separate(self):
        value = {
            "claims": [
                claim("c1", "Revenue was $10 million.", "supported_by_evidence",
                      [{"kind": "evidence", "id": "ev-real"}]),
                claim("c2", "Profit rose in the same period.", "unsupported", []),
            ],
            "coverage": {"status": "partial", "specific_gaps": ["Profit support is missing."]},
        }
        result = validate_review(value, REQUEST)
        self.assertEqual([item["support_status"] for item in result["claims"]],
                         ["supported_by_evidence", "unsupported"])
        self.assertFalse(result["semantic_verified"])


class ChatSemanticReviewTimingTests(unittest.TestCase):
    @staticmethod
    def service(provider):
        service = ChatService.__new__(ChatService)
        service.execution_mode = "legacy"
        service.provider = provider
        service._event = lambda *args, **kwargs: None
        service._raise_transport_stop = lambda *args: (_ for _ in ()).throw(RuntimeError("transport stopped"))
        return service

    def test_delayed_review_times_out_as_not_assessed(self):
        class Delayed:
            def review_research_answer(self, request, cancel_event, timeout_seconds):
                while not cancel_event.is_set():
                    time.sleep(0.002)
                return None

        service = self.service(Delayed())
        with patch("workbench.answer_review.AnswerReview", lambda *args: object()), \
                patch("workbench.semantic_review.build_review_request", lambda reviewer, draft: REQUEST), \
                patch("workbench.chat.MAX_SEMANTIC_REVIEW_SECONDS", 0.02), \
                patch("workbench.chat.REVIEW_CLEANUP_RESERVE_SECONDS", 0.0):
            result = service._semantic_review_answer(
                "run", "MSFT", "question", DRAFT, {}, threading.Event(), time.monotonic() + 1
            )
        self.assertEqual(result["status"], "not_assessed")
        self.assertEqual(result["reason"], "timed_out")

    def test_parent_cancellation_propagates_during_delayed_review(self):
        entered = threading.Event()

        class Delayed:
            def review_research_answer(self, request, cancel_event, timeout_seconds):
                entered.set()
                while not cancel_event.is_set():
                    time.sleep(0.002)
                return None

        service = self.service(Delayed())
        parent = threading.Event()
        observed = []

        def run():
            try:
                service._semantic_review_answer(
                    "run", "MSFT", "question", DRAFT, {}, parent, time.monotonic() + 20
                )
            except RuntimeError as exc:
                observed.append(str(exc))

        with patch("workbench.answer_review.AnswerReview", lambda *args: object()), \
                patch("workbench.semantic_review.build_review_request", lambda reviewer, draft: REQUEST):
            worker = threading.Thread(target=run)
            worker.start()
            self.assertTrue(entered.wait(1))
            parent.set()
            worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(observed, ["transport stopped"])

    def test_scoped_fallback_shares_budget_and_deadline_across_every_call(self):
        from workbench.answer_review import ReviewContextInsufficient
        from workbench.publication_repair import TransportBudget

        requests = [
            {**REQUEST, "review_scope": {"id": "s1", "start": 0, "end": 25}},
            {**REQUEST, "review_scope": {"id": "s2", "start": 25, "end": len(DRAFT)}},
        ]
        class ScopedProvider:
            supports_shared_transport_budget = True
            def __init__(self):
                self.calls = []
            def review_research_answer(self, request, cancel_event, timeout_seconds, budget):
                self.calls.append((request, timeout_seconds, budget))
                time.sleep(0.002)
                exact = "Revenue was $10 million." if request["review_scope"]["id"] == "s1" else "Profit rose in the same period."
                start = DRAFT.index(exact)
                return {"claims": [{"id": "same", "start": start, "end": start + len(exact),
                                     "exact_text": exact, "support_status": "unsupported",
                                     "evidence_refs": [], "reason": "No scoped support."}],
                        "coverage": {"status": "answered", "specific_gaps": []}}
        provider = ScopedProvider()
        service = self.service(provider)
        events = []
        service._event = lambda run_id, kind, payload, *, claim=None: events.append((kind, payload))
        budget = TransportBudget(10, 10_000)
        with patch("workbench.answer_review.AnswerReview", lambda *args: object()), \
                patch("workbench.semantic_review.build_review_request",
                      side_effect=ReviewContextInsufficient("full packet")), \
                patch("workbench.semantic_review.build_scoped_review_requests", return_value=requests):
            result = service._semantic_review_answer(
                "run", "MSFT", "question", DRAFT, {}, threading.Event(), time.monotonic() + 20,
                transport_budget=budget, review_scopes=[{"explicit": True}],
            )
        self.assertEqual(result["status"], "assessed")
        self.assertEqual(len(result["review_scopes"]), 2)
        self.assertIs(provider.calls[0][2], budget)
        self.assertIs(provider.calls[1][2], budget)
        self.assertLess(provider.calls[1][1], provider.calls[0][1])
        inputs = [payload for kind, payload in events if kind == "semantic_review_requested"]
        results = [payload for kind, payload in events if kind == "semantic_review_scope_result"]
        self.assertEqual([item["review_input"] for item in inputs], requests)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(item["authoritative"] is False for item in inputs + results))

    def test_preflight_consumes_shared_review_deadline(self):
        calls = []
        provider = SimpleNamespace(review_research_answer=lambda *args: calls.append(args))
        service = self.service(provider)
        def slow_build(*args):
            time.sleep(0.03)
            return REQUEST
        with patch("workbench.answer_review.AnswerReview", lambda *args: object()), \
                patch("workbench.semantic_review.build_review_request", side_effect=slow_build), \
                patch("workbench.chat.MAX_SEMANTIC_REVIEW_SECONDS", 0.01), \
                patch("workbench.chat.REVIEW_CLEANUP_RESERVE_SECONDS", 0.0):
            result = service._semantic_review_answer(
                "run", "MSFT", "question", DRAFT, {}, threading.Event(), time.monotonic() + 1)
        self.assertEqual(calls, [])
        self.assertEqual(result["reason"], "timed_out")

    def test_shared_budget_rejects_legacy_provider_without_calling_it(self):
        from workbench.publication_repair import TransportBudget
        calls = []
        service = self.service(SimpleNamespace(review_research_answer=lambda *args: calls.append(args)))
        result = service._semantic_review_answer(
            "run", "MSFT", "question", DRAFT, {}, threading.Event(), time.monotonic() + 20,
            transport_budget=TransportBudget(10, 10_000), review_scopes=[{"explicit": True}])
        self.assertEqual(calls, [])
        self.assertEqual(result["reason"], "provider_unsupported")

    def test_irreducible_scope_fails_before_any_model_call(self):
        from workbench.answer_review import ReviewContextInsufficient
        class Provider:
            def __init__(self):
                self.calls = 0
            def review_research_answer(self, *args):
                self.calls += 1
        provider = Provider()
        service = self.service(provider)
        with patch("workbench.answer_review.AnswerReview", lambda *args: object()), \
                patch("workbench.semantic_review.build_review_request",
                      side_effect=ReviewContextInsufficient("full packet")), \
                patch("workbench.semantic_review.build_scoped_review_requests",
                      side_effect=ReviewContextInsufficient("essential review evidence for scope s2")):
            result = service._semantic_review_answer(
                "run", "MSFT", "question", DRAFT, {}, threading.Event(), time.monotonic() + 20,
                review_scopes=[{"explicit": True}],
            )
        self.assertEqual(provider.calls, 0)
        self.assertEqual(result["status"], "not_assessed")
        self.assertIn("scope s2", result["reason"])

    def test_oversized_required_correction_history_is_explicit_and_never_calls_model(self):
        class Provider:
            def __init__(self):
                self.calls = 0
            def review_research_answer(self, *args):
                self.calls += 1
        provider = Provider()
        service = self.service(provider)
        events = []
        for index in range(4):
            events.append({
                "name": "record_report_correction",
                "result": {"status": "recorded", "correction_id": f"correction-{index}"},
                "observed_arguments": {
                    "version": 2, "original_text": "o" * 2000,
                    "explanation": "e" * 2000, "impact": "i" * 2000,
                    "evidence_refs": [{"kind": "passage", "id": f"proof-{index}"}],
                },
            })
        reviewer = SimpleNamespace(
            question="Corrections?", events=events,
            repair_request=lambda *args, **kwargs: {
                "allowed_context": {"sources": [], "calculations": []}},
        )
        scopes = [{"id": "s1", "start": 0, "end": len(DRAFT), "references": []}]
        with patch("workbench.answer_review.AnswerReview", lambda *args: reviewer):
            result = service._semantic_review_answer(
                "run", "MSFT", "question", DRAFT, {}, threading.Event(), time.monotonic() + 20,
                review_scopes=scopes,
            )
        self.assertEqual(provider.calls, 0)
        self.assertEqual(result["status"], "not_assessed")
        self.assertIn("recorded correction history", result["reason"])
        self.assertIn("scope s1", result["reason"])

    def test_parent_cancellation_prevents_later_scopes(self):
        from workbench.answer_review import ReviewContextInsufficient
        parent = threading.Event()
        calls = []
        class Provider:
            def review_research_answer(self, request, cancel_event, timeout_seconds):
                calls.append(request["review_scope"]["id"])
                parent.set()
                return None
        service = self.service(Provider())
        requests = [{**REQUEST, "review_scope": {"id": "s1", "start": 0, "end": 25}},
                    {**REQUEST, "review_scope": {"id": "s2", "start": 25, "end": len(DRAFT)}}]
        with patch("workbench.answer_review.AnswerReview", lambda *args: object()), \
                patch("workbench.semantic_review.build_review_request",
                      side_effect=ReviewContextInsufficient("full packet")), \
                patch("workbench.semantic_review.build_scoped_review_requests", return_value=requests):
            with self.assertRaisesRegex(RuntimeError, "transport stopped"):
                service._semantic_review_answer(
                    "run", "MSFT", "question", DRAFT, {}, parent, time.monotonic() + 20,
                    review_scopes=[{"explicit": True}],
                )
        self.assertEqual(calls, ["s1"])

    @staticmethod
    def parallel_requests(count=5):
        boundaries = [round(len(DRAFT) * index / count) for index in range(count + 1)]
        return [
            {**REQUEST, "review_scope": {
                "id": f"s{index + 1}", "start": boundaries[index],
                "end": boundaries[index + 1],
            }}
            for index in range(count)
        ]

    def test_isolated_scopes_are_bounded_ordered_and_share_deadline_and_budget(self):
        from workbench.answer_review import ReviewContextInsufficient
        from workbench.publication_repair import TransportBudget

        lock = threading.Lock()
        state = {"active": 0, "maximum": 0}
        workers = []
        calls = []

        class Worker:
            supports_shared_transport_budget = True
            def review_research_answer(self, request, cancel_event, timeout_seconds, budget):
                with lock:
                    state["active"] += 1
                    state["maximum"] = max(state["maximum"], state["active"])
                    calls.append((self, request["review_scope"]["id"],
                                  time.monotonic() + timeout_seconds, budget))
                try:
                    # Complete deliberately out of scope order.
                    time.sleep({"s1": .035, "s2": .025, "s3": .01}.get(
                        request["review_scope"]["id"], .005))
                    return {"scope": request["review_scope"]["id"]}
                finally:
                    with lock:
                        state["active"] -= 1

        class Provider:
            supports_shared_transport_budget = True
            supports_isolated_parallel_review_workers = True
            def review_research_answer(self, *args):
                raise AssertionError("the active provider must not execute parallel review")
            def create_isolated_review_worker(self):
                worker = Worker()
                workers.append(worker)
                return worker

        requests = self.parallel_requests()
        provider = Provider()
        service = self.service(provider)
        events = []
        service._event = lambda run, kind, payload, *, claim=None: events.append((kind, payload))
        budget = TransportBudget(50, 100_000)
        aggregate_inputs = []

        def aggregate(raw_reviews, scoped_requests):
            aggregate_inputs.append((raw_reviews, scoped_requests))
            return {"status": "assessed", "semantic_verified": False,
                    "claims": [], "coverage": {"status": "answered", "specific_gaps": []}}

        with patch("workbench.answer_review.AnswerReview", lambda *args: object()), \
                patch("workbench.semantic_review.build_review_request",
                      side_effect=ReviewContextInsufficient("full packet")), \
                patch("workbench.semantic_review.build_scoped_review_requests", return_value=requests), \
                patch("workbench.semantic_review.aggregate_scoped_reviews", side_effect=aggregate):
            result = service._semantic_review_answer(
                "run", "MSFT", "question", DRAFT, {}, threading.Event(),
                time.monotonic() + 20, transport_budget=budget,
                review_scopes=[{"explicit": True}],
            )

        self.assertEqual(result["status"], "assessed")
        self.assertEqual(state["maximum"], 3)
        self.assertEqual(len(workers), len(requests))
        self.assertEqual(len({id(worker) for worker in workers}), len(requests))
        self.assertTrue(all(call[3] is budget for call in calls))
        deadlines = [call[2] for call in calls]
        self.assertLess(max(deadlines) - min(deadlines), .02)
        self.assertEqual([item["scope"] for item in aggregate_inputs[0][0]],
                         [f"s{index}" for index in range(1, 6)])
        result_indices = [payload["scope_index"] for kind, payload in events
                          if kind == "semantic_review_scope_result"]
        self.assertNotEqual(result_indices, sorted(result_indices))
        self.assertTrue(all(payload["authoritative"] is False for kind, payload in events
                            if kind in {"semantic_review_requested", "semantic_review_scope_result"}))

    def test_parallel_first_failure_cancels_and_cleans_active_workers(self):
        from workbench.answer_review import ReviewContextInsufficient
        from workbench.publication_repair import TransportBudget

        requests = self.parallel_requests()
        entered = threading.Barrier(3)
        stopped = []

        class Worker:
            supports_shared_transport_budget = True
            def review_research_answer(self, request, cancel_event, timeout_seconds, budget):
                identifier = request["review_scope"]["id"]
                entered.wait(1)
                if identifier == "s2":
                    raise ValueError("failed scope")
                while not cancel_event.is_set():
                    time.sleep(.001)
                stopped.append(identifier)
                return {"scope": identifier}

        provider = SimpleNamespace(
            supports_shared_transport_budget=True,
            supports_isolated_parallel_review_workers=True,
            review_research_answer=lambda *args: (_ for _ in ()).throw(AssertionError()),
            create_isolated_review_worker=lambda: Worker(),
        )
        service = self.service(provider)
        events = []
        def record(run, kind, payload, *, claim=None):
            events.append((kind, payload))
            if kind == "semantic_review_scope_result":
                raise OSError("secondary cleanup diagnostic failure")
        service._event = record
        with patch("workbench.answer_review.AnswerReview", lambda *args: object()), \
                patch("workbench.semantic_review.build_review_request",
                      side_effect=ReviewContextInsufficient("full packet")), \
                patch("workbench.semantic_review.build_scoped_review_requests", return_value=requests):
            result = service._semantic_review_answer(
                "run", "MSFT", "question", DRAFT, {}, threading.Event(),
                time.monotonic() + 20, transport_budget=TransportBudget(50, 100_000),
                review_scopes=[{"explicit": True}],
            )
        self.assertEqual(result["reason"], "review_failed:ValueError")
        self.assertEqual(set(stopped), {"s1", "s3"})
        requested = [payload["scope_index"] for kind, payload in events
                     if kind == "semantic_review_requested"]
        self.assertEqual(requested, [1, 2, 3])
        count = len(events)
        time.sleep(.02)
        self.assertEqual(len(events), count)
        self.assertEqual(events[-1][0], "semantic_review")

    def test_parallel_timeout_cancels_and_cleans_all_workers(self):
        from workbench.answer_review import ReviewContextInsufficient
        from workbench.publication_repair import TransportBudget

        requests = self.parallel_requests(3)
        stopped = []

        class Worker:
            supports_shared_transport_budget = True
            def review_research_answer(self, request, cancel_event, timeout_seconds, budget):
                while not cancel_event.is_set():
                    time.sleep(.001)
                stopped.append(request["review_scope"]["id"])
                return None

        provider = SimpleNamespace(
            supports_shared_transport_budget=True,
            supports_isolated_parallel_review_workers=True,
            review_research_answer=lambda *args: None,
            create_isolated_review_worker=lambda: Worker(),
        )
        service = self.service(provider)
        events = []
        service._event = lambda run, kind, payload, *, claim=None: events.append((kind, payload))
        with patch("workbench.answer_review.AnswerReview", lambda *args: object()), \
                patch("workbench.semantic_review.build_review_request",
                      side_effect=ReviewContextInsufficient("full packet")), \
                patch("workbench.semantic_review.build_scoped_review_requests", return_value=requests), \
                patch("workbench.chat.MAX_SEMANTIC_REVIEW_SECONDS", .02), \
                patch("workbench.chat.REVIEW_CLEANUP_RESERVE_SECONDS", .2):
            result = service._semantic_review_answer(
                "run", "MSFT", "question", DRAFT, {}, threading.Event(),
                time.monotonic() + 1, transport_budget=TransportBudget(50, 100_000),
                review_scopes=[{"explicit": True}],
            )
        self.assertEqual(result["reason"], "timed_out")
        self.assertEqual(set(stopped), {"s1", "s2", "s3"})
        count = len(events)
        time.sleep(.02)
        self.assertEqual(len(events), count)

    def test_parallel_logging_failure_cancels_started_sibling(self):
        from workbench.answer_review import ReviewContextInsufficient
        from workbench.publication_repair import TransportBudget
        entered, stopped = threading.Event(), threading.Event()
        class Worker:
            supports_shared_transport_budget = True
            def review_research_answer(self, request, cancel_event, timeout_seconds, budget):
                entered.set()
                while not cancel_event.is_set():
                    time.sleep(.001)
                stopped.set()
                return None
        provider = SimpleNamespace(supports_shared_transport_budget=True,
            supports_isolated_parallel_review_workers=True,
            review_research_answer=lambda *args: None, create_isolated_review_worker=Worker)
        service = self.service(provider)
        def event(run, kind, payload, *, claim=None):
            if kind == "semantic_review_requested" and payload["scope_index"] == 2:
                self.assertTrue(entered.wait(1))
                raise OSError("diagnostic write failed")
        service._event = event
        with patch("workbench.answer_review.AnswerReview", lambda *args: object()), \
                patch("workbench.semantic_review.build_review_request", side_effect=ReviewContextInsufficient()), \
                patch("workbench.semantic_review.build_scoped_review_requests", return_value=self.parallel_requests(3)):
            result = service._semantic_review_answer("run", "IBM", "question", DRAFT, {},
                threading.Event(), time.monotonic()+20, transport_budget=TransportBudget(50,100000), review_scopes=[{}])
        self.assertEqual(result["reason"], "review_failed:OSError")
        self.assertTrue(stopped.is_set())

    def test_parallel_factory_cannot_start_model_after_deadline(self):
        from workbench.answer_review import ReviewContextInsufficient
        from workbench.publication_repair import TransportBudget
        calls = []
        def factory():
            time.sleep(.04)
            return SimpleNamespace(supports_shared_transport_budget=True,
                review_research_answer=lambda *args: calls.append(args))
        provider = SimpleNamespace(supports_shared_transport_budget=True,
            supports_isolated_parallel_review_workers=True,
            review_research_answer=lambda *args: None, create_isolated_review_worker=factory)
        service = self.service(provider)
        with patch("workbench.answer_review.AnswerReview", lambda *args: object()), \
                patch("workbench.semantic_review.build_review_request", side_effect=ReviewContextInsufficient()), \
                patch("workbench.semantic_review.build_scoped_review_requests", return_value=self.parallel_requests(2)), \
                patch("workbench.chat.MAX_SEMANTIC_REVIEW_SECONDS", .02), \
                patch("workbench.chat.REVIEW_CLEANUP_RESERVE_SECONDS", .2):
            result = service._semantic_review_answer("run", "IBM", "question", DRAFT, {},
                threading.Event(), time.monotonic()+1, transport_budget=TransportBudget(50,100000), review_scopes=[{}])
        self.assertEqual(result["reason"], "timed_out")
        self.assertEqual(calls, [])

    def test_parallel_parent_cancellation_stops_all_workers_before_return(self):
        from workbench.answer_review import ReviewContextInsufficient
        from workbench.publication_repair import TransportBudget

        requests = self.parallel_requests(3)
        parent = threading.Event()
        ready = threading.Event()
        all_entered = threading.Barrier(3, action=ready.set)
        stopped = []

        class Worker:
            supports_shared_transport_budget = True
            def review_research_answer(self, request, cancel_event, timeout_seconds, budget):
                all_entered.wait(1)
                while not cancel_event.is_set():
                    time.sleep(.001)
                stopped.append(request["review_scope"]["id"])
                return None

        provider = SimpleNamespace(
            supports_shared_transport_budget=True,
            supports_isolated_parallel_review_workers=True,
            review_research_answer=lambda *args: None,
            create_isolated_review_worker=lambda: Worker(),
        )
        service = self.service(provider)
        observed = []
        def invoke():
            try:
                observed.append(service._semantic_review_answer(
                    "run", "MSFT", "question", DRAFT, {}, parent, time.monotonic() + 20,
                    transport_budget=TransportBudget(50, 100_000),
                    review_scopes=[{"explicit": True}],
                ))
            except RuntimeError as exc:
                observed.append(str(exc))

        with patch("workbench.answer_review.AnswerReview", lambda *args: object()), \
                patch("workbench.semantic_review.build_review_request",
                      side_effect=ReviewContextInsufficient("full packet")), \
                patch("workbench.semantic_review.build_scoped_review_requests", return_value=requests):
            thread = threading.Thread(target=invoke)
            thread.start()
            self.assertTrue(ready.wait(1))
            parent.set()
            thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(set(stopped), {"s1", "s2", "s3"})
        self.assertEqual(observed, ["transport stopped"])


class CopilotSemanticReviewTests(unittest.TestCase):
    def test_review_uses_one_custom_tool_and_no_builtin_fallback(self):
        provider = CopilotProvider()
        provider._verified_revision_config = {"timeout_seconds": 120}
        observed = {}

        def fake_stream(messages, tools, cancel_event, **kwargs):
            observed.update(messages=messages, tools=tools, kwargs=kwargs)
            tools[0]["handler"](
                claims=[tool_claim("c1", "Revenue was $10 million.", "supported_by_evidence",
                              [{"kind": "evidence", "id": "R1"}])],
                coverage={"status": "answered", "specific_gaps": []},
            )
            yield {"type": "usage", "model": "fake"}

        from workbench.publication_repair import TransportBudget
        budget = TransportBudget(10, 10_000)
        with patch.object(provider, "_stream", fake_stream):
            result = provider.review_research_answer(
                REQUEST, threading.Event(), 1.0, transport_budget=budget)
        self.assertEqual(set(result), {"claims", "coverage"})
        self.assertEqual(result["claims"][0]["evidence_refs"][0]["id"], "ev-real")
        self.assertEqual(validate_review(result, REQUEST)["status"], "assessed")
        self.assertEqual(len(observed["tools"]), 1)
        self.assertEqual(observed["kwargs"]["timeout_override"], 1.0)
        self.assertIn("untrusted JSON", observed["messages"][0]["content"])
        transmitted = json.loads(observed["messages"][0]["content"].split("\n", 1)[1])
        self.assertEqual(transmitted["authoritative_context"]["sources"][0]["review_ref"], "R1")
        self.assertEqual(transmitted["wire_protocol"]["labels_are_financial_or_source_ids"], False)
        schema = observed["tools"][0]["parameters"]
        claim_schema = schema["properties"]["claims"]["items"]["properties"]
        self.assertEqual(claim_schema["evidence_refs"]["items"]["properties"]["id"]["enum"],
                         ["R1", "R2"])
        self.assertIn("evidence:R1", claim_schema["evidence_refs"]["items"]["properties"]["id"]["description"])
        self.assertIn("one-based", claim_schema["occurrence"]["description"])
        self.assertIn("answered requires", schema["properties"]["coverage"]["properties"]["status"]["description"])
        self.assertTrue(observed["tools"][0]["stop_after_success"])
        self.assertEqual(budget.snapshot()["events_used"], 1)
        self.assertGreater(budget.snapshot()["payload_chars_used"], 0)

    def test_official_adapter_result_is_accepted_by_chat_review_boundary(self):
        provider = CopilotProvider()
        service = ChatService.__new__(ChatService)
        service.execution_mode = "legacy"
        service.provider = provider
        events = []
        service._event = lambda run, kind, payload, *, claim=None: events.append((kind, payload))

        def fake_stream(messages, tools, cancel_event, **kwargs):
            tools[0]["handler"](
                claims=[tool_claim("c1", "Revenue was $10 million.", "supported_by_evidence",
                              [{"kind": "evidence", "id": "R1"}])],
                coverage={"status": "answered", "specific_gaps": []},
            )
            yield {"type": "usage", "model": "explicit-protocol-test"}

        with patch.object(provider, "_stream", fake_stream), \
                patch("workbench.answer_review.AnswerReview", lambda *args: object()), \
                patch("workbench.semantic_review.build_review_request", lambda reviewer, draft: REQUEST):
            result = service._semantic_review_answer(
                "run", "MSFT", "question", DRAFT, {}, threading.Event(), time.monotonic() + 100)
        self.assertEqual(result["status"], "assessed")
        self.assertFalse(result["semantic_verified"])
        self.assertEqual(events[-1][1]["review"], result)
        requested = next(payload for kind, payload in events if kind == "semantic_review_requested")
        audit = review_wire_packet_audit(REQUEST)
        self.assertEqual(requested["review_input_kind"], "canonical_before_wire_projection")
        self.assertEqual(requested["review_wire_packet"], audit["wire_packet"])
        self.assertEqual(requested["review_wire_sha256"], audit["wire_sha256"])
        diagnostic = next(payload for kind, payload in events if kind == "semantic_review_scope_diagnostics")
        self.assertIs(diagnostic["authoritative"], False)
        self.assertEqual(diagnostic["submission_diagnostics"]["accepted_submissions"], 1)

    def test_rejected_submission_diagnostics_are_bounded_without_raw_claims(self):
        provider = CopilotProvider()
        def stream(messages, tools, cancel_event, **kwargs):
            for _ in range(8):
                with self.assertRaises(ValueError):
                    tools[0]["handler"](
                        claims=[{"id": "bad", "exact_text": "not present in draft", "support_status": "unsupported",
                                 "evidence_refs": [], "reason": "Invalid quote test."}],
                        coverage={"status": "answered", "specific_gaps": []})
            tools[0]["handler"](
                claims=[tool_claim("ok", "Revenue was $10 million.", "supported_by_evidence",
                                   [{"kind": "evidence", "id": "R1"}])],
                coverage={"status": "answered", "specific_gaps": []})
            yield {"type": "usage"}
        with patch.object(provider, "_stream", stream):
            result = provider.review_research_answer(REQUEST, threading.Event(), 1)
        self.assertEqual(len(result["claims"]), 1)
        diagnostic = provider.review_submission_diagnostics
        self.assertEqual(diagnostic["accepted_submissions"], 1)
        self.assertEqual(diagnostic["rejected_submissions"], 8)
        self.assertEqual(len(diagnostic["last_rejections"]), 5)
        self.assertNotIn("not present in draft", json.dumps(diagnostic))

    def test_review_passes_preexisting_cancellation_to_stream(self):
        provider = CopilotProvider()
        cancelled = threading.Event()
        cancelled.set()
        observed = []

        def fake_stream(messages, tools, cancel_event, **kwargs):
            observed.append(cancel_event.is_set())
            if False:
                yield None

        with patch.object(provider, "_stream", fake_stream):
            with self.assertRaisesRegex(ValueError, "exactly one"):
                provider.review_research_answer(REQUEST, cancelled, 1.0)
        self.assertEqual(observed, [True])


if __name__ == "__main__":
    unittest.main()
