"use strict";

(function reportReader(global) {
  const SUPPORTED_SCHEMA = "report-reader-payload/1";
  const state = { payload: null, pages: [], lastFocus: null, request: null, panelRevision: 0, page: 0, readingMode: "single" };
  let smallScreen;
  let shell;
  let wideScreen;
  let root;
  let toc;
  let title;
  let meta;
  let panel;
  let backdrop;
  let evidenceTitle;
  let evidenceBody;
  let infoButton;
  let tocButton;
  let themeButton;
  let detailsFrame;
  let resizeFrame;
  const lifecycle = { mounted: false, generation: 0, controller: null, cleanups: [], callbacks: {} };

  function text(value, fallback = "") {
    return value === null || value === undefined ? fallback : String(value);
  }

  function el(tag, options = {}, children = []) {
    const node = document.createElement(tag);
    if (options.className) node.className = options.className;
    if (options.text !== undefined) node.textContent = text(options.text);
    for (const [key, value] of Object.entries(options.attrs || {})) {
      if (value !== undefined && value !== null) node.setAttribute(key, String(value));
    }
    for (const child of Array.isArray(children) ? children : [children]) {
      if (child) node.append(child);
    }
    return node;
  }

  function list(value) {
    return Array.isArray(value) ? value : [];
  }

  function verifierOutcomeLabel(outcome) {
    return ({
      supported: "Machine verification: supported",
      corrected_and_rechecked: "Machine verification: corrected and rechecked",
      conflict_disclosed: "Machine verification: conflict disclosed",
      insufficient_material_disclosed: "Machine verification: insufficient material disclosed",
    })[outcome] || "";
  }

  function renderRequiredVerifier(findings, preciseBindingStatus) {
    const items = list(findings).filter(item => item && verifierOutcomeLabel(item.outcome));
    if (!items.length) return null;
    return el("section", { className: "evidence-card reader-machine-verification" }, [
      el("h3", { text: "Independent model verification" }),
      el("ul", {}, items.map(item => el("li", {}, [
        el("strong", { text: verifierOutcomeLabel(item.outcome) || item.outcome_label }),
        item.explanation ? document.createTextNode(`: ${item.explanation}`) : null,
      ]))),
      preciseBindingStatus ? el("p", { text: (preciseBindingStatus === "未建立"
        ? "Verification applies to the whole passage; evidence is not annotated sentence by sentence."
        : `Point-by-point review: ${preciseBindingStatus}.`) + "Model verification does not replace human approval or eliminate uncertainty in analytical judgments and assumptions." }) : null,
    ]);
  }

  function validatePayload(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("Report data is not a valid object.");
    if (payload.schema_version !== SUPPORTED_SCHEMA) {
      throw new Error(payload.schema_version
        ? `Unsupported report data version: ${text(payload.schema_version)}`
        : "The report data has no version and cannot be opened safely.");
    }
    if (!payload.report || typeof payload.report !== "object" || typeof payload.report.id !== "string" || !payload.report.id
      || typeof payload.report.title !== "string" || !payload.report.title.trim()) {
      throw new Error("The report data has no valid report identity or title.");
    }
    if (!payload.question || typeof payload.question !== "object"
      || (!text(payload.question.original).trim() && !text(payload.question.normalized).trim())) {
      throw new Error("The report data has no research question.");
    }
    if (!Array.isArray(payload.chapters) || !payload.chapters.length
      || payload.chapters.some(chapter => !chapter || typeof chapter.id !== "string" || !chapter.id
        || typeof chapter.title !== "string" || !Array.isArray(chapter.blocks))) {
      throw new Error("The report data has no valid chapter structure.");
    }
    if (!Array.isArray(payload.sources) || !Array.isArray(payload.anchors)) {
      throw new Error("The report data has no source or evidence collection.");
    }
    return payload;
  }

  function setError(message) {
    title.textContent = "Unable to open report";
    meta.textContent = "";
    const card = el("div", { className: "reader-error" }, [
      el("p", { text: message }),
      el("p", { text: "Open a published final report from “My Research.” Unpublished earlier answers do not appear in the report reader." }),
      el("div", { className: "reader-error-actions" }, [
        el("a", { text: "Back to research history", attrs: { href: "/#/research?tab=history" } }),
      ]),
    ]);
    root.replaceChildren(card);
  }

  function readerLoginUrl() {
    const input = new URLSearchParams(location.search);
    const output = new URLSearchParams();
    for (const key of ["report_id", "conversation_id", "message_id", "candidate_id", "version_id", "page", "reading"]) {
      if (input.has(key)) output.set(key, input.get(key));
    }
    return `/#/report-reader?${output}`;
  }

  function authenticationRequired(response, target) {
    if (response.status !== 401) return false;
    if (target) target.replaceChildren(el("p", { className: "reader-error", text: "Your session has expired. Returning to sign-in; after signing in, you will return to this report and page." }));
    location.replace(readerLoginUrl());
    return true;
  }

  async function loadPayload(options, signal) {
    const params = new URLSearchParams(global.location.search);
    if (options.reportId) params.set("report_id", options.reportId);
    if (options.conversationId) params.set("conversation_id", options.conversationId);
    if (options.messageId) params.set("message_id", options.messageId);
    const conversation = params.get("conversation_id");
    const message = params.get("message_id");
    const report = params.get("report_id");
    const candidate = params.get("candidate_id");
    const version = params.get("version_id");
    if (!report && !(conversation && message) && !(conversation && candidate && version)) {
      setError("No formal report or candidate version identity was provided. Open the report from research history.");
      return null;
    }
    const candidateQuery = candidate && version && conversation
      ? new URLSearchParams({ conversation_id: conversation, candidate_id: candidate, version_id: version }) : null;
    const query = report ? new URLSearchParams({ report_id: report }) : new URLSearchParams({ conversation_id: conversation, message_id: message });
    const response = await fetch(`${candidateQuery ? "/api/verification/reader" : "/api/reports/reader"}?${candidateQuery || query}`, { headers: { Accept: "application/json" }, signal });
    if (authenticationRequired(response, root)) return null;
    if (!response.ok) throw new Error(`reader payload request failed: ${response.status}`);
    return validatePayload(await response.json());
  }

  function usefulSubsectionTitle(block) {
    const value = text(block && block.title).trim();
    if (!value || /^(?:表格|图表|数据集|数据表|来源|限制|问题|结论)(?:组件)?$/u.test(value)) return "";
    return value.replace(/^(?:图表|表格|数据表)\s*[：:]\s*/u, "");
  }

  function pageFor(pages, predicate) {
    const index = pages.findIndex(items => items.some(predicate));
    return Math.max(0, index);
  }

  function renderToc(chapters, pages, coreBlockId) {
    const nodes = [];
    chapters.forEach((chapter, chapterIndex) => {
      const majorPage = pageFor(pages, item => item.kind === "chapter" && item.chapterId === chapter.id);
      const group = el("div", { className: "toc-chapter" });
      group.append(el("a", { className: "toc-item toc-major", attrs: {
        href: `#${chapter.id}`, "data-page": majorPage, "data-toc-chapter": chapterIndex,
        title: chapter.title, "aria-label": `${chapter.title}, page ${majorPage + 1}`,
      } }, [el("strong", { text: chapter.title }), el("span", { text: majorPage + 1 })]));
      const candidates = [];
      for (const block of list(chapter.blocks)) {
        let label = usefulSubsectionTitle(block);
        if (block.type === "question") label = "Research question";
        if (block.id === coreBlockId) label = "Key conclusion";
        if (!label || candidates.some(item => item.label === label)) continue;
        candidates.push({ label, block });
      }
      candidates.forEach(({ label, block }) => {
        const page = pageFor(pages, item => item.kind === "block" && item.block.id === block.id);
        group.append(el("a", { className: "toc-item toc-sub", attrs: {
          href: `#block-${block.id}`, "data-page": page, "data-toc-chapter": chapterIndex,
          title: label, "aria-label": `${label}, page ${page + 1}`,
        } }, [el("strong", { text: label }), el("span", { text: page + 1 })]));
      });
      nodes.push(group);
    });
    toc.replaceChildren(...nodes);
  }

  async function loadVersions(payload, signal) {
    const container = document.getElementById("reader-versions");
    container.hidden = false;
    if (payload.report.kind === "verification_candidate") {
      container.textContent = "Checking the candidate’s persisted verification status…";
      try {
        const identity = new URLSearchParams({
          conversation_id: payload.report.conversation_id,
          candidate_id: payload.report.candidate_id,
        });
        const response = await fetch(`/api/verification/candidate?${identity}`, { headers: { Accept: "application/json" }, signal });
        if (authenticationRequired(response, container)) return;
        if (!response.ok) throw new Error("candidate status unavailable");
        const candidate = await response.json();
        const fixed = el("span", { text: `Currently showing the fixed candidate version ${list(candidate.versions).find(item => item.version_id === payload.report.candidate_version_id)?.version_number || "?"}; the report body will not be replaced automatically.` });
        const nodes = [fixed];
        if (candidate.formal_message_id) {
          const reportsResponse = await fetch(`/api/reports?${new URLSearchParams({ conversation_id: candidate.conversation_id })}`, { headers: { Accept: "application/json" }, signal });
          if (reportsResponse.ok) {
            const index = await reportsResponse.json();
            const formal = list(index.reports).find(item => item.message_id === candidate.formal_message_id);
            if (formal) nodes.push(el("a", { text: "Verification complete: open new formal version", attrs: { href: formal.reader_href } }));
          }
        } else {
          nodes.push(el("span", { text: candidate.status === "unresolved" ? "Verification is incomplete; no formal report was created." : "The candidate is still being verified and will not appear among formal reports." }));
        }
        container.replaceChildren(...nodes);
      } catch (_) {
        container.textContent = "The candidate body remains fixed at the current version; verification status cannot be refreshed right now.";
      }
      return;
    }
    container.textContent = "Checking report versions and correction records…";
    try {
      const query = new URLSearchParams({ conversation_id: payload.report.conversation_id });
      const response = await fetch(`/api/reports?${query}`, { headers: { Accept: "application/json" }, signal });
      if (authenticationRequired(response, container)) return;
      if (!response.ok) throw new Error("version index unavailable");
      const index = await response.json();
      const versions = list(index.reports);
      const current = versions.find(item => item.report_id === payload.report.id);
      if (!current) throw new Error("current report missing from index");
      const clean = value => text(value).replace(/\s*\[(?:claim|evidence|passage|calculation|fact|query):[^\]\s]+\]/g, "");
      const reportLink = (reportId, label) => el("a", {
        text: label, attrs: { href: `/report-reader.html?${new URLSearchParams({ report_id: reportId })}` },
      });
      const describeItem = (section, value) => {
        const item = value && value.from ? value.from : value;
        if (section === "claims") return item.text || "empty-text claim";
        if (section === "evidence") return `${item.reference?.kind || "evidence"}:${item.reference?.id || "unknown identity"}`;
        if (section === "calculations") return item.calculation_id || "unknown calculation";
        if (section === "limitations") return `${item.scope || "report"}: ${item.text || ""}`;
        return "unnamed item";
      };
      const renderDiff = diff => {
        if (!diff || diff.schema_version !== "report-diff/1") throw new Error("unsupported diff payload");
        const labels = { claims: "Claims", evidence: "Evidence", calculations: "Calculations", limitations: "Limitations" };
        const nodes = [el("p", { className: "reader-meta", text:
          `Structured difference: ${diff.summary.added} added, ${diff.summary.removed} removed, ${diff.summary.unchanged} unchanged. Changes are expressed conservatively as additions and removals.` })];
        for (const [name, section] of Object.entries(diff.sections || {})) {
          const details = el("details", { className: "reader-correction-details" }, [
            el("summary", { text: `${labels[name] || name}: ${list(section.added).length} added, ${list(section.removed).length} removed, ${list(section.unchanged).length} unchanged` }),
          ]);
          for (const [status, label] of [["added", "Added"], ["removed", "Removed"], ["unchanged", "Unchanged"]]) {
            if (!list(section[status]).length) continue;
            details.append(el("strong", { text: label }), el("ul", {}, list(section[status]).map(item =>
              el("li", { text: describeItem(name, item) }))));
          }
          nodes.push(details);
        }
        nodes.push(el("p", { className: "reader-meta", text: `Algorithm ${diff.algorithm_version} · checksum ${diff.diff_sha256}` }));
        return el("div", { className: "reader-diff-result" }, nodes);
      };
      const compareControl = (oldReportId, newReportId, label = "Compare structured differences") => {
        const result = el("div", { attrs: { "aria-live": "polite" } });
        const button = el("button", { text: label, attrs: { type: "button" } });
        button.addEventListener("click", async () => {
          button.disabled = true;
          result.textContent = "Loading two frozen reports and calculating differences…";
          try {
            const query = new URLSearchParams({ old_report_id: oldReportId, new_report_id: newReportId });
            const response = await fetch(`/api/reports/diff?${query}`, { headers: { Accept: "application/json" }, signal });
            if (authenticationRequired(response, result)) return;
            if (!response.ok) throw new Error(`report diff unavailable: ${response.status}`);
            result.replaceChildren(renderDiff(await response.json()));
          } catch (error) {
            if (signal?.aborted) return;
            result.replaceChildren(el("p", { className: "reader-correction", text: "Comparison failed, so differences between the two versions cannot be determined. Try again later." }));
            button.disabled = false;
          }
        });
        return el("div", { className: "reader-diff-control" }, [button, result]);
      };
      const notices = [];
      for (const [key, incoming] of [["corrected_by", true], ["corrections", false]]) {
        for (const correction of list(current[key])) {
          notices.push(el("section", { className: "reader-correction" }, [
            el("h2", { text: incoming ? "A later correction exists" : "This version includes a correction" }),
            el("p", { className: "reader-meta", text: "The correction has not been independently approved; the original body and evidence remain unchanged." }),
            el("details", { className: "reader-correction-details" }, [
              el("summary", { text: "View original statement, correction, and impact" }),
              el("p", { text: `Original conclusion: ${clean(correction.original_text)}` }),
              el("p", { text: `Correction: ${clean(correction.explanation)}` }),
              el("p", { text: `Impact: ${clean(correction.impact)}` }),
            ]),
            reportLink(incoming ? correction.new_report_id : correction.old_report_id,
              incoming ? `View version ${correction.new_version} and its evidence` : `Review the original text of version ${correction.old_version}`),
            compareControl(correction.old_report_id, correction.new_report_id),
          ]));
        }
      }
      const historyItems = versions.map(item => {
        if (item.report_id === current.report_id) {
          return el("li", {}, el("span", { text: `Version ${item.version} · ${item.title} (currently reading)`, attrs: { "aria-current": "page" } }));
        }
        const oldVersion = item.version < current.version ? item : current;
        const newVersion = item.version < current.version ? current : item;
        return el("li", {}, [
          reportLink(item.report_id, `Version ${item.version} · ${item.title}`),
          compareControl(oldVersion.report_id, newVersion.report_id, `Compare with version ${current.version}`),
        ]);
      });
      const history = el("details", { className: "reader-version-history" }, [
        el("summary", { text: `Version ${current.version} · View version history (${versions.length})` }),
        el("ul", {}, historyItems),
      ]);
      container.replaceChildren(...notices, history);
    } catch (error) {
      if (signal?.aborted) return;
      // Missing relationship metadata must not be mistaken for no corrections.
      container.replaceChildren(el("p", { className: "reader-correction", text: "Unable to check versions and correction records. The current body remains the saved version; refresh and try again. This does not establish that no later correction exists." }));
    }
  }

  function citation(anchor) {
    const index = list(state.payload.anchors).findIndex(item => item.id === anchor.id) + 1;
    return el("button", { className: "reader-citation cite-mark", text: String(index || "Evidence"), attrs: {
      type: "button", "data-anchor-id": anchor.id, title: `Review evidence: ${anchor.text || "this passage"}`,
      "aria-label": `Review evidence ${index || ""}: ${anchor.text || "this passage"}`,
    } });
  }

  function renderBlock(block, context = {}) {
    const appendix = block.type === "review_finding";
    const firstFragment = context.firstFragment ?? !context.fragmentIndex;
    const lastFragment = context.lastFragment ?? context.fragmentIndex === context.fragmentCount - 1;
    const attrs = { "data-block-id": block.id };
    if (firstFragment) attrs.id = `block-${block.id}`;
    const article = el(appendix ? "details" : "section", {
      className: `section reader-block reader-block-${block.type}${context.isCore ? " reader-block-core" : ""}`, attrs,
    });
    if (appendix && firstFragment) article.append(el("summary", { text: `Pending-review appendix · ${block.title || "Unsupported content"} (not a conclusion)` }));
    else if (block.type === "question" && firstFragment) article.append(el("h2", { text: "Research question" }));
    else if (context.isCore && firstFragment) article.append(el("h2", { text: "Key conclusion" }));
    else if (firstFragment && block.title && block.type !== "dataset") article.append(el("h3", { text: block.title }));

    const anchors = list(block.anchor_ids).map(id => list(state.payload.anchors).find(item => item.id === id)).filter(Boolean);
    const binding = ReportReaderBindings.planBindings(text(block.text), anchors, block.inline_anchor_ids);
    if (block.text && block.type !== "limitations" && (block.type !== "sources" || firstFragment)) {
      const contents = [];
      if (context.isCore && firstFragment && block.title) contents.push(el("strong", { className: "reader-summary-lead", text: block.title }));
      for (const segment of binding.segments) {
        contents.push(document.createTextNode(segment.text));
        if (segment.anchorId) contents.push(citation(anchors.find(anchor => anchor.id === segment.anchorId)));
      }
      for (const anchor of binding.remaining) contents.push(citation(anchor));
      if (block.type === "question" && lastFragment) {
        const clarify = el("button", { className: "reader-question-link", text: "Original question and clarification", attrs: { type: "button" } });
        clarify.addEventListener("click", () => openQuestionInfo(clarify));
        contents.push(clarify);
      }
      article.append(el("p", { className: "reader-narrative" }, contents));
    }
    const statusLabel = block.status === "not_assessed" && list(block.required_verifier_findings).length
      ? "Independent model verification is recorded; human approval is still pending."
      : ({ unsupported: "Unsupported by evidence; not a conclusion", conflicting: "Evidence conflicts; not a definitive conclusion",
      partial_support: "This passage is not fully verified", inference: "Analytical judgment, not an established fact", not_assessed: "Not semantically reviewed" })[block.status] || block.status_label;
    if (lastFragment && list(block.required_verifier_findings).length) {
      article.append(el("p", { className: "reader-machine-verification", text: list(block.required_verifier_findings)
        .map(item => verifierOutcomeLabel(item.outcome) || item.outcome_label).filter(Boolean).join("; ") }));
    }
    if (lastFragment && block.status === "partial_support") {
      const boundary = el("details", { className: "reader-support-warning" }, [
        el("summary", { text: `${statusLabel} · View scope and evidence` }),
        el("p", { text: "Some statements have evidence, but the entire passage cannot therefore be treated as established fact. The following are the verification limits recorded in this report; they do not indicate completion of an independent review." }),
      ]);
      const limitations = list(block.limitations).filter(item => typeof item === "string" && item.trim());
      if (limitations.length) boundary.append(el("ul", {}, limitations.map(item => el("li", { text: item }))));
      else boundary.append(el("p", { text: "This report does not enumerate every unverified point. Review the saved claims, review reasons, and original sources." }));
      if (block.reasoning) boundary.append(el("p", { text: block.reasoning }));
      if (anchors.length) boundary.append(el("div", { className: "reader-review-actions" }, anchors.map((anchor, index) => {
        const action = el("button", { className: "reader-review-action", text: anchors.length === 1 ? "View the corresponding evidence and review notes" : `View evidence ${index + 1} and review notes`,
          attrs: { type: "button", "data-anchor-id": anchor.id, "data-review-anchor-id": anchor.id, title: anchor.text } });
        return action;
      })));
      else boundary.append(el("p", { text: "No point-specific evidence that can be opened was saved for this passage." }));
      article.append(boundary);
    } else if (lastFragment && ["unsupported", "conflicting"].includes(block.status)) {
      article.append(el("p", { className: "reader-support-warning", text: statusLabel }));
    }
    if (appendix && lastFragment) for (const flag of list(block.review_flags)) {
      if (flag.message) article.append(el("p", { text: `Review concern: ${flag.message}` }));
    }
    if (block.type === "dataset") article.append(renderDataset(block, context));
    if (block.type === "sources") article.append(renderSources(block));
    if (block.type === "limitations" && list(block.items).length) {
      article.append(el("ul", {}, list(block.items).map(item => el("li", { text: item }))));
    }
    if (lastFragment && block.status !== "partial_support" && (block.reasoning || statusLabel)) {
      article.append(el("details", { className: "reader-reasoning" }, [
        el("summary", { text: "Evidence and judgment boundaries" }),
        statusLabel ? el("p", { className: "reader-assessment-note", text: statusLabel }) : null,
        block.reasoning ? el("p", { text: block.reasoning }) : null,
      ]));
    }
    return article;
  }

  function anchorButtonLabel(anchor, index) {
    if (!anchor) return `View evidence ${index + 1}`;
    const keys = list(anchor.reference_keys).map(key => key.split(":")[0]);
    if (anchor.kind === "number" || keys.includes("calculation")) return "View calculation";
    if (anchor.kind === "table_cell" || keys.includes("evidence")) return "View table fact";
    if (keys.includes("passage")) return "View source text";
    if (keys.includes("query")) return "View SQL";
    return "View source";
  }

  function renderSources(block) {
    const sources = list(block.source_ids).map(id => list(state.payload.sources).find(source => source.id === id)).filter(Boolean);
    if (!sources.length) return el("p", { className: "reader-empty", text: "No frozen sources to display." });
    return el("div", { className: "reader-table-wrap" }, el("table", { className: "source-catalog-table" }, [
      el("thead", {}, el("tr", {}, ["Material", "Disclosure date", "Saved scope"].map(label => el("th", { text: label, attrs: { scope: "col" } })))),
      el("tbody", {}, sources.map(source => el("tr", {}, [
        el("th", { attrs: { scope: "row" } }, el("button", {
          text: source.title || "Title not recorded", attrs: { type: "button", "data-source-id": source.id },
        })),
        el("td", { text: source.published_at || "Not recorded" }),
        el("td", { text: source.media_type === "application/pdf" ? "PDF source and saved excerpts"
          : source.media_type === "text/html" ? "Web source and saved excerpts" : "Saved source; click to review" }),
      ]))),
    ]));
  }

  function rowDisplay(row) {
    return text(row.display_text) || `${text(row.value, "—")} ${text(row.unit)}`.trim();
  }

  function renderDataset(block, context = {}) {
    const rows = list(block.rows);
    if (!rows.length) return el("p", { className: "reader-empty", text: "This dataset has no rows to display." });
    const children = [];
    const description = block.description ? el("p", { className: "reader-dataset-description", text: block.description }) : null;
    if (description && block.description_anchor_id && !context.matrixContinuation) {
      const anchor = list(state.payload.anchors).find(item => item.id === block.description_anchor_id);
      if (anchor) description.append(citation(anchor));
    }
    const ordinal = [...state.payload.chapters.flatMap(chapter => list(chapter.blocks))]
      .filter(item => item.type === "dataset").findIndex(item => item.id === block.id) + 1;
    const caption = el("figcaption", {
      text: `${block.chart ? "Figure" : "Table"} ${ordinal} · ${block.title || "Research data"}${context.matrixContinuation ? " (continued)" : ""}`,
    });
    if (block.presentation?.type === "matrix") {
      const table = ReportReaderTables.createComparisonTable(block);
      table.className = "reader-table reader-comparison-table";
      for (const button of table.querySelectorAll("[data-anchor-id]")) {
        button.className = "reader-table-evidence";
        const point = rows.find(row => row.anchor_id === button.dataset.anchorId);
        if (point) {
          const context = [point.label, window.ReportReaderBindings.periodText(point.period), point.basis].filter(Boolean).join(" · ");
          button.title = context;
          button.setAttribute("aria-label", `${context}: ${button.textContent}; review evidence`);
        }
      }
      children.push(el("figure", { className: "reader-report-figure" }, [caption, description,
        el("div", { className: "reader-table-wrap", attrs: { tabindex: "0" } }, table),
        block.presentation.unit ? el("p", { className: "reader-table-note", text: `Unit: ${block.presentation.unit}` }) : null,
        block.presentation.note ? el("p", { className: "reader-table-note", text: block.presentation.note }) : null]));
      return el("div", { className: "reader-dataset" }, children);
    }
    const chartNode = block.chart ? renderChart(block.chart, rows) : null;
    const lineModel = block.chart?.type === "line" ? window.ReportReaderBindings.validateTimeSeries({ chart: block.chart, rows }) : null;
    const chartOwnsBindings = (block.chart?.type === "bar" && numericRows(rows).every(item => item.numeric !== null))
      || (lineModel?.safe === true);
    const table = el("table", { className: "reader-table" }, [
      el("thead", {}, el("tr", {}, [
        el("th", { text: "Item", attrs: { scope: "col" } }),
        el("th", { text: "Value", attrs: { scope: "col" } }),
        el("th", { text: "Period / basis", attrs: { scope: "col" } }),
      ])),
      el("tbody", {}, rows.map(row => el("tr", {}, [
        el("th", { text: row.label, attrs: { scope: "row" } }),
        el("td", {}, row.anchor_id && !chartOwnsBindings ? el("button", {
          className: "reader-table-evidence",
          text: rowDisplay(row),
          attrs: { type: "button", "data-anchor-id": row.anchor_id },
        }) : el("span", { text: rowDisplay(row), attrs: row.anchor_id ? { "data-value-anchor-id": row.anchor_id } : {} })),
        el("td", { text: [window.ReportReaderBindings.periodText(row.period), row.basis].filter(Boolean).join(" · ") || "Not recorded" }),
      ]))),
    ]);
    children.push(el("figure", { className: "reader-report-figure" }, [caption, description, chartNode,
      el("div", { className: "reader-table-wrap", attrs: { tabindex: "0" } }, table)]));
    return el("div", { className: "reader-dataset" }, children);
  }

  function renderChart(chart, rows) {
    if (numericRows(rows).some(item => item.numeric === null)) {
      return el("p", { className: "reader-empty", text: "Values exceed the safe charting range, so no chart was drawn. Complete exact values remain in the data table below." });
    }
    if (chart.type === "bar") return renderBarChart(rows);
    if (chart.type === "line") {
      const model = window.ReportReaderBindings.validateTimeSeries({ chart, rows });
      return model.safe ? renderLineChart(model) : el("p", { className: "reader-empty", text: `Timeline semantics cannot be represented safely (${model.reasons.join("; ")}); complete exact values, periods, and bases remain in the table below.` });
    }
    if (chart.type === "scatter") {
      return el("p", { className: "reader-empty", text: "The saved data has no independent, verifiable x-axis semantics, so a potentially misleading scatter plot was not drawn. The complete typed data remains in the table below." });
    }
    return el("p", { className: "reader-empty", text: "The current chart type lacks sufficient saved semantics. The complete data table and cell evidence are retained." });
  }

  function numericRows(rows) {
    return rows.map((row, index) => {
      return { row, index, numeric: window.ReportReaderBindings.safeChartNumber(row.value) };
    });
  }

  function renderBarChart(rows) {
    const parsed = numericRows(rows);
    const max = Math.max(...parsed.map(item => Math.abs(item.numeric)), 1);
    return el("div", { className: "reader-bar-chart", attrs: { "aria-label": "Clickable chart data points" } }, parsed.map(item => {
      const percent = Math.max(3, Math.round(Math.abs(item.numeric) / max * 100));
      const button = el("button", {
        className: "reader-bar-point",
        text: `${item.row.label} · ${rowDisplay(item.row)}`.trim(),
        attrs: { type: "button", "data-anchor-id": item.row.anchor_id },
      });
      button.style.setProperty("--bar-size", `${percent}%`);
      return button;
    }));
  }

  function renderLineChart(model) {
    // Geometry and point buttons share one plot box; text labels stay in CSS pixels.
    const width = 1000, height = 1000;
    const times = model.sorted.map(item => item.end), values = model.sorted.map(item => item.numeric);
    const minTime = Math.min(...times), timeSpan = Math.max(Math.max(...times) - minTime, 1);
    let minValue = Math.min(...values), maxValue = Math.max(...values);
    if (minValue === maxValue) { minValue -= 1; maxValue += 1; }
    const x = item => (item.end - minTime) / timeSpan * width;
    const y = item => (maxValue - item.numeric) / (maxValue - minValue) * height;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`); svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("preserveAspectRatio", "none");
    function svgNode(tag, attrs, content) {
      const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
      Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
      if (content !== undefined) node.textContent = String(content);
      svg.append(node); return node;
    }
    svgNode("line", { x1: 0, y1: height, x2: width, y2: height, class: "reader-line-axis" });
    svgNode("line", { x1: 0, y1: 0, x2: 0, y2: height, class: "reader-line-axis" });
    model.segments.filter(segment => segment.length > 1).forEach(segment => svgNode("polyline", {
      points: segment.map(item => `${x(item)},${y(item)}`).join(" "), class: "reader-line-path", fill: "none",
    }));
    const tick = value => String(value).length <= 8 ? String(value) : `≈${value.toExponential(2)}`;
    const plot = el("div", { className: "reader-line-plot" }, [svg,
      el("span", { className: "reader-line-label reader-line-date-start", text: model.sorted[0].row.period.end }),
      el("span", { className: "reader-line-label reader-line-date-end", text: model.sorted.at(-1).row.period.end }),
      el("span", { className: "reader-line-label reader-line-ymax", text: tick(maxValue) }),
      el("span", { className: "reader-line-label reader-line-ymin", text: tick(minValue) }),
    ]);
    const wrap = el("div", { className: "reader-line-chart", attrs: { role: "group", "aria-label": "Verifiable fact line plotted by actual UTC date distance; gaps break the line" } }, plot);
    model.sorted.forEach(item => {
      const button = el("button", {
        className: "reader-line-point", text: rowDisplay(item.row),
        attrs: { type: "button", "data-anchor-id": item.row.anchor_id,
          title: `${item.row.label}; ${window.ReportReaderBindings.periodText(item.row.period)}; ${rowDisplay(item.row)}; ${item.row.basis}`,
          "aria-label": `${item.row.label}; period ${window.ReportReaderBindings.periodText(item.row.period)}; exact value ${rowDisplay(item.row)}; basis ${item.row.basis}; review evidence` },
      });
      // Numeric CSSOM assignments work under the site's strict CSP; a style attribute does not.
      button.style.setProperty("--point-x", `${x(item) / width * 100}%`);
      button.style.setProperty("--point-y", `${y(item) / height * 100}%`);
      plot.append(button);
    });
    return el("div", { className: "reader-time-series" }, [wrap,
      el("p", { className: "reader-table-note", text: `X-axis: actual period end date (UTC); y-axis unit: ${model.sorted[0].row.display_unit || model.sorted[0].row.unit}. Missing periods are neither imputed nor connected; exact values can be reviewed point by point.` }),
    ]);
  }

  function scopeLine(question, sourceCount) {
    const scope = question.scope || {};
    const subjects = list(scope.subjects).map(subject => subject.label || subject.name || subject.id).filter(Boolean).join("、");
    const period = scope.period_text || window.ReportReaderBindings.periodText(scope.period);
    return [subjects, period, `${sourceCount} frozen sources`].filter(Boolean).join(" · ");
  }

  function renderPayload(payload) {
    state.payload = payload;
    title.textContent = text(payload.report && payload.report.title, "Research report");
    const currentLink = document.getElementById("reader-current-link");
    if (currentLink && payload.report?.id) currentLink.href = `/report-reader.html?${new URLSearchParams({ report_id: payload.report.id })}`;
    meta.textContent = [scopeLine(payload.question || {}, list(payload.sources).length),
      payload.report?.status_label || "Pending approval",
      payload.audit?.semantic_review?.status === "not_assessed" ? "Full-text semantic review is incomplete" : null,
    ].filter(Boolean).join(" · ");
    // Apply saved review status to immutable legacy payloads without changing the
    // payload itself. The adopted question is a display projection only.
    const chapters = ReportReaderBindings.projectReviewedChapters(list(payload.chapters));
    const adoptedQuestion = payload.question?.normalized || payload.question?.original || "Research question not recorded";
    for (const chapter of chapters) chapter.blocks = list(chapter.blocks).map(block => (
      block.type === "question" ? { ...block, text: adoptedQuestion } : block
    ));
    const core = list(chapters[0]?.blocks).find(block => block.type === "finding");
    const flow = ReportReaderBindings.buildContentFlow(chapters, list(payload.anchors), { maxText: 360, maxRows: 5 });
    state.chapters = chapters;
    state.coreId = core?.id;
    state.flow = flow;
    state.flowNodes = flow.map(item => item.kind === "chapter"
      ? el("span", { className: "reader-page-title", attrs: { id: item.chapterId, hidden: "" } })
      : renderBlock(item.block, {
        fragmentIndex: item.fragmentIndex, fragmentCount: item.fragmentCount,
        isCore: item.block.id === core?.id,
      }));
    state.layoutKey = null;
    restoreReading();
    const continuationButton = document.getElementById("reader-continuation-button");
    window.ReportContinuation?.attach({
      payload,
      trigger: continuationButton,
      panel,
      body: evidenceBody,
      openPanel: () => showPanel("Continue research", "Continue research from this report", continuationButton),
      closePanel: closeEvidence,
      isPanelOpen: () => !panel.hidden && panel.dataset.panelMode === "continuation",
      authenticationRequired: response => authenticationRequired(response, evidenceBody),
    });
  }

  function pageStep() {
    return state.readingMode === "two" && wideScreen.matches ? 2 : 1;
  }

  function layoutMeasuredPages(step) {
    if (!state.flowNodes) return;
    const width = (root.clientWidth - (step === 2 ? 14 : 0)) / step;
    const target = step === 2 ? Math.max(720, Math.min(innerHeight - 280, 920)) : 720;
    const key = `${width}:${target}:${step}`;
    if (state.layoutKey === key) return;
    const focused = document.activeElement;
    const focusAnchor = root.contains(focused) ? focused.closest?.("[data-anchor-id]")?.dataset.anchorId : null;
    const focusSource = root.contains(focused) ? focused.closest?.("[data-source-id]")?.dataset.sourceId : null;
    const focusBlockId = root.contains(focused) ? focused.closest?.("[data-block-id]")?.dataset.blockId : null;
    const focusClass = root.contains(focused) ? [...focused.classList].find(name => name === "reader-table-wrap") : null;
    const focusWasPage = root.contains(focused) && focused.classList.contains("reader-page");
    const focusIndex = state.flowNodes.findIndex(node => node === focused || node.contains(focused));
    const previousItem = focusIndex >= 0 ? state.flow[focusIndex] : state.pages[state.page]?.[0];
    const probe = el("section", { className: "reader-page", attrs: { "aria-hidden": "true" } });
    // Measure with the actual paper typography and column width. Matrix rows are
    // greedily added as real DOM; no character count or nominal row height is
    // used as a pagination substitute.
    Object.assign(probe.style, { position: "absolute", visibility: "hidden", pointerEvents: "none",
      width: `${width}px`, height: "auto", minHeight: "0", top: "0", left: "0" });
    root.append(probe);
    const groups = [];
    for (let i = 0; i < state.flow.length; i += 1) {
      const indices = [i];
      if (state.flow[i].kind === "chapter" && i + 1 < state.flow.length) indices.push(++i);
      // Begin with the actual question and core conclusion,
      // not a duplicate chapter heading or a question-only cover page.
      if (groups.length === 0 && state.coreId && state.flow[i + 1]?.block?.id === state.coreId) indices.push(++i);
      groups.push(indices);
    }
    const pages = [];
    const oversizedCollections = new Set();
    let current = [];
    const fits = () => probe.getBoundingClientRect().height <= target + 1;
    const flush = () => {
      if (current.length) pages.push(current);
      current = [];
      probe.replaceChildren();
    };
    const record = (item, node) => ({ item, node });
    const isSplitMatrix = item => item.kind === "block" && item.block?.type === "dataset"
      && item.block.presentation?.type === "matrix" && !item.block.chart
      && list(item.block.presentation.rows).length > 1;
    const collectionLength = item => {
      if (isSplitMatrix(item)) return item.block.presentation.rows.length;
      if (item.block?.type === "sources") return list(item.block.source_ids).length;
      if (item.block?.type === "limitations") return list(item.block.items).length;
      return 0;
    };
    const isSplitCollection = item => collectionLength(item) > 1;
    const collectionNode = (item, start, end) => {
      const total = collectionLength(item);
      const matrix = isSplitMatrix(item);
      const field = item.block.type === "sources" ? "source_ids" : "items";
      const block = matrix ? ReportReaderTables.sliceComparisonDataset(item.block, start, end)
        : { ...item.block, [field]: item.block[field].slice(start, end) };
      const node = renderBlock(block, {
        firstFragment: start === 0 && item.fragmentIndex === 0,
        lastFragment: end === total && item.fragmentIndex === item.fragmentCount - 1,
        matrixContinuation: start > 0, isCore: block.id === state.coreId,
      });
      node.dataset.flowRowStart = String(start);
      node.dataset.flowRowEnd = String(end);
      if (matrix) {
        node.dataset.matrixRowStart = String(start);
        node.dataset.matrixRowEnd = String(end);
      }
      return node;
    };
    const appendCollection = item => {
      const total = collectionLength(item);
      probe.append(state.flowNodes[state.flow.indexOf(item)]);
      if (fits()) {
        current.push(record(item, state.flowNodes[state.flow.indexOf(item)]));
        return;
      }
      state.flowNodes[state.flow.indexOf(item)].remove();
      let start = 0;
      while (start < total) {
        let bestEnd = start;
        let trial = null;
        for (let end = start + 1; end <= total; end += 1) {
          trial?.remove();
          trial = collectionNode(item, start, end);
          probe.append(trial);
          if (!fits()) {
            trial.remove();
            trial = null;
            break;
          }
          bestEnd = end;
        }
        if (bestEnd === start) {
          if (current.some(value => value.item.kind !== "chapter") && !oversizedCollections.has(item)) {
            flush();
            continue;
          }
          // A complete row/item may exceed the usable paper. Keep it intact,
          // with its chapter marker, and let this single paper grow naturally.
          trial = collectionNode(item, start, start + 1);
          probe.append(trial);
          bestEnd = start + 1;
        } else if (!trial || Number(trial.dataset.flowRowEnd) !== bestEnd) {
          trial?.remove();
          trial = collectionNode(item, start, bestEnd);
          probe.append(trial);
        }
        current.push(record(item, trial));
        start = bestEnd;
        if (start < total) flush();
      }
    };
    for (const group of groups) {
      const collectionIndex = group.find(index => isSplitCollection(state.flow[index]));
      if (collectionIndex === undefined) {
        const records = group.map(index => record(state.flow[index], state.flowNodes[index]));
        records.forEach(value => probe.append(value.node));
        if (current.length && !fits()) {
          records.forEach(value => value.node.remove());
          flush();
          records.forEach(value => probe.append(value.node));
        }
        current.push(...records);
        continue;
      }
      // A chapter is navigation metadata, not a new sheet. Keep its marker
      // with only the first complete row/item, then fill the remaining paper.
      if (current.length) {
        const previews = [];
        for (const index of group) {
          const item = state.flow[index];
          const node = isSplitCollection(item) ? collectionNode(item, 0, 1) : state.flowNodes[index];
          previews.push(node); probe.append(node);
          if (isSplitCollection(item)) break;
        }
        const previewFits = fits();
        previews.forEach(node => node.remove());
        if (!previewFits) {
          // Moving an intrinsically oversized complete item cannot solve overflow.
          // Keep the continuous flow and let this one paper grow naturally.
          const existing = [...probe.children];
          probe.replaceChildren(...previews);
          const oversized = !fits();
          probe.replaceChildren(...existing);
          previews.forEach(node => node.remove());
          if (oversized) oversizedCollections.add(state.flow[collectionIndex]);
          else flush();
        }
      }
      for (const index of group) {
        const item = state.flow[index];
        if (isSplitCollection(item)) appendCollection(item);
        else { probe.append(state.flowNodes[index]); current.push(record(item, state.flowNodes[index])); }
      }
    }
    flush();
    probe.remove();
    state.pages = pages.map(records => records.map(value => value.item));
    root.replaceChildren(...pages.map((records, pageIndex) => {
      const chapterIds = [...new Set(records.map(value => value.item.chapterId))];
      return el("section", { className: "reader-page", attrs: {
        "data-page-chapter": chapterIds[0] || "", "data-page-chapters": chapterIds.join(" "),
        "data-layout-target": target, tabindex: "-1",
      } }, [...records.map(value => value.node), el("div", {
        className: "page-number reader-page-number", text: `Page ${pageIndex + 1}`,
        attrs: { "aria-hidden": "true" },
      })]);
    }));
    if (focusAnchor || focusSource) {
      const restored = focusAnchor
        ? [...root.querySelectorAll("[data-anchor-id]")].find(node => node.dataset.anchorId === focusAnchor)
        : [...root.querySelectorAll("[data-source-id]")].find(node => node.dataset.sourceId === focusSource);
      if (restored) {
        state.page = [...root.querySelectorAll(".reader-page")].indexOf(restored.closest(".reader-page"));
        state.restorePageFocus = restored;
      }
    } else {
      if (previousItem) state.page = Math.max(0, state.pages.findIndex(items => items.includes(previousItem)));
      if (root.contains(focused)) state.restorePageFocus = focused;
      else if (focusBlockId && focusClass) {
        const replacement = [...root.querySelectorAll("[data-block-id]")]
          .find(node => node.dataset.blockId === focusBlockId)?.querySelector(`.${focusClass}`);
        if (replacement) state.restorePageFocus = replacement;
      }
    }
    if (focusWasPage) state.restorePageFocus = "page";
    state.layoutKey = key;
    renderToc(state.chapters, state.pages, state.coreId);
  }

  function updateReading() {
    const step = pageStep();
    root.classList.toggle("reader-two-page", step === 2);
    root.classList.toggle("two-page-body", step === 2);
    document.body.classList.toggle("reading-two-page", step === 2);
    layoutMeasuredPages(step);
    const pages = [...root.querySelectorAll(".reader-page")];
    if (!pages.length) return;
    state.page = Math.max(0, Math.min(state.page, pages.length - 1));
    pages.forEach((page, index) => { page.hidden = index < state.page || index >= state.page + step; });
    root.classList.toggle("reader-two-page", step === 2);
    root.classList.toggle("two-page-body", step === 2);
    document.body.classList.toggle("reading-two-page", step === 2);
    document.getElementById("reader-controls").hidden = false;
    document.getElementById("reader-page-indicator").textContent =
      `${state.page + 1}${step === 2 && state.page + 1 < pages.length ? `–${Math.min(state.page + step, pages.length)}` : ""} / ${pages.length}`;
    document.getElementById("reader-prev").disabled = state.page === 0;
    document.getElementById("reader-next").disabled = state.page + step >= pages.length;
    const input = document.getElementById("reader-goto");
    input.max = String(pages.length); input.value = String(state.page + 1);
    for (const button of document.querySelectorAll("[data-reading-mode]")) {
      const active = button.dataset.readingMode === state.readingMode;
      button.setAttribute("aria-pressed", String(active));
      button.classList.toggle("active", active);
      button.disabled = button.dataset.readingMode === "two" && !wideScreen.matches;
    }
    for (const link of toc.querySelectorAll("[data-page]")) {
      if (Number(link.dataset.page) === state.page) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
    const compactToc = evidenceBody.querySelector(".reader-compact-toc");
    if (compactToc) {
      // Resizing can repaginate the report while its outline is still open.
      const focusedHref = compactToc.contains(document.activeElement)
        ? document.activeElement.getAttribute("href") : null;
      compactToc.replaceChildren(...[...toc.children].map(child => child.cloneNode(true)));
      if (focusedHref) [...compactToc.querySelectorAll("a")]
        .find(link => link.getAttribute("href") === focusedHref)?.focus({ preventScroll: true });
    }
    if (state.restorePageFocus) {
      const target = state.restorePageFocus === "page" ? pages[state.page] : state.restorePageFocus;
      state.restorePageFocus = null;
      target?.focus({ preventScroll: true });
    }
  }

  function restoreReading() {
    const url = new URL(location.href);
    const requested = Number(url.searchParams.get("page"));
    const mode = url.searchParams.get("reading");
    // Legacy `all` links remain openable but resolve to the supported single mode.
    state.readingMode = mode === "two" ? "two" : "single";
    state.page = Number.isInteger(requested) && requested > 0 ? requested - 1 : 0;
    updateReading();
    if (!url.searchParams.has("page") && url.hash) {
      const target = document.getElementById(url.hash.slice(1));
      const page = target?.closest(".reader-page");
      const index = [...root.querySelectorAll(".reader-page")].indexOf(page);
      if (index >= 0) state.page = index;
    }
    updateReading();
  }

  function goToPage(page, { save = true, focus = false, hash = "" } = {}) {
    if (!Number.isInteger(page)) return;
    if (!panel.hidden) closeEvidence();
    state.page = page;
    updateReading();
    if (save) {
      const url = new URL(location.href);
      url.searchParams.set("page", String(state.page + 1));
      url.searchParams.set("reading", state.readingMode);
      url.hash = hash;
      history.pushState(null, "", url);
    }
    const target = root.querySelectorAll(".reader-page")[state.page];
    target?.scrollIntoView({ block: "start", behavior: "instant" });
    if (focus) target?.focus({ preventScroll: true });
  }

  function syncPanelMode() {
    const modal = !panel.hidden;
    const information = panel.dataset.panelMode === "information" || panel.dataset.panelMode === "continuation";
    panel.classList.toggle("question-dialog", information);
    panel.classList.toggle("evidence-dialog", !information);
    panel.classList.toggle("continuation-dialog", panel.dataset.panelMode === "continuation");
    evidenceBody.classList.toggle("question-modal-body", information);
    shell.inert = modal;
    backdrop.hidden = !modal;
    panel.setAttribute("aria-modal", String(modal));
    document.body.classList.toggle("reader-modal-open", modal);
    document.body.classList.toggle("reader-evidence-open", modal && !information);
    updateReading();
  }

  function showPanel(kicker, heading, trigger) {
    if (state.request) state.request.abort();
    state.request = new AbortController();
    state.panelRevision += 1;
    if (state.lastFocus) state.lastFocus.removeAttribute("aria-expanded");
    state.lastFocus = trigger || document.activeElement;
    state.lastFocus?.setAttribute("aria-expanded", "true");
    panel.dataset.panelMode = kicker === "Continue research" ? "continuation"
      : ["Research question", "Report information", "Report contents"].includes(kicker) ? "information" : "evidence";
    const label = panel.querySelector(".reader-kicker");
    if (label) label.textContent = kicker;
    evidenceTitle.textContent = heading;
    evidenceBody.replaceChildren();
    panel.hidden = false;
    syncPanelMode();
    panel.focus({ preventScroll: true });
    return state.panelRevision;
  }

  function navigateFromToc(event) {
    const link = event.target.closest("a[data-page]");
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button) return;
    const index = Number(link.dataset.page);
    if (!Number.isInteger(index)) return;
    event.preventDefault();
    goToPage(index, { focus: true, hash: link.hash });
  }

  function openCompactToc() {
    if (!state.payload) return;
    showPanel("Report contents", "Contents", tocButton);
    // Reuse the measured desktop outline, including the current page and exact
    // links. No second chapter model or separate mobile pagination.
    const navigation = el("nav", { className: "report-toc reader-compact-toc", attrs: { "aria-label": "Report contents" } },
      [...toc.children].map(child => child.cloneNode(true)));
    navigation.addEventListener("click", navigateFromToc);
    evidenceBody.replaceChildren(navigation);
    const current = navigation.querySelector('[aria-current="page"]') || navigation.querySelector("a");
    current?.focus({ preventScroll: true });
    current?.scrollIntoView({ block: "nearest", behavior: "instant" });
  }

  function recordedText(value) {
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.map(recordedText).filter(Boolean).join("; ");
    if (!value || typeof value !== "object") return "";
    return [value.label, value.text, value.question, value.explanation, recordedText(value.selected), value.user_notes]
      .filter(Boolean).join("; ");
  }

  function scopeText(scope) {
    if (!scope || typeof scope !== "object") return "Research scope was not saved separately";
    const labels = { subjects: "Subjects", period_text: "Period", metrics: "Metrics", basis: "Basis", directions: "Directions", unit: "Unit" };
    const parts = Object.entries(labels).map(([key, label]) => {
      const value = recordedText(scope[key]);
      return value ? `${label}: ${value}` : "";
    }).filter(Boolean);
    return parts.join("; ") || "Research scope was not saved separately";
  }

  function openQuestionInfo(trigger) {
    const question = state.payload?.question || {};
    showPanel("Research question", "From the original question to this research", trigger);
    const adopted = question.normalized || question.original || "Not recorded";
    const hasRecordedNormalization = Boolean(question.normalized)
      && (question.snapshot_schema_version || question.normalized !== question.original);
    const normalizationNote = hasRecordedNormalization
      ? adopted : `${adopted} (This version did not save a separate normalized question; the report body uses the original question.)`;
    const section = (heading, content, className) => el("section", { className }, [
      el("h3", { text: heading }), el("p", { text: content }),
    ]);
    const scope = question.scope || {};
    const rows = Object.entries({ subjects: "Research subjects", period_text: "Research period", metrics: "Metrics",
      basis: "Comparison basis", directions: "Research directions", unit: "Amount unit" })
      .map(([key, label]) => [label, recordedText(scope[key])]).filter(([, value]) => value);
    const scopeTable = rows.length ? el("table", { className: "normalization-table" }, [
      el("thead", {}, el("tr", {}, ["Scope", "Used for this research"].map(label => el("th", { text: label, attrs: { scope: "col" } })))),
      el("tbody", {}, rows.map(([label, value]) => el("tr", {}, [el("th", { text: label, attrs: { scope: "row" } }), el("td", { text: value })]))),
    ]) : section("Research scope", "This version did not save a separate scope summary.", "adopted-question");
    const clarification = recordedText(question.clarifications);
    evidenceBody.replaceChildren(
      section("Original question", question.original || "Not recorded", "original-question-block"),
      section("Research question", normalizationNote, "adopted-question"),
      scopeTable,
      section("Clarification record", clarification || (question.snapshot_schema_version
        ? "No additional user confirmation was recorded. The table above summarizes research scope; it is not a clarification response from the user."
        : "This version did not save a separate clarification record."), "open-uncertainties"),
      section("Assumptions used", recordedText(question.assumptions) || "No additional assumptions were recorded.", "open-uncertainties"),
      section("Open uncertainties", recordedText([...list(question.open_uncertainties), ...list(question.scope_gaps)]) || "No additional uncertainties were recorded; this does not mean the conclusions were independently approved.", "open-uncertainties"),
      section("Material timing choice", recordedText(question.time_choice) || "This version did not record an additional reuse or refresh choice.", "open-uncertainties")
    );
  }

  function openReportInfo(trigger) {
    const payload = state.payload || {};
    const report = payload.report || {};
    const question = payload.question || {};
    const audit = payload.audit || {};
    showPanel("Report information", "Scope, materials, and status", trigger);
    const process = report.conversation_id ? el("section", { className: "evidence-card" }, [
      el("h3", { text: "Execution record" }),
      el("p", { text: "To review historical run records, return to this research’s secondary record page. To continue research, use “Continue research” on the report page." }),
      el("a", { className: "reader-source-download", text: "View execution record", attrs: {
        href: `/${ResearchNavigation.format({ conversation: report.conversation_id, tab: "answer" })}`,
      } }),
    ]) : null;
    evidenceBody.replaceChildren(...[
      evidenceCard("Research question", question.normalized || question.original || "Not recorded"),
      evidenceCard("Report status", `${text(report.status_label, "Pending review")}. The current version cannot be edited directly; later factual changes should create a new version.`),
      evidenceCard("Material coverage", `${list(payload.sources).length} frozen sources, ${audit.trace_count || 0} evidence paths, ${audit.calculation_count || 0} calculation records.`),
      evidenceCard("Reading boundaries", "By default, the report body shows only conclusions and necessary explanations. Sources, formulas, table cells, and review steps expand progressively when evidence is selected."),
      process,
    ].filter(Boolean));
  }

  function currentTheme() {
    return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
  }

  function updateThemeButton() {
    if (!themeButton) return;
    const dark = currentTheme() === "dark";
    themeButton.setAttribute("aria-pressed", dark ? "true" : "false");
    themeButton.title = dark ? "Switch to light theme" : "Switch to dark theme";
  }

  function toggleTheme() {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("report-reader-theme", next);
    } catch (error) {
      // Theme persistence is optional; the visual switch itself has already happened.
    }
    updateThemeButton();
  }

  function readWithRetry(kind, trigger, read) {
    return read().catch(error => {
      if (error.name === "AbortError" || panel.hidden || state.lastFocus !== trigger) return;
      evidenceTitle.textContent = `${kind} is temporarily unavailable`;
      const retry = el("button", { className: "reader-review-action", text: "Retry",
        attrs: { type: "button", "data-retry-reader": "true" } });
      retry.addEventListener("click", () => readWithRetry(kind, trigger, read));
      evidenceBody.replaceChildren(el("p", { className: "reader-error",
        text: `Unable to load ${kind}. Check your connection and retry, or close the window and continue reading. Retrying only loads saved content and does not rerun research.` }), retry);
    });
  }

  async function openEvidence(anchorId, trigger) {
    const report = state.payload && state.payload.report;
    if (!report) return;
    const revision = showPanel("Evidence review", "Loading evidence", trigger);
    const query = new URLSearchParams({
      report_id: report.id,
      anchor_id: anchorId,
    });
    const response = await fetch(`/api/reports/evidence?${query}`, { headers: { Accept: "application/json" }, signal: state.request.signal });
    if (revision !== state.panelRevision || panel.hidden) return;
    if (authenticationRequired(response, evidenceBody)) return;
    if (!response.ok) {
      if (response.status >= 500 || response.status === 429) throw new Error("reader_temporarily_unavailable");
      evidenceTitle.textContent = "Evidence unavailable";
      evidenceBody.replaceChildren(el("p", { className: "reader-error", text: "This evidence was not found. The report body will not fabricate evidence in response." }));
      return;
    }

    const payload = await response.json();
    if (revision !== state.panelRevision || panel.hidden) return;
    evidenceTitle.textContent = "Point-by-point review";
    const verifier = renderRequiredVerifier(payload.evidence?.verification?.required_verifier_findings,
      payload.evidence?.verification?.precise_binding_status);
    evidenceBody.replaceChildren(...[verifier,
      ...renderValueSemantics(payload.evidence),
      ReportEvidenceBrowser.render({ payload, reportPayload: state.payload })].filter(Boolean));
  }

  async function openSource(sourceId, trigger) {
    const report = state.payload && state.payload.report;
    if (!report) return;
    const revision = showPanel("Source viewer", "Loading source", trigger);
    const query = new URLSearchParams({ report_id: report.id, source_id: sourceId });
    const response = await fetch(`/api/reports/source?${query}`, { headers: { Accept: "application/json" }, signal: state.request.signal });
    if (revision !== state.panelRevision || panel.hidden) return;
    if (authenticationRequired(response, evidenceBody)) return;
    if (!response.ok) {
      if (response.status >= 500 || response.status === 429) throw new Error("reader_temporarily_unavailable");
      evidenceTitle.textContent = "Source unavailable";
      evidenceBody.replaceChildren(el("p", { className: "reader-error", text: "This frozen source was not found." }));
      return;
    }
    const payload = await response.json();
    if (revision !== state.panelRevision || panel.hidden) return;
    const source = payload.source || {};
    evidenceTitle.textContent = source.title || "Frozen source";
    const download = payload.raw_download_href ? el("a", {
      className: "reader-source-download",
      text: "Download frozen source",
      attrs: { href: payload.raw_download_href },
    }) : null;
    const safeSourceUrl = window.ReportReaderSecurity.safeExternalUrl(source.url);
    const sourceLink = safeSourceUrl ? el("a", {
      className: "reader-source-download reader-source-link",
      text: "Open source page",
      attrs: { href: safeSourceUrl, rel: "noreferrer", target: "_blank" },
    }) : null;
    evidenceBody.replaceChildren(
      evidenceCard("Source summary", [source.title, source.published_at, source.media_type, source.rights_status].filter(Boolean).join(" · ") || "Not recorded"),
      evidenceCard("Frozen status", [source.parse_status, source.byte_length ? `${source.byte_length} bytes` : null, payload.raw_download_available ? "Source available for download" : "Source not directly downloadable"].filter(Boolean).join(" · ")),
      ...[sourceLink, download].filter(Boolean),
      renderRelatedEvidence(payload.related_evidence)
    );
  }

  function renderRelatedEvidence(items) {
    const related = list(items);
    if (!related.length) return evidenceCard("Related evidence", "No excerpts are directly associated with the current anchor.");
    return el("section", { className: "evidence-card evidence-related" }, [
      el("h3", { text: "Related evidence excerpts" }),
      ...related.slice(0, 8).map(item => el("article", { className: "related-evidence-item" }, [
        el("p", { text: item.claim || "Claim not recorded" }),
        item.quote ? el("blockquote", { text: item.quote }) : null,
      ])),
    ]);
  }

  function evidenceCard(label, value) {
    return el("section", { className: "evidence-card" }, [
      el("h3", { text: label }),
      el("p", { text: text(value, "Not recorded") }),
    ]);
  }

  function renderValueSemantics(evidence) {
    const formationLabels = {
      observed: "Observed: a value that occurred or was measured in the source record.",
      estimated: "Estimated: an estimate of an unknown or future value; not an observed fact.",
      assumed: "Assumed: an input set for analysis or a scenario.",
      derived: "Derived: calculated from explicit inputs using a reproducible method.",
    };
    if (!formationLabels[evidence?.value_formation]) return [];
    const attribution = evidence.attribution;
    const attributionText = attribution
      ? `${text(attribution.principal?.label)} (${text(attribution.principal?.type)} · ${text(attribution.principal?.identifier)}) stated this on ${text(attribution.stated_at)}; exact reference ${text(attribution.statement_reference?.kind)}:${text(attribution.statement_reference?.id)}. This identifies the speaker and does not mean the system accepts the statement as fact.`
      : "No explicit principal statement; attribution is null. The system did not infer a principal from the title, URL, or company identifier.";
    return [
      evidenceCard("Value formation", formationLabels[evidence.value_formation]),
      evidenceCard("Statement attribution", attributionText),
    ];
  }

  function traceStatusLabel(status) {
    return {
      available: "Verifiable",
      partial: "Partially verifiable",
      unavailable: "Path unavailable",
    }[status] || "Review status unrecognized";
  }

  function evidenceTable(headers, rows) {
    return el("div", { className: "reader-table-wrap", attrs: { tabindex: "0" } }, el("table", { className: "reader-table" }, [
      el("thead", {}, el("tr", {}, headers.map(label => el("th", { text: label, attrs: { scope: "col" } })))),
      el("tbody", {}, rows.map(row => el("tr", {}, row.map(value => el("td", { text: value }))))),
    ]));
  }

  function renderCalculation(calculation, source) {
    if (!calculation) return [];
    const arithmetic = calculation.arithmetic || {};
    const output = [evidenceCard("Calculation formula and result", `${text(calculation.formula)} = ${text(calculation.value)} ${text(calculation.unit)}`)];
    if (list(arithmetic.operands).length) output.push(evidenceTable(["Operand", "Meaning", "Saved value"],
      arithmetic.operands.map((operand, index) => [`operand[${index}]`, operand.metric || "Upstream calculation output", `${text(operand.value)} ${text(operand.unit)}`])));
    if (list(source.inputs).length) {
      output.push(evidenceCard("Underlying inputs", "The frozen facts used by this calculation are below. Direct operands for composite operations appear above; underlying facts are not presented as upstream calculation results."));
      output.push(evidenceTable(["Subject / metric", "Value", "Input period / basis"], source.inputs.map(fact => [
        [fact.company_id, fact.row_label || fact.metric].filter(Boolean).join(" · "),
        `${text(fact.value)} ${text(fact.unit)}`,
        `${text(fact.period_start)} to ${text(fact.period_end)} · ${text(fact.basis)}`,
      ])));
      if (source.inputs.some(fact => Object.values(fact.metadata_provenance || {}).some(value => value.status === "explicit_interpretation"))) {
        output.push(evidenceCard("Basis boundaries", "Some units, periods, or bases are interpretations explicitly recorded during research and cannot be treated as directly established by the source table. Review the source text and headers below."));
      }
    }
    if (arithmetic.rounding?.applied) output.push(evidenceCard("Display precision", `The displayed value is rounded to ${arithmetic.rounding.decimal_places} decimal places; subsequent calculations use the saved exact value.`));
    if (list(arithmetic.steps).length) output.push(el("details", {}, [
      el("summary", { text: "Expand upstream calculations" }),
      ...arithmetic.steps.map(step => evidenceCard("Recorded upstream step", `${step.formula} = ${step.value} ${step.unit}`)),
    ]));
    output.push(...list(calculation.limitations).map(item => el("p", { className: "evidence-limit", text: item })));
    return output;
  }

  function renderOriginal(source) {
    const nodes = [];
    if (source.title) nodes.push(el("h4", { text: source.title }));
    if (source.quote) nodes.push(el("blockquote", { text: source.quote }));
    const pdf = source.locator?.pdf_provenance;
    if (pdf?.schema_version === "pdf-table-cell-locator/1" && Number.isInteger(pdf.physical_page) && pdf.physical_page > 0) {
      nodes.push(evidenceCard("PDF source location", `File page ${pdf.physical_page} (file page order; may differ from printed page numbers)${pdf.target?.cell?.raw_text ? ` · Source cell: ${pdf.target.cell.raw_text}` : ""}`));
      for (const [label, context] of [["Source table title", pdf.title], ["Source table unit", pdf.unit_line], ["Source table fiscal period", pdf.fiscal_period_header]]) {
        if (context?.raw_text) nodes.push(evidenceCard(label, context.raw_text));
      }
      const box = pdf.target?.cell?.bbox;
      if (box && [box.x_min, box.y_min, box.x_max, box.y_max].every(Number.isFinite)) {
        nodes.push(el("details", {}, [
          el("summary", { text: "Expand source cell location and review boundaries" }),
          evidenceCard("Source page coordinates", `Left ${box.x_min} · Top ${box.y_min} · Right ${box.x_max} · Bottom ${box.y_max}`),
          evidenceCard("Review boundary", "Page and coordinates show where the number came from. Successful parsing does not independently approve its basis, economic meaning, or source usage rights."),
        ]));
      }
    }
    const excerpt = source.table_excerpt;
    if (excerpt && list(excerpt.rows).length) {
      nodes.push(el("details", { className: "reader-original-table" }, [
        el("summary", { text: "Review source table headers and selected row (relevant slice, not the complete table)" }),
        el("div", { className: "reader-table-wrap", attrs: { tabindex: "0" } }, el("table", { className: "reader-table" },
          el("tbody", {}, excerpt.rows.map(row => el("tr", {}, list(row.cells).map((cell, column) => el("td", {
            text: cell, className: row.row_index === excerpt.selected_row && column === excerpt.selected_column ? "reader-selected-cell" : "",
          }))))))),
      ]));
    }
    const sourceId = source.document_version_id || source.source_id;
    if (sourceId && list(state.payload.sources).some(item => item.id === sourceId && item.document_version_id)) {
      nodes.push(el("a", { className: "reader-source-download", text: "Download this frozen source", attrs: {
        href: `/api/reports/raw-source?${new URLSearchParams({ report_id: state.payload.report.id, source_id: sourceId })}`,
      } }));
    }
    return nodes;
  }

  function renderTraceCards(traces) {
    if (!traces.length) return [evidenceCard("Evidence path", "No expandable path was saved")];
    return traces.map((trace, index) => {
      const source = trace.source || {};
      const details = [];
      details.push(...renderCalculation(trace.calculation, source), ...renderOriginal(source));
      for (const inputSource of list(source.sources)) details.push(...renderOriginal(inputSource));
      if (source.context && source.context !== source.quote) details.push(el("p", { text: source.context }));
      if (source.query) details.push(el("pre", { text: JSON.stringify(source.query, null, 2) }));
      if (Array.isArray(source.rows) && source.rows.length) details.push(el("pre", { text: JSON.stringify({ columns: source.columns || [], rows: source.rows }, null, 2) }));
      return el("section", { className: "evidence-card evidence-trace" }, [
        el("h3", { text: `Evidence path ${index + 1} · ${traceStatusLabel(trace.status)}` }),
        el("ol", {}, list(trace.steps).map(step => el("li", {}, [
          el("strong", { text: `${text(step.layer)} · ${text(step.title)}` }),
          el("span", { text: step.details ? `: ${step.details}` : "" }),
        ]))),
        ...details,
        ...list(trace.limitations).map(item => el("p", { className: "evidence-limit", text: `Limitation: ${item}` })),
      ]);
    });
  }

  function closeEvidence() {
    if (state.request) state.request.abort();
    state.panelRevision += 1;
    const viewerClose = panel.querySelector('.source-viewer:not([hidden]) [data-close-source-viewer]');
    if (viewerClose) viewerClose.click();
    panel.hidden = true;
    syncPanelMode();
    const previous = state.lastFocus;
    previous?.removeAttribute("aria-expanded");
    evidenceBody.replaceChildren();
    let trigger = previous?.isConnected ? previous : null;
    if (!trigger && previous?.dataset.anchorId) {
      trigger = [...root.querySelectorAll("[data-anchor-id]")]
        .find(node => node.dataset.anchorId === previous.dataset.anchorId);
    }
    if (!trigger && previous?.dataset.sourceId) {
      trigger = [...root.querySelectorAll("[data-source-id]")]
        .find(node => node.dataset.sourceId === previous.dataset.sourceId);
    }
    if (!trigger && previous?.classList.contains("reader-question-link")) {
      const blockId = previous.closest("[data-block-id]")?.dataset.blockId;
      trigger = [...root.querySelectorAll("[data-block-id]")]
        .find(node => node.dataset.blockId === blockId)?.querySelector(".reader-question-link");
    }
    const page = trigger?.closest(".reader-page");
    if (page) {
      state.page = [...root.querySelectorAll(".reader-page")].indexOf(page);
      updateReading();
    }
    trigger?.removeAttribute("aria-expanded");
    (trigger || root.querySelector(".reader-page:not([hidden])"))?.focus({ preventScroll: true });
    state.lastFocus = null;
  }

  function downloadFilename(response) {
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename="([^"\\\r\n]+)"/iu);
    return match ? match[1] : "frozen-source.bin";
  }

  function showDownloadError(link, message) {
    const previous = evidenceBody.querySelector(".reader-download-error");
    if (previous) previous.remove();
    const error = el("p", { className: "reader-error reader-download-error", text: message, attrs: { role: "status" } });
    link.insertAdjacentElement("afterend", error);
  }

  async function downloadFrozenSource(link) {
    link.setAttribute("aria-disabled", "true");
    try {
      const response = await fetch(link.href, { credentials: "same-origin", headers: { Accept: "application/octet-stream" } });
      if (authenticationRequired(response, evidenceBody)) return;
      if (!response.ok) {
        const detail = response.status === 404 ? "The frozen source is unavailable or does not belong to the current report." :
          response.status === 503 ? "The frozen-source service is temporarily unavailable. Try again later." : `Frozen-source download failed (${response.status}).`;
        showDownloadError(link, detail);
        return;
      }
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      let anchor;
      try {
        anchor = el("a", { attrs: { href: objectUrl, download: downloadFilename(response) } });
        document.body.append(anchor);
        anchor.click();
      } finally {
        anchor?.remove();
        setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
      }
    } catch (error) {
      showDownloadError(link, "The frozen-source download did not complete. Check your connection and try again.");
    } finally {
      link.removeAttribute("aria-disabled");
    }
  }

  function listen(target, type, handler, options) {
    target.addEventListener(type, handler, options);
    lifecycle.cleanups.push(() => target.removeEventListener(type, handler, options));
  }

  function eligibleFocusTargets(focusRoot) {
    return [...focusRoot.querySelectorAll('button, a[href], input, textarea, select, summary, [tabindex], [contenteditable="true"]')]
      .filter(node => node.tabIndex >= 0
        && !node.matches(":disabled")
        && !node.closest('[hidden], [inert], [aria-hidden="true"]')
        && node.getClientRects().length
        && getComputedStyle(node).visibility !== "hidden");
  }

  function bindEvents() {
  listen(root, "click", event => {
    const button = event.target.closest("[data-anchor-id]");
    const sourceButton = event.target.closest("[data-source-id]");
    if (button) readWithRetry("Evidence", button, () => openEvidence(button.getAttribute("data-anchor-id"), button));
    if (sourceButton) readWithRetry("source", sourceButton, () => openSource(sourceButton.getAttribute("data-source-id"), sourceButton));
  });
  listen(panel, "click", event => {
    const link = event.target.closest("a[href]");
    if (!link) return;
    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin || url.pathname !== "/api/reports/raw-source") return;
    event.preventDefault();
    if (link.getAttribute("aria-disabled") === "true") return;
    downloadFrozenSource(link);
  });
  listen(infoButton, "click", () => openReportInfo(infoButton));
  listen(tocButton, "click", openCompactToc);
  // The React shell owns theming in the new reader; retain this only for the legacy page.
  if (themeButton) listen(themeButton, "click", toggleTheme);
  listen(document.getElementById("evidence-close"), "click", closeEvidence);
  listen(backdrop, "click", closeEvidence);
  listen(document, "keydown", event => {
    if (panel.hidden) return;
    if (event.key === "Escape") {
      const viewerClose = panel.querySelector('.source-viewer:not([hidden]) [data-close-source-viewer]');
      event.preventDefault();
      if (viewerClose) viewerClose.click();
      else closeEvidence();
      return;
    }
    if (event.key === "Tab") {
      const visibleViewer = panel.querySelector('.source-viewer:not([hidden])');
      const focusRoot = visibleViewer || panel;
      const focusable = eligibleFocusTargets(focusRoot);
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const focused = document.activeElement;
      const moveFocus = target => {
        event.preventDefault();
        (target || panel).focus({ preventScroll: true });
        // Recover containment if a target was synchronously disabled or detached by
        // a render between key dispatch and focus assignment.
        if (!panel.contains(document.activeElement)) panel.focus({ preventScroll: true });
      };
      if (!focusRoot.contains(focused) || focused === focusRoot || focused === panel) {
        moveFocus(event.shiftKey ? last : first);
      } else if (event.shiftKey && focused === first) {
        moveFocus(last);
      } else if (!event.shiftKey && focused === last) {
        moveFocus(first);
      }
    }
  });

  listen(document.getElementById("reader-prev"), "click", () => goToPage(state.page - pageStep()));
  listen(document.getElementById("reader-next"), "click", () => goToPage(state.page + pageStep()));
  listen(document.getElementById("reader-goto"), "change", event => {
    goToPage(Number(event.target.value) - 1);
  });
  for (const button of document.querySelectorAll("[data-reading-mode]")) listen(button, "click", () => {
    state.readingMode = button.dataset.readingMode;
    goToPage(state.page);
  });
  listen(toc, "click", navigateFromToc);
  const sidebarToggle = document.getElementById("toggle-sidebar");
  if (sidebarToggle && themeButton) {
    const syncSidebar = () => {
      const collapsed = document.body.classList.contains("sidebar-collapsed");
      sidebarToggle.textContent = collapsed ? "›" : "‹";
      sidebarToggle.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
    };
    listen(sidebarToggle, "click", () => {
      document.body.classList.toggle("sidebar-collapsed");
      syncSidebar();
      updateReading();
      try { localStorage.setItem("report-reader-sidebar-collapsed", String(document.body.classList.contains("sidebar-collapsed"))); } catch (_) {}
    });
    try { document.body.classList.toggle("sidebar-collapsed", localStorage.getItem("report-reader-sidebar-collapsed") === "true"); } catch (_) {}
    syncSidebar();
  }
  listen(document, "keydown", event => {
    if (!panel.hidden || event.ctrlKey || event.metaKey || event.altKey
      || event.target.closest('input, textarea, select, [contenteditable="true"], .reader-table-wrap')) return;
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      event.preventDefault(); goToPage(state.page + (event.key === "ArrowRight" ? 1 : -1) * pageStep(), { focus: true });
    }
  });
  listen(window, "popstate", () => { if (!panel.hidden) closeEvidence(); restoreReading(); });
  listen(window, "hashchange", restoreReading);
  listen(wideScreen, "change", updateReading);
  listen(root, "toggle", event => {
    if (!event.target.matches("details")) return;
    cancelAnimationFrame(detailsFrame);
    detailsFrame = requestAnimationFrame(() => { state.layoutKey = null; updateReading(); });
  }, true);
  listen(window, "resize", () => {
    cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(updateReading);
  });
  listen(smallScreen, "change", syncPanelMode);
  }

  function resolveElements() {
    smallScreen = global.matchMedia("(max-width: 760px)");
    wideScreen = global.matchMedia("(min-width: 1101px)");
    shell = document.querySelector(".reader-shell");
    root = document.getElementById("reader-root");
    toc = document.getElementById("reader-toc-list");
    title = document.getElementById("reader-title");
    meta = document.getElementById("reader-meta");
    panel = document.getElementById("evidence-panel");
    backdrop = document.getElementById("evidence-backdrop");
    evidenceTitle = document.getElementById("evidence-title");
    evidenceBody = document.getElementById("evidence-body");
    infoButton = document.getElementById("reader-info-button");
    tocButton = document.getElementById("reader-toc-button");
    themeButton = document.getElementById("reader-theme-toggle");
    const required = [shell, root, toc, title, meta, panel, backdrop, evidenceTitle, evidenceBody, infoButton, tocButton,
      document.getElementById("evidence-close"), document.getElementById("reader-prev"),
      document.getElementById("reader-next"), document.getElementById("reader-goto"), document.getElementById("reader-controls")];
    if (!required.every(Boolean)) throw new Error("The report reader host structure is incomplete.");
  }

  function update(options = {}) {
    if (!lifecycle.mounted) throw new Error("The report reader is not mounted.");
    const payload = validatePayload(options.payload);
    lifecycle.controller?.abort();
    lifecycle.controller = new AbortController();
    renderPayload(payload);
    loadVersions(payload, lifecycle.controller.signal);
    lifecycle.callbacks.onReady?.(payload);
    return payload;
  }

  function dispose() {
    if (!lifecycle.mounted) return;
    lifecycle.mounted = false;
    lifecycle.generation += 1;
    lifecycle.controller?.abort();
    state.request?.abort();
    cancelAnimationFrame(detailsFrame);
    cancelAnimationFrame(resizeFrame);
    lifecycle.cleanups.splice(0).reverse().forEach(cleanup => cleanup());
    global.ReportContinuation?.dispose?.();
    state.lastFocus?.removeAttribute("aria-expanded");
    state.lastFocus = null;
    shell.inert = false;
    document.body.classList.remove("reader-modal-open", "reader-evidence-open", "reading-two-page");
    panel.hidden = true;
    backdrop.hidden = true;
    document.getElementById("reader-controls").hidden = true;
    root.replaceChildren();
    toc.replaceChildren();
    evidenceBody.replaceChildren();
    state.payload = null;
    state.pages = [];
    state.flow = null;
    state.flowNodes = null;
    lifecycle.callbacks.onDispose?.();
    lifecycle.callbacks = {};
  }

  function mount(options = {}) {
    if (lifecycle.mounted) dispose();
    resolveElements();
    lifecycle.mounted = true;
    lifecycle.generation += 1;
    lifecycle.callbacks = options.callbacks || {};
    lifecycle.controller = new AbortController();
    bindEvents();
    updateThemeButton();
    const generation = lifecycle.generation;
    document.fonts?.ready.then(() => {
      if (lifecycle.mounted && lifecycle.generation === generation) { state.layoutKey = null; updateReading(); }
    });
    if (options.payload) {
      try { update(options); } catch (error) { setError(error.message); lifecycle.callbacks.onError?.(error); }
      return { update, dispose };
    }
    loadPayload(options, lifecycle.controller.signal).then(payload => {
      if (!payload || !lifecycle.mounted || lifecycle.generation !== generation) return;
      renderPayload(payload);
      loadVersions(payload, lifecycle.controller.signal);
      lifecycle.callbacks.onReady?.(payload);
    }).catch(error => {
      if (error.name === "AbortError" || !lifecycle.mounted || lifecycle.generation !== generation) return;
      setError(error.message);
      lifecycle.callbacks.onError?.(error);
    });
    return { update, dispose };
  }

  global.ReportReaderRenderer = { mount, update, dispose, validatePayload, schemaVersion: SUPPORTED_SCHEMA };
  if (!global.__REPORT_READER_MANUAL_LIFECYCLE__) queueMicrotask(() => {
    if (!lifecycle.mounted && document.getElementById("reader-root")) mount();
  });
}(window));
