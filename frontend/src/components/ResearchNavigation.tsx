import { useLayoutEffect, useRef } from "react";
import type { ResearchTask } from "../data/types";
import { presentTask } from "../data/research";
import { destination } from "../app/links";
import { Icon } from "./Icon";
import styles from "../app/App.module.css";

const SEARCH_KEY = "research-navigation-search/1";
const SCROLL_KEY = "research-navigation-scroll/1";
export function readNavigationSearch() {
  try { return sessionStorage.getItem(SEARCH_KEY) ?? ""; } catch { return ""; }
}

export function ResearchNavigation({tasks, loading, search, onSearch, currentTask, currentReport, sources, readOnly = false, fromReader = false}: {
  tasks: ResearchTask[]; loading: boolean; search: string; onSearch: (value:string)=>void;
  currentTask?: string; currentReport?: string; sources?: boolean; readOnly?: boolean; fromReader?: boolean;
}) {
  const list = useRef<HTMLElement>(null);
  const restored = useRef(false);
  useLayoutEffect(() => {
    if (loading || restored.current || !list.current) return;
    try { list.current.scrollTop = Number(sessionStorage.getItem(SCROLL_KEY) || 0) || 0; } catch {}
    restored.current = true;
  }, [loading]);
  function changeSearch(value: string) {
    try { sessionStorage.setItem(SEARCH_KEY, value); } catch {}
    if (list.current) list.current.scrollTop = 0;
    onSearch(value);
  }
  const prefix = fromReader ? "/" : "";
  const filtered = tasks.filter(task => `${task.question} ${(task.subjects ?? []).map(item => `${item.id} ${item.label}`).join(" ")}`.toLowerCase().includes(search.toLowerCase()));
  return <>
    <a className={styles.newResearch} href={`${prefix}#/research`}><Icon name="plus" />New Research</a>
    <label className={styles.search}><Icon name="search" /><span className="sr-only">Search research</span><input value={search} onChange={event => changeSearch(event.target.value)} placeholder="Search research or companies" /></label>
    <div className={styles.listHeading}><a href={`${prefix}#/research?tab=history`}>My Research</a><span>{loading ? "Loading" : filtered.length}</span></div>
    <nav ref={list} className={styles.taskList} aria-label="My Research" onScroll={event => {
      if (restored.current) try { sessionStorage.setItem(SCROLL_KEY, String(event.currentTarget.scrollTop)); } catch {}
    }}>{filtered.map(task => {
      const status = presentTask(task), target = destination(task);
      const selected = currentTask === task.task_id || Boolean(currentReport && (task.reader_report_id === currentReport || task.latest_report?.reader_report_id === currentReport));
      return <a key={task.task_id} href={target.startsWith("#") ? prefix + target : target} className={styles.task} aria-current={selected ? "page" : undefined}>
        <span className={styles.taskTitle}>{task.reader_title || task.question}</span>
        <span className={styles.taskMeta}><span data-tone={status.tone}>{status.label}</span><time dateTime={task.created_at}>{new Date(task.created_at).toLocaleDateString("en",{month:"short",day:"numeric"})}</time></span>
      </a>;
    })}{!loading && !filtered.length && <p className={styles.listEmpty}>{search ? "No matching research." : "No research records yet."}</p>}</nav>
    <a className={styles.libraryLink} href={`${prefix}#/sources`} aria-current={sources ? "page" : undefined}><Icon name="library" />Sources</a>
    {readOnly && <p className={styles.preview}>Read-only candidate · Existing research is unaffected</p>}
  </>;
}
