"""Durable candidate, verification, and atomic formal-publication boundary.

This module stores only explicit model outputs and sanitized tool-result references.  It
never stores hidden reasoning and does not execute models or render reader payloads.
Feature schema creation is deliberately explicit through :meth:`initialize_feature`.
"""
from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import sqlite3
import time
import uuid
from typing import Any

from .job_queue import JobClaim, JobQueue
from .report_publications import ReportPublications, VERSION as PUBLICATION_VERSION
from .research_records import ResearchRecordError, ResearchRecordStore, thaw_object
from .candidate_contracts import ArtifactRef, AuthorInputs, CandidateSubmission
from .publication_contracts import CanonicalPublication
from .verification_contracts import (
    VerificationFinding, PreparedPublication, RequiredFinding, FINDING_OUTCOMES, TERMINAL_OUTCOMES,
    completed_check_nodes, verification_checks,
)


MAX_JSON_CHARS = 1_000_000
MAX_TEXT_CHARS = 20_000


class VerificationStoreError(RuntimeError):
    """Base error for safe, explicit verifier persistence failures."""


class VerificationFeatureNotInitialized(VerificationStoreError):
    pass


class VerificationIntegrityError(VerificationStoreError):
    pass


class VerificationBudgetExceeded(VerificationStoreError):
    """A durable resource limit was reached; this is not evidence corruption."""

    def __init__(self, stage: str, code: str, requested_size: int):
        super().__init__("verification cumulative budget exhausted")
        self.stage = stage
        self.code = code
        self.requested_size = requested_size


class VerificationConflict(VerificationStoreError):
    pass


class VerificationNotFound(VerificationStoreError):
    pass


@dataclass(frozen=True)
class VerificationCheckpoint:
    candidate_id: str
    version_id: str
    checkpoint_key: str
    covered_node_ids: tuple[str, ...]
    remaining_node_ids: tuple[str, ...]
    tool_result_refs: tuple[ArtifactRef, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class VerificationToolReceipt:
    candidate_id: str
    version_id: str
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    result: dict[str, Any]


@dataclass(frozen=True)
class RevisedCandidate:
    candidate_id: str
    base_version_id: str
    publication: CanonicalPublication
    changed_node_ids: tuple[str, ...]
    revision_summary: str

    def __post_init__(self) -> None:
        if type(self.publication) is not CanonicalPublication:
            raise TypeError("revision publication must be CanonicalPublication")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS verification_feature(
 feature TEXT PRIMARY KEY,
 schema_version INTEGER NOT NULL,
 initialized_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS report_current(
 conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id),
 message_id TEXT REFERENCES messages(message_id),
 updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verification_candidates(
 candidate_id TEXT PRIMARY KEY,
 conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
 author_run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
 author_job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
 verify_job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
 question TEXT NOT NULL,
 scope_json TEXT NOT NULL,
 input_snapshot_json TEXT NOT NULL,
 base_message_id TEXT REFERENCES messages(message_id),
 evidence_refs_json TEXT NOT NULL,
 calculation_refs_json TEXT NOT NULL,
 initial_version_id TEXT NOT NULL UNIQUE,
 input_hash TEXT NOT NULL UNIQUE,
 owner_scope TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidate_versions(
 version_id TEXT PRIMARY KEY,
 candidate_id TEXT NOT NULL REFERENCES verification_candidates(candidate_id),
 version_number INTEGER NOT NULL CHECK(version_number >= 1),
 parent_version_id TEXT REFERENCES candidate_versions(version_id),
 publication_json TEXT NOT NULL,
 publication_sha256 TEXT NOT NULL,
 nodes_json TEXT NOT NULL,
 dependency_hash TEXT NOT NULL,
 created_by_attempt_id TEXT REFERENCES job_attempts(attempt_id),
 changed_node_ids_json TEXT NOT NULL,
 revision_summary TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(candidate_id,version_number)
);
CREATE TABLE IF NOT EXISTS verification_continuations(
 continuation_id TEXT PRIMARY KEY,
 candidate_id TEXT NOT NULL REFERENCES verification_candidates(candidate_id),
 previous_job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
 verify_job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
 target_version_id TEXT NOT NULL REFERENCES candidate_versions(version_id),
 request_identity TEXT NOT NULL UNIQUE,
 input_hash TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verification_findings(
 finding_id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 attempt_id TEXT NOT NULL REFERENCES job_attempts(attempt_id),
 candidate_id TEXT NOT NULL REFERENCES verification_candidates(candidate_id),
 version_id TEXT NOT NULL REFERENCES candidate_versions(version_id),
 node_id TEXT NOT NULL,
 node_hash TEXT NOT NULL,
 dependency_hash TEXT NOT NULL,
 check_kind TEXT NOT NULL CHECK(check_kind IN ('source_identity','temporal_alignment','numeric_reproducibility','semantic_support','limitation_disclosure')),
 outcome TEXT NOT NULL CHECK(outcome IN ('supported','corrected_and_rechecked','conflict_disclosed','insufficient_material_disclosed','unchecked','unresolved')),
 explanation TEXT NOT NULL,
 tool_result_refs_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(attempt_id,version_id,node_id,check_kind)
);
CREATE TABLE IF NOT EXISTS verification_checkpoints(
 checkpoint_id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 attempt_id TEXT NOT NULL REFERENCES job_attempts(attempt_id),
 candidate_id TEXT NOT NULL REFERENCES verification_candidates(candidate_id),
 version_id TEXT NOT NULL REFERENCES candidate_versions(version_id),
 publication_sha256 TEXT NOT NULL,
 dependency_hash TEXT NOT NULL,
 checkpoint_key TEXT NOT NULL,
 covered_node_ids_json TEXT NOT NULL,
 remaining_node_ids_json TEXT NOT NULL,
 tool_result_refs_json TEXT NOT NULL,
 note TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(attempt_id,checkpoint_key)
);
CREATE TABLE IF NOT EXISTS verification_tool_receipts(
 receipt_id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 attempt_id TEXT NOT NULL REFERENCES job_attempts(attempt_id),
 candidate_id TEXT NOT NULL REFERENCES verification_candidates(candidate_id),
 version_id TEXT NOT NULL REFERENCES candidate_versions(version_id),
 call_id TEXT NOT NULL,
 tool_name TEXT NOT NULL,
 arguments_json TEXT NOT NULL,
 result_json TEXT NOT NULL,
 result_sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(attempt_id,call_id)
);
CREATE TABLE IF NOT EXISTS verification_control_diagnostics(
 diagnostic_id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL REFERENCES jobs(job_id),
 attempt_id TEXT NOT NULL REFERENCES job_attempts(attempt_id),
 candidate_id TEXT NOT NULL REFERENCES verification_candidates(candidate_id),
 operation TEXT NOT NULL,
 error_code TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verification_records(
 verification_id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
 attempt_id TEXT NOT NULL REFERENCES job_attempts(attempt_id),
 candidate_id TEXT NOT NULL REFERENCES verification_candidates(candidate_id),
 version_id TEXT NOT NULL REFERENCES candidate_versions(version_id),
 publication_sha256 TEXT NOT NULL,
 dependency_hash TEXT NOT NULL,
 input_hash TEXT NOT NULL,
 outcome TEXT NOT NULL CHECK(outcome IN ('verified','verified_with_limitations','unresolved')),
 coverage_json TEXT NOT NULL,
 limitations_json TEXT NOT NULL,
 formal_message_id TEXT UNIQUE REFERENCES messages(message_id),
 commit_hash TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(job_id,input_hash)
);
CREATE TABLE IF NOT EXISTS verification_author_inputs(
 candidate_id TEXT PRIMARY KEY REFERENCES verification_candidates(candidate_id),
 inputs_json TEXT NOT NULL,
 inputs_sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verification_usage(
 job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
 provider_events INTEGER NOT NULL DEFAULT 0,
 output_chars INTEGER NOT NULL DEFAULT 0,
 payload_bytes INTEGER NOT NULL DEFAULT 0,
 tool_calls INTEGER NOT NULL DEFAULT 0,
 elapsed_seconds REAL NOT NULL DEFAULT 0,
 active_attempt_id TEXT,
 active_started_at REAL,
 exhaustion_stage TEXT,
 exhaustion_code TEXT,
 exhaustion_requested_size INTEGER
);
CREATE TRIGGER IF NOT EXISTS verification_candidates_no_update BEFORE UPDATE ON verification_candidates
BEGIN SELECT RAISE(ABORT,'verification candidates are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verification_candidates_no_delete BEFORE DELETE ON verification_candidates
BEGIN SELECT RAISE(ABORT,'verification candidates are immutable'); END;
CREATE TRIGGER IF NOT EXISTS candidate_versions_no_update BEFORE UPDATE ON candidate_versions
BEGIN SELECT RAISE(ABORT,'candidate versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS candidate_versions_no_delete BEFORE DELETE ON candidate_versions
BEGIN SELECT RAISE(ABORT,'candidate versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verification_continuations_candidate_version BEFORE INSERT ON verification_continuations
WHEN NOT EXISTS(SELECT 1 FROM candidate_versions v WHERE v.version_id=NEW.target_version_id AND v.candidate_id=NEW.candidate_id)
BEGIN SELECT RAISE(ABORT,'verification continuation candidate version differs'); END;
CREATE TRIGGER IF NOT EXISTS verification_continuations_no_update BEFORE UPDATE ON verification_continuations
BEGIN SELECT RAISE(ABORT,'verification continuations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verification_continuations_no_delete BEFORE DELETE ON verification_continuations
BEGIN SELECT RAISE(ABORT,'verification continuations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verification_findings_no_update BEFORE UPDATE ON verification_findings
BEGIN SELECT RAISE(ABORT,'verification findings are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verification_findings_no_delete BEFORE DELETE ON verification_findings
BEGIN SELECT RAISE(ABORT,'verification findings are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verification_checkpoints_no_update BEFORE UPDATE ON verification_checkpoints
BEGIN SELECT RAISE(ABORT,'verification checkpoints are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verification_checkpoints_no_delete BEFORE DELETE ON verification_checkpoints
BEGIN SELECT RAISE(ABORT,'verification checkpoints are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verification_tool_receipts_no_update BEFORE UPDATE ON verification_tool_receipts
BEGIN SELECT RAISE(ABORT,'verification tool receipts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verification_tool_receipts_no_delete BEFORE DELETE ON verification_tool_receipts
BEGIN SELECT RAISE(ABORT,'verification tool receipts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verification_control_diagnostics_no_update BEFORE UPDATE ON verification_control_diagnostics
BEGIN SELECT RAISE(ABORT,'verification control diagnostics are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verification_control_diagnostics_no_delete BEFORE DELETE ON verification_control_diagnostics
BEGIN SELECT RAISE(ABORT,'verification control diagnostics are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verification_records_no_update BEFORE UPDATE ON verification_records
BEGIN SELECT RAISE(ABORT,'verification records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verification_records_no_delete BEFORE DELETE ON verification_records
BEGIN SELECT RAISE(ABORT,'verification records are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verification_author_inputs_no_update BEFORE UPDATE ON verification_author_inputs
BEGIN SELECT RAISE(ABORT,'verification author inputs are immutable'); END;
CREATE TRIGGER IF NOT EXISTS verification_author_inputs_no_delete BEFORE DELETE ON verification_author_inputs
BEGIN SELECT RAISE(ABORT,'verification author inputs are immutable'); END;
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: Any, name: str = "value", maximum: int = MAX_JSON_CHARS) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be finite JSON") from exc
    if len(raw) > maximum:
        raise ValueError(f"{name} exceeds {maximum} characters")
    return raw


def _hash_raw(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _reject_hidden_thoughts(value: Any, name: str) -> None:
    forbidden = {"chain_of_thought", "hidden_thoughts", "reasoning_trace", "scratchpad"}
    if isinstance(value, dict):
        if any(type(key) is str and key.lower() in forbidden for key in value):
            raise ValueError(f"{name} cannot contain hidden model reasoning")
        for item in value.values():
            _reject_hidden_thoughts(item, name)
    elif isinstance(value, list):
        for item in value:
            _reject_hidden_thoughts(item, name)


def _text(value: str, name: str, maximum: int = MAX_TEXT_CHARS, *, empty: bool = False) -> str:
    if type(value) is not str or len(value) > maximum or (not empty and not value.strip()):
        raise TypeError(f"{name} must be {'0' if empty else '1'}..{maximum} characters")
    return value


def _identifier(value: str, name: str) -> str:
    return _text(value, name, 200)


def _refs(values: tuple[ArtifactRef, ...], name: str) -> list[dict[str, str]]:
    if type(values) is not tuple or len(values) > 10_000:
        raise TypeError(f"{name} must be a tuple with at most 10000 references")
    result: list[dict[str, str]] = []
    seen = set()
    for value in values:
        if type(value) is not ArtifactRef:
            raise TypeError(f"{name} entries must be ArtifactRef")
        kind = _identifier(value.kind, f"{name}.kind")
        identifier = _identifier(value.identifier, f"{name}.identifier")
        if type(value.sha256) is not str or len(value.sha256) != 64:
            raise TypeError(f"{name}.sha256 must be a lowercase SHA-256")
        try:
            int(value.sha256, 16)
        except ValueError as exc:
            raise TypeError(f"{name}.sha256 must be a lowercase SHA-256") from exc
        if value.sha256 != value.sha256.lower():
            raise TypeError(f"{name}.sha256 must be a lowercase SHA-256")
        key = (kind, identifier, value.sha256)
        if key in seen:
            continue
        seen.add(key)
        result.append({"kind": kind, "id": identifier, "sha256": value.sha256})
    result.sort(key=lambda item: (item["kind"], item["id"], item["sha256"]))
    return result


def _tuple_strings(values: tuple[str, ...], name: str, maximum: int = 10_000) -> list[str]:
    if type(values) is not tuple or len(values) > maximum:
        raise TypeError(f"{name} must be a bounded tuple")
    result = []
    for value in values:
        result.append(_identifier(value, name))
    if len(set(result)) != len(result):
        raise ValueError(f"{name} cannot contain duplicates")
    return result


def _publication(value: CanonicalPublication) -> tuple[dict[str, Any], str, str]:
    if type(value) is not CanonicalPublication:
        raise TypeError("publication must be CanonicalPublication")
    publication = value.to_json()
    if publication.get("schema_version") != PUBLICATION_VERSION:
        raise VerificationIntegrityError("candidate uses an unsupported publication schema")
    _text(publication.get("answer"), "publication.answer", MAX_JSON_CHARS)
    if type(publication.get("blocks")) is not list or not publication["blocks"]:
        raise VerificationIntegrityError("publication must contain canonical report blocks")
    raw = _json(publication, "publication")
    return publication, raw, _hash_raw(raw)


def _nodes(publication: dict[str, Any]) -> tuple[dict[str, dict[str, str]], str]:
    """Derive stable structural node and dependency hashes without semantic inference."""
    nodes: dict[str, dict[str, str]] = {}

    def add(node_id: str, content: Any, dependencies: Any) -> None:
        _identifier(node_id, "node_id")
        if node_id in nodes:
            raise VerificationIntegrityError("publication contains duplicate content node identities")
        content_raw = _json(content, "node content")
        dependency_raw = _json(dependencies, "node dependencies")
        nodes[node_id] = {
            "node_hash": _hash_raw(content_raw),
            "dependency_hash": _hash_raw(dependency_raw),
        }

    add("report-title", publication.get("title", ""), [])
    for index, block in enumerate(publication["blocks"], 1):
        node_id = block.get("block_id") or f"report-block-{index}"
        dependencies = {
            "support": block.get("support", []),
            "bindings": [span.get("binding") for span in block.get("spans", []) if span.get("binding")],
        }
        add(node_id, block, dependencies)
    for index, dataset in enumerate(publication.get("datasets", []), 1):
        add(f"report-dataset-{index}", dataset,
            {"points": [point.get("binding") for point in dataset.get("points", [])],
             "support": dataset.get("support", [])})
    nodes_raw = _json(nodes, "publication nodes")
    return nodes, _hash_raw(nodes_raw)


def effective_findings(findings):
    """Latest result per node/check, retaining unresolved independent checks."""
    latest = {}
    for finding in findings:
        latest[(finding["node_id"], finding["check_kind"])] = finding
    return list(latest.values())


def checked_finding_nodes(findings):
    current = effective_findings(findings)
    unresolved = {item["node_id"] for item in current
                  if item["outcome"] in {"unchecked", "unresolved"}}
    return {item["node_id"] for item in current} - unresolved


def required_verifier_projection(findings) -> tuple[RequiredFinding, ...]:
    """Return the public projection of effective, wholly terminal node checks."""
    current = effective_findings(findings)
    checked = checked_finding_nodes(current)
    projected = []
    for finding in current:
        if (finding.get("node_id") not in checked
                or finding.get("outcome") not in TERMINAL_OUTCOMES):
            continue
        receipts = []
        for reference in finding.get("tool_result_refs", []):
            if (not isinstance(reference, dict)
                    or reference.get("kind") != "verification_tool_result"):
                continue
            sha256 = reference.get("sha256")
            if (not isinstance(sha256, str) or len(sha256) != 64
                    or sha256.lower() != sha256
                    or any(character not in "0123456789abcdef" for character in sha256)):
                raise VerificationIntegrityError("verifier receipt identity has an invalid SHA-256")
            receipts.append({
                "receipt_id": _identifier(reference.get("id"), "receipt_id"),
                "sha256": sha256,
            })
        receipts.sort(key=lambda item: (item["receipt_id"], item["sha256"]))
        projected.append({
            "node_id": _identifier(finding.get("node_id"), "node_id"),
            "check_kind": _identifier(finding.get("check_kind"), "check_kind"),
            "outcome": finding["outcome"],
            "explanation": _text(finding.get("explanation"), "explanation"),
            "receipts": receipts,
        })
    projected.sort(key=lambda item: (item["node_id"], item["check_kind"]))
    _json(projected, "required_verifier_findings")
    return tuple(RequiredFinding.decode(item) for item in projected)


class VerificationStore:
    """Owner-scoped persistence and short atomic publication transactions."""

    def __init__(self, connect, jobs: JobQueue, publications: ReportPublications,
                 snapshot_preparer=None):
        self._connect = connect
        self._jobs = jobs
        self._publications = publications
        self._records = ResearchRecordStore(connect)
        self._snapshot_preparer = snapshot_preparer

    def set_snapshot_preparer(self, preparer) -> None:
        """Require a read-only canonical snapshot projection for subsequent formal writes."""
        if self._snapshot_preparer is not None:
            raise RuntimeError("publication snapshot preparer is already configured")
        if not callable(preparer):
            raise TypeError("publication snapshot preparer must be callable")
        self._snapshot_preparer = preparer

    def initialize_feature(self) -> None:
        """Install or additively migrate the verifier schema; never rewrites reports."""
        with closing(self._connect()) as connection, connection:
            connection.executescript(_SCHEMA)
            columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(verification_usage)")}
            additions = {
                "payload_bytes": "INTEGER NOT NULL DEFAULT 0",
                "exhaustion_stage": "TEXT",
                "exhaustion_code": "TEXT",
                "exhaustion_requested_size": "INTEGER",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE verification_usage ADD COLUMN {name} {declaration}")
            connection.execute(
                "INSERT OR IGNORE INTO verification_feature VALUES('durable_verifier',2,?)", (_now(),))
            connection.execute(
                "UPDATE verification_feature SET schema_version=2 "
                "WHERE feature='durable_verifier' AND schema_version=1")

    @staticmethod
    def _require_feature(connection) -> None:
        try:
            row = connection.execute(
                "SELECT schema_version FROM verification_feature WHERE feature='durable_verifier'").fetchone()
        except sqlite3.OperationalError as exc:
            raise VerificationFeatureNotInitialized("durable verifier feature is not initialized") from exc
        if row is None or row[0] != 2:
            raise VerificationFeatureNotInitialized("durable verifier feature is not initialized")

    @staticmethod
    def _latest_formal_message(connection, conversation_id: str) -> str | None:
        row = connection.execute(
            "SELECT m.message_id FROM messages m JOIN report_publications p ON p.message_id=m.message_id "
            "WHERE m.conversation_id=? AND m.role='assistant' AND m.status='completed' "
            "ORDER BY m.created_at DESC,m.rowid DESC LIMIT 1", (conversation_id,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _head_matches(connection, conversation_id: str, expected: str | None) -> bool:
        row = connection.execute(
            "SELECT message_id FROM report_current WHERE conversation_id=?", (conversation_id,)).fetchone()
        return row is not None and row[0] == expected

    def save_candidate_and_enqueue(self, author_claim: JobClaim,
                                   submission: CandidateSubmission) -> dict[str, Any]:
        """Terminalize the author then atomically save its candidate and enqueue verify."""
        if type(submission) is not CandidateSubmission:
            raise TypeError("submission must be CandidateSubmission")
        if type(author_claim) is not JobClaim or author_claim.job_type != "research" or not author_claim.run_id:
            raise TypeError("author_claim must be a research JobClaim with run_id")
        question = _text(submission.question, "question")
        scope = submission.scope.to_json()
        input_snapshot = thaw_object(submission.input_snapshot)
        _reject_hidden_thoughts(scope, "scope")
        _reject_hidden_thoughts(input_snapshot, "input_snapshot")
        scope_raw = _json(scope, "scope")
        inputs_raw = _json(input_snapshot, "input_snapshot")
        base_message_id = submission.base_message_id
        if base_message_id is not None:
            _identifier(base_message_id, "base_message_id")
        request_identity = _identifier(submission.request_identity, "request_identity")
        if type(submission.max_attempts) is not int or not 1 <= submission.max_attempts <= 20:
            raise ValueError("max_attempts must be an integer from 1 to 20")
        if submission.deadline_at is not None and (
                type(submission.deadline_at) not in (int, float)
                or not math.isfinite(submission.deadline_at)):
            raise TypeError("deadline_at must be finite or None")
        publication, publication_raw, publication_hash = _publication(submission.publication)
        nodes, dependency_hash = _nodes(publication)
        nodes_raw = _json(nodes, "publication nodes")
        evidence = _refs(submission.evidence_refs, "evidence_refs")
        calculations = _refs(submission.calculation_refs, "calculation_refs")
        author_inputs = submission.author_inputs.to_json()
        _reject_hidden_thoughts(author_inputs, "author_inputs")
        author_inputs_raw = _json(author_inputs, "author_inputs")
        author_inputs_hash = _hash_raw(author_inputs_raw)
        for item in evidence + calculations:
            value = author_inputs.get(item["kind"], {}).get(item["id"])
            if value is None or _hash_raw(_json(value)) != item["sha256"]:
                raise VerificationIntegrityError("author input differs from its submitted reference digest")
        evidence_raw, calculations_raw = _json(evidence), _json(calculations)
        candidate_id, version_id = _id("candidate"), _id("candidate_version")
        input_raw = _json({
            "candidate_id": candidate_id, "author_run_id": author_claim.run_id,
            "question": question, "scope": scope,
            "input_snapshot": input_snapshot, "base_message_id": base_message_id,
            "publication_sha256": publication_hash, "evidence_refs": evidence,
            "calculation_refs": calculations, "author_inputs_sha256": author_inputs_hash,
        }, "candidate input")
        input_hash = _hash_raw(input_raw)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_feature(connection)
            self._jobs.assert_claim(connection, author_claim)
            if author_claim.conversation_id != connection.execute(
                    "SELECT conversation_id FROM runs WHERE run_id=?", (author_claim.run_id,)).fetchone()[0]:
                raise VerificationConflict("author run ownership differs from its durable claim")
            try:
                formal_snapshot = self._records.read_snapshot_in(connection, author_claim.run_id)
                formal_receipts = self._records.read_receipts(author_claim.run_id)
                if (evidence or calculations) and not formal_receipts:
                    raise VerificationIntegrityError("candidate references have no formal author tool receipts")
                company_ids = submission.scope.company_ids
                self._records.assert_scope(
                    author_claim.run_id, author_claim.conversation_id, tuple(company_ids))
                if formal_snapshot.snapshot_object() != input_snapshot:
                    raise VerificationIntegrityError("candidate snapshot differs from the formal author snapshot")
            except ResearchRecordError as exc:
                raise VerificationIntegrityError(exc.message) from exc
            latest = self._latest_formal_message(connection, author_claim.conversation_id)
            if latest != base_message_id:
                raise VerificationConflict("candidate base is not the current formal report")
            head = connection.execute(
                "SELECT message_id FROM report_current WHERE conversation_id=?",
                (author_claim.conversation_id,)).fetchone()
            if head is None:
                connection.execute("INSERT INTO report_current VALUES(?,?,?)",
                                   (author_claim.conversation_id, latest, _now()))
            elif head[0] != latest:
                raise VerificationConflict("formal report pointer differs from publication history")
            changed = connection.execute(
                "UPDATE runs SET status='completed',finished_at=? WHERE run_id=? AND status='running' "
                "AND cancel_requested=0", (_now(), author_claim.run_id)).rowcount
            if changed != 1:
                raise VerificationConflict("author run is no longer publishable as a candidate")
            self._jobs.succeed_in_transaction(connection, author_claim)
            verify_job = self._jobs.enqueue_verify(
                connection, conversation_id=author_claim.conversation_id,
                request_identity=request_identity, input_hash=input_hash,
                max_attempts=submission.max_attempts, deadline_at=submission.deadline_at)
            created = _now()
            connection.execute(
                "INSERT INTO verification_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (candidate_id, author_claim.conversation_id, author_claim.run_id,
                 author_claim.job_id, verify_job["job_id"], question, scope_raw, inputs_raw,
                 base_message_id, evidence_raw, calculations_raw, version_id, input_hash,
                 author_claim.conversation_id, created))
            connection.execute(
                "INSERT INTO candidate_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (version_id, candidate_id, 1, None, publication_raw, publication_hash,
                 nodes_raw, dependency_hash, None, "[]", "author candidate", created))
            connection.execute(
                "INSERT INTO verification_author_inputs VALUES(?,?,?,?)",
                (candidate_id, author_inputs_raw, author_inputs_hash, created))
            connection.execute(
                "INSERT INTO verification_usage(job_id) VALUES(?)", (verify_job["job_id"],))
            seq = connection.execute(
                "SELECT coalesce(max(seq),0)+1 FROM events WHERE run_id=?", (author_claim.run_id,)).fetchone()[0]
            connection.execute("INSERT INTO events VALUES(?,?,?,?,?)",
                               (author_claim.run_id, seq, "candidate_saved",
                                _json({"candidate_id": candidate_id, "verify_job_id": verify_job["job_id"]}),
                                created))
        return {"candidate_id": candidate_id, "version_id": version_id,
                "verify_job_id": verify_job["job_id"], "input_hash": input_hash,
                "status": "queued"}

    def continue_verification(self, owner_scope: str, candidate_id: str,
                              expected_version_id: str, request_id: str) -> dict[str, Any]:
        """Start a fresh verifier from the latest immutable budget-failed version."""
        owner = _identifier(owner_scope, "owner_scope")
        candidate = _identifier(candidate_id, "candidate_id")
        version_id = _identifier(expected_version_id, "expected_version_id")
        request = _identifier(request_id, "request_id")
        request_identity = f"verification-continuation:{owner}:{request}"
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_feature(connection)
            replay = connection.execute(
                "SELECT vc.*,j.status FROM verification_continuations vc "
                "JOIN jobs j ON j.job_id=vc.verify_job_id WHERE vc.request_identity=?",
                (request_identity,)).fetchone()
            if replay is not None:
                if (replay["candidate_id"] != candidate
                        or replay["target_version_id"] != version_id):
                    raise VerificationConflict(
                        "continuation request identity is already bound to another candidate version")
                return {
                    "candidate_id": replay["candidate_id"],
                    "version_id": replay["target_version_id"],
                    "previous_verify_job_id": replay["previous_job_id"],
                    "verify_job_id": replay["verify_job_id"],
                    "status": replay["status"],
                }
            row = connection.execute(
                "SELECT c.*,coalesce(vc.verify_job_id,c.verify_job_id) AS active_verify_job_id,"
                "j.status AS verify_status,j.error_json AS verify_error "
                "FROM verification_candidates c LEFT JOIN verification_continuations vc "
                "ON vc.candidate_id=c.candidate_id AND NOT EXISTS(SELECT 1 "
                "FROM verification_continuations nx WHERE nx.candidate_id=c.candidate_id "
                "AND nx.previous_job_id=vc.verify_job_id) "
                "JOIN jobs j ON j.job_id=coalesce(vc.verify_job_id,c.verify_job_id) "
                "WHERE c.candidate_id=? AND c.conversation_id=? AND c.owner_scope=?",
                (candidate, owner, owner)).fetchone()
            if row is None:
                raise VerificationNotFound("candidate does not exist")
            published = connection.execute(
                "SELECT 1 FROM verification_records WHERE candidate_id=? "
                "AND formal_message_id IS NOT NULL", (candidate,)).fetchone()
            if published is not None:
                raise VerificationConflict("a published candidate cannot be continued")
            latest = connection.execute(
                "SELECT version_id,publication_sha256,dependency_hash FROM candidate_versions "
                "WHERE candidate_id=? ORDER BY version_number DESC LIMIT 1", (candidate,)).fetchone()
            if latest is None or latest["version_id"] != version_id:
                raise VerificationConflict("expected candidate version is not the latest immutable version")
            try:
                error = json.loads(row["verify_error"]) if row["verify_error"] else None
            except json.JSONDecodeError as exc:
                raise VerificationIntegrityError("verification job error is not valid JSON") from exc
            if (row["verify_status"] != "failed" or not isinstance(error, dict)
                    or error.get("code") != "verification_budget_exhausted"):
                raise VerificationConflict(
                    "only a verification budget failure can continue from its latest version")
            continuation_input = _json({
                "candidate_id": candidate,
                "candidate_input_hash": row["input_hash"],
                "previous_verify_job_id": row["active_verify_job_id"],
                "target_version_id": version_id,
                "publication_sha256": latest["publication_sha256"],
                "dependency_hash": latest["dependency_hash"],
            })
            continuation_hash = _hash_raw(continuation_input)
            verify_job = self._jobs.enqueue_verify(
                connection, conversation_id=owner, request_identity=request_identity,
                input_hash=continuation_hash, max_attempts=3, deadline_at=None)
            connection.execute(
                "INSERT INTO verification_continuations VALUES(?,?,?,?,?,?,?,?)",
                (_id("continuation"), candidate, row["active_verify_job_id"], verify_job["job_id"],
                 version_id, request_identity, continuation_hash, _now()))
            connection.execute(
                "INSERT INTO verification_usage(job_id) VALUES(?)", (verify_job["job_id"],))
        return {
            "candidate_id": candidate, "version_id": version_id,
            "previous_verify_job_id": row["active_verify_job_id"],
            "verify_job_id": verify_job["job_id"], "status": verify_job["status"],
        }

    def _candidate_for_claim(self, connection, claim: JobClaim, candidate_id: str):
        self._jobs.assert_claim(connection, claim)
        if claim.job_type != "verify" or claim.run_id is not None:
            raise VerificationConflict("verification writes require a verify claim with no run_id")
        row = connection.execute(
            "SELECT c.*,j.input_hash AS job_input_hash,"
            "coalesce(vc.input_hash,c.input_hash) AS bound_job_input_hash,"
            "CASE WHEN c.verify_job_id=j.job_id OR vc.candidate_id=c.candidate_id "
            "THEN 1 ELSE 0 END AS job_bound "
            "FROM verification_candidates c JOIN jobs j ON j.job_id=? "
            "LEFT JOIN verification_continuations vc ON vc.verify_job_id=j.job_id "
            "WHERE c.candidate_id=?",
            (claim.job_id, candidate_id)).fetchone()
        if row is None:
            raise VerificationNotFound("candidate does not exist")
        if (not row["job_bound"] or row["conversation_id"] != claim.conversation_id
                or row["owner_scope"] != claim.conversation_id
                or row["bound_job_input_hash"] != row["job_input_hash"]):
            raise VerificationConflict("candidate is not bound to this exact verification job and owner")
        return row

    @staticmethod
    def _lineage_job_ids(connection, candidate_id: str, job_id: str) -> tuple[str, ...]:
        """Return the bounded initial-to-current verification continuation chain."""
        lineage = []
        current = job_id
        while current not in lineage:
            lineage.append(current)
            if len(lineage) > 100:
                raise VerificationIntegrityError("verification continuation chain is too deep")
            continuation = connection.execute(
                "SELECT candidate_id,previous_job_id FROM verification_continuations "
                "WHERE verify_job_id=?", (current,)).fetchone()
            if continuation is None:
                initial = connection.execute(
                    "SELECT verify_job_id FROM verification_candidates WHERE candidate_id=?",
                    (candidate_id,)).fetchone()
                if initial is None or initial["verify_job_id"] != current:
                    raise VerificationIntegrityError(
                        "verification continuation chain does not reach the candidate origin")
                return tuple(reversed(lineage))
            if continuation["candidate_id"] != candidate_id:
                raise VerificationIntegrityError("verification continuation candidate differs")
            current = continuation["previous_job_id"]
        raise VerificationIntegrityError("verification continuation chain contains a cycle")

    @staticmethod
    def _finding_rows(connection, lineage: tuple[str, ...], candidate_id: str,
                      version_id: str):
        """Read findings in immutable lineage order, then SQLite insertion order."""
        rows = []
        for job_id in lineage:
            rows.extend(connection.execute(
                "SELECT node_id,node_hash,dependency_hash,check_kind,outcome,explanation,"
                "tool_result_refs_json FROM verification_findings "
                "WHERE job_id=? AND candidate_id=? AND version_id=? ORDER BY rowid",
                (job_id, candidate_id, version_id)).fetchall())
        return rows

    @staticmethod
    def _version(connection, candidate_id: str, version_id: str):
        row = connection.execute(
            "SELECT * FROM candidate_versions WHERE candidate_id=? AND version_id=?",
            (candidate_id, version_id)).fetchone()
        if row is None:
            raise VerificationNotFound("candidate version does not exist")
        if _hash_raw(row["publication_json"]) != row["publication_sha256"]:
            raise VerificationIntegrityError("candidate publication checksum differs")
        return row

    def append_finding(self, claim: JobClaim, finding: VerificationFinding) -> dict[str, str]:
        if type(finding) is not VerificationFinding:
            raise TypeError("finding must be VerificationFinding")
        _identifier(finding.check_kind, "check_kind")
        if finding.outcome not in FINDING_OUTCOMES:
            raise ValueError("unsupported finding outcome")
        explanation = _text(finding.explanation, "explanation")
        tool_refs = _refs(finding.tool_result_refs, "tool_result_refs")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_feature(connection)
            self._candidate_for_claim(connection, claim, finding.candidate_id)
            version = self._version(connection, finding.candidate_id, finding.version_id)
            lineage = self._lineage_job_ids(connection, finding.candidate_id, claim.job_id)
            for reference in tool_refs:
                if reference["kind"] != "verification_tool_result":
                    raise VerificationIntegrityError(
                        "finding references must be verifier tool receipts")
                receipt = connection.execute(
                    "SELECT result_json,result_sha256 FROM verification_tool_receipts "
                    "WHERE receipt_id=? AND candidate_id=? AND version_id=?",
                    (reference["id"], finding.candidate_id, finding.version_id)).fetchone()
                owner = connection.execute(
                    "SELECT job_id FROM verification_tool_receipts WHERE receipt_id=?",
                    (reference["id"],)).fetchone()
                if (receipt is None or owner is None or owner["job_id"] not in lineage
                        or receipt["result_sha256"] != reference["sha256"]
                        or _hash_raw(receipt["result_json"]) != receipt["result_sha256"]):
                    raise VerificationIntegrityError(
                        "finding receipt is missing, stale, altered, or outside its verification lineage")
            nodes = json.loads(version["nodes_json"])
            node = nodes.get(finding.node_id)
            if node is None or node != {"node_hash": finding.node_hash,
                                        "dependency_hash": finding.dependency_hash}:
                raise VerificationIntegrityError("finding node or dependency hash differs from candidate")
            applicable = verification_checks(json.loads(version["publication_json"]))
            if finding.check_kind not in applicable[finding.node_id]["required"]:
                raise VerificationIntegrityError("finding check kind is not applicable to candidate node")
            finding_id = _id("finding")
            try:
                connection.execute(
                    "INSERT INTO verification_findings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (finding_id, claim.job_id, claim.attempt_id, finding.candidate_id,
                     finding.version_id, finding.node_id, finding.node_hash,
                     finding.dependency_hash, finding.check_kind, finding.outcome,
                     explanation, _json(tool_refs), _now()))
            except sqlite3.IntegrityError as exc:
                raise VerificationConflict("this attempt already recorded that exact check identity") from exc
        return {"finding_id": finding_id, "status": "recorded"}

    def append_checkpoint(self, claim: JobClaim,
                          checkpoint: VerificationCheckpoint) -> dict[str, str]:
        if type(checkpoint) is not VerificationCheckpoint:
            raise TypeError("checkpoint must be VerificationCheckpoint")
        key = _identifier(checkpoint.checkpoint_key, "checkpoint_key")
        covered = _tuple_strings(checkpoint.covered_node_ids, "covered_node_ids")
        remaining = _tuple_strings(checkpoint.remaining_node_ids, "remaining_node_ids")
        if set(covered) & set(remaining):
            raise ValueError("covered and remaining node IDs must be disjoint")
        refs = _refs(checkpoint.tool_result_refs, "tool_result_refs")
        note = _text(checkpoint.note, "note", 4000, empty=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_feature(connection)
            self._candidate_for_claim(connection, claim, checkpoint.candidate_id)
            version = self._version(connection, checkpoint.candidate_id, checkpoint.version_id)
            nodes = set(json.loads(version["nodes_json"]))
            if not set(covered + remaining) <= nodes:
                raise VerificationIntegrityError("checkpoint refers to an unknown candidate node")
            checkpoint_id = _id("verification_checkpoint")
            try:
                connection.execute(
                    "INSERT INTO verification_checkpoints VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (checkpoint_id, claim.job_id, claim.attempt_id, checkpoint.candidate_id,
                     checkpoint.version_id, version["publication_sha256"],
                     version["dependency_hash"], key, _json(covered), _json(remaining),
                     _json(refs), note, _now()))
            except sqlite3.IntegrityError as exc:
                raise VerificationConflict("checkpoint keys are append-only within an attempt") from exc
        return {"checkpoint_id": checkpoint_id, "status": "recorded"}

    def begin_usage(self, claim: JobClaim) -> dict[str, float | int]:
        """Start or reclaim one cumulative budget clock under the current lease.

        A dead attempt's open reservation is charged through the instant the next
        attempt starts.  This is intentionally conservative: process death can never
        turn consumed verifier time into a fresh budget.
        """
        now = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_feature(connection)
            candidate = connection.execute(
                "SELECT c.candidate_id FROM verification_candidates c "
                "LEFT JOIN verification_continuations vc ON vc.candidate_id=c.candidate_id "
                "WHERE c.verify_job_id=? OR vc.verify_job_id=?",
                (claim.job_id, claim.job_id)).fetchone()
            if candidate is None:
                raise VerificationNotFound("verification claim has no candidate")
            self._candidate_for_claim(connection, claim, candidate["candidate_id"])
            row = connection.execute(
                "SELECT * FROM verification_usage WHERE job_id=?", (claim.job_id,)).fetchone()
            if row is None:
                raise VerificationIntegrityError("verification usage record is missing")
            elapsed = row["elapsed_seconds"]
            if row["active_attempt_id"] is not None and row["active_attempt_id"] != claim.attempt_id:
                elapsed += max(0.0, now - row["active_started_at"])
            if row["active_attempt_id"] != claim.attempt_id:
                connection.execute(
                    "UPDATE verification_usage SET elapsed_seconds=?,active_attempt_id=?,active_started_at=? WHERE job_id=?",
                    (elapsed, claim.attempt_id, now, claim.job_id))
            return {"provider_events": row["provider_events"], "output_chars": row["output_chars"],
                    "payload_bytes": row["payload_bytes"], "tool_calls": row["tool_calls"],
                    "elapsed_seconds": elapsed,
                    "exhaustion_stage": row["exhaustion_stage"],
                    "exhaustion_code": row["exhaustion_code"],
                    "exhaustion_requested_size": row["exhaustion_requested_size"]}

    def consume_usage(self, claim: JobClaim, *, provider_events=0, output_chars=0,
                      payload_bytes=0, tool_calls=0, limits: dict[str, float | int],
                      stage="execution") -> dict[str, float | int]:
        """Atomically reserve usage and durably retain an overflowing request."""
        values = (provider_events, output_chars, payload_bytes, tool_calls)
        if any(type(value) is not int or value < 0 for value in values) or not any(values):
            raise ValueError("usage increments must be non-negative integers with one positive value")
        if type(stage) is not str or stage not in {
                "provider_event", "model_output", "tool_result", "tool_invocation", "execution"}:
            raise ValueError("invalid usage reservation stage")
        now = time.time()
        exceeded = None
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_feature(connection)
            self._jobs.assert_claim(connection, claim)
            row = connection.execute(
                "SELECT * FROM verification_usage WHERE job_id=?", (claim.job_id,)).fetchone()
            if row is None or row["active_attempt_id"] != claim.attempt_id:
                raise VerificationConflict("verification usage is not reserved by this attempt")
            elapsed = row["elapsed_seconds"] + max(0.0, now - row["active_started_at"])
            updated = {"provider_events": row["provider_events"] + provider_events,
                       "output_chars": row["output_chars"] + output_chars,
                       "payload_bytes": row["payload_bytes"] + payload_bytes,
                       "tool_calls": row["tool_calls"] + tool_calls,
                       "elapsed_seconds": elapsed}
            dimensions = (
                ("provider_events", provider_events), ("output_chars", output_chars),
                ("payload_bytes", payload_bytes), ("tool_calls", tool_calls))
            failed = next(((name, requested) for name, requested in dimensions
                           if updated[name] > limits[name]), None)
            if failed is None and elapsed >= limits["elapsed_seconds"]:
                failed = ("elapsed_seconds", 0)
            if failed is not None:
                code, requested = failed
                requested = min(requested, 1_000_000_000)
                for name, _increment in dimensions:
                    updated[name] = min(updated[name], int(limits[name]) + 1)
                connection.execute(
                    "UPDATE verification_usage SET provider_events=?,output_chars=?,payload_bytes=?,"
                    "tool_calls=?,exhaustion_stage=?,exhaustion_code=?,"
                    "exhaustion_requested_size=? WHERE job_id=?",
                    (updated["provider_events"], updated["output_chars"],
                     updated["payload_bytes"], updated["tool_calls"], stage, code,
                     requested, claim.job_id))
                exceeded = VerificationBudgetExceeded(stage, code, requested)
            else:
                connection.execute(
                    "UPDATE verification_usage SET provider_events=?,output_chars=?,payload_bytes=?,"
                    "tool_calls=? WHERE job_id=?",
                    (updated["provider_events"], updated["output_chars"],
                     updated["payload_bytes"], updated["tool_calls"], claim.job_id))
        if exceeded is not None:
            raise exceeded
        return updated

    def end_usage(self, claim: JobClaim) -> None:
        """Settle the current wall-clock reservation; stale claims safely do nothing."""
        now = time.time()
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM verification_usage WHERE job_id=?", (claim.job_id,)).fetchone()
            if row is None or row["active_attempt_id"] != claim.attempt_id:
                return
            elapsed = row["elapsed_seconds"] + max(0.0, now - row["active_started_at"])
            connection.execute(
                "UPDATE verification_usage SET elapsed_seconds=?,active_attempt_id=NULL,active_started_at=NULL "
                "WHERE job_id=? AND active_attempt_id=?",
                (elapsed, claim.job_id, claim.attempt_id))

    def append_tool_receipt(self, claim: JobClaim,
                            receipt: VerificationToolReceipt) -> ArtifactRef:
        """Persist one runner-produced result before it can support a finding."""
        if type(receipt) is not VerificationToolReceipt:
            raise TypeError("receipt must be VerificationToolReceipt")
        call_id = _identifier(receipt.call_id, "call_id")
        tool_name = _identifier(receipt.tool_name, "tool_name")
        if type(receipt.arguments) is not dict or type(receipt.result) is not dict:
            raise TypeError("receipt arguments and result must be dicts")
        _reject_hidden_thoughts(receipt.arguments, "receipt arguments")
        _reject_hidden_thoughts(receipt.result, "receipt result")
        arguments_raw = _json(receipt.arguments, "receipt arguments")
        result_raw = _json(receipt.result, "receipt result")
        digest = _hash_raw(result_raw)
        receipt_id = _id("verification_receipt")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_feature(connection)
            self._candidate_for_claim(connection, claim, receipt.candidate_id)
            self._version(connection, receipt.candidate_id, receipt.version_id)
            try:
                connection.execute(
                    "INSERT INTO verification_tool_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (receipt_id, claim.job_id, claim.attempt_id, receipt.candidate_id,
                     receipt.version_id, call_id, tool_name, arguments_raw, result_raw,
                     digest, _now()))
            except sqlite3.IntegrityError as exc:
                raise VerificationConflict("tool call identity was already persisted") from exc
        return ArtifactRef("verification_tool_result", receipt_id, digest)

    def read_claim_input(self, claim: JobClaim) -> dict[str, Any]:
        """Read the exact clean-session packet for a currently owned verify claim."""
        if type(claim) is not JobClaim or claim.job_type != "verify":
            raise TypeError("claim must be a verify JobClaim")
        with closing(self._connect()) as connection:
            self._require_feature(connection)
            self._jobs.assert_claim(connection, claim)
            row = connection.execute(
                "SELECT c.* FROM verification_candidates c "
                "LEFT JOIN verification_continuations vc ON vc.candidate_id=c.candidate_id "
                "WHERE c.verify_job_id=? OR vc.verify_job_id=?",
                (claim.job_id, claim.job_id)).fetchone()
            if row is None:
                raise VerificationNotFound("verification claim has no candidate")
            version = connection.execute(
                "SELECT * FROM candidate_versions WHERE candidate_id=? "
                "ORDER BY version_number DESC LIMIT 1", (row["candidate_id"],)).fetchone()
            lineage = self._lineage_job_ids(connection, row["candidate_id"], claim.job_id)
            placeholders = ",".join("?" for _ in lineage)
            checkpoints = connection.execute(
                "SELECT checkpoint_key,version_id,covered_node_ids_json,remaining_node_ids_json,"
                "tool_result_refs_json,note FROM verification_checkpoints "
                f"WHERE job_id IN ({placeholders}) AND candidate_id=? AND version_id=? "
                "ORDER BY rowid", (*lineage, row["candidate_id"], version["version_id"])).fetchall()
            usage = connection.execute(
                "SELECT * FROM verification_usage WHERE job_id=?", (claim.job_id,)).fetchone()
            inputs = connection.execute(
                "SELECT inputs_json,inputs_sha256 FROM verification_author_inputs WHERE candidate_id=?",
                (row["candidate_id"],)).fetchone()
            if inputs is None or _hash_raw(inputs["inputs_json"]) != inputs["inputs_sha256"]:
                raise VerificationIntegrityError("sealed author inputs are missing or altered")
            author_inputs = AuthorInputs.decode(json.loads(inputs["inputs_json"])).to_json()
            evidence_refs = json.loads(row["evidence_refs_json"])
            calculation_refs = json.loads(row["calculation_refs_json"])
            for item in evidence_refs + calculation_refs:
                value = author_inputs.get(item["kind"], {}).get(item["id"])
                if value is None or _hash_raw(_json(value)) != item["sha256"]:
                    raise VerificationIntegrityError(
                        "sealed author input differs from its candidate reference digest")
        return {
            "candidate_id": row["candidate_id"], "conversation_id": row["conversation_id"],
            "author_run_id": row["author_run_id"], "question": row["question"],
            "scope": json.loads(row["scope_json"]),
            "input_snapshot": json.loads(row["input_snapshot_json"]),
            "base_message_id": row["base_message_id"], "input_hash": row["input_hash"],
            "version_id": version["version_id"], "version_number": version["version_number"],
            "publication": CanonicalPublication.decode(json.loads(version["publication_json"])).to_json(),
            "publication_sha256": version["publication_sha256"],
            "nodes": json.loads(version["nodes_json"]),
            "dependency_hash": version["dependency_hash"],
            "evidence_refs": evidence_refs,
            "calculation_refs": calculation_refs,
            "author_inputs": author_inputs,
            "cumulative_tool_calls": usage["tool_calls"],
            "cumulative_provider_events": usage["provider_events"],
            "cumulative_output_chars": usage["output_chars"],
            "cumulative_elapsed_seconds": usage["elapsed_seconds"],
            "checkpoints": [{
                "checkpoint_key": item["checkpoint_key"], "version_id": item["version_id"],
                "covered_node_ids": json.loads(item["covered_node_ids_json"]),
                "remaining_node_ids": json.loads(item["remaining_node_ids_json"]),
                "tool_result_refs": json.loads(item["tool_result_refs_json"]),
                "note": item["note"],
            } for item in checkpoints],
        }

    def receipt_exists(self, claim: JobClaim, reference: ArtifactRef,
                       *, candidate_id: str, version_id: str) -> bool:
        """Claim-fenced binding check used by strict application finalization."""
        if type(reference) is not ArtifactRef or reference.kind != "verification_tool_result":
            return False
        with closing(self._connect()) as connection:
            self._require_feature(connection)
            self._candidate_for_claim(connection, claim, candidate_id)
            lineage = self._lineage_job_ids(connection, candidate_id, claim.job_id)
            placeholders = ",".join("?" for _ in lineage)
            row = connection.execute(
                "SELECT result_sha256 FROM verification_tool_receipts WHERE receipt_id=? "
                f"AND job_id IN ({placeholders}) AND candidate_id=? AND version_id=?",
                (reference.identifier, *lineage, candidate_id, version_id)).fetchone()
            return row is not None and row[0] == reference.sha256

    def read_receipts(self, claim: JobClaim, references: tuple[ArtifactRef, ...],
                      *, candidate_id: str, version_id: str) -> list[dict[str, Any]]:
        """Return exact runner receipts after owner, claim, version, and checksum checks."""
        with closing(self._connect()) as connection:
            self._require_feature(connection)
            self._candidate_for_claim(connection, claim, candidate_id)
            lineage = self._lineage_job_ids(connection, candidate_id, claim.job_id)
            placeholders = ",".join("?" for _ in lineage)
            values = []
            for reference in references:
                if type(reference) is not ArtifactRef or reference.kind != "verification_tool_result":
                    raise VerificationIntegrityError("finding refers to a non-verifier result")
                row = connection.execute(
                    "SELECT tool_name,result_json,result_sha256 FROM verification_tool_receipts "
                    f"WHERE receipt_id=? AND job_id IN ({placeholders}) "
                    "AND candidate_id=? AND version_id=?",
                    (reference.identifier, *lineage, candidate_id, version_id)).fetchone()
                if row is None or row["result_sha256"] != reference.sha256:
                    raise VerificationIntegrityError("finding receipt is missing, stale, or altered")
                if _hash_raw(row["result_json"]) != row["result_sha256"]:
                    raise VerificationIntegrityError("persisted verifier receipt checksum differs")
                values.append({"name": row["tool_name"], "result": json.loads(row["result_json"]),
                               "type": "tool_result", "authoritative": True})
            return values

    def resolve_receipt_ids(self, claim: JobClaim, receipt_ids: tuple[str, ...],
                            *, candidate_id: str, version_id: str) -> tuple[ArtifactRef, ...]:
        """Resolve IDs through the current claim, candidate, version, and checksum fence.

        Receipts are job-owned rather than attempt-owned, allowing a reclaimed verifier
        to use persisted reads from an earlier attempt. Foreign IDs look merely missing.
        """
        identifiers = _tuple_strings(receipt_ids, "receipt_ids")
        if len(identifiers) > 10_000:
            raise ValueError("receipt_ids must be bounded")
        with closing(self._connect()) as connection:
            self._require_feature(connection)
            self._candidate_for_claim(connection, claim, candidate_id)
            self._version(connection, candidate_id, version_id)
            lineage = self._lineage_job_ids(connection, candidate_id, claim.job_id)
            placeholders = ",".join("?" for _ in lineage)
            values = []
            for identifier in identifiers:
                row = connection.execute(
                    "SELECT result_json,result_sha256 FROM verification_tool_receipts WHERE receipt_id=? "
                    f"AND job_id IN ({placeholders}) AND candidate_id=? AND version_id=?",
                    (identifier, *lineage, candidate_id, version_id)).fetchone()
                if row is None:
                    raise VerificationIntegrityError(
                        "verification receipt is missing, stale, or outside the current candidate")
                if _hash_raw(row["result_json"]) != row["result_sha256"]:
                    raise VerificationIntegrityError("persisted verifier receipt checksum differs")
                values.append(ArtifactRef(
                    "verification_tool_result", identifier, row["result_sha256"]))
        return tuple(values)

    def resolve_successful_receipts(self, claim: JobClaim, *, candidate_id: str,
                                    version_id: str) -> tuple[ArtifactRef, ...]:
        """Return bounded successful verifier data reads under the current claim fence.

        This intentionally reads only verifier-owned receipts. Failed and incomplete
        operations cannot provide evidence, while a successful empty search remains an
        actual data read that can support a disclosed absence.
        """
        maximum = 10_000
        with closing(self._connect()) as connection:
            self._require_feature(connection)
            self._candidate_for_claim(connection, claim, candidate_id)
            self._version(connection, candidate_id, version_id)
            lineage = self._lineage_job_ids(connection, candidate_id, claim.job_id)
            placeholders = ",".join("?" for _ in lineage)
            rows = connection.execute(
                "SELECT receipt_id,result_json,result_sha256 FROM verification_tool_receipts "
                f"WHERE job_id IN ({placeholders}) AND candidate_id=? AND version_id=? "
                "ORDER BY created_at,rowid LIMIT ?",
                (*lineage, candidate_id, version_id, maximum + 1)).fetchall()
            if len(rows) > maximum:
                raise VerificationIntegrityError("successful verifier receipts must be bounded")
            values = []
            for row in rows:
                if _hash_raw(row["result_json"]) != row["result_sha256"]:
                    raise VerificationIntegrityError("persisted verifier receipt checksum differs")
                result = json.loads(row["result_json"])
                if (not isinstance(result, dict) or result.get("completed") is False
                        or "error" in result or result.get("state") not in (None, "available")):
                    continue
                values.append(ArtifactRef(
                    "verification_tool_result", row["receipt_id"], row["result_sha256"]))
        return tuple(values)

    def read_findings(self, claim: JobClaim, *, candidate_id: str,
                      version_id: str) -> list[dict[str, Any]]:
        """Read exact current-version findings from every attempt under this job."""
        with closing(self._connect()) as connection:
            self._require_feature(connection)
            self._candidate_for_claim(connection, claim, candidate_id)
            version = self._version(connection, candidate_id, version_id)
            nodes = json.loads(version["nodes_json"])
            lineage = self._lineage_job_ids(connection, candidate_id, claim.job_id)
            rows = self._finding_rows(connection, lineage, candidate_id, version_id)
            values = []
            for row in rows:
                if nodes.get(row["node_id"]) != {
                        "node_hash": row["node_hash"],
                        "dependency_hash": row["dependency_hash"]}:
                    raise VerificationIntegrityError("persisted finding differs from candidate version")
                values.append({
                    "node_id": row["node_id"], "check_kind": row["check_kind"],
                    "outcome": row["outcome"], "explanation": row["explanation"],
                    "tool_result_refs": json.loads(row["tool_result_refs_json"]),
                })
        return values

    def append_control_diagnostic(self, claim: JobClaim, *, candidate_id: str,
                                  operation: str, error_code: str) -> dict[str, str]:
        """Append a bounded non-evidence record for one rejected control call."""
        operation = _identifier(operation, "operation")
        error_code = _identifier(error_code, "error_code")
        diagnostic_id = _id("verification_control_diagnostic")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_feature(connection)
            self._candidate_for_claim(connection, claim, candidate_id)
            connection.execute(
                "INSERT INTO verification_control_diagnostics VALUES(?,?,?,?,?,?,?)",
                (diagnostic_id, claim.job_id, claim.attempt_id, candidate_id,
                 operation, error_code, _now()))
        return {"diagnostic_id": diagnostic_id, "status": "recorded"}

    @staticmethod
    def _receipt_event(row) -> dict[str, Any]:
        result = json.loads(row["result_json"])
        if _hash_raw(row["result_json"]) != row["result_sha256"]:
            raise VerificationIntegrityError("persisted verifier receipt checksum differs")
        return {"type": "tool_result", "name": row["tool_name"],
                "call_id": row["call_id"], "authoritative": True,
                "truncated": False, "result": result,
                "observed_arguments": json.loads(row["arguments_json"]),
                "verification_receipt_id": row["receipt_id"],
                "verification_receipt_sha256": row["result_sha256"]}

    def read_evidence_events(self, claim: JobClaim, *, candidate_id: str,
                             version_id: str) -> list[dict[str, Any]]:
        """Read exact author and current-version verifier result records under a claim."""
        with closing(self._connect()) as connection:
            self._require_feature(connection)
            candidate = self._candidate_for_claim(connection, claim, candidate_id)
            self._version(connection, candidate_id, version_id)
            try:
                receipts = self._records.read_receipts(candidate["author_run_id"])
            except ResearchRecordError as exc:
                raise VerificationIntegrityError(exc.message) from exc
            author = [{
                "type": "tool_result", "name": receipt.tool_name,
                "call_id": receipt.call_id, "authoritative": True, "truncated": False,
                "result": receipt.result_object(),
                "observed_arguments": receipt.arguments_object(),
                "author_receipt_revision": receipt.revision,
                "author_receipt_sha256": receipt.receipt_sha256,
            } for receipt in receipts]
            lineage = self._lineage_job_ids(connection, candidate_id, claim.job_id)
            placeholders = ",".join("?" for _ in lineage)
            verifier = [self._receipt_event(row) for row in connection.execute(
                f"SELECT * FROM verification_tool_receipts WHERE job_id IN ({placeholders}) "
                "AND candidate_id=? AND version_id=? ORDER BY created_at,rowid",
                (*lineage, candidate_id, version_id))]
        return author + verifier

    def read_published_evidence(self, owner_scope: str, formal_message_id: str) -> dict[str, Any]:
        """Public read-only contract for one exact verified formal publication.

        It never projects verifier receipts into the author's run and never accepts a
        candidate/version supplied by the caller.  All identities come from the one
        immutable verification record bound to ``formal_message_id``.
        """
        owner_scope = _identifier(owner_scope, "owner_scope")
        formal_message_id = _identifier(formal_message_id, "formal_message_id")
        with closing(self._connect()) as connection:
            self._require_feature(connection)
            row = connection.execute(
                "SELECT r.*,c.author_run_id,c.conversation_id,c.input_snapshot_json,c.scope_json,"
                "v.publication_json,v.publication_sha256 AS sealed_publication_sha256 "
                "FROM verification_records r JOIN verification_candidates c USING(candidate_id) "
                "JOIN candidate_versions v ON v.version_id=r.version_id "
                "WHERE r.formal_message_id=? AND c.owner_scope=?",
                (formal_message_id, owner_scope)).fetchone()
            if row is None or row["outcome"] not in {"verified", "verified_with_limitations"}:
                raise VerificationNotFound("published verification evidence does not exist")
            if (row["publication_sha256"] != row["sealed_publication_sha256"]
                    or _hash_raw(row["publication_json"]) != row["publication_sha256"]):
                raise VerificationIntegrityError("published candidate version differs from its verification record")
            lineage = self._lineage_job_ids(connection, row["candidate_id"], row["job_id"])
            placeholders = ",".join("?" for _ in lineage)
            receipts = [self._receipt_event(item) for item in connection.execute(
                f"SELECT * FROM verification_tool_receipts WHERE job_id IN ({placeholders}) "
                "AND candidate_id=? AND version_id=? ORDER BY created_at,rowid",
                (*lineage, row["candidate_id"], row["version_id"]))]
        return {"formal_message_id": formal_message_id,
                "verification_id": row["verification_id"],
                "candidate_id": row["candidate_id"], "version_id": row["version_id"],
                "publication_sha256": row["publication_sha256"],
                "author_run_id": row["author_run_id"], "conversation_id": row["conversation_id"],
                "scope": json.loads(row["scope_json"]),
                "input_snapshot": json.loads(row["input_snapshot_json"]),
                "receipts": receipts}

    def published_evidence_for_author_run(self, author_run_id: str) -> dict[str, Any] | None:
        """Resolve the unique formal contract for an author run, for artifact adapters."""
        author_run_id = _identifier(author_run_id, "author_run_id")
        with closing(self._connect()) as connection:
            self._require_feature(connection)
            row = connection.execute(
                "SELECT c.owner_scope,r.formal_message_id FROM verification_candidates c "
                "JOIN verification_records r USING(candidate_id) WHERE c.author_run_id=? "
                "AND r.formal_message_id IS NOT NULL", (author_run_id,)).fetchone()
        return None if row is None else self.read_published_evidence(row["owner_scope"], row["formal_message_id"])

    def append_revision(self, claim: JobClaim, revision: RevisedCandidate) -> dict[str, Any]:
        if type(revision) is not RevisedCandidate:
            raise TypeError("revision must be RevisedCandidate")
        changed_nodes = _tuple_strings(revision.changed_node_ids, "changed_node_ids")
        summary = _text(revision.revision_summary, "revision_summary", 4000)
        publication, publication_raw, publication_hash = _publication(revision.publication)
        nodes, dependency_hash = _nodes(publication)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_feature(connection)
            self._candidate_for_claim(connection, claim, revision.candidate_id)
            base = self._version(connection, revision.candidate_id, revision.base_version_id)
            latest = connection.execute(
                "SELECT * FROM candidate_versions WHERE candidate_id=? "
                "ORDER BY version_number DESC LIMIT 1", (revision.candidate_id,)).fetchone()
            if latest["version_id"] != base["version_id"]:
                raise VerificationConflict("revision base is not the latest candidate version")
            old_nodes = json.loads(base["nodes_json"])
            actual_changed = {node_id for node_id in set(old_nodes) | set(nodes)
                              if old_nodes.get(node_id) != nodes.get(node_id)}
            if not actual_changed:
                raise VerificationIntegrityError("revised candidate does not change any content node")
            if changed_nodes and set(changed_nodes) != actual_changed:
                raise VerificationIntegrityError("changed_node_ids must exactly match structural changes")
            changed_nodes = sorted(actual_changed)
            version_id = _id("candidate_version")
            connection.execute(
                "INSERT INTO candidate_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (version_id, revision.candidate_id, latest["version_number"] + 1,
                 revision.base_version_id, publication_raw, publication_hash,
                 _json(nodes), dependency_hash, claim.attempt_id,
                 _json(changed_nodes), summary, _now()))
        return {"version_id": version_id, "version_number": latest["version_number"] + 1,
                "publication_sha256": publication_hash}

    @staticmethod
    def _prepared(prepared: PreparedPublication) -> tuple[dict[str, Any], str, dict[str, Any]]:
        if type(prepared) is not PreparedPublication:
            raise TypeError("prepared must be PreparedPublication")
        semantic_review = prepared.semantic_review.to_json()
        verification = prepared.verification.to_json()
        _reject_hidden_thoughts(semantic_review, "semantic_review")
        _reject_hidden_thoughts(verification, "verification")
        publication, publication_raw, publication_hash = _publication(prepared.publication)
        required = list(prepared.coverage.required)
        checked = list(prepared.coverage.checked)
        unresolved = list(prepared.coverage.unresolved)
        limitations = list(prepared.limitations)
        projected_findings = [item.to_json() for item in prepared.required_verifier_findings]
        semantic_raw = _json(semantic_review, "semantic_review")
        citations_raw = _json([item.to_json() for item in prepared.citations], "citations")
        verification_raw = _json(verification, "verification")
        coverage = {"required_node_ids": required, "checked_node_ids": checked,
                    "unresolved_node_ids": unresolved}
        commit_raw = _json({
            "candidate_id": prepared.candidate_id, "version_id": prepared.version_id,
            "input_hash": prepared.input_hash, "outcome": prepared.outcome,
            "publication_sha256": publication_hash,
            "semantic_review_sha256": _hash_raw(semantic_raw),
            "citations_sha256": _hash_raw(citations_raw),
            "verification_sha256": _hash_raw(verification_raw),
            "required_verifier_findings": list(projected_findings),
            "coverage": coverage, "limitations": limitations,
        }, "prepared commit")
        return publication, _hash_raw(commit_raw), {
            "publication_raw": publication_raw, "coverage": coverage,
            "limitations": limitations, "citations_raw": citations_raw,
            "verification_raw": verification_raw,
            "required_verifier_findings": list(projected_findings),
        }

    def finalize(self, claim: JobClaim, prepared: PreparedPublication) -> dict[str, Any]:
        """Atomically commit verification and, when complete, the existing formal output.

        Identical retries return the prior record even after the attempt became terminal.
        Conflicting retries, cancellation, stale claims, tampering, or a newer formal
        report are rejected. Snapshot rendering runs read-only before the lock; no
        model, resolver, renderer, or callback runs inside the publication transaction.
        """
        publication, commit_hash, encoded = self._prepared(prepared)
        # Idempotent completed retries must not rerun today's projection code.
        with closing(self._connect()) as connection:
            self._require_feature(connection)
            prior = connection.execute(
                "SELECT * FROM verification_records WHERE job_id=?", (claim.job_id,)).fetchone()
            if prior is not None:
                if (prior["input_hash"] != prepared.input_hash
                        or prior["commit_hash"] != commit_hash):
                    raise VerificationConflict("verification job was already committed with different output")
                return self._result(prior)
        formal_message_id = None
        prepared_snapshot = None
        if prepared.outcome != "unresolved" and self._snapshot_preparer is not None:
            formal_message_id = "msg_" + hashlib.sha256(
                f"formal-publication/1\0{prepared.candidate_id}\0{prepared.version_id}".encode("utf-8")
            ).hexdigest()
            # Projection may open arbitrary read paths, so it runs before BEGIN IMMEDIATE.
            prepared_snapshot = self._snapshot_preparer(claim, prepared, formal_message_id)
            if (not isinstance(prepared_snapshot, dict)
                    or not isinstance(prepared_snapshot.get("artifact"), dict)
                    or not isinstance(prepared_snapshot.get("payload"), dict)
                    or not isinstance(prepared_snapshot.get("receipt_manifest"), list)):
                raise VerificationIntegrityError("publication snapshot preparation returned an invalid closure")
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_feature(connection)
            prior = connection.execute(
                "SELECT * FROM verification_records WHERE job_id=?", (claim.job_id,)).fetchone()
            if prior is not None:
                if (prior["input_hash"] != prepared.input_hash
                        or prior["commit_hash"] != commit_hash):
                    raise VerificationConflict("verification job was already committed with different output")
                return self._result(prior)
            candidate = self._candidate_for_claim(connection, claim, prepared.candidate_id)
            if candidate["input_hash"] != prepared.input_hash:
                raise VerificationIntegrityError("prepared input hash differs from immutable candidate input")
            version = self._version(connection, prepared.candidate_id, prepared.version_id)
            latest_version = connection.execute(
                "SELECT version_id FROM candidate_versions WHERE candidate_id=? "
                "ORDER BY version_number DESC LIMIT 1", (prepared.candidate_id,)).fetchone()[0]
            if latest_version != prepared.version_id:
                raise VerificationConflict("only the latest candidate revision can be finalized")
            if json.loads(version["publication_json"]) != publication:
                raise VerificationIntegrityError("prepared publication differs from the candidate version")
            nodes = set(json.loads(version["nodes_json"]))
            coverage = encoded["coverage"]
            if set(coverage["required_node_ids"]) != nodes:
                raise VerificationIntegrityError("required coverage must exactly enumerate candidate nodes")
            if not set(coverage["checked_node_ids"]) <= nodes or not set(coverage["unresolved_node_ids"]) <= nodes:
                raise VerificationIntegrityError("coverage refers to unknown candidate nodes")
            lineage = self._lineage_job_ids(connection, prepared.candidate_id, claim.job_id)
            placeholders = ",".join("?" for _ in lineage)
            finding_rows = self._finding_rows(
                connection, lineage, prepared.candidate_id, prepared.version_id)
            findings = [{
                "node_id": row["node_id"], "check_kind": row["check_kind"],
                "outcome": row["outcome"], "explanation": row["explanation"],
                "tool_result_refs": json.loads(row["tool_result_refs_json"]),
            } for row in finding_rows]
            check_projection = verification_checks(publication)
            terminal_nodes = completed_check_nodes(findings, check_projection)
            if set(coverage["checked_node_ids"]) - terminal_nodes:
                raise VerificationConflict(
                    "coverage cannot claim checks without every applicable terminal finding")
            terminal_findings = [item for item in effective_findings(findings)
                                 if item["node_id"] in terminal_nodes]
            if [item.to_json() for item in required_verifier_projection(terminal_findings)] != encoded["required_verifier_findings"]:
                raise VerificationIntegrityError(
                    "required verifier projection differs from persisted effective findings")
            if prepared.outcome != "unresolved":
                if self._snapshot_preparer is not None and prepared_snapshot is None:
                    raise VerificationIntegrityError("formal publication snapshot was not prepared")
                if formal_message_id is None:
                    formal_message_id = _id("msg")
                if prepared_snapshot is not None:
                    current_manifest = sorted((row["receipt_id"], row["result_sha256"])
                        for row in connection.execute(
                            "SELECT receipt_id,result_sha256 FROM verification_tool_receipts "
                            f"WHERE job_id IN ({placeholders}) AND candidate_id=? AND version_id=?",
                            (*lineage, prepared.candidate_id, prepared.version_id)))
                    if [tuple(item) for item in prepared_snapshot["receipt_manifest"]] != current_manifest:
                        raise VerificationIntegrityError(
                            "prepared snapshot verifier receipts differ from the final receipt set")
                base = candidate["base_message_id"]
                if (self._latest_formal_message(connection, claim.conversation_id) != base
                        or not self._head_matches(connection, claim.conversation_id, base)):
                    raise VerificationConflict("a newer formal report prevents current-pointer takeover")
                verification_payload = json.loads(encoded["verification_raw"])
                verification_payload.update({
                    "publication_schema": PUBLICATION_VERSION,
                    "verification_outcome": prepared.outcome,
                    "verification_candidate_id": prepared.candidate_id,
                })
                connection.execute(
                    "INSERT INTO messages(message_id,conversation_id,role,content,created_at,run_id,status,citations_json,verification_json) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (formal_message_id, claim.conversation_id, "assistant", publication["answer"],
                     _now(), candidate["author_run_id"], "completed",
                     encoded["citations_raw"], _json(verification_payload)))
                self._publications.save(
                    connection, message_id=formal_message_id, run_id=candidate["author_run_id"],
                    publication=publication, semantic_review=prepared.semantic_review.to_json())
                if prepared_snapshot is not None:
                    from .report_reader import store_prepared_reader_snapshot
                    store_prepared_reader_snapshot(
                        connection, artifact=prepared_snapshot["artifact"],
                        payload=prepared_snapshot["payload"], registered_at=_now())
                if base is None:
                    changed = connection.execute(
                        "UPDATE report_current SET message_id=?,updated_at=? WHERE conversation_id=? "
                        "AND message_id IS NULL", (formal_message_id, _now(), claim.conversation_id)).rowcount
                else:
                    changed = connection.execute(
                        "UPDATE report_current SET message_id=?,updated_at=? WHERE conversation_id=? "
                        "AND message_id=?", (formal_message_id, _now(), claim.conversation_id, base)).rowcount
                if changed != 1:
                    raise VerificationConflict("formal report pointer changed during commit")
                connection.execute("UPDATE conversations SET updated_at=? WHERE conversation_id=?",
                                   (_now(), claim.conversation_id))
            verification_id = _id("verification")
            connection.execute(
                "INSERT INTO verification_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (verification_id, claim.job_id, claim.attempt_id, prepared.candidate_id,
                 prepared.version_id, version["publication_sha256"],
                 version["dependency_hash"], prepared.input_hash, prepared.outcome,
                 _json(coverage), _json(encoded["limitations"]), formal_message_id,
                 commit_hash, _now()))
            self._jobs.succeed_in_transaction(connection, claim)
            row = connection.execute(
                "SELECT * FROM verification_records WHERE verification_id=?", (verification_id,)).fetchone()
        return self._result(row)

    @staticmethod
    def _result(row) -> dict[str, Any]:
        return {"verification_id": row["verification_id"], "candidate_id": row["candidate_id"],
                "version_id": row["version_id"], "outcome": row["outcome"],
                "formal_message_id": row["formal_message_id"],
                "status": ("unresolved" if row["outcome"] == "unresolved" else row["outcome"])}

    def get_candidate(self, owner_scope: str, candidate_id: str) -> dict[str, Any]:
        owner_scope = _identifier(owner_scope, "owner_scope")
        candidate_id = _identifier(candidate_id, "candidate_id")
        with closing(self._connect()) as connection:
            self._require_feature(connection)
            row = connection.execute(
                "SELECT c.*,coalesce(vc.verify_job_id,c.verify_job_id) AS active_verify_job_id,"
                "j.status AS job_status,j.cancel_requested,v.outcome,v.formal_message_id "
                "FROM verification_candidates c LEFT JOIN verification_continuations vc "
                "ON vc.candidate_id=c.candidate_id AND NOT EXISTS(SELECT 1 "
                "FROM verification_continuations nx WHERE nx.candidate_id=c.candidate_id "
                "AND nx.previous_job_id=vc.verify_job_id) "
                "JOIN jobs j ON j.job_id=coalesce(vc.verify_job_id,c.verify_job_id) "
                "LEFT JOIN verification_records v ON v.job_id=j.job_id "
                "WHERE c.candidate_id=? AND c.owner_scope=?", (candidate_id, owner_scope)).fetchone()
            if row is None:
                raise VerificationNotFound("candidate does not exist in this owner scope")
            versions = connection.execute(
                "SELECT version_id,version_number,parent_version_id,publication_sha256,dependency_hash,created_at "
                "FROM candidate_versions WHERE candidate_id=? ORDER BY version_number", (candidate_id,)).fetchall()
        return self._candidate_view(row, versions)

    def read_version(self, owner_scope: str, candidate_id: str,
                     version_id: str) -> dict[str, Any]:
        """Return one immutable canonical candidate version within its owner scope."""
        owner_scope = _identifier(owner_scope, "owner_scope")
        candidate_id = _identifier(candidate_id, "candidate_id")
        version_id = _identifier(version_id, "version_id")
        with closing(self._connect()) as connection:
            self._require_feature(connection)
            candidate = connection.execute(
                "SELECT evidence_refs_json,calculation_refs_json FROM verification_candidates "
                "WHERE candidate_id=? AND owner_scope=?", (candidate_id, owner_scope)).fetchone()
            if candidate is None:
                raise VerificationNotFound("candidate does not exist in this owner scope")
            version = self._version(connection, candidate_id, version_id)
        return {
            "candidate_id": candidate_id, "version_id": version_id,
            "version_number": version["version_number"],
            "parent_version_id": version["parent_version_id"],
            "publication": CanonicalPublication.decode(json.loads(version["publication_json"])).to_json(),
            "publication_sha256": version["publication_sha256"],
            "nodes": json.loads(version["nodes_json"]),
            "dependency_hash": version["dependency_hash"],
            "evidence_refs": json.loads(candidate["evidence_refs_json"]),
            "calculation_refs": json.loads(candidate["calculation_refs_json"]),
        }

    def list_candidates(self, owner_scope: str, *, limit: int = 50) -> list[dict[str, Any]]:
        owner_scope = _identifier(owner_scope, "owner_scope")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer from 1 to 200")
        with closing(self._connect()) as connection:
            self._require_feature(connection)
            rows = connection.execute(
                "SELECT c.*,coalesce(vc.verify_job_id,c.verify_job_id) AS active_verify_job_id,"
                "j.status AS job_status,j.cancel_requested,v.outcome,v.formal_message_id "
                "FROM verification_candidates c LEFT JOIN verification_continuations vc "
                "ON vc.candidate_id=c.candidate_id AND NOT EXISTS(SELECT 1 "
                "FROM verification_continuations nx WHERE nx.candidate_id=c.candidate_id "
                "AND nx.previous_job_id=vc.verify_job_id) "
                "JOIN jobs j ON j.job_id=coalesce(vc.verify_job_id,c.verify_job_id) "
                "LEFT JOIN verification_records v ON v.job_id=j.job_id "
                "WHERE c.owner_scope=? ORDER BY c.created_at DESC,c.rowid DESC LIMIT ?",
                (owner_scope, limit)).fetchall()
            return [self._candidate_view(row, []) for row in rows]

    @staticmethod
    def _candidate_view(row, versions) -> dict[str, Any]:
        if row["outcome"] is not None:
            status = row["outcome"]
        elif row["job_status"] in {"queued", "retry_wait"}:
            status = "queued"
        elif row["job_status"] == "running":
            status = "running"
        else:
            status = "unresolved"
        return {
            "candidate_id": row["candidate_id"], "conversation_id": row["conversation_id"],
            "author_run_id": row["author_run_id"],
            "verify_job_id": row["active_verify_job_id"],
            "question": row["question"], "scope": json.loads(row["scope_json"]),
            "input_snapshot": json.loads(row["input_snapshot_json"]),
            "base_message_id": row["base_message_id"], "input_hash": row["input_hash"],
            "status": status, "formal_message_id": row["formal_message_id"],
            "semantic_truth_guaranteed": False,
            "versions": [dict(item) for item in versions],
        }
