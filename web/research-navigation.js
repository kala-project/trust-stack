"use strict";

(function exposeResearchNavigation(root) {
  const ROUTE = "/research";
  const ALLOWED_TABS = new Set(["research", "answer", "history"]);

  function clean(value, maximum = 240) {
    if (typeof value !== "string") return "";
    const text = value.trim();
    return text && text.length <= maximum && !/[\u0000-\u001f]/u.test(text) ? text : "";
  }

  function parse(hash) {
    const raw = typeof hash === "string" ? hash.replace(/^#/, "") : "";
    const split = raw.indexOf("?");
    const path = split < 0 ? raw : raw.slice(0, split);
    if (path !== ROUTE) return null;
    const params = new URLSearchParams(split < 0 ? "" : raw.slice(split + 1));
    const tabCandidate = clean(params.get("tab") || "", 20);
    return {
      task: clean(params.get("task") || "", 200),
      conversation: clean(params.get("conv") || "", 200),
      message: clean(params.get("message") || "", 200),
      tab: ALLOWED_TABS.has(tabCandidate) ? tabCandidate : "research"
    };
  }

  function format(route = {}) {
    const params = new URLSearchParams();
    const task = clean(route.task || "", 200);
    const conversation = clean(route.conversation || "", 200);
    const message = clean(route.message || "", 200);
    const tab = ALLOWED_TABS.has(route.tab) ? route.tab : "research";
    if (task) params.set("task", task);
    if (conversation) params.set("conv", conversation);
    if (message) params.set("message", message);
    if (tab !== "research") params.set("tab", tab);
    const query = params.toString();
    return `#${ROUTE}${query ? `?${query}` : ""}`;
  }

  function same(left, right) {
    const parsedLeft = parse(left), parsedRight = parse(right);
    return Boolean(parsedLeft && parsedRight && format(parsedLeft) === format(parsedRight));
  }

  function ownerMatches(owner, current) {
    return Boolean(owner && current && owner.sequence === current.sequence && owner.navigation === current.navigation && owner.task === current.task);
  }

  root.ResearchNavigation = Object.freeze({ parse, format, same, ownerMatches });
})(typeof window === "undefined" ? globalThis : window);
