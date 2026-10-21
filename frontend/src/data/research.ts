import { ApiError } from "./api";
import type { Conversation, ResearchTask, Run, SubjectCoverage, VerificationCandidate } from "./types";

export function describeCoverage(coverage: SubjectCoverage): string {
  if (coverage.documents === null && coverage.facts === null) return "Source coverage statistics are not available yet.";
  const documents = coverage.documents?.enrolled;
  const facts = coverage.facts?.count;
  return `At scope identification, ${documents == null ? "the document count was not recorded" : `${documents} documents were available`} and ${facts == null ? "the fact count was not recorded" : `${facts} fact records were available`}. This is not the list of sources actually used in this report.`;
}

export interface RegressionDetailFact {
  metric: string; value: string; unit: string;
  basis: string | null; period_start: string | null; period_end: string | null;
}
export interface RegressionDetails {
  method: string; equation: string; sample_count: number; lag_years: number;
  y_transform: "level" | "percent_change";
  samples: { x: RegressionDetailFact; y: RegressionDetailFact; y_prior: RegressionDetailFact | null }[];
  limitations: string[];
}

/** Read only the known display contract; never calculate or repair sample values. */
export function readRegressionDetails(value: unknown): RegressionDetails | null {
  if (!value || typeof value !== "object") return null;
  const d = value as Record<string, unknown>;
  const fact = (v: unknown): v is RegressionDetailFact => {
    if (!v || typeof v !== "object") return false;
    const f = v as Record<string, unknown>;
    return ["metric", "value", "unit"].every(k => typeof f[k] === "string")
      && ["basis", "period_start", "period_end"].every(k => f[k] === null || typeof f[k] === "string");
  };
  if (typeof d.method !== "string" || typeof d.equation !== "string"
      || !Number.isInteger(d.sample_count) || !Number.isInteger(d.lag_years)
      || typeof d.y_transform !== "string" || !["level", "percent_change"].includes(d.y_transform)
      || !Array.isArray(d.limitations) || !d.limitations.every(x => typeof x === "string")
      || !Array.isArray(d.samples) || d.samples.length !== d.sample_count || !d.samples.length) return null;
  for (const sample of d.samples) {
    if (!sample || typeof sample !== "object" || !fact(sample.x) || !fact(sample.y)
        || (sample.y_prior !== null && !fact(sample.y_prior))
        || (d.y_transform === "percent_change" && sample.y_prior === null)) return null;
  }
  return d as unknown as RegressionDetails;
}

export const ACTIVE_RUN_STATUSES = new Set<Run["status"]>(["queued", "running"]);
export const TERMINAL_RUN_STATUSES = new Set<Run["status"]>([
  "completed", "failed", "cancelled", "blocked",
]);

/** Latest active run attached to this conversation, otherwise its latest known run. */
export function selectCurrentRun(conversation: Conversation, runs: readonly Run[]): Run | null {
  const byId = new Map(runs.map((run) => [run.run_id, run]));
  const linked = [...conversation.messages]
    .reverse()
    .map((message) => message.run_id ? byId.get(message.run_id) : undefined)
    .filter((run): run is Run => Boolean(run));
  return linked.find((run) => ACTIVE_RUN_STATUSES.has(run.status)) ?? linked[0] ?? null;
}

export type ReportDestination =
  | { kind: "formal_report"; href: string; reportId: string | null }
  | { kind: "research_process"; href: string };

/** A completed run or legacy answer is not a formal report; only reader_href proves publication. */
export function reportDestination(task: ResearchTask, processHref: string): ReportDestination {
  const report = task.latest_report;
  const href = report?.reader_href || task.reader_href;
  if (href) {
    return {
      kind: "formal_report",
      href,
      reportId: report?.reader_report_id ?? task.reader_report_id ?? null,
    };
  }
  return { kind: "research_process", href: processHref };
}

export interface RunPresentation {
  state: "queued" | "running" | "awaiting_decision" | "decision_resolved" | "completed" | "failed" | "cancelled" | "blocked" | "unknown";
  tone: "neutral" | "warning" | "good" | "bad";
  label: string;
  terminal: boolean;
  hasFormalReport: boolean;
}

/** Pure mapping only; report publication remains a separate dimension. */
export function presentRun(run: Run, hasFormalReport = false): RunPresentation {
  if (run.termination_reason === "confirmation_required") {
    if (run.decision?.status === "pending") {
      return { state: "awaiting_decision", tone: "warning", label: "Awaiting your confirmation", terminal: true, hasFormalReport };
    }
    if (run.decision?.status === "accepted") {
      return { state: "decision_resolved", tone: "neutral", label: "Confirmed", terminal: true, hasFormalReport };
    }
    if (run.decision?.status === "declined") {
      return { state: "decision_resolved", tone: "warning", label: "Changes declined", terminal: true, hasFormalReport };
    }
  }
  if (run.status === "queued") {
    return { state: "queued", tone: "neutral", label: "Research queued", terminal: false, hasFormalReport };
  }
  if (run.status === "running") {
    return { state: "running", tone: "warning", label: "Research in progress", terminal: false, hasFormalReport };
  }
  if (run.status === "completed") {
    return { state: "completed", tone: "good", label: hasFormalReport ? "Formal report published" : "Run complete · No formal report yet", terminal: true, hasFormalReport };
  }
  if (run.status === "cancelled") {
    const requested = run.cancel_requested === true || ["user_cancelled", "user_canceled", "cancel_requested"].includes(run.termination_reason || "");
    return {
      state: "cancelled",
      tone: requested ? "warning" : "bad",
      label: requested ? "Stopped at your request · No answer" : "Unexpected interruption · No answer",
      terminal: true,
      hasFormalReport,
    };
  }
  if (run.status === "failed") {
    return { state: "failed", tone: "bad", label: "Run failed · No answer", terminal: true, hasFormalReport };
  }
  if (run.status === "blocked") return { state: "blocked", tone: "bad", label: "Run blocked · No answer", terminal: true, hasFormalReport };
  return { state: "unknown", tone: "warning", label: "Run status temporarily unavailable", terminal: true, hasFormalReport };
}

export interface VerificationPresentation {
  state: "awaiting_verification" | "verifying" | "revising" | "verified" | "incomplete";
  tone: RunPresentation["tone"];
  label: string;
  terminal: boolean;
}

/** Verification completion and report content limitations remain separate dimensions. */
export function presentVerification(candidate: VerificationCandidate): VerificationPresentation {
  if (candidate.status === "queued") return { state: "awaiting_verification", tone: "warning", label: "Candidate awaiting verification", terminal: false };
  if (candidate.status === "running") return candidate.versions.length > 1
    ? { state: "revising", tone: "warning", label: "Revising after verification", terminal: false }
    : { state: "verifying", tone: "warning", label: "Verifying candidate", terminal: false };
  if (candidate.status === "verified") return { state: "verified", tone: "good", label: "Verification complete · Formal report published", terminal: true };
  if (candidate.status === "verified_with_limitations") return { state: "verified", tone: "warning", label: "Verification complete · Formal report has content limitations", terminal: true };
  return { state: "incomplete", tone: "bad", label: "Verification incomplete · Next step required", terminal: true };
}

export interface TaskPresentation {
  state: "ready" | "clarification" | "unavailable" | "recorded" | RunPresentation["state"];
  tone: RunPresentation["tone"];
  label: string;
  hasFormalReport: boolean;
}

export function presentTask(task: ResearchTask): TaskPresentation {
  const hasFormalReport = Boolean(task.latest_report?.reader_href || task.reader_href);
  const run = task.latest_run ?? null;
  if (run) return presentRun(run, hasFormalReport);
  if (task.run_status) {
    return presentRun({
      run_id: "",
      conversation_id: "",
      status: task.run_status,
      termination_reason: undefined,
      cancel_requested: undefined,
    }, hasFormalReport);
  }
  if (task.status === "ready") return { state: "ready", tone: "neutral", label: "Ready to start", hasFormalReport };
  if (task.status === "needs_clarification") {
    return { state: "clarification", tone: "warning", label: "Research subject needs confirmation", hasFormalReport };
  }
  if (["unsupported_scope", "missing_data", "model_unavailable"].includes(task.status)) {
    return { state: "unavailable", tone: "bad", label: "Research not started", hasFormalReport };
  }
  return { state: "recorded", tone: "warning", label: "Recorded · Review required", hasFormalReport };
}

/** Only persisted intent-parser failures may be retried in the same task. */
export function canRetryIntent(task: ResearchTask): boolean {
  return (task.status === "model_unavailable" && task.code === "intent_model_unavailable")
    || (task.status === "needs_review" && task.code === "intent_schema_invalid");
}

export interface RequestOwner {
  sequence: number;
  navigation: number;
  taskId: string | null;
  conversationId: string | null;
}

/** Callers must check ownership after every await, even when the fetch was aborted. */
export function requestOwnerMatches(owner: RequestOwner | null, current: RequestOwner | null): boolean {
  return Boolean(owner && current
    && owner.sequence === current.sequence
    && owner.navigation === current.navigation
    && owner.taskId === current.taskId
    && owner.conversationId === current.conversationId);
}

export interface PendingTaskSubmission {
  question: string;
  requestId: string;
  state: "prepared" | "unknown";
}

export interface PendingDecisionSubmission {
  signature: string;
  requestId: string;
  state: "prepared" | "unknown";
}

export function createRequestId(prefix = "web"): string {
  if (globalThis.crypto?.randomUUID) return `${prefix}_${globalThis.crypto.randomUUID()}`;
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return `${prefix}_${Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("")}`;
}

/**
 * Preserve the same request_id while retrying the exact question. The caller owns
 * persistence and must invoke createTask only from an explicit user action.
 */
export function prepareTaskSubmission(
  question: string,
  previous: PendingTaskSubmission | null = null,
): PendingTaskSubmission {
  const normalized = question.trim();
  if (!normalized) throw new Error("question must not be empty");
  if (previous?.question === normalized) return previous;
  return { question: normalized, requestId: createRequestId(), state: "prepared" };
}

/** Reuses request_id only for the exact same decision payload; callers persist it before POST. */
export function prepareDecisionSubmission(
  decisionId: string,
  selectedIds: readonly string[],
  notes: string,
  action: "accepted" | "declined",
  previous: PendingDecisionSubmission | null = null,
): PendingDecisionSubmission {
  const signature = JSON.stringify({ decisionId, selectedIds: [...selectedIds], notes, action });
  if (previous?.signature === signature) return previous;
  return { signature, requestId: createRequestId(), state: "prepared" };
}

/** Transport errors, 429, and 5xx after dispatch may have committed the POST. */
export function submissionOutcomeUnknown(error: unknown, dispatched: boolean, acknowledged: boolean): boolean {
  if (!dispatched || acknowledged) return false;
  if (!(error instanceof ApiError)) return true;
  return error.status === 408 || error.status === 429 || error.status >= 500;
}

export function markSubmissionUnknown<T extends PendingTaskSubmission | PendingDecisionSubmission>(pending: T): T {
  return { ...pending, state: "unknown" };
}
