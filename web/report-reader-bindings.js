"use strict";

// Only locate exact, unambiguous saved excerpts. Never infer a reference from a
// similar number, a company name, or its position in the displayed paragraph.
(function exposeBindings(scope) {
  function planBindings(body, anchors, eligibleIds = null) {
    const candidates = anchors.filter(anchor => (!eligibleIds || eligibleIds.includes(anchor.id))
      && typeof anchor.text === "string" && anchor.text.length)
      .map(anchor => ({ anchor, start: body.indexOf(anchor.text), end: body.indexOf(anchor.text) + anchor.text.length }))
      .filter(item => item.start >= 0 && body.indexOf(item.anchor.text, item.start + 1) === -1)
      .filter(item => anchors.filter(anchor => anchor.text === item.anchor.text).length === 1)
      .sort((a, b) => (a.anchor.kind === "number" ? 0 : 1) - (b.anchor.kind === "number" ? 0 : 1)
        || a.anchor.text.length - b.anchor.text.length);
    const selected = [];
    for (const candidate of candidates) {
      if (!selected.some(item => candidate.start < item.end && candidate.end > item.start)) selected.push(candidate);
    }
    selected.sort((a, b) => a.start - b.start);
    const segments = [];
    let cursor = 0;
    for (const item of selected) {
      if (item.start > cursor) segments.push({ text: body.slice(cursor, item.start) });
      segments.push({ text: body.slice(item.start, item.end), anchorId: item.anchor.id });
      cursor = item.end;
    }
    if (cursor < body.length) segments.push({ text: body.slice(cursor) });
    return { segments, remaining: anchors.filter(anchor => !selected.some(item => item.anchor.id === anchor.id)) };
  }
  function splitTextRanges(value, maxLength = 900) {
    const text = String(value || "");
    if (!text) return [{ text: "", start: 0, end: 0 }];
    const ranges = [];
    let start = 0;
    while (start < text.length) {
      let end = Math.min(text.length, start + maxLength);
      if (end < text.length) {
        const window = text.slice(start, end);
        const candidates = [window.lastIndexOf("\n"), window.lastIndexOf("。") + 1,
          window.lastIndexOf("！") + 1, window.lastIndexOf("？") + 1,
          window.lastIndexOf("；") + 1, window.lastIndexOf(". ") + 1];
        const boundary = Math.max(...candidates);
        if (boundary > 0 && boundary >= Math.floor(maxLength * .45)) end = start + boundary;
      }
      // Do not cut a Unicode surrogate pair between separate DOM text nodes.
      if (end < text.length && /[\uD800-\uDBFF]/.test(text[end - 1]) && /[\uDC00-\uDFFF]/.test(text[end])) end -= 1;
      if (end <= start) end = Math.min(text.length, start + 2);
      ranges.push({ text: text.slice(start, end), start, end });
      start = end;
    }
    return ranges;
  }

  // Build one ordered report flow. Chapter membership is navigation metadata,
  // never a physical page boundary. Long prose and tables are split only at
  // explicit text/row boundaries, and every frozen anchor/point remains on one
  // and only one fragment.
  function buildContentFlow(chapters, anchors = [], options = {}) {
    const maxText = options.maxText || 900;
    const maxRows = options.maxRows || 8;
    const anchorById = new Map(anchors.map(anchor => [anchor.id, anchor]));
    const flow = [];
    chapters.forEach((chapter, chapterIndex) => {
      flow.push({ kind: "chapter", id: `chapter:${chapter.id}`, chapterId: chapter.id,
        chapterTitle: chapter.title, chapterIndex });
      (chapter.blocks || []).forEach((block, blockIndex) => {
        const base = { kind: "block", chapterId: chapter.id, chapterTitle: chapter.title,
          chapterIndex, blockIndex, block };
        if (block.type === "dataset" && block.presentation?.type !== "matrix" && Array.isArray(block.rows) && block.rows.length > maxRows) {
          for (let start = 0; start < block.rows.length; start += maxRows) {
            const rows = block.rows.slice(start, start + maxRows);
            flow.push({ ...base, id: `${block.id}:rows:${start}`, fragmentIndex: start / maxRows,
              fragmentCount: Math.ceil(block.rows.length / maxRows), block: { ...block, rows } });
          }
          return;
        }
        // These collections are paginated by measured complete rows/items in
        // the renderer. Text splitting must not repeat their entire collection,
        // and a fixed item count must not create artificial spacing boundaries.
        if ((block.type === "limitations" && Array.isArray(block.items))
            || (block.type === "sources" && Array.isArray(block.source_ids))) {
          flow.push({ ...base, id: block.id, fragmentIndex: 0, fragmentCount: 1 });
          return;
        }
        const ranges = splitTextRanges(block.text, maxText);
        if (ranges.length === 1) {
          flow.push({ ...base, id: block.id, fragmentIndex: 0, fragmentCount: 1 });
          return;
        }
        const ids = block.anchor_ids || [];
        const originalPlan = planBindings(String(block.text), ids.map(id => anchorById.get(id)).filter(Boolean));
        const inlineIds = originalPlan.segments.map(segment => segment.anchorId).filter(Boolean);
        const assigned = new Set();
        ranges.forEach((range, fragmentIndex) => {
          const fragmentIds = ids.filter(id => {
            if (assigned.has(id)) return false;
            const needle = anchorById.get(id)?.text;
            const offset = inlineIds.includes(id) && typeof needle === "string" && needle ? String(block.text).indexOf(needle) : -1;
            if (offset >= range.start && offset < range.end) { assigned.add(id); return true; }
            return false;
          });
          if (fragmentIndex === ranges.length - 1) ids.forEach(id => { if (!assigned.has(id)) { assigned.add(id); fragmentIds.push(id); } });
          flow.push({ ...base, id: `${block.id}:text:${fragmentIndex}`, fragmentIndex,
            fragmentCount: ranges.length, block: { ...block, text: range.text, anchor_ids: fragmentIds, inline_anchor_ids: inlineIds } });
        });
      });
    });
    return flow;
  }

  function flowItemWeight(item) {
    if (item.kind === "chapter") return 220;
    const block = item.block || {};
    return 150 + String(block.text || "").length
      + (block.rows || []).length * 105
      + (block.items || []).reduce((sum, value) => sum + 45 + String(value).length, 0);
  }

  function paginateFlow(flow, capacity = 1500) {
    const pages = [];
    let page = [], weight = 0;
    for (let index = 0; index < flow.length; index += 1) {
      const item = flow[index];
      const size = flowItemWeight(item);
      const nextSize = item.kind === "chapter" ? flowItemWeight(flow[index + 1] || {}) : 0;
      if (page.length && weight + size + nextSize > capacity) { pages.push(page); page = []; weight = 0; }
      else if (page.length && weight + size > capacity) { pages.push(page); page = []; weight = 0; }
      page.push(item); weight += size;
    }
    if (page.length || !pages.length) pages.push(page);
    return pages;
  }

  // Compatibility for older unit callers. New reader code uses the complete
  // cross-chapter flow rather than invoking this once per chapter.
  function paginateBlocks(blocks, capacity = 1400) {
    const pages = [];
    let page = [], weight = 0;
    for (const block of blocks) {
      const size = 160 + String(block.text || "").length + (block.rows || []).length * 80
        + (block.items || []).reduce((sum, item) => sum + String(item).length, 0);
      if (page.length && weight + size > capacity) { pages.push(page); page = []; weight = 0; }
      page.push(block); weight += size;
    }
    if (page.length || !pages.length) pages.push(page);
    return pages;
  }
  function projectReviewedChapters(savedChapters) {
    const chapters = savedChapters.map(chapter => ({ ...chapter, blocks: [...(chapter.blocks || [])] }));
    const isExcluded = block => block.type === "finding" && ["unsupported", "conflicting"].includes(block.status);
    const primary = chapters[0]?.blocks.find(block => block.type === "finding");
    if (primary && isExcluded(primary) && chapters[1]) {
      // An older frozen payload may have placed several findings in its first
      // chapter. Do not promote a later comment when its conclusion is excluded.
      const later = chapters[0].blocks.filter(block => block.type === "finding" && !isExcluded(block));
      chapters[0].blocks = chapters[0].blocks.filter(block => !later.includes(block));
      chapters[1].blocks.unshift(...later);
    }
    const excluded = [];
    for (const chapter of chapters.slice(0, 2)) {
      chapter.blocks = chapter.blocks.filter(block => {
        if (!isExcluded(block)) return true;
        excluded.push({ ...block, type: "review_finding" });
        return false;
      });
    }
    if (excluded.length) {
      if (!chapters[0].blocks.some(block => block.id === "unsupported-content-notice")) {
        chapters[0].blocks.push({ id: "unsupported-content-notice", type: "notice", title: "Some content cannot be treated as a conclusion",
          text: "The saved review record contains content that is unsupported or contradicted by evidence. It has been moved to the pending-review appendix under “Boundaries and Methods”; the related questions cannot yet be answered reliably." });
      }
      const boundaries = chapters.find(chapter => chapter.id === "boundaries-method");
      if (boundaries) boundaries.blocks.push(...excluded);
    }
    return chapters;
  }
  function safeChartNumber(value) {
    const raw = String(value).trim();
    if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(raw)) return null;
    const numeric = Number(raw);
    return Number.isFinite(numeric) && Math.abs(numeric) <= Number.MAX_SAFE_INTEGER
      && !(numeric === 0 && /[1-9]/.test(raw)) ? numeric : null;
  }
  function periodText(period) {
    if (!period) return "";
    if (Array.isArray(period.ranges) && period.ranges.length) {
      return (period.ranges.length > 1 ? "Comparison periods: " : "")
        + period.ranges.map(range => range.filter(Boolean).join(" to ")).join("; ");
    }
    return [period.start, period.end].filter(Boolean).join(" to ");
  }
  function utcDate(value) {
    const match = typeof value === "string" && /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
    if (!match || Number(match[1]) < 1) return null;
    const date = new Date(0);
    date.setUTCFullYear(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    const time = date.getTime();
    return date.getUTCFullYear() === Number(match[1]) && date.getUTCMonth() + 1 === Number(match[2])
      && date.getUTCDate() === Number(match[3]) ? time : null;
  }
  function validateTimeSeries(dataset) {
    const reasons = [], rows = Array.isArray(dataset?.rows) ? dataset.rows : [];
    const chart = dataset?.chart, axis = chart?.time_axis;
    if (chart?.type !== "line" || chart.x !== "period.end" || chart.y !== "value"
        || axis?.version !== "utc-period-axis/1" || axis.field !== "period_end"
        || axis.timezone !== "UTC" || axis.gap_policy !== "break") reasons.push("Supported frozen timeline semantics are missing");
    if (rows.length < 2) reasons.push("A true line chart requires at least two fact points");
    if (new Set(rows.map(row => row?.id)).size !== rows.length) reasons.push("Fact point identifiers are duplicated");
    const normalized = rows.map((row, index) => {
      const start = utcDate(row?.period?.start), end = utcDate(row?.period?.end);
      const numeric = safeChartNumber(row?.value);
      if (row?.record_kind !== "fact") reasons.push(`Row ${index + 1} is not a direct fact`);
      if (!["year", "quarter"].includes(row?.period?.kind)) reasons.push(`Row ${index + 1} has an unsupported period type`);
      if (Array.isArray(row?.period?.ranges)) reasons.push(`Row ${index + 1} spans multiple periods`);
      if (start === null || end === null || start > end) reasons.push(`Row ${index + 1} has incomplete or invalid dates`);
      if (numeric === null) reasons.push(`Row ${index + 1} has a value outside the safe charting range`);
      for (const key of ["id", "company_id", "metric", "unit", "basis"]) {
        if (typeof row?.[key] !== "string" || !row[key].trim()) reasons.push(`Row ${index + 1} is missing ${key}`);
      }
      return { row, index, start, end, numeric };
    });
    for (const key of ["company_id", "metric", "unit", "basis"]) {
      if (new Set(rows.map(row => row?.[key])).size > 1) reasons.push(`Facts have inconsistent ${key}`);
    }
    if (new Set(rows.map(row => row?.period?.kind)).size > 1) reasons.push("Facts have inconsistent period types");
    const sorted = [...normalized].sort((a, b) => (a.end ?? 0) - (b.end ?? 0));
    if (new Set(sorted.map(item => item.end)).size !== sorted.length) reasons.push("Facts have duplicate period end dates");
    for (let index = 1; index < sorted.length; index++) {
      if (sorted[index].start !== null && sorted[index - 1].end !== null && sorted[index].start <= sorted[index - 1].end) reasons.push("Fact periods overlap");
    }
    const segments = [];
    sorted.forEach((item, index) => {
      const previous = sorted[index - 1];
      if (!previous || item.start !== previous.end + 86400000) segments.push([]);
      segments[segments.length - 1].push(item);
    });
    return { safe: reasons.length === 0, reasons: [...new Set(reasons)], rows: normalized, sorted, segments };
  }
  scope.ReportReaderBindings = { planBindings, splitTextRanges, buildContentFlow, paginateFlow, paginateBlocks, projectReviewedChapters, safeChartNumber, periodText, utcDate, validateTimeSeries };
  if (typeof module !== "undefined") module.exports = { planBindings, splitTextRanges, buildContentFlow, paginateFlow, paginateBlocks, projectReviewedChapters, safeChartNumber, periodText, utcDate, validateTimeSeries };
}(typeof window !== "undefined" ? window : globalThis));
