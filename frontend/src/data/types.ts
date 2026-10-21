export type ResearchTaskStatus =
  | "ready"
  | "needs_clarification"
  | "unsupported_scope"
  | "missing_data"
  | "model_unavailable"
  | "needs_review";

export type RunStatus = "queued" | "running" | "completed" | "failed" | "cancelled" | "blocked";
export type DecisionStatus = "pending" | "accepted" | "declined";
export type VerificationStatus = "queued" | "running" | "verified" | "verified_with_limitations" | "unresolved";

export interface VerificationCandidateVersion {
  version_id: string;
  version_number: number;
  parent_version_id: string | null;
  publication_sha256: string;
  dependency_hash: string;
  created_at: string;
}

export interface VerificationCandidate {
  candidate_id: string;
  conversation_id: string;
  author_run_id: string;
  verify_job_id: string;
  question: string;
  scope: Record<string, unknown>;
  base_message_id: string | null;
  status: VerificationStatus;
  formal_message_id: string | null;
  semantic_truth_guaranteed: false;
  versions: VerificationCandidateVersion[];
}

export interface VerificationCandidateList {
  conversation_id: string;
  candidates: VerificationCandidate[];
}

export interface CancelVerificationResult { candidate_id: string; status: string }
export type DecisionKind = "basis" | "model" | "coverage" | "time" | "other";

export interface ResearchSubject {
  kind: string;
  id: string | null;
  label: string;
}

export interface ResearchScope {
  subjects: ResearchSubject[];
  period_text: string | null;
  metrics: string[];
  basis: string[];
  unit: string | null;
  directions: string[];
  assumptions: string[];
  uncertainties: string[];
  original_intent: string;
  geography: string | null;
  sample_definition_ref: string | null;
  material_selection: unknown;
  ambiguities: unknown[];
  gaps: string[];
  human_selection?: {
    company_id: string;
    from_scope_version: number;
  };
  intent_metadata?: Record<string, unknown>;
}

export interface ResearchTaskOption {
  company_id: string;
  label: string;
}

export interface SubjectCoverage {
  facts: { available: boolean; count: number } | null;
  documents: { available?: boolean; enrolled?: number; parsed?: number; [field: string]: unknown } | null;
}

// Multi-subject task scopes return coverage per issuer, not top-level facts.
export type ResearchCoverage = SubjectCoverage | { subjects: Record<string, SubjectCoverage> };

export interface TaskRunLink {
  scope_version: number;
  conversation_id: string;
  run_id: string;
  status: RunStatus;
  created_at: string;
}

export interface ReportReference {
  reader_report_id: string;
  reader_href: string;
  conversation_id: string;
  message_id: string;
  run_id: string | null;
  created_at: string;
}

export interface ResearchTask {
  task_id: string;
  question: string;
  origin: "user" | "machine_acceptance";
  created_at: string;
  current_scope_version: number;
  status: ResearchTaskStatus;
  code: string;
  message: string;
  scope?: ResearchScope;
  subjects?: ResearchSubject[];
  period_text?: string | null;
  options?: ResearchTaskOption[];
  coverage?: ResearchCoverage;
  context_conversation_id?: string | null;
  runs?: TaskRunLink[];
  latest_run?: Run | null;
  latest_report?: ReportReference | null;
  run_status?: RunStatus | null;
  started_at?: string | null;
  finished_at?: string | null;
  answer_status?: "answered" | "partial" | "unanswered";
  answer_summary?: string;
  failure_reason?: string;
  risk_status?: "needs_review" | "not_checked";
  reader_report_id?: string;
  reader_href?: string;
  reader_title?: string;
}

export interface DecisionOption {
  id: string;
  label: string;
  benefit: string;
  limitation: string;
  regression?: unknown;
  regression_details?: unknown;
  forecast?: unknown;
  forecast_details?: unknown;
}

export interface DecisionGroup {
  id: string;
  label: string;
  option_ids: string[];
  minimum: number;
  maximum: number;
}

export interface Decision {
  decision_id: string;
  run_id: string;
  kind: DecisionKind;
  question: string;
  explanation: string;
  options: DecisionOption[];
  multiple: boolean;
  groups: DecisionGroup[];
  created_at: string;
  status: DecisionStatus;
  selected_ids: string[];
  notes: string | null;
  continuation_run_id: string | null;
  base_message_id?: string | null;
  request_message?: string | null;
}

export interface RunEvent {
  seq: number;
  type: string;
  [field: string]: unknown;
}

export interface Run {
  run_id: string;
  conversation_id: string;
  status: RunStatus;
  created_at?: string;
  events?: RunEvent[];
  next_cursor?: number;
  termination_reason?: string | null;
  termination_message?: string | null;
  cancel_requested?: boolean;
  error?: Record<string, unknown>;
  decision?: Decision;
}

export interface ConversationMessage {
  message_id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
  run_id?: string;
  status?: string;
  citations?: unknown[];
  verification?: Record<string, unknown>;
}

export interface ReportEvidenceReference {
  kind: string;
  id: string;
}

export interface ReportCorrection {
  correction_id: string;
  old_message_id: string;
  new_message_id: string;
  start: number;
  end: number;
  original_text: string;
  explanation: string;
  impact: string;
  created_at: string;
  old_version: number;
  new_version: number;
  evidence_refs: ReportEvidenceReference[];
  semantic_verified: false;
}

export interface ReportVersion {
  version: number;
  message_id: string;
  run_id: string | null;
  created_at: string;
  corrected_by: ReportCorrection[];
  corrections: ReportCorrection[];
}

export interface ExecutionScope {
  version: "research-execution-scope/1";
  company_ids: string[];
  scope_id: string;
  payload_sha256: string;
}

export interface Conversation {
  conversation_id: string;
  company_id: string;
  title: string;
  created_at: string;
  display_question?: string;
  display_task_id?: string;
  execution_scope: ExecutionScope;
  messages: ConversationMessage[];
  decision: Decision | null;
  report_versions: ReportVersion[];
  research_scope: Record<string, unknown>;
}

export interface DocumentCatalogEntry {
  document_version_id: string;
  source_key: string;
  source_version: number;
  title: string;
  source_url: string;
  source_sha256: string;
  published_at: string | null;
  collected_at: string;
  media_type: string;
  rights_status: string;
  parse_status: "parsed" | "parse_failed" | "unsupported_pdf";
  parse_error: string | null;
  supersedes_document_version_id: string | null;
  parser_version?: string;
  tables?: Array<{ table_index: number; table_sha256: string }>;
}

export interface DocumentCatalog {
  enrolled: number;
  parsed: number;
  failed: number;
  unsupported: number;
  documents: DocumentCatalogEntry[];
}

export interface TaskListResponse { tasks: ResearchTask[] }

export interface StartTaskResult {
  task_id: string;
  scope_version: number;
  company_id: string;
  conversation_id: string;
  run_id: string;
  company_ids: string[];
}

export interface CancelRunResult { run_id: string; status: RunStatus }

export interface DecisionAnswerResult {
  run_id: string | null;
  conversation_id: string;
  status: "accepted" | "declined";
}
