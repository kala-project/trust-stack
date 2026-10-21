"use strict";

const path = require("node:path");

class ClassList {
  constructor(element) { this.element = element; }
  values() { return (this.element.className || "").split(/\s+/).filter(Boolean); }
  contains(name) { return this.values().includes(name); }
  add(name) { if (!this.contains(name)) this.element.className = this.values().concat(name).join(" "); }
  remove(name) { this.element.className = this.values().filter(value => value !== name).join(" "); }
  toggle(name, force) {
    const enabled = force === undefined ? !this.contains(name) : Boolean(force);
    if (enabled) this.add(name); else this.remove(name);
    return enabled;
  }
}

function splitSelector(selector) {
  return selector.trim().split(/\s+/);
}

function matchesSimple(element, selector) {
  if (!(element instanceof Element)) return false;
  const attrMatches = Array.from(selector.matchAll(/\[([^\]^=]+)(?:([\^]?=)['"]?([^\]'" ]*)['"]?)?\]/g));
  let base = selector.replace(/\[[^\]]+\]/g, "");
  const tag = (base.match(/^[a-zA-Z][\w-]*/) || [""])[0];
  if (tag && element.tagName !== tag.toUpperCase()) return false;
  const classes = Array.from(base.matchAll(/\.([\w-]+)/g), match => match[1]);
  if (classes.some(name => !element.classList.contains(name))) return false;
  for (const match of attrMatches) {
    const actual = element.getAttribute(match[1]);
    if (!match[2] && actual === null) return false;
    if (match[2] === "=" && actual !== match[3]) return false;
    if (match[2] === "^=" && !(actual || "").startsWith(match[3])) return false;
  }
  return true;
}

function matchesSelector(element, selector) {
  const parts = splitSelector(selector);
  let current = element;
  if (!matchesSimple(current, parts.pop())) return false;
  while (parts.length) {
    const wanted = parts.pop();
    current = current.parentNode;
    while (current && !matchesSimple(current, wanted)) current = current.parentNode;
    if (!current) return false;
  }
  return true;
}

class Node {
  constructor() { this.parentNode = null; this.childNodes = []; }
  appendChild(child) {
    if (child instanceof DocumentFragment) {
      child.childNodes.slice().forEach(item => this.appendChild(item));
      child.childNodes = [];
      return child;
    }
    if (child.parentNode) child.parentNode.childNodes = child.parentNode.childNodes.filter(item => item !== child);
    child.parentNode = this;
    this.childNodes.push(child);
    return child;
  }
  replaceChildren(...children) {
    this.childNodes.forEach(child => { child.parentNode = null; });
    this.childNodes = [];
    children.forEach(child => { if (child) this.appendChild(child); });
  }
  get children() { return this.childNodes.filter(child => child instanceof Element); }
  get textContent() { return this.childNodes.map(child => child.textContent).join(""); }
  set textContent(value) { this.childNodes = [new TextNode(String(value))]; this.childNodes[0].parentNode = this; }
  querySelectorAll(selector) {
    const selectors = selector.split(",").map(value => value.trim());
    const found = [];
    function visit(parent) {
      parent.childNodes.forEach(child => {
        if (child instanceof Element) {
          if (selectors.some(item => matchesSelector(child, item))) found.push(child);
          visit(child);
        }
      });
    }
    visit(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

class TextNode extends Node {
  constructor(value) { super(); this.value = value; }
  get textContent() { return this.value; }
  set textContent(value) { this.value = String(value); }
}

class DocumentFragment extends Node {}

class Element extends Node {
  constructor(tagName, ownerDocument) {
    super();
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = ownerDocument;
    this.className = "";
    this.attributes = new Map();
    this.listeners = new Map();
    this.classList = new ClassList(this);
  }
  setAttribute(name, value) {
    const stringValue = String(value);
    this.attributes.set(name, stringValue);
    if (name === "class") this.className = stringValue;
  }
  getAttribute(name) { return name === "class" ? (this.className || null) : (this.attributes.has(name) ? this.attributes.get(name) : null); }
  removeAttribute(name) { this.attributes.delete(name); if (name === "class") this.className = ""; }
  addEventListener(type, listener) { const listeners = this.listeners.get(type) || []; listeners.push(listener); this.listeners.set(type, listeners); }
  click() { (this.listeners.get("click") || []).forEach(listener => listener({ target: this })); }
  focus() { this.ownerDocument.activeElement = this; }
  get dataset() {
    const output = {};
    this.attributes.forEach((value, name) => {
      if (name.startsWith("data-")) output[name.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = value;
    });
    return output;
  }
  get href() {
    const href = this.getAttribute("href");
    return href === null ? "" : new URL(href, this.ownerDocument.baseURI).href;
  }
}

class Document extends Node {
  constructor() { super(); this.baseURI = "http://127.0.0.1:9999/tests/contract.html"; this.activeElement = null; }
  createElement(tag) { return new Element(tag, this); }
  createDocumentFragment() { return new DocumentFragment(); }
  getElementById(id) { return this.querySelector("[id='" + id + "']"); }
}

const document = new Document();
const fixture = document.createElement("main");
fixture.setAttribute("id", "fixture");
document.appendChild(fixture);
global.document = document;
global.window = global;

require(path.resolve(__dirname, "../web/report-reader-evidence.js"));
require(path.resolve(__dirname, "report_reader_evidence_contract.js"));

if (!global.__evidenceContractResult || !global.__evidenceContractResult.ok) {
  console.error(global.__evidenceContractResult && global.__evidenceContractResult.error || "contract did not complete");
  process.exit(1);
}
console.log("PASS report-reader-evidence contract (" + global.__evidenceContractResult.checks.length + " checks)");
