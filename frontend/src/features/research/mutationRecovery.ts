import type { ResearchTask } from "../../data/types";
import type { PendingDecisionSubmission } from "../../data/research";

const CREATED_TASK_KEY = "research-new-created-task/1";
const DECISION_KEY = "research-decision-submission/1";

export interface DecisionPayload {
  action: "accepted" | "declined";
  selected: string[];
  notes: string;
}

export interface StoredDecisionSubmission {
  decisionId: string;
  pending: PendingDecisionSubmission;
  payload: DecisionPayload;
}

export function readCreatedTask(): ResearchTask | null {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(CREATED_TASK_KEY) || "null");
    if (value && typeof value === "object" && typeof (value as ResearchTask).task_id === "string") {
      return value as ResearchTask;
    }
  } catch { /* Invalid or unavailable recovery storage is treated as empty. */ }
  return null;
}

export function storeCreatedTask(task: ResearchTask | null): void {
  if (task) sessionStorage.setItem(CREATED_TASK_KEY, JSON.stringify(task));
  else sessionStorage.removeItem(CREATED_TASK_KEY);
}

export function readDecisionSubmission(decisionId: string): StoredDecisionSubmission | null {
  try {
    const value: unknown = JSON.parse(sessionStorage.getItem(`${DECISION_KEY}/${decisionId}`) || "null");
    if (!value || typeof value !== "object") return null;
    const record = value as Partial<StoredDecisionSubmission>;
    if (typeof record.decisionId !== "string" || (decisionId && record.decisionId !== decisionId)
      || !record.pending || typeof record.pending.requestId !== "string"
      || (record.pending.state !== "prepared" && record.pending.state !== "unknown")
      || !record.payload || (record.payload.action !== "accepted" && record.payload.action !== "declined")
      || !Array.isArray(record.payload.selected) || typeof record.payload.notes !== "string") return null;
    return record as StoredDecisionSubmission;
  } catch { return null; }
}

export function storeDecisionSubmission(record: StoredDecisionSubmission | null, decisionId?: string): void {
  const id = record?.decisionId ?? decisionId;
  if (!id) throw new Error("Missing decision identity for recovery storage");
  const key = `${DECISION_KEY}/${id}`;
  if (record) sessionStorage.setItem(key, JSON.stringify(record));
  else sessionStorage.removeItem(key);
}
