"use strict";

const assert = require("assert");
const tables = require("../web/report-reader-tables.js");

function fixture() {
  return {
    id: "dataset-1",
    title: "契约测试矩阵",
    rows: [
      { id: "point-a", label: "标签不参与映射", value: "100", unit: "CNY", period: { kind: "year" } },
      { id: "point-b", label: "可完全不同", value: "200", unit: "CNY", period: { kind: "year" } }
    ],
    presentation: {
      type: "matrix",
      after_block_id: "report-block-2",
      columns: [{ key: "p1", label: "期间一" }, { key: "p2", label: "期间二" }],
      rows: [{ key: "metric", label: "指标", cells: [
        { point_id: "point-a", anchor_id: "point-a-cell-value" },
        { point_id: "point-b", anchor_id: "point-b-cell-value" }
      ] }]
    }
  };
}

const input = fixture();
const before = JSON.stringify(input);
const model = tables.buildComparisonTableModel(input);
assert.deepStrictEqual(model.columns.map(column => column.label), ["期间一", "期间二"]);
assert.deepStrictEqual(model.rows.map(row => row.label), ["指标"]);
assert.deepStrictEqual(model.rows[0].cells.map(cell => cell.value), ["100", "200"]);
assert.deepStrictEqual(model.rows[0].cells.map(cell => cell.anchorId),
                       ["point-a-cell-value", "point-b-cell-value"]);
assert.strictEqual(model.afterBlockId, "report-block-2");
assert.strictEqual(JSON.stringify(input), before, "helper must not mutate payload");

const slicedInput = fixture();
slicedInput.rows.push(
  { id: "point-d", label: "数组位置不代表单元格", value: "400", unit: "CNY", period: { kind: "year" } },
  { id: "point-c", label: "非顺序点位", value: "300", unit: "CNY", period: { kind: "year" } }
);
slicedInput.presentation.rows.push({ key: "metric-2", label: "指标二", cells: [
  { point_id: "point-c", anchor_id: "point-c-cell-value" },
  { point_id: "point-d", anchor_id: "point-d-cell-value" }
] });
const slicedBefore = JSON.stringify(slicedInput);
const secondRow = tables.sliceComparisonDataset(slicedInput, 1, 2);
assert.deepStrictEqual(secondRow.presentation.rows.map(row => row.key), ["metric-2"]);
assert.deepStrictEqual(secondRow.rows.map(row => row.id), ["point-d", "point-c"],
                       "slice preserves saved point order while selecting only explicit IDs");
assert.deepStrictEqual(tables.buildComparisonTableModel(secondRow).rows[0].cells.map(cell => cell.value), ["300", "400"]);
assert.strictEqual(secondRow.id, slicedInput.id, "physical fragments retain one dataset identity");
assert.strictEqual(JSON.stringify(slicedInput), slicedBefore, "slicing must not mutate the frozen payload");
assert.throws(() => tables.sliceComparisonDataset(slicedInput, 1, 3), /Invalid comparison table/);

for (const mutate of [
  data => { data.presentation.rows[0].cells[1] = { point_id: "point-a", anchor_id: "point-a-cell-value" }; },
  data => { data.presentation.rows[0].cells[1].point_id = "unknown"; },
  data => { data.presentation.rows[0].cells[1].anchor_id = "wrong"; },
  data => { data.presentation.rows[0].cells.pop(); }
]) {
  const invalid = fixture();
  mutate(invalid);
  assert.throws(() => tables.buildComparisonTableModel(invalid), /Invalid comparison table/);
}

console.log("report reader tables contract: 16 checks");
