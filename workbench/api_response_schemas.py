"""Readable outward HTTP response schemas for route-specific API boundaries.

Versioned artifacts and provider/event payloads deliberately remain opaque only where
called out below; their containing envelopes and identities are still closed.
"""
from __future__ import annotations


def obj(properties, required=(), *, additional=False, description=None):
    schema = {"type": "object", "additionalProperties": additional,
              "properties": properties, "required": list(required)}
    if description:
        schema["description"] = description
    return schema


def arr(items):
    return {"type": "array", "items": items}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
NSTRING = {"type": ["string", "null"]}
OPAQUE = {"description": "Versioned producer-owned JSON value; interpret using its adjacent version discriminator."}
STRINGS = arr(S)
SPAN = obj({"start": I, "end": I}, ("start", "end"))
HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}

COMPANY = obj({"id": S, "name": S, "ticker": S, "description": S},
              ("id", "name", "ticker"))
SOURCE = obj({
    "source_id": S, "title": S, "url": S, "published_at": NSTRING,
    "collected_at": S, "sha256": S, "rights_status": S, "bytes": I,
}, ("source_id", "title", "url", "published_at", "collected_at", "sha256", "rights_status", "bytes"))
CLAIM = obj({
    "claim_id": S, "company_id": S, "text": S, "kind": S,
    "verification_status": S, "explanation": S, "evidence_ids": STRINGS,
    "fact_ids": STRINGS, "activity_ids": STRINGS, "limitations": STRINGS,
}, ("claim_id", "company_id", "text", "kind", "verification_status", "explanation",
    "evidence_ids", "fact_ids", "activity_ids", "limitations"))
FACT = obj({
    "fact_id": S, "company_id": S, "metric": S,
    "value": {"type": ["string", "number", "null"]}, "unit": NSTRING,
    "period_start": NSTRING, "period_end": NSTRING, "period_kind": S, "basis": S,
    "evidence_id": S, "activity_id": NSTRING,
}, ("fact_id", "company_id", "metric", "value", "unit", "period_start", "period_end",
    "period_kind", "basis", "evidence_id", "activity_id"))
LOCATOR = obj({"type": S, "start": I, "end": I, "table_index": I, "row_index": I},
              ("type", "start", "end"), additional=False)
EVIDENCE = obj({"evidence_id": S, "source_id": S, "quote": S, "locator": LOCATOR,
                "source_sha256": S},
               ("evidence_id", "source_id", "quote", "locator", "source_sha256"))
ACTIVITY = obj({"activity_id": S, "type": S, "method": S, "created_at": S,
                "inputs": OPAQUE, "outputs": OPAQUE, "parameters": OPAQUE},
               ("activity_id", "type", "method", "created_at", "inputs", "outputs", "parameters"))

VERSION_SUMMARY = obj({
    "version_id": S, "version_number": I, "parent_version_id": NSTRING,
    "publication_sha256": HASH, "dependency_hash": HASH, "created_at": S,
}, ("version_id", "version_number", "parent_version_id", "publication_sha256", "dependency_hash", "created_at"))
ARTIFACT_REF = obj({"kind": S, "id": S, "sha256": HASH}, ("kind", "id", "sha256"))
NODE_IDENTITY = obj({"node_hash": HASH, "dependency_hash": HASH},
                    ("node_hash", "dependency_hash"))
EVIDENCE_CHAIN = obj({"claim_id": S, "fact_ids": STRINGS, "evidence_ids": STRINGS,
                      "activity_ids": STRINGS, "source_ids": STRINGS},
                     ("claim_id", "fact_ids", "evidence_ids", "activity_ids", "source_ids"))
PASSAGE_PAGE_LOCATOR = obj({"start": I, "end": I, "page": I, "page_start": I,
                            "page_end": I, "page_basis": S},
                           ("start", "end", "page", "page_start", "page_end", "page_basis"))
EVIDENCE_TEXT_LOCATOR = obj({"type": {"const": "text_span"}, "start": I, "end": I},
                            ("type", "start", "end"))
EVIDENCE_PAGE_LOCATOR = obj({"type": {"const": "page_text_span"}, "page": I,
                             "start": I, "end": I, "page_start": I, "page_end": I,
                             "basis": S},
                            ("type", "page", "start", "end", "page_start", "page_end", "basis"))
PASSAGE_CANDIDATE = obj({"passage_id": HASH, "document_version_id": HASH,
                         "company_id": S, "quote": S, "locator": SPAN},
                        ("passage_id", "document_version_id", "company_id", "quote", "locator"))
COMPANY_ID = {"type": "string", "minLength": 1, "maxLength": 100,
              "pattern": r"^\S(?:[\s\S]*\S)?$"}
SHA256 = {"type": "string", "pattern": r"^[0-9a-f]{64}$"}
RESEARCH_EXECUTION_SCOPE = obj({
    "version": {"const": "research-execution-scope/1"},
    "company_ids": {"type": "array", "items": COMPANY_ID,
                    "minItems": 1, "maxItems": 8, "uniqueItems": True},
    "scope_id": {"type": "string", "pattern": r"^scope-[0-9a-f]{64}$"},
    "payload_sha256": SHA256,
}, ("version", "company_ids", "scope_id", "payload_sha256"))

CITATION_COMMON_PROPERTIES = {
    "detail_schema_version": {"const": "citation-detail/1"},
    "company_id": S, "conversation_id": S,
    "execution_scope": RESEARCH_EXECUTION_SCOPE,
    "message_id": S, "id": S, "title": NSTRING,
    "limitations": STRINGS,
}
CITATION_SOURCE_PROPERTIES = {
    "source_url": NSTRING, "published_at": NSTRING, "collected_at": NSTRING,
    "source_sha256": NSTRING, "document_version_id": NSTRING,
    "quote": NSTRING, "context": NSTRING,
    "offset": {"type": ["integer", "null"]},
    "locator": {"$ref": "#/components/schemas/CitationLocator"},
    "parser_version": NSTRING, "rights_status": NSTRING,
    "tables": arr({"$ref": "#/components/schemas/CitationTable"}),
}
CITATION_COMMON_REQUIRED = (
    "detail_schema_version", "company_id", "conversation_id", "execution_scope",
    "message_id", "kind", "id", "title", "limitations",
)
CITATION_SOURCE_REQUIRED = tuple(CITATION_SOURCE_PROPERTIES)
CITATION_DIRECT_SOURCE_PROPERTIES = {
    "source_company_id": S,
    "source_visibility": {"enum": ["public", "private"]},
    "source_scope": {"const": "session_material"},
}
CITATION_OPEN_OBJECT = {
    "type": "object", "additionalProperties": {"$ref": "#/components/schemas/JsonValue"},
}
CITATION_OPEN_ARRAY = arr({"$ref": "#/components/schemas/JsonValue"})


def citation_detail(kind, properties, required=(), *, description):
    return obj({
        **CITATION_COMMON_PROPERTIES, "kind": {"const": kind}, **properties,
    }, (*CITATION_COMMON_REQUIRED, *required), description=description)


EXTRA_RESPONSE_SCHEMAS = {
    "ResearchExecutionScope": RESEARCH_EXECUTION_SCOPE,
    "JsonValue": {"type": ["null", "boolean", "number", "string", "array", "object"],
                  "items": {"$ref": "#/components/schemas/JsonValue"},
                  "additionalProperties": {"$ref": "#/components/schemas/JsonValue"}},
    "CitationLocator": {
        "type": ["object", "null"],
        "additionalProperties": {"$ref": "#/components/schemas/JsonValue"},
        "description": "Producer-defined source locator JSON. The citation envelope names this boundary but does not claim every locator variant is closed.",
    },
    "CitationTable": {
        **CITATION_OPEN_OBJECT,
        "description": "Verified producer table projection JSON; table and cell variants remain producer-defined.",
    },
    "CitationCalculationPayload": {
        **CITATION_OPEN_OBJECT,
        "description": "Recorded deterministic calculation JSON, versioned by its calculation producer.",
    },
    "CitationInputSource": {
        **CITATION_OPEN_OBJECT,
        "description": "Calculation input provenance JSON; legacy evidence and direct-document variants are intentionally not presented as one closed shape.",
    },
    "CitationClaimChain": {
        **CITATION_OPEN_OBJECT,
        "description": "Evidence-chain JSON from the frozen research store; the top-level citation remains closed while chain internals retain their producer contract.",
    },
    "CitationQueryPayload": {
        **CITATION_OPEN_OBJECT,
        "description": "Recorded native SQL result JSON governed by its embedded version field.",
    },
    "CitationQueryDisplay": {
        **CITATION_OPEN_OBJECT,
        "description": "Display-only native query projection; rows and storage classes follow the query producer version.",
    },
    "CitationNativeSource": {
        **CITATION_OPEN_OBJECT,
        "description": "Authorized native snapshot descriptor JSON; mixed source visibility is not inferred at the citation envelope.",
    },
    "CompanyIdentity": COMPANY,
    "EvidenceSource": SOURCE,
    "EvidenceClaim": CLAIM,
    "EvidenceFact": FACT,
    "EvidenceLocator": LOCATOR,
    "EvidenceItem": EVIDENCE,
    "EvidenceActivity": ACTIVITY,
    "LegacyReport": obj({
        "company": COMPANY, "generated_at": S, "exported_at": S,
        "status": {"const": "unreviewed"}, "mode": S,
        "input_hashes": obj({}, additional=HASH),
        "manifest": obj({"schema_version": S, "build_version": S,
                         "source_text_included": {"const": False},
                         "public_redistribution": {"const": False},
                         "snapshot_is_approval": {"const": False}},
                        ("schema_version", "build_version", "source_text_included",
                         "public_redistribution", "snapshot_is_approval")),
        "limitations": STRINGS, "claims": arr(CLAIM), "facts": arr(FACT),
        "sources": arr(SOURCE), "evidence": arr(EVIDENCE), "activities": arr(ACTIVITY),
        "evidence_chains": arr(EVIDENCE_CHAIN),
    }, ("company", "generated_at", "exported_at", "status", "mode", "input_hashes",
        "manifest", "limitations", "claims", "facts", "sources", "evidence",
        "activities", "evidence_chains"),
       description="Closed legacy export envelope. Activity inputs/outputs/parameters are the only opaque members and are governed by manifest.schema_version/build_version."),
    "Overview": obj({
        "mode": S, "companies": arr(COMPANY), "sources": arr(SOURCE),
        "stats": obj({"sources": I, "facts": I, "claims": I}, ("sources", "facts", "claims")),
        "limitations": STRINGS,
        "agent": obj({"enabled": B, "reason": S, "chat_implemented": B, "live_verification": S},
                     ("enabled", "reason", "chat_implemented", "live_verification")),
    }, ("mode", "companies", "sources", "stats", "limitations", "agent")),
    "CompanyEvidence": obj({
        "company": COMPANY, "claims": arr(CLAIM), "facts": arr(FACT),
        "wiki": obj({"version": S, "title": S, "status": S, "mode": S,
                     "sections": arr(obj({"heading": S, "body": S, "claim_ids": STRINGS},
                                         ("heading", "body", "claim_ids")))},
                    ("version", "title", "status", "mode", "sections")),
        "updates": arr(obj({"title": S, "body": S, "severity": S}, ("title", "body", "severity"))),
        "limitations": STRINGS,
    }, ("company", "claims", "facts", "wiki", "updates", "limitations")),
    "EvidenceBundle": obj({"claim": CLAIM, "facts": arr(FACT), "activities": arr(ACTIVITY),
                           "sources": arr(SOURCE), "evidence": arr(EVIDENCE)},
                          ("claim", "facts", "activities", "sources", "evidence")),
    "SourceDetail": obj({"source": SOURCE, "text": S,
                         "tables": arr({"type": "object", "additionalProperties": {"$ref": "#/components/schemas/JsonValue"}}),
                         "parser_version": S, "warning": NSTRING},
                        ("source", "text", "tables", "parser_version", "warning")),
    "ConversationSummary": obj({
        "conversation_id": S, "company_id": S, "title": S, "created_at": S,
        "display_question": S, "display_task_id": S,
        "execution_scope": RESEARCH_EXECUTION_SCOPE,
    }, ("conversation_id", "company_id", "title", "created_at", "execution_scope")),
    "ConversationList": obj({"conversations": arr({"$ref": "#/components/schemas/ConversationSummary"})}, ("conversations",)),
    "ChatRun": obj({
        "run_id": S, "conversation_id": S,
        "status": {"enum": ["queued", "running", "blocked", "completed", "failed", "cancelled"]},
        "events": arr({"type": "object", "required": ["seq", "type"],
                       "properties": {"seq": I, "type": S},
                       "additionalProperties": {"$ref": "#/components/schemas/JsonValue"}}),
        "next_cursor": I, "termination_reason": NSTRING, "termination_message": NSTRING,
        "cancel_requested": B, "error": {"type": "object"}, "decision": {"type": "object"},
    }, ("run_id", "conversation_id", "status", "events", "next_cursor", "termination_reason",
        "termination_message", "cancel_requested")),
    "ChatCancel": obj({"run_id": S, "status": S}, ("run_id", "status")),
    "CitationDetail": {"oneOf": [
        citation_detail("passage", {
            **CITATION_SOURCE_PROPERTIES, **CITATION_DIRECT_SOURCE_PROPERTIES,
            "media_type": NSTRING,
        }, (*CITATION_SOURCE_REQUIRED, "source_company_id", "source_visibility", "media_type"),
            description="Closed passage citation envelope. Locator and table internals are explicitly named producer JSON boundaries."),
        citation_detail("evidence", {
            **CITATION_SOURCE_PROPERTIES, **CITATION_DIRECT_SOURCE_PROPERTIES,
            "fact": {"$ref": "#/components/schemas/JsonValue"},
            "interpretation": {"$ref": "#/components/schemas/JsonValue"},
            "semantic_verified": B,
        }, CITATION_SOURCE_REQUIRED,
            description="Closed evidence citation envelope. Direct document-cell fields are optional because legacy evidence remains valid."),
        citation_detail("claim", {
            **CITATION_SOURCE_PROPERTIES,
            "chain": {"$ref": "#/components/schemas/CitationClaimChain"},
            "source_type": {"const": "existing_research_statement"},
            "support_status": {"const": "not_primary_source"},
        }, (*CITATION_SOURCE_REQUIRED, "chain", "source_type", "support_status"),
            description="Closed derived-claim citation envelope with an explicitly open frozen chain boundary."),
        citation_detail("calculation", {
            **CITATION_SOURCE_PROPERTIES,
            "calculation": {"$ref": "#/components/schemas/CitationCalculationPayload"},
            "formula": S, "value": {"$ref": "#/components/schemas/JsonValue"},
            "unit": {"$ref": "#/components/schemas/JsonValue"},
            "inputs": CITATION_OPEN_ARRAY,
            "input_sources": arr({"$ref": "#/components/schemas/CitationInputSource"}),
            "interpretation": {"const": "unverified"},
            "regression_outputs": CITATION_OPEN_ARRAY,
            "forecast_model": {"$ref": "#/components/schemas/JsonValue"},
            "forecast_steps": {"$ref": "#/components/schemas/JsonValue"},
        }, (*CITATION_SOURCE_REQUIRED, "calculation", "formula", "value", "unit", "inputs",
            "input_sources", "interpretation"),
            description="Closed calculation citation envelope. Recorded calculation, provenance, regression and forecast internals retain named producer JSON boundaries."),
        citation_detail("query", {
            "query": {"$ref": "#/components/schemas/CitationQueryPayload"},
            "replay_matches": {"const": True},
            "display": {"$ref": "#/components/schemas/CitationQueryDisplay"},
            "sources": arr({"$ref": "#/components/schemas/CitationNativeSource"}),
        }, ("query", "replay_matches", "display", "sources"),
            description="Closed native-query citation envelope. Query, display and source descriptor internals are named version-owned JSON boundaries; mixed visibility is not inferred."),
    ]},
    "ResearchTaskList": obj({"tasks": arr({"$ref": "#/components/schemas/ResearchTaskSummary"})}, ("tasks",)),
    "ResearchTaskSummary": obj({
        "task_id": S, "question": S, "created_at": S, "origin": {"enum": ["user", "machine_acceptance"]},
        "current_scope_version": I, "status": S, "code": S, "message": S, "subjects": arr({"type": "object"}),
        "run_status": NSTRING, "period_text": NSTRING, "started_at": NSTRING, "finished_at": NSTRING,
        "answer_status": {"enum": ["answered", "partial", "unanswered"]}, "answer_summary": S,
        "failure_reason": S, "risk_status": {"enum": ["needs_review", "not_checked"]},
        "latest_run": {"type": ["object", "null"]}, "reader_report_id": S,
        "reader_href": S, "reader_title": S,
    }, ("task_id", "question", "created_at", "origin", "current_scope_version", "status", "code", "message",
        "subjects", "run_status", "period_text", "started_at", "finished_at", "answer_status", "answer_summary",
        "failure_reason", "risk_status", "latest_run")),
    "ResearchIntake": obj({"status": S, "code": S, "message": S, "scope": {"type": "object"},
                           "options": arr({"type": "object"}), "coverage": {"oneOf": [
                               {"$ref": "#/components/schemas/SubjectCoverage"},
                               obj({"subjects": {"type": "object", "additionalProperties": {"$ref": "#/components/schemas/SubjectCoverage"}}}, ("subjects",))
                           ]}}, ("status", "code", "message", "scope", "options", "coverage")),
    "AnswerArtifact": obj({
        "schema_version": S, "algorithm_version": S, "conversation_id": S, "message_id": S,
        "run_id": S, "company_id": S, "execution_scope": RESEARCH_EXECUTION_SCOPE, "question": S, "answer": S,
        "verification": OPAQUE, "findings": OPAQUE, "anchor_contract": OPAQUE, "anchors": OPAQUE,
        "review_flags": OPAQUE, "calculations": OPAQUE, "datasets": OPAQUE, "presentation": OPAQUE,
        "audit": OPAQUE, "traces": OPAQUE, "limitations": OPAQUE, "artifact_id": S,
        "publication": OPAQUE, "question_snapshot": OPAQUE, "reader_report_id": S,
    }, ("schema_version", "algorithm_version", "conversation_id", "message_id", "run_id", "company_id",
        "execution_scope", "question", "answer", "verification", "findings", "anchor_contract", "anchors",
        "review_flags", "calculations", "datasets", "presentation", "audit", "traces", "limitations",
        "artifact_id"), description="Closed identity/version envelope; large nested artifact members remain opaque and version-discriminated."),
    "ReportEvidence": obj({"schema_version": S, "report": {"type": "object"}, "anchor": {"type": "object"},
                           "evidence": {"type": "object"}, "frozen_detail": {"type": "object"}},
                          ("schema_version", "report", "anchor", "evidence", "frozen_detail")),
    "ReportSource": obj({"schema_version": S, "report": {"type": "object"}, "source": {"type": "object"},
                         "raw_download_available": B, "raw_download_href": NSTRING,
                         "related_evidence": arr(obj({"anchor_id": S, "claim": S, "trace_id": NSTRING,
                                                     "quote": NSTRING, "context": NSTRING,
                                                     "locator": {"type": ["object", "null"]}},
                                                    ("anchor_id", "claim", "trace_id", "quote", "context", "locator")))},
                        ("schema_version", "report", "source", "raw_download_available", "raw_download_href", "related_evidence")),
    "DocumentPassage": obj({
        "passage_id": HASH, "document_version_id": HASH, "company_id": S, "title": S,
        "source_url": NSTRING, "source_sha256": HASH, "parser_version": S, "quote": S,
        "locator": {"oneOf": [SPAN, PASSAGE_PAGE_LOCATOR]},
        "published_at": NSTRING, "collected_at": NSTRING, "source_key": S,
        "source_version": I, "media_type": S, "rights_status": S,
        "tables": arr(obj({"table_index": I, "locator": SPAN,
                           "rows": arr(arr(S)), "row_spans": arr(arr(I))},
                          ("table_index", "locator", "rows", "row_spans"))),
        "evidence_ref": obj({
            "source": obj({"source_id": S, "version_id": S,
                           "version": {"type": ["integer", "null"]}, "sha256": HASH,
                           "byte_length": I, "scope": {"enum": ["public", "private"]}},
                          ("source_id", "version_id", "version", "sha256", "byte_length", "scope")),
            "locator": {"oneOf": [EVIDENCE_TEXT_LOCATOR, EVIDENCE_PAGE_LOCATOR]},
            "processing_status": {"const": "parsed"}, "parser_version": S,
        }, ("source", "locator", "processing_status", "parser_version")),
        "context": S, "context_locator": SPAN, "offset": I, "truncated": B,
        "context_passages": arr(PASSAGE_CANDIDATE), "citation_guidance": S,
    }, ("passage_id", "document_version_id", "company_id", "title", "source_url",
        "source_sha256", "parser_version", "quote", "locator", "published_at",
        "collected_at", "source_key", "source_version", "media_type", "rights_status",
        "tables", "evidence_ref", "context", "context_locator", "offset", "truncated",
        "context_passages", "citation_guidance")),
    "ResearchDecisionAccepted": obj({"run_id": S, "conversation_id": S,
                                      "status": {"enum": ["queued", "accepted"]}},
                                     ("run_id", "conversation_id", "status")),
    "ResearchDecisionDeclined": obj({"run_id": {"type": "null"}, "conversation_id": S,
                                      "status": {"const": "declined"}},
                                     ("run_id", "conversation_id", "status")),
    "ResearchDecisionAnswer": {"oneOf": [
        {"$ref": "#/components/schemas/ResearchDecisionAccepted"},
        {"$ref": "#/components/schemas/ResearchDecisionDeclined"},
    ]},
    "SampleOptions": obj({
        "mode": {"const": "custom_sample_not_industry"}, "minimum_members": I,
        "companies": arr(COMPANY),
        "metrics": arr(obj({"id": S, "label": S}, ("id", "label"))),
        "period_kinds": arr(obj({"id": S, "label": S}, ("id", "label"))),
        "availability": {"type": "object", "additionalProperties": arr(obj({
            "metric": S, "period_start": NSTRING, "period_end": S, "period_kind": S, "unit": S, "basis": S,
        }, ("metric", "period_start", "period_end", "period_kind", "unit", "basis")))},
        "evidence_snapshot": {"type": "object", "additionalProperties": S}, "notice": S,
    }, ("mode", "minimum_members", "companies", "metrics", "period_kinds", "availability", "evidence_snapshot", "notice")),
    "InitializedStatus": obj({"status": {"const": "initialized"}}, ("status",)),
    "LoggedOutStatus": obj({"status": {"const": "logged_out"}}, ("status",)),
    "VerificationVersionSummary": VERSION_SUMMARY,
    "VerificationContinuation": obj({
        "candidate_id": S, "version_id": S, "previous_verify_job_id": S,
        "verify_job_id": S,
        "status": {"enum": ["queued", "running", "retry_wait", "blocked", "succeeded", "failed", "cancelled"]},
    }, ("candidate_id", "version_id", "previous_verify_job_id", "verify_job_id", "status")),
    "VerificationCandidate": obj({
        "candidate_id": S, "conversation_id": S, "author_run_id": S, "verify_job_id": S,
        "question": S, "scope": {"type": "object"}, "input_snapshot": {"type": "object"},
        "base_message_id": NSTRING, "input_hash": S,
        "status": {"enum": ["queued", "running", "verified", "verified_with_limitations", "unresolved"]},
        "formal_message_id": NSTRING, "semantic_truth_guaranteed": {"const": False},
        "versions": arr(VERSION_SUMMARY),
    }, ("candidate_id", "conversation_id", "author_run_id", "verify_job_id", "question", "scope",
        "input_snapshot", "base_message_id", "input_hash", "status", "formal_message_id",
        "semantic_truth_guaranteed", "versions"),
       description="Closed candidate lifecycle envelope; scope/input_snapshot are version-owned frozen JSON."),
    "VerificationVersion": obj({
        "candidate_id": S, "version_id": S, "version_number": I, "parent_version_id": NSTRING,
        "publication": {"type": "object"}, "publication_sha256": HASH,
        "nodes": obj({}, additional=NODE_IDENTITY), "dependency_hash": HASH,
        "evidence_refs": arr(ARTIFACT_REF), "calculation_refs": arr(ARTIFACT_REF),
    }, ("candidate_id", "version_id", "version_number", "parent_version_id", "publication",
        "publication_sha256", "nodes", "dependency_hash", "evidence_refs", "calculation_refs"),
       description="Closed immutable version envelope. Node identities and input references are fully typed; publication is the opaque structured artifact boundary governed by its embedded schema_version."),
}
