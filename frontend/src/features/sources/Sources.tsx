import { useEffect, useState } from "react";
import { createResearchApi, fetchJson } from "../../data/api";
import type { DocumentCatalog } from "../../data/types";
import styles from "./Sources.module.css";

const api = createResearchApi();
interface Company { id: string; name: string }
function publicUrl(raw: string) {
  try { const url = new URL(raw); return ["http:", "https:"].includes(url.protocol) ? url.href : null; } catch { return null; }
}

export function Sources() {
  const [companies, setCompanies] = useState<Company[]>([]);
  const [selected, setSelected] = useState("");
  const [catalog, setCatalog] = useState<DocumentCatalog | null>(null);
  const [error, setError] = useState("");
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    fetchJson<{companies: Company[]}>("/api/overview", { signal: controller.signal }).then(result => {
      if (!controller.signal.aborted) { setCompanies(result.companies); setSelected(previous => previous || result.companies[0]?.id || ""); }
    }).catch(reason => { if (!controller.signal.aborted) setError(reason.message); });
    return () => controller.abort();
  }, [revision]);
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController(); setCatalog(null); setError("");
    api.getDocumentCatalog(selected, controller.signal).then(result => {
      if (!controller.signal.aborted) setCatalog(result);
    }).catch(reason => { if (!controller.signal.aborted) setError(reason.message); });
    return () => controller.abort();
  }, [selected, revision]);
  return <section className={styles.page}>
    <h1>Sources</h1><p className={styles.description}>View locally archived originals. Retrieving and parsing a document does not mean every value in it has been verified.</p>
    <p><a href="/legacy.html#/sources">Import and manage sources</a></p>
    <div className={styles.filter}><label htmlFor="source-company">Company</label><select id="source-company" value={selected} onChange={event => setSelected(event.target.value)}>{companies.map(company => <option key={company.id} value={company.id}>{company.name} · {company.id}</option>)}</select><button onClick={() => setRevision(value => value + 1)}>Reload</button></div>
    {error ? <p role="alert">{error}</p> : !catalog ? <p role="status">Loading source catalog…</p> : <>
      <p className={styles.coverage}>{catalog.enrolled} archived · {catalog.parsed} parsed{catalog.failed ? ` · ${catalog.failed} failed to parse` : ""}{catalog.unsupported ? ` · ${catalog.unsupported} currently unsupported` : ""}</p>
      <p className={styles.description}>This is the current source catalog, not the frozen source set used by a specific report.</p>
      <div className={styles.documents}>{catalog.documents.map(document => {
        const source = publicUrl(document.source_url);
        return <article key={document.document_version_id} className={styles.document}>
          <h2>{document.title}</h2><p className={styles.meta}>{document.media_type} · {document.parse_status === "parsed" ? "Parsed and searchable" : document.parse_status === "parse_failed" ? "Parsing failed" : "Currently unsupported"}{document.published_at ? ` · Published ${document.published_at}` : ""}</p>
          <details><summary>Source and version</summary><dl><dt>Provided address</dt><dd>{source ? <a href={source} target="_blank" rel="noopener noreferrer">{source}</a> : "No public address is available"}</dd><dt>Content fingerprint</dt><dd><code>{document.source_sha256}</code></dd><dt>Collected at</dt><dd>{document.collected_at}</dd><dt>Rights status</dt><dd>{document.rights_status || "Not specified"}</dd></dl>{document.parse_error && <p>{document.parse_error}</p>}</details>
        </article>;
      })}</div>
      {!catalog.documents.length && <p>This company has no local documents yet. Automatic research retrieval depends on connected sources and the years required.</p>}
    </>}
  </section>;
}
