"""Compile frozen answer artifacts into the report-reader payload contract."""

from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import hashlib
import json
import re
import sqlite3
from typing import Any


SCHEMA_VERSION = "report-reader-payload/1"
REPORT_DIFF_SCHEMA_VERSION = "report-diff/1"
REPORT_DIFF_ALGORITHM_VERSION = "report-diff-exact/1"
_INLINE_REFERENCE = re.compile(r"\s*\[(claim|evidence|passage|calculation|fact|query):[^\]\s]+\]")

CHAPTERS = (
    ("question-conclusion", "Question and conclusion"),
    ("analysis-findings", "Analysis and findings"),
    ("evidence-sources", "Evidence and sources"),
    ("boundaries-method", "Boundaries and methods"),
)

_READER_SCHEMA = """
CREATE TABLE IF NOT EXISTS report_reader_versions (
    report_id TEXT PRIMARY KEY,
    version_id TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    message_id TEXT NOT NULL UNIQUE REFERENCES messages(message_id),
    run_id TEXT REFERENCES runs(run_id),
    version_number INTEGER NOT NULL CHECK(version_number > 0),
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    status_label TEXT NOT NULL,
    artifact_id TEXT,
    artifact_schema_version TEXT,
    artifact_algorithm_version TEXT,
    publication_schema TEXT,
    payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    reader_href TEXT NOT NULL,
    UNIQUE(conversation_id, version_number)
);
CREATE TRIGGER IF NOT EXISTS report_reader_versions_no_update
BEFORE UPDATE ON report_reader_versions
BEGIN SELECT RAISE(ABORT, 'report reader versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS report_reader_versions_no_delete
BEFORE DELETE ON report_reader_versions
BEGIN SELECT RAISE(ABORT, 'report reader versions are immutable'); END;
CREATE TABLE IF NOT EXISTS report_artifact_snapshots (
    report_id TEXT PRIMARY KEY REFERENCES report_reader_versions(report_id),
    artifact_id TEXT NOT NULL UNIQUE,
    conversation_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL CHECK(length(artifact_sha256) = 64),
    artifact_json TEXT NOT NULL,
    registered_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS report_artifact_snapshots_no_update
BEFORE UPDATE ON report_artifact_snapshots
BEGIN SELECT RAISE(ABORT, 'report artifact snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS report_artifact_snapshots_no_delete
BEFORE DELETE ON report_artifact_snapshots
BEGIN SELECT RAISE(ABORT, 'report artifact snapshots are immutable'); END;
"""


def report_id_for(conversation_id: str, message_id: str) -> str:
    return _stable_id("report", conversation_id, message_id)


def report_version_id_for(artifact_id: str | None, algorithm_version: str | None) -> str:
    return _stable_id("version", artifact_id, algorithm_version)


def _ensure_reader_schema(connection) -> None:
    # executescript commits a caller's pending transaction before executing SQL.
    # Execute complete statements individually so reader/artifact writes and
    # recovery keep their BEGIN IMMEDIATE lock through the final commit.
    statement = ""
    for line in _READER_SCHEMA.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("incomplete report reader schema statement")


def _payload_from_row(row) -> dict:
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise RuntimeError("stored report reader payload is invalid")
    recorded = payload.get("payload_sha256")
    if recorded != row["payload_sha256"]:
        raise RuntimeError("stored report reader payload hash differs from index")
    candidate = dict(payload)
    candidate.pop("payload_sha256", None)
    digest = hashlib.sha256(_json(candidate).encode("utf-8")).hexdigest()
    if digest != recorded:
        raise RuntimeError("stored report reader payload hash is not reproducible")
    return payload


def _artifact_from_row(row) -> dict:
    from .answer_artifacts import validate_artifact_snapshot
    artifact = json.loads(row["artifact_json"])
    validate_artifact_snapshot(artifact)
    digest = hashlib.sha256(_json(artifact).encode("utf-8")).hexdigest()
    if digest != row["artifact_sha256"]:
        raise RuntimeError("stored report artifact snapshot hash is not reproducible")
    for key in ("artifact_id", "conversation_id", "message_id", "run_id"):
        if artifact.get(key) != row[key]:
            raise RuntimeError("stored report artifact snapshot differs from its index")
    if (artifact.get("schema_version") != row["schema_version"]
            or artifact.get("algorithm_version") != row["algorithm_version"]):
        raise RuntimeError("stored report artifact version differs from its index")
    return artifact


def _reader_row_for_artifact(connection, artifact: dict, expected_report_id: str | None = None):
    from .answer_artifacts import validate_artifact_snapshot
    validate_artifact_snapshot(artifact)
    report_id = report_id_for(artifact["conversation_id"], artifact["message_id"])
    if expected_report_id is not None and report_id != expected_report_id:
        raise ValueError("artifact report id differs from the requested report")
    if artifact.get("reader_report_id") != report_id:
        raise ValueError("artifact reader report id is missing or inconsistent")
    row = connection.execute(
        "SELECT * FROM report_reader_versions WHERE report_id=?", (report_id,),
    ).fetchone()
    if row is None:
        raise ValueError("frozen reader report does not exist")
    for key in ("artifact_id", "conversation_id", "message_id", "run_id"):
        if row[key] != artifact.get(key):
            raise ValueError(f"artifact {key} differs from the frozen reader")
    if (row["artifact_schema_version"] != artifact.get("schema_version")
            or row["artifact_algorithm_version"] != artifact.get("algorithm_version")):
        raise ValueError("artifact version differs from the frozen reader")
    payload = _payload_from_row(row)
    report = _dict(payload.get("report"))
    for key in ("artifact_id", "conversation_id", "message_id", "run_id"):
        if report.get(key) != artifact.get(key):
            raise ValueError(f"artifact {key} differs from the frozen reader payload")
    return report_id, row


def _insert_artifact_snapshot(connection, artifact: dict, *, registered_at: str) -> dict:
    report_id, _reader = _reader_row_for_artifact(connection, artifact)
    artifact_json = _json(artifact)
    digest = hashlib.sha256(artifact_json.encode("utf-8")).hexdigest()
    existing = connection.execute(
        "SELECT * FROM report_artifact_snapshots WHERE report_id=?", (report_id,),
    ).fetchone()
    if existing is not None:
        saved = _artifact_from_row(existing)
        if existing["artifact_sha256"] != digest or saved != artifact:
            raise ValueError("a different immutable artifact snapshot already exists for this report")
        return saved
    connection.execute(
        "INSERT INTO report_artifact_snapshots VALUES(?,?,?,?,?,?,?,?,?,?)",
        (report_id, artifact["artifact_id"], artifact["conversation_id"], artifact["message_id"],
         artifact["run_id"], artifact["schema_version"], artifact["algorithm_version"],
         digest, artifact_json, registered_at),
    )
    return artifact


def load_reader_payload(chat, *, report_id: str | None = None,
                        conversation_id: str | None = None, message_id: str | None = None) -> dict | None:
    """Load an already frozen reader without invoking today's artifact projection."""
    if report_id is None:
        if not conversation_id or not message_id:
            raise ValueError("report identity is required")
        report_id = report_id_for(conversation_id, message_id)
    with closing(chat._connect()) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_reader_versions'"
        ).fetchone()
        if table is None:
            return None
        row = connection.execute(
            "SELECT * FROM report_reader_versions WHERE report_id=?", (report_id,),
        ).fetchone()
        return _payload_from_row(row) if row is not None else None


def load_reader_artifact_snapshot(chat, *, report_id: str | None = None,
                                  conversation_id: str | None = None,
                                  message_id: str | None = None) -> dict | None:
    if report_id is None:
        if not conversation_id or not message_id:
            raise ValueError("report identity is required")
        report_id = report_id_for(conversation_id, message_id)
    with closing(chat._connect()) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_artifact_snapshots'"
        ).fetchone()
        if table is None:
            return None
        row = connection.execute(
            "SELECT * FROM report_artifact_snapshots WHERE report_id=?", (report_id,),
        ).fetchone()
        if row is None:
            return None
        artifact = _artifact_from_row(row)
        _reader_row_for_artifact(connection, artifact, report_id)
        return artifact


def _require_publication(connection, conversation_id: str, message_id: str) -> None:
    from .chat import ChatError
    published = connection.execute(
        "SELECT 1 FROM report_publications p JOIN messages m ON m.message_id=p.message_id "
        "WHERE m.conversation_id=? AND m.message_id=? AND m.role='assistant' AND m.status='completed'",
        (conversation_id, message_id),
    ).fetchone()
    if published is None:
        raise ChatError("report_not_found", "structured report does not exist", 404)


def resolve_report(chat, *, report_id: str | None = None, conversation_id: str | None = None,
                   message_id: str | None = None) -> dict[str, str]:
    """Resolve a public reader report id to the internal frozen answer identity."""
    from .chat import ChatError
    if report_id is None:
        if conversation_id is None or message_id is None:
            raise ChatError("invalid_report_reference", "report_id or conversation_id/message_id is required")
        conversation_id = chat._valid_id(conversation_id, "conversation_id")
        message_id = chat._valid_id(message_id, "message_id")
        with closing(chat._connect()) as connection:
            _require_publication(connection, conversation_id, message_id)
        return {"report_id": report_id_for(conversation_id, message_id),
                "conversation_id": conversation_id, "message_id": message_id}
    report_id = chat._valid_id(report_id, "report_id")
    with closing(chat._connect()) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_reader_versions'"
        ).fetchone()
        indexed = (connection.execute(
            "SELECT conversation_id,message_id FROM report_reader_versions WHERE report_id=?",
            (report_id,),
        ).fetchone() if table is not None else None)
        if indexed is not None:
            _require_publication(connection, indexed["conversation_id"], indexed["message_id"])
            return {"report_id": report_id, "conversation_id": indexed["conversation_id"],
                    "message_id": indexed["message_id"]}
        rows = connection.execute(
            "SELECT m.conversation_id,m.message_id FROM messages m "
            "JOIN report_publications p ON p.message_id=m.message_id "
            "WHERE m.role='assistant' AND m.status='completed' ORDER BY m.created_at,m.rowid"
        ).fetchall()
    for row in rows:
        if report_id_for(row["conversation_id"], row["message_id"]) == report_id:
            return {"report_id": report_id, "conversation_id": row["conversation_id"], "message_id": row["message_id"]}
    raise ChatError("report_not_found", "report does not exist", 404)


def load_required_reader_snapshots(chat, *, report_id: str) -> tuple[dict, dict]:
    """Load the publication-time reader and artifact closure or fail intact."""
    from .chat import ChatError
    payload = load_reader_payload(chat, report_id=report_id)
    if payload is None:
        raise ChatError(
            "report_reader_snapshot_unavailable",
            "The formal report's publication-time reader snapshot is unavailable.", 409)
    artifact = load_reader_artifact_snapshot(chat, report_id=report_id)
    if artifact is None:
        raise ChatError(
            "report_artifact_snapshot_unavailable",
            "The formal report's publication-time artifact snapshot is unavailable.", 409)
    return payload, artifact

def store_prepared_reader_snapshot(connection, *, artifact: dict, payload: dict,
                                   registered_at: str) -> dict:
    """Append a pre-rendered formal reader closure in the caller transaction."""
    from .answer_artifacts import validate_artifact_snapshot
    validate_artifact_snapshot(artifact)
    if build_reader_payload(artifact) != payload:
        raise ValueError("prepared reader payload differs from the canonical renderer")
    conversation_id, message_id, run_id = (artifact.get(key) for key in (
        "conversation_id", "message_id", "run_id"))
    report_id = report_id_for(conversation_id, message_id)
    if artifact.get("reader_report_id") != report_id:
        raise ValueError("prepared artifact report identity is inconsistent")
    message = connection.execute(
        "SELECT message_id,conversation_id,run_id,created_at FROM messages "
        "WHERE message_id=? AND conversation_id=? AND role='assistant' AND status='completed'",
        (message_id, conversation_id),
    ).fetchone()
    if message is None or message["run_id"] != run_id:
        raise ValueError("prepared snapshot is not bound to the formal message")
    _ensure_reader_schema(connection)
    if connection.execute(
            "SELECT 1 FROM report_reader_versions WHERE report_id=?", (report_id,)).fetchone():
        raise ValueError("formal reader snapshot already exists")
    ordered = connection.execute(
        "SELECT message_id FROM messages WHERE conversation_id=? AND role='assistant' "
        "AND status='completed' ORDER BY created_at,rowid", (conversation_id,),
    ).fetchall()
    version_number = next(
        (index for index, row in enumerate(ordered, 1) if row["message_id"] == message_id), None)
    if version_number is None:
        raise ValueError("formal message is absent from report ordering")
    report = _dict(payload.get("report"))
    publication = _dict(artifact.get("publication"))
    connection.execute(
        "INSERT INTO report_reader_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (report_id, report["version_id"], conversation_id, message_id, run_id,
         version_number, _text(report.get("title"), "Research report"),
         _text(report.get("status"), "needs_review"),
         _text(report.get("status_label"), "Pending human approval"), artifact["artifact_id"],
         artifact["schema_version"], artifact["algorithm_version"],
         publication.get("schema_version"), payload["payload_sha256"], _json(payload),
         message["created_at"], registered_at, f"/report-reader.html?report_id={report_id}"),
    )
    _insert_artifact_snapshot(connection, artifact, registered_at=registered_at)
    return {"report_id": report_id, "artifact_id": artifact["artifact_id"],
            "payload_sha256": payload["payload_sha256"]}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _stable_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha256(_json(parts).encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return fallback


def _reader_text(value: Any, fallback: str = "") -> str:
    """Return user-facing report text without internal citation marker syntax."""
    cleaned = _INLINE_REFERENCE.sub("", _text(value, fallback))
    return cleaned.strip()


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _reference_key(reference: dict) -> tuple[str, str]:
    return (_text(reference.get("kind")).lower(), _text(reference.get("id")))


def _reference_label(reference: dict) -> str:
    kind = _text(reference.get("kind")).lower()
    labels = {
        "claim": "Claim evidence",
        "evidence": "Table/fact evidence",
        "passage": "Source passage evidence",
        "calculation": "Calculation evidence",
        "query": "SQL evidence",
    }
    return labels.get(kind, "Evidence")


def _verification_method(references: list[dict], anchor_kind: str) -> str:
    kinds = {_text(reference.get("kind")).lower() for reference in references}
    if "query" in kinds:
        return "Inspect the frozen SQL query, result rows and columns, and native database snapshot."
    if "calculation" in kinds or anchor_kind == "number":
        return "Inspect the recorded inputs, formula, units, period, and result."
    if "passage" in kinds:
        return "Inspect the frozen source passage, context, and source version."
    if "evidence" in kinds:
        return "Inspect the frozen fact or table record and its source."
    if kinds:
        return "Inspect the evidence explicitly bound when the report was published."
    return "This content is not yet bound to an openable sentence-level source."


def _support_label(status: str) -> str:
    return {
        "calculated": "Calculation bound",
        "supported": "Evidence bound",
        "source_linked": "Source linked",
        "unresolved": "Needs review",
        "reference_invalid": "Invalid reference",
        "unreferenced": "No evidence bound",
        "needs_review": "Pending review",
        "unsupported": "Unsupported by evidence; not presented as a conclusion",
        "conflicting": "Conflicting evidence; not presented as a definitive conclusion",
        "partial_support": "Only partially supported; review required",
        "inference": "Analytical judgment, not an established fact",
        "not_assessed": "Not semantically reviewed",
        "mixed": "Mixed review results; review required",
    }.get(status, "Needs review")


def _precise_binding_label(status: str, has_node_verification: bool) -> str:
    if has_node_verification and status == "unresolved":
        return "Reviewed as a whole; evidence not annotated sentence by sentence"
    return _support_label(status)


_REQUIRED_VERIFIER_LABELS = {
    "supported": "Machine review: supported",
    "corrected_and_rechecked": "Machine review: corrected and rechecked",
    "conflict_disclosed": "Machine review: conflict disclosed",
    "insufficient_material_disclosed": "Machine review: insufficient material disclosed",
}


def _required_verifier_findings(value) -> list[dict]:
    projected = []
    for item in _list(value):
        item = _dict(item)
        outcome = _text(item.get("outcome"))
        if outcome not in _REQUIRED_VERIFIER_LABELS:
            continue
        receipts = []
        for receipt in _list(item.get("receipts")):
            receipt = _dict(receipt)
            if _text(receipt.get("receipt_id")) and _text(receipt.get("sha256")):
                receipts.append({"receipt_id": _text(receipt.get("receipt_id")),
                                 "sha256": _text(receipt.get("sha256"))})
        projected.append({
            "node_id": _text(item.get("node_id")),
            "check_kind": _text(item.get("check_kind")),
            "outcome": outcome,
            "outcome_label": _REQUIRED_VERIFIER_LABELS[outcome],
            "explanation": _text(item.get("explanation")),
            "receipts": receipts,
        })
    return projected


def _finding_title(index: int, finding: dict) -> str:
    explicit = _text(finding.get("title"))
    if explicit:
        return explicit
    classification = _text(finding.get("classification"))
    if index == 0:
        return "Conclusion summary"
    return {
        "recorded_calculation": "Calculations and derived metrics",
        "source_statement": "Source excerpt",
        "evidence_gap": "Evidence gap",
        "interpretation": "Analysis",
    }.get(classification, f"Analysis finding {index + 1}")


def _finding_appendix(finding: dict) -> list[str]:
    appendix = []
    reasoning = _text(finding.get("reasoning"))
    if reasoning:
        appendix.append(f"Reasoning (analytical judgment, not independently verified): {reasoning}")
    appendix.extend(f"Limitation: {item}" for item in _list(finding.get("limitations")) if isinstance(item, str))
    return appendix


def _finding_body(finding: dict) -> str:
    body = _reader_text(finding.get("text"))
    appendix = _finding_appendix(finding)
    if appendix:
        suffix = "\n\n" + "\n\n".join(appendix)
        if body.endswith(suffix):
            return body[:-len(suffix)].strip()
    return body


def _trace_summary(trace: dict, calculation: dict | None = None, *, include_identity=False) -> dict:
    steps = []
    for step in _list(trace.get("steps")):
        step = _dict(step)
        steps.append({
            "layer": _text(step.get("layer"), "Uncategorized"),
            "title": _text(step.get("title"), "Step not recorded"),
            "details": _text(step.get("details"), "Details not recorded"),
            "operation": _text(step.get("operation")),
        })
    source = _dict(trace.get("source"))
    return {
        "id": _text(trace.get("id")),
        "status": _text(trace.get("status"), "unavailable"),
        "reference": deepcopy(_dict(trace.get("reference"))),
        "steps": steps,
        "source": _source_summary(source, include_identity=include_identity) if source else None,
        "calculation": ({key: deepcopy(calculation[key]) for key in (
            "formula", "value", "unit", "operation", "arithmetic", "limitations",
        ) if key in calculation} if calculation else None),
        "limitations": [_text(item) for item in _list(trace.get("limitations")) if _text(item)],
    }


def _source_summary(source: dict, *, include_identity=False) -> dict:
    result = {
        key: deepcopy(source.get(key)) for key in (
            "source_id", "source_sha256", "title", "url", "document_version_id",
            "source_url", "company_id", "company_ids", "source_key", "source_version",
            "published_at", "period_end", "parser_version", "locator",
            "quote", "context", "query", "columns", "rows", "rights_status",
            "formula", "value", "unit",
        ) if key in source
    }
    if include_identity:
        identity = _dict(source.get("fact"))
        for key in ("fact_id", "evidence_id"):
            if source.get(key) or identity.get(key):
                result[key] = source.get(key) or identity[key]
    if isinstance(source.get("inputs"), list):
        result["inputs"] = [{key: deepcopy(fact[key]) for key in (
            "company_id", "metric", "value", "unit", "basis", "period_start", "period_end",
            "period_kind", "row_label", "raw_value", "headers", "metadata_provenance", "locator",
        ) + (("fact_id", "evidence_id") if include_identity else ()) if key in fact} for fact in source["inputs"] if isinstance(fact, dict)]
    if isinstance(source.get("sources"), list):
        result["sources"] = [_source_summary(item, include_identity=include_identity) for item in source["sources"] if isinstance(item, dict)]
    table = _dict(source.get("table"))
    rows = _list(table.get("rows"))
    locator = _dict(source.get("locator"))
    if rows:
        selected = _list(locator.get("header_row_indices")) + [locator.get("row_index")]
        indices = list(dict.fromkeys(index for index in selected if type(index) is int and 0 <= index < len(rows)))
        result["table_excerpt"] = {
            "selection": "selected_headers_and_source_row", "total_rows": len(rows),
            "rows": [{"row_index": index, "cells": deepcopy(rows[index])} for index in indices],
            "selected_row": locator.get("row_index"), "selected_column": locator.get("column_index"),
        }
    return result


def _sources(artifact: dict) -> list[dict]:
    audit = _dict(artifact.get("audit"))
    raw = []
    seen = set()
    for item in _list(audit.get("frozen_raw_sources")) + _list(audit.get("frozen_sources")):
        item = _dict(item)
        digest = _text(item.get("source_sha256") or item.get("sha256"))
        source_id = _text(item.get("document_version_id") or item.get("source_id") or digest)
        key = source_id or digest
        if not key or key in seen:
            continue
        seen.add(key)
        raw.append({
            "id": source_id,
            "title": _text(item.get("title"), "Untitled source"),
            "url": item.get("source_url") or item.get("url"),
            "source_sha256": digest or None,
            "document_version_id": item.get("document_version_id"),
            "company_id": item.get("company_id"),
            "source_key": item.get("source_key"),
            "source_version": item.get("source_version"),
            "published_at": item.get("published_at"),
            "collected_at": item.get("collected_at"),
            "period_end": item.get("period_end"),
            "media_type": item.get("media_type"),
            "byte_length": item.get("byte_length"),
            "rights_status": item.get("rights_status"),
            "parse_status": item.get("parse_status"),
            "status": "frozen",
        })
    return raw


def _finding_blocks(findings: list[dict], anchors_by_finding: dict[str, list[dict]]) -> list[dict]:
    blocks = []
    for index, finding in enumerate(findings):
        finding_id = _text(finding.get("id"), f"finding-{index + 1}")
        appendix = set(_finding_appendix(finding))
        anchor_ids = [
            _text(anchor.get("id")) for anchor in anchors_by_finding.get(finding_id, [])
            if _text(anchor.get("id")) and _reader_text(anchor.get("text")) not in appendix
        ]
        status = _text(finding.get("semantic_status") or finding.get("status"), "needs_review")
        blocks.append({
            "id": finding_id,
            "type": "finding",
            "title": _finding_title(index, finding),
            "classification": _text(finding.get("classification"), "interpretation"),
            "text": _finding_body(finding),
            "status": status,
            "status_label": (
                _support_label(status) if status in {
                    "unsupported", "conflicting", "partial_support", "inference", "not_assessed",
                    "unreferenced", "reference_invalid", "unresolved", "needs_review",
                } else ""
            ),
            "anchor_ids": anchor_ids,
            "reasoning": _text(finding.get("reasoning")),
            "limitations": [_text(item) for item in _list(finding.get("limitations")) if _text(item)],
            "review_flags": deepcopy(_list(finding.get("review_flags"))),
            **({"required_verifier_findings": _required_verifier_findings(
                finding.get("required_verifier_findings"))}
               if finding.get("required_verifier_findings") else {}),
        })
    return blocks


def _dataset_blocks(datasets: list[dict]) -> list[dict]:
    blocks = []
    for dataset in datasets:
        dataset = _dict(dataset)
        dataset_id = _text(dataset.get("id"), _stable_id("dataset", dataset))
        rows = []
        for index, row in enumerate(_list(dataset.get("rows")), 1):
            row = _dict(row)
            row_id = _text(row.get("id"), f"{dataset_id}-row-{index}")
            rows.append({
                "id": row_id,
                "anchor_id": f"{row_id}-cell-value",
                "label": _text(row.get("label")),
                **{key: deepcopy(row[key]) for key in ("record_kind", "company_id", "metric") if key in row},
                "value": row.get("value"),
                "unit": row.get("unit"),
                **({"display_text": row["display_text"]} if isinstance(row.get("display_text"), str) else {}),
                "basis": row.get("basis"),
                "period": deepcopy(row.get("period")),
                "reference": deepcopy(row.get("reference")),
                "trace_id": row.get("trace_id"),
                "value_formation": row.get("value_formation"),
                "attribution": deepcopy(row.get("attribution")),
                "semantic_review": deepcopy(_dict(row.get("semantic_review"))),
            })
        block = {
            "id": dataset_id,
            "type": "dataset",
            "title": _text(dataset.get("title"), "Dataset"),
            "description": _text(dataset.get("description")),
            **({"description_anchor_id": dataset["description_anchor"]["id"]}
               if isinstance(dataset.get("description_anchor"), dict) else {}),
            "display": "chart" if dataset.get("chart") else "table",
            "columns": deepcopy(_list(dataset.get("columns"))),
            "rows": rows,
            "chart": deepcopy(dataset.get("chart")),
            "limitations": deepcopy(_list(dataset.get("limitations"))),
            "semantic_review": deepcopy(_dict(dataset.get("semantic_review"))),
            "review_flags": deepcopy(_list(dataset.get("review_flags"))),
            **({"required_verifier_findings": _required_verifier_findings(
                dataset.get("required_verifier_findings"))}
               if dataset.get("required_verifier_findings") else {}),
        }
        status = _text(_dict(dataset.get("semantic_review")).get("status"), "not_assessed")
        block["status"] = status
        block["status_label"] = "" if status == "supported" else _support_label(status)
        if isinstance(dataset.get("presentation"), dict):
            block["presentation"] = deepcopy(dataset["presentation"])
        blocks.append(block)
    return blocks


def _dataset_row_anchors(datasets: list[dict]) -> list[dict]:
    anchors = []
    for dataset in datasets:
        dataset = _dict(dataset)
        dataset_id = _text(dataset.get("id"), _stable_id("dataset", dataset))
        if isinstance(dataset.get("description_anchor"), dict):
            anchors.append(deepcopy(dataset["description_anchor"]))
        for index, row in enumerate(_list(dataset.get("rows")), 1):
            row = _dict(row)
            row_id = _text(row.get("id"), f"{dataset_id}-row-{index}")
            reference = _dict(row.get("reference"))
            references = [reference] if reference.get("kind") and reference.get("id") else []
            trace_id = row.get("trace_id")
            kind = _text(reference.get("kind")).lower()
            value = row.get("value")
            unit = row.get("unit")
            anchors.append({
                "id": f"{row_id}-cell-value",
                "finding_id": dataset_id,
                "start": None,
                "end": None,
                "utf16_start": None,
                "utf16_end": None,
                "text": (f'{_text(row.get("label"), "Data")}：{row["display_text"]}'
                         if isinstance(row.get("display_text"), str) and row["display_text"]
                         else f'{_text(row.get("label"), "Data")}：{_text(value, "—")} {_text(unit)}'.strip()),
                "kind": "table_cell",
                "support_status": "calculated" if kind in {"calculation", "query"} else "supported" if references else "unresolved",
                "classification": "recorded_calculation" if kind in {"calculation", "query"} else "direct_statement",
                "references": references,
                "trace_ids": [trace_id] if isinstance(trace_id, str) and trace_id else [],
                "calculation_id": reference.get("id") if kind == "calculation" else None,
                "value_formation": row.get("value_formation"),
                "attribution": deepcopy(row.get("attribution")),
                "warnings": [],
                "semantic_review": deepcopy(_dict(row.get("semantic_review"))),
                "evidence": [],
                "explanation": "The report table cell is bound to the data point selected at publication time.",
            })
    return anchors


def _evidence_bundle(anchor: dict, traces_by_id: dict[str, dict], calculations: dict | None = None) -> dict:
    references = [_dict(reference) for reference in _list(anchor.get("references"))]
    traces = [_trace_summary(traces_by_id[trace_id], (calculations or {}).get(
        _dict(traces_by_id[trace_id].get("reference")).get("id"))) for trace_id in _list(anchor.get("trace_ids"))
              if isinstance(trace_id, str) and trace_id in traces_by_id]
    status = _text(anchor.get("support_status"), "unresolved")
    semantic = _dict(anchor.get("semantic_review"))
    return {
        "id": f'evidence-{_text(anchor.get("id"))}',
        "anchor_id": _text(anchor.get("id")),
        "claim": _reader_text(anchor.get("text")),
        "support_status": status,
        "support_label": _precise_binding_label(status, bool(anchor.get("required_verifier_findings"))),
        **({"value_formation": anchor["value_formation"],
            "attribution": deepcopy(anchor.get("attribution"))}
           if anchor.get("value_formation") in {"observed", "estimated", "assumed", "derived"} else {}),
        "verification": {
            "method": _verification_method(references, _text(anchor.get("kind"))),
            "boundary": "Review only the selected claim or value; do not extrapolate automatically to other report conclusions.",
            "semantic_status": semantic.get("support_status"),
            "semantic_reason": semantic.get("reason"),
            "semantic_verified": False,
            **({"required_verifier_findings": _required_verifier_findings(
                anchor.get("required_verifier_findings")),
                "precise_binding_status": (
                    "Not established" if status == "unresolved" else _support_label(status))}
               if anchor.get("required_verifier_findings") else {}),
        },
        "references": [{
            "kind": _text(reference.get("kind")).lower(),
            "id": _text(reference.get("id")),
            "label": _reference_label(reference),
            "valid": reference.get("valid") if type(reference.get("valid")) is bool else None,
        } for reference in references],
        "traces": traces,
        "warnings": deepcopy(_list(anchor.get("warnings"))),
        "explanation": _text(anchor.get("explanation")),
    }


def _question_payload(artifact: dict) -> dict:
    """Expose recorded question context without reconstructing legacy metadata."""
    snapshot = _dict(artifact.get("question_snapshot"))
    original = _text(snapshot.get("original_question")) or _text(artifact.get("question"))
    normalized = _text(snapshot.get("normalized_question"))
    scope = _dict(snapshot.get("recorded_scope"))
    normalization = []
    for subject in _list(scope.get("subjects")):
        label = _text(_dict(subject).get("label"))
        if label:
            normalization.append({"kind": "subject", "text": label})
    for field, kind in (("period_text", "period"), ("unit", "unit")):
        text = _text(scope.get(field))
        if text:
            normalization.append({"kind": kind, "text": text})
    for field, kind in (("metrics", "metric"), ("basis", "basis"),
                        ("directions", "direction")):
        for item in _list(scope.get(field)):
            if isinstance(item, str) and item:
                normalization.append({"kind": kind, "text": item})
    assumptions = [item for item in _list(snapshot.get("recorded_assumptions"))
                   if isinstance(item, str)]
    normalization.extend({"kind": "assumption", "text": item} for item in assumptions)
    clarifications = deepcopy(_list(snapshot.get("clarifications")))
    for clarification in clarifications:
        clarification = _dict(clarification)
        for option in _list(clarification.get("selected")):
            option = _dict(option)
            label = _text(option.get("label"))
            if label:
                normalization.append({
                    "kind": "clarification_choice", "text": label,
                    "decision_kind": clarification.get("kind"), "option_id": option.get("id"),
                })
        notes = _text(clarification.get("user_notes"))
        if notes:
            normalization.append({"kind": "user_clarification", "text": notes,
                                  "decision_kind": clarification.get("kind")})
    return {
        "original": original,
        "normalized": normalized,
        "normalization": normalization,
        "open_uncertainties": deepcopy(_list(snapshot.get("open_uncertainties"))),
        "scope": deepcopy(scope),
        "assumptions": assumptions,
        "scope_gaps": deepcopy(_list(snapshot.get("scope_gaps"))),
        "clarifications": clarifications,
        "time_choice": deepcopy(snapshot.get("time_choice")),
        "latest_user_question": snapshot.get("latest_user_question"),
        "snapshot_schema_version": snapshot.get("schema_version"),
    }


def build_reader_payload(artifact: dict) -> dict:
    """Return a presentation-safe reader payload from one frozen answer artifact.

    The compiler is intentionally conservative: it never infers new findings,
    never revalidates claims, and never rewrites numbers. Missing information is
    surfaced as absent/needs-review fields so the reader can show boundaries.
    """
    artifact = _dict(artifact)
    publication = _dict(artifact.get("publication"))
    report_id = report_id_for(_text(artifact.get("conversation_id")), _text(artifact.get("message_id")))
    version_id = report_version_id_for(_text(artifact.get("artifact_id")), _text(artifact.get("algorithm_version")))
    title = _text(publication.get("title")) or _text(artifact.get("question"), "Research report")
    findings = [_dict(item) for item in _list(artifact.get("findings"))]
    findings_by_id = {_text(finding.get("id")): finding for finding in findings}
    datasets = _list(artifact.get("datasets"))
    anchors = [_dict(item) for item in _list(artifact.get("anchors"))] + _dataset_row_anchors(datasets)
    traces = [_dict(item) for item in _list(artifact.get("traces"))]
    traces_by_id = {_text(trace.get("id")): trace for trace in traces if _text(trace.get("id"))}
    calculations = {item.get("calculation_id"): item for item in _list(artifact.get("calculations"))
                    if isinstance(item, dict) and item.get("status") == "recorded"}
    anchors_by_finding: dict[str, list[dict]] = {}
    for anchor in anchors:
        anchors_by_finding.setdefault(_text(anchor.get("finding_id")), []).append(anchor)

    finding_blocks = _finding_blocks(findings, anchors_by_finding)
    dataset_blocks = _dataset_blocks(datasets)
    excluded = [dict(block, type="review_finding") for block in finding_blocks
                if block["status"] in {"unsupported", "conflicting"}]
    excluded_datasets = [block for block in dataset_blocks if block["status"] in {
        "unsupported", "conflicting",
    } or any(
        _text(flag.get("code")) in {"semantic_unsupported", "semantic_conflicting"}
        for flag in _list(block.get("review_flags")) if isinstance(flag, dict)
    ) or any(
        _text(claim.get("support_status")) in {"unsupported", "conflicting"}
        for claim in _list(_dict(block.get("semantic_review")).get("claims")) if isinstance(claim, dict)
    )]
    dataset_blocks = [block for block in dataset_blocks if block not in excluded_datasets]
    body_blocks = [block for block in finding_blocks if block["status"] not in {"unsupported", "conflicting"}]
    # If the submitted conclusion is unsupported, do not promote a later
    # management quote or method note into its place as a new conclusion.
    primary_blocks = [block for block in finding_blocks[:1] if block in body_blocks]
    if excluded or excluded_datasets:
        primary_blocks.append({
            "id": "unsupported-content-notice", "type": "notice", "title": "Some content cannot be presented as a conclusion",
            "text": "Saved review records contain unsupported or conflicting content. It is listed in the review appendix under “Boundaries and methods”; the related question cannot yet be answered reliably.",
            "anchor_ids": [],
        })
    analysis_blocks = [block for block in body_blocks
                       if not finding_blocks or block["id"] != finding_blocks[0]["id"]]
    placed_dataset_ids = set()
    placement_counts = {}
    for dataset_block in dataset_blocks:
        after_block_id = _text(_dict(dataset_block.get("presentation")).get("after_block_id"))
        if not after_block_id:
            continue
        offset = placement_counts.get(after_block_id, 0)
        position = next((index + 1 + offset for index, block in enumerate(analysis_blocks)
                         if block.get("id") == after_block_id), None)
        primary_position = next((index + 1 + offset for index, block in enumerate(primary_blocks)
                                 if block.get("id") == after_block_id), None)
        excluded_position = next((index + 1 + offset for index, block in enumerate(excluded)
                                  if block.get("id") == after_block_id), None)
        if primary_position is not None:
            primary_blocks.insert(primary_position, dataset_block)
            placed_dataset_ids.add(dataset_block["id"])
        elif position is not None:
            analysis_blocks.insert(position, dataset_block)
            placed_dataset_ids.add(dataset_block["id"])
        elif excluded_position is not None:
            excluded.insert(excluded_position, dataset_block)
            placed_dataset_ids.add(dataset_block["id"])
        if dataset_block["id"] in placed_dataset_ids:
            placement_counts[after_block_id] = offset + 1
    analysis_blocks.extend(block for block in dataset_blocks if block["id"] not in placed_dataset_ids)
    excluded_placement_counts = {}
    for dataset_block in excluded_datasets:
        after_block_id = _text(_dict(dataset_block.get("presentation")).get("after_block_id"))
        offset = excluded_placement_counts.get(after_block_id, 0)
        position = next((index + 1 + offset for index, block in enumerate(excluded)
                         if block.get("id") == after_block_id), None)
        if position is None:
            excluded.append(dataset_block)
        else:
            excluded.insert(position, dataset_block)
            excluded_placement_counts[after_block_id] = offset + 1
    limitations = [_text(item) for item in _list(artifact.get("limitations")) if _text(item)]
    limitations.extend(
        _text(item) for finding in findings for item in _list(finding.get("limitations")) if _text(item)
    )
    limitations = list(dict.fromkeys(limitations))
    if not analysis_blocks:
        analysis_blocks = [{
            "id": "analysis-empty",
            "type": "notice",
            "title": "No analysis can be safely expanded",
            "text": "Available evidence is insufficient for further reliable analysis.",
            "anchor_ids": [],
        }]

    chapters = [
        {
            "id": CHAPTERS[0][0], "title": CHAPTERS[0][1],
            "blocks": [{
                "id": "research-question",
                "type": "question",
                "title": "Research question",
                "text": _text(artifact.get("question")),
                "anchor_ids": [],
            }, *primary_blocks],
        },
        {"id": CHAPTERS[1][0], "title": CHAPTERS[1][1], "blocks": analysis_blocks},
        {
            "id": CHAPTERS[2][0], "title": CHAPTERS[2][1],
            "blocks": [{
                "id": "source-summary",
                "type": "sources",
                "title": "Frozen sources",
                "text": "The following sources come from the frozen answer snapshot. An openable source does not mean the conclusion has received human approval.",
                "source_ids": [source["id"] for source in _sources(artifact)],
            }],
        },
        {
            "id": CHAPTERS[3][0], "title": CHAPTERS[3][1],
            "blocks": [{
                "id": "limitations",
                "type": "limitations",
                "title": "Boundaries and methods",
                "items": limitations,
                "text": "；".join(limitations) if limitations else "No additional limitations were recorded; this still does not indicate human approval.",
            }, *excluded],
        },
    ]

    reader_anchors = []
    evidence = {}
    for anchor in anchors:
        anchor_id = _text(anchor.get("id"))
        if not anchor_id:
            continue
        references = [_dict(reference) for reference in _list(anchor.get("references"))]
        bundle = _evidence_bundle(anchor, traces_by_id, calculations)
        finding = findings_by_id.get(_text(anchor.get("finding_id")))
        display_text = (_finding_body(finding) if finding and anchor.get("text") == finding.get("text")
                        else _reader_text(anchor.get("text")))
        bundle["claim"] = display_text
        evidence[bundle["id"]] = bundle
        reader_anchors.append({
            "id": anchor_id,
            "finding_id": _text(anchor.get("finding_id")),
            "kind": _text(anchor.get("kind"), "assertion"),
            "text": display_text,
            "support_status": _text(anchor.get("support_status"), "unresolved"),
            "support_label": _precise_binding_label(
                _text(anchor.get("support_status")), bool(anchor.get("required_verifier_findings"))),
            "evidence_id": bundle["id"],
            "reference_keys": [":".join(_reference_key(reference)) for reference in references],
            "semantic_review": deepcopy(anchor.get("semantic_review")),
            **({"required_verifier_findings": _required_verifier_findings(
                anchor.get("required_verifier_findings"))}
               if anchor.get("required_verifier_findings") else {}),
        })

    payload = {
        "schema_version": SCHEMA_VERSION,
        "report": {
            "id": report_id,
            "version_id": version_id,
            "title": title,
            "status": "needs_review",
            "status_label": "Pending human approval",
            "conversation_id": artifact.get("conversation_id"),
            "message_id": artifact.get("message_id"),
            "run_id": artifact.get("run_id"),
            "artifact_id": artifact.get("artifact_id"),
            "source_schema_version": artifact.get("schema_version"),
        },
        "question": _question_payload(artifact),
        "chapters": chapters,
        "anchors": reader_anchors,
        "evidence": evidence,
        "sources": _sources(artifact),
        **({"required_verifier_findings": _required_verifier_findings(
            artifact.get("required_verifier_findings"))}
           if artifact.get("required_verifier_findings") else {}),
        "audit": {
            "available": bool(artifact.get("audit")),
            "trace_count": len(traces),
            "calculation_count": len(_list(artifact.get("calculations"))),
            "dataset_count": len(_list(artifact.get("datasets"))),
            "semantic_review": deepcopy(_dict(_dict(artifact.get("verification")).get("semantic_review"))),
        },
    }
    payload["payload_sha256"] = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
    return payload


def frozen_trace_details(payload: dict, artifact: dict, anchor_id: str) -> dict:
    """Supplement a frozen payload from its SAME artifact, never re-run calculations.

    Older payload compilers omitted formula/inputs. Keep their bytes unchanged and
    expose this separately labelled reading projection of the already-frozen record.
    """
    report = _dict(payload.get("report"))
    if any(report.get(key) != artifact.get(key) for key in ("artifact_id", "conversation_id", "message_id")):
        raise ValueError("frozen evidence artifact identity mismatch")
    evidence = build_reader_evidence_from_payload(payload, anchor_id)["evidence"]
    ids = {trace["id"] for trace in evidence["traces"]}
    calculations = {item.get("calculation_id"): item for item in _list(artifact.get("calculations"))
                    if isinstance(item, dict) and item.get("status") == "recorded"}
    return {
        "origin": "same_frozen_artifact", "artifact_id": artifact["artifact_id"],
        "traces": [_trace_summary(trace, calculations.get(_dict(trace.get("reference")).get("id")), include_identity=True)
                   for trace in _list(artifact.get("traces")) if trace.get("id") in ids],
    }


def build_reader_evidence(artifact: dict, anchor_id: str) -> dict:
    return build_reader_evidence_from_payload(build_reader_payload(artifact), anchor_id)


def build_reader_evidence_from_payload(payload: dict, anchor_id: str) -> dict:
    if not isinstance(anchor_id, str) or not anchor_id:
        raise KeyError("anchor_id")
    payload = _dict(payload)
    anchor = next((item for item in payload["anchors"] if item["id"] == anchor_id), None)
    if anchor is None:
        raise KeyError(anchor_id)
    bundle = payload["evidence"].get(anchor["evidence_id"])
    if bundle is None:
        raise KeyError(anchor_id)
    return {
        "schema_version": payload["schema_version"],
        "report": deepcopy(payload["report"]),
        "anchor": deepcopy(anchor),
        "evidence": deepcopy(bundle),
    }


def build_reader_source(artifact: dict, source_id: str) -> dict:
    return build_reader_source_from_payload(build_reader_payload(artifact), source_id)


def build_reader_source_from_payload(payload: dict, source_id: str) -> dict:
    if not isinstance(source_id, str) or not source_id:
        raise KeyError("source_id")
    payload = _dict(payload)
    source = next((item for item in payload["sources"] if item["id"] == source_id), None)
    if source is None:
        raise KeyError(source_id)
    related = []
    source_sha = source.get("source_sha256")
    # Legacy fact sources can name the exact same bytes as a registered document.
    # Reuse only an identity already frozen in THIS report, never a URL/title match
    # or today's document catalog. Actual download still verifies the answer snapshot.
    raw_document = next((item for item in payload["sources"]
                         if source_sha and item.get("source_sha256") == source_sha
                         and item.get("document_version_id")), None)
    for bundle in payload["evidence"].values():
        for trace in _list(bundle.get("traces")):
            trace_source = _dict(trace.get("source"))
            if (trace_source.get("source_id") == source_id
                    or trace_source.get("document_version_id") == source_id
                    or (source_sha and trace_source.get("source_sha256") == source_sha)):
                related.append({
                    "anchor_id": bundle["anchor_id"],
                    "claim": bundle["claim"],
                    "trace_id": trace.get("id"),
                    "quote": trace_source.get("quote"),
                    "context": trace_source.get("context"),
                    "locator": trace_source.get("locator"),
                })
    return {
        "schema_version": payload["schema_version"],
        "report": deepcopy(payload["report"]),
        "source": deepcopy(source),
        "raw_download_available": raw_document is not None,
        "raw_download_href": (
            f'/api/reports/raw-source?report_id={payload["report"]["id"]}&source_id={source["id"]}'
            if raw_document is not None else None
        ),
        "related_evidence": related,
    }


def _diff_reference(value) -> dict:
    reference = _dict(value)
    return {"kind": _text(reference.get("kind")), "id": _text(reference.get("id"))}


def _diff_claims(artifact: dict) -> list[dict]:
    """Project exact claim semantics and bindings without guessing cross-version identity."""
    projected = []
    publication = _dict(artifact.get("publication"))
    title = _text(publication.get("title"))
    if title:
        projected.append({"object_kind": "report_title", "text": title})

    anchors_by_finding: dict[str, list[dict]] = {}
    for value in _list(artifact.get("anchors")):
        anchor = _dict(value)
        finding_id = _text(anchor.get("finding_id"))
        if not finding_id or "value_formation" not in anchor:
            continue
        anchors_by_finding.setdefault(finding_id, []).append({
            "text": _text(anchor.get("text")),
            "value_formation": _text(anchor.get("value_formation")),
            "attribution": deepcopy(anchor.get("attribution")),
            "references": _sorted_values([
                _diff_reference(reference) for reference in _list(anchor.get("references"))
            ]),
            "calculation_id": anchor.get("calculation_id"),
        })

    for value in _list(artifact.get("findings")):
        item = _dict(value)
        finding_id = _text(item.get("id"))
        projected.append({
            "object_kind": "claim",
            "title": _text(item.get("title")),
            "text": _text(item.get("text")),
            "classification": _text(item.get("classification")),
            "reasoning": _text(item.get("reasoning")),
            "limitations": deepcopy(_list(item.get("limitations"))),
            "references": _sorted_values([
                _diff_reference(reference) for reference in _list(item.get("references"))
            ]),
            "value_bindings": _sorted_values(anchors_by_finding.get(finding_id, [])),
        })

    for value in _list(artifact.get("datasets")):
        dataset = _dict(value)
        rows = []
        for row_value in _list(dataset.get("rows")):
            row = _dict(row_value)
            rows.append({
                key: deepcopy(row.get(key)) for key in (
                    "label", "value", "unit", "basis", "period", "reference",
                    "value_formation", "attribution", "record_kind", "company_id", "metric",
                ) if row.get(key) is not None
            })
        projected.append({
            "object_kind": "dataset",
            "title": _text(dataset.get("title")),
            "description": _text(dataset.get("description")),
            "limitations": deepcopy(_list(dataset.get("limitations"))),
            "rows": _sorted_values(rows),
        })
    return projected


def _diff_evidence(artifact: dict) -> list[dict]:
    """Project frozen evidence identities without interpreting their support."""
    projected = []
    identity_fields = (
        "source_id", "source_sha256", "document_version_id", "fact_id", "evidence_id",
        "query_id", "snapshot_id", "source_key", "source_version", "locator",
    )
    for value in _list(artifact.get("traces")):
        trace = _dict(value)
        reference = _dict(trace.get("reference"))
        kind, identifier = _text(reference.get("kind")), _text(reference.get("id"))
        if not kind or not identifier or kind == "calculation":
            continue
        source = _dict(trace.get("source"))
        projected.append({
            "reference": {"kind": kind, "id": identifier},
            "frozen_identity": {
                key: deepcopy(source[key]) for key in identity_fields if source.get(key) is not None
            },
        })
    return projected


def _diff_calculations(artifact: dict) -> list[dict]:
    return [deepcopy(item) for item in _list(artifact.get("calculations"))
            if isinstance(item, dict) and _text(item.get("calculation_id"))]


def _diff_limitations(artifact: dict) -> list[dict]:
    projected = []

    def add(values, scope: str) -> None:
        for value in _list(values):
            if isinstance(value, dict):
                text = _text(value.get("text"))
                item_scope = _text(value.get("scope"), scope)
            else:
                text, item_scope = _text(value), scope
            if text:
                projected.append({"scope": item_scope, "text": text})

    add(artifact.get("limitations"), "report")
    add(_dict(artifact.get("verification")).get("limitations"), "verification")
    for finding in _list(artifact.get("findings")):
        add(_dict(finding).get("limitations"), "claim")
    return projected


def _sorted_values(values: list[dict]) -> list[dict]:
    return sorted((deepcopy(value) for value in values), key=_json)


def _exact_section(old_values: list[dict], new_values: list[dict]) -> dict:
    """Return a deterministic exact multiset diff without semantic pairing."""
    old_groups: dict[str, list[dict]] = {}
    new_groups: dict[str, list[dict]] = {}
    for value in old_values:
        old_groups.setdefault(_json(value), []).append(value)
    for value in new_values:
        new_groups.setdefault(_json(value), []).append(value)
    added, removed, unchanged = [], [], []
    for identifier in sorted(set(old_groups) | set(new_groups)):
        old_items = _sorted_values(old_groups.get(identifier, []))
        new_items = _sorted_values(new_groups.get(identifier, []))
        shared = min(len(old_items), len(new_items))
        unchanged.extend(old_items[:shared])
        removed.extend(old_items[shared:])
        added.extend(new_items[shared:])
    return {"added": _sorted_values(added), "removed": _sorted_values(removed),
            "unchanged": _sorted_values(unchanged)}


def _diff_report_identity(payload: dict, artifact: dict) -> dict:
    report = _dict(payload.get("report"))
    return {
        "report_id": _text(report.get("id")),
        "version_id": _text(report.get("version_id")),
        "conversation_id": _text(report.get("conversation_id")),
        "message_id": _text(report.get("message_id")),
        "artifact_id": _text(artifact.get("artifact_id")),
        "payload_sha256": _text(payload.get("payload_sha256")),
    }


def build_report_diff(old_payload: dict, old_artifact: dict,
                      new_payload: dict, new_artifact: dict, *,
                      direct_corrections: list[dict] | None = None) -> dict:
    """Compare two complete frozen closures without mutating or semantically judging them."""
    old_identity = _diff_report_identity(old_payload, old_artifact)
    new_identity = _diff_report_identity(new_payload, new_artifact)
    if not old_identity["report_id"] or not new_identity["report_id"]:
        raise ValueError("report diff requires frozen report identities")
    if old_identity["conversation_id"] != new_identity["conversation_id"]:
        raise ValueError("report diff requires reports from the same conversation")

    sections = {
        "claims": _exact_section(_diff_claims(old_artifact), _diff_claims(new_artifact)),
        "evidence": _exact_section(_diff_evidence(old_artifact), _diff_evidence(new_artifact)),
        "calculations": _exact_section(
            _diff_calculations(old_artifact), _diff_calculations(new_artifact)),
        "limitations": _exact_section(_diff_limitations(old_artifact), _diff_limitations(new_artifact)),
    }
    section_summary = {
        name: {status: len(section[status]) for status in ("added", "removed", "unchanged")}
        for name, section in sections.items()
    }
    result = {
        "schema_version": REPORT_DIFF_SCHEMA_VERSION,
        "from": old_identity,
        "to": new_identity,
        "direct_corrections": _sorted_values(list(direct_corrections or [])),
        "sections": sections,
        "summary": {
            status: sum(item[status] for item in section_summary.values())
            for status in ("added", "removed", "unchanged")
        } | {"sections": section_summary},
        "algorithm_version": REPORT_DIFF_ALGORITHM_VERSION,
    }
    result["diff_sha256"] = hashlib.sha256(_json(result).encode("utf-8")).hexdigest()
    return result


def load_report_diff(chat, *, old_report_id: str, new_report_id: str) -> dict:
    """Load two complete formal closures and their direct correction relationship."""
    from .chat import ChatError
    old = resolve_report(chat, report_id=old_report_id)
    new = resolve_report(chat, report_id=new_report_id)
    if old["conversation_id"] != new["conversation_id"]:
        raise ChatError("report_diff_conversation_mismatch",
                        "Reports must belong to the same conversation", 400)
    old_payload, old_artifact = load_required_reader_snapshots(chat, report_id=old["report_id"])
    new_payload, new_artifact = load_required_reader_snapshots(chat, report_id=new["report_id"])
    index = list_reader_reports(chat, old["conversation_id"])
    corrections = [correction for report in index["reports"]
                   if report["report_id"] == new["report_id"]
                   for correction in report["corrections"]
                   if correction["old_report_id"] == old["report_id"]
                   and correction["new_report_id"] == new["report_id"]]
    return build_report_diff(old_payload, old_artifact, new_payload, new_artifact,
                             direct_corrections=corrections)


def list_reader_reports(chat, conversation_id: str) -> dict:
    from .chat import ChatError
    conversation_id = chat._valid_id(conversation_id, "conversation_id")
    with closing(chat._connect()) as connection:
        exists = connection.execute(
            "SELECT title,company_id FROM conversations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()
        if exists is None:
            raise ChatError("conversation_not_found", "conversation does not exist", 404)
        rows = connection.execute(
            "SELECT m.message_id,m.run_id,m.content,m.created_at,m.verification_json FROM messages m "
            "JOIN report_publications p ON p.message_id=m.message_id "
            "WHERE m.conversation_id=? AND m.role='assistant' AND m.status='completed' "
            "ORDER BY m.created_at,m.rowid",
            (conversation_id,),
        ).fetchall()
    skipped_reports: list[dict] = []
    valid_report_ids: set[str] = set()
    if rows:
        for row in rows:
            report_id = report_id_for(conversation_id, row["message_id"])
            try:
                load_required_reader_snapshots(chat, report_id=report_id)
                valid_report_ids.add(report_id)
            except ChatError as exc:
                if exc.status_code < 400 or exc.status_code >= 500:
                    raise
                skipped_reports.append({
                    "message_id": row["message_id"],
                    "reason": exc.code,
                })
    with closing(chat._connect()) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_reader_versions'"
        ).fetchone()
        stored = (connection.execute(
            "SELECT r.* FROM report_reader_versions r "
            "JOIN report_publications p ON p.message_id=r.message_id "
            "WHERE r.conversation_id=? ORDER BY r.version_number,r.created_at,r.report_id",
            (conversation_id,),
        ).fetchall() if table is not None else [])
    stored = [row for row in stored if row["report_id"] in valid_report_ids]
    versions = [{
        "report_id": row["report_id"],
        "version": row["version_number"],
        "version_id": row["version_id"],
        "conversation_id": row["conversation_id"],
        "message_id": row["message_id"],
        "run_id": row["run_id"],
        "title": row["title"],
        "status": row["status"],
        "status_label": row["status_label"],
        "publication_schema": row["publication_schema"],
        "payload_sha256": row["payload_sha256"],
        "registered_at": row["registered_at"],
        "reader_href": row["reader_href"],
    } for row in stored]
    # Relationships are a current append-only index projection, NOT part of a frozen
    # payload. Discovering a later correction must never rewrite an older version.
    by_message = {item["message_id"]: item for item in versions}
    for item in versions:
        item.update(corrected_by=[], corrections=[])
    with closing(chat._connect()) as connection:
        historical = [dict(row) for row in chat.report_versions._reports(connection, conversation_id)]
        projected = chat.report_versions.project(connection, conversation_id, historical)
    for history in projected:
        for correction in history["corrections"]:
            old = by_message.get(correction["old_message_id"])
            new = by_message.get(correction["new_message_id"])
            # A completed legacy answer or failed/unpublished successor is not a
            # formal reader version. Do not manufacture report links for either.
            if old is None or new is None:
                continue
            relation = {**correction,
                        "old_report_id": old["report_id"], "new_report_id": new["report_id"],
                        "old_reader_href": old["reader_href"], "new_reader_href": new["reader_href"]}
            old["corrected_by"].append(relation)
            new["corrections"].append(relation)
    return {
        "schema_version": "report-index/1",
        "conversation_id": conversation_id,
        "company_id": exists["company_id"],
        "title": exists["title"],
        "reports": versions,
        "skipped_reports": skipped_reports,
    }
