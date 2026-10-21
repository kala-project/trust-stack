"""Read-only projection of canonical verification candidates into the report reader.

Candidates are not formal reports.  This adapter preserves the canonical publication text
and stable candidate/version identities, exposes no evidence capability that the public
candidate APIs cannot actually serve, and never writes a reader or publication record.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Any

from .report_reader import SCHEMA_VERSION


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _text(value: Any, fallback: str = "") -> str:
    return value if isinstance(value, str) else fallback


def candidate_reader_payload(candidate: dict, version: dict) -> dict:
    """Project one immutable candidate version without creating a formal identity."""
    if candidate.get("candidate_id") != version.get("candidate_id"):
        raise ValueError("candidate version identity mismatch")
    publication = _dict(version.get("publication"))
    title = _text(publication.get("title"), "Candidate research report")
    question = _text(candidate.get("question"))
    blocks = []
    limitations = []
    for index, raw in enumerate(_list(publication.get("blocks")), 1):
        block = _dict(raw)
        block_id = _text(block.get("id"), f"candidate-block-{index}")
        text = _text(block.get("text"))
        block_limits = [item for item in _list(block.get("limitations")) if isinstance(item, str) and item]
        limitations.extend(block_limits)
        blocks.append({
            "id": block_id,
            "type": "finding",
            "title": "Candidate conclusion" if index == 1 else f"Candidate analysis {index}",
            "classification": _text(block.get("classification"), "interpretation"),
            "text": text,
            "status": "pending_verification",
            "status_label": "Awaiting independent verification",
            "anchor_ids": [],
            "reasoning": _text(block.get("reasoning")),
            "limitations": block_limits,
            "review_flags": [],
        })
    if not blocks:
        blocks.append({
            "id": "candidate-content-unavailable", "type": "notice", "title": "Candidate content is incomplete",
            "text": "The candidate version has no structured content to display.", "anchor_ids": [],
        })
    status = _text(candidate.get("status"), "unresolved")
    status_label = {
        "queued": "Awaiting verification", "running": "Verification in progress", "verified": "Verification complete",
        "verified_with_limitations": "Verification complete (with limitations)", "unresolved": "Verification incomplete",
    }.get(status, "Verification status unknown")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "report": {
            "id": candidate["candidate_id"],
            "version_id": version["version_id"],
            "title": title,
            "status": "pending_verification" if status in {"queued", "running"} else status,
            "status_label": status_label,
            "kind": "verification_candidate",
            "conversation_id": candidate.get("conversation_id"),
            "message_id": None,
            "run_id": candidate.get("author_run_id"),
            "artifact_id": None,
            "source_schema_version": publication.get("schema_version"),
            "candidate_id": candidate["candidate_id"],
            "candidate_version_id": version["version_id"],
            "formal_message_id": candidate.get("formal_message_id"),
        },
        "question": {
            "original": question, "normalized": "", "normalization": [],
            "open_uncertainties": [], "scope": deepcopy(_dict(candidate.get("scope"))),
            "assumptions": [], "scope_gaps": [], "clarifications": [],
            "time_choice": None, "latest_user_question": question,
            "snapshot_schema_version": None,
        },
        "chapters": [
            {"id": "question-conclusion", "title": "Question and candidate conclusion", "blocks": [
                {"id": "research-question", "type": "question", "title": "Research question",
                 "text": question, "anchor_ids": []},
                blocks[0],
            ]},
            {"id": "analysis-findings", "title": "Candidate analysis", "blocks": blocks[1:] or [{
                "id": "candidate-analysis-pending", "type": "notice", "title": "Verification in progress",
                "text": "This content is an author candidate draft; verification may revise it or determine that it is incomplete.", "anchor_ids": [],
            }]},
            {"id": "evidence-sources", "title": "Evidence availability", "blocks": [{
                "id": "candidate-source-boundary", "type": "notice", "title": "Evidence viewing is not yet available",
                "text": "Candidate records preserve only frozen citation identities. They are not presented as openable, supported point-level evidence until formal verification is complete.",
                "anchor_ids": [],
            }]},
            {"id": "boundaries-method", "title": "Boundaries and methods", "blocks": [{
                "id": "limitations", "type": "limitations", "title": "Content limitations",
                "items": list(dict.fromkeys(limitations)),
                "text": "；".join(dict.fromkeys(limitations)) if limitations else "Candidate content is still awaiting independent verification.",
            }]},
        ],
        "anchors": [], "evidence": {}, "sources": [],
        "audit": {
            "available": False, "trace_count": 0, "calculation_count": 0,
            "dataset_count": len(_list(publication.get("datasets"))),
            "semantic_review": {}, "verification_pending": status in {"queued", "running"},
        },
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    payload["payload_sha256"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return payload
