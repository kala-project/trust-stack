"use strict";

(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ReportReaderTables = api;
}(typeof globalThis !== "undefined" ? globalThis : this, function () {
  function fail(message) {
    throw new TypeError("Invalid comparison table: " + message);
  }

  function text(value, field) {
    if (typeof value !== "string" || !value.trim()) fail(field + " must be non-empty text");
    return value;
  }

  function buildComparisonTableModel(dataset) {
    if (!dataset || typeof dataset !== "object" || Array.isArray(dataset)) fail("dataset is required");
    const presentation = dataset.presentation;
    if (!presentation || presentation.type !== "matrix") fail("explicit matrix presentation is required");
    if (!Array.isArray(dataset.rows) || !dataset.rows.length) fail("bound point rows are required");
    if (!Array.isArray(presentation.columns) || !presentation.columns.length) fail("columns are required");
    if (!Array.isArray(presentation.rows) || !presentation.rows.length) fail("rows are required");

    const points = new Map();
    dataset.rows.forEach(function (point) {
      if (!point || typeof point.id !== "string" || points.has(point.id)) fail("point IDs must be unique");
      points.set(point.id, point);
    });
    const used = new Set();
    const columns = presentation.columns.map(function (column) {
      return { key: text(column && column.key, "column key"), label: text(column && column.label, "column label") };
    });
    if (new Set(columns.map(function (column) { return column.key; })).size !== columns.length) fail("column keys must be unique");

    const rows = presentation.rows.map(function (row) {
      if (!row || !Array.isArray(row.cells) || row.cells.length !== columns.length) fail("each row must map one cell per column");
      const cells = row.cells.map(function (cell) {
        const pointId = text(cell && cell.point_id, "cell point_id");
        const point = points.get(pointId);
        if (!point) fail("cell references an unknown point");
        if (used.has(pointId)) fail("a point is mapped more than once");
        const expectedAnchor = pointId + "-cell-value";
        if (!cell.anchor_id || cell.anchor_id !== expectedAnchor) fail("cell anchor does not match its bound point");
        used.add(pointId);
        return {
          pointId: pointId,
          anchorId: cell.anchor_id,
          value: point.value,
          unit: point.unit,
          displayText: typeof point.display_text === "string" ? point.display_text : null,
          basis: point.basis,
          period: point.period,
          reference: point.reference,
          traceId: point.trace_id
        };
      });
      return { key: text(row.key, "row key"), label: text(row.label, "row label"), cells: cells };
    });
    if (new Set(rows.map(function (row) { return row.key; })).size !== rows.length) fail("row keys must be unique");
    if (used.size !== points.size) fail("every bound point must be mapped exactly once");

    return {
      id: text(dataset.id, "dataset id"),
      title: typeof dataset.title === "string" ? dataset.title : "",
      description: typeof dataset.description === "string" ? dataset.description : "",
      afterBlockId: text(presentation.after_block_id, "after_block_id"),
      unit: typeof presentation.unit === "string" ? presentation.unit : null,
      note: typeof presentation.note === "string" ? presentation.note : null,
      columns: columns,
      rows: rows
    };
  }

  function createComparisonTable(dataset, options) {
    const model = buildComparisonTableModel(dataset);
    const settings = options || {};
    const doc = settings.document || (typeof document !== "undefined" ? document : null);
    if (!doc) fail("a document is required for rendering");
    const table = doc.createElement("table");
    table.dataset.datasetId = model.id;
    const thead = doc.createElement("thead");
    const headRow = doc.createElement("tr");
    const corner = doc.createElement("th");
    corner.scope = "col";
    corner.textContent = settings.rowHeaderLabel || "Item";
    headRow.appendChild(corner);
    model.columns.forEach(function (column) {
      const th = doc.createElement("th");
      th.scope = "col";
      th.textContent = column.label;
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = doc.createElement("tbody");
    model.rows.forEach(function (row) {
      const tr = doc.createElement("tr");
      const th = doc.createElement("th");
      th.scope = "row";
      th.textContent = row.label;
      tr.appendChild(th);
      row.cells.forEach(function (cell) {
        const td = doc.createElement("td");
        const control = doc.createElement("button");
        control.type = "button";
        control.dataset.anchorId = cell.anchorId;
        control.textContent = cell.displayText || [cell.value, cell.unit].filter(Boolean).join(" ");
        if (typeof settings.onAnchor === "function") {
          control.addEventListener("click", function () { settings.onAnchor(cell.anchorId, cell); });
        }
        td.appendChild(control);
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    return table;
  }

  function sliceComparisonDataset(dataset, start, end) {
    // Validate the complete publisher-authored mapping before projecting a
    // physical-page fragment. A fragment only contains complete matrix rows;
    // point membership is never inferred from labels or array position.
    buildComparisonTableModel(dataset);
    const sourceRows = dataset.presentation.rows;
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end <= start || end > sourceRows.length) {
      fail("row slice is outside the explicit presentation");
    }
    const presentationRows = sourceRows.slice(start, end);
    const pointIds = new Set();
    presentationRows.forEach(function (row) {
      row.cells.forEach(function (cell) { pointIds.add(cell.point_id); });
    });
    const fragment = {
      ...dataset,
      rows: dataset.rows.filter(function (point) { return pointIds.has(point.id); }),
      presentation: { ...dataset.presentation, rows: presentationRows }
    };
    buildComparisonTableModel(fragment);
    return fragment;
  }

  return { buildComparisonTableModel: buildComparisonTableModel, createComparisonTable: createComparisonTable,
    sliceComparisonDataset: sliceComparisonDataset };
}));
