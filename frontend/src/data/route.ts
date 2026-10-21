export type ResearchTab = "research" | "history" | "answer";

export interface ResearchRoute {
  page: "research";
  task: string;
  conversation: string;
  tab: ResearchTab;
}

export interface SourcesRoute { page: "sources" }
export type AppRoute = ResearchRoute | SourcesRoute;

const ALLOWED_TABS = new Set<ResearchTab>(["research", "history", "answer"]);

function clean(value: string | null, maximum: number): string {
  if (typeof value !== "string") return "";
  const text = value.trim();
  return text && text.length <= maximum && !/[\u0000-\u001f]/u.test(text) ? text : "";
}

/** Parses only the legacy-compatible owned hashes; unrelated hashes return null. */
export function parseAppRoute(hash: string): AppRoute | null {
  const raw = typeof hash === "string" ? hash.replace(/^#/, "") : "";
  if (raw === "/sources") return { page: "sources" };
  const split = raw.indexOf("?");
  const path = split < 0 ? raw : raw.slice(0, split);
  if (path !== "/research") return null;
  const params = new URLSearchParams(split < 0 ? "" : raw.slice(split + 1));
  const candidate = clean(params.get("tab"), 20) as ResearchTab;
  return {
    page: "research",
    task: clean(params.get("task"), 200),
    conversation: clean(params.get("conv"), 200),
    tab: ALLOWED_TABS.has(candidate) ? candidate : "research",
  };
}

/** URLSearchParams owns encoding, so task and conversation identities are never interpolated raw. */
export function formatAppRoute(route: AppRoute): string {
  if (route.page === "sources") return "#/sources";
  const params = new URLSearchParams();
  const task = clean(route.task, 200);
  const conversation = clean(route.conversation, 200);
  const tab = ALLOWED_TABS.has(route.tab) ? route.tab : "research";
  if (task) params.set("task", task);
  if (conversation) params.set("conv", conversation);
  if (tab !== "research") params.set("tab", tab);
  const query = params.toString();
  return `#/research${query ? `?${query}` : ""}`;
}

export function sameAppRoute(left: string, right: string): boolean {
  const parsedLeft = parseAppRoute(left);
  const parsedRight = parseAppRoute(right);
  return Boolean(parsedLeft && parsedRight
    && formatAppRoute(parsedLeft) === formatAppRoute(parsedRight));
}
