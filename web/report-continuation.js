"use strict";

(function reportContinuation(global) {
  const MAX_MESSAGE_LENGTH = 4000;
  const POLL_DELAY = 900;
  let context = null;
  let state = null;
  let pollTimer = null;
  let polling = false;
  let submitting = false;
  let active = false;
  let requestController = null;

  function request(url, options = {}) {
    return fetch(url, { ...options, signal: requestController?.signal });
  }

  function unresolvedSubmission() {
    return Boolean(state?.request_id && state.submitted_message && state.status === "submitting");
  }

  const list = value => Array.isArray(value) ? value : [];
  const text = (value, fallback = "") => value === null || value === undefined ? fallback : String(value);
  const node = (tag, options = {}, children = []) => {
    const result = document.createElement(tag);
    if (options.className) result.className = options.className;
    if (options.text !== undefined) result.textContent = text(options.text);
    for (const [key, value] of Object.entries(options.attrs || {})) {
      if (value !== null && value !== undefined) result.setAttribute(key, String(value));
    }
    for (const child of Array.isArray(children) ? children : [children]) if (child) result.append(child);
    return result;
  };

  function requestId() {
    if (global.crypto && typeof global.crypto.randomUUID === "function") return `web_${global.crypto.randomUUID()}`;
    const bytes = new Uint8Array(16);
    global.crypto.getRandomValues(bytes);
    return `web_${Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("")}`;
  }

  function storageKey(report) {
    return `report-continuation/1/${report.conversation_id}/${report.id}`;
  }

  function freshState() {
    return {
      draft: "", request_id: "", submitted_message: "", run_id: "", cursor: 0,
      status: "idle", feedback: "", errorKind: "", reportIndex: null,
      capabilities: null, checking: false, report: null, decision: null,
      decisionPending: null, messageIds: [], baselineReportIds: [],
    };
  }

  function restore(report) {
    const initial = freshState();
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey(report)) || "null");
      if (!saved || saved.base_message_id !== report.message_id) return initial;
      for (const key of ["draft", "request_id", "submitted_message", "run_id", "status", "feedback", "errorKind"]) {
        if (typeof saved[key] === "string") initial[key] = saved[key];
      }
      if (Number.isInteger(saved.cursor) && saved.cursor >= 0) initial.cursor = saved.cursor;
      initial.messageIds = list(saved.messageIds).filter(value => typeof value === "string").slice(-4);
      initial.baselineReportIds = list(saved.baselineReportIds).filter(value => typeof value === "string");
      if (saved.decisionPending && typeof saved.decisionPending === "object") initial.decisionPending = saved.decisionPending;
      if (!initial.run_id && initial.status !== "submitting") initial.status = "idle";
      return initial;
    } catch (_) {
      return initial;
    }
  }

  function persist() {
    if (!context || !state) return;
    const report = context.payload.report;
    const saved = {
      base_message_id: report.message_id,
      // The draft is tab-scoped, limited to 4,000 characters, and removed after completion.
      draft: state.draft.slice(0, MAX_MESSAGE_LENGTH),
      request_id: state.request_id,
      submitted_message: state.submitted_message.slice(0, MAX_MESSAGE_LENGTH),
      run_id: state.run_id, cursor: state.cursor, status: state.status,
      feedback: state.feedback, errorKind: state.errorKind,
      messageIds: state.messageIds.slice(-4), baselineReportIds: state.baselineReportIds,
      decisionPending: state.decisionPending,
    };
    try { sessionStorage.setItem(storageKey(report), JSON.stringify(saved)); } catch (_) {}
  }

  function clearSaved() {
    if (!context) return;
    try { sessionStorage.removeItem(storageKey(context.payload.report)); } catch (_) {}
  }

  function panelOpen() {
    return context && context.isPanelOpen();
  }

  async function responseBody(response) {
    try { return await response.json(); } catch (_) { return {}; }
  }

  function errorCode(body) {
    if (typeof body.error === "string") return body.error;
    return body.error?.code || body.code || "";
  }

  function errorMessage(body, fallback) {
    const message = body.error?.message || body.message;
    return typeof message === "string" && /[\u3400-\u9fff]/u.test(message) ? message : fallback;
  }

  function reportVersion(item) {
    const value = Number(item && item.version);
    return Number.isFinite(value) ? value : -1;
  }

  function versionSituation() {
    const reports = list(state.reportIndex?.reports);
    const current = reports.find(item => item.report_id === context.payload.report.id);
    if (!current) return { current: null, latest: null, newer: false };
    const latest = reports.reduce((best, item) => reportVersion(item) > reportVersion(best) ? item : best, current);
    return { current, latest, newer: reportVersion(latest) > reportVersion(current) };
  }

  function reportHref(item) {
    return `/report-reader.html?${new URLSearchParams({ report_id: item.report_id })}`;
  }

  function actionButton(label, handler, secondary = false) {
    const button = node("button", { className: `continuation-button${secondary ? " secondary" : ""}`, text: label, attrs: { type: "button" } });
    button.addEventListener("click", handler);
    return button;
  }

  function statusNotice(message, kind = "") {
    return node("p", { className: `continuation-status${kind ? ` ${kind}` : ""}`, text: message, attrs: { role: "status", "aria-live": "polite" } });
  }

  function rememberEventIdentity(events) {
    for (const event of list(events)) {
      for (const candidate of [event.message_id, event.assistant_message_id, event.result?.message_id]) {
        if (typeof candidate === "string" && !state.messageIds.includes(candidate)) state.messageIds.push(candidate);
      }
    }
    state.messageIds = state.messageIds.slice(-4);
  }

  function latestLinkBlock(situation, explanation) {
    if (!situation.latest) return statusNotice(explanation, "warning");
    const block = node("section", { className: "continuation-version-warning" }, [
      node("strong", { text: `You are reading version ${situation.current?.version ?? "current"}; version ${situation.latest.version} is available.` }),
      node("p", { text: explanation }),
      node("div", { className: "continuation-actions" }, [
        node("a", { className: "continuation-button", text: "Open latest version to continue", attrs: { href: reportHref(situation.latest) } }),
        actionButton("Keep reading this version", () => context.closePanel(), true),
      ]),
    ]);
    return block;
  }

  function renderDecision(decision) {
    const section = node("section", { className: "continuation-decision", attrs: { "aria-label": "Research confirmation required", "data-decision-id": decision.decision_id } });
    if (decision.request_message) section.append(node("p", { className: "continuation-original-question", text: `Question to continue: ${decision.request_message}` }));
    section.append(node("h3", { text: decision.question || "Continuing the research requires your confirmation" }));
    if (decision.explanation) section.append(node("p", { text: decision.explanation }));
    if (decision.kind !== "time") section.append(node("p", { className: "continuation-muted", text: "After confirmation, the materials available when the options were proposed will be reused; they will not be refreshed automatically." }));
    if (decision.status && decision.status !== "pending") {
      section.append(statusNotice(decision.status === "accepted" ? "Confirmed. Research is continuing." : "This change was declined. The original report remains unchanged."));
      return section;
    }
    const groups = list(decision.groups);
    const options = list(decision.options);
    const fields = node("fieldset", {}, node("legend", { text: groups.length ? "Confirm each research choice below" : decision.multiple ? "You may select multiple approaches for parallel research" : "Select the option to use for this research" }));
    const choiceGroups = groups.length ? groups : [{ id: "all", option_ids: options.map(option => option.id), minimum: 1, maximum: decision.multiple ? options.length : 1 }];
    for (const group of choiceGroups) {
      const target = groups.length ? node("fieldset", { attrs: { "data-decision-group": group.id } }, [
        node("legend", { text: group.label || "Research choice" }),
        node("p", { className: "continuation-muted", text: group.maximum === 1 ? "Select one option" : `Select ${group.minimum} to ${group.maximum} options for parallel research` }),
      ]) : fields;
      for (const option of options.filter(item => list(group.option_ids).includes(item.id))) {
        const input = node("input", { attrs: { type: group.maximum > 1 ? "checkbox" : "radio", name: `${decision.decision_id}-${group.id}`, value: option.id } });
        target.append(node("label", { className: "continuation-decision-option" }, [input, node("span", {}, [
          node("strong", { text: option.label }),
          option.benefit ? node("span", { text: `Purpose and benefits: ${option.benefit}` }) : null,
          option.limitation ? node("span", { text: `Limitations and trade-offs: ${option.limitation}` }) : null,
        ])]));
      }
      if (groups.length) fields.append(target);
    }
    const notes = node("textarea", { attrs: { rows: "2", maxlength: String(MAX_MESSAGE_LENGTH), "aria-label": "Additional requirements (optional)" } });
    const feedback = statusNotice("");
    fields.addEventListener("change", () => { feedback.textContent = ""; });
    const accept = actionButton("Confirm and continue research", () => answerDecision("accepted"));
    const decline = actionButton("Decline and keep original question", () => answerDecision("declined"), true);
    const recover = actionButton("Reload confirmation status", () => schedulePoll(0), true);
    async function answerDecision(action) {
      const selected = action === "accepted" ? [...fields.querySelectorAll("input:checked")].map(input => input.value) : [];
      if (action === "accepted" && !selected.length) { feedback.textContent = "Select an option for this research first."; return; }
      if (action === "accepted") {
        const invalid = groups.find(group => {
          const count = selected.filter(id => list(group.option_ids).includes(id)).length;
          return count < group.minimum || count > group.maximum;
        });
        if (invalid) { feedback.textContent = `For “${invalid.label}”, select ${invalid.minimum} to ${invalid.maximum} options.`; return; }
      }
      const payload = { decision_id: decision.decision_id, selected_ids: selected, notes: notes.value, action };
      const signature = JSON.stringify(payload);
      if (!state.decisionPending || state.decisionPending.signature !== signature) state.decisionPending = { signature, request_id: requestId() };
      persist();
      fields.disabled = notes.disabled = accept.disabled = decline.disabled = true;
      feedback.textContent = "Saving your selection…";
      try {
        const response = await request("/api/research/decisions/answer", {
          method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ ...payload, request_id: state.decisionPending.request_id }),
        });
        if (context.authenticationRequired(response)) return;
        const result = await responseBody(response);
        if (!response.ok) throw Object.assign(new Error(errorMessage(result, "Please try again")), { response, result });
        if (result.conversation_id !== context.payload.report.conversation_id
          || (action === "accepted" && (typeof result.run_id !== "string" || !result.run_id))
          || (action === "declined" && (result.run_id !== null || result.status !== "declined"))) throw new Error("decision_result_unknown");
        state.decisionPending = null;
        state.decision = null;
        state.run_id = result.run_id || "";
        state.cursor = 0; state.messageIds = [];
        if (action === "declined") {
          state.request_id = ""; state.submitted_message = "";
          state.status = "idle";
          state.feedback = "This change was declined. The original report remains unchanged. You can revise the question and submit it again.";
        } else {
          state.status = result.status || "queued";
          state.feedback = "Your selection was saved. Research is continuing.";
        }
        persist(); render();
        if (state.run_id) schedulePoll(0);
      } catch (error) {
        feedback.textContent = "Could not confirm whether the selection was saved. Reload the status or try saving the current selection again.";
        recover.hidden = false;
        fields.disabled = notes.disabled = accept.disabled = decline.disabled = false;
      }
    }
    section.append(fields, node("label", { className: "continuation-notes" }, [node("span", { text: "Additional requirements (optional)" }), notes]), node("div", { className: "continuation-actions" }, [accept, decline]), feedback, recover);
    return section;
  }

  function renderActive(body) {
    const labels = { queued: "Research queued", running: "Research in progress", cancelling: "Cancelling", cancelled: "Research cancelled", failed: "Research could not be completed", blocked: "Research paused pending action" };
    body.append(statusNotice(labels[state.status] || "Loading research status"));
    if (state.feedback) body.append(statusNotice(state.feedback, state.errorKind === "poll" ? "error" : ""));
    if (state.decision) body.append(renderDecision(state.decision));
    if (["queued", "running", "cancelling"].includes(state.status)) {
      body.append(node("div", { className: "continuation-actions" }, [actionButton("Cancel this continued research", cancelRun, true)]));
    }
    if (state.errorKind === "poll") body.append(actionButton("Reload status", () => schedulePoll(0)));
    if (["failed", "cancelled"].includes(state.status)) body.append(actionButton("Revise question and submit as a new request", () => {
      state.run_id = ""; state.request_id = ""; state.submitted_message = "";
      state.status = "idle"; state.feedback = ""; state.errorKind = ""; state.decision = null;
      persist(); render();
    }, true));
  }

  function renderCompleted(body) {
    if (state.report) {
      body.append(statusNotice("Continued research is complete and a new formal report is available.", "success"));
      body.append(node("a", { className: "continuation-button", text: "Open new report", attrs: { href: reportHref(state.report) } }));
      return;
    }
    body.append(statusNotice("The research has finished, but a new formal report is not yet available. You can check for updates later without repeating the research.", "warning"));
    body.append(actionButton("Check for report updates", locateCompletedReport));
  }

  function renderForm(body) {
    const situation = versionSituation();
    if (situation.newer && !unresolvedSubmission()) {
      body.append(latestLinkBlock(situation, "Open the latest report before continuing research. The version you are reading will not be switched or rewritten automatically."));
      return;
    }
    if (state.errorKind === "context") {
      if (situation.newer) body.append(latestLinkBlock(situation, "The research baseline changed before submission. Open the latest version and ask the question again."));
      else body.append(statusNotice("This research has updates, but no new report version is available yet. Check again later; this older version remains readable.", "warning"));
      body.append(actionButton("Check for report updates", checkReadiness, true));
      return;
    }
    const form = node("form", { className: "continuation-form" });
    const input = node("textarea", { attrs: { rows: "5", maxlength: String(MAX_MESSAGE_LENGTH), placeholder: "For example: Investigate counterexamples to this conclusion and explain how they affect it.", required: "", "aria-label": "Question for continued research" } });
    input.value = state.draft;
    input.readOnly = submitting || unresolvedSubmission();
    input.addEventListener("input", () => {
      state.draft = input.value;
      if (!state.submitted_message || state.draft !== state.submitted_message) {
        state.request_id = "";
        if (state.status === "failed" || state.status === "cancelled") state.status = "idle";
        state.errorKind = ""; state.feedback = "";
      }
      persist();
    });
    const submit = actionButton(state.status === "submitting" ? "Retry the same submission" : "Submit and continue research", () => {});
    submit.setAttribute("type", "submit");
    submit.disabled = state.capabilities !== true || state.checking || submitting;
    form.addEventListener("submit", event => { event.preventDefault(); submitMessage(input.value); });
    form.append(node("label", {}, [node("span", { text: "What would you like to research further?" }), input]),
      node("p", { className: "continuation-muted", text: "Your question will be retained in this tab, up to 4,000 characters. Existing reports will not be overwritten." }),
      submit);
    body.append(form);
    if (state.capabilities === false) body.append(statusNotice("This service version does not support continuing research from a report. Wait for a service update before submitting; the legacy submission endpoint will not be used.", "warning"));
    else if (state.checking) body.append(statusNotice("Checking service capabilities and report version…"));
    else if (state.feedback) body.append(statusNotice(state.feedback, state.errorKind ? "error" : ""));
    if (state.errorKind === "readiness") body.append(actionButton("Recheck connection and version", checkReadiness, true));
  }

  function render() {
    if (!active || !panelOpen()) return;
    const body = context.body;
    const ownedFocusWasDetached = body.contains(document.activeElement);
    body.replaceChildren(node("section", { className: "continuation-intro" }, [
      node("p", { className: "continuation-version", text: `Reading: ${versionSituation().current ? `version ${versionSituation().current.version}` : "currently saved version"} · ${context.payload.report.title || "Research report"}` }),
      node("p", { text: "Follow-up questions will reuse this research’s materials and scope. The original report remains unchanged." }),
    ]));
    // Do not expose a selectable confirmation form while the initial reads can
    // still replace it. A refresh/open may have readiness and run reads in flight.
    if (state.decision && (state.checking || polling)) body.append(statusNotice("Checking the latest status of this confirmation…"));
    else if (state.status === "completed") renderCompleted(body);
    else if (state.run_id || ["queued", "running", "cancelling"].includes(state.status) || state.decision) renderActive(body);
    else renderForm(body);
    if (ownedFocusWasDetached && !context.panel.contains(document.activeElement)) {
      context.panel.focus({ preventScroll: true });
    }
  }

  async function fetchReportIndex() {
    const query = new URLSearchParams({ conversation_id: context.payload.report.conversation_id });
    const response = await request(`/api/reports?${query}`, { headers: { Accept: "application/json" } });
    if (context.authenticationRequired(response)) return null;
    if (!response.ok) throw new Error("Report versions are temporarily unavailable");
    return response.json();
  }

  async function fetchConversation() {
    const query = new URLSearchParams({ id: context.payload.report.conversation_id });
    const response = await request(`/api/chat/conversation?${query}`, { headers: { Accept: "application/json" } });
    if (context.authenticationRequired(response)) return null;
    if (!response.ok) throw new Error("The conversation confirmation status is temporarily unavailable");
    return response.json();
  }

  function claimConversationDecision(conversation) {
    // Never replace a tab's exact lost-response recovery key or an already-owned run.
    if (state.run_id || unresolvedSubmission() || state.decisionPending
      || (state.request_id && state.submitted_message)) return false;
    const decision = conversation?.decision;
    const report = context.payload.report;
    if (conversation?.conversation_id !== report.conversation_id || !decision
      || decision.base_message_id !== report.message_id || versionSituation().newer
      || typeof decision.decision_id !== "string" || typeof decision.run_id !== "string") return false;
    if (decision.status === "pending") {
      state.run_id = decision.run_id;
      state.cursor = 0; state.messageIds = [];
      state.status = "blocked"; state.decision = decision;
      state.feedback = "A pending confirmation for this research was found on another page. Review the original follow-up question and choices before deciding.";
      state.errorKind = "";
      return true;
    }
    if (decision.status === "accepted" && typeof decision.continuation_run_id === "string" && decision.continuation_run_id) {
      state.run_id = decision.continuation_run_id;
      state.cursor = 0; state.messageIds = [];
      state.status = "queued"; state.decision = null; state.decisionPending = null;
      state.feedback = "Confirmed on another page. Resuming the follow-up research."; state.errorKind = "";
      return true;
    }
    if (decision.status === "declined") {
      state.status = "idle"; state.decision = null; state.decisionPending = null;
      state.feedback = "This change was declined on another page. The original report remains unchanged."; state.errorKind = "";
      return true;
    }
    return false;
  }

  async function checkReadiness() {
    state.checking = true; state.feedback = ""; render();
    try {
      const [statusResponse, reportIndex, conversation] = await Promise.all([
        request("/api/chat/status", { headers: { Accept: "application/json" } }), fetchReportIndex(), fetchConversation(),
      ]);
      if (context.authenticationRequired(statusResponse)) return;
      const status = await responseBody(statusResponse);
      if (!statusResponse.ok) throw new Error("Unable to check continued-research capability at this time");
      state.capabilities = status.capabilities?.report_continuation === true;
      state.reportIndex = reportIndex;
      claimConversationDecision(conversation);
      if (state.errorKind !== "context") state.errorKind = "";
    } catch (error) {
      state.capabilities = null;
      state.feedback = "Unable to check the connection and report version. Check your connection and try again.";
      state.errorKind = "readiness";
    } finally {
      state.checking = false; persist(); render();
      if (state.run_id && ["queued", "running", "cancelling"].includes(state.status)) schedulePoll(0);
    }
  }

  async function submitMessage(value) {
    const message = text(value).trim();
    if (!message) { state.feedback = "Enter the question you want to research further."; render(); return; }
    if (message.length > MAX_MESSAGE_LENGTH) { state.feedback = `The question cannot exceed ${MAX_MESSAGE_LENGTH} characters.`; render(); return; }
    if (submitting || state.run_id) return;
    const replay = unresolvedSubmission() && state.submitted_message === message;
    submitting = true;
    try {
      await checkReadiness();
      // A lost successful response may already have produced a newer report.
      // Replay its exact key before asking the user to switch report versions.
      if (state.capabilities !== true || state.run_id || state.decision
        || (!replay && versionSituation().newer)) { render(); return; }
      if (!state.request_id || state.submitted_message !== message) {
        state.request_id = requestId();
        state.submitted_message = message;
        state.baselineReportIds = list(state.reportIndex?.reports).map(item => item.report_id);
        state.cursor = 0; state.messageIds = [];
      }
      state.draft = message;
      state.status = "submitting";
      state.feedback = "Submitting. Research will not be started again before the result is confirmed.";
      state.errorKind = ""; persist(); render();
      try {
        const report = context.payload.report;
        const response = await request("/api/chat/messages", {
          method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ conversation_id: report.conversation_id, message, request_id: state.request_id, base_message_id: report.message_id }),
        });
        if (context.authenticationRequired(response)) return;
        const result = await responseBody(response);
        if (!response.ok) {
          if (response.status >= 500 || response.status === 429) throw new Error("submission_result_unknown");
          const code = errorCode(result);
          if (response.status === 409 && code === "report_context_conflict") {
            state.status = "failed"; state.errorKind = "context";
            state.feedback = "The research baseline changed, so no new research was started.";
            try { state.reportIndex = await fetchReportIndex(); } catch (_) {}
          } else if (response.status === 409 && code === "idempotency_conflict") {
            state.status = "failed"; state.errorKind = "idempotency";
            state.feedback = "This submission does not match the previous question. Review and edit the question before submitting again.";
          } else {
            state.status = "failed"; state.errorKind = code || "submit";
            state.feedback = errorMessage(result, `Submission failed (${response.status}); no other research endpoint was used automatically.`);
            if (result.decision) state.decision = result.decision;
          }
          persist(); render(); return;
        }
        if (typeof result.run_id !== "string" || !result.run_id || result.conversation_id !== report.conversation_id) throw new Error("submission_result_unknown");
        state.run_id = result.run_id;
        state.status = result.status || "queued";
        state.feedback = ""; state.errorKind = "";
        if (result.decision) state.decision = result.decision;
        persist(); render(); schedulePoll(0);
      } catch (_) {
        // The server may have accepted the POST. Keep the exact body and request ID;
        // an explicit retry replays this request rather than creating another run.
        state.status = "submitting"; state.errorKind = "lost_response";
        state.feedback = "No submission response was received. Retry to recover this research; the system will claim an existing submitted result rather than starting it again.";
        persist(); render();
      }
    } finally {
      submitting = false; render();
    }
  }

  function schedulePoll(delay = POLL_DELAY) {
    clearTimeout(pollTimer);
    if (!active) return;
    if (!state?.run_id) return;
    pollTimer = setTimeout(pollRun, delay);
  }

  async function pollRun() {
    if (polling || !state?.run_id) return;
    polling = true;
    const polledRunId = state.run_id;
    try {
      const query = new URLSearchParams({ id: polledRunId, after: String(state.cursor) });
      const response = await request(`/api/chat/run?${query}`, { headers: { Accept: "application/json" } });
      if (context.authenticationRequired(response)) return;
      const run = await responseBody(response);
      if (state.run_id !== polledRunId) return;
      if (!response.ok) throw new Error(errorMessage(run, "Research status is temporarily unavailable"));
      if (run.run_id !== state.run_id || run.conversation_id !== context.payload.report.conversation_id) throw new Error("The returned research status does not match the current report");
      if (run.decision?.status === "accepted" && run.decision.continuation_run_id && run.decision.continuation_run_id !== state.run_id) {
        state.run_id = run.decision.continuation_run_id; state.cursor = 0; state.messageIds = [];
        state.status = "queued"; state.decision = null; state.decisionPending = null;
        state.feedback = "Resuming confirmed research."; state.errorKind = "";
        persist(); render(); schedulePoll(); return;
      }
      if (run.decision?.status === "declined") {
        state.run_id = ""; state.request_id = ""; state.submitted_message = ""; state.cursor = 0;
        state.status = "idle"; state.decision = null; state.decisionPending = null;
        state.feedback = "This change was not accepted. The original report remains unchanged."; state.errorKind = "";
        persist(); render(); return;
      }
      rememberEventIdentity(run.events);
      if (Number.isInteger(run.next_cursor) && run.next_cursor >= state.cursor) state.cursor = run.next_cursor;
      state.status = run.status;
      state.decision = run.decision?.status === "pending" ? run.decision : null;
      state.feedback = run.error ? errorMessage(run, "Research could not be completed") : "";
      state.errorKind = run.error ? "run" : "";
      persist(); render();
      if (["queued", "running", "cancelling"].includes(run.status)) schedulePoll();
      else if (run.status === "completed") await locateCompletedReport();
    } catch (error) {
      if (state.run_id !== polledRunId) return;
      state.errorKind = "poll";
      state.feedback = "Unable to load research progress. Check your connection and reload the status; the question will not be submitted again.";
      persist(); render();
    } finally {
      polling = false;
      render();
      if (state.run_id && state.run_id !== polledRunId) schedulePoll(0);
    }
  }

  async function cancelRun() {
    if (!state.run_id || state.status === "cancelling") return;
    const previousStatus = state.status;
    state.status = "cancelling"; state.feedback = "Cancelling this continued research…"; persist(); render();
    try {
      const response = await request("/api/chat/cancel", {
        method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ run_id: state.run_id }),
      });
      if (context.authenticationRequired(response)) return;
      const result = await responseBody(response);
      if (!response.ok) throw new Error(errorMessage(result, "Cancellation failed"));
      state.status = result.status || "cancelling"; state.feedback = ""; persist(); render(); schedulePoll(0);
    } catch (error) {
      state.status = previousStatus;
      state.feedback = "Cancellation has not been confirmed. You can cancel again or reload the research status.";
      state.errorKind = "poll"; persist(); render();
    }
  }

  async function locateCompletedReport() {
    try {
      state.reportIndex = await fetchReportIndex();
      const reports = list(state.reportIndex?.reports);
      state.report = reports.find(item => item.run_id === state.run_id)
        || reports.find(item => state.messageIds.includes(item.message_id)) || null;
      state.status = "completed"; state.feedback = ""; state.errorKind = "";
      if (state.report) {
        state.draft = ""; state.submitted_message = "";
        clearSaved();
      } else persist();
      render();
    } catch (error) {
      state.status = "completed"; state.errorKind = "reports";
      state.feedback = error.message || "The formal report index is temporarily unavailable";
      persist(); render();
    }
  }

  function open() {
    context.openPanel();
    render();
    checkReadiness();
    if (state.run_id && state.status === "completed") locateCompletedReport();
    else if (state.run_id && !["cancelled", "failed"].includes(state.status)) schedulePoll(0);
  }

  function attach(options) {
    if (!options?.payload?.report?.id || !options.payload.report.conversation_id || !options.payload.report.message_id) return;
    dispose();
    active = true;
    requestController = new AbortController();
    context = options;
    state = restore(options.payload.report);
    options.trigger.hidden = false;
    if (!options.trigger.dataset.continuationBound) {
      options.trigger.dataset.continuationBound = "true";
      options.trigger.addEventListener("click", open);
    }
    if (state.run_id && !["completed", "cancelled", "failed"].includes(state.status)) schedulePoll(0);
  }

  function dispose() {
    active = false;
    clearTimeout(pollTimer);
    pollTimer = null;
    requestController?.abort();
    requestController = null;
    if (context?.trigger) {
      context.trigger.removeEventListener("click", open);
      delete context.trigger.dataset.continuationBound;
      context.trigger.hidden = true;
    }
  }

  global.ReportContinuation = { attach, dispose };
}(window));
