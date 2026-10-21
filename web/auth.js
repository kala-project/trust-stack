"use strict";

const loginForm = document.querySelector("#login-form");
const initializeForm = document.querySelector("#initialize-form");
const errorBox = document.querySelector("#auth-error");

function authenticatedReturnUrl() {
  const raw = location.hash.replace(/^#/, "");
  if (/%(?![0-9a-f]{2})/iu.test(raw) || raw.includes("\uFFFD")) return "/";
  const [path, query = ""] = raw.split("?", 2);
  const input = new URLSearchParams(query), output = new URLSearchParams();
  const entries = [...input.keys()];
  if (entries.some((key, index) => entries.indexOf(key) !== index)) return "/";
  const bounded = (key, maximum = 200) => {
    const item = (input.get(key) || "").trim();
    return item && item.length <= maximum && !/[\u0000-\u001f\u007f\uFFFD]/u.test(item) ? item : "";
  };
  if (path === "/report-reader") {
    if (entries.some(key => !["report_id", "conversation_id", "message_id", "page", "reading"].includes(key))) return "/";
    const report = bounded("report_id");
    const conversation = bounded("conversation_id");
    const message = bounded("message_id");
    if (!((report && !conversation && !message) || (!report && conversation && message))) return "/";
    if (report) output.set("report_id", report);
    else {
      output.set("conversation_id", conversation);
      output.set("message_id", message);
    }
    const page = input.get("page");
    if (page !== null) {
      if (!/^[0-9]+$/u.test(page) || Number(page) < 1 || Number(page) > 10000) return "/";
      output.set("page", String(Number(page)));
    }
    const reading = input.get("reading");
    if (reading !== null) {
      if (!["single", "two", "all"].includes(reading)) return "/";
      output.set("reading", reading);
    }
    return `/report-reader.html?${output}`;
  }
  if (path !== "/research" || entries.some(key => !["task", "conv", "message", "tab"].includes(key))) return "/";
  for (const key of ["task", "conv", "message"]) {
    const item = bounded(key);
    if (item) output.set(key, item);
  }
  const tab = input.get("tab");
  if (["answer", "history"].includes(tab)) output.set("tab", tab);
  const encoded = output.toString();
  return `/#/research${encoded ? `?${encoded}` : ""}`;
}

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
  errorBox.focus();
}

async function send(path, body) {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!response.ok) {
    if (response.status === 429) throw new Error("Too many attempts. Try again later.");
    throw new Error("The operation could not be completed. Check your input and try again.");
  }
}

function busy(form, value) {
  form.querySelectorAll("input, button").forEach((control) => { control.disabled = value; });
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.hidden = true;
  busy(loginForm, true);
  try {
    await send("/api/auth/login", { password: document.querySelector("#password").value });
    document.querySelector("#password").value = "";
    location.replace(authenticatedReturnUrl());
  } catch (error) {
    showError(error.message);
    busy(loginForm, false);
    document.querySelector("#password").focus();
  }
});

initializeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  errorBox.hidden = true;
  const password = document.querySelector("#new-password").value;
  if (password !== document.querySelector("#confirm-password").value) {
    showError("The passwords do not match.");
    return;
  }
  busy(initializeForm, true);
  try {
    await send("/api/auth/initialize", { bootstrap: document.querySelector("#bootstrap").value, password });
    initializeForm.reset();
    initializeForm.hidden = true;
    loginForm.hidden = false;
    document.querySelector("#password").focus();
  } catch (error) {
    showError(error.message);
    busy(initializeForm, false);
  }
});

fetch("/api/auth/status", { credentials: "same-origin", headers: { Accept: "application/json" } })
  .then((response) => response.ok ? response.json() : Promise.reject())
  .then((status) => {
    if (status.authenticated) location.replace(authenticatedReturnUrl());
    if (!status.initialized) {
      loginForm.hidden = true;
      initializeForm.hidden = false;
      document.querySelector("#bootstrap").focus();
    }
  })
  .catch(() => showError("Unable to verify sign-in status. Try again later."));
