import { useEffect, useRef, useState } from "react";
import { createResearchApi } from "../../data/api";
import {
  markSubmissionUnknown,
  prepareTaskSubmission,
  submissionOutcomeUnknown,
  type PendingTaskSubmission,
} from "../../data/research";
import type { ResearchTask } from "../../data/types";
import { readCreatedTask, storeCreatedTask } from "./mutationRecovery";
import styles from "./Research.module.css";

const api = createResearchApi();
const DRAFT_KEY = "research-new-draft/1";
const PENDING_KEY = "research-new-submission/1";

function readStored(key: string): string {
  try { return sessionStorage.getItem(key) ?? ""; } catch { return ""; }
}

function readPending(): PendingTaskSubmission | null {
  try {
    const value: unknown = JSON.parse(readStored(PENDING_KEY) || "null");
    if (value && typeof value === "object") {
      const pending = value as Partial<PendingTaskSubmission>;
      if (typeof pending.question === "string" && typeof pending.requestId === "string"
        && (pending.state === "prepared" || pending.state === "unknown")) {
        // A prepared record surviving a reload may already have reached the server.
        return markSubmissionUnknown(pending as PendingTaskSubmission);
      }
    }
  } catch { /* An invalid recovery record is ignored. */ }
  return null;
}

function message(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback;
}

export function NewResearch({ readOnly, onTask }: { readOnly: boolean; onTask: (id: string) => void }) {
  const initialPending = useRef(readPending());
  const initialCreated = useRef(readCreatedTask());
  const [question, setQuestion] = useState(() => initialPending.current?.question ?? initialCreated.current?.question ?? readStored(DRAFT_KEY));
  const [pending, setPending] = useState<PendingTaskSubmission | null>(initialPending.current);
  const [created, setCreated] = useState<ResearchTask | null>(initialCreated.current);
  const [busy, setBusy] = useState(false);
  const [startRecoverable, setStartRecoverable] = useState(Boolean(initialCreated.current?.status === "ready" && !(initialCreated.current.runs?.length)));
  const [status, setStatus] = useState(initialPending.current
    ? "The previous submission outcome is still unconfirmed. Explicitly recover it; the original question and request ID will be reused."
    : initialCreated.current ? ((initialCreated.current.runs?.length ?? 0) > 0
      ? "The saved task and its associated run were recovered. It will not be started again." : "The saved task was recovered. Open the task or explicitly retry starting it.") : "");
  const guard = useRef(false);
  const mounted = useRef(true);
  const scope = useRef(0);

  useEffect(() => () => { mounted.current = false; scope.current += 1; }, []);
  const owns = (token: number) => mounted.current && scope.current === token;

  function storePending(next: PendingTaskSubmission | null) {
    try {
      if (next) sessionStorage.setItem(PENDING_KEY, JSON.stringify(next));
      else sessionStorage.removeItem(PENDING_KEY);
    } catch {
      throw new Error("The browser could not save a recovery record, so nothing was submitted. Allow this tab to use session storage and try again.");
    }
    setPending(next);
  }

  function changeQuestion(value: string) {
    setQuestion(value);
    try { sessionStorage.setItem(DRAFT_KEY, value); } catch { /* Draft storage is optional. */ }
  }

  async function start(task: ResearchTask, token: number) {
    if (!owns(token)) return;
    setStatus("Question saved. Creating a research run…");
    try {
      await api.startTask({ taskId: task.task_id, expectedScopeVersion: task.current_scope_version });
      if (!owns(token)) return;
      storeCreatedTask(null);
      onTask(task.task_id);
    } catch (error) {
      if (!owns(token)) return;
      setStatus(`${message(error, "The research could not be started")} Checking whether a run was created for the task…`);
      try {
        const checked = await api.getTask(task.task_id);
        if (!owns(token)) return;
        storeCreatedTask(checked); setCreated(checked);
        if ((checked.runs?.length ?? 0) > 0) {
          setStatus("A run may have been created and an associated record was found. Open the task to continue; it will not be started again.");
          setStartRecoverable(false);
        } else {
          setStatus("No associated run was found. You can explicitly retry starting it; the system will not resend automatically.");
          setStartRecoverable(true);
        }
      } catch (checkError) {
        if (!owns(token)) return;
        setStatus(`${message(checkError, "The task could not be checked")} The start outcome is unknown. Check again before starting another run.`);
        setStartRecoverable(false);
      }
    }
  }

  async function submit() {
    if (readOnly || guard.current || created) return;
    const normalized = question.trim();
    if (!normalized) { setStatus("Enter a question to research."); return; }
    guard.current = true;
    const token = ++scope.current;
    setBusy(true);
    let dispatched = false;
    let acknowledged = false;
    let submission: PendingTaskSubmission | null = null;
    try {
      const prepared = prepareTaskSubmission(normalized, pending);
      submission = prepared;
      storePending(prepared);
      setStatus(pending ? "Recovering the same submission…" : "Interpreting and saving the question…");
      dispatched = true;
      const task = await api.createTask({ question: prepared.question, contextConversationId: null, requestId: prepared.requestId });
      acknowledged = true;
      // Task identity must be durable before the ambiguous submission record is removed.
      storeCreatedTask(task);
      try { sessionStorage.removeItem(PENDING_KEY); } catch { /* Created identity remains the recovery source. */ }
      if (!owns(token)) return;
      setCreated(task);
      setPending(null);
      try { sessionStorage.removeItem(DRAFT_KEY); } catch { /* Draft cleanup is optional. */ }
      if ((task.runs?.length ?? 0) > 0) {
        setStatus("An associated run was found for this task, so it will not be started again. Open the task to continue.");
        setStartRecoverable(false);
      } else if (task.status === "ready") await start(task, token);
      else {
        storeCreatedTask(null);
        onTask(task.task_id);
      }
    } catch (error) {
      if (!owns(token)) return;
      const current = submission ?? pending ?? prepareTaskSubmission(normalized);
      if (submissionOutcomeUnknown(error, dispatched, acknowledged)) {
        const unknown = markSubmissionUnknown(current);
        try { storePending(unknown); } catch { /* The original storage succeeded before dispatch. */ }
        setStatus(`${message(error, "The submission outcome is unknown.")} Select “Recover same submission”; the system will reuse the same question and request ID.`);
      } else {
        setStatus(`${message(error, "The research could not be created.")} The question is still saved and can be explicitly retried.`);
      }
    } finally {
      guard.current = false;
      if (owns(token)) setBusy(false);
    }
  }

  async function retryStart() {
    if (!created || readOnly || guard.current || !startRecoverable) return;
    guard.current = true;
    const token = ++scope.current;
    setBusy(true); setStartRecoverable(false);
    try { await start(created, token); }
    finally { guard.current = false; if (owns(token)) setBusy(false); }
  }

  const lockedQuestion = Boolean(pending) || busy;
  return (
    <section className={styles.page}>
      <section className={styles.composer} aria-labelledby="new-research-title">
        <div>
          <h1 id="new-research-title">What would you like to research?</h1>
          <p className={styles.intro}>Use one complete question to identify the subject, time period, and what you want to compare or assess.</p>
        </div>
        <label className={styles.label} htmlFor="research-question">Research question</label>
        <textarea id="research-question" rows={6} maxLength={12000} value={question}
          onChange={(event) => changeQuestion(event.target.value)} readOnly={lockedQuestion || Boolean(created)}
          placeholder="For example: Compare these companies’ cash flow quality over the last three fiscal years and explain the main differences." />
        <div className={styles.actionRow}>
          <button type="button" className={styles.primary} disabled={readOnly || busy || !question.trim() || Boolean(created)} onClick={submit}>
            {busy ? "Processing…" : pending ? "Recover same submission" : "Start Research"}
          </button>
          {created && <button type="button" onClick={() => { storeCreatedTask(null); onTask(created.task_id); }}>Open saved task</button>}
          {created && startRecoverable && <button type="button" disabled={readOnly || busy} onClick={retryStart}>Retry start</button>}
        </div>
        {readOnly && <p className={styles.readOnly}>Read-only preview; no research will be submitted or changed</p>}
        {pending && <p className={styles.hint}>The question remains locked until recovery is complete. Continue this submission to avoid treating a possibly saved question as a new one.</p>}
        <p className={styles.status} role="status" aria-live="polite">{status}</p>
      </section>
    </section>
  );
}
