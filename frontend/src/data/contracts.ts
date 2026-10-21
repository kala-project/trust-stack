import type {
  Conversation,
  ConversationMessage,
  Decision,
  DecisionGroup,
  DecisionKind,
  DecisionOption,
  DecisionStatus,
  ExecutionScope,
  ReportCorrection,
  ReportEvidenceReference,
  ReportVersion,
} from "./types";

const SCOPE_FIELDS = ["company_ids", "payload_sha256", "scope_id", "version"];
const CONVERSATION_REQUIRED_FIELDS = [
  "company_id",
  "conversation_id",
  "created_at",
  "decision",
  "execution_scope",
  "messages",
  "report_versions",
  "research_scope",
  "title",
];
const CONVERSATION_OPTIONAL_FIELDS = ["display_question", "display_task_id"];
const MESSAGE_REQUIRED_FIELDS = ["content", "created_at", "message_id", "role"];
const MESSAGE_OPTIONAL_FIELDS = ["citations", "run_id", "status", "verification"];
const DECISION_FIELDS = [
  "base_message_id",
  "continuation_run_id",
  "created_at",
  "decision_id",
  "explanation",
  "groups",
  "kind",
  "multiple",
  "notes",
  "options",
  "question",
  "request_message",
  "run_id",
  "selected_ids",
  "status",
];
const OPTION_FIELDS = ["benefit", "id", "label", "limitation"];
const GROUP_FIELDS = ["id", "label", "maximum", "minimum", "option_ids"];
const REPORT_VERSION_FIELDS = ["corrected_by", "corrections", "created_at", "message_id", "run_id", "version"];
const CORRECTION_FIELDS = [
  "correction_id",
  "created_at",
  "end",
  "evidence_refs",
  "explanation",
  "impact",
  "new_message_id",
  "new_version",
  "old_message_id",
  "old_version",
  "original_text",
  "semantic_verified",
  "start",
];
const EVIDENCE_REFERENCE_FIELDS = ["id", "kind"];
const SHA256 = /^[0-9a-f]{64}$/;

function invalidConversation(): never {
  throw new Error("API returned an invalid conversation");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactFields(
  value: Record<string, unknown>,
  required: readonly string[],
  optional: readonly string[] = [],
): boolean {
  const keys = Object.keys(value);
  const allowed = new Set([...required, ...optional]);
  return required.every((field) => Object.hasOwn(value, field))
    && keys.every((field) => allowed.has(field));
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item): item is string => typeof item === "string");
}

function isDecisionKind(value: unknown): value is DecisionKind {
  return value === "basis" || value === "model" || value === "coverage" || value === "time" || value === "other";
}

function isDecisionStatus(value: unknown): value is DecisionStatus {
  return value === "pending" || value === "accepted" || value === "declined";
}

export function decodeExecutionScope(value: unknown): ExecutionScope {
  if (!isRecord(value) || !hasExactFields(value, SCOPE_FIELDS)) {
    throw new Error("API returned an invalid execution scope");
  }
  const companyIds = value.company_ids;
  if (!isStringArray(companyIds)
      || companyIds.length < 1 || companyIds.length > 8
      || companyIds.some((companyId) => companyId.length < 1 || companyId.length > 100 || companyId.trim() !== companyId)
      || new Set(companyIds).size !== companyIds.length
      || companyIds.some((companyId, index) => index > 0 && companyIds[index - 1] >= companyId)
      || value.version !== "research-execution-scope/1"
      || typeof value.payload_sha256 !== "string" || !SHA256.test(value.payload_sha256)
      || typeof value.scope_id !== "string" || value.scope_id !== `scope-${value.payload_sha256}`) {
    throw new Error("API returned an invalid execution scope");
  }
  return {
    version: value.version,
    company_ids: companyIds,
    scope_id: value.scope_id,
    payload_sha256: value.payload_sha256,
  };
}

function decodeMessage(value: unknown): ConversationMessage {
  if (!isRecord(value) || !hasExactFields(value, MESSAGE_REQUIRED_FIELDS, MESSAGE_OPTIONAL_FIELDS)
      || typeof value.message_id !== "string" || typeof value.content !== "string"
      || typeof value.created_at !== "string"
      || (value.role !== "user" && value.role !== "assistant")) {
    return invalidConversation();
  }

  let runId: string | undefined;
  if (Object.hasOwn(value, "run_id")) {
    if (typeof value.run_id !== "string") return invalidConversation();
    runId = value.run_id;
  }
  let status: string | undefined;
  if (Object.hasOwn(value, "status")) {
    if (typeof value.status !== "string") return invalidConversation();
    status = value.status;
  }
  let citations: unknown[] | undefined;
  if (Object.hasOwn(value, "citations")) {
    if (!Array.isArray(value.citations)) return invalidConversation();
    citations = value.citations;
  }
  let verification: Record<string, unknown> | undefined;
  if (Object.hasOwn(value, "verification")) {
    if (!isRecord(value.verification)) return invalidConversation();
    verification = value.verification;
  }

  return {
    message_id: value.message_id,
    role: value.role,
    content: value.content,
    created_at: value.created_at,
    ...(runId === undefined ? {} : { run_id: runId }),
    ...(status === undefined ? {} : { status }),
    ...(citations === undefined ? {} : { citations }),
    ...(verification === undefined ? {} : { verification }),
  };
}

function decodeDecisionOption(value: unknown): DecisionOption {
  if (!isRecord(value)) return invalidConversation();
  const base = hasExactFields(value, OPTION_FIELDS);
  const regression = hasExactFields(value, [...OPTION_FIELDS, "regression", "regression_details"]);
  const forecast = hasExactFields(value, [...OPTION_FIELDS, "forecast", "forecast_details"]);
  if ((!base && !regression && !forecast)
      || typeof value.id !== "string" || typeof value.label !== "string"
      || typeof value.benefit !== "string" || typeof value.limitation !== "string") {
    return invalidConversation();
  }
  return {
    id: value.id,
    label: value.label,
    benefit: value.benefit,
    limitation: value.limitation,
    ...(regression ? { regression: value.regression, regression_details: value.regression_details } : {}),
    ...(forecast ? { forecast: value.forecast, forecast_details: value.forecast_details } : {}),
  };
}

function decodeDecisionGroup(value: unknown): DecisionGroup {
  if (!isRecord(value) || !hasExactFields(value, GROUP_FIELDS)
      || typeof value.id !== "string" || typeof value.label !== "string"
      || !isStringArray(value.option_ids)
      || typeof value.minimum !== "number" || !Number.isInteger(value.minimum)
      || typeof value.maximum !== "number" || !Number.isInteger(value.maximum)) {
    return invalidConversation();
  }
  return {
    id: value.id,
    label: value.label,
    option_ids: value.option_ids,
    minimum: value.minimum,
    maximum: value.maximum,
  };
}

function nullableString(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function decodeDecision(value: unknown): Decision | null {
  if (value === null) return null;
  if (!isRecord(value) || !hasExactFields(value, DECISION_FIELDS)
      || typeof value.decision_id !== "string" || typeof value.run_id !== "string"
      || !isDecisionKind(value.kind) || typeof value.question !== "string"
      || typeof value.explanation !== "string" || !Array.isArray(value.options)
      || typeof value.multiple !== "boolean" || !Array.isArray(value.groups)
      || typeof value.created_at !== "string" || !isDecisionStatus(value.status)
      || !isStringArray(value.selected_ids) || !nullableString(value.notes)
      || !nullableString(value.continuation_run_id) || !nullableString(value.base_message_id)
      || !nullableString(value.request_message)) {
    return invalidConversation();
  }
  return {
    decision_id: value.decision_id,
    run_id: value.run_id,
    kind: value.kind,
    question: value.question,
    explanation: value.explanation,
    options: value.options.map(decodeDecisionOption),
    multiple: value.multiple,
    groups: value.groups.map(decodeDecisionGroup),
    created_at: value.created_at,
    status: value.status,
    selected_ids: value.selected_ids,
    notes: value.notes,
    continuation_run_id: value.continuation_run_id,
    base_message_id: value.base_message_id,
    request_message: value.request_message,
  };
}

function decodeEvidenceReference(value: unknown): ReportEvidenceReference {
  if (!isRecord(value) || !hasExactFields(value, EVIDENCE_REFERENCE_FIELDS)
      || typeof value.kind !== "string" || typeof value.id !== "string") {
    return invalidConversation();
  }
  return { kind: value.kind, id: value.id };
}

function decodeReportCorrection(value: unknown): ReportCorrection {
  if (!isRecord(value) || !hasExactFields(value, CORRECTION_FIELDS)
      || typeof value.correction_id !== "string" || typeof value.old_message_id !== "string"
      || typeof value.new_message_id !== "string" || typeof value.start !== "number" || !Number.isInteger(value.start)
      || typeof value.end !== "number" || !Number.isInteger(value.end) || typeof value.original_text !== "string"
      || typeof value.explanation !== "string" || typeof value.impact !== "string"
      || typeof value.created_at !== "string" || typeof value.old_version !== "number" || !Number.isInteger(value.old_version)
      || typeof value.new_version !== "number" || !Number.isInteger(value.new_version)
      || !Array.isArray(value.evidence_refs) || value.semantic_verified !== false) {
    return invalidConversation();
  }
  return {
    correction_id: value.correction_id,
    old_message_id: value.old_message_id,
    new_message_id: value.new_message_id,
    start: value.start,
    end: value.end,
    original_text: value.original_text,
    explanation: value.explanation,
    impact: value.impact,
    created_at: value.created_at,
    old_version: value.old_version,
    new_version: value.new_version,
    evidence_refs: value.evidence_refs.map(decodeEvidenceReference),
    semantic_verified: value.semantic_verified,
  };
}

function decodeReportVersion(value: unknown): ReportVersion {
  if (!isRecord(value) || !hasExactFields(value, REPORT_VERSION_FIELDS)
      || typeof value.version !== "number" || !Number.isInteger(value.version)
      || typeof value.message_id !== "string" || !nullableString(value.run_id)
      || typeof value.created_at !== "string" || !Array.isArray(value.corrected_by)
      || !Array.isArray(value.corrections)) {
    return invalidConversation();
  }
  return {
    version: value.version,
    message_id: value.message_id,
    run_id: value.run_id,
    created_at: value.created_at,
    corrected_by: value.corrected_by.map(decodeReportCorrection),
    corrections: value.corrections.map(decodeReportCorrection),
  };
}

export function decodeConversation(value: unknown): Conversation {
  if (!isRecord(value)
      || !hasExactFields(value, CONVERSATION_REQUIRED_FIELDS, CONVERSATION_OPTIONAL_FIELDS)
      || typeof value.conversation_id !== "string" || typeof value.company_id !== "string"
      || typeof value.title !== "string" || typeof value.created_at !== "string"
      || (Object.hasOwn(value, "display_question") && typeof value.display_question !== "string")
      || (Object.hasOwn(value, "display_task_id") && typeof value.display_task_id !== "string")
      || !Array.isArray(value.messages) || !Array.isArray(value.report_versions)
      || !isRecord(value.research_scope)) {
    return invalidConversation();
  }

  let displayQuestion: string | undefined;
  if (Object.hasOwn(value, "display_question")) {
    if (typeof value.display_question !== "string") return invalidConversation();
    displayQuestion = value.display_question;
  }
  let displayTaskId: string | undefined;
  if (Object.hasOwn(value, "display_task_id")) {
    if (typeof value.display_task_id !== "string") return invalidConversation();
    displayTaskId = value.display_task_id;
  }

  return {
    conversation_id: value.conversation_id,
    company_id: value.company_id,
    title: value.title,
    created_at: value.created_at,
    ...(displayQuestion === undefined ? {} : { display_question: displayQuestion }),
    ...(displayTaskId === undefined ? {} : { display_task_id: displayTaskId }),
    execution_scope: decodeExecutionScope(value.execution_scope),
    messages: value.messages.map(decodeMessage),
    decision: decodeDecision(value.decision),
    report_versions: value.report_versions.map(decodeReportVersion),
    research_scope: value.research_scope,
  };
}
