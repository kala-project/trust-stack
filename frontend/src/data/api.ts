import type {
  CancelRunResult,
  CancelVerificationResult,
  Conversation,
  DecisionAnswerResult,
  DocumentCatalog,
  ResearchTask,
  Run,
  StartTaskResult,
  TaskListResponse,
  VerificationCandidate,
  VerificationCandidateList,
} from "./types";
import { decodeConversation, decodeExecutionScope } from "./contracts";

export { decodeExecutionScope };

export interface ApiErrorBody {
  error?: string;
  message?: string;
  reason?: string;
  [field: string]: unknown;
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly body: ApiErrorBody;
  readonly authenticationRequired: boolean;

  constructor(status: number, body: ApiErrorBody, statusText = "") {
    const detail = body.message || body.reason || body.error || statusText;
    super(`API returned ${status}${detail ? `: ${detail}` : ""}`);
    this.name = "ApiError";
    this.status = status;
    this.code = typeof body.error === "string" ? body.error : "";
    this.body = body;
    this.authenticationRequired = status === 401;
  }
}

export interface FetchJsonOptions extends Omit<RequestInit, "body"> {
  body?: unknown;
  onAuthenticationRequired?: (error: ApiError) => void;
}

/**
 * Performs one request only. In particular, POST is never retried. Aborting the
 * signal only stops this client request; it does not cancel a backend run.
 */
export async function fetchJson<T>(url: string, options: FetchJsonOptions = {}): Promise<T> {
  const { body, headers, onAuthenticationRequired, ...init } = options;
  const requestHeaders = new Headers(headers);
  requestHeaders.set("Accept", "application/json");
  if (body !== undefined) requestHeaders.set("Content-Type", "application/json");

  const response = await fetch(url, {
    ...init,
    credentials: init.credentials ?? "same-origin",
    headers: requestHeaders,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });

  if (!response.ok) {
    let errorBody: ApiErrorBody = {};
    try {
      const parsed: unknown = await response.json();
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        errorBody = parsed as ApiErrorBody;
      }
    } catch {
      // Status and statusText remain authoritative when no JSON body is available.
    }
    const error = new ApiError(response.status, errorBody, response.statusText);
    if (error.authenticationRequired) onAuthenticationRequired?.(error);
    throw error;
  }

  return (await response.json()) as T;
}

export interface ApiClientOptions {
  onAuthenticationRequired?: (error: ApiError) => void;
}

export interface CreateTaskInput {
  question: string;
  contextConversationId: string | null;
  requestId: string;
}

export interface ResolveTaskInput {
  taskId: string;
  expectedScopeVersion: number;
  companyId: string;
}

export interface StartTaskInput {
  taskId: string;
  expectedScopeVersion: number;
}

export type RetryIntentInput = StartTaskInput;

export interface AnswerDecisionInput {
  decisionId: string;
  selectedIds: string[];
  notes: string;
  requestId: string;
}

export interface ResearchApi {
  listTasks(includeInternal?: boolean, signal?: AbortSignal): Promise<ResearchTask[]>;
  getTask(taskId: string, signal?: AbortSignal): Promise<ResearchTask>;
  createTask(input: CreateTaskInput, signal?: AbortSignal): Promise<ResearchTask>;
  resolveTask(input: ResolveTaskInput, signal?: AbortSignal): Promise<ResearchTask>;
  retryIntent(input: RetryIntentInput, signal?: AbortSignal): Promise<ResearchTask>;
  startTask(input: StartTaskInput, signal?: AbortSignal): Promise<StartTaskResult>;
  getConversation(conversationId: string, signal?: AbortSignal): Promise<Conversation>;
  getRun(runId: string, after?: number, signal?: AbortSignal): Promise<Run>;
  cancelRun(runId: string, signal?: AbortSignal): Promise<CancelRunResult>;
  listVerificationCandidates(conversationId: string, signal?: AbortSignal): Promise<VerificationCandidate[]>;
  getVerificationCandidate(conversationId: string, candidateId: string, signal?: AbortSignal): Promise<VerificationCandidate>;
  cancelVerification(conversationId: string, candidateId: string, signal?: AbortSignal): Promise<CancelVerificationResult>;
  acceptDecision(input: AnswerDecisionInput, signal?: AbortSignal): Promise<DecisionAnswerResult>;
  declineDecision(input: Omit<AnswerDecisionInput, "selectedIds">, signal?: AbortSignal): Promise<DecisionAnswerResult>;
  getDocumentCatalog(companyId: string, signal?: AbortSignal): Promise<DocumentCatalog>;
}

function query(path: string, values: Record<string, string | number | boolean>): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) params.set(key, String(value));
  return `${path}?${params.toString()}`;
}

/** Creates a side-effect-free adapter; writes occur only when a write method is explicitly called. */
export function createResearchApi(options: ApiClientOptions = {}): ResearchApi {
  const request = <T>(url: string, init: FetchJsonOptions = {}) =>
    fetchJson<T>(url, { ...init, onAuthenticationRequired: options.onAuthenticationRequired });
  const post = <T>(url: string, body: unknown, signal?: AbortSignal) =>
    request<T>(url, { method: "POST", body, signal });

  return {
    async listTasks(includeInternal = false, signal) {
      const data = await request<TaskListResponse>(
        query("/api/research/tasks", { include_internal: includeInternal }),
        { signal },
      );
      return data.tasks;
    },
    getTask: (taskId, signal) =>
      request<ResearchTask>(query("/api/research/task", { id: taskId }), { signal }),
    createTask: (input, signal) => post<ResearchTask>("/api/research/tasks", {
      question: input.question,
      context_conversation_id: input.contextConversationId,
      request_id: input.requestId,
    }, signal),
    resolveTask: (input, signal) => post<ResearchTask>("/api/research/tasks/resolve", {
      task_id: input.taskId,
      expected_scope_version: input.expectedScopeVersion,
      company_id: input.companyId,
    }, signal),
    retryIntent: (input, signal) => post<ResearchTask>("/api/research/tasks/retry-intent", {
      task_id: input.taskId,
      expected_scope_version: input.expectedScopeVersion,
    }, signal),
    startTask: (input, signal) => post<StartTaskResult>("/api/research/tasks/start", {
      task_id: input.taskId,
      expected_scope_version: input.expectedScopeVersion,
    }, signal),
    async getConversation(conversationId, signal) {
      const value = await request<unknown>(
        query("/api/chat/conversation", { id: conversationId }), { signal });
      return decodeConversation(value);
    },
    getRun: (runId, after = 0, signal) =>
      request<Run>(query("/api/chat/run", { id: runId, after }), { signal }),
    cancelRun: (runId, signal) => post<CancelRunResult>("/api/chat/cancel", { run_id: runId }, signal),
    async listVerificationCandidates(conversationId, signal) {
      const data = await request<VerificationCandidateList>(
        query("/api/verifications", { conversation_id: conversationId, limit: 50 }), { signal });
      return data.candidates;
    },
    getVerificationCandidate: (conversationId, candidateId, signal) => request<VerificationCandidate>(
      query("/api/verification/candidate", { conversation_id: conversationId, candidate_id: candidateId }), { signal }),
    cancelVerification: (conversationId, candidateId, signal) => post<CancelVerificationResult>(
      "/api/verification/cancel", { conversation_id: conversationId, candidate_id: candidateId }, signal),
    acceptDecision: (input, signal) => post<DecisionAnswerResult>("/api/research/decisions/answer", {
      decision_id: input.decisionId,
      selected_ids: input.selectedIds,
      notes: input.notes,
      action: "accepted",
      request_id: input.requestId,
    }, signal),
    declineDecision: (input, signal) => post<DecisionAnswerResult>("/api/research/decisions/answer", {
      decision_id: input.decisionId,
      selected_ids: [],
      notes: input.notes,
      action: "declined",
      request_id: input.requestId,
    }, signal),
    getDocumentCatalog: (companyId, signal) =>
      request<DocumentCatalog>(query("/api/documents/coverage", { company: companyId }), { signal }),
  };
}
