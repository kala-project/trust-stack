(function () {
  "use strict";

  const STATUS = {
    available: "Verifiable",
    partial: "Partially verifiable",
    unavailable: "Path unavailable",
  };

  function list(value) {
    return Array.isArray(value) ? value : [];
  }

  function object(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }

  function text(value, fallback) {
    if (value === 0 || value === false) return String(value);
    return typeof value === "string" && value.trim() ? value.trim() : (fallback || "");
  }

  function node(tag, options, children) {
    const element = document.createElement(tag);
    const config = options || {};
    if (config.className) element.className = config.className;
    if (config.text !== undefined) element.textContent = text(config.text);
    Object.entries(config.attrs || {}).forEach(function (entry) {
      if (entry[1] !== null && entry[1] !== undefined) element.setAttribute(entry[0], String(entry[1]));
    });
    list(Array.isArray(children) ? children : [children]).forEach(function (child) {
      if (child) element.appendChild(child);
    });
    return element;
  }

  function paragraph(label, value, className) {
    return node("div", { className: className || "evidence-browser-field" }, [
      node("dt", { text: label }),
      node("dd", { text: text(value, "Not recorded") }),
    ]);
  }

  function safeExternalUrl(value) {
    if (typeof value !== "string" || !/^https?:\/\//iu.test(value.trim())) return null;
    try {
      const url = new URL(value.trim());
      return (url.protocol === "http:" || url.protocol === "https:") ? url.href : null;
    } catch (error) {
      return null;
    }
  }

  function sourceKey(source) {
    return text(source.document_version_id || source.source_id);
  }

  function registeredSource(source, reportPayload) {
    const key = sourceKey(source);
    if (!key) return null;
    const sources = list(object(reportPayload).sources);
    const direct = sources.find(function (candidate) {
      return candidate && candidate.document_version_id && (candidate.id === key || candidate.document_version_id === key);
    });
    if (direct) return source.source_sha256 && source.source_sha256 !== direct.source_sha256 ? null : direct;
    // Legacy structured evidence uses the frozen source key, not a document-version ID.
    // Resolve only an unambiguous identity in THIS report, never by title or live catalog.
    const matches = sources.filter(function (candidate) {
      return candidate && candidate.document_version_id && candidate.source_key === key
        && source.source_sha256 && candidate.source_sha256 === source.source_sha256
        && source.source_url && candidate.url === source.source_url;
    });
    return matches.length === 1 ? matches[0] : null;
  }

  function rawDownloadHref(source, reportPayload) {
    const registered = registeredSource(source, reportPayload);
    const reportId = text(object(object(reportPayload).report).id);
    if (!registered || !reportId || !registered.source_sha256 || !registered.document_version_id || registered.raw_download_available === false) return null;
    return "/api/reports/raw-source?" + new URLSearchParams({ report_id: reportId, source_id: registered.id }).toString();
  }

  function traceLabel(trace, index) {
    const calculation = object(trace.calculation);
    const source = object(trace.source);
    const lastStep = list(trace.steps).slice(-1)[0] || {};
    if (text(calculation.formula)) return "Calculation · " + text(calculation.value, text(source.value, "saved result")) + (text(calculation.unit) ? " " + calculation.unit : "");
    if (text(source.title)) return "Source text · " + source.title;
    if (text(lastStep.title)) return lastStep.title;
    return "Evidence " + (index + 1);
  }

  function traceKind(trace) {
    if (Object.keys(object(trace.calculation)).length) return "Calculation record";
    const source = object(trace.source);
    if (list(source.inputs).length) return "Composite inputs";
    if (source.table_excerpt || object(source.locator).type === "document_table_cell") return "Source table cell";
    if (source.quote || source.context) return "Source excerpt";
    return "Frozen record";
  }

  function sourceChoices(trace) {
    const source = object(trace.source);
    const nested = list(source.sources);
    if (nested.length) {
      const inputs = list(source.inputs);
      return nested.map(function (item, index) {
        // Join only on the saved fact identity; array position is not evidence.
        const matches = inputs.filter(function (fact) {
          return item.fact_id && fact.fact_id === item.fact_id
            && (!item.evidence_id || fact.evidence_id === item.evidence_id);
        });
        const uniqueSource = nested.filter(function (candidate) { return item.fact_id && candidate.fact_id === item.fact_id; }).length === 1;
        const fact = matches.length === 1 && uniqueSource ? matches[0] : {};
        const label = [text(fact.row_label || fact.metric), text(fact.value), text(fact.unit)].filter(Boolean).join(" · ");
        return { source: object(item), label: label || text(item.title, "Underlying source " + (index + 1)) + " (unbound input)", fact: fact };
      });
    }
    if (Object.keys(source).length && (source.quote || source.context || source.locator || source.title || source.source_id)) {
      return [{ source: source, label: text(source.title, "Direct source"), fact: {} }];
    }
    return [];
  }

  function makeColumn(step, title) {
    const headingId = "evidence-browser-heading-" + step + "-" + Math.random().toString(36).slice(2);
    const body = node("div", {
      className: "evidence-browser-column-body",
      attrs: { role: "tabpanel", "aria-labelledby": headingId },
    });
    const column = node("section", {
      className: "miller-col evidence-browser-column evidence-browser-step-" + step + (step === 4 ? " source-col" : ""),
      attrs: { "data-evidence-step": step, "aria-labelledby": headingId },
    }, [
      node("div", { className: "col-head evidence-browser-column-head", attrs: { id: headingId } }, [
        node("span", { text: String(step), attrs: { "aria-hidden": "true" } }),
        node("strong", { text: title }),
      ]),
      body,
    ]);
    return { column: column, body: body };
  }

  function empty(message) {
    return node("p", { className: "evidence-browser-empty", text: message });
  }

  function tableExcerpt(source) {
    const excerpt = object(source.table_excerpt);
    const rows = list(excerpt.rows);
    if (!rows.length) return null;
    const table = node("table", { className: "source-table evidence-browser-table" });
    const tbody = node("tbody");
    rows.forEach(function (row) {
      const tr = node("tr");
      list(row.cells).forEach(function (cell, columnIndex) {
        const selected = row.row_index === excerpt.selected_row && columnIndex === excerpt.selected_column;
        tr.appendChild(node("td", {
          className: selected ? "highlight evidence-browser-target-cell" : "",
          text: cell,
          attrs: selected ? { "aria-label": "Target cell: " + text(cell) } : {},
        }));
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    return node("div", { className: "evidence-browser-table-wrap", attrs: { tabindex: "0" } }, table);
  }

  function pdfSummary(source) {
    const pdf = object(object(source.locator).pdf_provenance);
    if (!Number.isInteger(pdf.physical_page) || pdf.physical_page < 1) return null;
    const target = object(object(pdf.target).cell);
    const parts = ["File page " + pdf.physical_page + " (file page order; may differ from printed page numbers)"];
    if (text(target.raw_text)) parts.push("Source cell: " + target.raw_text);
    const output = [node("p", { className: "evidence-browser-pdf", text: parts.join(" · ") })];
    [["Source table title", pdf.title], ["Source table unit", pdf.unit_line], ["Source table fiscal period", pdf.fiscal_period_header]].forEach(function (entry) {
      if (text(object(entry[1]).raw_text)) output.push(node("p", { className: "evidence-browser-context-line", text: entry[0] + ": " + entry[1].raw_text }));
    });
    const box = object(target.bbox);
    if ([box.x_min, box.y_min, box.x_max, box.y_max].every(Number.isFinite)) {
      output.push(node("details", { className: "evidence-browser-technical" }, [
        node("summary", { text: "Source cell coordinates and parsing status" }),
        node("p", { text: "Left " + box.x_min + " · Top " + box.y_min + " · Right " + box.x_max + " · Bottom " + box.y_max }),
        text(pdf.capability_status) ? node("p", { text: "Parsing status: " + pdf.capability_status }) : null,
      ]));
    }
    return node("div", {}, output);
  }

  function locationSummary(source, fact) {
    const locator = object(source.locator);
    const pdf = object(locator.pdf_provenance);
    const dl = node("dl", { className: "field-list evidence-browser-fields" });
    dl.appendChild(paragraph("Source", text(source.title, "Title not recorded")));
    if (text(source.published_at)) dl.appendChild(paragraph("Publication date", source.published_at));
    if (Number.isInteger(pdf.physical_page) && pdf.physical_page > 0) dl.appendChild(paragraph("PDF location", "File page " + pdf.physical_page));
    if (Number.isInteger(locator.table_index)) dl.appendChild(paragraph("Table location", "Table " + (locator.table_index + 1) + (Number.isInteger(locator.row_index) ? " · Row " + (locator.row_index + 1) : "") + (Number.isInteger(locator.column_index) ? " · Column " + (locator.column_index + 1) : "")));
    if (Number.isInteger(locator.start) || Number.isInteger(locator.end)) dl.appendChild(paragraph("Source-text location", [Number.isInteger(locator.start) ? "Character " + locator.start : "", Number.isInteger(locator.end) ? "to " + locator.end : ""].filter(Boolean).join(" ")));
    if (text(fact.row_label || fact.metric)) dl.appendChild(paragraph("Selected input", [fact.row_label || fact.metric, fact.raw_value || fact.value, fact.unit].filter(Boolean).join(" · ")));
    if (text(fact.period_start) || text(fact.period_end)) dl.appendChild(paragraph("Recorded period", [fact.period_start, fact.period_end].filter(Boolean).join(" to ")));
    if (text(source.rights_status) && source.rights_status !== "approved") dl.appendChild(paragraph("Source permissions", "Current status: " + source.rights_status + "; this is not considered independently approved.", "evidence-browser-field evidence-browser-warning"));
    if (dl.children.length === 1 && !Object.keys(locator).length) dl.appendChild(paragraph("Location", "No more precise location was saved."));
    return dl;
  }

  function sourceContent(source, reportPayload) {
    const fragment = document.createDocumentFragment();
    fragment.appendChild(node("p", { className: "evidence-browser-slice-note", text: "Only the frozen slice relevant to this review is shown below; it is not the complete source or full text." }));
    const pdf = pdfSummary(source);
    if (pdf) fragment.appendChild(pdf);
    const excerpt = tableExcerpt(source);
    if (excerpt) fragment.appendChild(excerpt);
    if (text(source.context)) fragment.appendChild(node("div", { className: "evidence-browser-context" }, [
      node("p", { className: "evidence-browser-small-label", text: "Saved context" }),
      node("p", { text: source.context }),
    ]));
    if (text(source.quote)) fragment.appendChild(node("blockquote", { className: "source-quote evidence-browser-quote", text: source.quote }));

    const links = node("div", { className: "source-actions evidence-browser-links" });
    const external = safeExternalUrl(source.source_url || source.url);
    if (external) links.appendChild(node("a", { className: "reader-source-download evidence-browser-link", text: "Open source page", attrs: { href: external, target: "_blank", rel: "noreferrer" } }));
    const download = rawDownloadHref(source, reportPayload);
    if (download) links.appendChild(node("a", { className: "reader-source-download evidence-browser-link", text: "Download frozen source", attrs: { href: download } }));
    if (links.children.length) fragment.appendChild(links);
    if (!pdf && !excerpt && !text(source.context) && !text(source.quote)) fragment.appendChild(empty("This source is registered, but no displayable source text or table slice was saved."));
    if (!download) fragment.appendChild(node("p", { className: "evidence-browser-unavailable", text: "No frozen-source download is currently available; the complete source text will not be reconstructed." }));
    return fragment;
  }

  function originalPreview(source, reportPayload, onOpen) {
    const openButton = node("button", { className: "open-source", text: "Enlarge source", attrs: { type: "button" } });
    const preview = node("article", { className: "source-preview evidence-browser-source-preview" }, [openButton]);
    openButton.addEventListener("click", function () { onOpen(source, openButton); });
    preview.appendChild(sourceContent(source, reportPayload));
    return preview;
  }

  function semanticReviewSummary(anchor) {
    const review = object(object(anchor).semantic_review);
    let claims = list(review.claims).filter(function (claim) { return claim && typeof claim === "object"; });
    if (!claims.length && (text(review.support_status) || text(review.reviewed_text))) {
      claims = [{
        id: review.claim_id,
        exact_text: review.reviewed_text,
        support_status: review.support_status,
        reason: review.reason,
        evidence_refs: review.evidence_refs,
      }];
    }
    const statusLabels = {
      supported_by_evidence: "Supported",
      inference: "Inference",
      unsupported: "Unsupported",
      conflicting: "Conflicting",
    };
    if (!claims.length) {
      return node("div", { className: "evidence-browser-semantic-review evidence-browser-semantic-empty" }, [
        node("p", { text: "No displayable model review was saved; this does not mean the claim was approved." }),
      ]);
    }
    const counts = {};
    claims.forEach(function (claim) {
      const status = text(claim.support_status, "not_assessed");
      counts[status] = (counts[status] || 0) + 1;
    });
    const statusText = Object.keys(counts).map(function (status) {
      const count = counts[status];
      return (statusLabels[status] || "Unrecognized status") + ": " + count + (count === 1 ? " claim" : " claims");
    }).join("; ");
    const details = node("details", { className: "evidence-browser-semantic-details" }, [
      node("summary", { text: "View saved model review (" + claims.length + ")" }),
    ]);
    claims.forEach(function (claim) {
      const status = text(claim.support_status, "not_assessed");
      details.appendChild(node("article", {
        className: "evidence-browser-semantic-claim semantic-status-" + status,
        attrs: { "data-semantic-status": status },
      }, [
        node("strong", { text: statusLabels[status] || "Status unrecognized" }),
        node("p", { className: "evidence-browser-semantic-text", text: text(claim.exact_text, "Exact sentence not saved") }),
        node("p", { className: "evidence-browser-semantic-reason", text: "Reason: " + text(claim.reason, "Not recorded") }),
      ]));
    });
    return node("div", { className: "evidence-browser-semantic-review" }, [
      node("p", { className: "evidence-browser-semantic-boundary", text: "This is a model review of selected claims, not independent approval of the full passage or report." }),
      node("p", { className: "evidence-browser-semantic-status", text: "Saved status: " + statusText }),
      details,
    ]);
  }

  function render(options) {
    const payload = object(options && options.payload);
    const reportPayload = object(options && options.reportPayload);
    const evidence = object(payload.evidence);
    const verification = object(evidence.verification);
    const traces = list(object(payload.frozen_detail).traces).length ? list(payload.frozen_detail.traces) : list(evidence.traces);
    const root = node("section", { className: "report-evidence-browser", attrs: { "data-mobile-step": "1", "aria-label": "Evidence hierarchy browser" } });

    root.appendChild(node("section", { className: "selected-claim evidence-browser-summary" }, [
      node("div", { className: "claim-card-head" }, [node("span", { text: "Selected claim" })]),
      node("p", { text: text(evidence.claim, text(object(payload.anchor).text, "Claim not recorded")) }),
      semanticReviewSummary(payload.anchor),
    ]));
    root.appendChild(node("section", { className: "verification-goal evidence-browser-goal" }, [
      node("article", {}, [node("span", { text: "Verification method" }), node("p", { text: text(verification.method, "Review the saved evidence and sources.") })]),
      node("article", {}, [node("span", { text: "Scope boundary" }), node("p", { text: text(verification.boundary, "Supports only the current claim; it cannot be extrapolated automatically.") })]),
    ]));

    const mobileNav = node("nav", { className: "evidence-browser-mobile-nav", attrs: { "aria-label": "Evidence browser steps" } });
    ["Evidence list", "Evidence details", "Source location", "Source material"].forEach(function (label, index) {
      const step = index + 1;
      const button = node("button", { text: step + " " + label, attrs: { type: "button", "data-mobile-target": step, "aria-current": step === 1 ? "step" : null } });
      button.addEventListener("click", function () {
        root.setAttribute("data-mobile-step", String(step));
        mobileNav.querySelectorAll("button").forEach(function (item) { item.removeAttribute("aria-current"); });
        button.setAttribute("aria-current", "step");
      });
      mobileNav.appendChild(button);
    });
    root.appendChild(mobileNav);

    const columns = [makeColumn(1, "Evidence list"), makeColumn(2, "Evidence details"), makeColumn(3, "Source location"), makeColumn(4, "Source material")];
    const grid = node("div", { className: "miller evidence-browser-grid" }, columns.map(function (item) { return item.column; }));
    root.appendChild(grid);

    const viewerTitle = node("h2", { text: "Frozen source" });
    const viewerClose = node("button", { text: "Back to evidence", attrs: { type: "button", "data-close-source-viewer": "" } });
    const viewerBody = node("div", { className: "source-viewer-body" });
    const viewer = node("section", { className: "source-viewer", attrs: { "aria-label": "Source material viewer", hidden: "" } }, [
      node("header", { className: "source-viewer-head" }, [
        node("div", {}, [node("p", { className: "kicker", text: "Source material" }), viewerTitle]),
        viewerClose,
      ]),
      viewerBody,
    ]);
    viewer.hidden = true;
    root.appendChild(viewer);
    let viewerTrigger = null;

    function closeViewer() {
      if (viewer.hidden) return;
      viewer.hidden = true;
      viewer.setAttribute("hidden", "");
      grid.removeAttribute("inert");
      root.querySelector(".selected-claim").removeAttribute("inert");
      root.querySelector(".verification-goal").removeAttribute("inert");
      mobileNav.removeAttribute("inert");
      if (viewerTrigger) viewerTrigger.focus();
    }

    function openViewer(source, trigger) {
      viewerTrigger = trigger;
      viewerTitle.textContent = text(source.title, "Frozen source");
      viewerBody.replaceChildren(node("article", { className: "source-document" }, sourceContent(source, reportPayload)));
      viewer.hidden = false;
      viewer.removeAttribute("hidden");
      grid.setAttribute("inert", "");
      root.querySelector(".selected-claim").setAttribute("inert", "");
      root.querySelector(".verification-goal").setAttribute("inert", "");
      mobileNav.setAttribute("inert", "");
      viewerClose.focus();
    }
    viewerClose.addEventListener("click", closeViewer);

    function renderSource(choice) {
      columns[2].body.replaceChildren(locationSummary(choice.source, choice.fact));
      columns[3].body.replaceChildren(originalPreview(choice.source, reportPayload, openViewer));
    }

    function renderTrace(trace, traceIndex) {
      columns[0].body.querySelectorAll("button").forEach(function (button, index) {
        const selected = index === traceIndex;
        button.setAttribute("aria-selected", selected ? "true" : "false");
        button.classList.toggle("is-selected", selected);
        button.classList.toggle("active", selected);
      });
      const detail = document.createDocumentFragment();
      detail.appendChild(node("p", { className: "evidence-browser-kind", text: traceKind(trace) + " · " + (STATUS[trace.status] || "Status unrecognized") }));
      const calculation = object(trace.calculation);
      if (Object.keys(calculation).length) {
        detail.appendChild(node("div", { className: "formula-line evidence-browser-formula" }, [
          node("span", { text: text(calculation.formula, "Formula not recorded") }),
          node("strong", { text: [calculation.value, calculation.unit].filter(function (value) { return value !== null && value !== undefined && value !== ""; }).join(" ") }),
        ]));
        const operands = list(object(calculation.arithmetic).operands);
        if (operands.length) {
          const operandList = node("ul", { className: "evidence-browser-operands" });
          operands.forEach(function (operand) { operandList.appendChild(node("li", { text: [operand.metric || "Upstream calculation output", operand.value, operand.unit].filter(Boolean).join(" · ") })); });
          detail.appendChild(operandList);
        }
      }
      const steps = list(trace.steps);
      if (steps.length) {
        const path = node("ol", { className: "evidence-browser-path" });
        steps.forEach(function (step) { path.appendChild(node("li", { text: [step.title, step.details].filter(Boolean).join(": ") })); });
        detail.appendChild(path);
      }
      list(trace.limitations).forEach(function (limitation) { detail.appendChild(node("p", { className: "evidence-browser-warning", text: "Limitation: " + limitation })); });
      list(calculation.limitations).forEach(function (limitation) { detail.appendChild(node("p", { className: "evidence-browser-warning", text: "Limitation: " + limitation })); });

      const choices = sourceChoices(trace);
      if (choices.length) {
        detail.appendChild(node("p", { className: "evidence-browser-small-label", text: choices.length > 1 ? "Select an underlying input or source" : "Direct source" }));
        const chooser = node("div", { className: "evidence-menu evidence-browser-source-choices", attrs: { role: "tablist", "aria-label": "Underlying sources" } });
        choices.forEach(function (choice, choiceIndex) {
          const button = node("button", { className: "evidence-menu-item", attrs: { type: "button", role: "tab", "aria-selected": choiceIndex === 0 ? "true" : "false" } }, [
            node("span", { text: "Source" }), node("strong", { text: choice.label }), node("em", { text: "View source location and frozen slice" }),
          ]);
          button.addEventListener("click", function () {
            chooser.querySelectorAll("button").forEach(function (item) { item.setAttribute("aria-selected", "false"); item.classList.remove("is-selected"); item.classList.remove("active"); });
            button.setAttribute("aria-selected", "true");
            button.classList.add("is-selected");
            button.classList.add("active");
            renderSource(choice);
          });
          if (choiceIndex === 0) { button.classList.add("is-selected"); button.classList.add("active"); }
          chooser.appendChild(button);
        });
        detail.appendChild(chooser);
        columns[1].body.replaceChildren(detail);
        renderSource(choices[0]);
      } else {
        columns[1].body.replaceChildren(detail);
        columns[2].body.replaceChildren(empty("No locatable underlying source was saved for this evidence."));
        columns[3].body.replaceChildren(empty("Source material is unavailable; inferences will not be shown as direct evidence."));
      }
    }

    if (!traces.length) {
      columns[0].body.replaceChildren(empty("No expandable evidence path was saved for this claim."));
      columns[1].body.replaceChildren(empty("No evidence details to display."));
      columns[2].body.replaceChildren(empty("No source location to display."));
      columns[3].body.replaceChildren(empty("Source material is unavailable."));
      return root;
    }

    const menu = node("div", { className: "evidence-menu evidence-browser-traces", attrs: { role: "tablist", "aria-label": "Evidence list" } });
    traces.forEach(function (trace, index) {
      const button = node("button", {
        className: "evidence-menu-item evidence-browser-trace",
        attrs: { type: "button", role: "tab", "aria-selected": index === 0 ? "true" : "false" },
      }, [node("span", { text: traceKind(trace) }), node("strong", { text: traceLabel(trace, index) }), node("em", { text: STATUS[trace.status] || "Status unrecognized" })]);
      button.addEventListener("click", function () { renderTrace(trace, index); });
      if (index === 0) { button.classList.add("is-selected"); button.classList.add("active"); }
      menu.appendChild(button);
    });
    columns[0].body.replaceChildren(menu);
    renderTrace(traces[0], 0);
    return root;
  }

  window.ReportReaderSecurity = Object.freeze({ safeExternalUrl: safeExternalUrl });
  window.ReportEvidenceBrowser = Object.freeze({ render: render });
}());
