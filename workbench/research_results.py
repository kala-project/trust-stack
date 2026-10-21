"""Immutable, editable research-note drafts derived from completed chat answers."""

from __future__ import annotations

from contextlib import closing
import csv
from html import escape
import io
import json
import sqlite3
import uuid
from typing import Any

from .answer_artifacts import AnswerArtifactService
from .chat import ChatError, ChatService, _json, _now


MAX_TITLE_CHARS = 300
MAX_BODY_CHARS = 50_000
MAX_LIMITATIONS = 50
MAX_LIMITATION_CHARS = 1_000

_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS research_result_versions (
 result_id TEXT NOT NULL, version INTEGER NOT NULL,
 conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
 message_id TEXT NOT NULL REFERENCES messages(message_id), company_id TEXT NOT NULL,
 title TEXT NOT NULL, body TEXT NOT NULL, limitations_json TEXT NOT NULL,
 question TEXT NOT NULL, citations_json TEXT NOT NULL, source_snapshot_json TEXT NOT NULL,
 artifact_json TEXT, artifact_gap_json TEXT,
 created_at TEXT NOT NULL,
 PRIMARY KEY(result_id,version)
);
CREATE INDEX IF NOT EXISTS research_results_conversation_idx
 ON research_result_versions(conversation_id,result_id,version);
CREATE TRIGGER IF NOT EXISTS research_result_versions_no_update
 BEFORE UPDATE ON research_result_versions
 BEGIN
  SELECT RAISE(ABORT, 'research result versions are append-only');
 END;
CREATE TRIGGER IF NOT EXISTS research_result_versions_no_delete
 BEFORE DELETE ON research_result_versions
 BEGIN
  SELECT RAISE(ABORT, 'research result versions are append-only');
 END;
"""
_WARNING = "Edits are not semantically reviewed or revalidated; references_checked only confirms that original answer references existed in authoritative tool results."


def _text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ChatError("invalid_research_result", f"{name} must be a string")
    value = value.strip()
    if not value or len(value) > maximum:
        raise ChatError("invalid_research_result", f"{name} must contain 1..{maximum} characters", 413 if len(value) > maximum else 400)
    return value


def _limitations(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_LIMITATIONS:
        raise ChatError("invalid_research_result", f"limitations must be an array of at most {MAX_LIMITATIONS} strings")
    result = []
    for item in value:
        if not isinstance(item, str) or len(item.strip()) > MAX_LIMITATION_CHARS:
            raise ChatError("invalid_research_result", f"each limitation must be a string of at most {MAX_LIMITATION_CHARS} characters")
        if item.strip():
            result.append(item.strip())
    return result


class ResearchResultsService:
    """Stores append-only versions in the existing chat database."""

    def __init__(self, chat: ChatService, artifact_service=None):
        self.chat = chat
        self.artifacts = artifact_service or AnswerArtifactService(chat)
        with closing(self.chat._connect()) as connection:
            connection.executescript(_SCHEMA)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(research_result_versions)")}
            if "artifact_json" not in columns:
                connection.execute("ALTER TABLE research_result_versions ADD COLUMN artifact_json TEXT")
            if "artifact_gap_json" not in columns:
                connection.execute("ALTER TABLE research_result_versions ADD COLUMN artifact_gap_json TEXT")

    @staticmethod
    def _artifact_fields(row: sqlite3.Row) -> tuple[dict | None, dict | None]:
        artifact = json.loads(row["artifact_json"]) if row["artifact_json"] is not None else None
        gap = json.loads(row["artifact_gap_json"]) if row["artifact_gap_json"] is not None else None
        if artifact is None and gap is None:
            gap = {
                "status": "unavailable", "reason": "not_captured",
                "detail": "This version predates frozen answer artifacts.",
            }
        return artifact, gap

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        citations = json.loads(row["citations_json"])
        references_checked = bool(citations) and all(item.get("valid") is True for item in citations)
        artifact, artifact_gap = ResearchResultsService._artifact_fields(row)
        return {
            "result_id": row["result_id"], "version": row["version"],
            "conversation_id": row["conversation_id"], "message_id": row["message_id"],
            "company_id": row["company_id"], "title": row["title"], "body": row["body"],
            "limitations": json.loads(row["limitations_json"]), "question": row["question"],
            "citations": citations, "source_snapshot": json.loads(row["source_snapshot_json"]),
            "artifact": artifact, "artifact_gap": artifact_gap,
            "artifact_context": {
                "applies_to": "original_answer", "edits_revalidated": False,
                "status": "frozen" if artifact is not None else "unavailable",
            },
            "created_at": row["created_at"], "result_type": "human_draft",
            "human_review_status": "unreviewed", "publication_status": "draft",
            "machine_verification": {
                "status": "references_checked" if references_checked else "unverified",
                "scope": "original_answer_reference_existence_only",
            },
            "warning": _WARNING,
        }

    def create(self, conversation_id, message_id, title, body, limitations) -> dict:
        title = _text(title, "title", MAX_TITLE_CHARS)
        body = _text(body, "body", MAX_BODY_CHARS)
        limitations = _limitations(limitations)
        answer = self.chat.answer_context(conversation_id, message_id)
        artifact, artifact_gap = None, None
        try:
            artifact = self.artifacts.get(conversation_id, message_id)
        except Exception as exc:
            artifact_gap = {
                "status": "unavailable", "reason": "projection_failed",
                "error_code": exc.code if isinstance(exc, ChatError) else type(exc).__name__,
                "detail": "The original answer artifact could not be frozen when this version was saved.",
            }
        result_id, created_at = f"result_{uuid.uuid4().hex}", _now()
        with closing(self.chat._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO research_result_versions "
                "(result_id,version,conversation_id,message_id,company_id,title,body,limitations_json,"
                "question,citations_json,source_snapshot_json,created_at,artifact_json,artifact_gap_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (result_id, 1, answer["conversation_id"], answer["message_id"], answer["company_id"],
                 title, body, _json(limitations), answer["question"], _json(answer["citations"]),
                 _json(answer["source_snapshot"]), created_at,
                 _json(artifact) if artifact is not None else None,
                 _json(artifact_gap) if artifact_gap is not None else None),
            )
        return self.get(result_id, answer["conversation_id"], answer["company_id"], 1)

    def revise(self, result_id, conversation_id, company_id, expected_version, title, body, limitations) -> dict:
        result_id = self.chat._valid_id(result_id, "result_id")
        conversation_id = self.chat._valid_id(conversation_id, "conversation_id")
        company_id = self.chat._valid_id(company_id, "company_id")
        if type(expected_version) is not int or expected_version < 1:
            raise ChatError("invalid_result_version", "expected_version must be a positive integer")
        title = _text(title, "title", MAX_TITLE_CHARS)
        body = _text(body, "body", MAX_BODY_CHARS)
        limitations = _limitations(limitations)
        with closing(self.chat._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            latest = connection.execute(
                "SELECT * FROM research_result_versions WHERE result_id=? AND conversation_id=? AND company_id=? "
                "ORDER BY version DESC LIMIT 1",
                (result_id, conversation_id, company_id),
            ).fetchone()
            if latest is None:
                connection.rollback()
                raise ChatError("research_result_not_found", "research result does not exist", 404)
            if latest["version"] != expected_version:
                connection.rollback()
                raise ChatError("result_version_conflict", "research result has a newer version", 409)
            version = expected_version + 1
            connection.execute(
                "INSERT INTO research_result_versions "
                "(result_id,version,conversation_id,message_id,company_id,title,body,limitations_json,"
                "question,citations_json,source_snapshot_json,created_at,artifact_json,artifact_gap_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (result_id, version, latest["conversation_id"], latest["message_id"], latest["company_id"],
                 title, body, _json(limitations), latest["question"], latest["citations_json"],
                 latest["source_snapshot_json"], _now(), latest["artifact_json"],
                 latest["artifact_gap_json"]),
            )
            connection.commit()
        return self.get(result_id, conversation_id, company_id, version)

    def list(self, conversation_id) -> dict:
        conversation_id = self.chat._valid_id(conversation_id, "conversation_id")
        with closing(self.chat._connect()) as connection:
            exists = connection.execute("SELECT 1 FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            if exists is None:
                raise ChatError("conversation_not_found", "conversation does not exist", 404)
            rows = connection.execute(
                "SELECT v.* FROM research_result_versions v JOIN "
                "(SELECT result_id,max(version) version FROM research_result_versions WHERE conversation_id=? GROUP BY result_id) latest "
                "ON latest.result_id=v.result_id AND latest.version=v.version ORDER BY v.created_at DESC,v.result_id",
                (conversation_id,),
            ).fetchall()
            return {"conversation_id": conversation_id, "results": [self._row(row) for row in rows]}

    def get(self, result_id, conversation_id, company_id, version=None) -> dict:
        result_id = self.chat._valid_id(result_id, "result_id")
        conversation_id = self.chat._valid_id(conversation_id, "conversation_id")
        company_id = self.chat._valid_id(company_id, "company_id")
        if version is not None and (type(version) is not int or version < 1):
            raise ChatError("invalid_result_version", "version must be a positive integer")
        with closing(self.chat._connect()) as connection:
            if version is None:
                row = connection.execute(
                    "SELECT * FROM research_result_versions WHERE result_id=? AND conversation_id=? AND company_id=? "
                    "ORDER BY version DESC LIMIT 1", (result_id, conversation_id, company_id),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM research_result_versions WHERE result_id=? AND conversation_id=? AND company_id=? "
                    "AND version=?", (result_id, conversation_id, company_id, version),
                ).fetchone()
        if row is None:
            raise ChatError("research_result_not_found", "research result version does not exist", 404)
        return self._row(row)

    @staticmethod
    def _csv_safe(value: Any) -> str:
        text = "" if value is None else str(value)
        if text.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + text
        return text

    @staticmethod
    def _reference_title(citation: dict, snapshot: dict) -> str | None:
        """Find a display title in the frozen snapshot without consulting live sources."""
        evidence_snapshot = snapshot.get("evidence") if isinstance(snapshot, dict) else None
        if not isinstance(evidence_snapshot, dict):
            evidence_snapshot = {}
        sources = {
            item.get("source_id"): item for item in evidence_snapshot.get("sources", [])
            if isinstance(item, dict) and item.get("source_id")
        }
        evidence = {
            item.get("evidence_id"): item for item in evidence_snapshot.get("evidence", [])
            if isinstance(item, dict) and item.get("evidence_id")
        }

        source_ids = []
        if citation.get("kind") == "evidence":
            source_ids.append(evidence.get(citation.get("id"), {}).get("source_id"))
        elif citation.get("kind") == "claim":
            claim = next((item for item in evidence_snapshot.get("claims", [])
                          if isinstance(item, dict) and item.get("claim_id") == citation.get("id")), {})
            source_ids.extend(evidence.get(identifier, {}).get("source_id")
                              for identifier in claim.get("evidence_ids", []))
        for source_id in source_ids:
            title = sources.get(source_id, {}).get("title")
            if isinstance(title, str) and title.strip():
                return title.strip()

        # Future document snapshots may include descriptive records. Use such a
        # title only when the frozen record carries the cited version identifier.
        expected_document = citation.get("document_version_id")
        if expected_document:
            def find_title(value: Any) -> str | None:
                if isinstance(value, dict):
                    if value.get("document_version_id") == expected_document:
                        title = value.get("title")
                        if isinstance(title, str) and title.strip():
                            return title.strip()
                    for child in value.values():
                        found = find_title(child)
                        if found:
                            return found
                elif isinstance(value, list):
                    for child in value:
                        found = find_title(child)
                        if found:
                            return found
                return None

            return find_title(snapshot.get("documents", {}))
        return None

    def export(self, result_id, conversation_id, company_id, version, format_name) -> tuple[str, str, str]:
        if format_name not in {"html", "json", "csv"}:
            raise ChatError("invalid_export_format", "format must be html, json, or csv")
        result = self.get(result_id, conversation_id, company_id, version)
        filename = f'{result["result_id"]}-v{result["version"]}.{format_name}'
        if format_name == "json":
            return json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), "application/json", filename
        if format_name == "html":
            e = lambda value: escape(str(value if value is not None else ""), quote=True).replace("=", "&#x3D;")
            limitations = ("".join(f"<li>{e(item)}</li>" for item in result["limitations"])
                           or "<li>No additional limitations recorded.</li>")
            refs = "".join(
                "<li><span class=\"reference-type\">" + e(item.get("kind") or "unknown") +
                "</span><span>" + e(self._reference_title(item, result["source_snapshot"])
                                      or "The frozen snapshot did not provide a readable title") + "</span></li>"
                for item in result["citations"]
            ) or "<li>The original answer has no citations.</li>"
            technical = {
                "result_id": result["result_id"],
                "conversation_id": result["conversation_id"],
                "message_id": result["message_id"],
                "result_type": result["result_type"],
                "human_review_status": result["human_review_status"],
                "publication_status": result["publication_status"],
                "machine_verification": result["machine_verification"],
                "warning": result["warning"],
                "citations": result["citations"],
                "source_snapshot": result["source_snapshot"],
                "artifact": result["artifact"],
                "artifact_gap": result["artifact_gap"],
                "artifact_context": result["artifact_context"],
            }
            artifact = result["artifact"]
            artifact_html = ""
            if artifact is not None:
                datasets = "".join(
                    "<article><h3>" + e(item.get("id") or "Datasets") + "</h3>"
                    "<p>Charts: " + e(json.dumps(item.get("chart"), ensure_ascii=False, sort_keys=True)) + "</p>"
                    "<pre>" + e(json.dumps(item.get("rows") or item.get("raw_rows") or [],
                                                  ensure_ascii=False, sort_keys=True, indent=2)) + "</pre></article>"
                    for item in artifact.get("datasets", []) if isinstance(item, dict)
                ) or "<p>No frozen chart data.</p>"
                traces = "".join(
                    "<article><h3>" + e(item.get("id") or "Lineage records") + "</h3><pre>" +
                    e(json.dumps({"reference": item.get("reference"), "status": item.get("status"),
                                  "source": item.get("source"), "steps": item.get("steps")},
                                 ensure_ascii=False, sort_keys=True, indent=2)) + "</pre></article>"
                    for item in artifact.get("traces", []) if isinstance(item, dict)
                ) or "<p>No frozen lineage records.</p>"
                artifact_html = (
                    "<section><h2>Original answer data and charts</h2>" + datasets + "</section>"
                    "<section><h2>Original answer evidence lineage</h2>" + traces + "</section>"
                )
            else:
                artifact_html = (
                    "<section><h2>Original answer artifact gaps</h2><pre>" +
                    e(json.dumps(result["artifact_gap"], ensure_ascii=False, sort_keys=True, indent=2)) +
                    "</pre></section>"
                )
            html = (
                "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                "<title>Research result | " + e(result["title"]) + "</title><style>"
                ":root{color-scheme:light;font-family:system-ui,-apple-system,\"Segoe UI\",sans-serif;color:#172033;background:#f4f6f8}"
                "*{box-sizing:border-box}body{margin:0;padding:clamp(1rem,4vw,3rem)}"
                "main{max-width:58rem;margin:auto;background:#fff;padding:clamp(1.1rem,4vw,3rem);border-radius:.75rem;box-shadow:0 .2rem 1.2rem #17203318}"
                "h1{font-size:clamp(1.65rem,5vw,2.5rem);line-height:1.2;margin-top:0}h2{margin-top:2rem;font-size:1.2rem}"
                ".summary{display:grid;grid-template-columns:max-content 1fr;gap:.4rem 1rem;margin:1.5rem 0}.summary dt{font-weight:700}.summary dd{margin:0;overflow-wrap:anywhere}"
                ".warning{border-left:.35rem solid #b45309;background:#fff7ed;padding:.85rem 1rem}.warning h2{margin:.1rem 0 .5rem;font-size:1rem}"
                ".pre-wrap{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word;font:inherit;line-height:1.7;margin:0}"
                ".reference-type{display:inline-block;margin-right:.65rem;padding:.12rem .45rem;border-radius:.3rem;background:#e8eef7;font-family:ui-monospace,monospace;font-size:.85em}"
                "li+li{margin-top:.45rem}details{margin-top:2rem;border-top:1px solid #d8dee8;padding-top:1rem}summary{cursor:pointer;font-weight:700}"
                "details pre{max-height:32rem;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;background:#101827;color:#e5edf8;padding:1rem;border-radius:.45rem;font-size:.78rem}"
                "@media(max-width:32rem){.summary{grid-template-columns:1fr;gap:.15rem}.summary dd+dt{margin-top:.6rem}}"
                "</style></head><body><main><h1>" + e(result["title"]) +
                "</h1><dl class=\"summary\"><dt>Company</dt><dd>" + e(result["company_id"]) +
                "</dd><dt>Version</dt><dd>" + e(result["version"]) +
                "</dd><dt>Created</dt><dd>" + e(result["created_at"]) +
                "</dd></dl><aside class=\"warning\"><h2>Editing and publication warning</h2>"
                "<p><strong>Unreviewed; Wiki not published.</strong></p>"
                "<p>A citation’s presence does not equal semantic support. Edited content has not undergone automated semantic review, and original-answer citations do not automatically support edited statements.</p>"
                "</aside><section><h2>Source question</h2><p class=\"pre-wrap\">" + e(result["question"]) +
                "</p></section><section><h2>Content</h2><pre class=\"pre-wrap\">" + e(result["body"]) +
                "</pre></section><section><h2>Limitations</h2><ul>" + limitations +
                "</ul></section><section><h2>Original answer citations (do not automatically support edited content)</h2><ol>" + refs +
                "</ol></section><details><summary>Frozen original answer artifacts (collapsed by default)</summary>" +
                artifact_html + "</details>" +
                "<details><summary>Technical metadata (collapsed by default)</summary><pre>" +
                e(json.dumps(technical, ensure_ascii=False, sort_keys=True, indent=2)) +
                "</pre></details></main></body></html>"
            )
            return html, "text/html", filename
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["record_type", "label", "value", "metadata"])
        fields = (("result_id", result["result_id"]), ("version", result["version"]),
                  ("status", result["human_review_status"]),
                  ("publication_status", result["publication_status"]),
                  ("title", result["title"]), ("company", result["company_id"]),
                  ("question", result["question"]), ("body", result["body"]),
                  ("warning", result["warning"]))
        for label, value in fields:
            writer.writerow(["result", label, self._csv_safe(value), ""])
        for item in result["limitations"]:
            writer.writerow(["limitation", "limitation", self._csv_safe(item), ""])
        for item in result["citations"]:
            label = f'[{item.get("kind")}:{item.get("id")}]'
            writer.writerow(["reference", self._csv_safe(label), "",
                             self._csv_safe(json.dumps(item, ensure_ascii=False, sort_keys=True))])
        writer.writerow(["source_snapshot", "immutable", "",
                         self._csv_safe(json.dumps(result["source_snapshot"], ensure_ascii=False, sort_keys=True))])
        if result["artifact"] is None:
            writer.writerow([
                "artifact_gap", "original_answer", "",
                self._csv_safe(json.dumps(result["artifact_gap"], ensure_ascii=False, sort_keys=True)),
            ])
        else:
            artifact = result["artifact"]
            writer.writerow([
                "artifact", "original_answer", self._csv_safe(artifact.get("artifact_id")),
                self._csv_safe(json.dumps(result["artifact_context"], ensure_ascii=False, sort_keys=True)),
            ])
            for dataset in artifact.get("datasets", []):
                if not isinstance(dataset, dict):
                    continue
                dataset_metadata = {
                    key: value for key, value in dataset.items()
                    if key not in {"rows", "raw_rows"}
                }
                rows = dataset.get("rows") or dataset.get("raw_rows") or []
                for row in rows:
                    writer.writerow([
                        "artifact_dataset", self._csv_safe(dataset.get("id")),
                        self._csv_safe(json.dumps(row, ensure_ascii=False, sort_keys=True)),
                        self._csv_safe(json.dumps(dataset_metadata, ensure_ascii=False, sort_keys=True)),
                    ])
            for trace in artifact.get("traces", []):
                if isinstance(trace, dict):
                    writer.writerow([
                        "artifact_trace", self._csv_safe(trace.get("id")), "",
                        self._csv_safe(json.dumps(trace, ensure_ascii=False, sort_keys=True)),
                    ])
        return output.getvalue(), "text/csv", filename
