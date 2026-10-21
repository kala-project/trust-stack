"""Deterministic, read-only projections for completed research answers.

The projection deliberately reports mechanical linkage rather than semantic truth.  It
uses only the answer's persisted snapshot and authoritative runner events; it never
calls a model and never writes to the chat database.
"""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any

from .chat import ChatError, ChatService
from .documents import DocumentError
from .atomic_anchors import (
    ANCHOR_SCHEMA_VERSION, OFFSET_UNIT, build_candidates, utf16_offset,
)


SCHEMA_VERSION = "answer-artifact/1.0"
ALGORITHM_VERSION = "answer-artifact-projection/2.10.0"
_CITATION = re.compile(r"\[(claim|evidence|passage|calculation|query):([^\]\s]+)\]")
_PARAGRAPH = re.compile(r"\n\s*\n+")
_CALCULATION_LANGUAGE = re.compile(
    r"(?i)(同比|环比|增长(?:率)?|下降(?:了|率)?|增加(?:了)?|减少(?:了)?|差额|相差|合计|总计|"
    r"year[- ]over[- ]year|yoy|growth|grew|rose|fell|increas(?:e|ed)|decreas(?:e|ed)|declin(?:e|ed)|"
    r"difference|percent(?:age)? change|sum|total|斜率|截距|slope|intercept|R²|R-squared)"
)
_ABSENCE_ASSERTION = re.compile(
    r"(没有披露|未披露|并未披露|未提供(?:任何)?解释|没有提供(?:任何)?解释|无调节表|未提供调节表|"
    r"(?:未|没有)(?:给出|提供)(?:明确的?|任何)?(?:调节表|解释)|"
    r"not disclosed|did not disclose|no disclosure|provided no explanation|no reconciliation table)", re.I
)
_SCOPED_SEARCH = re.compile(
    r"(在|就|仅限于).{0,24}(已检索|所检索|检索到|本次检索|上述资料|这些资料)|"
    r"(?:现有|本次|这些|所检索)(?:材料|资料|检索)|"
    r"(within|in) (?:the )?(?:searched|retrieved|reviewed) (?:material|documents?|sources?)", re.I
)

# These aliases are intentionally disjoint. English "income" alone is never a
# metric: revenue and operating income are different rows.
_METRIC_PATTERNS = {
    "revenue": (
        re.compile(r"营业收入|营业额|营收|收入"),
        re.compile(r"(?<!operating )(?<!net )\brevenues?\b", re.I),
    ),
    "operating_income": (
        re.compile(r"营业利润|经营利润"),
        re.compile(r"\boperating\s+(?:income|profit)\b", re.I),
    ),
    "net_income": (re.compile(r"净利润|净收益"), re.compile(r"\bnet\s+income\b", re.I)),
    "eps_diluted": (re.compile(r"摊薄每股收益|稀释每股收益"), re.compile(r"\bdiluted\s+(?:EPS|earnings?\s+per\s+share)\b", re.I)),
    "capital_expenditures": (
        re.compile(r"资本(?:性)?支出|物业及设备购置|购置物业及设备"),
        re.compile(r"\bcap(?:ital\s+)?ex(?:penditures?)?\b|\badditions?\s+to\s+property\s+and\s+equipment\b", re.I),
    ),
}
_METRIC_LABELS = {
    "revenue": "Revenue", "operating_income": "Operating income",
    "net_income": "Net income", "eps_diluted": "Diluted EPS",
    "capital_expenditures": "Capital expenditures",
}

_BASE_LIMITATIONS = [
    "Citation linking, model review, and numerical recomputation are separate checks. See each review record for scope and results; none constitutes human professional approval.",
    "This organizes only the original answer, materials frozen for this run, and actual tool events; it does not indicate complete material coverage.",
    "Human-edited content does not inherit the original answer’s review status shown here.",
]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = _canonical([SCHEMA_VERSION, ALGORITHM_VERSION, *parts]).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()}"


def _dataset_semantic_claim_ids(datasets):
    """Collect only claims that a projected dataset actually carries."""
    identifiers = set()
    for dataset in datasets:
        reviews = [dataset.get("semantic_review")]
        reviews.extend(row.get("semantic_review") for row in dataset.get("rows", []))
        reviews.append((dataset.get("description_anchor") or {}).get("semantic_review"))
        for review in reviews:
            if isinstance(review, dict):
                identifiers.update(claim.get("id") for claim in review.get("claims", [])
                                   if isinstance(claim, dict))
    return identifiers


def validate_artifact_snapshot(artifact: dict) -> dict:
    """Validate an exact saved artifact using its explicitly supported algorithm.

    ``reader_report_id`` is attached after artifact identity generation and therefore
    is intentionally excluded together with ``artifact_id`` when reproducing the id.
    Unknown historical algorithms fail closed rather than being treated as equivalent.
    """
    if not isinstance(artifact, dict):
        raise ValueError("artifact snapshot must be a JSON object")
    if artifact.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported artifact schema version")
    if artifact.get("algorithm_version") != ALGORITHM_VERSION:
        raise ValueError("unsupported artifact algorithm version")
    candidate = deepcopy(artifact)
    recorded = candidate.pop("artifact_id", None)
    candidate.pop("reader_report_id", None)
    if not isinstance(recorded, str) or recorded != _stable_id("artifact", candidate):
        raise ValueError("artifact snapshot id is not reproducible")
    for key in ("conversation_id", "message_id", "run_id"):
        if not isinstance(artifact.get(key), str) or not artifact[key]:
            raise ValueError(f"artifact snapshot is missing {key}")
    return artifact


from .evidence_checks import (
    EvidenceCheckError, WorkspaceCalculationReplay,
    _answer_metric_identities, _calculation_input_amounts, _calculation_output_matches,
    _currency_amount, _currency_unit, _decimal_scale,
    _decimal_text, _declared_currency_units, _dimensional_coefficient_ranges,
    _join_table_context, _local_source_dimensions, _metric_mentions, _money_amounts,
    _normalized_metric_label, _period_kinds, _period_label, _plain_numbers,
    _quote_contains_fact_value, _readable_money, _source_metric_identities,
    _table_calculation_matches, _unitless_quote_amounts, calculation_records,
    calculation_subject, eligible_fact, findings as evidence_findings,
    frozen_fact_amounts, projection_indexes, read_evidence_record,
    resolve_event_reference, resolve_source,
    validate_calculation,
)
from .answer_review import assemble_persisted_evidence_input


class AnswerArtifactService:
    """Build Answer Artifact v1 from one exact persisted assistant message."""

    def __init__(self, chat):
        self.chat = chat

    # Compatibility delegates for direct historical projection callers. The
    # deterministic implementations live only in evidence_checks.
    @staticmethod
    def _findings(text, citation_map, trace_by_reference, citation_sources,
                  calculations=None, frozen_fact_amounts=None, question=""):
        return evidence_findings(
            text, citation_map, trace_by_reference, citation_sources,
            calculations, frozen_fact_amounts, question,
        )

    @staticmethod
    def _source_for_reference(kind, identifier, original, indexes):
        return resolve_source(kind, identifier, original, indexes)

    _event_reference = staticmethod(resolve_event_reference)
    _calculation_subject = staticmethod(calculation_subject)

    def _calculation_records(self, answer, snapshot, events, indexes, *, check_input=None):
        if check_input is None:
            raise EvidenceCheckError(
                "historical calculation projection requires an explicitly assembled sealed input")

        class _UnusedReplay:
            def replay_arithmetic(self, *args):
                raise EvidenceCheckError("calculation replay is unavailable")
            def replay_model(self, *args):
                raise EvidenceCheckError("model replay is unavailable")

        replay = (WorkspaceCalculationReplay(self.chat.evidence_db, self.chat.decisions)
                  if self.chat is not None else _UnusedReplay())
        result = calculation_records(check_input, indexes, replay)
        return list(result.records), result.event_by_id, list(result.limitations)

    def _frozen_fact_amounts(self, answer, snapshot, events, indexes, *, check_input=None):
        if check_input is None:
            raise EvidenceCheckError(
                "historical fact projection requires an explicitly assembled sealed input")
        return frozen_fact_amounts(check_input, indexes)

    def _valid_calculation(self, item, answer, snapshot, indexes, *, check_input=None):
        if check_input is None:
            raise EvidenceCheckError(
                "historical calculation validation requires an explicitly assembled sealed input")

        class _UnusedReplay:
            def replay_arithmetic(self, *args):
                raise EvidenceCheckError("calculation replay is unavailable")
            def replay_model(self, *args):
                raise EvidenceCheckError("model replay is unavailable")

        replay = (WorkspaceCalculationReplay(self.chat.evidence_db, self.chat.decisions)
                  if self.chat is not None else _UnusedReplay())
        return validate_calculation(item, check_input, indexes, replay)

    def get(self, conversation_id, message_id) -> dict:
        answer = self.chat.answer_context(conversation_id, message_id)
        if isinstance(self.chat, ChatService):
            documents = self.chat.documents_for(answer)
            if documents is not None:
                with documents.frozen_replay_scope():
                    snapshot = self._saved_snapshot(answer)
                    self._validate_saved_snapshot_sources(answer, snapshot)
                    return snapshot
            snapshot = self._saved_snapshot(answer)
            self._validate_saved_snapshot_sources(answer, snapshot)
            return snapshot
        return self._get(conversation_id, message_id, answer)

    def _saved_snapshot(self, answer: dict) -> dict:
        from .report_reader import load_reader_artifact_snapshot
        snapshot = load_reader_artifact_snapshot(
            self.chat, conversation_id=answer["conversation_id"], message_id=answer["message_id"])
        if snapshot is None:
            raise ChatError("report_artifact_snapshot_unavailable",
                            "The formal report's publication-time artifact snapshot is unavailable.", 409)
        return snapshot

    def _validate_saved_snapshot_sources(self, answer: dict, artifact: dict) -> None:
        """Keep formal source-record checks active when serving a frozen artifact."""
        if any(artifact.get(key) != answer.get(key) for key in (
                "conversation_id", "message_id", "run_id", "company_id", "answer")):
            raise ChatError("artifact_snapshot_mismatch", "saved artifact differs from its answer identity", 409)
        run = self.chat.get_run(answer["run_id"])
        if run.get("conversation_id") != answer["conversation_id"] or run.get("status") != "completed":
            raise ChatError("answer_run_mismatch", "answer run is not a completed run in this conversation", 409)
        effective = self.chat.effective_snapshot(answer["run_id"], answer["company_id"])
        if answer.get("source_snapshot") != effective:
            raise ChatError("source_snapshot_mismatch", "the answer snapshot differs from its formal research records", 409)
        audit = artifact.get("audit") if isinstance(artifact.get("audit"), dict) else {}
        if (audit.get("run_id") != answer["run_id"] or audit.get("company_id") != answer["company_id"]
                or audit.get("data_version") != effective.get("data_version")):
            raise ChatError("artifact_snapshot_mismatch", "saved artifact audit identity differs from the frozen snapshot", 409)
        for citation in answer.get("citations", []):
            if (isinstance(citation, dict) and citation.get("valid") is True
                    and citation.get("kind") in {"passage", "evidence", "claim", "calculation", "query"}
                    and isinstance(citation.get("id"), str)):
                self.chat.read_citation(
                    answer["conversation_id"], answer["message_id"], citation["kind"], citation["id"])

    def _get(self, conversation_id, message_id, answer) -> dict:
        publication = None
        if isinstance(self.chat, ChatService):
            from .research_tools import ToolError
            try:
                publication = self.chat.publications.read(answer)
            except ToolError as exc:
                raise ChatError(exc.code, exc.message, 409) from exc
        run = self.chat.get_run(answer["run_id"])
        if run.get("conversation_id") != answer["conversation_id"] or run.get("status") != "completed":
            raise ChatError("answer_run_mismatch", "answer run is not a completed run in this conversation", 409)

        snapshot = answer.get("source_snapshot")
        effective = self.chat.effective_snapshot(answer["run_id"], answer["company_id"])
        if not isinstance(snapshot, dict) or snapshot != effective:
            raise ChatError("source_snapshot_mismatch", "the answer snapshot differs from its formal research records", 409)
        frozen = snapshot.get("evidence")
        if not isinstance(frozen, dict) or frozen.get("company_id") != answer["company_id"]:
            raise ChatError("source_snapshot_mismatch", "the frozen evidence snapshot is absent or out of scope", 409)

        try:
            check_input = assemble_persisted_evidence_input(
                self.chat, answer["run_id"], answer["company_id"],
                expected_snapshot=snapshot)
            events = list(check_input.events)
            indexes = projection_indexes(check_input)
            calculation_result = calculation_records(
                check_input, indexes, WorkspaceCalculationReplay(self.chat.evidence_db, self.chat.decisions)
            )
        except EvidenceCheckError as exc:
            raise ChatError("evidence_check_input_invalid", str(exc), 409) from exc
        calculations = list(calculation_result.records)
        calculation_events = calculation_result.event_by_id
        calculation_limitations = list(calculation_result.limitations)
        indexes["calculations"] = {
            item["calculation_id"]: item for item in calculations
            if item.get("status") == "recorded" and isinstance(item.get("calculation_id"), str)
        }
        citations = answer.get("citations") if isinstance(answer.get("citations"), list) else []
        citation_map = {
            (item.get("kind"), item.get("id")): item for item in citations
            if isinstance(item, dict) and isinstance(item.get("kind"), str)
            and isinstance(item.get("id"), str)
        }

        traces: list[dict] = []
        trace_by_reference: dict[tuple[str, str], str] = {}
        citation_sources: dict[tuple[str, str], dict | None] = {}
        citation_details: dict[tuple[str, str], dict] = {}
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            kind, identifier = citation.get("kind"), citation.get("id")
            if kind not in {"passage", "evidence", "claim", "calculation", "query"} or not isinstance(identifier, str):
                continue
            record_read = read_evidence_record(
                check_input, kind, identifier,
                calculations=calculation_result.records,
                calculation_events=calculation_result.event_by_id,
            )
            event = record_read.event if record_read.state == "available" else None
            original = record_read.record if record_read.state == "available" else None
            verified_source = None
            if citation.get("valid") is True:
                if event is None or original is None:
                    raise ChatError("citation_source_mismatch", "a valid citation lacks an authoritative original tool result", 409)
                # read_citation is a bounded, read-only local verification of the
                # archived bytes. Its structured cells/context may establish linkage;
                # the unverified event quote alone may not.
                if kind in {"passage", "calculation", "query"} or (kind == "evidence" and identifier.startswith("doccell-")):
                    verified_source = self.chat.read_citation(
                        answer["conversation_id"], answer["message_id"], kind, identifier
                    )
                    if kind == "passage":
                        verified_source = self._bounded_passage_source(
                            answer, identifier, verified_source
                        )
            source, trace_limitations = resolve_source(kind, identifier, original, indexes)
            if kind == "query" and isinstance(verified_source, dict):
                source = verified_source
            if isinstance(source, dict) and isinstance(verified_source, dict):
                source = {**source}
                for field in ("context", "offset", "tables", "fact", "interpretation"):
                    if verified_source.get(field) is not None:
                        source[field] = verified_source[field]
                locator = verified_source.get("locator")
                offset = verified_source.get("offset")
                if isinstance(locator, dict) and type(locator.get("start")) is int and type(offset) is int:
                    source["context_start"] = locator["start"] - offset
            trace = self._trace(
                answer, kind, identifier, citation, event, original, source,
                trace_limitations, indexes
            )
            traces.append(trace)
            trace_by_reference[(kind, identifier)] = trace["id"]
            citation_sources[(kind, identifier)] = source
            if kind == "passage" and isinstance(original, dict):
                citation_details[(kind, identifier)] = {
                    "document_version_id": original.get("document_version_id"),
                    "tables": original.get("tables", []),
                }

        calculation_traces = self._calculation_traces(
            answer, calculations, calculation_events, indexes, trace_by_reference
        )
        traces.extend(calculation_traces)
        checked_fact_amounts = frozen_fact_amounts(check_input, indexes)
        if publication is not None:
            from .report_publications import project
            findings, anchors = project(publication, citation_map, trace_by_reference)
            anchor_warnings = []
        else:
            findings = evidence_findings(
                answer["answer"], citation_map, trace_by_reference, citation_sources,
                calculations, checked_fact_amounts, answer["question"]
            )
            anchors, anchor_warnings = self._atomic_anchors(
                answer["answer"], findings, citation_map, trace_by_reference,
                citation_sources, calculations, answer.get("assertion_annotations")
            )
        semantic_events = [event for event in run.get("events", [])
                           if event.get("type") == "semantic_review"
                           and event.get("authoritative") is True
                           and event.get("truncated") is not True
                           and isinstance(event.get("review"), dict)]
        semantic_review = semantic_events[-1]["review"] if semantic_events else {
            "status": "not_assessed", "semantic_verified": False,
            "reason": "authoritative_review_event_unavailable", "claims": [],
            "coverage": None,
        }
        if publication is not None:
            semantic_review = publication["semantic_review"]
        required_verifier_findings = (deepcopy(publication.get("required_verifier_findings", []))
                                      if publication is not None else [])
        verifier_findings_by_node = {}
        for verifier_finding in required_verifier_findings:
            if (isinstance(verifier_finding, dict)
                    and isinstance(verifier_finding.get("node_id"), str)):
                verifier_findings_by_node.setdefault(verifier_finding["node_id"], []).append(
                    verifier_finding)
        mapped_semantic_claims = set()
        finding_ranges = set()
        finding_cursor = 0
        for finding in findings:
            if verifier_findings_by_node.get(finding.get("id")):
                finding["required_verifier_findings"] = deepcopy(
                    verifier_findings_by_node[finding["id"]])
            start = answer["answer"].find(finding["text"], finding_cursor)
            if start < 0:
                finding["semantic_status"] = "not_assessed"
                continue
            end = start + len(finding["text"])
            finding_cursor = end
            finding_ranges.add((start, end, finding["text"]))
            reviews = [claim for claim in semantic_review.get("claims", [])
                       if semantic_review.get("status") == "assessed" and isinstance(claim, dict)
                       and type(claim.get("start")) is int and type(claim.get("end")) is int
                       and 0 <= claim["start"] < claim["end"] <= len(answer["answer"])
                       and answer["answer"][claim["start"]:claim["end"]] == claim.get("exact_text")
                       and claim["start"] < end and claim["end"] > start]
            statuses = {claim.get("support_status") for claim in reviews}
            finding["semantic_status"] = next(
                (status for status in ("conflicting", "unsupported", "inference", "supported_by_evidence")
                 if status in statuses), "not_assessed")
            if finding["semantic_status"] == "supported_by_evidence":
                finding["semantic_status"] = "partial_support"
            for claim in reviews:
                if claim.get("support_status") in {"conflicting", "unsupported"}:
                    finding["review_flags"].append({
                        "code": "semantic_" + claim["support_status"], "severity": "high",
                        "message": claim.get("reason") or "Available evidence cannot support this statement.",
                        "action": "Retain only in the review appendix, not as a conclusion in the main content.",
                    })
                    finding["status"] = "needs_review"
                    finding["label"] = "Needs review"
                    finding["explanation"] = "；".join(flag["message"] for flag in finding["review_flags"])
            if "inference" in statuses and finding["status"] == "source_linked":
                finding["classification"] = "interpretation"
        if semantic_review.get("status") == "assessed":
            from .report_publications import _review_claims_within, _review_summary
            for anchor in anchors:
                matches = [claim for claim in semantic_review.get("claims", [])
                           if isinstance(claim, dict)
                           and type(claim.get("start")) is int and type(claim.get("end")) is int
                           and claim.get("start") == anchor["start"]
                           and claim.get("end") == anchor["end"]
                           and claim.get("exact_text") == anchor["text"]
                           and answer["answer"][anchor["start"]:anchor["end"]] == anchor["text"]]
                scope = "exact"
                if not matches:
                    matches = [claim for claim in semantic_review.get("claims", [])
                               if isinstance(claim, dict)
                               and type(claim.get("start")) is int and type(claim.get("end")) is int
                               and 0 <= claim["start"] <= anchor["start"] < anchor["end"] <= claim["end"] <= len(answer["answer"])
                               and answer["answer"][claim["start"]:claim["end"]] == claim.get("exact_text")]
                    scope = "containing_statement"
                contained = []
                if (anchor.get("kind") == "assertion"
                        and (anchor.get("start"), anchor.get("end"), anchor.get("text")) in finding_ranges):
                    contained = _review_claims_within(
                        answer["answer"], anchor["start"], anchor["end"],
                        semantic_review.get("claims", []))
                if contained and (len(contained) > 1 or len(matches) != 1):
                    anchor["semantic_review"] = _review_summary(contained, assessed=True)
                    anchor["semantic_review"].update({
                        "scope": "contained_claims", "review_scope": "partial",
                        "support_status": "partial_support",
                        "semantic_verified": False,
                    })
                    mapped_semantic_claims.update(claim.get("id") for claim in contained)
                elif len(matches) == 1:
                    claim = matches[0]
                    anchor["semantic_review"] = {
                        "claim_id": claim.get("id"),
                        "support_status": claim.get("support_status"),
                        "evidence_refs": claim.get("evidence_refs", []),
                        "reason": claim.get("reason"),
                        "scope": scope,
                        "reviewed_text": claim.get("exact_text"),
                        "semantic_verified": False,
                    }
                    mapped_semantic_claims.add(claim.get("id"))
        for anchor in anchors:
            node_findings = verifier_findings_by_node.get(anchor.get("finding_id"))
            finding = next((item for item in findings
                            if item.get("id") == anchor.get("finding_id")), None)
            if (node_findings and finding is not None
                    and anchor.get("text") == finding.get("text")):
                anchor["required_verifier_findings"] = deepcopy(node_findings)
        fact_datasets, fact_traces, data_limitations = self._fact_datasets(
            answer, snapshot, events, indexes, trace_by_reference
        )
        traces.extend(fact_traces)
        table_datasets = self._table_datasets(citation_details, trace_by_reference)
        datasets = fact_datasets + table_datasets
        if publication is not None:
            from .report_publications import project_datasets
            datasets = project_datasets(publication, trace_by_reference)
            mapped_semantic_claims.update(_dataset_semantic_claim_ids(datasets))

        limitations = list(_BASE_LIMITATIONS)
        limitations.extend(calculation_limitations)
        limitations.extend(data_limitations)
        if anchor_warnings:
            limitations.append("Model-provided assertion boundaries failed source-range validation and were ignored in favor of conservative deterministic segmentation.")
        if not datasets:
            limitations.append(
                "No eligible structured values or readable tables were obtained in this run, so no chart is generated."
            )
        if any(dataset.get("chart") is None for dataset in datasets):
            limitations.append(
                "Raw tables and incomparable values are displayed only as tables; dimensions and units are not guessed for charting."
            )
        if any(citation.get("valid") is not True for citation in citations if isinstance(citation, dict)):
            limitations.append("At least one original-answer citation failed source-link validation.")
        if semantic_review.get("status") == "assessed" and any(
                claim.get("id") not in mapped_semantic_claims
                for claim in semantic_review.get("claims", []) if isinstance(claim, dict)):
            limitations.append("Some semantic-review ranges did not match the public anchors’ source ranges and were not attached to those anchors.")
        limitations = list(dict.fromkeys(limitations))

        review_flags = [flag for finding in findings for flag in finding["review_flags"]]
        presentation = self._presentation(answer["question"], findings, datasets) if publication is None else {
            "概览": findings[0]["text"] if findings else None, "analysis_purpose": "explicit_report",
            "recommended_dataset_id": datasets[0]["id"] if datasets else None,
            "recommended_dataset_reason": "Display datasets in the order explicitly submitted with the report; do not infer from company or question keywords.",
            "audit_accessible_dataset_ids": [item["id"] for item in datasets],
        }
        for dataset in datasets:
            if verifier_findings_by_node.get(dataset.get("id")):
                dataset["required_verifier_findings"] = deepcopy(
                    verifier_findings_by_node[dataset["id"]])
        audit = self._audit_summary(answer, snapshot, events, traces, run.get("events", []))
        artifact = {
            "schema_version": SCHEMA_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "conversation_id": answer["conversation_id"],
            "message_id": answer["message_id"],
            "run_id": answer["run_id"],
            "company_id": answer["company_id"],
            "execution_scope": answer.get("execution_scope"),
            "question": answer["question"],
            "answer": answer["answer"],
            "verification": {
                "status": "needs_review", "label": "Needs review",
                "semantic_verified": False,
                "semantic_review": semantic_review,
                "limitations": list(_BASE_LIMITATIONS),
            },
            **({"required_verifier_findings": required_verifier_findings}
               if required_verifier_findings else {}),
            "findings": findings,
            "anchor_contract": {
                "schema_version": ANCHOR_SCHEMA_VERSION,
                "range_target": "answer",
                "offset_unit": OFFSET_UNIT,
                "end_exclusive": True,
                "javascript_conversion": "Use utf16_start/utf16_end for String.slice; start/end index Unicode code points.",
                "source_resolution": "Resolve trace_ids against artifact.traces; anchors do not duplicate frozen source payloads.",
                "annotation_warnings": anchor_warnings,
            },
            "anchors": anchors,
            "review_flags": review_flags,
            "calculations": calculations,
            "datasets": datasets,
            "presentation": presentation,
            "audit": audit,
            "traces": traces,
            "limitations": limitations,
        }
        if publication is not None:
            artifact["publication"] = {"schema_version": publication["schema_version"],
                                       "title": publication["title"], "immutable": True,
                                       "binding_method": "explicit_publication_structure"}
        question_snapshot = (self.chat.question_snapshot_for(answer)
                             if isinstance(self.chat, ChatService) else None)
        if question_snapshot is not None:
            artifact["question_snapshot"] = question_snapshot
        artifact["artifact_id"] = _stable_id("artifact", artifact)
        if publication is not None:
            from .report_reader import report_id_for
            artifact["reader_report_id"] = report_id_for(answer["conversation_id"], answer["message_id"])
        return artifact

    def _bounded_passage_source(self, answer, identifier, verified):
        """Expand one already-verified passage locally, with a fixed read bound."""
        documents = (self.chat.documents_for(answer) if isinstance(self.chat, ChatService)
                     else getattr(self.chat, "document_service", None))
        if documents is None or not isinstance(verified, dict):
            return verified
        try:
            expanded = documents.read_passage(
                company_id=verified.get("source_company_id", answer["company_id"]), passage_id=identifier,
                context_chars=2048,
            )
        except DocumentError:
            # read_citation already established the source chain. Failure to obtain
            # optional wider context must make matching conservative, not break history.
            return verified
        identity = ("document_version_id", "source_sha256", "quote", "locator", "parser_version")
        if (expanded.get("company_id") != verified.get("source_company_id", answer["company_id"])
                or any(expanded.get(key) != verified.get(key) for key in identity)):
            raise ChatError("citation_source_mismatch", "expanded passage differs from the verified citation", 409)
        return {**verified, "context": expanded.get("context"),
                "offset": expanded.get("offset"), "tables": expanded.get("tables", [])}

    @staticmethod
    def _step(layer, title, operation, inputs, outputs, details):
        return {
            "layer": layer, "title": title, "operation": operation,
            "inputs": inputs, "outputs": outputs, "details": details,
        }

    def _trace(self, answer, kind, identifier, citation, event, original, source, limitations, indexes):
        valid = citation.get("valid") is True
        marker = f"[{kind}:{identifier}]"
        if kind == "query":
            available = valid and isinstance(source, dict) and source.get("replay_matches") is True
            query = source.get("query", {}) if available else {}
            snapshots = source.get("sources", []) if available else []
            steps = [
                self._step("L4", "Report-to-query-result binding", "persisted_native_query_reference",
                           {"message_id": answer["message_id"], "marker": marker},
                           {"reference_valid": valid}, "The value comes from a registered query cell; the textual explanation still requires review."),
                self._step("L3", "Read-only SQL and execution result", "native_sql_replay" if available else "gap",
                           {key: query.get(key) for key in ("sql", "parameters", "sources")},
                           {key: query.get(key) for key in ("columns", "rows", "row_limit", "truncated", "sqlite_version", "version")},
                           "Replay against the frozen native database and compare complete results; this does not establish exact financial decimals or semantic correctness."),
                self._step("L2", "Native relational structure and processing provenance", "registered_native_schema",
                           {"snapshot_ids": [item["snapshot_id"] for item in snapshots]},
                           {"sources": [{key: item[key] for key in ("snapshot_id", "schema", "origin")} for item in snapshots]},
                           "Preserve original table names, columns, types, and database semantics without converting to a financial-statement fact model."),
                self._step("L1", "Frozen original database", "native_snapshot_sha256",
                           {"sources": [{key: item[key] for key in ("snapshot_id", "source_key", "title")} for item in snapshots]},
                           {"snapshots": [{key: item[key] for key in ("sha256", "byte_length", "revision")} for item in snapshots]},
                           "Check the original database byte digest before and after replay; source attribution does not equal independent approval."),
            ]
            return {"id": _stable_id("trace", answer["message_id"], kind, identifier),
                    "reference": {"kind": kind, "id": identifier},
                    "status": "available" if available else "unavailable", "steps": steps,
                    "source": source, "limitations": list(limitations)}
        if kind == "calculation":
            calculation = indexes.get("calculations", {}).get(identifier)
            if not isinstance(calculation, dict):
                return self._unavailable_calculation_trace(answer, identifier, marker, event, limitations)
            return self._calculation_trace(answer, calculation, event, source, marker in answer["answer"], valid, limitations, indexes)
        steps = [self._step(
            "L4", "Answer-to-evidence binding", "persisted_citation_lookup",
            {"message_id": answer["message_id"], "marker": marker},
            {"present": marker in answer["answer"], "reference_valid": valid},
            "The original answer cited this evidence; this proves only that the link exists, not that the statement is correct.",
        )]
        if event is None:
            steps.append(self._step(
                "L3", "Evidence actually retrieved in this research", "gap", {"reference": {"kind": kind, "id": identifier}},
                {}, "No complete tool record containing this evidence was found; a lineage gap exists.",
            ))
        else:
            steps.append(self._step(
                "L3", "Evidence actually retrieved in this research", "runner_tool_result_lookup",
                {"event_seq": event.get("seq"), "tool": event.get("name"), "arguments": event.get("observed_arguments")},
                {"reference": {"kind": kind, "id": identifier}},
                "Taken from actual tool results in this run; excludes tool echoes produced by the model.",
            ))

        if kind == "passage" and isinstance(original, dict) and original.get("parser_version"):
            steps.append(self._step(
                "L2", "Source parsing and location record", "frozen_passage_parser",
                {"document_version_id": original.get("document_version_id")},
                {"parser_version": original.get("parser_version"), "locator": original.get("locator")},
                "The parser version and source location come from actual retrieval or read results in this run.",
            ))
        elif kind == "evidence" and identifier in indexes.get("document_sources", {}):
            derived = indexes["document_sources"][identifier]
            steps.append(self._step(
                "L2", "Frozen table-cell extraction", "document_table_cell_derivation",
                {"document_version_id": derived["document_version_id"], "locator": derived["locator"]},
                {"fact": derived["fact"], "interpretation": derived["interpretation"]},
                "Extraction activity, original table, and original bytes were rechecked against this run’s record; interpretation of metric basis still requires review.",
            ))
        elif kind in {"evidence", "claim"}:
            item = indexes["evidence"].get(identifier) if kind == "evidence" else indexes["claims"].get(identifier)
            steps.append(self._step(
                "L2", "Frozen evidence record", "snapshot_reference_lookup" if item else "gap",
                {"snapshot_company_id": answer["company_id"]},
                {"reference_id": identifier} if item else {},
                "The evidence link comes from this run’s frozen snapshot; extraction activity is not expanded at this citation." if item
                else "No matching record exists in the materials frozen for this run.",
            ))
        else:
            steps.append(self._step(
                "L2", "Parsing and processing record", "gap", {}, {},
                "This citation has no recoverable parsing or extraction record.",
            ))

        if isinstance(source, dict) and source.get("source_sha256"):
            steps.append(self._step(
                "L1", "Original material and version", "sha256_identity",
                {"source_id": source.get("source_id")},
                {"source_sha256": source.get("source_sha256")},
                "The file digest identifies the original used in this run; opening a passage citation revalidates the original and location.",
            ))
        else:
            steps.append(self._step(
                "L1", "Original material and version", "gap", {}, {},
                "No recoverable digest identifies a single original, so a complete source chain cannot be claimed.",
            ))
        status = "available" if valid and source is not None else "partial" if event is not None else "unavailable"
        return {
            "id": _stable_id("trace", answer["message_id"], kind, identifier),
            "reference": {"kind": kind, "id": identifier}, "status": status,
            "steps": steps, "source": source, "limitations": list(limitations),
        }

    def _unavailable_calculation_trace(self, answer, identifier, marker, event, limitations):
        return {
            "id": _stable_id("trace", answer["message_id"], "calculation", identifier),
            "reference": {"kind": "calculation", "id": identifier}, "status": "unavailable",
            "source": None, "limitations": list(limitations),
            "steps": [
                self._step("L4", "Calculation result in the answer", "persisted_citation_lookup",
                           {"message_id": answer["message_id"], "marker": marker},
                           {"present": marker in answer["answer"], "reference_valid": False},
                           "The answer contains a calculation citation, but no validated authoritative calculation record."),
                self._step("L3", "Calculation call record", "gap", {"event_seq": event.get("seq") if event else None}, {},
                           "No complete, valid calculation inputs, formula, and Decimal output were found."),
                self._step("L2", "Frozen input facts", "gap", {}, {}, "Input facts or operations cannot be inferred from the answer text."),
                self._step("L1", "Input sources", "gap", {}, {}, "No verifiable input source chain exists."),
            ],
        }

    def _calculation_trace(self, answer, calculation, event, source, cited, citation_valid, limitations, indexes):
        identifier = calculation["calculation_id"]
        activities = []
        for fact in calculation["inputs"]:
            activity = indexes["activities"].get(fact.get("activity_id"))
            activities.append({
                "fact_id": fact["fact_id"], "evidence_id": fact["evidence_id"],
                "activity_id": fact.get("activity_id"),
                "activity": activity if isinstance(activity, dict) else None,
            })
        source_rows = source.get("sources", []) if isinstance(source, dict) else []
        return {
            "id": _stable_id("trace", answer["message_id"], "calculation", identifier),
            "reference": {"kind": "calculation", "id": identifier},
            "status": "available" if source_rows and (citation_valid or not cited) else "partial",
            "source": source, "limitations": list(limitations) + [
                "The record establishes that the tool output is traceable; it does not mean the answer’s interpretation passed semantic review."
            ],
            "steps": [
                self._step("L4", "Specific calculation output in the answer", "persisted_calculation_output_lookup",
                           {"message_id": answer["message_id"], "marker": f"[calculation:{identifier}]"},
                           {"present": cited, "value": calculation["value"], "unit": calculation["unit"]},
                           "Bind only this recorded output; do not treat other numbers or the full explanation as verified."),
                self._step("L3", "Actual calculation call", "authoritative_calculation_event",
                           {"event_seq": event.get("seq") if event else None,
                            "call_id": event.get("call_id") if event else None,
                            "verification_receipt_id": event.get("verification_receipt_id") if event else None,
                            "verification_receipt_sha256": event.get("verification_receipt_sha256") if event else None,
                            "tool": event.get("name") if event else None,
                            "arguments": event.get("observed_arguments") if event else None,
                            "input_facts": calculation["inputs"], "formula": calculation["formula"]},
                           {"decimal_value": calculation["value"], "unit": calculation["unit"],
                            "operation": calculation["operation"], "comparison_mode": calculation["comparison_mode"]},
                           "Inputs, formula, and Decimal output come from the authoritative tool event in this run, not from the prose."),
                self._step("L2", "Frozen input facts and processing activities", "snapshot_fact_provenance",
                           {"data_version": calculation["data_version"],
                            **self._calculation_subject(calculation)},
                           {"facts": activities, "evidence_ids": calculation["evidence_ids"]},
                           "Preserve facts, evidence, and actual frozen activities item by item; missing activities are shown as null."),
                self._step("L1", "Original sources for input facts", "sha256_identity_list",
                           {"evidence_ids": calculation["evidence_ids"]},
                           {"sources": source_rows},
                           "Each input source retains a digest and quotation; a calculation citation is not a single-source quotation."),
            ],
        }

    def _calculation_traces(self, answer, calculations, events, indexes, existing):
        traces = []
        for calculation in calculations:
            identifier = calculation.get("calculation_id")
            if calculation.get("status") != "recorded" or not identifier or ("calculation", identifier) in existing:
                continue
            source, limitations = resolve_source("calculation", identifier, calculation, indexes)
            trace = self._calculation_trace(
                answer, calculation, events.get(identifier), source, False, False, limitations, indexes
            )
            traces.append(trace)
            existing[("calculation", identifier)] = trace["id"]
        return traces

    @staticmethod
    def _normalized_statement(value: str) -> str:
        value = _CITATION.sub("", value)
        value = re.sub(r"[`*_#>|]", " ", value)
        return re.sub(r"\s+", " ", value).strip(" .。;；:：\n\t").casefold()

    @staticmethod
    def _basis_mentions(value: str) -> set[str]:
        result = set()
        if re.search(r"(?<!non[- ])\bGAAP\b|公认会计准则", value, re.I):
            result.add("gaap")
        if re.search(r"non[- ]?GAAP|非公认会计准则", value, re.I):
            result.add("non-gaap")
        if re.search(r"重述|重列|recast|restat", value, re.I):
            result.add("recast")
        return result

    @staticmethod
    def _source_table_unit(source, table):
        """Read one table's currency/scale only from its bounded verified preamble."""
        context = source.get("context") if isinstance(source, dict) else None
        context_start = source.get("context_start") if isinstance(source, dict) else None
        locator = table.get("locator") if isinstance(table, dict) else None
        if not isinstance(context, str) or type(context_start) is not int or not isinstance(locator, dict):
            return None
        table_start = locator.get("start")
        if type(table_start) is not int:
            return None
        relative = table_start - context_start
        if not 0 <= relative <= len(context):
            return None
        # Parsed table locators include the title/unit line itself, so inspect a
        # small window across that exact boundary rather than the passage at large.
        heading = context[max(0, relative - 320):min(len(context), relative + 200)]
        scale_match = re.search(
            r"(?i)(?:\(\s*[$¥￥]?\s*in\s+(millions?|billions?|thousands?)(?:\s*,[^)]{0,100})?\s*\)|"
            r"单位\s*[：:]?\s*(百万元|十亿元|亿元|万元))",
            heading,
        )
        if not scale_match:
            return None
        scale_label = next(value for value in scale_match.groups() if value)
        powers = {"million": 6, "billion": 9, "thousand": 3,
                  "百万元": 6, "十亿元": 9, "亿元": 8, "万元": 4}
        power = powers.get(scale_label.lower().rstrip("s"))
        rows = table.get("rows")
        flattened = " ".join(str(cell) for row in rows for cell in row) if isinstance(rows, list) else ""
        currency = "USD" if "$" in flattened or re.search(r"\bUSD\b", heading, re.I) else (
            "CNY" if re.search(r"[¥￥]|\b(?:CNY|RMB)\b|人民币", flattened + heading, re.I) else None
        )
        if currency is None or power is None:
            return None
        return {"currency": currency, "power": power, "header": scale_match.group(0).strip()}

    @staticmethod
    def _table_column_dimensions(rows):
        """Return exact year/period headers for numeric columns in a parsed table."""
        if not isinstance(rows, list) or len(rows) < 2:
            return {}, 0
        header_count = 0
        for row in rows:
            if not isinstance(row, list):
                break
            first = str(row[0]).strip() if row else ""
            unit_header = re.search(r"\bin\s+(?:millions?|billions?|thousands?)\b|单位", first, re.I)
            if first and not unit_header and not _period_kinds(" ".join(str(cell) for cell in row)):
                break
            header_count += 1
        headers = rows[:header_count]
        width = max((len(row) for row in rows), default=0)
        dimensions = {}
        year_columns = []
        for column in range(1, width):
            labels = [str(row[column]).strip() for row in headers
                      if column < len(row) and str(row[column]).strip()]
            years = {int(year) for label in labels
                     for year in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", label)}
            dimensions[column] = {"headers": labels, "years": years, "period_kinds": set()}
            if years:
                year_columns.append(column)
        groups = []
        for row in headers:
            for column, cell in enumerate(row[1:], 1):
                kinds = _period_kinds(str(cell))
                if kinds:
                    groups.append((column, str(cell).strip(), kinds))
        # HTML colspans are flattened with empty placeholders. Associate ordered
        # period groups with equal ordered year-column blocks, never by quote-wide
        # year intersection.
        if groups and year_columns and len(year_columns) % len(groups) == 0:
            block = len(year_columns) // len(groups)
            for index, (_, label, kinds) in enumerate(groups):
                for column in year_columns[index * block:(index + 1) * block]:
                    dimensions[column]["period_kinds"].update(kinds)
                    dimensions[column]["headers"].insert(0, label)
        elif len(groups) == 1 and not year_columns:
            # Transposed tables put years in rows and share one period header.
            _, label, kinds = groups[0]
            for dimension in dimensions.values():
                dimension["period_kinds"].update(kinds)
                if label not in dimension["headers"]:
                    dimension["headers"].insert(0, label)
        return dimensions, header_count

    def _structured_numeric_match(self, candidate, source, value, expected_amount):
        """Match a number to one exact verified source cell and its row/column headers."""
        tables = source.get("tables") if isinstance(source, dict) else None
        if not isinstance(tables, list) or not tables:
            return None
        desired_metrics = _answer_metric_identities(candidate)
        context = candidate.get("context") or candidate.get("text") or ""
        desired_years = {int(year) for year in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", context)}
        desired_periods = _period_kinds(context)
        matches, numeric_cells = [], 0
        for table in tables:
            rows = table.get("rows") if isinstance(table, dict) else None
            dimensions, header_count = self._table_column_dimensions(rows)
            unit = self._source_table_unit(source, table)
            if not isinstance(rows, list):
                continue
            for row_index, row in enumerate(rows[header_count:], header_count):
                if not isinstance(row, list) or not row:
                    continue
                row_header = str(row[0]).strip()
                source_metrics = _source_metric_identities(row_header)
                row_years = {int(year) for year in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", row_header)}
                for column in range(1, len(row)):
                    raw = str(row[column]).strip()
                    parsed = re.fullmatch(r"\(?\s*([$¥￥]?)\s*([+\-−]?\d[\d,]*(?:\.\d+)?)\s*\)?", raw)
                    if not parsed:
                        continue
                    try:
                        cell_value = abs(Decimal(parsed.group(2).replace("−", "-").replace(",", "")))
                    except InvalidOperation:
                        continue
                    if expected_amount is not None:
                        if unit is None:
                            continue
                        source_amount = (unit["currency"], _decimal_scale(cell_value, unit["power"]))
                        numeric_equal = source_amount == (expected_amount[0], abs(expected_amount[1]))
                    else:
                        numeric_equal = cell_value == abs(value)
                    if not numeric_equal:
                        continue
                    numeric_cells += 1
                    column_dimensions = dimensions.get(column, {"headers": [], "years": set(), "period_kinds": set()})
                    cell_metrics = source_metrics
                    cell_years = column_dimensions["years"]
                    cell_periods = column_dimensions["period_kinds"]
                    if row_years and not cell_years:
                        metric_headers = [header for header in column_dimensions["headers"]
                                          if not _period_kinds(header) and not _declared_currency_units(header)]
                        cell_metrics = set().union(*(_source_metric_identities(header) for header in metric_headers))
                        cell_years = row_years
                        cell_periods = cell_periods | _period_kinds(row_header)
                    # Every dimension named by the answer must be established by this
                    # exact cell. Unknown answer metrics cannot borrow a known row.
                    if desired_metrics:
                        if cell_metrics != desired_metrics:
                            continue
                    elif cell_metrics:
                        continue
                    if desired_years and cell_years != desired_years:
                        continue
                    if desired_periods and cell_periods != desired_periods:
                        continue
                    matches.append({
                        "match": "verified_source_table_cell",
                        "decimal": str(value),
                        "base_currency_amount": ([expected_amount[0], format(expected_amount[1], "f")]
                                                 if expected_amount else None),
                        "dimensions": {
                            "metrics": sorted(desired_metrics), "years": sorted(desired_years),
                            "period_kinds": sorted(desired_periods),
                            "basis": sorted(self._basis_mentions(context)),
                        },
                        "table": candidate.get("table_context"),
                        "source_cell": {
                            "table_index": table.get("table_index"), "row_index": row_index,
                            "column_index": column, "row_header": row_header,
                            "column_headers": column_dimensions["headers"], "value": raw,
                            "unit_header": unit["header"] if unit else None,
                        },
                    })
        if len(matches) == 1:
            return True, False, matches[0]
        if len(matches) > 1:
            return True, True, matches[0]
        return (False, False, {"dimension_mismatch": True}) if numeric_cells else (False, False, {})

    def _numeric_anchor_match(self, candidate, source):
        """Return (matched, ambiguous, details) for one frozen local source."""
        quote = source.get("quote") if isinstance(source, dict) else None
        if not isinstance(quote, str):
            return False, False, {}
        try:
            value = Decimal(candidate["numeric_text"])
        except (InvalidOperation, KeyError):
            return False, False, {}
        if not value.is_finite():
            return False, False, {}
        context = candidate.get("context") or candidate["text"]
        percent = bool(re.search(r"%|％|percent|per\s+cent|百分点|percentage\s+points?", candidate["text"], re.I))
        expected_amount = None
        if percent:
            values = {Decimal(raw.replace(",", "")) for raw in re.findall(
                r"([+-]?\d[\d,]*(?:\.\d+)?)\s*(?:%|％|percent\b|per\s+cent\b|个百分点|百分点|percentage\s+points?)",
                quote.replace("−", "-"), re.I)}
            lexical_match = value in values
        else:
            units = _declared_currency_units(context)
            if units and len(units) == 1:
                currency, power = next(iter(units))
                expected_amount = (currency, _decimal_scale(value, power))
                lexical_match = expected_amount in _money_amounts(quote)
                lexical_match = lexical_match or expected_amount in _unitless_quote_amounts(quote, units)
            else:
                lexical_match = abs(value) in _plain_numbers(quote)
        structured = self._structured_numeric_match(candidate, source, value, expected_amount)
        if structured is not None and (structured[0] or structured[2].get("dimension_mismatch")):
            return structured
        if not lexical_match:
            return False, False, {}

        desired_metric_identities = _answer_metric_identities(candidate)
        desired_metrics = {item.split(":", 1)[1] for item in desired_metric_identities
                           if item.startswith("known:")}
        desired_years = {int(year) for year in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", context)}
        desired_periods = _period_kinds(context)
        desired_basis = self._basis_mentions(context)
        source_metrics = _metric_mentions(quote)
        source_years = {int(year) for year in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", quote)}
        source_periods = _period_kinds(quote)
        source_basis = self._basis_mentions(quote)
        # Exact numeric equality is not enough when the assertion names a
        # dimension that the frozen source cannot establish or contradicts.
        checks = ((desired_metrics, source_metrics), (desired_years, source_years),
                  (desired_periods, source_periods), (desired_basis, source_basis))
        if (desired_metric_identities and not desired_metrics) or any(
                wanted and (not available or wanted.isdisjoint(available))
                for wanted, available in checks):
            return False, False, {"dimension_mismatch": True}

        raw_contexts = _local_source_dimensions(quote, {abs(value)})
        compatible = []
        for item in raw_contexts:
            if desired_metrics and item["metrics"] != desired_metrics:
                continue
            if desired_years and item["years"] != desired_years:
                continue
            if desired_periods and item["period_kinds"] != desired_periods:
                continue
            compatible.append(item)
        if not compatible:
            return False, False, {"dimension_mismatch": True}
        signatures = {(tuple(sorted(item["metrics"])), tuple(sorted(item["years"])),
                       tuple(sorted(item["period_kinds"]))) for item in compatible}
        ambiguous = len(signatures) > 1 and not (desired_metrics or desired_years or desired_periods)
        return True, ambiguous, {
            "match": "exact_decimal_with_dimensions",
            "decimal": str(value),
            "base_currency_amount": ([expected_amount[0], format(expected_amount[1], "f")]
                                     if expected_amount else None),
            "dimensions": {
                "metrics": sorted(desired_metrics), "years": sorted(desired_years),
                "period_kinds": sorted(desired_periods), "basis": sorted(desired_basis),
            },
            "table": candidate.get("table_context"),
        }

    @staticmethod
    def _calculation_anchor_matches(candidate, calculation):
        """Match this token, not another output elsewhere in its statement."""
        if candidate.get("kind") != "number":
            return False
        try:
            displayed = Decimal(candidate["numeric_text"])
            calculated = Decimal(calculation["value"])
        except (InvalidOperation, KeyError, TypeError):
            return False
        if not displayed.is_finite() or not calculated.is_finite():
            return False
        unit = str(calculation.get("unit") or "").strip()
        if unit in {"%", "percent", "percentage points"}:
            return bool(re.search(r"%|％|percent|per\s+cent|百分点|percentage\s+points?",
                                  candidate["text"], re.I)) and displayed == calculated
        calculated_amount = _currency_amount(calculation.get("value"), unit)
        declared = _declared_currency_units(candidate.get("context") or "")
        if calculated_amount is not None and declared and len(declared) == 1:
            currency, power = next(iter(declared))
            return calculated_amount == (currency, _decimal_scale(displayed, power))
        return False

    def _atomic_anchors(self, text, findings, citation_map, trace_by_reference,
                        citation_sources, calculations, annotations=None):
        candidates, annotation_warnings = build_candidates(text, findings, annotations)
        finding_by_id = {item["id"]: item for item in findings}
        calculation_by_id = {item.get("calculation_id"): item for item in calculations
                             if item.get("status") == "recorded"}
        anchors = []
        for ordinal, candidate in enumerate(candidates):
            finding_id = candidate.get("finding_id")
            if finding_id not in finding_by_id:
                continue
            finding = finding_by_id[finding_id]
            references = []
            evidence, matched_calculations = [], []
            ambiguous = False
            for kind, identifier in candidate["local_references"]:
                citation = citation_map.get((kind, identifier))
                valid = bool(citation and citation.get("valid") is True)
                reference = {"kind": kind, "id": identifier, "valid": valid}
                references.append(reference)
                if not valid:
                    continue
                trace_id = trace_by_reference.get((kind, identifier))
                if kind == "calculation" and identifier in calculation_by_id:
                    calculation = calculation_by_id[identifier]
                    if self._calculation_anchor_matches(candidate, calculation):
                        matched_calculations.append(identifier)
                        evidence.append({"reference": {"kind": kind, "id": identifier},
                                         "trace_id": trace_id, "match": "recorded_exact_output"})
                    continue
                if candidate["kind"] == "number":
                    matched, is_ambiguous, details = self._numeric_anchor_match(
                        candidate, citation_sources.get((kind, identifier)))
                    ambiguous = ambiguous or is_ambiguous
                    if matched:
                        evidence.append({"reference": {"kind": kind, "id": identifier},
                                         "trace_id": trace_id, **details})
                else:
                    source = citation_sources.get((kind, identifier))
                    quote = source.get("quote") if isinstance(source, dict) else None
                    statement = self._normalized_statement(candidate["text"])
                    if statement and isinstance(quote, str) and statement in self._normalized_statement(quote):
                        evidence.append({"reference": {"kind": kind, "id": identifier},
                                         "trace_id": trace_id, "match": "direct_bounded_statement"})

            warnings = list(finding.get("review_flags") or [])
            blocking = any(item.get("severity") == "high" for item in warnings if isinstance(item, dict))
            valid_refs = [item for item in references if item["valid"]]
            invalid_refs = [item for item in references if not item["valid"]]
            calculation_id = matched_calculations[0] if len(matched_calculations) == 1 else None
            if invalid_refs and not valid_refs:
                support_status, classification = "unavailable", "unresolved"
                explanation = "The local citation is missing or invalid, and legacy history cannot restore this binding from an authoritative run record."
            elif blocking:
                support_status, classification = "unresolved", "unresolved"
                explanation = "The containing statement has a high-risk review flag; a local value match cannot be promoted to support."
            elif len(matched_calculations) > 1 or ambiguous:
                support_status, classification = "ambiguous", "unresolved"
                explanation = "Multiple exact candidates cannot be uniquely disambiguated by local metric, period, or basis."
            elif calculation_id:
                support_status, classification = "calculated", "recorded_calculation"
                explanation = "The local value matches the exact output and unit of the cited authoritative calculation record."
            elif evidence and candidate["kind"] == "number":
                support_status, classification = "supported", "direct_statement"
                explanation = "The local value matches the exact value and verifiable dimensions in the frozen source."
            elif evidence and candidate["kind"] == "assertion":
                support_status, classification = "supported", "direct_statement"
                explanation = "The bounded statement can be found directly in the locally cited frozen source."
            elif valid_refs:
                support_status = "ambiguous" if candidate["kind"] == "assertion" else "unresolved"
                classification = "inference" if candidate["kind"] == "assertion" else "unresolved"
                explanation = ("The citation is local, but the frozen source cannot directly establish this independent statement; at most it is an inference pending review."
                               if candidate["kind"] == "assertion" else
                               "The local citation is valid, but no exact binding matching value, metric, period, unit, and basis was found.")
            elif invalid_refs:
                support_status, classification = "unavailable", "unresolved"
                explanation = "The local citation is missing or invalid, and legacy history cannot restore this binding from an authoritative run record."
            else:
                support_status, classification = "unresolved", "unresolved"
                explanation = "This independent content has no local citation. It remains viewable, but no source chain can be fabricated."
            trace_ids = list(dict.fromkeys(item["trace_id"] for item in evidence if item.get("trace_id")))
            anchor = {
                "id": _stable_id("anchor", finding_id, ordinal, candidate["start"],
                                 candidate["end"], candidate["text"]),
                "finding_id": finding_id,
                "start": candidate["start"], "end": candidate["end"],
                "utf16_start": utf16_offset(text, candidate["start"]),
                "utf16_end": utf16_offset(text, candidate["end"]),
                "text": candidate["text"], "kind": candidate["kind"],
                "support_status": support_status, "classification": classification,
                "references": references, "trace_ids": trace_ids,
                "calculation_id": calculation_id, "warnings": warnings,
                "evidence": evidence, "explanation": explanation,
            }
            # This invariant is intentionally checked at projection time: a span
            # must never survive if future extraction changes its exact text.
            if text[anchor["start"]:anchor["end"]] != anchor["text"]:
                continue
            anchors.append(anchor)
        return anchors, annotation_warnings

    @staticmethod
    def _analysis_purpose(question, findings):
        text = question.lower()
        if any(item.get("classification") == "evidence_gap" for item in findings) or re.search(r"披露|证据|依据|disclos|evidence", text):
            return "evidence_gap"
        if re.search(r"调节|核对|reconcil", text):
            return "reconciliation"
        if re.search(r"趋势|增长|下降|同比|环比|trend|growth|yoy|over time", text):
            return "trend"
        return "comparison"

    def _presentation(self, question, findings, datasets):
        purpose = self._analysis_purpose(question, findings)
        overview = findings[0]["text"] if findings else None
        audit_ids = [item["id"] for item in datasets]
        if not datasets:
            return {
                "概览": overview, "analysis_purpose": purpose,
                "recommended_dataset_id": None,
                "recommended_dataset_reason": "No dataset can be recommended for this run; see audit information for complete limitations.",
                "audit_accessible_dataset_ids": [],
            }
        desired_metrics = _metric_mentions(question)
        desired_period = "quarter" if re.search(r"季度|Q[1-4]", question, re.I) else "year" if re.search(r"年度|全年|财年", question) else None
        requested_years = {int(year) for year in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", question)}
        segment_aliases = {
            "intelligent_cloud": ("智能云", "intelligent cloud"),
            "more_personal_computing": ("更多个人计算", "more personal computing"),
            "productivity_and_business_processes": ("生产力与业务流程", "productivity and business processes"),
        }
        desired_segments = {key for key, words in segment_aliases.items()
                            if any(word in question.lower() for word in words)}
        desired_basis = "non-gaap" if re.search(r"non[- ]?gaap|非公认会计准则|调整后", question, re.I) else "gaap" if re.search(r"(?<!non-)gaap|公认会计准则", question, re.I) else None
        eligible = []
        for position, dataset in enumerate(datasets):
            rows = dataset.get("rows") if isinstance(dataset.get("rows"), list) else []
            if not rows:
                continue
            title = str(dataset.get("title") or "")
            dataset_metrics = _metric_mentions(title)
            # Canonical stored names are preferable to lexical guessing.
            if title in _METRIC_PATTERNS:
                dataset_metrics.add(title)
            if len(desired_metrics) != 1 or dataset_metrics != desired_metrics:
                continue
            kinds = {row.get("period", {}).get("kind") for row in rows if isinstance(row.get("period"), dict)}
            if desired_period and kinds != {desired_period}:
                continue
            years = {int(match.group()) for row in rows for match in
                     [re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", str(row.get("label") or row.get("period", {}).get("end") or ""))] if match}
            if requested_years and not requested_years <= years:
                continue
            scope_text = " ".join(str(dataset.get(key) or "") for key in ("scope", "segment", "geography", "title")).lower()
            dataset_segments = {key for key, words in segment_aliases.items()
                                if any(word in scope_text for word in words) or key in scope_text}
            scoped = bool(dataset.get("scope") or dataset.get("segment") or dataset.get("geography") or dataset_segments)
            if desired_segments:
                if dataset_segments != desired_segments:
                    continue
            elif scoped:
                continue
            basis = str(dataset.get("basis") or (rows[0].get("basis") if rows else "")).lower()
            if desired_basis == "non-gaap" and not re.search(r"non[- ]?gaap|adjust", basis):
                continue
            if desired_basis == "gaap" and ("gaap" not in basis or "non-gaap" in basis):
                continue
            eligible.append((position, dataset))
        if not eligible:
            return {
                "概览": overview, "analysis_purpose": purpose,
                "recommended_dataset_id": None,
                "recommended_dataset_reason": "No dataset simultaneously matches company level or segment, period, metric, basis, and requested year. Retrieved data remains auditable but is not recommended for this question.",
                "audit_accessible_dataset_ids": audit_ids,
            }
        _, selected = eligible[0]
        return {
            "概览": overview, "analysis_purpose": purpose,
            "recommended_dataset_id": selected["id"],
            "recommended_dataset_reason": "The dataset matches the question’s company level or segment, period, metric, basis, and requested year, and retains a frozen-source verification path.",
            "audit_accessible_dataset_ids": audit_ids,
        }

    def _audit_summary(self, answer, snapshot, events, traces, run_events=None):
        frozen = snapshot.get("evidence", {})
        sources = []
        for source in frozen.get("sources", []) if isinstance(frozen.get("sources"), list) else []:
            if isinstance(source, dict) and isinstance(source.get("source_id"), str):
                sources.append({
                    "source_id": source["source_id"], "source_version": source.get("version"),
                    "source_sha256": source.get("sha256"), "title": source.get("title"),
                    "source_url": source.get("url"),
                })
        operations = [{
            "event_seq": event.get("seq"), "call_id": event.get("call_id"),
            "operation": event.get("name"), "input": event.get("observed_arguments"),
            "output": event.get("result"),
        } for event in events]
        if run_events is not None:
            operations = []
            authoritative = [event for event in run_events if event.get("authoritative") is True and event.get("type") in {"tool_call", "tool_result"}]
            calls = [event for event in authoritative if event["type"] == "tool_call"]
            results = [event for event in authoritative if event["type"] == "tool_result"]
            paired = set()
            for call in calls:
                matches = [result for result in results if call.get("call_id") and result.get("call_id") == call["call_id"] and result.get("name") == call.get("name")]
                # Never infer pairing for legacy events without runner-generated IDs.
                result = matches[0] if len(matches) == 1 else None
                if result is not None:
                    paired.add(result["seq"])
                truncated = call.get("truncated") is True or (result or {}).get("truncated") is True
                output = (result or {}).get("result")
                status = "truncated" if truncated else "missing_output" if result is None else "error" if isinstance(output, dict) and output.get("error") else "completed"
                operations.append({"call_event_seq": call.get("seq"), "event_seq": (result or call).get("seq"),
                                   "call_id": call.get("call_id"), "operation": call.get("name"), "status": status,
                                   "input": call.get("arguments"), "output": output,
                                   "call_record": call, "result_record": result})
            for result in results:
                if result.get("seq") not in paired:
                    operations.append({"event_seq": result.get("seq"), "call_id": result.get("call_id"),
                                       "operation": result.get("name"), "status": "unpaired_output", "input": None,
                                       "output": result.get("result"), "result_record": result})
            operations.sort(key=lambda operation: operation.get("call_event_seq", operation.get("event_seq", 0)))
        return {
            "label": "Complete audit information",
            "run_control_events": [event for event in (run_events or []) if event.get("type") not in {"data_snapshot", "data_snapshot_addition", "tool_call", "tool_result", "delta"}],
            "run_id": answer["run_id"], "question": answer["question"],
            "company_id": answer["company_id"], "data_version": snapshot.get("data_version"),
            "execution_scope": answer.get("execution_scope"),
            "frozen_sources": sources,
            "frozen_document_version_ids": list(snapshot.get("documents", {}).get("document_version_ids", [])),
            "frozen_raw_sources": [version for version in snapshot.get("documents", {}).get("coverage", {}).get("documents", [])
                                   if isinstance(version, dict) and version.get("document_version_id") in snapshot.get("documents", {}).get("document_version_ids", [])],
            "authoritative_tool_operations": operations,
            "trace_layers": [{
                "layer": layer, "label": label,
                "trace_ids": [trace["id"] for trace in traces
                              if any(step.get("layer") == layer for step in trace.get("steps", []))],
            } for layer, label in (
                ("L4", "Specific output in the answer"), ("L3", "Actual tool calls and outputs"),
                ("L2", "Frozen facts and processing activities"), ("L1", "Original sources and digests"),
            )],
        }

    def _fact_datasets(self, answer, snapshot, events, indexes, existing_traces):
        facts: dict[str, tuple[dict, dict]] = {}
        limitations: list[str] = []
        for event in events:
            if event.get("name") not in {"get_company_facts", "get_claim_evidence", "calculate_yoy", "calculate_facts"}:
                continue
            result = event["result"]
            if not isinstance(result, dict) or "error" in result:
                limitations.append("Failed retrieval or calculation calls do not constitute fact records; error details remain in the run record.")
                continue
            if result.get("company_id") not in indexes.get("company_ids", [answer["company_id"]]) or result.get("data_version") != snapshot.get("data_version"):
                limitations.append("An authoritative fact result was excluded because its company or data version did not match the frozen run.")
                continue
            if result.get("truncated") is True:
                limitations.append("A frozen get_company_facts result was capped by its tool result limit; only returned facts are represented.")
            returned_facts = result.get("facts", []) if isinstance(result.get("facts"), list) else []
            if event.get("name") == "calculate_yoy":
                returned_facts = [result.get("current_fact"), result.get("prior_fact")]
            elif event.get("name") == "calculate_facts":
                returned_facts = result.get("inputs", []) if isinstance(result.get("inputs"), list) else []
            for fact in returned_facts:
                if not self._eligible_fact(fact, answer["company_id"], indexes):
                    limitations.append("A returned fact was excluded because typed decimal or frozen provenance fields were incomplete.")
                    continue
                fact = dict(fact) if fact["fact_id"] in indexes.get("document_facts", {}) else {key: val for key, val in fact.items() if key != "source_id"}
                fact_id = fact["fact_id"]
                previous = facts.get(fact_id)
                if previous is not None and previous[0] != fact:
                    raise ChatError("fact_snapshot_mismatch", "conflicting frozen values were returned for one fact", 409)
                facts[fact_id] = (fact, event)

        groups: dict[tuple, list[tuple[dict, dict]]] = {}
        for fact, event in facts.values():
            key = (fact["metric"], fact["unit"], fact["period_kind"], fact["basis"],
                   fact.get("scope"), fact.get("segment"), fact.get("geography"))
            groups.setdefault(key, []).append((fact, event))

        datasets, traces = [], []
        for key in sorted(groups, key=lambda value: tuple(str(item or "") for item in value)):
            metric, unit, period_kind, basis, scope, segment, geography = key
            members = sorted(groups[key], key=lambda pair: (
                pair[0].get("period_end") or "", pair[0].get("period_start") or "", pair[0]["fact_id"]
            ))
            rows = []
            for fact, event in members:
                trace_id = _stable_id("trace", answer["message_id"], "fact", fact["fact_id"])
                if ("fact", fact["fact_id"]) not in existing_traces:
                    traces.append(self._fact_trace(answer, fact, event, indexes, trace_id))
                    existing_traces[("fact", fact["fact_id"])] = trace_id
                rows.append({
                    "id": fact["fact_id"], "label": _period_label(fact), "value": fact["value"],
                    "unit": unit, "period": {
                        "start": fact.get("period_start"), "end": fact.get("period_end"),
                        "kind": period_kind,
                    },
                    "basis": basis, "reference": {"kind": "fact", "id": fact["fact_id"]},
                    "trace_id": trace_id,
                })
            comparable = len(rows) >= 2
            dataset_limitations = [
                "These values come from actual retrieval in this run and do not automatically establish any judgment in the original answer."
            ]
            if not comparable:
                dataset_limitations.append("Charting requires at least two data points with the same metric, unit, period type, and basis.")
            datasets.append({
                "id": _stable_id("dataset", "facts", key, [row["id"] for row in rows]),
                "title": metric, "description": f"Retrieved {'quarterly ' if period_kind == 'quarter' else 'annual ' if period_kind == 'year' else period_kind} data in this run; each item retains its original basis.",
                "unit": unit, "basis": basis,
                "scope": scope, "segment": segment, "geography": geography,
                "columns": [{"key": "label", "label": "Period"}, {"key": "value", "label": metric}],
                "rows": rows,
                "chart": {"type": "bar", "x": "label", "y": "value"} if comparable else None,
                "limitations": dataset_limitations,
            })
        return datasets, traces, list(dict.fromkeys(limitations))

    @staticmethod
    def _eligible_fact(fact, company_id, indexes):
        return eligible_fact(fact, company_id, indexes)

    def _fact_trace(self, answer, fact, event, indexes, trace_id):
        source = indexes.get("document_sources", {}).get(fact["evidence_id"])
        if source is not None:
            extraction = indexes["document_events"][fact["fact_id"]]
            trace = self._trace(answer, "evidence", fact["evidence_id"], {"valid": True}, extraction,
                                extraction["result"], source, [], indexes)
            return {**trace, "id": trace_id, "reference": {"kind": "fact", "id": fact["fact_id"]}}
        evidence = indexes["evidence"][fact["evidence_id"]]
        source_item = indexes["sources"][evidence["source_id"]]
        source = {
            "source_id": source_item["source_id"], "document_version_id": None,
            "source_sha256": source_item["sha256"], "source_url": source_item.get("url"),
            "title": source_item.get("title"), "quote": evidence.get("quote"),
            "locator": evidence.get("locator"),
        }
        marker = f"[evidence:{fact['evidence_id']}]"
        cited = marker in answer["answer"]
        activity = indexes["activities"].get(fact.get("activity_id"))
        activity_valid = (isinstance(activity, dict)
                          and fact["fact_id"] in activity.get("outputs", [])
                          and fact["evidence_id"] in activity.get("inputs", []))
        activity_step = self._step(
            "L2", "Deterministic data processing", activity.get("method", "recorded_activity"),
            {"inputs": activity.get("inputs"), "parameters": activity.get("parameters")},
            {"outputs": activity.get("outputs"), "activity_id": activity.get("activity_id")},
            "Process source text using frozen extraction rules; inputs, parameters, and outputs all come from the saved processing record for this run.",
        ) if activity_valid else self._step(
            "L2", "Data-processing record gap", "activity_identity_only",
            {"evidence_id": fact["evidence_id"]}, {"activity_id": fact["activity_id"]},
            "The historical run preserved only the activity identifier, not rules and parameters; current processing records are not substituted.",
        )
        return {
            "id": trace_id, "reference": {"kind": "fact", "id": fact["fact_id"]},
            "status": "available", "source": source,
            "limitations": [
                "This value belongs to this run’s retrieval results; it may not be cited by the original answer and does not automatically support it.",
            ] + ([] if activity_valid else ["The historical run did not freeze processing activity details; an L2 gap exists."]),
            "steps": [
                self._step("L4", "Answer-to-evidence binding", "persisted_evidence_marker_lookup" if cited else "not_bound_to_answer",
                           {"message_id": answer["message_id"], "evidence_id": fact["evidence_id"]},
                           {"present": cited}, "Reading data does not validate judgments in the answer."),
                self._step("L3", "Values read in this research", "runner_tool_result_lookup",
                           {"event_seq": event.get("seq"), "tool": event.get("name"), "arguments": event.get("observed_arguments")},
                           {"fact_id": fact["fact_id"], "value": fact["value"], "unit": fact["unit"]},
                           "The values come from actual tool results in this run and match the frozen data version."),
                activity_step,
                self._step("L1", "Original material and version", "sha256_identity",
                           {"source_id": source_item["source_id"]}, {"source_sha256": source_item["sha256"]},
                           "The digest of the evidence referenced by the fact matches the frozen source."),
            ],
        }

    @staticmethod
    def _table_datasets(details, trace_by_reference):
        datasets = []
        seen = set()
        for (kind, identifier), detail in sorted(details.items()):
            if kind != "passage" or not isinstance(detail.get("tables"), list):
                continue
            for table in detail["tables"]:
                if not isinstance(table, dict) or not isinstance(table.get("rows"), list):
                    continue
                key = (detail.get("document_version_id"), table.get("table_index"))
                if key in seen:
                    continue
                seen.add(key)
                raw_rows = [
                    [str(cell) for cell in row] for row in table["rows"]
                    if isinstance(row, list)
                ]
                width = max((len(row) for row in raw_rows), default=0)
                datasets.append({
                    "id": _stable_id("dataset", "raw_table", key, raw_rows),
                    "title": f"Source table {table.get('table_index')}",
                    "description": "A table parsed from the source with original cells preserved; column semantics are not yet confirmed, so it does not indicate that conclusions are established.",
                    "unit": None,
                    "columns": [{"key": f"column_{index + 1}", "label": f"Column {index + 1}"} for index in range(width)],
                    "rows": [], "raw_rows": raw_rows, "chart": None,
                    "reference": {"kind": "passage", "id": identifier},
                    "trace_id": trace_by_reference.get((kind, identifier)),
                    "limitations": [
                        "Original cells are preserved; headers, dimensions, units, and value types are not inferred.",
                        "Raw tables are not charted automatically because comparable dimensions are unconfirmed.",
                    ],
                })
        return datasets
