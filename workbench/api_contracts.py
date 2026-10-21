"""Versioned client HTTP contracts shared by routing, validation, and OpenAPI.

This is intentionally a small boundary validator, not a JSON Schema engine.  Deeper
ownership, integrity, and domain validation remains in the application services.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re
from typing import Any

from .session_materials import MAX_UPLOAD_BODY
from .api_response_schemas import EXTRA_RESPONSE_SCHEMAS

API_VERSION = "1.1.0"
OPENAPI_VERSION = "3.1.0"


def field(type_: str = "string", *, required: bool = False, nullable: bool = False,
          enum: tuple[Any, ...] = (), minimum: int | None = None,
          maximum: int | None = None, min_length: int | None = None,
          max_length: int | None = None, pattern: str | None = None,
          default: Any = None, description: str | None = None, items: dict | None = None,
          min_items: int | None = None, max_items: int | None = None) -> dict:
    value = {"type": type_, "required": required, "nullable": nullable}
    for key, item in (("enum", list(enum) if enum else None), ("minimum", minimum),
                      ("maximum", maximum), ("minLength", min_length),
                      ("maxLength", max_length), ("pattern", pattern),
                      ("description", description), ("items", items),
                      ("minItems", min_items), ("maxItems", max_items)):
        if item is not None:
            value[key] = item
    if default is not None:
        value["default"] = default
    return value


ID = lambda required=False: field(required=required, min_length=1, max_length=200)
POSITIVE_INT = lambda required=False: field("integer", required=required, minimum=1, maximum=999_999_999_999)
BOOL_QUERY = lambda default=None: field(required=False, enum=("true", "false"), default=default)


def _get(query: dict[str, dict], *, access="owner_client", category="research_read",
         response="JsonObject", media=("application/json",), summary="",
         identity_sets: tuple[tuple[str, ...], ...] = ()) -> dict:
    return {"query": query, "access": access, "category": category, "response": response,
            "media": media, "summary": summary, "identity_sets": identity_sets,
            "unknown_fields": "reject"}


def _post(body: dict[str, dict], *, access="owner_client", category="client_write",
          response="JsonObject", statuses=(200,), summary="", idempotency="none",
          version_semantics="none", max_body=65536, error_statuses: tuple[int, ...] = ()) -> dict:
    return {"body": body, "access": access, "category": category, "response": response,
            "statuses": statuses, "summary": summary, "idempotency": idempotency,
            "version_semantics": version_semantics, "max_body": max_body,
            "error_statuses": error_statuses, "unknown_fields": "reject"}


# Every /api GET implemented by EvidenceHandler. Static HTML/assets are not API routes.
GET_CONTRACTS: dict[str, dict] = {
    "/api/health": _get({}, access="public_health", category="health", response="Health", summary="Process health"),
    "/api/auth/status": _get({}, access="public_auth", category="authentication", response="AuthStatus", summary="Owner authentication status"),
    "/api/overview": _get({}, response="Overview", summary="Evidence overview"),
    "/api/company": _get({"id": field(required=True, min_length=1, max_length=180)}, response="CompanyEvidence", summary="Company evidence projection"),
    "/api/evidence": _get({"id": field(required=True, min_length=1, max_length=180)}, response="EvidenceBundle", summary="Evidence bundle"),
    "/api/source": _get({"id": field(required=True, min_length=1, max_length=180)}, response="SourceDetail", summary="Source metadata"),
    "/api/report": _get({"company": field(required=True, enum=("MSFT", "AAPL")),
                          "format": field(enum=("json", "html"), default="json")},
                         response="LegacyReport", media=("application/json", "text/html"), summary="Legacy evidence report"),
    "/api/chat/status": _get({}, response="ChatStatus", summary="Configured chat capability"),
    "/api/chat/conversations": _get({"company": ID(), "include_internal": BOOL_QUERY("false")}, response="ConversationList", summary="List conversations"),
    "/api/chat/conversation": _get({"id": ID(True)}, response="Conversation", summary="Read one conversation"),
    "/api/chat/run": _get({"id": ID(True), "after": field("integer", minimum=0, maximum=999_999_999_999, default=0)}, response="ChatRun", summary="Poll a chat run"),
    "/api/chat/citation": _get({"conversation": ID(True), "message": ID(True),
                                 "kind": field(required=True, enum=("passage", "evidence", "claim", "calculation", "query")),
                                 "id": ID(True)}, response="CitationDetail", summary="Read a scoped citation"),
    "/api/research/tasks": _get({"include_internal": BOOL_QUERY("false")}, response="ResearchTaskList", summary="List research tasks"),
    "/api/research/task": _get({"id": ID(True)}, response="ResearchTask", summary="Read a research task"),
    "/api/research/materials": _get({"conversation_id": ID(True)}, response="MaterialList", summary="List private session materials"),
    "/api/research/materials/raw": _get({"conversation_id": ID(True), "document_version_id": ID(True)}, response="Binary", media=("application/octet-stream",), summary="Download an owned session material"),
    "/api/reports": _get({"conversation_id": ID(True)}, response="ReportList", summary="List frozen reader reports"),
    "/api/verifications": _get({"conversation_id": ID(True), "limit": field("integer", minimum=1, maximum=200, default=50)}, response="VerificationCandidateList", summary="List owned verification candidates"),
    "/api/verification/candidate": _get({"conversation_id": ID(True), "candidate_id": ID(True)}, response="VerificationCandidate", summary="Read owned verification candidate", identity_sets=(("conversation_id", "candidate_id"),)),
    "/api/verification/version": _get({"conversation_id": ID(True), "candidate_id": ID(True), "version_id": ID(True)}, response="VerificationVersion", summary="Read an immutable owned candidate version", identity_sets=(("conversation_id", "candidate_id", "version_id"),)),
    "/api/verification/reader": _get({"conversation_id": ID(True), "candidate_id": ID(True), "version_id": ID(True)}, response="ReaderPayload", summary="Project an immutable candidate version into the shared reader", identity_sets=(("conversation_id", "candidate_id", "version_id"),)),
    "/api/research/artifact": _get({"conversation_id": ID(True), "message_id": ID(True)}, response="AnswerArtifact", summary="Read a scoped answer artifact"),
    "/api/reports/reader": _get({"report_id": ID(), "conversation_id": ID(), "message_id": ID()}, response="ReaderPayload", summary="Read a frozen report", identity_sets=(("report_id",), ("conversation_id", "message_id"))),
    "/api/reports/evidence": _get({"report_id": ID(), "conversation_id": ID(), "message_id": ID(), "anchor_id": ID(True)}, response="ReportEvidence", summary="Read evidence for a report anchor", identity_sets=(("report_id", "anchor_id"), ("conversation_id", "message_id", "anchor_id"))),
    "/api/reports/source": _get({"report_id": ID(), "conversation_id": ID(), "message_id": ID(), "source_id": ID(True)}, response="ReportSource", summary="Read a report source", identity_sets=(("report_id", "source_id"), ("conversation_id", "message_id", "source_id"))),
    "/api/reports/raw-source": _get({"report_id": ID(), "conversation_id": ID(), "message_id": ID(), "source_id": ID(True)}, response="Binary", media=("application/octet-stream",), summary="Download a frozen report source", identity_sets=(("report_id", "source_id"), ("conversation_id", "message_id", "source_id"))),
    "/api/research/raw-source": _get({"conversation_id": ID(True), "message_id": ID(True), "source_sha256": field(required=True, pattern="^[0-9a-f]{64}$")}, response="Binary", media=("application/octet-stream",), summary="Download an answer-scoped raw source"),
    "/api/research/native-source": _get({"conversation_id": ID(True), "message_id": ID(True), "snapshot_id": ID(True)}, response="Binary", media=("application/octet-stream",), summary="Download an answer-scoped native snapshot"),
    "/api/research/results": _get({"conversation": ID(True)}, response="ResearchResultList", summary="List drafted research results"),
    "/api/research/result": _get({"id": ID(True), "conversation_id": ID(True), "company_id": ID(True), "version": POSITIVE_INT()}, response="ResearchResult", summary="Read a scoped result version"),
    "/api/research/export": _get({"id": ID(True), "conversation_id": ID(True), "company_id": ID(True), "version": POSITIVE_INT(), "format": field(enum=("html", "json", "csv"), default="json")}, response="Binary", media=("text/html", "application/json", "text/csv"), summary="Export a scoped result version"),
    "/api/wiki/page": _get({"company": ID(True), "version": field("integer", minimum=0, maximum=999_999_999_999)}, response="WikiPage", summary="Read a public-knowledge page"),
    "/api/wiki/changes": _get({"company": ID(True)}, access="maintenance", category="maintenance", response="WikiChangeList", summary="List maintenance changes"),
    "/api/wiki/change": _get({"id": ID(True)}, access="maintenance", category="maintenance", response="WikiChange", summary="Read a maintenance change"),
    "/api/documents/search": _get({"company": ID(True), "query": field(required=True, min_length=1), "limit": field("integer", minimum=1, maximum=50, default=8)}, response="DocumentSearch", summary="Search the document catalog"),
    "/api/documents/passage": _get({"company": ID(True), "id": field(required=True, pattern="^[0-9a-f]{64}$"), "context": field("integer", minimum=0, maximum=10000, default=500)}, response="DocumentPassage", summary="Read a catalog passage"),
    "/api/documents/coverage": _get({"company": ID(True)}, response="DocumentCoverage", summary="Read document coverage"),
    "/api/sample-research/options": _get({}, response="SampleOptions", summary="Read supported sample options"),
    "/api/sample-research/result": _get({"id": ID(True), "version": POSITIVE_INT()}, response="SampleResult", summary="Read a saved sample result"),
    "/api/sample-research/raw-source": _get({"id": ID(True), "version": POSITIVE_INT(True), "company_id": ID(True), "source_sha256": field(required=True, pattern="^[0-9a-f]{64}$")}, response="Binary", media=("application/octet-stream",), summary="Download a sample-version source"),
}

S = lambda required=True, nullable=False, max_length=None: field("string", required=required, nullable=nullable, max_length=max_length)
ARR = lambda required=True, items=None, min_items=None, max_items=None: field(
    "array", required=required, items=items or {}, min_items=min_items, max_items=max_items)
OBJ = lambda required=True: field("object", required=required)

POST_CONTRACTS: dict[str, dict] = {
    "/api/auth/initialize": _post({"bootstrap": S(), "password": S()}, access="public_auth", category="authentication", response="InitializedStatus", summary="Initialize owner authentication", max_body=4096, error_statuses=(409, 429, 503)),
    "/api/auth/login": _post({"password": S()}, access="public_auth", category="authentication", response="LoginStatus", summary="Create an owner session", max_body=4096, error_statuses=(401, 429, 503)),
    "/api/auth/logout": _post({}, access="public_auth", category="authentication", response="LoggedOutStatus", summary="End an owner session", max_body=4096),
    "/api/research/materials": _post({"conversation_id": S(), "filename": S(max_length=240), "content_base64": S(), "allow_model_use": field("boolean", required=True)}, response="MaterialDescriptor", statuses=(201,), summary="Upload an explicitly consented session material", max_body=MAX_UPLOAD_BODY),
    "/api/research/decisions/answer": _post({"decision_id": S(), "selected_ids": ARR(items={"type": "string"}), "notes": S(max_length=4000), "action": field("string", required=True, enum=("accepted", "declined")), "request_id": S()}, response="ResearchDecisionAnswer", statuses=(200, 202), summary="Answer a research confirmation", idempotency="request_id"),
    "/api/chat/conversations": _post({"company_id": S()}, response="Conversation", statuses=(201,), summary="Create a conversation"),
    "/api/chat/messages": _post({"conversation_id": S(), "message": S(), "request_id": S(False), "base_message_id": S(False)}, response="RunAccepted", statuses=(202,), summary="Submit a chat message", idempotency="optional request_id", version_semantics="optional base_message_id report-context precondition"),
    "/api/chat/cancel": _post({"run_id": S()}, response="ChatCancel", summary="Cancel a run"),
    "/api/verification/cancel": _post({"conversation_id": S(), "candidate_id": S()}, response="VerificationCancel", summary="Durably cancel an owned verification job", version_semantics="stable conversation and candidate identities; terminal races return persisted status"),
    "/api/verification/continue": _post({"conversation_id": S(), "candidate_id": S(), "expected_version_id": S(), "request_id": S()}, response="VerificationContinuation", statuses=(202,), summary="Continue a budget-exhausted verification from its latest immutable candidate version", idempotency="request_id", version_semantics="expected_version_id must be the latest immutable candidate version; only budget-exhausted jobs are eligible"),
    "/api/research/intake": _post({"question": S(), "context_conversation_id": S(nullable=True)}, response="ResearchIntake", summary="Resolve research intake"),
    "/api/research/tasks": _post({"question": S(), "context_conversation_id": S(nullable=True), "request_id": S(), "origin": field("string", enum=("user", "machine_acceptance"))}, response="ResearchTask", statuses=(201,), summary="Create an idempotent research task", idempotency="request_id"),
    "/api/research/tasks/retry-intent": _post({"task_id": S(), "expected_scope_version": field("integer", required=True, minimum=1)}, response="ResearchTask", summary="Retry failed intent parsing in place", idempotency="task_id plus expected scope version within one API process; committed next-version replays are durable", version_semantics="expected_scope_version; stale or non-failed scopes return 409"),
    "/api/research/tasks/resolve": _post({"task_id": S(), "expected_scope_version": field("integer", required=True, minimum=1), "company_id": S(nullable=True)}, response="ResearchTask", summary="Resolve task scope", version_semantics="expected_scope_version; stale values return 409"),
    "/api/research/tasks/start": _post({"task_id": S(), "expected_scope_version": field("integer", required=True, minimum=1)}, response="RunAccepted", statuses=(202,), summary="Start a task version", idempotency="task_id plus scope version", version_semantics="expected_scope_version; stale values return 409"),
    "/api/research/results": _post({"conversation_id": S(), "message_id": S(), "title": S(max_length=300), "body": S(max_length=50000), "limitations": ARR(items={"type": "string", "maxLength": 1000}, max_items=50)}, response="ResearchResult", statuses=(201,), summary="Create a human draft result"),
    "/api/research/results/revise": _post({"result_id": S(), "conversation_id": S(), "company_id": S(), "expected_version": field("integer", required=True, minimum=1), "title": S(max_length=300), "body": S(max_length=50000), "limitations": ARR(items={"type": "string", "maxLength": 1000}, max_items=50)}, response="ResearchResult", summary="Append a result version", version_semantics="expected_version; stale values return 409"),
    "/api/wiki/changes": _post({"company_id": S(), "base_version": field("integer", required=True, minimum=0), "title": S(), "statements": ARR(max_items=100, items={"type": "object", "additionalProperties": False, "required": ["kind", "text", "claim_ids"], "properties": {"kind": {"type": "string", "enum": ["evidence_summary", "hypothesis", "open_question"]}, "text": {"type": "string", "minLength": 1, "maxLength": 4000}, "claim_ids": {"type": "array", "maxItems": 50, "items": {"type": "string", "minLength": 1, "maxLength": 200}}}})}, access="maintenance", category="maintenance", response="WikiChange", statuses=(201,), summary="Create a knowledge maintenance change", version_semantics="base_version; stale values return 409"),
    "/api/wiki/review": _post({"change_id": S(), "expected_hash": S(max_length=64), "decision": field("string", required=True, enum=("approve", "reject"))}, access="maintenance", category="maintenance", response="WikiChange", summary="Review a maintenance change", version_semantics="immutable content hash and current base/evidence"),
    "/api/wiki/publish": _post({"change_id": S(), "expected_hash": S(max_length=64)}, access="maintenance", category="maintenance", response="WikiPage", summary="Publish an approved maintenance change", version_semantics="immutable content hash, approval, current base/evidence"),
    "/api/sample-research/results": _post({"company_ids": ARR(items={"type": "string", "maxLength": 80}, min_items=2, max_items=20), "metric": field("string", required=True, enum=("revenue", "operating_income")), "period_kind": field("string", required=True, enum=("quarter", "year")), "start_date": S(), "end_date": S()}, response="SampleResult", statuses=(201,), summary="Create or reuse a deterministic sample result", idempotency="normalized request fingerprint"),
}

ROUTES = {("GET", path): contract for path, contract in GET_CONTRACTS.items()}
ROUTES.update({("POST", path): contract for path, contract in POST_CONTRACTS.items()})


def allowed_fields(method: str, path: str) -> set[str]:
    contract = ROUTES[(method, path)]
    return set(contract.get("query", contract.get("body", {})))


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "integer":
        return type(value) is int
    if expected == "boolean":
        return type(value) is bool
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "number":
        return type(value) in {int, float}
    return True


def _validate_value(name: str, value: Any, spec: dict, *, query: bool) -> None:
    if value is None:
        if spec.get("nullable"):
            return
        raise ValueError(f"{name} cannot be null")
    expected = spec["type"]
    if query and expected == "integer":
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
            raise ValueError(f"{name} must be an unsigned decimal integer")
        comparable: Any = int(value)
    else:
        if not _matches_type(value, expected):
            raise ValueError(f"{name} has the wrong type")
        comparable = value
    if "enum" in spec and comparable not in spec["enum"]:
        raise ValueError(f"{name} is not an allowed value")
    if isinstance(comparable, int) and not isinstance(comparable, bool):
        if "minimum" in spec and comparable < spec["minimum"]:
            raise ValueError(f"{name} is below its minimum")
        if "maximum" in spec and comparable > spec["maximum"]:
            raise ValueError(f"{name} exceeds its maximum")
    if isinstance(comparable, str):
        if len(comparable) < spec.get("minLength", 0) or len(comparable) > spec.get("maxLength", 2**31):
            raise ValueError(f"{name} has invalid length")
        if "pattern" in spec and re.fullmatch(spec["pattern"], comparable) is None:
            raise ValueError(f"{name} has invalid format")
    if isinstance(comparable, list):
        if len(comparable) < spec.get("minItems", 0) or len(comparable) > spec.get("maxItems", 2**31):
            raise ValueError(f"{name} has invalid item count")
        item_spec = spec.get("items", {})
        if item_spec.get("type"):
            for item in comparable:
                if not _matches_type(item, item_spec["type"]):
                    raise ValueError(f"{name} contains an invalid item")
                if isinstance(item, str) and len(item) > item_spec.get("maxLength", 2**31):
                    raise ValueError(f"{name} contains an oversized item")


def validate_query(path: str, params: dict[str, list[str]]) -> None:
    contract = GET_CONTRACTS[path]
    specs = contract["query"]
    if set(params) - set(specs):
        raise ValueError("unsupported parameters")
    if any(len(values) != 1 for values in params.values()):
        raise ValueError("duplicate parameters")
    if contract.get("identity_sets") and tuple(sorted(params)) not in {
            tuple(sorted(group)) for group in contract["identity_sets"]}:
        raise ValueError("invalid parameter combination")
    for name, spec in specs.items():
        if spec.get("required") and name not in params:
            raise ValueError(f"missing {name}")
    for name, values in params.items():
        _validate_value(name, values[0], specs[name], query=True)


def validate_body(path: str, data: Any) -> None:
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    specs = POST_CONTRACTS[path]["body"]
    if set(data) - set(specs):
        raise ValueError("unexpected fields")
    missing = {name for name, spec in specs.items() if spec.get("required") and name not in data}
    if missing:
        raise ValueError("missing fields")
    for name, value in data.items():
        _validate_value(name, value, specs[name], query=False)


def _schema(spec: dict) -> dict:
    result = {key: deepcopy(value) for key, value in spec.items()
              if key not in {"required", "nullable", "default"}}
    if spec.get("nullable"):
        result["type"] = [spec["type"], "null"]
    if "default" in spec:
        result["default"] = spec["default"]
    return result


def _object_schema(fields: dict[str, dict]) -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": {name: _schema(spec) for name, spec in fields.items()},
            "required": [name for name, spec in fields.items() if spec.get("required")]}


def build_openapi() -> dict:
    schemas = {
        "Error": {"type": "object", "properties": {"error": {"type": "string"}, "message": {"type": "string"}}, "required": ["error"]},
        "JsonObject": {"type": "object", "additionalProperties": True, "description": "Route-specific legacy payload; fields not yet fully constrained by this contract."},
        "Binary": {"type": "string", "format": "binary"},
        "Health": {"type": "object", "additionalProperties": False, "properties": {"status": {"const": "ok"}}, "required": ["status"]},
        "AuthStatus": {"type": "object", "additionalProperties": False, "properties": {"authentication": {"enum": ["disabled", "required"]}, "initialized": {"type": "boolean"}, "authenticated": {"type": "boolean"}}, "required": ["authentication", "initialized", "authenticated"]},
        "Status": {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"], "additionalProperties": True},
        "LoginStatus": {"type": "object", "properties": {"status": {"const": "authenticated"}, "expires_at": {"type": "integer"}}, "required": ["status", "expires_at"], "additionalProperties": False},
        "ProviderStatus": {"type": "object", "properties": {"available": {"type": "boolean"}, "reason": {"type": "string"}, "provider": {"type": "string"}}, "required": ["available", "reason"], "additionalProperties": True, "description": "Provider-adapter extension; only declared fields are portable."},
        "ChatStatus": {"type": "object", "additionalProperties": False, "required": ["available", "provider", "blocked", "budgets", "capabilities"], "properties": {"available": {"type": "boolean"}, "provider": {"$ref": "#/components/schemas/ProviderStatus"}, "blocked": {"type": ["object", "null"], "additionalProperties": False, "required": ["cause", "message"], "properties": {"cause": {"const": "provider_unavailable"}, "message": {"type": "string"}}}, "budgets": {"type": "object", "additionalProperties": False, "required": ["max_user_chars", "max_history_chars", "max_output_chars", "max_tool_calls", "max_turns", "max_provider_events", "run_timeout_seconds"], "properties": {**{name: {"type": "integer"} for name in ("max_user_chars", "max_history_chars", "max_output_chars", "max_tool_calls", "max_turns", "max_provider_events")}, "run_timeout_seconds": {"type": "number"}}}, "capabilities": {"type": "object", "additionalProperties": False, "required": ["streaming", "cancellation", "tools", "persistent_history", "report_continuation", "chat_submission_idempotency", "durable_execution", "durable_verify_executor"], "properties": {name: {"type": "boolean"} for name in ("streaming", "cancellation", "tools", "persistent_history", "report_continuation", "chat_submission_idempotency", "durable_execution", "durable_verify_executor")}}}},
        "Conversation": {"type": "object", "additionalProperties": False, "required": ["conversation_id", "company_id", "title", "created_at", "execution_scope", "messages", "decision", "report_versions", "research_scope"], "properties": {"conversation_id": {"type": "string"}, "company_id": {"type": "string"}, "title": {"type": "string"}, "created_at": {"type": "string"}, "display_question": {"type": "string"}, "display_task_id": {"type": "string"}, "execution_scope": {"$ref": "#/components/schemas/ResearchExecutionScope"}, "messages": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["message_id", "role", "content", "created_at"], "properties": {"message_id": {"type": "string"}, "role": {"type": "string"}, "content": {"type": "string"}, "created_at": {"type": "string"}, "run_id": {"type": "string"}, "status": {"type": "string"}, "citations": {"type": "array"}, "verification": {"type": "object"}}}}, "decision": {"type": ["object", "null"], "description": "Persisted decision-protocol variant."}, "report_versions": {"type": "array"}, "research_scope": {"type": "object", "description": "Versioned frozen run-scope projection."}}},
        "ChatRunAccepted": {"type": "object", "additionalProperties": False, "required": ["run_id", "conversation_id", "status"], "properties": {"run_id": {"type": "string"}, "conversation_id": {"type": "string"}, "status": {"enum": ["queued", "running", "blocked", "completed", "failed", "cancelled"]}}},
        "TaskRunAccepted": {"type": "object", "additionalProperties": False, "required": ["task_id", "scope_version", "company_id", "company_ids", "conversation_id", "run_id"], "properties": {"task_id": {"type": "string"}, "scope_version": {"type": "integer"}, "company_id": {"type": "string"}, "company_ids": {"type": "array", "items": {"type": "string"}}, "conversation_id": {"type": "string"}, "run_id": {"type": "string"}}},
        "RunAccepted": {"oneOf": [{"$ref": "#/components/schemas/ChatRunAccepted"}, {"$ref": "#/components/schemas/TaskRunAccepted"}]},
        "ResearchTask": {"type": "object", "additionalProperties": False, "required": ["task_id", "question", "context_conversation_id", "origin", "created_at", "current_scope_version", "status", "code", "message", "scope", "options", "coverage", "runs", "latest_run", "latest_report"], "properties": {"task_id": {"type": "string"}, "question": {"type": "string"}, "context_conversation_id": {"type": ["string", "null"]}, "origin": {"enum": ["user", "machine_acceptance"]}, "created_at": {"type": "string"}, "current_scope_version": {"type": "integer"}, "status": {"type": "string"}, "code": {"type": "string"}, "message": {"type": "string"}, "scope": {"type": "object", "additionalProperties": True, "description": "Structured-intent record governed by its embedded producer version."}, "options": {"type": "array", "items": {"type": "object"}}, "coverage": {"oneOf": [{"$ref": "#/components/schemas/SubjectCoverage"}, {"type": "object", "additionalProperties": False, "required": ["subjects"], "properties": {"subjects": {"type": "object", "additionalProperties": {"$ref": "#/components/schemas/SubjectCoverage"}}}}]}, "runs": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["scope_version", "conversation_id", "run_id", "status", "created_at"], "properties": {"scope_version": {"type": "integer"}, "conversation_id": {"type": "string"}, "run_id": {"type": "string"}, "status": {"type": "string"}, "created_at": {"type": "string"}}}}, "latest_run": {"type": ["object", "null"], "description": "Persisted run/decision summary."}, "latest_report": {"type": ["object", "null"], "description": "Optional reader navigation summary."}}},
        "ResearchResult": {"type": "object", "additionalProperties": False, "required": ["result_id", "version", "conversation_id", "message_id", "company_id", "title", "body", "limitations", "question", "citations", "source_snapshot", "artifact", "artifact_gap", "artifact_context", "created_at", "result_type", "human_review_status", "publication_status", "machine_verification", "warning"], "properties": {"result_id": {"type": "string"}, "version": {"type": "integer"}, "conversation_id": {"type": "string"}, "message_id": {"type": "string"}, "company_id": {"type": "string"}, "title": {"type": "string"}, "body": {"type": "string"}, "limitations": {"type": "array", "items": {"type": "string"}}, "question": {"type": "string"}, "citations": {"type": "array", "items": {"type": "object", "description": "Frozen legacy citation variant."}}, "source_snapshot": {"type": "object", "description": "Frozen source snapshot governed by the artifact schema."}, "artifact": {"type": ["object", "null"], "description": "Versioned frozen answer artifact."}, "artifact_gap": {"type": ["object", "null"]}, "artifact_context": {"type": "object", "additionalProperties": False, "required": ["applies_to", "edits_revalidated", "status"], "properties": {"applies_to": {"const": "original_answer"}, "edits_revalidated": {"const": False}, "status": {"enum": ["frozen", "unavailable"]}}}, "created_at": {"type": "string"}, "result_type": {"const": "human_draft"}, "human_review_status": {"const": "unreviewed"}, "publication_status": {"const": "draft"}, "machine_verification": {"type": "object", "additionalProperties": False, "required": ["status", "scope"], "properties": {"status": {"enum": ["references_checked", "unverified"]}, "scope": {"const": "original_answer_reference_existence_only"}}}, "warning": {"type": "string"}}},
        "WikiStatement": {"type": "object", "additionalProperties": False, "required": ["kind", "text", "claim_ids"], "properties": {"kind": {"enum": ["evidence_summary", "hypothesis", "open_question"]}, "text": {"type": "string"}, "claim_ids": {"type": "array", "items": {"type": "string"}}}},
        "WikiDataVersion": {"type": "object", "additionalProperties": False, "required": ["generated_at", "schema_version", "build_version", "manifest_hash"], "properties": {"generated_at": {"type": "string"}, "schema_version": {"type": "string"}, "build_version": {"type": "string"}, "manifest_hash": {"type": "string"}}},
        "WikiPage": {"type": "object", "additionalProperties": False, "required": ["company_id", "version", "title", "statements", "versions", "freshness_status", "semantic_status", "limitations"], "properties": {"company_id": {"type": "string"}, "version": {"type": "integer"}, "title": {"type": "string"}, "statements": {"type": "array", "items": {"$ref": "#/components/schemas/WikiStatement"}}, "content_hash": {"type": "string"}, "data_version": {"$ref": "#/components/schemas/WikiDataVersion"}, "versions": {"type": "array", "items": {"type": "integer"}}, "freshness_status": {"enum": ["current", "needs_review"]}, "semantic_status": {"const": "unreviewed"}, "limitations": {"type": "array", "items": {"type": "string"}}}},
        "WikiEvent": {"type": "object", "additionalProperties": False, "required": ["type", "detail", "created_at"], "properties": {"type": {"enum": ["created", "approved", "rejected", "stale", "published"]}, "detail": {"type": "object", "additionalProperties": True, "description": "Event-type-specific immutable detail."}, "created_at": {"type": "string"}}},
        "WikiChange": {"type": "object", "additionalProperties": False, "required": ["change_id", "company_id", "base_version", "title", "statements", "content_hash", "data_version", "status", "created_at", "events", "limitations"], "properties": {"change_id": {"type": "string"}, "company_id": {"type": "string"}, "base_version": {"type": "integer"}, "title": {"type": "string"}, "statements": {"type": "array", "items": {"$ref": "#/components/schemas/WikiStatement"}}, "content_hash": {"type": "string"}, "data_version": {"$ref": "#/components/schemas/WikiDataVersion"}, "status": {"enum": ["awaiting_approval", "approved", "rejected", "stale", "applied"]}, "created_at": {"type": "string"}, "events": {"type": "array", "items": {"$ref": "#/components/schemas/WikiEvent"}}, "limitations": {"type": "array", "items": {"type": "string"}}}},
        "WikiChangeList": {"type": "object", "additionalProperties": False, "required": ["changes"], "properties": {"changes": {"type": "array", "items": {"$ref": "#/components/schemas/WikiChange"}}}},
        "SampleResult": {"type": "object", "properties": {"result_id": {"type": "string"}, "version": {"type": "integer"}, "content_hash": {"type": "string"}}, "required": ["result_id", "version", "content_hash"], "additionalProperties": True},
        "ReaderReportFormal": {"type": "object", "additionalProperties": False, "required": ["id", "version_id", "title", "status", "status_label", "conversation_id", "message_id", "run_id", "artifact_id", "source_schema_version"], "properties": {"id": {"type": "string"}, "version_id": {"type": "string"}, "title": {"type": "string"}, "status": {"const": "needs_review"}, "status_label": {"type": "string"}, "conversation_id": {"type": "string"}, "message_id": {"type": "string"}, "run_id": {"type": "string"}, "artifact_id": {"type": "string"}, "source_schema_version": {"type": "string"}}},
        "ReaderReportCandidate": {"type": "object", "additionalProperties": False, "required": ["id", "version_id", "title", "status", "status_label", "kind", "conversation_id", "message_id", "run_id", "artifact_id", "source_schema_version", "candidate_id", "candidate_version_id", "formal_message_id"], "properties": {"id": {"type": "string"}, "version_id": {"type": "string"}, "title": {"type": "string"}, "status": {"type": "string"}, "status_label": {"type": "string"}, "kind": {"const": "verification_candidate"}, "conversation_id": {"type": "string"}, "message_id": {"type": "null"}, "run_id": {"type": "string"}, "artifact_id": {"type": "null"}, "source_schema_version": {"type": ["string", "null"]}, "candidate_id": {"type": "string"}, "candidate_version_id": {"type": "string"}, "formal_message_id": {"type": ["string", "null"]}}},
        "RequiredVerifierReceipt": {"type": "object", "additionalProperties": False, "required": ["receipt_id", "sha256"], "properties": {"receipt_id": {"type": "string"}, "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"}}},
        "RequiredVerifierFinding": {"type": "object", "additionalProperties": False, "required": ["node_id", "check_kind", "outcome", "outcome_label", "explanation", "receipts"], "properties": {"node_id": {"type": "string"}, "check_kind": {"type": "string"}, "outcome": {"enum": ["supported", "corrected_and_rechecked", "conflict_disclosed", "insufficient_material_disclosed"]}, "outcome_label": {"type": "string"}, "explanation": {"type": "string"}, "receipts": {"type": "array", "items": {"$ref": "#/components/schemas/RequiredVerifierReceipt"}}}},
        "ReaderPayload": {"type": "object", "additionalProperties": False, "required": ["schema_version", "report", "question", "chapters", "anchors", "evidence", "sources", "audit", "payload_sha256"], "properties": {"schema_version": {"const": "report-reader-payload/1"}, "report": {"oneOf": [{"$ref": "#/components/schemas/ReaderReportFormal"}, {"$ref": "#/components/schemas/ReaderReportCandidate"}]}, "question": {"type": "object", "description": "Versioned frozen question projection."}, "chapters": {"type": "array", "items": {"type": "object", "additionalProperties": False, "required": ["id", "title", "blocks"], "properties": {"id": {"type": "string"}, "title": {"type": "string"}, "blocks": {"type": "array", "items": {"type": "object", "required": ["id", "type"], "description": "Discriminated renderer block with producer-versioned display fields."}}}}}, "anchors": {"type": "array", "items": {"type": "object"}}, "evidence": {"type": "object", "description": "Map of immutable versioned evidence bundles."}, "sources": {"type": "array", "items": {"type": "object"}}, "required_verifier_findings": {"type": "array", "items": {"$ref": "#/components/schemas/RequiredVerifierFinding"}, "description": "Optional future-only projection of exact persisted effective required-node findings; absent from legacy frozen payloads."}, "audit": {"type": "object", "additionalProperties": False, "required": ["available", "trace_count", "calculation_count", "dataset_count", "semantic_review"], "properties": {"available": {"type": "boolean"}, "trace_count": {"type": "integer"}, "calculation_count": {"type": "integer"}, "dataset_count": {"type": "integer"}, "semantic_review": {"type": "object"}, "verification_pending": {"type": "boolean"}}}, "payload_sha256": {"type": "string"}}, "description": "Frozen formal report or read-only verification candidate projection rendered by the same reader."},
        "VerificationCandidate": {"type": "object", "additionalProperties": True, "required": ["candidate_id", "conversation_id", "author_run_id", "status", "versions", "semantic_truth_guaranteed"], "properties": {"candidate_id": {"type": "string"}, "conversation_id": {"type": "string"}, "author_run_id": {"type": "string"}, "status": {"enum": ["queued", "running", "verified", "verified_with_limitations", "unresolved"]}, "formal_message_id": {"type": ["string", "null"]}, "versions": {"type": "array"}, "semantic_truth_guaranteed": {"const": False}}},
        "VerificationCandidateList": {"type": "object", "additionalProperties": False, "required": ["conversation_id", "candidates"], "properties": {"conversation_id": {"type": "string"}, "candidates": {"type": "array", "items": {"$ref": "#/components/schemas/VerificationCandidate"}}}},
        "VerificationVersion": {"type": "object", "additionalProperties": True, "required": ["candidate_id", "version_id", "version_number", "publication", "publication_sha256", "evidence_refs", "calculation_refs"], "properties": {"candidate_id": {"type": "string"}, "version_id": {"type": "string"}, "version_number": {"type": "integer"}, "publication": {"type": "object"}, "publication_sha256": {"type": "string"}, "evidence_refs": {"type": "array"}, "calculation_refs": {"type": "array"}}},
        "VerificationCancel": {"type": "object", "additionalProperties": False, "required": ["candidate_id", "status"], "properties": {"candidate_id": {"type": "string"}, "status": {"enum": ["succeeded", "failed", "cancelled", "superseded"]}}},
        "ReportList": {"type": "object", "additionalProperties": True, "required": ["schema_version", "conversation_id", "company_id", "reports", "skipped_reports"], "properties": {
            "schema_version": {"const": "report-index/1"}, "conversation_id": {"type": "string"},
            "company_id": {"type": "string"}, "reports": {"type": "array", "items": {
                "type": "object", "additionalProperties": True,
                "required": ["report_id", "version", "version_id", "conversation_id", "message_id", "run_id", "status", "publication_schema", "payload_sha256", "reader_href"],
                "properties": {"report_id": {"type": "string"}, "version": {"type": "integer"},
                    "version_id": {"type": "string"}, "conversation_id": {"type": "string"},
                    "message_id": {"type": "string"}, "run_id": {"type": "string"},
                    "status": {"enum": ["needs_review"]}, "publication_schema": {"type": "string"},
                    "payload_sha256": {"type": "string"}, "reader_href": {"type": "string"}}}},
            "skipped_reports": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                "required": ["message_id", "reason"], "properties": {"message_id": {"type": "string"}, "reason": {"type": "string"}}}}}},
        "ResearchResultList": {"type": "object", "additionalProperties": False,
            "required": ["conversation_id", "results"], "properties": {
                "conversation_id": {"type": "string"}, "results": {"type": "array", "items": {
                    "type": "object", "additionalProperties": True,
                    "required": ["result_id", "version", "conversation_id", "message_id", "company_id", "result_type", "human_review_status", "publication_status", "machine_verification"],
                    "properties": {"result_id": {"type": "string"}, "version": {"type": "integer"},
                        "conversation_id": {"type": "string"}, "message_id": {"type": "string"},
                        "company_id": {"type": "string"}, "result_type": {"const": "human_draft"},
                        "human_review_status": {"const": "unreviewed"}, "publication_status": {"const": "draft"},
                        "machine_verification": {"type": "object", "additionalProperties": False,
                            "required": ["status", "scope"], "properties": {
                                "status": {"enum": ["references_checked", "unverified"]},
                                "scope": {"const": "original_answer_reference_existence_only"}}}}}}}},
        "DocumentDescriptor": {"type": "object", "additionalProperties": True,
            "required": ["document_version_id", "source_key", "source_version", "title", "source_sha256", "media_type", "rights_status", "parse_status"],
            "properties": {"document_version_id": {"type": "string"}, "source_key": {"type": "string"},
                "source_version": {"type": "integer"}, "title": {"type": "string"},
                "source_sha256": {"type": "string"}, "media_type": {"type": "string"},
                "rights_status": {"type": "string"},
                "parse_status": {"enum": ["parsed", "parse_failed", "unsupported_pdf"]}}},
        "DocumentCoverage": {"type": "object", "additionalProperties": False,
            "required": ["enrolled", "parsed", "failed", "unsupported", "documents"],
            "properties": {"enrolled": {"type": "integer"}, "parsed": {"type": "integer"},
                "failed": {"type": "integer"}, "unsupported": {"type": "integer"},
                "documents": {"type": "array", "items": {"$ref": "#/components/schemas/DocumentDescriptor"}}}},
        "SubjectCoverage": {"type": "object", "additionalProperties": False,
            "required": ["facts", "documents"], "properties": {
                "facts": {"type": ["object", "null"]},
                "documents": {"type": ["object", "null"]}}},
        "DocumentSearch": {"type": "object", "additionalProperties": False,
            "required": ["results", "coverage", "expanded_query", "interpretation", "version_scope", "as_of"],
            "properties": {"results": {"type": "array", "items": {"type": "object", "additionalProperties": True,
                    "required": ["passage_id", "document_version_id", "company_id", "source_key", "source_version", "source_sha256", "rights_status", "quote", "locator"],
                    "properties": {"passage_id": {"type": "string"}, "document_version_id": {"type": "string"},
                        "company_id": {"type": "string"}, "source_key": {"type": "string"},
                        "source_version": {"type": "integer"}, "source_sha256": {"type": "string"},
                        "rights_status": {"type": "string"}, "quote": {"type": "string"},
                        "locator": {"type": "object", "additionalProperties": True, "required": ["start", "end"],
                            "properties": {"start": {"type": "integer"}, "end": {"type": "integer"}}}}}},
                "coverage": {"$ref": "#/components/schemas/DocumentCoverage"},
                "expanded_query": {"type": "object"}, "interpretation": {"type": "string"},
                "version_scope": {"enum": ["explicit", "current_per_source"]}, "as_of": {"type": ["string", "null"]}}},
        "MaterialDescriptor": {"type": "object", "additionalProperties": True,
            "required": ["document_version_id", "source_key", "source_version", "title", "source_sha256", "media_type", "rights_status", "parse_status", "conversation_id", "source_scope"],
            "properties": {"document_version_id": {"type": "string"}, "source_key": {"type": "string"},
                "source_version": {"type": "integer"}, "title": {"type": "string"},
                "source_sha256": {"type": "string"}, "media_type": {"type": "string"},
                "rights_status": {"type": "string"}, "parse_status": {"enum": ["parsed", "parse_failed", "unsupported_pdf"]},
                "conversation_id": {"type": "string"}, "source_scope": {"const": "session_material"}}},
        "MaterialList": {"type": "object", "additionalProperties": False,
            "required": ["conversation_id", "materials", "supported_upload_types", "max_upload_bytes", "limitations"],
            "properties": {"conversation_id": {"type": "string"},
                "materials": {"type": "array", "items": {"$ref": "#/components/schemas/MaterialDescriptor"}},
                "supported_upload_types": {"type": "array", "items": {"type": "string"}},
                "max_upload_bytes": {"type": "integer"}, "limitations": {"type": "array", "items": {"type": "string"}}}},
    }
    schemas.update(deepcopy(EXTRA_RESPONSE_SCHEMAS))
    paths: dict[str, dict] = {}
    for (method, path), contract in sorted(ROUTES.items()):
        operation: dict[str, Any] = {
            "operationId": method.lower() + "_" + path.strip("/").replace("/", "_").replace("-", "_"),
            "summary": contract["summary"],
            "tags": [contract["category"]],
            "x-access-category": contract["access"],
            "x-unknown-fields": contract["unknown_fields"],
            "x-idempotency": contract.get("idempotency", "safe read" if method == "GET" else "none"),
            "x-version-semantics": contract.get("version_semantics", "none"),
            "x-response-schema-completeness": (
                "complete" if contract["response"] in {
                    "Health", "AuthStatus", "InitializedStatus", "LoginStatus", "LoggedOutStatus", "Binary",
                    "WikiPage", "WikiChange", "WikiChangeList", "Overview", "CompanyEvidence",
                    "EvidenceBundle", "SourceDetail", "ConversationList", "ResearchTaskList",
                    "SampleOptions", "ChatCancel", "VerificationCancel", "LegacyReport",
                    "DocumentPassage", "ResearchDecisionAnswer",
                }
                else "nested-critical-fields" if contract["response"] in {
                    "ChatStatus", "Conversation", "ChatRun", "CitationDetail", "RunAccepted", "ResearchTask",
                    "ResearchIntake", "ResearchResult", "AnswerArtifact", "ReaderPayload", "ReportEvidence", "ReportSource",
                    "ReportList", "ResearchResultList", "DocumentCoverage", "DocumentSearch",
                    "MaterialDescriptor", "MaterialList", "VerificationCandidate", "VerificationCandidateList",
                    "VerificationVersion",
                }
                else "top-level-critical-fields"
            ),
            "security": [] if contract["access"] in {"public_health", "public_auth"}
                        else [{"ownerSession": []}],
        }
        if method == "GET":
            operation["parameters"] = [{"name": name, "in": "query", "required": spec.get("required", False), "schema": _schema(spec)} for name, spec in contract["query"].items()]
            if contract.get("identity_sets"):
                operation["x-exact-query-field-sets"] = [list(group) for group in contract["identity_sets"]]
            statuses = (200,)
        else:
            operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": _object_schema(contract["body"])}}}
            operation["x-max-body-bytes"] = contract["max_body"]
            statuses = contract["statuses"]
        content = {media: {"schema": {"$ref": f"#/components/schemas/{contract['response']}"}} for media in contract.get("media", ("application/json",))}
        operation["responses"] = {str(status): {"description": "Successful response", "content": content} for status in statuses}
        errors = {"400": "Invalid request", "403": "Host, origin, or CSRF check failed"}
        if method == "GET":
            errors["414"] = "URI too long"
        else:
            errors.update({"408": "Request body timeout", "413": "Body too large",
                           "415": "Unsupported media type"})
        if contract["access"] in {"owner_client", "maintenance"}:
            errors.update({"401": "Authentication required", "404": "Scoped object not found",
                           "409": "State, idempotency, or version conflict",
                           "503": "Dependency unavailable or integrity failure"})
        labels = {401: "Authentication failed", 409: "State conflict", 429: "Rate limited",
                  503: "Authentication store unavailable"}
        for error_status in contract.get("error_statuses", ()):
            errors[str(error_status)] = labels[error_status]
        operation["responses"].update({code: {"description": label, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}} for code, label in errors.items()})
        paths.setdefault(path, {})[method.lower()] = operation
    partial = [{"method": method, "path": path, "schema": contract["response"],
                "coverage": paths[path][method.lower()]["x-response-schema-completeness"]}
               for (method, path), contract in sorted(ROUTES.items())
               if paths[path][method.lower()]["x-response-schema-completeness"] != "complete"]
    return {"openapi": OPENAPI_VERSION, "info": {"title": "TrustStack owner client HTTP API", "version": API_VERSION, "description": "Client-facing owner API, not an anonymous public Internet API. Model tool APIs are separate and not part of this document. Owner authentication may be disabled only by explicit development configuration."}, "x-partial-response-contracts": partial, "paths": paths, "components": {"schemas": schemas, "securitySchemes": {"ownerSession": {"type": "apiKey", "in": "cookie", "name": "workbench_session", "description": "Owner session cookie used when authentication is enabled."}}}}


def _validate_response_schema(value: Any, schema: dict, schemas: dict[str, dict], location: str) -> None:
    if "oneOf" in schema:
        matches = 0
        for variant in schema["oneOf"]:
            try:
                _validate_response_schema(value, variant, schemas, location)
            except RuntimeError:
                continue
            matches += 1
        if matches != 1:
            raise RuntimeError(f"HTTP contract response has invalid {location} variant")
        return
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        _validate_response_schema(value, schemas[name], schemas, location)
        return
    if "const" in schema and value != schema["const"]:
        raise RuntimeError(f"HTTP contract response has invalid {location}")
    if "enum" in schema and value not in schema["enum"]:
        raise RuntimeError(f"HTTP contract response has invalid {location}")
    expected = schema.get("type")
    if expected:
        expected_types = expected if isinstance(expected, list) else [expected]
        type_ok = any(item == "null" and value is None or
                      item != "null" and _matches_type(value, item)
                      for item in expected_types)
        if not type_ok:
            raise RuntimeError(f"HTTP contract response has mistyped {location}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise RuntimeError(f"HTTP contract response has short {location}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise RuntimeError(f"HTTP contract response has long {location}")
        if "pattern" in schema and re.fullmatch(schema["pattern"], value) is None:
            raise RuntimeError(f"HTTP contract response has invalid {location} format")
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                raise RuntimeError(f"HTTP contract response lacks {location}.{name}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(properties)
            if extra:
                raise RuntimeError(f"HTTP contract response has unexpected {location} field")
        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            for name in set(value) - set(properties):
                _validate_response_schema(value[name], additional, schemas, f"{location}.{name}")
        for name, property_schema in properties.items():
            if name in value:
                _validate_response_schema(value[name], property_schema, schemas, f"{location}.{name}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise RuntimeError(f"HTTP contract response has too few {location} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise RuntimeError(f"HTTP contract response has too many {location} items")
        if schema.get("uniqueItems") and len({json.dumps(item, sort_keys=True) for item in value}) != len(value):
            raise RuntimeError(f"HTTP contract response has duplicate {location} items")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate_response_schema(item, schema["items"], schemas, f"{location}[{index}]")


def validate_response(method: str, path: str, status: int, payload: Any) -> None:
    """Validate declared success statuses and safe JSON errors before emission.

    Variable legacy/SQL payload members remain unconstrained where their schema says
    so; nested identity and lifecycle fields are recursively enforced when declared.
    """
    spec = build_openapi()
    schemas = spec["components"]["schemas"]
    contract = ROUTES.get((method, path))
    if status >= 300:
        _validate_response_schema(payload, schemas["Error"], schemas, f"{method} {path} error")
        return
    if status < 200 or contract is None:
        raise RuntimeError(f"HTTP contract response has undeclared status {status} for {method} {path}")
    statuses = (200,) if method == "GET" else contract["statuses"]
    if status not in statuses:
        raise RuntimeError(f"HTTP contract response has undeclared status {status} for {method} {path}")
    if contract["response"] != "Binary":
        _validate_response_schema(payload, schemas[contract["response"]], schemas, f"{method} {path}")


def write_openapi(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(build_openapi(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
