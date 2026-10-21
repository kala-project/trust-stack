import { useCallback, useEffect, useRef, useState } from "react";
import { createResearchApi } from "../data/api";
import { parseAppRoute, type AppRoute } from "../data/route";
import { presentTask } from "../data/research";
import type { ResearchTask } from "../data/types";
import { ResearchNavigation, readNavigationSearch } from "../components/ResearchNavigation";
import { AppShell } from "../components/AppShell";
import { Icon } from "../components/Icon";
import { PageBoundary } from "../components/PageBoundary";
import { NewResearch } from "../features/research/NewResearch";
import { ResearchWorkspace } from "../features/research/ResearchWorkspace";
import { Sources } from "../features/sources/Sources";
import { destination, safeReaderHref, taskHref } from "./links";
import styles from "./App.module.css";

const api = createResearchApi();
const defaultRoute: AppRoute = { page: "research", task: "", conversation: "", tab: "research" };
const date = (value: string) => new Date(value).toLocaleDateString("en", { month: "short", day: "numeric" });

export function App({ readOnly }: { readOnly: boolean }) {
  const [route, setRoute] = useState(() => parseAppRoute(location.hash) ?? defaultRoute);
  const [tasks, setTasks] = useState<ResearchTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  const [search, setSearch] = useState(readNavigationSearch);
  const main = useRef<HTMLElement>(null);
  const refresh = useCallback(() => setRevision(value => value + 1), []);
  const onTask = useCallback((id: string) => { location.hash = taskHref(id); refresh(); }, [refresh]);
  const onReport = useCallback((href: string) => {
    const safe = safeReaderHref(href);
    if (safe) location.assign(safe);
    else setError("The report address could not be verified, so the link was not opened. Refresh the research record and try again.");
  }, []);

  useEffect(() => {
    const change = () => setRoute(parseAppRoute(location.hash) ?? defaultRoute);
    window.addEventListener("hashchange", change);
    return () => window.removeEventListener("hashchange", change);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError("");
    api.listTasks(false, controller.signal).then(items => {
      if (!controller.signal.aborted) { setTasks(items); setLoading(false); }
    }).catch(reason => { if (!controller.signal.aborted) { setError(reason instanceof Error ? reason.message : "The research list is temporarily unavailable"); setLoading(false); } });
    return () => controller.abort();
  }, [revision]);

  useEffect(() => {
    const scroll = main.current;
    if (!scroll) return;
    const key = `workbench-position:${location.hash}`;
    try { scroll.scrollTop = Number(sessionStorage.getItem(key) || 0); } catch {}
    return () => { try { sessionStorage.setItem(key, String(scroll.scrollTop)); } catch {} };
  }, [route]);

  const isTask = route.page === "research" && Boolean(route.task || route.conversation);
  const isHistory = route.page === "research" && route.tab === "history" && !isTask;
  const filtered = tasks.filter(task => `${task.question} ${(task.subjects ?? []).map(item => `${item.id} ${item.label}`).join(" ")}`.toLowerCase().includes(search.toLowerCase()));

  const sidebar = <ResearchNavigation tasks={tasks} loading={loading} search={search} onSearch={setSearch}
    currentTask={route.page === "research" ? route.task : undefined} sources={route.page === "sources"} readOnly={readOnly} />;

  return <AppShell sidebar={sidebar} sidebarLabel="Research navigation" mainRef={main} mainClassName={styles.main}
    sectionTitle={route.page === "sources" ? "Sources" : isTask ? "Current Research" : isHistory ? "My Research" : "New Research"}
    toolbar={<button className="icon-button" aria-label="Refresh research list" onClick={refresh}><Icon name="refresh" /></button>}>
    {error && <div className={styles.error} role="alert"><p>{error}</p><button onClick={refresh}>Reload</button></div>}
    <PageBoundary key={JSON.stringify(route)}>
    {route.page === "sources" ? <Sources /> : isTask ? <ResearchWorkspace key={`${route.task}/${route.conversation}`} taskId={route.task || undefined} conversationId={route.conversation || undefined} readOnly={readOnly} onReport={onReport} onChanged={refresh} /> : isHistory ? <section className={styles.history}>
      <h1>My Research</h1><p className={styles.description}>Reports and research records, ordered by creation date.</p>
      {loading && <p role="status">Loading research…</p>}
      {filtered.map(task => <article className={styles.historyRow} key={task.task_id}>
        <div className={styles.historyMeta}><span data-tone={presentTask(task).tone}>{presentTask(task).label}</span><time dateTime={task.created_at}>{date(task.created_at)}</time></div>
        <h2><a href={destination(task)}>{task.reader_title || task.question}</a></h2>
        <p className={styles.subjects}>{(task.subjects ?? []).map(subject => subject.label).join(" · ")}{task.period_text ? ` · ${task.period_text}` : ""}</p>
        {task.answer_summary && <p className={styles.summary}>{task.answer_summary}</p>}
      </article>)}
      {!loading && !filtered.length && <p>No research matches yet. You can start a new question.</p>}
    </section> : <NewResearch readOnly={readOnly} onTask={onTask} />}
    </PageBoundary>
  </AppShell>;
}
