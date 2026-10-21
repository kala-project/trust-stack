import { createRoot } from "react-dom/client";
import { useEffect, useState } from "react";
import { AppShell } from "./components/AppShell";
import { ResearchNavigation, readNavigationSearch } from "./components/ResearchNavigation";
import { createResearchApi } from "./data/api";
import type { ResearchTask } from "./data/types";
import "./styles/base.css";
import styles from "./styles/readerShell.module.css";

type ReaderMountOptions = {
  callbacks?: { onReady?: (payload: unknown) => void; onError?: (error: Error) => void; onDispose?: () => void };
};
type ReaderLifecycle = { update(options: { payload: unknown }): unknown; dispose(): void };
type ReaderRenderer = { mount(options?: ReaderMountOptions): ReaderLifecycle };

declare global {
  interface Window {
    ReportReaderRenderer?: ReaderRenderer;
    __REPORT_READER_MANUAL_LIFECYCLE__?: boolean;
  }
}

const legacyScripts = [
  "/research-navigation.js",
  "/report-reader-bindings.js",
  "/report-reader-tables.js",
  "/report-reader-evidence.js",
  "/report-continuation.js",
  "/report-reader.js",
];

const scriptLoads = new Map<string, Promise<void>>();

function loadScript(src: string) {
  const existing = scriptLoads.get(src);
  if (existing) return existing;
  const pending = new Promise<void>((resolve, reject) => {
    const script = document.createElement("script");
    script.src = src;
    script.dataset.readerResource = "true";
    script.onload = () => resolve();
    script.onerror = () => reject(new Error(`Unable to load ${src}`));
    document.body.append(script);
  });
  scriptLoads.set(src, pending);
  return pending;
}

function ReaderApp() {
  const [tasks, setTasks] = useState<ResearchTask[]>([]);
  const [search, setSearch] = useState(readNavigationSearch);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    const controller = new AbortController();
    createResearchApi().listTasks(false, controller.signal).then(items => {
      if (!controller.signal.aborted) { setTasks(items); setLoading(false); }
    }).catch(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    let active = true;
    let lifecycle: ReaderLifecycle | undefined;
    window.__REPORT_READER_MANUAL_LIFECYCLE__ = true;
    legacyScripts.reduce((ready, src) => ready.then(() => active ? loadScript(src) : undefined), Promise.resolve())
      .then(() => {
        if (!active) return;
        if (!window.ReportReaderRenderer) throw new Error("Reader renderer did not register its lifecycle API");
        lifecycle = window.ReportReaderRenderer.mount();
      })
      .catch(() => {
        if (!active) return;
        const title = document.getElementById("reader-title");
        const root = document.getElementById("reader-root");
        if (title) title.textContent = "Unable to open report";
        if (root) root.textContent = "Reader resources are temporarily unavailable. Refresh and try again.";
      });
    return () => {
      active = false;
      lifecycle?.dispose();
    };
  }, []);

  const sidebar = <ResearchNavigation tasks={tasks} loading={loading} search={search} onSearch={setSearch}
    currentReport={new URLSearchParams(location.search).get("report_id") ?? undefined} fromReader />;

  return <>
    <AppShell sidebar={sidebar} sidebarLabel="Report navigation" sectionTitle="Report Reader"
      shellClassName="reader-shell" mainClassName={`${styles.readerMain} reader-main report-page`}>
      <header className="reader-header report-header">
        <div className="reader-kicker breadcrumb">Research Report / Generated</div>
        <h1 id="reader-title">Loading report</h1>
        <div className="report-meta-line">
          <span id="reader-meta" className="reader-meta"></span>
          <div className="reader-header-actions header-actions">
            <button id="reader-continuation-button" className="reader-continuation-button" type="button" aria-haspopup="dialog" aria-controls="evidence-panel" hidden>Continue research</button>
            <button id="reader-toc-button" className="reader-toc-button" type="button" aria-haspopup="dialog" aria-controls="evidence-panel">Contents</button>
            <button id="reader-info-button" className="reader-info-button" type="button">Report information</button>
          </div>
        </div>
        <section id="reader-versions" className="reader-versions" aria-label="Report versions and corrections" aria-live="polite" hidden></section>
      </header>
      <nav id="reader-toc-list" className={`${styles.tocSource} report-toc`} aria-label="Report contents data"></nav>
      <article id="reader-root" className="reader-document report-body" aria-live="polite"></article>
      <nav id="reader-controls" className="reader-controls reader-bottom-bar" aria-label="Reading and pagination" hidden>
        <div className="view-toggle" role="group" aria-label="Reading layout">
          <button type="button" data-reading-mode="single">Single page</button>
          <button type="button" data-reading-mode="two">Two pages</button>
        </div>
        <div className="page-controls" role="group" aria-label="Pagination">
          <button id="reader-prev" type="button">Previous page</button>
          <span id="reader-page-indicator" aria-live="polite"></span>
          <label className="goto-page">Go to <input id="reader-goto" type="number" min="1" inputMode="numeric" aria-label="Go to page" /></label>
          <button id="reader-next" type="button">Next page</button>
        </div>
      </nav>
    </AppShell>
    <button id="evidence-backdrop" className="evidence-backdrop modal-backdrop" type="button" aria-label="Close dialog" hidden></button>
    <section id="evidence-panel" className="evidence-panel evidence-dialog" role="dialog" aria-modal="true" aria-labelledby="evidence-title" tabIndex={-1} hidden>
      <header className="modal-head">
        <div><p className="kicker reader-kicker">Evidence review</p><h2 id="evidence-title">Reviewing evidence</h2></div>
        <button id="evidence-close" type="button" aria-label="Close evidence review">Close</button>
      </header>
      <div id="evidence-body"></div>
    </section>
  </>;
}

createRoot(document.getElementById("root")!).render(<ReaderApp />);
