import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createResearchApi } from "../../data/api";
import {
  ACTIVE_RUN_STATUSES,
  canRetryIntent,
  describeCoverage,
  markSubmissionUnknown,
  prepareDecisionSubmission,
  presentRun,
  presentTask,
  presentVerification,
  requestOwnerMatches,
  selectCurrentRun,
  submissionOutcomeUnknown,
  type PendingDecisionSubmission,
  type RequestOwner,
} from "../../data/research";
import { formatAppRoute } from "../../data/route";
import type { Conversation, Decision, ResearchTask, Run, RunEvent, VerificationCandidate } from "../../data/types";
import {
  readDecisionSubmission,
  storeDecisionSubmission,
  type DecisionPayload,
} from "./mutationRecovery";
import styles from "./Research.module.css";
import { RegressionDetails } from "./RegressionDetails";

const api = createResearchApi();
const POLL_MS = 2500;

function errorText(error: unknown, fallback: string) {
  return error instanceof Error && error.message ? error.message : fallback;
}

function eventLabel(event: RunEvent) {
  const labels: Record<string, string> = {
    run_started: "Research started", run_completed: "Research completed", tool_started: "Source review started",
    tool_completed: "Source review completed", decision_required: "Confirmation required", error: "Error occurred",
  };
  return labels[event.type] ?? event.type.replaceAll("_", " ");
}

function eventSummary(event: RunEvent) {
  for (const key of ["message", "summary", "title", "status", "reason"]) {
    if (typeof event[key] === "string" && event[key]) return String(event[key]).slice(0, 300);
  }
  return "This step was recorded.";
}

function legacyHref(taskId?: string, conversationId?: string) {
  const hash = formatAppRoute({ page: "research", task: taskId ?? "", conversation: conversationId ?? "", tab: "research" });
  return `/legacy.html${hash}`;
}

interface DecisionPanelProps {
  decision: Decision;
  readOnly: boolean;
  taskId?: string;
  conversationId?: string;
  onSettled: () => void;
}

function DecisionPanel({ decision, readOnly, taskId, conversationId, onSettled }: DecisionPanelProps) {
  const recovered = useRef(readDecisionSubmission(decision.decision_id));
  const recoveredPending = recovered.current ? markSubmissionUnknown(recovered.current.pending) : null;
  const [selected, setSelected] = useState<string[]>(recovered.current?.payload.selected ?? decision.selected_ids ?? []);
  const [notes, setNotes] = useState(recovered.current?.payload.notes ?? decision.notes ?? "");
  const [feedback, setFeedback] = useState(recovered.current
    ? "The previous selection outcome is still unconfirmed. This decision remains pending; explicitly recover the same submission." : "");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<PendingDecisionSubmission | null>(recoveredPending);
  const pendingPayload = useRef<DecisionPayload | null>(recovered.current?.payload ?? null);
  const guard = useRef(false);
  const mounted = useRef(true);
  const scope = useRef(0);
  const groups = decision.groups ?? [];
  const supported = decision.options.length > 0 && groups.every((group) => group.minimum >= 0
    && group.maximum >= group.minimum && group.option_ids.every((id) => decision.options.some((option) => option.id === id)));

  useEffect(() => () => { mounted.current = false; scope.current += 1; }, []);
  const owns = (token: number) => mounted.current && scope.current === token;

  function toggle(optionId: string, groupId: string, multiple: boolean) {
    if (pending?.state === "unknown") return;
    const group = groups.find((item) => item.id === groupId);
    const peers = group?.option_ids ?? decision.options.map((option) => option.id);
    setSelected((current) => multiple
      ? current.includes(optionId) ? current.filter((id) => id !== optionId) : [...current, optionId]
      : [...current.filter((id) => !peers.includes(id)), optionId]);
  }

  function validate(ids: string[]) {
    if (!ids.length) return "Select the method to use for this research first.";
    for (const group of groups) {
      const count = ids.filter((id) => group.option_ids.includes(id)).length;
      if (count < group.minimum || count > group.maximum) return `For “${group.label}”, select ${group.minimum} to ${group.maximum} items.`;
    }
    return "";
  }

  async function send(action: "accepted" | "declined", payloadOverride?: DecisionPayload | null) {
    if (readOnly || guard.current) return;
    const payload: DecisionPayload | null = payloadOverride ?? { action, selected: action === "accepted" ? [...selected] : [], notes };
    if (!payload) return;
    const invalid = payload.action === "accepted" ? validate(payload.selected) : "";
    if (invalid) { setFeedback(invalid); return; }
    guard.current = true;
    const token = ++scope.current;
    setBusy(true); setFeedback("Saving your selection…");
    let dispatched = false; let acknowledged = false;
    const prepared = prepareDecisionSubmission(decision.decision_id, payload.selected, payload.notes, payload.action, pending);
    try {
      // Persist the exact payload and id before dispatch; storage failure means no POST.
      storeDecisionSubmission({ decisionId: decision.decision_id, pending: prepared, payload });
      setPending(prepared); pendingPayload.current = payload;
      dispatched = true;
      if (payload.action === "accepted") await api.acceptDecision({ decisionId: decision.decision_id, selectedIds: payload.selected, notes: payload.notes, requestId: prepared.requestId });
      else await api.declineDecision({ decisionId: decision.decision_id, notes: payload.notes, requestId: prepared.requestId });
      acknowledged = true;
      if (!owns(token)) return;
      storeDecisionSubmission(null, decision.decision_id); setPending(null); pendingPayload.current = null;
      setFeedback(payload.action === "accepted" ? "Selection saved. Loading subsequent research." : "Decline recorded. The original question remains unchanged.");
      onSettled();
    } catch (error) {
      if (!owns(token)) return;
      if (submissionOutcomeUnknown(error, dispatched, acknowledged)) {
        const unknown = markSubmissionUnknown(prepared);
        try { storeDecisionSubmission({ decisionId: decision.decision_id, pending: unknown, payload }); } catch { /* Prepared record already exists. */ }
        setPending(unknown);
        setFeedback(`${errorText(error, "The save outcome is unknown.")} The selection is locked; explicitly check and retry the same submission.`);
      } else setFeedback(`${errorText(error, "The selection could not be saved.")} The research scope will not change before confirmation.`);
    } finally { guard.current = false; if (owns(token)) setBusy(false); }
  }

  if (!supported) return (
    <section className={styles.decision} aria-labelledby={`decision-${decision.decision_id}`}>
      <h2 id={`decision-${decision.decision_id}`}>{decision.question}</h2>
      <p>This confirmation includes combination rules that this interface cannot represent reliably. Do not simplify the selection here.</p>
      <a className={styles.textLink} href={legacyHref(taskId, conversationId)}>Continue in the legacy interface</a>
    </section>
  );

  const choiceGroups = groups.length ? groups : [{ id: "all", label: "Research method", option_ids: decision.options.map((o) => o.id), minimum: 1, maximum: decision.multiple ? decision.options.length : 1 }];
  const locked = busy || Boolean(pending);
  return (
    <section className={styles.decision} aria-labelledby={`decision-${decision.decision_id}`}>
      <h2 id={`decision-${decision.decision_id}`}>{decision.question}</h2>
      <p>{decision.explanation}</p>
      {decision.kind !== "time" && <p className={styles.hint}>Confirmation uses the sources available when the method was proposed; sources will not update automatically.</p>}
      {choiceGroups.map((group) => (
        <fieldset key={group.id} disabled={readOnly || locked}>
          <legend>{group.label}</legend>
          <p className={styles.hint}>{group.maximum === 1 ? "Select one item" : `Select ${group.minimum} to ${group.maximum} items`}</p>
          {decision.options.filter((option) => group.option_ids.includes(option.id)).map((option) => {
            const multiple = group.maximum > 1;
            return <Fragment key={option.id}><label className={styles.option}>
              <input type={multiple ? "checkbox" : "radio"} name={`${decision.decision_id}-${group.id}`}
                checked={selected.includes(option.id)} onChange={() => toggle(option.id, group.id, multiple)} />
              <span><strong>{option.label}</strong><small>Purpose and benefits: {option.benefit}</small><small>Limitations and trade-offs: {option.limitation}</small></span>
            </label><RegressionDetails value={option.regression_details} /></Fragment>;
          })}
        </fieldset>
      ))}
      <label className={styles.label} htmlFor={`decision-notes-${decision.decision_id}`}>Additional requirements (optional)</label>
      <textarea id={`decision-notes-${decision.decision_id}`} rows={3} maxLength={4000} value={notes}
        disabled={readOnly || locked} onChange={(event) => setNotes(event.target.value)} />
      <div className={styles.actionRow}>
        <button className={styles.primary} type="button" disabled={readOnly || locked} onClick={() => send("accepted")}>Confirm and continue research</button>
        <button type="button" disabled={readOnly || locked} onClick={() => send("declined")}>Decline and keep original question</button>
        {pending?.state === "unknown" && <button type="button" disabled={readOnly || busy} onClick={() => send(pendingPayload.current?.action ?? "accepted", pendingPayload.current)}>Check and retry same submission</button>}
      </div>
      {readOnly && <p className={styles.readOnly}>Read-only preview; no research will be submitted or changed</p>}
      <p className={styles.status} role="status" aria-live="polite">{feedback}</p>
    </section>
  );
}

export function ResearchWorkspace({ taskId, conversationId, readOnly, onReport, onChanged }: {
  taskId?: string; conversationId?: string; readOnly: boolean; onReport: (href: string) => void; onChanged: () => void;
}) {
  const [task, setTask] = useState<ResearchTask | null>(null);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [runs, setRuns] = useState<Run[]>([]);
  const [candidate, setCandidate] = useState<VerificationCandidate | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [status, setStatus] = useState("Loading research…");
  const [writing, setWriting] = useState(false);
  const [company, setCompany] = useState("");
  const [startRecoverable, setStartRecoverable] = useState(false);
  const ownerRef = useRef<RequestOwner | null>(null);
  const sequence = useRef(0);
  const navigation = useRef(0);
  const mutationGuard = useRef(false);
  const mounted = useRef(true);

  const ownsMutation = (token: number) => mounted.current && navigation.current === token;

  const load = useCallback(async (signal?: AbortSignal, quiet = false) => {
    const owner: RequestOwner = { sequence: ++sequence.current, navigation: navigation.current, taskId: taskId ?? null, conversationId: conversationId ?? null };
    ownerRef.current = owner;
    if (!quiet) { setLoading(true); setLoadFailed(false); setStatus("Loading research…"); }
    try {
      const loadedTask = taskId ? await api.getTask(taskId, signal) : null;
      if (!requestOwnerMatches(owner, ownerRef.current)) return;
      const selectedConversation = conversationId || loadedTask?.runs?.at(-1)?.conversation_id || loadedTask?.context_conversation_id || "";
      const loadedConversation = selectedConversation ? await api.getConversation(selectedConversation, signal) : null;
      if (!requestOwnerMatches(owner, ownerRef.current)) return;
      const ids = new Set<string>();
      loadedTask?.runs?.filter((link) => !selectedConversation || link.conversation_id === selectedConversation).slice(-20).forEach((link) => ids.add(link.run_id));
      loadedConversation?.messages.forEach((item) => { if (item.run_id) ids.add(item.run_id); });
      if (loadedTask?.latest_run?.run_id) ids.add(loadedTask.latest_run.run_id);
      const loadedRuns = await Promise.all([...ids].slice(-20).map((id) => api.getRun(id, 0, signal)));
      if (!requestOwnerMatches(owner, ownerRef.current)) return;
      const candidateSummaries = selectedConversation
        ? await api.listVerificationCandidates(selectedConversation, signal) : [];
      if (!requestOwnerMatches(owner, ownerRef.current)) return;
      const summary = candidateSummaries.find((item) => ids.has(item.author_run_id)) ?? candidateSummaries[0];
      const loadedCandidate = summary
        ? await api.getVerificationCandidate(selectedConversation, summary.candidate_id, signal) : null;
      if (!requestOwnerMatches(owner, ownerRef.current)) return;
      const loadedDecisions = [loadedConversation?.decision, ...loadedRuns.map((run) => run.decision)].filter(Boolean) as Decision[];
      for (const item of loadedDecisions) {
        if (item.status !== "pending" && readDecisionSubmission(item.decision_id)) storeDecisionSubmission(null, item.decision_id);
      }
      setTask(loadedTask); setConversation(loadedConversation); setRuns(loadedRuns); setCandidate(loadedCandidate);
      setCompany(loadedTask?.options?.[0]?.company_id ?? "");
      setStartRecoverable(Boolean(loadedTask?.status === "ready" && !(loadedTask.runs?.length) && loadedRuns.length === 0));
      setStatus("Research record updated"); setLoading(false);
    } catch (error) {
      if (signal?.aborted || !requestOwnerMatches(owner, ownerRef.current)) return;
      setStatus(errorText(error, "The research record is temporarily unavailable. Try again.")); setLoadFailed(true); setLoading(false);
    }
  }, [taskId, conversationId]);

  useEffect(() => {
    mounted.current = true;
    navigation.current += 1;
    const controller = new AbortController();
    void load(controller.signal);
    return () => { controller.abort(); ownerRef.current = null; mounted.current = false; navigation.current += 1; };
  }, [load]);

  const currentRun = useMemo(() => (conversation ? selectCurrentRun(conversation, runs) : null)
    ?? runs.find((run) => ACTIVE_RUN_STATUSES.has(run.status)) ?? runs.at(-1) ?? null, [conversation, runs]);

  useEffect(() => {
    if (!currentRun || !ACTIVE_RUN_STATUSES.has(currentRun.status)) return;
    let stopped = false; let timer: number | undefined; let controller: AbortController | null = null;
    const poll = async () => {
      if (stopped) return;
      controller = new AbortController();
      try {
        const updated = await api.getRun(currentRun.run_id, 0, controller.signal);
        if (stopped) return;
        setRuns((items) => [...items.filter((item) => item.run_id !== updated.run_id), updated]);
        setStatus(presentRun(updated, Boolean(task?.reader_href || task?.latest_report?.reader_href)).label);
        if (ACTIVE_RUN_STATUSES.has(updated.status)) timer = window.setTimeout(poll, POLL_MS);
        else { await load(undefined, true); if (!stopped) onChanged(); }
      } catch (error) {
        if (!stopped) { setStatus(errorText(error, "The run status is temporarily unavailable.")); timer = window.setTimeout(poll, POLL_MS * 2); }
      }
    };
    timer = window.setTimeout(poll, POLL_MS);
    return () => { stopped = true; controller?.abort(); if (timer) window.clearTimeout(timer); };
  }, [currentRun?.run_id, currentRun?.status, load, onChanged, task?.latest_report?.reader_href, task?.reader_href]);

  useEffect(() => {
    if (!candidate || !["queued", "running"].includes(candidate.status)) return;
    let stopped = false; let timer: number | undefined; let controller: AbortController | null = null;
    const poll = async () => {
      controller = new AbortController();
      try {
        const updated = await api.getVerificationCandidate(candidate.conversation_id, candidate.candidate_id, controller.signal);
        if (stopped) return;
        setCandidate(updated); setStatus(presentVerification(updated).label);
        if (["queued", "running"].includes(updated.status)) timer = window.setTimeout(poll, POLL_MS);
        else { await load(undefined, true); if (!stopped) onChanged(); }
      } catch (error) {
        if (!stopped) { setStatus(errorText(error, "The verification status is temporarily unavailable.")); timer = window.setTimeout(poll, POLL_MS * 2); }
      }
    };
    timer = window.setTimeout(poll, POLL_MS);
    return () => { stopped = true; controller?.abort(); if (timer) window.clearTimeout(timer); };
  }, [candidate?.candidate_id, candidate?.conversation_id, candidate?.status, load, onChanged]);

  async function cancelVerification() {
    if (!candidate || readOnly || mutationGuard.current || !["queued", "running"].includes(candidate.status)) return;
    mutationGuard.current = true; const token = navigation.current; setWriting(true); setStatus("Stopping verification…");
    try {
      await api.cancelVerification(candidate.conversation_id, candidate.candidate_id);
      if (!ownsMutation(token)) return;
      await load(undefined, true); if (ownsMutation(token)) onChanged();
    } catch (error) { if (ownsMutation(token)) setStatus(errorText(error, "The selected verification could not be stopped. Try again.")); }
    finally { mutationGuard.current = false; if (ownsMutation(token)) setWriting(false); }
  }

  async function cancel() {
    if (!currentRun || readOnly || mutationGuard.current || !ACTIVE_RUN_STATUSES.has(currentRun.status)) return;
    mutationGuard.current = true;
    const token = navigation.current;
    const runId = currentRun.run_id;
    setWriting(true); setStatus("Sending stop request…");
    try {
      await api.cancelRun(runId);
      if (!ownsMutation(token)) return;
      setStatus("Stop request sent. Checking run status.");
      await load(undefined, true);
      if (ownsMutation(token)) onChanged();
    } catch (error) { if (ownsMutation(token)) setStatus(errorText(error, "The selected run could not be stopped. Try again.")); }
    finally { mutationGuard.current = false; if (ownsMutation(token)) setWriting(false); }
  }

  async function startAndRecover(candidate: ResearchTask, token: number) {
    if (!ownsMutation(token)) return;
    setStartRecoverable(false); setStatus("Creating research run…");
    try {
      await api.startTask({ taskId: candidate.task_id, expectedScopeVersion: candidate.current_scope_version });
      if (!ownsMutation(token)) return;
      await load(undefined, true);
      if (ownsMutation(token)) onChanged();
      return;
    } catch (error) {
      if (!ownsMutation(token)) return;
      setStatus(`${errorText(error, "The research could not be started")} Checking whether an associated run was created…`);
      try {
        const checked = await api.getTask(candidate.task_id);
        if (!ownsMutation(token)) return;
        setTask(checked);
        if (checked.runs?.length) {
          setStatus("An associated run was found, so it will not be started again. Loading its status.");
          await load(undefined, true);
          if (ownsMutation(token)) onChanged();
        } else {
          setStartRecoverable(true);
          setStatus("No associated run was found. Explicitly select retry; the system will not resend the start request automatically.");
        }
      } catch (checkError) {
        if (ownsMutation(token)) setStatus(`${errorText(checkError, "The task could not be checked")} The start outcome is unknown. Reopen the task later to check.`);
      }
    }
  }

  async function retryStart() {
    if (!task || readOnly || mutationGuard.current || !startRecoverable) return;
    mutationGuard.current = true;
    const token = navigation.current;
    setWriting(true);
    try { await startAndRecover(task, token); }
    finally { mutationGuard.current = false; if (ownsMutation(token)) setWriting(false); }
  }

  async function retryIntent() {
    if (!task || readOnly || mutationGuard.current || !canRetryIntent(task)) return;
    mutationGuard.current = true;
    const token = navigation.current;
    const expectedVersion = task.current_scope_version;
    setWriting(true); setStatus("Reinterpreting the original question…");
    try {
      const retried = await api.retryIntent({ taskId: task.task_id, expectedScopeVersion: expectedVersion });
      if (!ownsMutation(token)) return;
      setTask(retried);
      setCompany(retried.options?.[0]?.company_id ?? "");
      setStartRecoverable(retried.status === "ready" && !(retried.runs?.length));
      setStatus(canRetryIntent(retried)
        ? "The original question still could not be interpreted. The task and existing results were preserved; you can explicitly retry later."
        : retried.status === "needs_clarification"
          ? "The original question was reinterpreted and the research scope needs your confirmation; research will not start automatically."
          : "The original question was reinterpreted. The task remains unchanged and research will not start automatically.");
      onChanged();
    } catch (error) {
      if (!ownsMutation(token)) return;
      setStatus(`${errorText(error, "The reinterpretation outcome is unknown.")} Checking the task’s persisted state…`);
      try {
        const checked = await api.getTask(task.task_id);
        if (!ownsMutation(token)) return;
        setTask(checked);
        setCompany(checked.options?.[0]?.company_id ?? "");
        setStartRecoverable(checked.status === "ready" && !(checked.runs?.length));
        setStatus(checked.current_scope_version > expectedVersion
          ? "The next persisted interpretation version was found, so the model will not be called again."
          : "No new interpretation version was found. Explicitly select “Reinterpret original question” to retry; the system will not resend automatically.");
      } catch (checkError) {
        if (ownsMutation(token)) setStatus(`${errorText(checkError, "The task could not be checked")} The original question remains saved. Reopen the task later.`);
      }
    } finally { mutationGuard.current = false; if (ownsMutation(token)) setWriting(false); }
  }

  async function resolveAndStart() {
    if (!task || !company || readOnly || mutationGuard.current) return;
    mutationGuard.current = true;
    const token = navigation.current;
    setWriting(true); setStatus("Confirming research subject…");
    try {
      const resolved = await api.resolveTask({ taskId: task.task_id, expectedScopeVersion: task.current_scope_version, companyId: company });
      if (!ownsMutation(token)) return;
      setTask(resolved);
      if (resolved.status === "ready" && !(resolved.runs?.length)) await startAndRecover(resolved, token);
      else {
        await load(undefined, true);
        if (ownsMutation(token)) onChanged();
      }
    } catch (error) { if (ownsMutation(token)) setStatus(`${errorText(error, "The research subject could not be confirmed.")} It will not retry automatically.`); }
    finally { mutationGuard.current = false; if (ownsMutation(token)) setWriting(false); }
  }

  const reportHref = task?.latest_report?.reader_href || task?.reader_href;
  const candidateVersion = candidate?.versions.at(-1);
  const candidateHref = candidate && candidateVersion
    ? `/report-reader.html?${new URLSearchParams({ conversation_id: candidate.conversation_id, candidate_id: candidate.candidate_id, version_id: candidateVersion.version_id })}` : null;
  const verificationPresentation = candidate ? presentVerification(candidate) : null;
  const presentation = verificationPresentation ?? (currentRun ? presentRun(currentRun, Boolean(reportHref)) : task ? presentTask(task) : null);
  const decision = currentRun?.decision ?? (conversation?.decision?.run_id === currentRun?.run_id ? conversation?.decision : null);
  const coverage = task?.coverage;
  const coverageRows = coverage ? ("subjects" in coverage ? Object.entries(coverage.subjects) : [["Current subject", coverage] as const]) : [];
  const subjects = task?.scope?.subjects?.length ? task.scope.subjects : task?.subjects ?? [];
  const question = task?.question || conversation?.display_question || conversation?.title || "Research record";

  return <section className={styles.page}>
    <article className={styles.workspace} aria-busy={loading}>
      <header className={styles.workspaceHeader}>
        <div><h1>{question}</h1></div>
        <div className={styles.actionRow}>
          {reportHref && <button type="button" className={styles.primary} onClick={() => onReport(reportHref)}>Read formal report</button>}
          {candidateHref && candidate && ["queued", "running", "unresolved"].includes(candidate.status)
            && <button type="button" onClick={() => onReport(candidateHref)}>View candidate</button>}
        </div>
      </header>
      <section className={`${styles.runStatus} ${presentation ? styles[presentation.tone] : ""}`} aria-labelledby="run-status-title">
        <div><h2 id="run-status-title">{presentation?.label ?? (loading ? "Loading" : "No run yet")}</h2>
          <p className={styles.status} role="status" aria-live="polite">{status}</p></div>
        {loadFailed && <button type="button" disabled={loading} onClick={() => { void load(); }}>Reload research</button>}
        {candidate && ["queued", "running"].includes(candidate.status)
          ? <button type="button" disabled={readOnly || writing} onClick={cancelVerification}>Stop verification</button>
          : currentRun && ACTIVE_RUN_STATUSES.has(currentRun.status) && <button type="button" disabled={readOnly || writing} onClick={cancel}>Stop this run</button>}
      </section>
      {readOnly && (Boolean(currentRun && ACTIVE_RUN_STATUSES.has(currentRun.status)) || task?.status === "needs_clarification")
        && <p className={styles.readOnly}>Read-only preview; no research will be submitted or changed</p>}

      {candidate?.status === "unresolved" && <section className={styles.clarification} aria-labelledby="verification-choice-title">
        <h2 id="verification-choice-title">Choose what to do next</h2>
        <p>Verification did not produce a formal report. Candidate content does not mean the run succeeded; the existing formal report remains unchanged.</p>
        <div className={styles.actionRow}>
          {reportHref && <button type="button" onClick={() => onReport(reportHref)}>Keep and read existing formal report</button>}
          <a href="#/research">Start new research</a>
        </div>
      </section>}

      {task && canRetryIntent(task) && <section className={styles.clarification} aria-labelledby="retry-intent-title">
        <h2 id="retry-intent-title">Original question not yet interpreted</h2>
        <p>{task.message} Reinterpreting adds a scope version to the same task. It will not create a new task, overwrite existing results, or start research automatically.</p>
        <div className={styles.actionRow}><button type="button" className={styles.primary} disabled={readOnly || writing} onClick={retryIntent}>Reinterpret original question</button></div>
        {readOnly && <p className={styles.readOnly}>Read-only preview; the model will not be called and research will not change</p>}
      </section>}

      {task?.status === "ready" && !currentRun && <section className={styles.clarification} aria-labelledby="start-title">
        <h2 id="start-title">Research has not started</h2><p>The question is saved. A start request is sent only after your explicit action.</p>
        <div className={styles.actionRow}><button type="button" className={styles.primary} disabled={readOnly || writing || !startRecoverable} onClick={retryStart}>{startRecoverable ? "Retry start" : "Reopen task to check"}</button></div>
        {readOnly && <p className={styles.readOnly}>Read-only preview; no research will be submitted or changed</p>}
      </section>}

      {task?.status === "needs_clarification" && task.options?.length ? <section className={styles.clarification} aria-labelledby="clarification-title">
        <h2 id="clarification-title">Confirm research subject</h2><p>{task.message}</p>
        <label className={styles.label} htmlFor="research-company">Research subject</label>
        <select id="research-company" value={company} disabled={readOnly || writing} onChange={(event) => setCompany(event.target.value)}>
          {task.options.map((option) => <option key={option.company_id} value={option.company_id}>{option.label}</option>)}
        </select>
        <div className={styles.actionRow}><button type="button" className={styles.primary} disabled={readOnly || writing || !company} onClick={resolveAndStart}>Confirm and start research</button></div>
      </section> : null}

      {decision?.status === "pending" && <DecisionPanel key={decision.decision_id} decision={decision} readOnly={readOnly}
        taskId={taskId} conversationId={conversationId || conversation?.conversation_id} onSettled={() => { void load(undefined, true); onChanged(); }} />}

      <details className={styles.metadata}>
        <summary>Research scope and sources</summary>
        <p><a href={`/legacy.html#/research?${taskId ? `task=${encodeURIComponent(taskId)}` : `conversation=${encodeURIComponent(conversationId || conversation?.conversation_id || "")}`}`}>Materials and notes</a></p>
        {subjects.length ? <ul className={styles.subjects}>{subjects.map((subject, index) => <li key={`${subject.kind}-${subject.id ?? index}`}><strong>{subject.label}</strong><span>{subject.kind}</span></li>)}</ul> : <p className={styles.hint}>No research subject has been identified yet.</p>}
        {task?.scope && <dl className={styles.scopeGrid}>
          {task.scope.period_text && <><dt>Period</dt><dd>{task.scope.period_text}</dd></>}
          {task.scope.metrics.length > 0 && <><dt>Metrics</dt><dd>{task.scope.metrics.join(", ")}</dd></>}
          {task.scope.basis.length > 0 && <><dt>Evidence scope</dt><dd>{task.scope.basis.join(", ")}</dd></>}
          {task.scope.unit && <><dt>Unit</dt><dd>{task.scope.unit}</dd></>}
        </dl>}
        {(task?.coverage || task?.scope?.assumptions.length || task?.scope?.uncertainties.length || task?.scope?.gaps.length) ? <details className={styles.details}>
          <summary>Source coverage and supporting notes</summary>
          {coverageRows.map(([subject, item]) => <p key={subject}>{subject}: {describeCoverage(item)}</p>)}
          {task?.scope?.assumptions.map((item) => <p key={`a-${item}`}>Assumption: {item}</p>)}
          {task?.scope?.uncertainties.map((item) => <p key={`u-${item}`}>Uncertainty: {item}</p>)}
          {task?.scope?.gaps.map((item) => <p key={`g-${item}`}>Source gap: {item}</p>)}
        </details> : null}
      </details>

      {runs.length > 0 && <details className={styles.history}>
        <summary>Run and event history ({runs.length})</summary>
        {[...runs].reverse().map((run) => <section key={run.run_id} className={styles.runHistory}>
          <h3>{presentRun(run, Boolean(reportHref)).label}</h3>
          <p className={styles.hint}>Run {run.run_id} · {run.events?.length ?? 0} events</p>
          {run.events?.length ? <ol>{run.events.map((event) => <li key={event.seq}><strong>{eventLabel(event)}</strong><span>{eventSummary(event)}</span></li>)}</ol> : <p>No events to display.</p>}
        </section>)}
      </details>}
      {!loading && !task && !conversation && <div className={styles.empty}><h2>No research selected</h2><p>Select an item from the research list.</p></div>}
    </article>
  </section>;
}
