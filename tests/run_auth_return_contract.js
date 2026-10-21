"use strict";

const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const control = { value: "", hidden: false, disabled: false, focus() {}, reset() {}, addEventListener() {}, querySelectorAll() { return []; } };
const context = {
  URLSearchParams,
  location: { hash: "", replace() {} },
  document: { querySelector() { return control; } },
  fetch() { return new Promise(() => {}); },
  console,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(path.resolve(__dirname, "../web/auth.js"), "utf8"), context);

const cases = [
  ["#/research?task=t1&conv=c1&message=m1&tab=history", "/#/research?task=t1&conv=c1&message=m1&tab=history"],
  ["#/report-reader?report_id=report-1&page=7&reading=two", "/report-reader.html?report_id=report-1&page=7&reading=two"],
  ["#/report-reader?conversation_id=conv-1&message_id=msg-1&page=2", "/report-reader.html?conversation_id=conv-1&message_id=msg-1&page=2"],
  ["#https://evil.example/report-reader?report_id=x", "/"],
  ["#//evil.example/report-reader?report_id=x", "/"],
  ["#/%2F%2Fevil.example?report_id=x", "/"],
  ["#/report-reader?report_id=x&next=https%3A%2F%2Fevil.example", "/"],
  ["#/report-reader?report_id=x&report_id=y", "/"],
  ["#/report-reader?report_id=x&page=0", "/"],
  ["#/report-reader?report_id=x&page=10001", "/"],
  ["#/report-reader?report_id=x%0Aheader", "/"],
  ["#/report-reader?report_id=x%ZZ", "/"],
  ["#/report-reader?report_id=x&conversation_id=c&message_id=m", "/"],
  ["#/research?tab=history&outside=1", "/"],
];
for (const [hash, expected] of cases) {
  context.location.hash = hash;
  const actual = context.authenticatedReturnUrl();
  if (actual !== expected) throw new Error(`${hash}: expected ${expected}, got ${actual}`);
}
console.log(`PASS auth return contract (${cases.length} checks)`);
