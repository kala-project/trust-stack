"""Persistent asynchronous chat orchestration for the restricted research provider."""

from __future__ import annotations

from contextlib import closing
from copy import copy, deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
import uuid
from typing import Any

from .documents import DocumentError
from .official_sources import OfficialSourceError, OfficialSources
from .research_tools import ResearchTools, ToolError
from .retrieval_budget import RetrievalBudget, SEARCH_TOOLS
from .document_facts import validate_document_fact, with_provenance
from .research_inputs import InputError, ResearchInputStore, canonical_derivation
from .research_records import ResearchRecordError, ResearchRecordStore
from .report_publications import ReportPublications, prepare as prepare_report, validate_bindings
from .job_queue import JobQueue, JobClaim, LeaseLost
from .task_lifecycle import TaskLifecycleRepository, TaskLifecycleWrites


ACTIVE = ("queued", "running")
TERMINAL = ("completed", "failed", "cancelled", "blocked")
MAX_USER_CHARS = 16_000
MAX_HISTORY_CHARS = 64_000
MAX_OUTPUT_CHARS = 32_000
MAX_TOOL_CALLS = 96
MAX_TURNS = 50
RUN_TIMEOUT_SECONDS = 600.0
MAX_EVENT_JSON_CHARS = 80_000
MAX_AUTHORITATIVE_EVENT_JSON_CHARS = 1_000_000
# Permit character-sized chunks for both a full draft and its citation-only repair.
MAX_PROVIDER_EVENTS = 2 * MAX_OUTPUT_CHARS + 6 * MAX_TOOL_CALLS
POST_GENERATION_TRANSPORT_CHAR_MULTIPLIER = 4
MAX_REPAIR_SECONDS = 60.0
MAX_SEMANTIC_REVIEW_SECONDS = 120.0
REVIEW_CLEANUP_RESERVE_SECONDS = 9.0
MAX_CONCURRENT_SEMANTIC_REVIEW_SCOPES = 3
REPAIR_CLEANUP_RESERVE_SECONDS = 9.0
SEMANTIC_REVIEW_RESERVE_SECONDS = MAX_SEMANTIC_REVIEW_SECONDS + REVIEW_CLEANUP_RESERVE_SECONDS

_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS conversations (
 conversation_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, title TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
 status TEXT NOT NULL CHECK(status IN ('queued','running','completed','failed','cancelled','blocked')),
 error_json TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
 owner_pid INTEGER, cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_conversation
 ON runs(conversation_id) WHERE status IN ('queued','running');
CREATE TABLE IF NOT EXISTS messages (
 message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
 role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL,
 created_at TEXT NOT NULL, run_id TEXT REFERENCES runs(run_id), status TEXT NOT NULL,
 citations_json TEXT, verification_json TEXT
);
CREATE INDEX IF NOT EXISTS messages_conversation_idx ON messages(conversation_id,created_at);
CREATE TABLE IF NOT EXISTS events (
 run_id TEXT NOT NULL REFERENCES runs(run_id), seq INTEGER NOT NULL,
 type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(run_id,seq)
);
CREATE TABLE IF NOT EXISTS chat_submission_requests (
 request_id TEXT PRIMARY KEY, request_fingerprint TEXT NOT NULL,
 conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
 run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
 user_message_id TEXT NOT NULL UNIQUE REFERENCES messages(message_id),
 created_at TEXT NOT NULL
);
"""
_CITATION = re.compile(r"\[(claim|evidence|passage|calculation|query):([^\]\s]+)\]")


class ChatError(RuntimeError):
    """Stable, safe error classification for an HTTP adapter."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "status_code": self.status_code}


class _UnavailableProvider:
    def status(self):
        return {"available": False, "reason": "no provider configured", "provider": "none"}

    def stream(self, messages, tools, cancel_event):  # pragma: no cover - submit rejects first
        raise RuntimeError("provider unavailable")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _database_paths_alias(first: Path, second: Path) -> bool:
    """Detect lexical/symlink aliases and hard links without creating either path."""
    first = first.resolve()
    second = second.resolve()
    if first == second:
        return True
    try:
        return first.exists() and second.exists() and os.path.samefile(first, second)
    except OSError:
        return False


class _RepairCancellation:
    """Expose user/run cancellation plus a repair-local stop without conflating them."""

    def __init__(self, run_cancel: threading.Event, local_cancel: threading.Event):
        self.run_cancel = run_cancel
        self.local_cancel = local_cancel

    def is_set(self):
        return self.run_cancel.is_set() or self.local_cancel.is_set()

    def set(self):
        self.local_cancel.set()


class ChatService:
    """SQLite-backed chat service backed exclusively by durable worker jobs."""

    max_user_chars = MAX_USER_CHARS
    max_history_chars = MAX_HISTORY_CHARS
    max_output_chars = MAX_OUTPUT_CHARS
    max_tool_calls = MAX_TOOL_CALLS
    max_turns = MAX_TURNS
    max_provider_events = MAX_PROVIDER_EVENTS
    run_timeout_seconds = RUN_TIMEOUT_SECONDS

    def __init__(self, evidence_db, chat_db, provider=None, *, document_service=None,
                 official_sources_factory=None, native_data_service=None, source_acquisition=None,
                 verification_config=None, job_lease_seconds=90.0,
                 job_retry_delays=(30.0, 120.0), material_object_store_factory=None,
                 evidence_reader_factory=None):
        evidence_db = Path(evidence_db).resolve()
        chat_db = Path(chat_db).resolve()
        if _database_paths_alias(evidence_db, chat_db):
            raise ChatError("database_path_collision", "evidence and chat databases must be distinct")
        self.evidence_db = evidence_db
        if evidence_reader_factory is None:
            from .service import EvidenceService
            evidence_reader_factory = EvidenceService
        self.evidence_reader_factory = evidence_reader_factory
        self.evidence_reader = evidence_reader_factory(evidence_db)
        self.chat_db = chat_db
        self.chat_db.parent.mkdir(parents=True, exist_ok=True)
        self.provider = provider if provider is not None else _UnavailableProvider()
        self.document_service = document_service
        self.native_data = native_data_service
        self.source_acquisition = source_acquisition
        self.official_sources_factory = official_sources_factory or OfficialSources
        self._lock = threading.RLock()
        self._submission_locks_guard = threading.Lock()
        self._submission_locks: dict[str, dict] = {}
        self._closed = False
        self._durable_claim: JobClaim | None = None
        self.jobs = JobQueue(
            self._connect, lease_seconds=job_lease_seconds, retry_delays=job_retry_delays,
            lifecycle_writes=TaskLifecycleWrites())
        self.task_lifecycle = TaskLifecycleRepository(self._connect, self.jobs)
        with closing(self._connect()) as connection:
            mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode != "wal":
                raise ChatError("chat_storage_unavailable", "Chat storage requires WAL mode for concurrent research and source reading", 503)
            connection.executescript(_SCHEMA)
            self.jobs.initialize(connection)
        self.inputs = ResearchInputStore(self._connect)
        self.records = ResearchRecordStore(self._connect)
        self.scopes = self.inputs.scopes
        self.publications = ReportPublications(self._connect)
        self.verification_store = None
        self.verification_executor = None
        from .verification_store import VerificationStore
        from .verification_executor import VerificationApplication
        self.verification_store = VerificationStore(self._connect, self.jobs, self.publications)
        self.verification_store.initialize_feature()
        from .publication_snapshot import PublicationSnapshotPreparer
        self.verification_store.set_snapshot_preparer(
            PublicationSnapshotPreparer(self, self.verification_store))

        def verification_research(packet):
            with closing(self._connect()) as connection:
                company_id = connection.execute(
                    "SELECT company_id FROM conversations WHERE conversation_id=?",
                    (packet["conversation_id"],)).fetchone()[0]
            return self.research_for(
                {"company_id": company_id, "conversation_id": packet["conversation_id"]},
                frozen_snapshot=packet["input_snapshot"])

        native_verification_definitions = None
        if self.native_data is not None:
            def native_verification_definitions(packet, cancelled):
                scope = packet["input_snapshot"].get("native_sources")
                if not isinstance(scope, dict):
                    return []
                from .native_tools import definitions
                return definitions(
                    self.native_data, owner=packet["conversation_id"],
                    run_id=packet["author_run_id"], scope=scope,
                    cancelled=cancelled.is_set)
        from .answer_review import validated_document_facts_from_receipts
        def verification_document_facts(packet, receipts):
            company_id = packet["input_snapshot"].get("evidence", {}).get("company_id")
            return validated_document_facts_from_receipts(
                self, packet["author_run_id"], company_id, receipts,
                expected_snapshot=packet["input_snapshot"])
        native_query_replay = None
        if self.native_data is not None:
            def native_query_replay(query_id, *, owner, run_id, scope):
                permitted = {
                    item["snapshot_id"]
                    for item in self.native_data.list_sources(owner=owner, scope=scope)
                }
                recorded = self.native_data.read_query(
                    query_id, owner=owner, run_id=run_id)
                if any(item.get("source_id") not in permitted
                       for item in recorded.get("sources", [])):
                    from .native_sql import NativeSqlError
                    raise NativeSqlError(
                        "native_source_out_of_scope",
                        "Query source is outside the candidate's frozen source scope")
                return self.native_data.replay(
                    query_id, owner=owner, run_id=run_id)
        self.verification_executor = VerificationApplication(
            self.verification_store, self.provider, verification_research,
            native_definitions_factory=native_verification_definitions,
            document_fact_factory=verification_document_facts,
            research_result_adapter=lambda research, _name, result:
                self._authoritative_document_fact_result(research, result),
            native_query_replay=native_query_replay,
            config=verification_config)
        from .research_decisions import ResearchDecisions
        self.decisions = ResearchDecisions(self)
        from .report_versions import ReportVersions
        self.report_versions = ReportVersions(self)
        from .session_materials import SessionMaterials
        self.materials = SessionMaterials(
            self, object_store_factory=material_object_store_factory)

    def documents_for(self, context):
        conversation_id = context.get("conversation_id")
        if not conversation_id and context.get("run_id"):
            with closing(self._connect()) as connection:
                row = connection.execute("SELECT conversation_id FROM runs WHERE run_id=?",
                                         (context["run_id"],)).fetchone()
            if row is not None:
                conversation_id = row["conversation_id"]
        return self.materials.documents(conversation_id) if conversation_id else self.document_service

    def execution_scope_for(self, context):
        conversation_id = context.get("conversation_id")
        if not conversation_id and context.get("run_id"):
            with closing(self._connect()) as connection:
                row = connection.execute("SELECT conversation_id FROM runs WHERE run_id=?",
                                         (context["run_id"],)).fetchone()
            if row is not None:
                conversation_id = row["conversation_id"]
        if not conversation_id:
            raise ChatError("scope_owner_missing", "Research scope requires a persisted conversation", 409)
        try:
            return self.scopes.read(conversation_id)
        except ToolError as exc:
            raise ChatError(exc.code, exc.message, 409) from exc

    def research_for(self, context, *, evidence_db=None, frozen_snapshot=None, include_documents=True):
        from .scoped_tools import tools_for_scope
        scope = self.execution_scope_for(context)
        selected_db = evidence_db if evidence_db is not None else self.evidence_db
        reader = (self.evidence_reader if evidence_db is None
                  else self.evidence_reader_factory(selected_db))
        return tools_for_scope(
            selected_db, context["company_id"], scope["company_ids"],
            self.documents_for(context) if include_documents else None,
            frozen_snapshot=frozen_snapshot, evidence_reader=reader)

    def document_company_for(self, answer, document_version_id):
        scope = self.execution_scope_for(answer)
        subjects = answer["source_snapshot"].get("subjects")
        if len(scope["company_ids"]) == 1:
            return scope["company_ids"][0]
        owners = [identifier for identifier in scope["company_ids"]
                  if isinstance(subjects, dict) and document_version_id in
                  subjects.get(identifier, {}).get("documents", {}).get("document_version_ids", [])]
        if not owners:
            raise ChatError("source_snapshot_mismatch", "Document has no authorized frozen subject binding", 409)
        return owners[0]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.chat_db, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def status(self) -> dict:
        try:
            raw = self.provider.status()
            provider_status = dict(raw) if isinstance(raw, dict) else {}
        except Exception:
            provider_status = {"available": False, "reason": "provider status check failed", "provider": "unknown"}
        provider_status.setdefault("available", False)
        provider_status.setdefault("reason", "provider unavailable" if not provider_status["available"] else "")
        return {
            "available": bool(provider_status["available"]),
            "provider": provider_status,
            "blocked": None if provider_status["available"] else {"cause": "provider_unavailable", "message": provider_status["reason"]},
            "budgets": {
                "max_user_chars": self.max_user_chars, "max_history_chars": self.max_history_chars,
                "max_output_chars": self.max_output_chars, "max_tool_calls": self.max_tool_calls,
                "max_turns": self.max_turns, "max_provider_events": self.max_provider_events,
                "run_timeout_seconds": self.run_timeout_seconds,
            },
            "capabilities": {
                "streaming": True, "cancellation": True, "tools": True, "persistent_history": True,
                "report_continuation": True, "chat_submission_idempotency": True,
                "durable_execution": True,
                "durable_verify_executor": True,
            },
        }

    def list_verification_candidates(self, conversation_id, *, limit=50):
        if self.verification_store is None:
            return []
        return self.verification_store.list_candidates(conversation_id, limit=limit)

    def get_verification_candidate(self, conversation_id, candidate_id):
        if self.verification_store is None:
            raise ChatError("verification_not_enabled", "Required verification is not enabled", 404)
        return self.verification_store.get_candidate(conversation_id, candidate_id)

    def get_verification_version(self, conversation_id, candidate_id, version_id):
        if self.verification_store is None:
            raise ChatError("verification_not_enabled", "Required verification is not enabled", 404)
        return self.verification_store.read_version(conversation_id, candidate_id, version_id)

    def published_verification_evidence(self, conversation_id, message_id):
        """Read immutable verifier receipts bound to one exact formal message."""
        if self.verification_store is None:
            return None
        try:
            return self.verification_store.read_published_evidence(conversation_id, message_id)
        except Exception as exc:
            from .verification_store import VerificationNotFound
            if isinstance(exc, VerificationNotFound):
                return None
            raise

    def published_evidence_for_author_run(self, run_id):
        if self.verification_store is None:
            return None
        return self.verification_store.published_evidence_for_author_run(run_id)

    def cancel_verification(self, conversation_id, candidate_id):
        candidate = self.get_verification_candidate(conversation_id, candidate_id)
        with closing(self._connect()) as connection, connection:
            status = self.jobs.cancel_in_transaction(connection, candidate["verify_job_id"])
        return {"candidate_id": candidate_id, "status": status}

    def continue_verification(self, conversation_id, candidate_id,
                              expected_version_id, request_id):
        if self.verification_store is None:
            raise ChatError("verification_not_enabled", "Required verification is not enabled", 404)
        return self.verification_store.continue_verification(
            conversation_id, candidate_id, expected_version_id, request_id)

    def create_conversation(self, company_id, *, company_ids=None) -> dict:
        if self._closed:
            raise ChatError("service_closed", "chat service is closed", 503)
        if not isinstance(company_id, str) or not company_id.strip() or len(company_id.strip()) > 100:
            raise ChatError("invalid_company", "company_id must contain 1..100 characters")
        company_id = company_id.strip()
        try:
            from .scoped_tools import tools_for_scope
            research = tools_for_scope(
                self.evidence_db, company_id,
                company_ids if company_ids is not None else [company_id],
                self.document_service, evidence_reader=self.evidence_reader)
        except ToolError as exc:
            raise ChatError(exc.code, exc.message, 404) from exc
        conversation_id = _id("conv")
        created_at = _now()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO conversations VALUES(?,?,?,?,?)",
                (conversation_id, company_id, "New research conversation", created_at, created_at),
            )
            self.scopes.create(connection, conversation_id, getattr(research, "company_ids", [company_id]))
        return self.get_conversation(conversation_id)

    def list_conversations(self, company_id=None, *, include_internal=False) -> dict:
        if company_id is not None and (not isinstance(company_id, str) or not company_id.strip()):
            raise ChatError("invalid_company", "company_id must be a non-empty string")
        if type(include_internal) is not bool:
            raise ChatError("invalid_include_internal", "include_internal must be a boolean")
        with closing(self._connect()) as connection:
            task_tables = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('research_tasks','research_task_runs')"
            ).fetchall()
            has_task_linkage = len(task_tables) == 2
            sql = "SELECT c.* FROM conversations c"
            conditions, params = [], []
            if company_id is not None:
                conditions.append("c.company_id=?")
                params.append(company_id.strip())
            if has_task_linkage and not include_internal:
                conditions.append(
                    "NOT EXISTS (SELECT 1 FROM research_task_runs tr "
                    "JOIN research_tasks t ON t.task_id=tr.task_id "
                    "WHERE tr.conversation_id=c.conversation_id AND t.origin='machine_acceptance')"
                )
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY c.updated_at DESC,c.conversation_id"
            rows = connection.execute(sql, tuple(params)).fetchall()
            conversations = [self._conversation_dict(connection, row, include_messages=False) for row in rows]
        return {"conversations": conversations}

    def get_conversation(self, conversation_id) -> dict:
        conversation_id = self._valid_id(conversation_id, "conversation_id")
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
            if row is None:
                raise ChatError("conversation_not_found", "conversation does not exist", 404)
            return self._conversation_dict(connection, row, include_messages=True)

    @staticmethod
    def _valid_id(value, name: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 200:
            raise ChatError("invalid_identifier", f"{name} is invalid")
        return value

    @staticmethod
    def _display_question_for_conversation(connection, conversation_id: str) -> dict[str, str] | None:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('research_tasks','research_task_runs')"
        ).fetchall()
        if len(tables) != 2:
            return None
        # A task question is optional display metadata. Legacy/partial task
        # schemas must not break the explicit internal-history view. The
        # separate public-list origin filter still fails closed on bad linkage.
        required = {"research_tasks": {"task_id", "question"},
                    "research_task_runs": {"task_id", "conversation_id", "created_at"}}
        for table, columns in required.items():
            available = {item[1] for item in connection.execute(f"PRAGMA table_info({table})")}
            if not columns <= available:
                return None
        row = connection.execute(
            "SELECT t.task_id,t.question FROM research_task_runs tr "
            "JOIN research_tasks t ON t.task_id=tr.task_id "
            "WHERE tr.conversation_id=? ORDER BY tr.created_at DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return {"task_id": row["task_id"], "question": row["question"]}

    @staticmethod
    def _latest_conversation_decision(connection, conversation_id: str) -> dict | None:
        """Project the latest immutable decision and the report it was submitted from."""
        row = connection.execute(
            "SELECT d.*,a.selected_json,a.notes,a.action,a.continuation_run_id,g.groups_json "
            "FROM research_decisions d JOIN runs r USING(run_id) "
            "LEFT JOIN research_decision_answers a USING(decision_id) "
            "LEFT JOIN research_decision_groups g USING(decision_id) "
            "WHERE r.conversation_id=? ORDER BY d.created_at DESC,d.rowid DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "decision_id": row["decision_id"], "run_id": row["run_id"],
            **ChatService._decision_origin(connection, conversation_id, row["run_id"]),
            "kind": row["kind"], "question": row["question"], "explanation": row["explanation"],
            "options": json.loads(row["options_json"]), "multiple": bool(row["multiple"]),
            "groups": json.loads(row["groups_json"] or "[]"), "created_at": row["created_at"],
            "status": row["action"] or "pending",
            "selected_ids": json.loads(row["selected_json"]) if row["selected_json"] else [],
            "notes": row["notes"], "continuation_run_id": row["continuation_run_id"],
        }

    @staticmethod
    def _decision_origin(connection, conversation_id: str, run_id: str) -> dict:
        source_message = connection.execute(
            "SELECT rowid,content FROM messages WHERE conversation_id=? AND run_id=? AND role='user' "
            "ORDER BY created_at,rowid LIMIT 1", (conversation_id, run_id),
        ).fetchone()
        base = None if source_message is None else connection.execute(
            "SELECT message_id FROM messages WHERE conversation_id=? AND role='assistant' "
            "AND status='completed' AND rowid<? ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (conversation_id, source_message["rowid"]),
        ).fetchone()
        return {
            "base_message_id": base["message_id"] if base is not None else None,
            "request_message": source_message["content"] if source_message is not None else None,
        }

    def _conversation_dict(self, connection, row, *, include_messages: bool) -> dict:
        result = {key: row[key] for key in ("conversation_id", "company_id", "title", "created_at")}
        display_question = self._display_question_for_conversation(connection, row["conversation_id"])
        if display_question is not None:
            result["display_question"] = display_question["question"]
            result["display_task_id"] = display_question["task_id"]
        try:
            result["execution_scope"] = self.scopes.read_in(connection, row["conversation_id"])
        except ToolError as exc:
            raise ChatError(exc.code, exc.message, 409) from exc
        if include_messages:
            messages = connection.execute(
                "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at,rowid", (row["conversation_id"],)
            ).fetchall()
            result["messages"] = [self._message_dict(item) for item in messages]
            result["decision"] = self._latest_conversation_decision(connection, row["conversation_id"])
            reports = [item for item in result["messages"]
                       if item["role"] == "assistant" and item["status"] == "completed"]
            result["report_versions"] = self.report_versions.project(
                connection, row["conversation_id"], reports)
            runs = connection.execute(
                "SELECT r.run_id,r.status,r.error_json,r.cancel_requested,e.payload_json "
                "FROM runs r JOIN events e ON e.run_id=r.run_id "
                "WHERE r.conversation_id=? AND e.type='data_snapshot' ORDER BY r.created_at DESC LIMIT 2",
                (row["conversation_id"],),
            ).fetchall()
            latest = connection.execute(
                "SELECT r.run_id,m.message_id,e.payload_json FROM runs r "
                "JOIN messages m ON m.run_id=r.run_id AND m.role='assistant' AND m.status='completed' "
                "JOIN events e ON e.run_id=r.run_id AND e.type='data_snapshot' "
                "WHERE r.conversation_id=? AND r.status='completed' "
                "ORDER BY m.created_at DESC,m.rowid DESC LIMIT 1",
                (row["conversation_id"],),
            ).fetchone()
            # Reading saved history must not depend on today's source index being
            # healthy. Preserve the frozen answer while reporting new-input failure.
            try:
                research = self.research_for(dict(row))
                newest_available = {
                    "available": True, "data_version": research.data_version,
                    "documents": {
                        "coverage": research.document_coverage,
                        "document_version_ids": list(research.document_version_ids),
                    },
                }
            except (ToolError, DocumentError, sqlite3.Error, OSError, RuntimeError, ValueError):
                newest_available = {
                    "available": False,
                    "reason": "Current source coverage cannot be verified; saved answers remain readable.",
                }
            result["research_scope"] = {
                "scope_type": "frozen_per_run",
                "latest_snapshot": None if latest is None else {
                    "run_id": latest["run_id"], "message_id": latest["message_id"],
                    "snapshot": self.effective_snapshot(latest["run_id"], row["company_id"]),
                },
                "runs": [
                    {
                        "run_id": item["run_id"], "status": item["status"],
                        "snapshot": self.effective_snapshot(item["run_id"], row["company_id"]),
                        **self._termination_fields(item),
                    }
                    for item in reversed(runs)
                ],
                "newest_available": newest_available,
            }
        return result

    def _snapshot_addition_descriptor(
        self, company_id: str, payload: dict, document_service=None,
    ) -> dict:
        """Revalidate an append-only addition against its exact archived bytes."""
        record = payload.get("document") if isinstance(payload, dict) else None
        source = payload.get("source") if isinstance(payload, dict) else None
        ids = payload.get("document_version_ids") if isinstance(payload, dict) else None
        if (not isinstance(record, dict) or not isinstance(source, dict)
                or payload.get("company_id") != company_id or record.get("company_id") != company_id
                or not isinstance(ids, list) or len(ids) != 1
                or ids[0] != record.get("document_version_id")
                or source.get("company_id") != company_id
                or source.get("source_sha256") != record.get("source_sha256")
                or source.get("byte_length") != record.get("byte_length")):
            raise ChatError("source_snapshot_mismatch", "snapshot addition identity is invalid", 409)
        documents = document_service if document_service is not None else self.document_service
        if documents is None:
            raise ChatError("source_snapshot_unavailable", "document source service is unavailable", 409)
        try:
            raw = documents.read_raw_version(
                company_id=company_id, document_version_id=ids[0])
        except DocumentError as exc:
            raise ChatError("source_snapshot_mismatch", "snapshot addition archive is unavailable", 409) from exc
        if hashlib.sha256(raw).hexdigest() != record.get("source_sha256") or len(raw) != record.get("byte_length"):
            raise ChatError("source_snapshot_mismatch", "snapshot addition archive was changed", 409)
        descriptor = {key: record.get(key) for key in (
            "document_version_id", "source_key", "source_version", "title", "source_url",
            "source_sha256", "published_at", "collected_at", "media_type", "rights_status",
            "parse_status", "parse_error", "supersedes_document_version_id",
        )}
        if record.get("source_scope") == "session_material":
            descriptor.update(source_scope="session_material", conversation_id=record.get("conversation_id"))
        if record.get("parse_status") == "parsed":
            try:
                frozen = documents.read_frozen_version(
                    company_id=company_id, document_version_id=ids[0])
            except DocumentError as exc:
                raise ChatError("source_snapshot_mismatch", "snapshot addition cannot be reproduced", 409) from exc
            if any(frozen.get(key) != descriptor.get(key) for key in descriptor):
                raise ChatError("source_snapshot_mismatch", "snapshot addition metadata was changed", 409)
            descriptor["parser_version"] = frozen["parser_version"]
            descriptor["tables"] = [{"table_index": item["table_index"],
                                      "table_sha256": item["table_sha256"]}
                                     for item in frozen.get("tables", [])]
        return descriptor

    def _grant_snapshot_addition(
        self, event, *, research, run_id, cancel_event, retrieval,
        authorized_company_ids, claim: JobClaim,
    ):
        """Validate and grant one archive addition within the immutable run membership."""
        if cancel_event.is_set():
            raise ToolError("cancelled", "run cancellation requested")
        if not isinstance(event, dict):
            raise ToolError("invalid_snapshot_addition", "snapshot addition event is invalid")
        company_id = event.get("company_id")
        authorized = frozenset(authorized_company_ids)
        actual = frozenset(getattr(research, "company_ids", (research.company_id,)))
        if authorized != actual or company_id not in authorized:
            raise ToolError("invalid_snapshot_addition", "snapshot subject is outside the run scope")
        subject = research.for_company(company_id) if hasattr(research, "subjects") else research
        if subject.company_id != company_id:
            raise ToolError("invalid_snapshot_addition", "snapshot subject differs from the selected tools")
        record = event.get("document")
        source = event.get("source")
        descriptor = subject.prepare_document_addition(record)
        version_id = descriptor["document_version_id"]
        if (event.get("document_version_ids") != [version_id]
                or not isinstance(source, dict) or source.get("company_id") != company_id
                or source.get("source_sha256") != record.get("source_sha256")
                or source.get("byte_length") != record.get("byte_length")):
            raise ToolError("invalid_snapshot_addition", "snapshot provenance differs from archive")
        if (source.get("kind") == "configured_public_acquisition"
                and subject._public_acquisition is None):
            raise ToolError("public_acquisition_unavailable", "This run cannot acquire new public sources")
        if source.get("kind") == "pdf_table_derivation":
            base_version_id = source.get("base_document_version_id")
            if base_version_id not in subject.document_version_ids:
                raise ToolError(
                    "invalid_snapshot_addition",
                    "derived snapshot base version is outside the selected subject",
                )
        payload = {key: value for key, value in event.items() if key != "type"}
        payload.update({"authoritative": True, "added_at": _now()})
        # Document registration and this chat event use separate stores. Cancellation
        # can leave an ungranted archive version, but can never widen this run's scope.
        with self._lock:
            if cancel_event.is_set():
                raise ToolError("cancelled", "run cancellation requested")
            subject_has = version_id in subject.document_version_ids
            aggregate_has = version_id in research.document_version_ids
            if subject_has and not aggregate_has:
                raise ToolError(
                    "invalid_snapshot_addition", "Subject and aggregate document scopes disagree"
                )
            if aggregate_has and next((item for item in research.document_coverage["documents"]
                                       if item["document_version_id"] == version_id), None) != descriptor:
                raise ToolError("invalid_snapshot_addition", "Shared document descriptors disagree")
            if subject_has:
                return {"status": "already_granted", "document_version_id": version_id}
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                active = connection.execute(
                    "SELECT 1 FROM runs WHERE run_id=? AND status='running' AND cancel_requested=0",
                    (run_id,),
                ).fetchone()
                if active is None:
                    raise ToolError("cancelled", "run is no longer active")
                event_seq = self._insert_event(
                    connection, run_id, "data_snapshot_addition", payload, claim=claim)
                try:
                    current = self.records.read_snapshot_in(connection, run_id)
                    complete = self._snapshot_after_addition(
                        current.snapshot_object(), descriptor, company_id,
                        tuple(sorted(authorized)), payload, event_seq)
                    self.records.append_snapshot(
                        connection, run_id=run_id, snapshot=complete, created_at=_now())
                except ResearchRecordError as exc:
                    raise ChatError(exc.code, exc.message, 409) from exc
            if hasattr(research, "add_document_for"):
                research.add_document_for(company_id, descriptor)
            else:
                research.add_document(descriptor)
            retrieval.add_document_version(version_id)
        return {"status": "granted", "document_version_id": version_id}

    @staticmethod
    def _snapshot_after_addition(snapshot: dict, descriptor: dict, company_id: str,
                                 company_ids: tuple[str, ...], payload: dict,
                                 event_seq: int) -> dict:
        """Build the next complete snapshot from formal state and one validated grant."""
        complete = deepcopy(snapshot)

        def append_document(value: dict, *, sort_merged: bool = False) -> None:
            documents = value.get("documents")
            coverage = documents.get("coverage") if isinstance(documents, dict) else None
            versions = coverage.get("documents") if isinstance(coverage, dict) else None
            ids = documents.get("document_version_ids") if isinstance(documents, dict) else None
            if not isinstance(versions, list) or not isinstance(ids, list):
                raise ChatError("source_snapshot_unavailable", "Formal document scope is invalid", 409)
            version_id = descriptor["document_version_id"]
            if version_id in ids:
                if sort_merged and next((item for item in versions
                                         if item.get("document_version_id") == version_id), None) == descriptor:
                    return
                raise ChatError("source_snapshot_mismatch", "Snapshot addition is duplicated or conflicting", 409)
            ids.append(version_id)
            versions.append(deepcopy(descriptor))
            if sort_merged:
                versions.sort(key=lambda item: item["document_version_id"])
                documents["document_version_ids"] = [item["document_version_id"] for item in versions]
            coverage["enrolled"] = int(coverage.get("enrolled", 0)) + 1
            status_key = {"parsed": "parsed", "parse_failed": "failed"}.get(
                descriptor.get("parse_status"), "unsupported")
            coverage[status_key] = int(coverage.get(status_key, 0)) + 1

        if len(company_ids) > 1:
            subjects = complete.get("subjects")
            if not isinstance(subjects, dict) or not isinstance(subjects.get(company_id), dict):
                raise ChatError("source_snapshot_mismatch", "Formal subject snapshot is invalid", 409)
            append_document(subjects[company_id])
            append_document(complete, sort_merged=True)
        else:
            append_document(complete)
        derivations = complete.setdefault("derivations", [])
        if not isinstance(derivations, list):
            raise ChatError("source_snapshot_mismatch", "Formal snapshot derivations are invalid", 409)
        derivations.append({
            "type": "data_snapshot_addition", "event_seq": event_seq,
            "recorded_at": payload["added_at"],
            "document_version_ids": [descriptor["document_version_id"]],
            "source": deepcopy(payload["source"]),
        })
        return complete

    def effective_snapshot(self, run_id: str, company_id: str | None = None) -> dict:
        """Read the latest append-only complete formal snapshot; events are display-only."""
        run_id = self._valid_id(run_id, "run_id")
        try:
            record = self.records.read_snapshot(run_id)
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT conversation_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise ChatError("run_not_found", "run does not exist", 404)
            scope = self.execution_scope_for({"conversation_id": row["conversation_id"]})
            self.records.assert_scope(
                run_id, row["conversation_id"], tuple(scope["company_ids"]))
            if company_id is not None and company_id not in scope["company_ids"]:
                raise ChatError("source_snapshot_mismatch", "snapshot company differs", 409)
            return record.snapshot_object()
        except ResearchRecordError as exc:
            raise ChatError(exc.code, exc.message, 409) from exc

    @staticmethod
    def _find_reference(value: Any, kind: str, identifier: str) -> dict | None:
        key = f"{kind}_id"
        if isinstance(value, dict):
            if value.get(key) == identifier:
                return value
            for child in value.values():
                found = ChatService._find_reference(child, kind, identifier)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = ChatService._find_reference(child, kind, identifier)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _evidence_claim_membership(snapshot: dict, evidence_id: str) -> set[str]:
        """Return the canonical claim set for one evidence span in a report snapshot."""
        memberships = {
            item["claim_id"] for item in snapshot.get("claims", [])
            if isinstance(item, dict) and isinstance(item.get("claim_id"), str)
            and isinstance(item.get("evidence_ids"), list)
            and evidence_id in item["evidence_ids"]
        }
        memberships.update(
            item["claim_id"] for item in snapshot.get("evidence_chains", [])
            if isinstance(item, dict) and isinstance(item.get("claim_id"), str)
            and isinstance(item.get("evidence_ids"), list)
            and evidence_id in item["evidence_ids"]
        )
        return memberships

    @classmethod
    def _evidence_linkage_matches(cls, item: dict, frozen_item: dict,
                                  source: dict, frozen_source: dict, report: dict,
                                  frozen: dict, company_id: str, citation: dict,
                                  original: dict) -> bool:
        """Compare immutable evidence while treating valid shared-claim links as a set."""
        company_ids = (company_id,) if isinstance(company_id, str) else tuple(company_id)
        if not all(isinstance(value, dict) for value in
                   (item, frozen_item, source, frozen_source, report, frozen,
                    citation, original)):
            return False
        current_members = cls._evidence_claim_membership(report, item.get("evidence_id"))
        frozen_members = cls._evidence_claim_membership(frozen, frozen_item.get("evidence_id"))
        if current_members != frozen_members:
            return False
        if not current_members:
            # Evidence may support a tool-returned fact without any curated claim.
            # Accept only ownership already captured in this run's frozen facts;
            # do not backfill old incomplete snapshots from today's database.
            def owning_facts(snapshot):
                return {fact.get("fact_id"): fact for fact in snapshot.get("facts", [])
                        if isinstance(fact, dict) and fact.get("evidence_id") == item.get("evidence_id")}
            current_facts, frozen_facts = owning_facts(report), owning_facts(frozen)
            if (not current_facts or current_facts != frozen_facts
                    or any(not identifier or fact.get("company_id") not in company_ids
                           for identifier, fact in frozen_facts.items())):
                return False

        # A serializer may select any one owning claim when a span is shared.
        # Everything except that projection remains an exact frozen comparison.
        linkage_keys = {"claim_id", "claim_ids"}
        current_raw = {key: value for key, value in item.items() if key not in linkage_keys}
        frozen_raw = {key: value for key, value in frozen_item.items() if key not in linkage_keys}
        if (current_raw != frozen_raw or source != frozen_source
                or item.get("source_id") != source.get("source_id")
                or item.get("source_sha256") != source.get("sha256")):
            return False

        claims_by_snapshot = (
            ({claim.get("claim_id"): claim for claim in snapshot.get("claims", [])
              if isinstance(claim, dict)}, members)
            for snapshot, members in ((report, current_members), (frozen, frozen_members))
        )
        if any(claim_id not in claims or claims[claim_id].get("company_id") not in company_ids
               for claims, members in claims_by_snapshot for claim_id in members):
            return False

        for record in (item, frozen_item, citation, original):
            if record.get("company_id") not in (None, *company_ids):
                return False
            linked = record.get("claim_id")
            if linked is not None and (not isinstance(linked, str) or linked not in frozen_members):
                return False
            linked_many = record.get("claim_ids")
            if linked_many is not None and (
                    not isinstance(linked_many, list) or not linked_many
                    or any(not isinstance(value, str) or value not in frozen_members
                           for value in linked_many)):
                return False

        for record in (citation, original):
            for key in ("source_id", "source_sha256", "quote", "locator"):
                if record.get(key) is not None and record.get(key) != item.get(key):
                    return False
        return True

    @staticmethod
    def _numeric_cell(value: Any) -> Decimal | None:
        if not isinstance(value, str):
            return None
        candidate = value.strip().replace(",", "").replace("$", "").replace(" ", "")
        if candidate.startswith("(") and candidate.endswith(")"):
            candidate = "-" + candidate[1:-1]
        try:
            return Decimal(candidate)
        except (InvalidOperation, ValueError):
            return None

    @staticmethod
    def _frozen_document_candidates(snapshot: dict, digest: str, activity: dict | None = None) -> list[dict]:
        documents = snapshot.get("documents", {})
        ids = documents.get("document_version_ids", []) if isinstance(documents, dict) else []
        coverage = documents.get("coverage", {}) if isinstance(documents, dict) else {}
        versions = coverage.get("documents", []) if isinstance(coverage, dict) else []
        candidates = [item for item in versions if isinstance(item, dict)
                      and item.get("document_version_id") in ids
                      and item.get("source_sha256") == digest]
        parameters = activity.get("parameters") if isinstance(activity, dict) else None
        explicit_version = None
        expected_parser = None
        if isinstance(activity, dict):
            explicit_version = activity.get("document_version_id")
            expected_parser = activity.get("parser_version")
        if isinstance(parameters, dict):
            explicit_version = parameters.get("document_version_id", explicit_version)
            expected_parser = parameters.get("parser_version", expected_parser)
        if isinstance(explicit_version, str):
            candidates = [item for item in candidates
                          if item.get("document_version_id") == explicit_version]
        if isinstance(expected_parser, str):
            candidates = [item for item in candidates
                          if item.get("parser_version") == expected_parser]
        return candidates

    def _matched_fact_table(self, *, table: dict, text: str, evidence: dict,
                            row_index: int, column_index: int | None,
                            expected_numeric: Decimal) -> dict | None:
        """Verify the exact frozen locator and raw numeric cell within one parsed table."""
        locator = evidence.get("locator")
        start = locator.get("start") if isinstance(locator, dict) else None
        end = locator.get("end") if isinstance(locator, dict) else None
        if (type(start) is not int or type(end) is not int or start < 0 or end < start
                or text[start:end] != evidence.get("quote")
                or not (table["locator"]["start"] <= start <= end <= table["locator"]["end"])
                or not 0 <= row_index < len(table.get("rows", []))
                or not 0 <= row_index < len(table.get("row_spans", []))
                or list(table["row_spans"][row_index]) != [start, end]):
            return None
        row = table["rows"][row_index]
        if type(column_index) is int:
            columns = [column_index] if 0 <= column_index < len(row) else []
        else:
            columns = [index for index, value in enumerate(row)
                       if self._numeric_cell(value) == expected_numeric]
        if len(columns) != 1 or self._numeric_cell(row[columns[0]]) != expected_numeric:
            return None
        result = dict(table)
        result["target_cell"] = {
            "row_index": row_index, "column_index": columns[0], "raw_value": row[columns[0]],
        }
        return result

    def _frozen_fact_table(self, *, answer: dict, fact: dict, evidence: dict,
                           activity: dict, digest: str) -> tuple[dict | None, dict] | None:
        """Resolve a table only through its complete formal snapshot manifest."""
        documents = self.documents_for(answer)
        if documents is None:
            return None
        parameters = activity.get("parameters") if isinstance(activity, dict) else None
        locators = parameters.get("cell_locators") if isinstance(parameters, dict) else None
        cell_locator = locators.get(fact.get("period_kind")) if isinstance(locators, dict) else None
        evidence_locator = evidence.get("locator")
        if not isinstance(evidence_locator, dict):
            return None
        table_index = (cell_locator.get("table_index") if isinstance(cell_locator, dict)
                       else evidence_locator.get("table_index"))
        row_index = (cell_locator.get("row_index") if isinstance(cell_locator, dict)
                     else evidence_locator.get("row_index"))
        if type(table_index) is not int or type(row_index) is not int:
            return None
        column_index = cell_locator.get("column_index") if isinstance(cell_locator, dict) else None
        raw_values = parameters.get("raw_values") if isinstance(parameters, dict) else None
        expected_raw = raw_values.get(fact.get("period_kind")) if isinstance(raw_values, dict) else fact.get("value")
        expected_numeric = self._numeric_cell(expected_raw)
        if expected_numeric is None:
            return None

        historical_matches = []
        historical_candidates = self._frozen_document_candidates(
            answer["source_snapshot"], digest, activity
        )
        for version in historical_candidates:
            manifests = version.get("tables")
            manifest = next((item for item in manifests if isinstance(item, dict)
                             and item.get("table_index") == table_index), None) if isinstance(manifests, list) else None
            if not isinstance(manifest, dict) or not isinstance(manifest.get("table_sha256"), str):
                continue
            try:
                frozen = documents.read_frozen_version(
                    company_id=fact["company_id"],
                    document_version_id=version["document_version_id"],
                )
            except DocumentError:
                continue
            table = next((item for item in frozen["tables"]
                          if item.get("table_index") == table_index), None)
            if (not isinstance(table, dict)
                    or table.get("table_sha256") != manifest["table_sha256"]
                    or frozen.get("parser_version") != version.get("parser_version")):
                continue
            matched = self._matched_fact_table(
                table=table, text=frozen["text"], evidence=evidence,
                row_index=row_index, column_index=column_index,
                expected_numeric=expected_numeric,
            )
            if matched is not None:
                matched.update({
                    "origin": "historical_frozen_parse",
                    "renderer": "historical_frozen",
                    "historical_processing_verified": True,
                })
                historical_matches.append((version, matched))
        if len(historical_matches) == 1:
            return historical_matches[0]
        if historical_matches:
            return None

        if historical_candidates and any("tables" not in version for version in historical_candidates):
            raise ChatError("source_snapshot_mismatch", "Formal table manifest is required", 409)
        return None

    def answer_context(self, conversation_id, message_id) -> dict:
        """Return server-derived answer identity, citations, question and run snapshot."""
        conversation_id = self._valid_id(conversation_id, "conversation_id")
        message_id = self._valid_id(message_id, "message_id")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT m.*,c.company_id FROM messages m JOIN conversations c USING(conversation_id) "
                "WHERE m.message_id=? AND m.conversation_id=?",
                (message_id, conversation_id),
            ).fetchone()
            if row is None:
                raise ChatError("message_not_found", "message does not exist in this conversation", 404)
            if row["role"] != "assistant" or row["status"] != "completed" or row["run_id"] is None:
                raise ChatError("message_not_eligible", "message is not a completed assistant answer", 409)
            question = connection.execute(
                "SELECT content FROM messages WHERE conversation_id=? AND run_id=? AND role='user'",
                (conversation_id, row["run_id"]),
            ).fetchone()
            display_question = self._display_question_for_conversation(connection, conversation_id)
            run_id, company_id = row["run_id"], row["company_id"]
        snapshot = self.effective_snapshot(run_id, company_id)
        return {
            "conversation_id": conversation_id, "message_id": message_id,
            "run_id": run_id, "company_id": company_id,
            "execution_scope": self.execution_scope_for({"conversation_id": conversation_id}),
            "question": (display_question or {}).get("question")
                        or (question["content"] if question is not None else ""),
            "execution_question": question["content"] if question is not None else "",
            "answer": row["content"], "citations": json.loads(row["citations_json"] or "[]"),
            "publication_schema": json.loads(row["verification_json"] or "{}").get("publication_schema"),
            "source_snapshot": snapshot,
        }

    def read_raw_source(self, conversation_id, message_id, source_sha256):
        """Download exact answer-frozen bytes; never fetch today's publication URL."""
        if not isinstance(source_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None:
            raise ChatError("invalid_source_hash", "source_sha256 must be a SHA-256 digest")
        answer = self.answer_context(conversation_id, message_id)
        snapshot = answer['source_snapshot']
        if not isinstance(snapshot, dict) or snapshot.get('authoritative') is not True or snapshot.get('truncated') is True:
            raise ChatError('source_snapshot_unavailable', 'an intact authoritative snapshot is required', 409)
        documents = snapshot.get('documents', {})
        if not isinstance(documents, dict) or not isinstance(documents.get('coverage'), dict):
            raise ChatError('source_snapshot_unavailable', 'frozen document coverage is unavailable', 409)
        frozen_ids = documents.get('document_version_ids', [])
        versions = documents['coverage'].get('documents', [])
        if not isinstance(frozen_ids, list) or not isinstance(versions, list):
            raise ChatError('source_snapshot_unavailable', 'frozen document versions are invalid', 409)
        selected = next((version for version in versions if isinstance(version, dict)
                         and version.get('source_sha256') == source_sha256
                         and version.get('document_version_id') in frozen_ids), None)
        if selected is None:
            raise ChatError('source_not_in_snapshot', 'this raw source was not frozen for this answer', 404)
        documents = self.documents_for(answer)
        if documents is None:
            raise ChatError('document_service_unavailable', 'the frozen raw document store is unavailable', 503)
        raw = documents.read_raw_version(
            company_id=self.document_company_for(answer, selected["document_version_id"]),
            document_version_id=selected["document_version_id"])
        if hashlib.sha256(raw).hexdigest() != source_sha256:
            raise ChatError('source_snapshot_mismatch', 'raw bytes do not match the answer-frozen digest', 409)
        return raw

    @staticmethod
    def _document_fact_results(payload: dict) -> list[dict]:
        if payload.get("authoritative") is not True:
            return []
        from .evidence_checks import document_fact_results
        return document_fact_results(payload)

    @staticmethod
    def _document_fact_derivation(payloads: list[dict], *, fact_id=None, evidence_id=None):
        derivation, reused = None, None
        for payload in payloads:
            for result in ChatService._document_fact_results(payload):
                fact = result["fact"]
                if ((fact_id is None or fact.get("fact_id") == fact_id)
                        and (evidence_id is None or fact.get("evidence_id") == evidence_id)):
                    candidate = {key: value for key, value in result.items()
                                 if not key.startswith("_") and key != "reused_from"}
                    if derivation is not None and candidate != derivation:
                        return None
                    derivation = candidate
                    if "reused_from" in result:
                        if reused is not None and result["reused_from"] != reused["reused_from"]:
                            return None
                        reused = {**candidate, "reused_from": result["reused_from"]}
        # Retain the reuse linkage so source validation cannot be bypassed by
        # a matching fresh extraction in the same run.
        return reused if reused is not None else derivation

    def _validated_document_fact(self, answer: dict, result: dict, expected_fact=None, *,
                                 source_run_id=None) -> tuple[dict, dict]:
        """Resolve ownership here; L2 alone replays extraction and verifies original data."""
        fact = result.get("fact") if isinstance(result, dict) else None
        if not isinstance(fact, dict):
            raise ChatError("citation_source_mismatch", "document-cell derivation is incomplete", 409)
        if fact.get("company_id") not in self.execution_scope_for(answer)["company_ids"]:
            raise ChatError("citation_source_mismatch", "Document-cell subject is outside the research scope", 409)
        current_run_id = answer.get("run_id")
        lookup_run_id = source_run_id or current_run_id
        reuse = result.get("reused_from")
        try:
            if self.inputs.has_contract(lookup_run_id):
                original = self.inputs.read(lookup_run_id, fact.get("fact_id"),
                                            consumer_run_id=current_run_id)
                if (canonical_derivation(original) != canonical_derivation(result)
                        or (reuse is not None and reuse != original.get("reused_from"))):
                    raise InputError("input_conflict", "Document-cell derivation differs from its registered identity or origin")
            else:
                raise InputError(
                    "input_contract_unavailable",
                    "Document fact run has no formal research input contract")
            return validate_document_fact(
                snapshot=answer.get("source_snapshot"), company_id=fact["company_id"],
                documents=self.documents_for(answer), result=result, expected_fact=expected_fact)
        except (InputError, DocumentError) as exc:
            code = "source_snapshot_unavailable" if exc.code == "source_snapshot_unavailable" else "citation_source_mismatch"
            raise ChatError(code, exc.message, 409) from exc

    def _published_document_fact(self, answer: dict, citation: dict, published: dict,
                                 evidence_id: str) -> tuple[dict, dict, dict]:
        """Validate one fact from the exact verifier receipt declared by a citation."""
        reference = citation.get("verification_receipt")
        if (not isinstance(reference, dict)
                or set(reference) != {"kind", "identifier", "sha256"}
                or not all(isinstance(reference.get(key), str) and reference[key]
                           for key in ("kind", "identifier"))
                or not re.fullmatch(r"[0-9a-f]{64}", reference.get("sha256", ""))):
            raise ChatError("citation_source_mismatch", "document-cell citation lacks an exact verifier receipt", 409)
        matches = [event for event in published.get("receipts", [])
                   if isinstance(event, dict)
                   and event.get("verification_receipt_id") == reference["identifier"]
                   and event.get("verification_receipt_sha256") == reference["sha256"]]
        if len(matches) != 1:
            raise ChatError("citation_source_mismatch", "document-cell verifier receipt is not in the published set", 409)
        try:
            from .answer_review import PersistedEvidenceSource
            from .evidence_checks import EvidenceCheckError
            source = PersistedEvidenceSource(
                self, answer["run_id"], answer["company_id"],
                expected_snapshot=answer.get("source_snapshot"))
            receipts = [receipt for receipt in source.receipts
                        if receipt.receipt_id == reference["identifier"]]
            if (len(receipts) != 1
                    or source.events_by_receipt.get(reference["identifier"], {}).get(
                        "verification_receipt_sha256") != reference["sha256"]):
                raise EvidenceCheckError("published document fact receipt identity mismatch")
            records = source.validated_document_facts_for_receipts(receipts)
            derivations = self._document_fact_results(
                source.events_by_receipt[reference["identifier"]])
            matches = [(derivation, record) for derivation in derivations for record in records
                       if derivation.get("fact") == record.fact
                       and record.fact.get("evidence_id") == evidence_id]
            if len(matches) != 1:
                raise EvidenceCheckError("published receipt does not contain the cited document fact")
            derivation, record = matches[0]
            return derivation, record.fact, record.source
        except (EvidenceCheckError, DocumentError, InputError) as exc:
            message = getattr(exc, "message", str(exc))
            raise ChatError("citation_source_mismatch", message, 409) from exc

    def read_citation(self, conversation_id, message_id, kind, identifier) -> dict:
        if kind not in {"passage", "evidence", "claim", "calculation", "query"}:
            raise ChatError("invalid_citation_kind", "kind must be passage, evidence, claim, calculation, or query")
        identifier = self._valid_id(identifier, "id")
        answer = self.answer_context(conversation_id, message_id)
        company_ids = answer["execution_scope"]["company_ids"]
        documents = self.documents_for(answer)
        citation = next((item for item in answer["citations"]
                         if item.get("kind") == kind and item.get("id") == identifier
                         and item.get("valid") is True), None)
        if citation is None:
            raise ChatError("citation_not_found", "citation is not a valid reference on this answer", 404)
        try:
            payloads = [{
                "type": "tool_result", "name": receipt.tool_name,
                "call_id": receipt.call_id, "authoritative": True,
                "result": receipt.result_object(),
                "observed_arguments": receipt.arguments_object(),
                "author_receipt_revision": receipt.revision,
                "author_receipt_sha256": receipt.receipt_sha256,
            } for receipt in self.records.read_receipts(answer["run_id"])]
        except ResearchRecordError as exc:
            raise ChatError(exc.code, exc.message, 409) from exc
        published = self.published_verification_evidence(conversation_id, message_id)
        if published is not None:
            if (published.get("author_run_id") != answer["run_id"]
                    or published.get("conversation_id") != conversation_id
                    or published.get("formal_message_id") != message_id
                    or published.get("input_snapshot") != answer["source_snapshot"]):
                raise ChatError("citation_source_mismatch", "published evidence identity differs from the answer", 409)
            payloads.extend(published["receipts"])
        if kind == "query":
            from .native_sql import NativeSqlError, display_query
            if self.native_data is None:
                raise ChatError("native_source_unavailable", "Native data service is unavailable", 409)
            original = next((self._find_reference(payload.get("result"), kind, identifier)
                             for payload in payloads if payload.get("authoritative") is True
                             and payload.get("name") == "execute_native_sql"
                             and self._find_reference(payload.get("result"), kind, identifier) is not None), None)
            if original is None:
                raise ChatError("citation_source_mismatch", "Query lacks an authoritative execution result", 409)
            try:
                replay = self.native_data.replay(identifier, owner=conversation_id, run_id=answer["run_id"])
                if replay["query"] != {key: value for key, value in original.items() if not key.startswith("_")}:
                    raise ChatError("citation_source_mismatch", "Native query differs from its persisted record", 409)
                source_scope = answer["source_snapshot"].get("native_sources", {})
                if any(item["source_id"] not in source_scope.get("snapshot_ids", [])
                       for item in replay["query"]["sources"]):
                    raise ChatError("citation_source_mismatch", "Query source is outside the frozen source scope", 409)
                sources = [self.native_data.source(item["source_id"], owner=conversation_id)
                           for item in replay["query"]["sources"]]
            except NativeSqlError as exc:
                raise ChatError(exc.code, exc.message, 409) from exc
            return {**replay, "display": display_query(replay["query"]), "sources": sources,
                    "detail_schema_version": "citation-detail/1", "kind": kind, "id": identifier,
                    "company_id": answer["company_id"], "execution_scope": answer["execution_scope"],
                    "conversation_id": conversation_id, "message_id": message_id,
                    "title": "Native SQL query", "limitations": [replay["query"]["numeric_semantics"]]}
        if kind == "calculation":
            authoritative, original = None, None
            for payload in payloads:
                if (payload.get("authoritative") is True
                        and payload.get("name") in {"calculate_facts", "calculate_yoy", "calculate_regression", "calculate_forecast"}):
                    found = self._find_reference(payload.get("result"), kind, identifier)
                    if found is not None:
                        authoritative, original = payload, found
                        break
        else:
            authoritative = None
            original = next((found for payload in payloads if payload.get("authoritative") is True
                             and (found := self._find_reference(payload.get("result"), kind, identifier)) is not None), None)
        if original is None:
            raise ChatError("citation_source_mismatch", "citation is not linked to an authoritative original tool result", 409)
        base = {
            "detail_schema_version": "citation-detail/1",
            "company_id": answer["company_id"], "conversation_id": answer["conversation_id"],
            "execution_scope": answer["execution_scope"],
            "message_id": answer["message_id"], "kind": kind, "id": identifier,
            "title": None, "source_url": None, "published_at": None, "collected_at": None,
            "source_sha256": None, "document_version_id": None, "quote": None,
            "context": None, "offset": None, "locator": None, "parser_version": None,
            "rights_status": None, "tables": [], "limitations": [],
        }
        if kind == "calculation":
            if (not re.fullmatch(r"calc-[0-9a-f]{64}", identifier)
                    or citation.get("company_id") not in company_ids
                    or original.get("company_id") != citation.get("company_id")
                    or original.get("data_version") != answer["source_snapshot"].get("data_version")):
                raise ChatError("citation_source_mismatch", "calculation identity differs from the answer snapshot", 409)
            frozen = answer["source_snapshot"].get("evidence")
            if not isinstance(frozen, dict) or frozen.get("company_id") != answer["company_id"]:
                raise ChatError("source_snapshot_unavailable", "calculation input snapshot is unavailable", 409)
            frozen_facts = {item.get("fact_id"): item for item in frozen.get("facts", [])
                            if isinstance(item, dict) and isinstance(item.get("fact_id"), str)}
            frozen_evidence = {item.get("evidence_id"): item for item in frozen.get("evidence", [])
                               if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)}
            frozen_sources = {item.get("source_id"): item for item in frozen.get("sources", [])
                              if isinstance(item, dict) and isinstance(item.get("source_id"), str)}
            frozen_activities = {item.get("activity_id"): item for item in frozen.get("activities", [])
                                 if isinstance(item, dict) and isinstance(item.get("activity_id"), str)}
            inputs = original.get("inputs")
            is_regression = authoritative["name"] == "calculate_regression"
            is_forecast = authoritative["name"] == "calculate_forecast"
            from .arithmetic import is_current, MAX_SOURCE_FACTS
            is_arithmetic = is_current(original)
            if (not isinstance(inputs, list)
                    or not (1 <= len(inputs) <= MAX_SOURCE_FACTS if is_arithmetic
                            else 3 <= len(inputs) <= 120 if is_regression else len(inputs) == 2)):
                raise ChatError("citation_source_mismatch", "calculation inputs are incomplete", 409)
            input_sources = []
            dynamic_fact_ids = set()
            for fact in inputs:
                if (not isinstance(fact, dict) or not isinstance(fact.get("fact_id"), str)
                        or fact.get("company_id") not in company_ids):
                    raise ChatError("citation_source_mismatch", "calculation input identity is invalid", 409)
                if frozen_facts.get(fact["fact_id"]) == fact:
                    evidence = frozen_evidence.get(fact.get("evidence_id"))
                    source = frozen_sources.get(evidence.get("source_id")) if evidence else None
                    digest = evidence.get("source_sha256") if evidence else None
                    input_hashes = original.get("data_version", {}).get("input_hashes", {})
                    if (not isinstance(evidence, dict) or not isinstance(source, dict)
                            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                            or source.get("sha256") != digest
                            or input_hashes.get(source.get("source_id")) != digest
                            or not isinstance(evidence.get("quote"), str)
                            or not isinstance(evidence.get("locator"), dict)):
                        raise ChatError("citation_source_mismatch", "calculation input source hash or locator is missing", 409)
                    input_sources.append({
                        "fact_id": fact["fact_id"], "evidence_id": evidence["evidence_id"],
                        "source_id": source["source_id"], "title": source.get("title"),
                        "source_url": source.get("url"), "published_at": source.get("published_at"),
                        "collected_at": source.get("collected_at"), "rights_status": source.get("rights_status"),
                        "source_sha256": digest, "quote": evidence["quote"],
                        "locator": evidence["locator"],
                    })
                    summary = input_sources[-1]
                    activity = frozen_activities.get(fact.get("activity_id"))
                    if isinstance(activity, dict):
                        summary["activity"] = activity
                    matched = self._frozen_fact_table(
                        answer=answer, fact=fact, evidence=evidence,
                        activity=activity, digest=digest,
                    ) if isinstance(activity, dict) else None
                    if matched is not None:
                        version, table = matched
                        if version is not None:
                            summary["document_version_id"] = version["document_version_id"]
                        summary["table"] = table
                    continue
                derivation = self._document_fact_derivation(payloads, fact_id=fact["fact_id"])
                if derivation is None:
                    raise ChatError("citation_source_mismatch", "calculation input has no unique authoritative derivation", 409)
                verified_derivation = self._document_fact_derivation(
                    published.get("receipts", []) if published is not None else [],
                    fact_id=fact["fact_id"])
                if (verified_derivation is not None and "reused_from" not in verified_derivation
                        and canonical_derivation(verified_derivation) == canonical_derivation(derivation)):
                    try:
                        _, summary = validate_document_fact(
                            snapshot=answer.get("source_snapshot"), company_id=fact["company_id"],
                            documents=documents, result=verified_derivation, expected_fact=fact)
                    except DocumentError as exc:
                        raise ChatError("citation_source_mismatch", exc.message, 409) from exc
                else:
                    _, summary = self._validated_document_fact(answer, derivation, expected_fact=fact)
                input_sources.append(summary)
                dynamic_fact_ids.add(fact["fact_id"])

            # Re-run only the named deterministic tool against the same data version.
            # Provider-emitted tool_result events can never enter this branch.
            research = self.research_for(answer, include_documents=False)
            if research.data_version != original["data_version"]:
                for archive in sorted((self.evidence_db.parent / "archives").glob("*.sqlite3"))[:128]:
                    if archive.is_symlink() or not re.fullmatch(r"[0-9a-f]{64}", archive.stem):
                        continue
                    try:
                        if hashlib.sha256(archive.read_bytes()).hexdigest() != archive.stem:
                            continue
                        candidate = self.research_for(answer, evidence_db=archive, include_documents=False)
                        if candidate.data_version == original["data_version"]:
                            research = candidate
                            break
                    except (OSError, sqlite3.Error, ValueError, ToolError):
                        continue
            if research.data_version != original["data_version"]:
                raise ChatError("citation_source_version_changed", "the evidence store differs from the calculation's frozen source version", 409)
            current_facts = {item["fact_id"]: item for item in research._company["facts"]}
            current_evidence = {item["evidence_id"]: item for item in research._company["evidence"]}
            current_sources = {item["source_id"]: item for item in research._company["sources"]}
            for fact in inputs:
                if fact["fact_id"] in dynamic_fact_ids:
                    research._facts[fact["fact_id"]] = fact
            for fact, summary in zip(inputs, input_sources):
                if fact["fact_id"] in dynamic_fact_ids:
                    continue
                evidence = current_evidence.get(fact["evidence_id"])
                source = current_sources.get(summary["source_id"])
                locator = evidence.get("locator") if evidence else None
                start = locator.get("start") if isinstance(locator, dict) else None
                end = locator.get("end") if isinstance(locator, dict) else None
                try:
                    detail = research.service.source(summary["source_id"])
                except KeyError as exc:
                    raise ChatError("citation_source_mismatch", "calculation input source is unavailable", 409) from exc
                if (current_facts.get(fact["fact_id"]) != fact
                        or evidence != frozen_evidence.get(fact["evidence_id"])
                        or source != frozen_sources.get(summary["source_id"])
                        or detail.get("source") != source
                        or not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start
                        or detail.get("text", "")[start:end] != evidence.get("quote")):
                    raise ChatError("citation_source_mismatch", "calculation input quote or locator differs from its frozen source", 409)
            try:
                if is_regression or is_forecast:
                    model = "regression" if is_regression else "forecast"
                    replayed = self.decisions.replay_model(
                        model, answer["run_id"], research, original.get("parameters"))
                    if authoritative["result"].get("calculations") != replayed["calculations"]:
                        raise ToolError(f"{model}_record_changed", "Model outputs differ from deterministic recomputation")
                    expected = next((item for item in replayed["calculations"]
                                     if item["calculation_id"] == identifier), None)
                elif authoritative["name"] == "calculate_yoy":
                    selected_research = (research.for_company(original["company_id"])
                                         if hasattr(research, "for_company") else research)
                    expected = selected_research.calculate_yoy(
                        current_fact_id=inputs[0]["fact_id"], prior_fact_id=inputs[1]["fact_id"])
                else:
                    expected = research.replay_arithmetic(original)
            except ToolError as exc:
                raise ChatError("citation_source_mismatch", "calculation no longer passes its recorded rules", 409) from exc
            recorded = {key: value for key, value in original.items() if not key.startswith("_")}
            if recorded != expected:
                raise ChatError("citation_source_mismatch", "recorded calculation differs from deterministic recomputation", 409)
            base.update({
                "calculation": recorded,
                "formula": recorded["formula"], "value": recorded["value"],
                "unit": recorded["unit"], "inputs": recorded["inputs"],
                "input_sources": input_sources, "limitations": recorded.get("limitations", []),
                "interpretation": "unverified",
            })
            if is_regression:
                base["regression_outputs"] = [
                    {key: item[key] for key in ("operation", "formula", "value", "unit", "calculation_id")}
                    for item in authoritative["result"]["calculations"]]
            if is_forecast:
                base["forecast_model"] = replayed["model"]
                base["forecast_steps"] = replayed["steps"]
            return base
        if kind == "passage":
            if documents is None:
                raise ChatError("source_snapshot_unavailable", "document source service is unavailable", 409)
            expected_version = citation.get("document_version_id")
            expected_company = citation.get("company_id")
            snapshot_ids = answer["source_snapshot"].get("documents", {}).get("document_version_ids", [])
            if expected_company not in company_ids or expected_version not in snapshot_ids:
                raise ChatError("citation_source_mismatch", "passage linkage does not match the answer snapshot", 409)
            try:
                item = documents.read_passage(
                    company_id=expected_company, passage_id=identifier, context_chars=500)
            except DocumentError as exc:
                status = 404 if exc.code == "passage_not_found" else 409
                raise ChatError("citation_source_mismatch", "stored passage version is unavailable or changed", status) from exc
            for key in ("company_id", "document_version_id", "source_sha256", "quote", "parser_version"):
                if original.get(key) is not None and item.get(key) != original.get(key):
                    raise ChatError("citation_source_mismatch", "stored passage version differs from the answer snapshot", 409)
            # A passage reached through read_passage.context_passages is an
            # authoritative projection: its locator contains the immutable text
            # offsets, while a direct read may add derived PDF page navigation.
            # Compare every recorded locator field rather than requiring that
            # the stored, verified result omit those additional fields.
            original_locator = original.get("locator")
            item_locator = item.get("locator")
            if (original_locator is not None
                    and (not isinstance(original_locator, dict)
                         or not isinstance(item_locator, dict)
                         or any(item_locator.get(key) != value
                                for key, value in original_locator.items()))):
                raise ChatError("citation_source_mismatch", "stored passage version differs from the answer snapshot", 409)
            if item.get("document_version_id") != expected_version:
                raise ChatError("citation_source_mismatch", "stored passage version differs from the answer snapshot", 409)
            source_visibility = item.get("evidence_ref", {}).get("source", {}).get("scope")
            if source_visibility not in {"public", "private"}:
                raise ChatError("citation_source_mismatch", "document visibility is unavailable", 409)
            base.update({
                "source_company_id": expected_company, "source_visibility": source_visibility,
                "title": item.get("title"), "source_url": item.get("source_url"),
                "published_at": item.get("published_at"), "collected_at": item.get("collected_at"),
                "source_sha256": item.get("source_sha256"), "document_version_id": item.get("document_version_id"),
                "quote": item.get("quote"), "context": item.get("context"), "offset": item.get("offset"),
                "locator": item.get("locator"), "parser_version": item.get("parser_version"),
                "media_type": item.get("media_type"), "rights_status": item.get("rights_status"),
                "tables": item.get("tables", []),
            })
            if item.get("source_scope") == "session_material":
                base.update(source_scope="session_material", conversation_id=item["conversation_id"])
            return base

        if kind == "evidence" and identifier.startswith("doccell-"):
            if published is not None and citation.get("verification_receipt") is not None:
                derivation, fact, summary = self._published_document_fact(
                    answer, citation, published, identifier)
            else:
                derivation = self._document_fact_derivation(payloads, evidence_id=identifier)
                if derivation is None:
                    raise ChatError("citation_source_mismatch", "document-cell citation has no unique authoritative derivation", 409)
                fact, summary = self._validated_document_fact(answer, derivation)
            if (citation.get("company_id") != fact["company_id"]
                    or citation.get("document_version_id") != summary["document_version_id"]
                    or original != fact):
                raise ChatError("citation_source_mismatch", "document-cell citation linkage differs", 409)
            base.update({
                "source_company_id": fact["company_id"],
                "title": summary["title"], "source_url": summary["source_url"],
                "published_at": summary["published_at"], "collected_at": summary["collected_at"],
                "source_sha256": summary["source_sha256"],
                "document_version_id": summary["document_version_id"],
                "quote": summary["quote"], "locator": summary["locator"],
                "parser_version": summary["locator"].get("parser_version"),
                "rights_status": summary["rights_status"], "tables": [summary["table"]],
                "fact": fact, "interpretation": summary["interpretation"],
                "semantic_verified": False,
                "limitations": derivation.get("limitations", []),
            })
            if summary.get("source_scope") == "session_material":
                base.update(source_scope="session_material", conversation_id=summary["conversation_id"],
                            source_visibility="private")
            else:
                base["source_visibility"] = "public"
            return base

        # Legacy references depend on their frozen evidence store, not a newer
        # unrelated full-text catalog's availability.
        research = self.research_for(answer, include_documents=False)
        frozen_version = answer["source_snapshot"].get("data_version")
        if research.data_version != frozen_version and isinstance(frozen_version, dict):
            # Only content-addressed local build archives are eligible. A client
            # cannot select a path, and metadata is never trusted without a digest.
            for archive in sorted((self.evidence_db.parent / "archives").glob("*.sqlite3"))[:128]:
                if archive.is_symlink() or not re.fullmatch(r"[0-9a-f]{64}", archive.stem):
                    continue
                try:
                    with closing(sqlite3.connect(f"file:{archive}?mode=ro", uri=True)) as connection:
                        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
                    archived_version = {
                        "generated_at": metadata.get("generated_at"),
                        "schema_version": metadata.get("schema_version"),
                        "build_version": metadata.get("build_version"),
                        "input_hashes": json.loads(metadata.get("input_hashes", "null")),
                    }
                    if archived_version != frozen_version:
                        continue
                    if hashlib.sha256(archive.read_bytes()).hexdigest() != archive.stem:
                        continue
                    candidate = self.research_for(answer, evidence_db=archive, include_documents=False)
                    if candidate.data_version == frozen_version:
                        research = candidate
                        break
                except (OSError, sqlite3.Error, ValueError, ToolError):
                    continue
        frozen_evidence_available = isinstance(answer["source_snapshot"].get("evidence"), dict)
        if (research.data_version != answer["source_snapshot"].get("data_version")
                and not (kind == "evidence" and frozen_evidence_available)):
            raise ChatError("citation_source_version_changed", "the evidence store differs from the answer's frozen source version", 409)
        # An evidence span can survive an unrelated database rebuild: below we
        # compare its complete frozen record and source, then verify raw bytes.
        # Derived legacy claims still require the original whole-store version.
        report = research._company
        claims = {item["claim_id"]: item for item in report["claims"]}
        evidence = {item["evidence_id"]: item for item in report["evidence"]}
        sources = {item["source_id"]: item for item in report["sources"]}
        frozen = answer["source_snapshot"].get("evidence")
        if frozen is not None:
            if frozen.get("company_id") != answer["company_id"]:
                raise ChatError("citation_source_mismatch", "evidence snapshot company differs from the conversation", 409)
            frozen_claims = {item["claim_id"]: item for item in frozen.get("claims", [])}
            frozen_evidence = {item["evidence_id"]: item for item in frozen.get("evidence", [])}
            frozen_sources = {item["source_id"]: item for item in frozen.get("sources", [])}
        else:  # Legacy branch above already requires the exact archived data version.
            frozen = report
            frozen_claims, frozen_evidence, frozen_sources = claims, evidence, sources
        if kind == "evidence":
            item = evidence.get(identifier)
            source = sources.get(item.get("source_id")) if item else None
            frozen_item = frozen_evidence.get(identifier)
            frozen_source = frozen_sources.get(frozen_item.get("source_id")) if frozen_item else None
            if (item is None or source is None or frozen_item is None or frozen_source is None
                    or not self._evidence_linkage_matches(
                        item, frozen_item, source, frozen_source, report, frozen,
                        company_ids, citation, original)):
                raise ChatError("citation_source_mismatch", "evidence linkage differs from the answer snapshot", 409)
            # The snapshot fields remain useful even when no archived bytes exist.
            # Never fill context or tables from today's ResearchTools source body.
            base.update({
                "title": frozen_source.get("title"), "source_url": frozen_source.get("url"),
                "published_at": frozen_source.get("published_at"),
                "collected_at": frozen_source.get("collected_at"),
                "source_sha256": frozen_source.get("sha256"), "quote": frozen_item.get("quote"),
                "locator": frozen_item.get("locator"),
                "rights_status": frozen_source.get("rights_status"),
            })
            candidates = self._frozen_document_candidates(
                answer["source_snapshot"], frozen_source.get("sha256"))
            if documents is not None and len(candidates) == 1:
                version = candidates[0]
                try:
                    source_detail = documents.read_frozen_version(
                        company_id=self.document_company_for(answer, version["document_version_id"]),
                        document_version_id=version["document_version_id"],
                    )
                except DocumentError:
                    source_detail = None
                locator = frozen_item.get("locator")
                start = locator.get("start") if isinstance(locator, dict) else None
                end = locator.get("end") if isinstance(locator, dict) else None
                if (isinstance(source_detail, dict)
                        and source_detail.get("source_sha256") == frozen_source.get("sha256")
                        and type(start) is int and type(end) is int and 0 <= start <= end
                        and source_detail["text"][start:end] == frozen_item.get("quote")):
                    text = source_detail["text"]
                    context_start, context_end = max(0, start - 500), min(len(text), end + 500)
                    base.update({
                        "document_version_id": version["document_version_id"],
                        "context": text[context_start:context_end],
                        "offset": start - context_start,
                        "parser_version": source_detail.get("parser_version"),
                    })
                    manifests = version.get("tables")
                    if (isinstance(manifests, list) and len(manifests) == len(source_detail["tables"])
                            and all(any(isinstance(manifest, dict)
                                        and manifest.get("table_index") == table.get("table_index")
                                        and manifest.get("table_sha256") == table.get("table_sha256")
                                        for manifest in manifests)
                                    for table in source_detail["tables"])):
                        base["tables"] = source_detail["tables"]
            return base

        claim = claims.get(identifier)
        frozen_claim = frozen_claims.get(identifier)
        if (claim is None or frozen_claim is None or claim != frozen_claim
                or claim.get("company_id") not in company_ids):
            raise ChatError("citation_source_mismatch", "claim differs from the answer snapshot", 409)
        chain = research._chain(identifier)
        if any(item.get("company_id") != claim["company_id"] for item in chain["facts"]):
            raise ChatError("citation_source_mismatch", "cross-company claim chains are not supported", 409)
        base.update({"title": claim.get("text"), "quote": claim.get("text"),
                     "context": claim.get("explanation"), "limitations": claim.get("limitations", []),
                     "chain": chain, "source_type": "existing_research_statement",
                     "support_status": "not_primary_source"})
        return base

    @staticmethod
    def _message_dict(row) -> dict:
        result = {key: row[key] for key in ("message_id", "role", "content", "created_at")}
        if row["run_id"] is not None:
            result["run_id"] = row["run_id"]
        if row["status"]:
            result["status"] = row["status"]
        if row["citations_json"] is not None:
            result["citations"] = json.loads(row["citations_json"])
        if row["verification_json"] is not None:
            result["verification"] = json.loads(row["verification_json"])
        return result

    def _validated_message(self, message) -> str:
        if self._closed:
            raise ChatError("service_closed", "chat service is closed", 503)
        if not isinstance(message, str):
            raise ChatError("invalid_message", "message must be a string")
        message = message.strip()
        if not message or len(message) > self.max_user_chars:
            raise ChatError("invalid_message", f"message must contain 1..{self.max_user_chars} characters", 413 if len(message) > self.max_user_chars else 400)
        return message

    def _require_provider(self) -> None:
        state = self.status()
        if not state["available"]:
            raise ChatError("provider_unavailable", str(state["blocked"]["message"]), 503)

    def _persist_submission(self, conversation_id: str | None, company_id: str | None,
                            message: str, *, transaction_guard=None,
                            transaction_precondition=None, transaction_hook=None,
                            confirmed_decision_id: str | None = None,
                            company_ids=None, native_scope=None, request_id: str | None = None,
                            request_fingerprint: str | None = None,
                            base_message_id: str | None = None) -> tuple[dict, bool]:
        """Create conversation/run/task linkage in one writer transaction."""
        run_id, message_id, created_at = _id("run"), _id("msg"), _now()
        if conversation_id is None:
            conversation_id = _id("conv")
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                if request_id is not None:
                    existing = self._submission_replay(
                        connection, request_id, request_fingerprint, conversation_id)
                    if existing is not None:
                        connection.rollback()
                        return existing, False
                if transaction_guard is not None:
                    existing = transaction_guard(connection)
                    if existing is not None:
                        connection.rollback()
                        return existing, False
                pending = connection.execute(
                    "SELECT d.decision_id FROM research_decisions d JOIN runs r USING(run_id) "
                    "LEFT JOIN research_decision_answers a USING(decision_id) "
                    "WHERE r.conversation_id=? AND a.decision_id IS NULL",
                    (conversation_id,),
                ).fetchall()
                if any(item["decision_id"] != confirmed_decision_id for item in pending):
                    raise ChatError("confirmation_required", "Resolve the current pending confirmation first.", 409)
                if transaction_precondition is not None:
                    transaction_precondition()
                if company_id is not None:
                    connection.execute(
                        "INSERT INTO conversations VALUES(?,?,?,?,?)",
                        (conversation_id, company_id, "New research conversation", created_at, created_at),
                    )
                    self.scopes.create(connection, conversation_id,
                                       company_ids if company_ids is not None else [company_id])
                conversation = connection.execute(
                    "SELECT * FROM conversations WHERE conversation_id=?", (conversation_id,)
                ).fetchone()
                if conversation is None:
                    raise ChatError("conversation_not_found", "conversation does not exist", 404)
                self._validate_report_base(connection, conversation_id, base_message_id)
                turns = connection.execute(
                    "SELECT count(*) FROM messages WHERE conversation_id=? AND role='assistant' AND status='completed'",
                    (conversation_id,),
                ).fetchone()[0]
                if turns >= self.max_turns:
                    raise ChatError("max_turns_exceeded", "conversation turn limit reached", 409)
                connection.execute(
                    "INSERT INTO runs(run_id,conversation_id,status,created_at,owner_pid) VALUES(?,?,?,?,?)",
                    (run_id, conversation_id, "queued", created_at, None),
                )
                self.inputs.begin_run(connection, run_id)
                scope = self.scopes.read_in(connection, conversation_id)
                self.records.begin_run(
                    connection, run_id, conversation_id, tuple(scope["company_ids"]))
                connection.execute(
                    "INSERT INTO messages(message_id,conversation_id,role,content,created_at,run_id,status) VALUES(?,?,?,?,?,?,?)",
                    (message_id, conversation_id, "user", message, created_at, run_id, "completed"),
                )
                if request_id is not None:
                    connection.execute(
                        "INSERT INTO chat_submission_requests VALUES(?,?,?,?,?,?)",
                        (request_id, request_fingerprint, conversation_id, run_id, message_id, created_at),
                    )
                title = message[:80] if conversation["title"] == "New research conversation" else conversation["title"]
                connection.execute("UPDATE conversations SET title=?,updated_at=? WHERE conversation_id=?", (title, created_at, conversation_id))
                self._insert_event(connection, run_id, "queued", {}, claim=None)
                stable_identity = request_id if request_id is not None else f"run:{run_id}"
                input_hash = request_fingerprint or hashlib.sha256(
                    _json({"conversation_id": conversation_id, "message": message,
                           "base_message_id": base_message_id}).encode("utf-8")
                ).hexdigest()
                self.jobs.enqueue_research(
                    connection, run_id=run_id, conversation_id=conversation_id,
                    request_identity=stable_identity, input_hash=input_hash,
                )
                if self.native_data is not None:
                    source_scope = (self.native_data.capture_scope(owner=conversation_id)
                                    if native_scope is None else native_scope)
                    self.native_data.list_sources(owner=conversation_id, scope=source_scope)
                    self._insert_event(connection, run_id, "native_source_catalog",
                                       {"authoritative": True, "scope": source_scope}, claim=None)
                result = {"run_id": run_id, "conversation_id": conversation_id, "status": "queued"}
                if transaction_hook is not None:
                    transaction_hook(connection, result)
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ChatError("active_run_exists", "conversation already has an active run", 409) from exc
        return result, True

    @staticmethod
    def _intent_snapshot(resolution: dict) -> dict:
        """Keep the resolved intent while excluding the prompt/history and arbitrary provider data."""
        snapshot = {key: resolution.get(key) for key in ("status", "code", "message", "options")}
        coverage = resolution.get("coverage")
        if isinstance(coverage, dict):
            facts = coverage.get("facts")
            documents = coverage.get("documents")
            snapshot["coverage"] = {
                "facts": ({key: facts.get(key) for key in ("available", "count", "reason") if key in facts}
                          if isinstance(facts, dict) else facts),
                "documents": ({key: documents.get(key) for key in ("available", "reason") if key in documents}
                              if isinstance(documents, dict) else documents),
            }
        scope = dict(resolution.get("scope") or {})
        metadata = scope.get("intent_metadata")
        if isinstance(metadata, dict):
            usage = metadata.get("usage")
            safe_usage = ({key: usage[key] for key in (
                "model", "input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens",
                "cache_write_tokens",
            ) if key in usage} if isinstance(usage, dict) else {})
            scope["intent_metadata"] = {
                key: metadata.get(key) for key in ("prompt_version", "provider", "model", "result", "error_code")
                if key in metadata
            }
            if safe_usage:
                scope["intent_metadata"]["usage"] = safe_usage
        snapshot["scope"] = scope
        return snapshot

    def _insert_intent_context(self, connection, run_id: str, resolution: dict,
                               original_question: str) -> None:
        """Freeze one new run's real intake record without backfilling historical runs."""
        snapshot = self._intent_snapshot(resolution)
        self._insert_event(connection, run_id, "intent_snapshot", {
            "authoritative": True, "resolution": snapshot,
        }, claim=None)
        self._insert_event(connection, run_id, "question_context", {
            "authoritative": True,
            "original_question": original_question,
            "resolution": snapshot,
        }, claim=None)

    def question_snapshot_for(self, answer: dict) -> dict | None:
        """Project only explicitly frozen intake/decision records for a new artifact.

        The dedicated ``question_context`` event is the compatibility boundary. Runs
        created before that event existed remain legacy artifacts; this method does not
        infer a scope from their prompts, answer prose, or current task state.
        """
        run_id = answer.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            return None
        visited: set[str] = set()
        clarifications: list[dict] = []
        context = None
        while run_id not in visited:
            visited.add(run_id)
            run = self.get_run(run_id)
            contexts = [event for event in run.get("events", [])
                        if event.get("type") == "question_context"
                        and event.get("authoritative") is True
                        and event.get("truncated") is not True]
            if contexts:
                context = contexts[-1]
                context_run_id = run_id
                break
            confirmations = [event for event in run.get("events", [])
                             if event.get("type") == "confirmed_decision"
                             and event.get("authoritative") is True
                             and event.get("truncated") is not True]
            if not confirmations:
                break
            confirmation = confirmations[-1]
            recorded = confirmation.get("context")
            if isinstance(recorded, dict):
                clarifications.append({
                    "decision_id": confirmation.get("decision_id"),
                    "kind": confirmation.get("kind"),
                    "question": recorded.get("decision"),
                    "explanation": recorded.get("explanation"),
                    "selected": deepcopy(recorded.get("selected") or []),
                    "user_notes": recorded.get("user_notes") or "",
                })
            source_run_id = confirmation.get("source_run_id")
            if not isinstance(source_run_id, str) or not source_run_id:
                break
            run_id = source_run_id
        if context is None:
            return None
        resolution = context.get("resolution")
        scope = resolution.get("scope") if isinstance(resolution, dict) else None
        if not isinstance(scope, dict):
            return None
        recorded_scope = {
            key: deepcopy(scope[key]) for key in (
                "subjects", "period_text", "metrics", "basis", "unit", "directions",
                "geography", "sample_definition_ref", "material_selection",
                "relationship_to_context", "human_selection",
            ) if key in scope
        }
        assumptions = [item for item in scope.get("assumptions", []) if isinstance(item, str)]
        uncertainties = []
        for key in ("uncertainties", "ambiguities"):
            for item in scope.get(key, []):
                if isinstance(item, str) and item not in uncertainties:
                    uncertainties.append(item)
        gaps = [item for item in scope.get("gaps", []) if isinstance(item, str)]
        clarifications.reverse()
        normalized = scope.get("normalized_question")
        if not isinstance(normalized, str) or not normalized.strip():
            normalized = None
        # Continuation submissions contain system-generated confirmation text,
        # not a new user question. Only the authoritative intake context is the
        # source of the question displayed in the report.
        recorded_question = context.get("original_question")
        original = recorded_question if isinstance(recorded_question, str) else ""
        latest = recorded_question
        return {
            "schema_version": "question-snapshot/1",
            "original_question": original,
            "latest_user_question": latest if isinstance(latest, str) else None,
            "normalized_question": normalized.strip() if normalized is not None else None,
            "recorded_scope": recorded_scope,
            "recorded_assumptions": assumptions,
            "open_uncertainties": uncertainties,
            "scope_gaps": gaps,
            "clarifications": clarifications,
            "time_choice": next((deepcopy(item) for item in reversed(clarifications)
                                 if item.get("kind") == "time"), None),
            "source": {"kind": "frozen_question_context", "run_id": context_run_id},
        }

    @staticmethod
    def _submission_fingerprint(conversation_id: str, message: str,
                                base_message_id: str | None) -> str:
        bound = _json({
            "conversation_id": conversation_id,
            "message": message,
            "base_message_id": base_message_id,
        })
        return hashlib.sha256(bound.encode("utf-8")).hexdigest()

    @staticmethod
    def _submission_replay(connection, request_id: str, fingerprint: str,
                           conversation_id: str) -> dict | None:
        row = connection.execute(
            "SELECT s.request_fingerprint,s.conversation_id,s.run_id,r.status "
            "FROM chat_submission_requests s JOIN runs r USING(run_id) WHERE s.request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            return None
        if row["request_fingerprint"] != fingerprint or row["conversation_id"] != conversation_id:
            raise ChatError("idempotency_conflict", "request_id is already bound to a different chat submission.", 409)
        return {"run_id": row["run_id"], "conversation_id": row["conversation_id"], "status": row["status"]}

    @staticmethod
    def _validate_report_base(connection, conversation_id: str,
                              base_message_id: str | None) -> None:
        if base_message_id is None:
            return
        base = connection.execute(
            "SELECT m.message_id,m.conversation_id,m.role,m.status,r.status AS run_status,"
            "p.message_id AS publication_id FROM messages m "
            "LEFT JOIN runs r ON r.run_id=m.run_id "
            "LEFT JOIN report_publications p ON p.message_id=m.message_id WHERE m.message_id=?",
            (base_message_id,),
        ).fetchone()
        latest = connection.execute(
            "SELECT message_id FROM messages WHERE conversation_id=? AND role='assistant' "
            "AND status='completed' ORDER BY created_at DESC,rowid DESC LIMIT 1",
            (conversation_id,),
        ).fetchone()
        if (base is None or base["conversation_id"] != conversation_id or base["role"] != "assistant"
                or base["status"] != "completed" or base["run_status"] != "completed"
                or base["publication_id"] is None or latest is None
                or latest["message_id"] != base_message_id):
            raise ChatError(
                "report_context_conflict",
                "The report baseline is outdated, unpublished, or does not belong to this session; reload the latest report.",
                409,
            )

    def submit(self, conversation_id, message, *, request_id=None, base_message_id=None) -> dict:
        """Submit or replay a follow-up, optionally bound to one published report."""
        conversation_id = self._valid_id(conversation_id, "conversation_id")
        message = self._validated_message(message)
        if request_id is not None:
            request_id = self._valid_id(request_id, "request_id")
        if base_message_id is not None:
            base_message_id = self._valid_id(base_message_id, "base_message_id")
        fingerprint = self._submission_fingerprint(conversation_id, message, base_message_id)
        if request_id is None:
            return self._submit_new(conversation_id, message, base_message_id=base_message_id)

        with closing(self._connect()) as connection:
            existing = self._submission_replay(connection, request_id, fingerprint, conversation_id)
        if existing is not None:
            return existing
        with self._submission_locks_guard:
            entry = self._submission_locks.get(request_id)
            if entry is None:
                entry = {"lock": threading.Lock(), "fingerprint": fingerprint, "users": 0}
                self._submission_locks[request_id] = entry
            elif entry["fingerprint"] != fingerprint:
                raise ChatError("idempotency_conflict", "request_id is already bound to a different chat submission.", 409)
            entry["users"] += 1
        entry["lock"].acquire()
        try:
            with closing(self._connect()) as connection:
                existing = self._submission_replay(connection, request_id, fingerprint, conversation_id)
            if existing is not None:
                return existing
            return self._submit_new(
                conversation_id, message, request_id=request_id,
                request_fingerprint=fingerprint, base_message_id=base_message_id)
        finally:
            entry["lock"].release()
            with self._submission_locks_guard:
                entry["users"] -= 1
                if entry["users"] == 0 and self._submission_locks.get(request_id) is entry:
                    del self._submission_locks[request_id]

    def _submit_new(self, conversation_id: str, message: str, *, request_id: str | None = None,
                    request_fingerprint: str | None = None,
                    base_message_id: str | None = None) -> dict:
        """Submit a follow-up only after one model-backed structured-intent intake."""
        with closing(self._connect()) as connection:
            conversation = connection.execute(
                "SELECT company_id,conversation_id FROM conversations WHERE conversation_id=?", (conversation_id,)
            ).fetchone()
        if conversation is None:
            raise ChatError("conversation_not_found", "conversation does not exist", 404)
        conversation = dict(conversation)
        with closing(self._connect()) as connection:
            self._validate_report_base(connection, conversation_id, base_message_id)
            pending = connection.execute(
                "SELECT d.decision_id FROM research_decisions d JOIN runs r USING(run_id) "
                "LEFT JOIN research_decision_answers a USING(decision_id) "
                "WHERE r.conversation_id=? AND a.decision_id IS NULL", (conversation_id,),
            ).fetchone()
        if pending:
            raise ChatError("confirmation_required", "Resolve the current pending confirmation first, or explicitly stop this research direction.", 409)
        # A time decision must not authorize a question outside this conversation's scope.
        from .research_tasks import ResearchTaskService
        from .research_scope import selected_companies
        resolution = ResearchTaskService(self).intake(message, conversation_id)
        relationship = (resolution.get("scope") or {}).get("relationship_to_context")
        requested_ids = set(selected_companies(resolution))
        authorized_ids = set(self.execution_scope_for(conversation)["company_ids"])
        if (resolution.get("status") != "needs_clarification"
                and relationship in {"different_company_new_research", "different_scope_new_research"}):
            raise ChatError(
                "scope_mismatch",
                "The question includes a subject outside the current scope. Create a new research task; the current session and scope were not changed.",
                409,
            )
        if (resolution.get("status") != "ready" or not requested_ids
                or not requested_ids.issubset(authorized_ids)):
            if resolution.get("status") == "model_unavailable":
                error = {
                    "code": "intent_model_unavailable",
                    "message": "The research-intent model is currently unavailable. The message draft was saved and research was not started.",
                }
                result, started = self._persist_submission(
                    conversation_id, None, message,
                    request_id=request_id, request_fingerprint=request_fingerprint,
                    base_message_id=base_message_id,
                    transaction_hook=lambda connection, run: self._insert_intent_context(
                        connection, run["run_id"], resolution, message,
                    ),
                )
                if started:
                    self._finish(result["run_id"], "failed", error, claim=None)
                    raise ChatError(error["code"], error["message"], 503)
                return result
            raise ChatError(
                "scope_clarification",
                resolution.get("message") or "The research subject needs clarification; research has not started.",
                409,
            )

        with closing(self._connect()) as connection:
            previous = connection.execute(
                "SELECT run_id,created_at FROM messages WHERE conversation_id=? "
                "AND role='assistant' AND status='completed' ORDER BY created_at DESC,rowid DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            has_snapshot = previous is not None and connection.execute(
                "SELECT 1 FROM events WHERE run_id=? AND type='data_snapshot'", (previous["run_id"],),
            ).fetchone() is not None
        changed_data = False
        current_native = None
        if previous and has_snapshot:
            snapshot = self.effective_snapshot(previous["run_id"], conversation["company_id"])
            current_data = self.research_for(conversation)
            changed_data = (current_data.data_version != snapshot["data_version"] or
                            list(current_data.document_version_ids) != snapshot["documents"]["document_version_ids"])
            if self.native_data is not None:
                current_native = self.native_data.capture_scope(owner=conversation_id)
                prior_native = snapshot.get("native_sources")
                changed_data = changed_data or (
                    current_native != prior_native if prior_native is not None else bool(current_native["snapshot_ids"]))
        if previous and (previous["created_at"][:10] != _now()[:10] or changed_data):
            def time_context(connection, run):
                self._insert_intent_context(connection, run["run_id"], resolution, message)
                self._insert_event(connection, run["run_id"], "time_reference", {
                    "source_run_id": previous["run_id"], "source_time": previous["created_at"],
                    "authoritative": True,
                }, claim=None)
            result, started = self._persist_submission(
                conversation_id, None, message, transaction_hook=time_context,
                request_id=request_id, request_fingerprint=request_fingerprint,
                base_message_id=base_message_id)
            if not started:
                return result
            self.decisions.request(
                result["run_id"], kind="time", question="Which point-in-time materials should this follow-up use?",
                explanation=f"The previous research version was completed on {previous['created_at'][:10]}. Materials may have changed. Confirm before continuing; the old report will not be modified."
                            + ("" if has_snapshot else "The previous version did not retain a complete data snapshot, so its point-in-time materials cannot be restored."),
                options=([
                    {"id": "retain", "label": "Reuse previous-version materials", "benefit": "Continue analysis with the same materials for easier comparison.",
                     "limitation": "Later additions or revisions will not be included."},
                ] if has_snapshot else []) + [
                    {"id": "refresh", "label": "Use currently available materials", "benefit": "Include current data from configured sources and materials uploaded in this session.",
                     "limitation": "Results may differ from the previous version; the new report will retain an independent data version."},
                ],
            )
            return {**result, "status": "blocked"}

        self._require_provider()
        result, started = self._persist_submission(
            conversation_id, None, message,
            native_scope=current_native,
            request_id=request_id, request_fingerprint=request_fingerprint,
            base_message_id=base_message_id,
            transaction_hook=lambda connection, run: self._insert_intent_context(
                connection, run["run_id"], resolution, message,
            ),
        )
        return result

    def create_and_submit(self, company_id, message, *, transaction_guard=None,
                          transaction_hook=None, company_ids=None) -> dict:
        """Atomically create a task-owned conversation, run, user turn and task link."""
        message = self._validated_message(message)
        if not isinstance(company_id, str) or not company_id.strip() or len(company_id.strip()) > 100:
            raise ChatError("invalid_company", "company_id must contain 1..100 characters")
        company_id = company_id.strip()
        try:
            from .scoped_tools import tools_for_scope
            research = tools_for_scope(
                self.evidence_db, company_id,
                company_ids if company_ids is not None else [company_id],
                self.document_service, evidence_reader=self.evidence_reader)
        except ToolError as exc:
            raise ChatError(exc.code, exc.message, 404) from exc
        # The SQLite writer lock elects one creator before provider preflight.
        # Replays discover the committed task link and never depend on current
        # provider availability.
        result, started = self._persist_submission(
            None, company_id, message, transaction_guard=transaction_guard,
            transaction_precondition=self._require_provider, transaction_hook=transaction_hook,
            company_ids=getattr(research, "company_ids", [company_id]))
        return result

    def get_run(self, run_id, after=0) -> dict:
        run_id = self._valid_id(run_id, "run_id")
        if type(after) is not int or after < 0:
            raise ChatError("invalid_cursor", "after must be a non-negative integer")
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise ChatError("run_not_found", "run does not exist", 404)
            event_rows = connection.execute(
                "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq", (run_id, after)).fetchall()
        events = [{"seq": event["seq"], "type": event["type"], **json.loads(event["payload_json"])}
                  for event in event_rows]
        result = {
            "run_id": row["run_id"], "conversation_id": row["conversation_id"], "status": row["status"],
            "events": events, "next_cursor": events[-1]["seq"] if events else after,
            **self._termination_fields(row),
        }
        if row["error_json"]:
            result["error"] = json.loads(row["error_json"])
        decision = self.decisions.for_run(run_id)
        if decision is not None:
            with closing(self._connect()) as connection:
                result["decision"] = {**decision, **self._decision_origin(connection, row["conversation_id"], run_id)}
        return result

    @staticmethod
    def _termination_fields(row) -> dict:
        """Expose persisted terminal attribution without guessing about legacy rows."""
        error = json.loads(row["error_json"]) if row["error_json"] else None
        cancel_requested = bool(row["cancel_requested"])
        reason = error.get("code") if isinstance(error, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        if reason is None and row["status"] == "cancelled":
            if cancel_requested:
                reason, message = "user_cancelled", "The user requested cancellation."
            else:
                reason = "unattributed_interruption"
                message = "The old record did not preserve the interruption source, so it is unclear whether a budget limit or user cancellation caused it."
        return {
            "termination_reason": reason,
            "termination_message": message,
            "cancel_requested": cancel_requested,
        }

    def cancel(self, run_id) -> dict:
        run_id = self._valid_id(run_id, "run_id")
        # Serialize cancellation with snapshot grants so a cancelled run can never gain scope.
        with self._lock:
            with closing(self._connect()) as connection, connection:
                row = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None:
                    raise ChatError("run_not_found", "run does not exist", 404)
                status = row["status"]
                job = self.jobs.for_run(connection, run_id)
                if job is not None and job["status"] not in {"succeeded", "failed", "cancelled", "superseded"}:
                    job_status = self.jobs.cancel_in_transaction(connection, job["job_id"])
                    if job_status == "cancelled":
                        changed = connection.execute(
                            "UPDATE runs SET cancel_requested=1,status='cancelled',finished_at=? "
                            "WHERE run_id=? AND status IN ('queued','running','blocked')", (_now(), run_id)
                        ).rowcount
                        if changed:
                            self._insert_event(connection, run_id, "cancelled", {}, claim=None)
                        status = "cancelled"
                elif status not in TERMINAL:
                    changed = connection.execute(
                        "UPDATE runs SET cancel_requested=1,status='cancelled',finished_at=? "
                        "WHERE run_id=? AND status IN ('queued','running')", (_now(), run_id)
                    ).rowcount
                    if changed:
                        self._insert_event(connection, run_id, "cancelled", {}, claim=None)
                        status = "cancelled"
                else:
                    return {"run_id": run_id, "status": status}
        return {"run_id": run_id, "status": status}

    def _insert_event(self, connection, run_id: str, event_type: str, payload: dict,
                      *, claim: JobClaim | None) -> int:
        if claim is not None and claim.run_id == run_id:
            self.jobs.assert_claim(connection, claim)
        seq = connection.execute("SELECT coalesce(max(seq),0)+1 FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
        connection.execute(
            "INSERT INTO events VALUES(?,?,?,?,?)", (run_id, seq, event_type, _json(payload), _now())
        )
        if claim is not None and claim.run_id == run_id and event_type == "confirmation_required":
            self.jobs.mark_blocked_in_transaction(connection, claim)
        return seq

    def _record_initial_snapshot(self, run_id: str, snapshot: dict, *, claim) -> None:
        """Atomically append the formal full snapshot and its display event."""
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if claim is not None:
                self.jobs.assert_claim(connection, claim)
            try:
                self.records.append_snapshot(
                    connection, run_id=run_id, snapshot=snapshot, created_at=_now())
            except ResearchRecordError as exc:
                raise ChatError(exc.code, exc.message, 409) from exc
            self._insert_event(
                connection, run_id, "data_snapshot",
                {**snapshot, "authoritative": True}, claim=claim)

    def _record_tool_completion(self, run_id: str, *, call_id: str, tool_name: str,
                                arguments: dict, result: dict, claim) -> None:
        """Atomically append actual runner I/O, document inputs, and display event."""
        payload = {
            "name": tool_name, "result": result, "observed_arguments": arguments,
            "authoritative": True, "call_id": call_id,
        }
        if len(_json(payload)) > MAX_AUTHORITATIVE_EVENT_JSON_CHARS:
            raise ChatError(
                "event_too_large", "Tool completion exceeds the authoritative event storage budget", 413)
        inputs = self._document_fact_results(payload)
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["status"] not in ACTIVE:
                raise ChatError("inactive_research", "Stopped research cannot register a tool result", 409)
            if claim is not None:
                self.jobs.assert_claim(connection, claim)
            try:
                self.records.append_receipt(
                    connection, run_id=run_id, call_id=call_id, tool_name=tool_name,
                    arguments=arguments, result=result, created_at=_now())
                for item in inputs:
                    self.inputs.record(connection, run_id, item)
            except (ResearchRecordError, InputError) as exc:
                raise ChatError(exc.code, exc.message, 409) from exc
            self._insert_event(connection, run_id, "tool_result", payload, claim=claim)

    def _event(self, run_id: str, event_type: str, payload: dict,
               *, claim: JobClaim) -> None:
        if event_type == "tool_result":
            raise ChatError(
                "formal_tool_receipt_required",
                "Tool results must be recorded atomically through the formal receipt producer", 409)
        raw = _json(payload)
        authoritative = payload.get("authoritative") is True and event_type in {
            "data_snapshot", "data_snapshot_addition", "research_policy", "tool_call",
            "answer_review_requested", "answer_review_outcome",
            "publication_repair_requested", "publication_repair_outcome",
        }
        limit = MAX_AUTHORITATIVE_EVENT_JSON_CHARS if authoritative else MAX_EVENT_JSON_CHARS
        if len(raw) > limit:
            payload = {"truncated": True, "message": "event payload exceeded storage budget",
                       "authoritative": authoritative, "name": payload.get("name"),
                       "call_id": payload.get("call_id"), "original_chars": len(raw), "limit": limit}
        with closing(self._connect()) as connection, connection:
            # SDK tool handlers and the stream consumer write concurrently.
            # Reserve the writer before reading max(seq), not only at INSERT.
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is not None and (row["status"] in ACTIVE or (
                    event_type in {"answer_review_outcome", "publication_repair_outcome"}
                    and row["status"] == "cancelled")):
                self._insert_event(connection, run_id, event_type, payload, claim=claim)

    def _history(self, connection, conversation_id: str) -> list[dict]:
        rows = connection.execute(
            "SELECT role,content,status,run_id FROM messages WHERE conversation_id=? "
            "AND (role='user' OR (role='assistant' AND status='completed')) ORDER BY created_at,rowid",
            (conversation_id,),
        ).fetchall()
        decisions = connection.execute(
            "SELECT e.payload_json FROM events e JOIN runs r USING(run_id) "
            "WHERE r.conversation_id=? AND e.type='confirmed_decision' ORDER BY r.created_at,r.rowid,e.seq",
            (conversation_id,),
        ).fetchall()
        confirmed_context = []
        for item in decisions:
            decision = json.loads(item["payload_json"])
            if decision.get("authoritative") is True:
                confirmed_context.append({**decision["context"], "decision_id": decision["decision_id"]})
        selected, used = [], 0
        context_attached = False
        for row in reversed(rows):
            content = row["content"]
            if row["role"] == "user" and not context_attached and confirmed_context:
                content += (
                    "\nRecorded user-confirmed research choices, in chronological order "
                    "(context, not source evidence):\n" + _json(confirmed_context)
                )
                context_attached = True
                if used + len(content) > self.max_history_chars:
                    raise ChatError("history_limit", "The current question and confirmation details exceed the context limit. Research did not continue by discarding the confirmation.", 413)
            if selected and used + len(content) > self.max_history_chars:
                break
            if len(content) > self.max_history_chars:
                raise ChatError("history_limit", "The current question and confirmation details exceed the context limit. Research did not continue with truncated input.", 413)
            selected.append({"role": row["role"], "content": content})
            used += len(content)
        return list(reversed(selected))

    def execute_durable_claim(self, claim: JobClaim, cancel_event: threading.Event) -> None:
        """Execute one already-claimed research job in a dedicated worker service."""
        if claim.job_type not in {"research", "verify"}:
            raise RuntimeError("no durable executor is available for this job type")
        if claim.job_type == "research" and not claim.run_id:
            raise RuntimeError("research claims require a run")
        if claim.job_type == "verify" and (claim.run_id is not None or self.verification_executor is None):
            raise RuntimeError("required verifier is not configured for this claim")
        if self._durable_claim is not None:
            raise RuntimeError("this ChatService already has an active durable claim")
        self._durable_claim = claim
        try:
            if claim.job_type == "verify":
                self.verification_executor.execute(claim, cancel_event)
            else:
                self._worker(claim.run_id, cancel_event, claim=claim)
        finally:
            self._durable_claim = None

    def _worker(self, run_id: str, cancel_event: threading.Event,
                *, claim: JobClaim) -> None:
        event = lambda event_type, payload: self._event(
            run_id, event_type, payload, claim=claim)
        deadline = time.monotonic() + self.run_timeout_seconds
        refs = {"claim": {}, "evidence": {}, "passage": {}, "calculation": {}, "query": {}}
        tool_count = 0
        tool_attempt_count = 0
        tool_count_lock = threading.Lock()
        tool_limit_reached = False
        provider_event_count = 0
        answer_parts: list[str] = []
        answer_length = 0
        timer = threading.Timer(
            self.run_timeout_seconds, self._timeout_run, args=(run_id, cancel_event, claim))
        timer.daemon = True
        timer.start()
        try:
            with closing(self._connect()) as connection, connection:
                if claim is not None:
                    self.jobs.assert_claim(connection, claim)
                changed = connection.execute(
                    "UPDATE runs SET status='running',started_at=? WHERE run_id=? AND status='queued' AND cancel_requested=0",
                    (_now(), run_id),
                ).rowcount
                if not changed:
                    return
                self._insert_event(connection, run_id, "running", {}, claim=claim)
                row = connection.execute(
                    "SELECT r.conversation_id,c.company_id FROM runs r JOIN conversations c USING(conversation_id) WHERE r.run_id=?",
                    (run_id,),
                ).fetchone()
                messages = self._history(connection, row["conversation_id"])
                company_id, conversation_id = row["company_id"], row["conversation_id"]
            with closing(self._connect()) as connection:
                confirmation = connection.execute(
                    "SELECT payload_json FROM events WHERE run_id=? AND type='confirmed_decision' ORDER BY seq DESC LIMIT 1",
                    (run_id,),
                ).fetchone()
            frozen_snapshot = None
            if confirmation:
                decision = json.loads(confirmation["payload_json"])
                with closing(self._connect()) as connection:
                    reference = connection.execute(
                        "SELECT payload_json FROM events WHERE run_id=? AND type='time_reference'",
                        (decision["source_run_id"],),
                    ).fetchone()
                if reference is not None:
                    reference = json.loads(reference["payload_json"])
                    if reference.get("authoritative") is not True or decision["kind"] != "time":
                        raise ChatError("time_reference_invalid", "The material point-in-time confirmation record is incomplete; current data was not substituted.", 409)
                    if decision["selected_ids"] == ["retain"]:
                        frozen_snapshot = self.effective_snapshot(reference["source_run_id"], company_id)
                    elif decision["selected_ids"] != ["refresh"]:
                        raise ChatError("time_reference_invalid", "The material point-in-time selection is invalid; current data was not substituted.", 409)
                else:
                    # A basis/model/period choice continues its proposal's data, not a new live snapshot.
                    frozen_snapshot = self.effective_snapshot(decision["source_run_id"], company_id)
            research = self.research_for({"company_id": company_id, "conversation_id": conversation_id},
                                         frozen_snapshot=frozen_snapshot)
            retrieval = RetrievalBudget(
                company_id=company_id,
                data_version=research.data_version,
                document_version_ids=research.document_version_ids,
            )
            from .scoped_tools import snapshot_for
            snapshot = snapshot_for(research)
            native_scope = None
            if self.native_data is not None:
                from .native_data import VERSION as NATIVE_VERSION
                if frozen_snapshot is None:
                    with closing(self._connect()) as connection:
                        catalogs = connection.execute(
                            "SELECT payload_json FROM events WHERE run_id=? AND type='native_source_catalog'",
                            (run_id,)).fetchall()
                    catalog = json.loads(catalogs[0][0]) if len(catalogs) == 1 else {}
                    if catalog.get("authoritative") is not True or catalog.get("truncated") is True:
                        raise ChatError("native_scope_unavailable", "The queued native source scope is unavailable", 409)
                    native_scope = catalog.get("scope")
                else:
                    native_scope = frozen_snapshot.get("native_sources", {
                        "version": NATIVE_VERSION, "owner": conversation_id, "snapshot_ids": []})
                self.native_data.list_sources(owner=conversation_id, scope=native_scope)
                snapshot["native_sources"] = native_scope
            self._record_initial_snapshot(run_id, snapshot, claim=claim)
            policy_reader = getattr(self.provider, 'audit_policy', None)
            if callable(policy_reader):
                try:
                    policy = policy_reader()
                    expected_fields = {'version', 'instructions', 'sha256', 'scope', 'provider_internal_prompt_available'}
                    if not isinstance(policy, dict) or set(policy) != expected_fields:
                        raise ValueError('invalid policy fields')
                    instructions = policy['instructions']
                    if (not isinstance(instructions, str) or not instructions or len(instructions) > 32_000
                            or not isinstance(policy['version'], str) or not 0 < len(policy['version']) <= 128
                            or policy['scope'] != 'application-supplied-instructions-only'
                            or policy['provider_internal_prompt_available'] is not False
                            or policy['sha256'] != hashlib.sha256(instructions.encode('utf-8')).hexdigest()):
                        raise ValueError('invalid application policy snapshot')
                    event('research_policy', {'authoritative': True, 'policy': policy})
                except Exception as exc:
                    event('research_policy', {'authoritative': True, 'unavailable': True, 'error_type': type(exc).__name__})
            event("retrieval_policy", {
                **retrieval.configuration(),
                "authoritative": True,
            })
            authorized_company_ids = frozenset(
                getattr(research, "company_ids", (research.company_id,)))
            def grant_snapshot_addition(event):
                return self._grant_snapshot_addition(
                    event, research=research, run_id=run_id, cancel_event=cancel_event,
                    retrieval=retrieval, authorized_company_ids=authorized_company_ids,
                    claim=claim)

            research.enable_document_derivation(grant_snapshot_addition)
            if frozen_snapshot is None and self.source_acquisition is not None:
                research.enable_public_acquisition(
                    self.source_acquisition, grant_snapshot_addition, cancelled=cancel_event.is_set)
            definitions = research.definitions()
            if native_scope is not None:
                from .native_tools import definitions as native_definitions
                definitions.extend(native_definitions(
                    self.native_data, owner=conversation_id, run_id=run_id,
                    scope=native_scope, cancelled=cancel_event.is_set))
            publication = None
            publication_arguments = None
            publication_calculations = {}
            def prepare_publication(title, blocks, datasets=None, value_style="recorded"):
                nonlocal publication, publication_arguments
                draft = prepare_report(
                    title=title, blocks=blocks, refs=refs, facts=research._facts,
                    calculations=publication_calculations, max_chars=self.max_output_chars,
                    datasets=datasets, value_style=value_style)
                from .answer_review import AnswerReview
                reviewer = AnswerReview(self, run_id, company_id, messages[-1]["content"], refs)
                validate_bindings(draft, reviewer)
                publication, publication_arguments = draft, deepcopy({
                    "title": title, "blocks": blocks, "datasets": datasets,
                    "value_style": value_style,
                })
                return {"status": "prepared", "schema_version": draft["schema_version"],
                        "block_count": len(draft["blocks"]),
                        "publication": "Committed only when this research run completes; not semantic verification."}
            definitions.append(self.publications.definition(prepare_publication))
            def validate_input(result, source_run_id):
                try:
                    self._validated_document_fact(
                        {"run_id": run_id, "company_id": company_id,
                         "source_snapshot": self.effective_snapshot(run_id, company_id)},
                        result, source_run_id=source_run_id)
                except ChatError as exc:
                    raise ToolError(exc.code, exc.message) from exc
            definitions.extend(research.input_definitions(self.inputs, run_id, validate_input))
            research_context = {"execution_date_utc": _now()[:10]}
            event("research_context", {"authoritative": True, **research_context})
            for definition in definitions:
                if definition["name"] == "discover_research_catalog":
                    definition["description"] += (
                        f" The runner's UTC execution date is {research_context['execution_date_utc']}. "
                        "This clock is not a financial data cutoff. Distinguish future periods from "
                        "historical scenarios and preserve any user-confirmed historical as-of date."
                    )
            decisions = self.decisions
            if claim is not None:
                claim_bound = claim
                class ClaimBoundChat:
                    def __getattr__(_bound, name):
                        return getattr(self, name)

                    def _insert_event(
                            _bound, connection, event_run_id, event_type, payload, *, claim):
                        return self._insert_event(
                            connection, event_run_id, event_type, payload, claim=claim_bound)


                decisions = copy(self.decisions)
                decisions.chat = ClaimBoundChat()
            definitions.append(decisions.definition(run_id, cancel_event, research))
            definitions.append(decisions.regression_definition(run_id, research))
            definitions.append(decisions.model_definition("forecast", run_id, research))
            definitions.extend(self.report_versions.definitions(run_id, conversation_id, refs, claim=claim))
            if (getattr(self.provider, "supports_official_acquisition", False) is True
                    and self.document_service is not None and company_id == "MSFT"):
                official = self.official_sources_factory(
                    self.document_service, snapshot_addition_callback=grant_snapshot_addition,
                    cancel_event=cancel_event, deadline=deadline, cancellation_lock=self._lock)
                for item in official.definitions():
                    original = item["handler"]

                    def official_handler(_original=original, **kwargs):
                        try:
                            return _original(**kwargs)
                        except OfficialSourceError as exc:
                            raise ToolError(exc.code, exc.message) from exc

                    definitions.append({**item, "handler": official_handler})
            tools = []
            for definition in definitions:
                original = definition["handler"]
                name = definition["name"]

                def handler(_original=original, _name=name, **kwargs):
                    nonlocal tool_count, tool_attempt_count, tool_limit_reached
                    if cancel_event.is_set():
                        try:
                            self._raise_transport_stop(run_id, deadline)
                        except ChatError as exc:
                            raise ToolError(exc.code, exc.message) from exc
                    with tool_count_lock:
                        originates_limit = (
                            not tool_limit_reached
                            and tool_attempt_count >= self.max_tool_calls
                        )
                        if originates_limit:
                            tool_limit_reached = True
                        over_limit = tool_limit_reached
                        if not over_limit:
                            tool_attempt_count += 1
                        current_attempt_count = tool_attempt_count
                    if over_limit:
                        if originates_limit:
                            self._fail_and_stop(
                                run_id, cancel_event, "tool_call_limit", "tool call limit reached",
                                claim=claim,
                            )
                        raise ToolError("tool_call_limit", "tool call limit reached")

                    execution_tool_count = None
                    execution_call_id = None
                    record_tool_result = False

                    def execute_original():
                        nonlocal tool_count, execution_tool_count, execution_call_id
                        execution_call_id = _id("tool")
                        with tool_count_lock:
                            tool_count += 1
                            execution_tool_count = tool_count
                        event("tool_call", {
                            "name": _name, "arguments": kwargs, "authoritative": True, "call_id": execution_call_id,
                        })
                        try:
                            return _original(**kwargs)
                        except ToolError as exc:
                            with tool_count_lock:
                                exc.tool_budget = {
                                    "used": tool_count, "attempts": tool_attempt_count,
                                    "remaining": max(0, self.max_tool_calls - tool_attempt_count),
                                    "limit": self.max_tool_calls,
                                    "instruction": "Failed attempts count. Replan against unresolved research questions and remaining resources; do not silently narrow the requested scope.",
                                }
                            self._record_tool_completion(
                                run_id, call_id=execution_call_id, tool_name=_name,
                                arguments=kwargs, result=exc.as_dict(), claim=claim)
                            raise

                    if _name in SEARCH_TOOLS:
                        outcome = retrieval.execute(_name, kwargs, execute_original)
                        progress = outcome.result["_retrieval_policy"]
                        event("policy_decision", {
                            "policy": "retrieval", "version": progress["version"],
                            "name": _name, "decision": outcome.action,
                            "progress": progress, "authoritative": True,
                        })
                        result = outcome.result
                        if outcome.action == "accepted":
                            self._record_refs(result, refs)
                            record_tool_result = True
                        if outcome.terminate:
                            message = "retrieval policy denials continued after search closure"
                            self._fail_and_stop(
                                run_id, cancel_event, "retrieval_budget_exhausted", message,
                                claim=claim,
                            )
                            raise ToolError("retrieval_budget_exhausted", message)
                    else:
                        result = execute_original()
                        if _name == "execute_native_sql":
                            refs["query"][result["query_id"]] = {
                                "query_id": result["query_id"], "record": deepcopy(result)}
                        if _name == "extract_document_table_fact":
                            result = self._authoritative_document_fact_result(research, result)
                        if isinstance(result, dict):
                            result = {
                                **result,
                                "_retrieval_policy": retrieval.metadata_for_tool(),
                            }
                        self._record_refs(result, refs)
                        if _name in {"calculate_facts", "calculate_yoy", "calculate_regression", "calculate_forecast"}:
                            self._record_calculation_ref(result, refs)
                            for record in result.get("calculations", [result]):
                                if isinstance(record, dict) and record.get("calculation_id") in refs["calculation"]:
                                    publication_calculations[record["calculation_id"]] = deepcopy(record)
                        record_tool_result = True
                    if isinstance(result, dict):
                        with tool_count_lock:
                            current_tool_count = (
                                tool_count if execution_tool_count is None else execution_tool_count
                            )
                        result = {
                            **result,
                            "_tool_budget": {
                                "used": current_tool_count,
                                "attempts": current_attempt_count,
                                "remaining": max(0, self.max_tool_calls - current_attempt_count),
                                "limit": self.max_tool_calls,
                            },
                        }
                    if record_tool_result:
                        if not isinstance(execution_call_id, str) or not isinstance(result, dict):
                            raise ChatError(
                                "invalid_tool_result",
                                "Executed tools must return a JSON object with a runner call identity", 502)
                        self._record_tool_completion(
                            run_id, call_id=execution_call_id, tool_name=_name,
                            arguments=kwargs, result=result, claim=claim)
                    return result

                tools.append({
                    **definition,
                    "description": definition["description"] + (
                        f" This run has a shared limit of {self.max_tool_calls} tool calls; "
                        "use remaining-budget metadata and do not repeat equivalent retrievals."
                    ),
                    "handler": handler,
                })
            for incoming in self.provider.stream(messages, tools, cancel_event):
                provider_event_count += 1
                if provider_event_count > self.max_provider_events:
                    self._fail_and_stop(
                        run_id, cancel_event, "event_limit", "provider event limit reached", claim=claim)
                    raise ChatError("event_limit", "provider event limit reached", 413)
                if time.monotonic() >= deadline:
                    self._fail_and_stop(
                        run_id, cancel_event, "timeout", "run exceeded time limit", claim=claim)
                    raise ChatError("timeout", "run exceeded time limit", 504)
                if cancel_event.is_set():
                    self._raise_transport_stop(run_id, deadline)
                event_type, payload = self._provider_event(incoming)
                if event_type == "delta":
                    text = payload.get("text")
                    if not isinstance(text, str):
                        raise ChatError("provider_protocol", "delta text must be a string", 502)
                    remaining = self.max_output_chars - answer_length
                    if len(text) > remaining:
                        if remaining:
                            part = text[:remaining]
                            answer_parts.append(part)
                            answer_length += len(part)
                            event("delta", {"text": part})
                        self._fail_and_stop(
                            run_id, cancel_event, "output_limit", "provider output exceeded limit",
                            claim=claim)
                        raise ChatError("output_limit", "provider output exceeded limit", 413)
                    answer_parts.append(text)
                    answer_length += len(text)
                    event("delta", {"text": text})
                elif event_type == "assistant_message":
                    text = payload.get("text")
                    message_id = payload.get("message_id")
                    phase = payload.get("phase")
                    if (not isinstance(text, str) or not isinstance(message_id, str)
                            or (phase is not None and not isinstance(phase, str))):
                        raise ChatError("provider_protocol", "assistant message is invalid", 502)
                    event("assistant_message", payload)
                elif event_type == "final_answer":
                    text = payload.get("text")
                    if not isinstance(text, str):
                        raise ChatError("provider_protocol", "final answer text must be a string", 502)
                    if len(text) > self.max_output_chars:
                        self._fail_and_stop(
                            run_id, cancel_event, "output_limit", "provider output exceeded limit",
                            claim=claim)
                        raise ChatError("output_limit", "provider output exceeded limit", 413)
                    answer_parts = [text]
                    answer_length = len(text)
                    event("final_answer", {"text": text})
                elif event_type in ("tool_call", "usage"):
                    # Provider telemetry is display-only and cannot impersonate runner evidence.
                    payload["authoritative"] = False
                    event(event_type, payload)
                elif event_type == "tool_result":
                    # Displaying an untrusted provider echo as a result would allow forged provenance.
                    event("provider_tool_result_ignored", {"call_id": payload.get("call_id"), "name": payload.get("name")})
                else:
                    raise ChatError("provider_protocol", "unknown provider event", 502)
            if cancel_event.is_set():
                self._raise_transport_stop(run_id, deadline)
            answer = "".join(answer_parts)
            if publication is None:
                raise ChatError(
                    "structured_candidate_required",
                    "Author jobs must produce a canonical report candidate for independent verification.",
                    409,
                )
            answer = publication["answer"]
            event("final_answer", {"text": answer, "publication_schema": publication["schema_version"]})
            if not answer.strip():
                raise ChatError("empty_response", "provider returned no answer", 502)
            # Reject recognizable transport/runtime contamination, not financial
            # semantics. Preserve the original event for diagnosis; never silently
            # trim it into a seemingly successful answer.
            if re.search(r"\[tool\]\s*You have \d+ weighted tokens left", answer, re.IGNORECASE):
                raise ChatError("invalid_final_answer", "The model returned runtime text instead of a usable research answer. The original question was preserved and can be researched again.", 502)
            from .answer_review import AnswerReview
            validate_bindings(
                publication,
                AnswerReview(self, run_id, company_id, messages[-1]["content"], refs),
            )
            quality_check = {
                "status": "clean", "scope": "explicit_record_bindings",
                "semantic_verified": False, "attempted": False,
                "outcome": "awaiting_clean_session_verification", "final_flags": [],
            }
            semantic_review = {
                "status": "not_assessed", "reason": "deferred_to_required_verifier",
            }
            event("answer_review_outcome", {"authoritative": True, **quality_check})
            if cancel_event.is_set():
                self._raise_transport_stop(run_id, deadline)
            citations, verification = self._validate_citations(answer, refs)
            verification["quality_check"] = quality_check
            verification["semantic_review"] = semantic_review
            verification["publication_schema"] = publication["schema_version"]
            semantic_concern = (
                semantic_review.get("status") == "assessed"
                and ((semantic_review.get("coverage") or {}).get("status") != "answered"
                     or any(item.get("support_status") in {"unsupported", "conflicting"}
                            for item in semantic_review.get("claims", [])))
            )
            if ((quality_check["status"] == "needs_review"
                    and quality_check.get("outcome") != "not_supported") or semantic_concern):
                verification["status"] = "needs_review"
            with closing(self._connect()) as connection:
                base = self.verification_store._latest_formal_message(connection, conversation_id)
            from .author_execution import candidate_submission
            from .candidate_contracts import AuthorInputs
            from .execution_scope import decode_scope
            from .publication_contracts import CanonicalPublication
            from .research_records import freeze_object
            self.verification_store.save_candidate_and_enqueue(claim, candidate_submission(
                question=messages[-1]["content"],
                scope=decode_scope(self.execution_scope_for({"conversation_id": conversation_id})),
                # Freeze the persisted final acquisition chain, not the initial snapshot.
                input_snapshot=freeze_object(self.effective_snapshot(run_id, company_id), "input_snapshot"),
                base_message_id=base, publication=CanonicalPublication.decode(publication),
                refs=AuthorInputs.decode(refs), request_identity=f"verify-{claim.job_id}",
            ))
            return
        except LeaseLost:
            # A cancelled or expired attempt must not mutate the current run or publication.
            pass
        except ChatError as exc:
            if exc.code == "cancelled":
                self._finish(run_id, "cancelled", None, claim=claim)
            else:
                self._finish(
                    run_id, "failed", {"code": exc.code, "message": exc.message}, claim=claim)
        except ToolError as exc:
            self._finish(
                run_id, "failed", {"code": exc.code, "message": exc.message}, claim=claim)
        except TimeoutError:
            self._finish(
                run_id, "failed", {"code": "timeout", "message": "run exceeded time limit"},
                claim=claim)
        except Exception:
            self._finish(
                run_id, "failed",
                {"code": "provider_error", "message": "provider execution failed"}, claim=claim)
        finally:
            timer.cancel()
            cancel_event.set()  # Revoke retained tool handlers after every terminal outcome.

    def _review_answer(self, run_id, company_id, question, draft, refs, cancel_event, deadline,
                       provider_event_count, *, claim: JobClaim):
        """Mechanically guard the text, then critique it without permitting a rewrite."""
        answer, quality = self._mechanical_review_answer(
            run_id, company_id, question, draft, refs, cancel_event, deadline,
            provider_event_count, claim=claim,
        )
        if cancel_event.is_set():
            self._raise_transport_stop(run_id, deadline)
        semantic = self._semantic_review_answer(
            run_id, company_id, question, answer, refs, cancel_event, deadline, claim=claim,
        )
        return answer, quality, semantic

    def _semantic_review_answer(self, run_id, company_id, question, draft, refs,
                                cancel_event, deadline, *, authoritative=True,
                                transport_budget=None, review_scopes=None, publication=None,
                                claim: JobClaim):
        event = lambda event_type, payload: self._event(
            run_id, event_type, payload, claim=claim)
        from .answer_review import AnswerReview, ReviewContextInsufficient
        from .semantic_review import (
            aggregate_scoped_reviews, build_review_request, build_scoped_review_requests,
            not_assessed, validate_review, review_wire_packet_audit,
        )

        review_method = getattr(self.provider, "review_research_answer", None)
        if (not callable(review_method) or (transport_budget is not None
                and getattr(self.provider, "supports_shared_transport_budget", False) is not True)):
            result = not_assessed("provider_unsupported")
            payload = {"authoritative": authoritative, "review": result}
            if transport_budget is not None:
                payload["transport_budget"] = transport_budget.snapshot()
            event("semantic_review", payload)
            return result
        remaining = deadline - time.monotonic()
        review_timeout = min(
            MAX_SEMANTIC_REVIEW_SECONDS,
            max(0.0, remaining - REVIEW_CLEANUP_RESERVE_SECONDS),
        )
        if review_timeout <= 0:
            result = not_assessed("timed_out")
            payload = {"authoritative": authoritative, "review": result}
            if transport_budget is not None:
                payload["transport_budget"] = transport_budget.snapshot()
            event("semantic_review", payload)
            return result
        started = time.monotonic()
        review_deadline = min(started + review_timeout, deadline - REVIEW_CLEANUP_RESERVE_SECONDS)
        scoped = False
        try:
            reviewer = AnswerReview(self, run_id, company_id, question, refs)
            try:
                requests = [build_review_request(reviewer, draft) if publication is None else
                            build_review_request(reviewer, draft, publication=publication,
                                                 review_scopes=review_scopes)]
            except ReviewContextInsufficient:
                if review_scopes is None:
                    raise
                requests = (build_scoped_review_requests(
                    reviewer, draft, review_scopes, merge_adjacent=True)
                    if publication is None else build_scoped_review_requests(
                        reviewer, draft, review_scopes, merge_adjacent=True, publication=publication))
                scoped = True
        except Exception as exc:
            reason = (f"scope_context_insufficient:{exc}" if isinstance(exc, ReviewContextInsufficient)
                      and review_scopes is not None else f"context_unavailable:{type(exc).__name__}")
            result = not_assessed(reason)
            payload = {"authoritative": authoritative, "review": result}
            if transport_budget is not None:
                payload["transport_budget"] = transport_budget.snapshot()
            event("semantic_review", payload)
            return result

        if cancel_event.is_set():
            self._raise_transport_stop(run_id, deadline)
        review_timeout = review_deadline - time.monotonic()
        if review_timeout <= 0:
            result = not_assessed("timed_out")
            payload = {"authoritative": authoritative, "review": result}
            if transport_budget is not None:
                payload["transport_budget"] = transport_budget.snapshot()
            event("semantic_review", payload)
            return result
        local_cancel = threading.Event()
        review_cancel = _RepairCancellation(cancel_event, local_cancel)
        timeout_fired = threading.Event()

        def expire_review():
            timeout_fired.set()
            local_cancel.set()

        timer = threading.Timer(review_timeout, expire_review)
        timer.daemon = True
        raw_reviews = []
        failure = None
        scope_threads = []

        def record_request(index, request, call_timeout):
            payload = {
                "authoritative": False, "review_phase_authoritative": authoritative,
                "scope_index": index + 1, "scope_count": len(requests),
                "review_input": request, "review_input_kind": "provider_input",
                "timeout_seconds": call_timeout,
            }
            if getattr(self.provider, "supports_review_wire_projection", False) is True:
                audit = review_wire_packet_audit(request)
                payload.update({
                    "review_input_kind": "canonical_before_wire_projection",
                    "review_wire_packet": audit["wire_packet"],
                    "review_wire_sha256": audit["wire_sha256"],
                    "canonical_draft_chars": audit["canonical_draft_chars"],
                    "wire_draft_chars": audit["wire_draft_chars"],
                })
            event("semantic_review_requested", payload)

        def review_diagnostics(provider):
            if getattr(provider, "supports_review_wire_projection", False) is not True:
                return None
            value = getattr(provider, "review_submission_diagnostics", None)
            if not isinstance(value, dict):
                return None
            counts = {key: value.get(key) for key in ("accepted_submissions", "rejected_submissions")}
            if not all(type(count) is int and count >= 0 for count in counts.values()):
                return None
            reasons = value.get("last_rejections", [])
            return {**counts, "last_rejections": [item[:300] for item in reasons[-5:]
                                                   if isinstance(item, str)]} if isinstance(reasons, list) else counts

        def record_diagnostics(index, diagnostics):
            if diagnostics is not None:
                event("semantic_review_scope_diagnostics", {
                    "authoritative": False, "scope_index": index + 1,
                    "scope_count": len(requests), "submission_diagnostics": diagnostics,
                })

        timer.start()
        try:
            # One claimed job owns the sole model slot; review scopes execute serially.
            parallel_factory = None
            use_parallel_workers = False
            if not use_parallel_workers:
                for request in requests:
                    if review_cancel.is_set():
                        raise InterruptedError("semantic review cancelled")
                    call_timeout = review_deadline - time.monotonic()
                    if call_timeout <= 0:
                        raise TimeoutError("semantic review deadline exhausted")
                    record_request(len(raw_reviews), request, call_timeout)
                    try:
                        if transport_budget is not None:
                            raw_review = review_method(request, review_cancel, call_timeout, transport_budget)
                        else:
                            raw_review = review_method(request, review_cancel, call_timeout)
                    except Exception:
                        try:
                            record_diagnostics(len(raw_reviews), review_diagnostics(self.provider))
                        except Exception:
                            pass  # A secondary diagnostic failure must not hide the original failure.
                        raise
                    else:
                        record_diagnostics(len(raw_reviews), review_diagnostics(self.provider))
                    raw_reviews.append(raw_review)
                    # Preserve completed partial diagnostics even if a later scope fails.
                    # These are never authoritative without complete validation/aggregation.
                    event("semantic_review_scope_result", {
                        "authoritative": False, "scope_index": len(raw_reviews),
                        "scope_count": len(requests), "raw_review": raw_review,
                    })
            else:
                completed: queue.Queue = queue.Queue()
                active: dict[int, threading.Thread] = {}
                ordered_reviews = [None] * len(requests)
                next_index = 0

                def run_scope(index, request, call_timeout):
                    worker_provider = None
                    try:
                        worker_provider = parallel_factory()
                        if worker_provider is self.provider:
                            raise RuntimeError("isolated review worker reused the active provider")
                        worker_review = getattr(worker_provider, "review_research_answer", None)
                        if (not callable(worker_review)
                                or getattr(worker_provider, "supports_shared_transport_budget", False) is not True):
                            raise RuntimeError("isolated review worker lacks shared-budget support")
                        # Worker construction also consumes the one absolute deadline.
                        remaining_timeout = review_deadline - time.monotonic()
                        if review_cancel.is_set() or remaining_timeout <= 0:
                            raise TimeoutError("semantic review deadline exhausted before worker call")
                        value = worker_review(request, review_cancel, remaining_timeout, transport_budget)
                    except Exception as exc:
                        completed.put((index, None, exc, review_diagnostics(worker_provider)))
                    else:
                        completed.put((index, value, None, review_diagnostics(worker_provider)))

                def record_completion(index, raw_review, exc, diagnostics):
                    nonlocal failure
                    thread = active.get(index)
                    if thread is not None:
                        join_deadline = min(deadline, review_deadline + REVIEW_CLEANUP_RESERVE_SECONDS)
                        thread.join(timeout=max(0.0, join_deadline - time.monotonic()))
                        if thread.is_alive():
                            if failure is None:
                                failure = TimeoutError("review worker did not finish after completion")
                            local_cancel.set()
                            return
                        active.pop(index, None)
                    if exc is not None:
                        if failure is None:
                            failure = exc
                            local_cancel.set()
                        record_diagnostics(index, diagnostics)
                        return
                    record_diagnostics(index, diagnostics)
                    ordered_reviews[index] = raw_review
                    event("semantic_review_scope_result", {
                        "authoritative": False, "scope_index": index + 1,
                        "scope_count": len(requests), "raw_review": raw_review,
                    })

                while (next_index < len(requests) or active) and failure is None:
                    if review_cancel.is_set():
                        failure = (TimeoutError("semantic review deadline exhausted")
                                   if timeout_fired.is_set()
                                   else InterruptedError("semantic review cancelled"))
                        local_cancel.set()
                        break
                    while (next_index < len(requests)
                           and len(active) < MAX_CONCURRENT_SEMANTIC_REVIEW_SCOPES):
                        call_timeout = review_deadline - time.monotonic()
                        if call_timeout <= 0:
                            failure = TimeoutError("semantic review deadline exhausted")
                            local_cancel.set()
                            break
                        index = next_index
                        request = requests[index]
                        record_request(index, request, call_timeout)
                        thread = threading.Thread(
                            target=run_scope, args=(index, request, call_timeout),
                            name=f"semantic-review-scope-{index + 1}", daemon=True,
                        )
                        active[index] = thread
                        next_index += 1
                        thread.start()
                        scope_threads.append(thread)
                    if failure is not None:
                        break
                    try:
                        item = completed.get(timeout=min(
                            0.05, max(0.001, review_deadline - time.monotonic())))
                    except queue.Empty:
                        continue
                    record_completion(*item)

                if failure is not None or review_cancel.is_set():
                    local_cancel.set()
                cleanup_deadline = min(
                    deadline, time.monotonic() + REVIEW_CLEANUP_RESERVE_SECONDS)
                while active and time.monotonic() < cleanup_deadline:
                    try:
                        item = completed.get(timeout=min(
                            0.05, max(0.001, cleanup_deadline - time.monotonic())))
                    except queue.Empty:
                        continue
                    record_completion(*item)
                for thread in active.values():
                    thread.join(timeout=0)
                if active and failure is None:
                    failure = TimeoutError("semantic review workers did not stop during cleanup")
                if failure is None:
                    raw_reviews = ordered_reviews
        except Exception as exc:
            if failure is None:
                failure = exc
        finally:
            # Logging/thread-start/queue failures must cancel and drain siblings too,
            # not merely errors reported by a provider worker.
            if failure is not None or review_cancel.is_set():
                local_cancel.set()
                cleanup_deadline = min(deadline, review_deadline + REVIEW_CLEANUP_RESERVE_SECONDS,
                                       time.monotonic() + REVIEW_CLEANUP_RESERVE_SECONDS)
                for thread in scope_threads:
                    thread.join(timeout=max(0.0, cleanup_deadline - time.monotonic()))
            timer.cancel()
            timer.join()
        if cancel_event.is_set():
            self._raise_transport_stop(run_id, deadline)
        if timeout_fired.is_set() or isinstance(failure, TimeoutError):
            result = not_assessed("timed_out")
        elif failure is not None:
            from .publication_repair import TransportBudgetExceeded
            reason = ("transport_budget_exhausted" if isinstance(failure, TransportBudgetExceeded)
                      else f"review_failed:{type(failure).__name__}")
            result = not_assessed(reason)
        elif local_cancel.is_set():
            result = not_assessed("review_incomplete")
        else:
            try:
                if scoped:
                    result = aggregate_scoped_reviews(raw_reviews, requests)
                else:
                    result = validate_review(raw_reviews[0], requests[0])
            except Exception as exc:
                result = not_assessed(f"invalid_review:{type(exc).__name__}")
        elapsed = round(time.monotonic() - started, 3)
        review_event = {"authoritative": authoritative, "review": result, "elapsed_seconds": elapsed}
        if transport_budget is not None:
            review_event["transport_budget"] = transport_budget.snapshot()
        event("semantic_review", review_event)
        return result

    def _repair_structured_publication(
            self, run_id, company_id, question, publication, arguments, original_review,
            refs, facts, calculations, cancel_event, deadline, transport_budget, *, claim: JobClaim):
        """Attempt one bounded prose-only repair and retain the original unless fully accepted."""
        event = lambda event_type, payload: self._event(
            run_id, event_type, payload, claim=claim)
        from .answer_review import AnswerReview
        from .publication_repair import (
            MAX_RESPONSE_CHARS, TransportBudgetExceeded, apply_response, build_request,
            has_conflicting_claims, review_is_acceptable,
        )

        base = {
            "authoritative": True, "attempted": False, "outcome": "not_needed",
            "transport_budget": transport_budget.snapshot(),
        }
        original_evidence = {
            "schema_version": publication.get("schema_version"),
            "rendered_draft": publication.get("answer"),
        }
        if not has_conflicting_claims(original_review):
            if original_review.get("status") != "assessed":
                base["outcome"] = ("provider_unsupported"
                                   if original_review.get("reason") == "provider_unsupported"
                                   else "review_not_assessed")
            event("publication_repair_outcome", {
                **base, "original_evidence": original_evidence, "original_review": original_review,
            })
            return publication, arguments, original_review
        revise = getattr(self.provider, "revise_publication", None)
        if (not callable(revise)
                or getattr(self.provider, "supports_shared_transport_budget", False) is not True):
            base["outcome"] = "provider_unsupported"
            event("publication_repair_outcome", {
                **base, "original_evidence": original_evidence, "original_review": original_review,
            })
            return publication, arguments, original_review

        remaining = deadline - time.monotonic()
        repair_timeout = min(MAX_REPAIR_SECONDS, max(0.0, remaining - SEMANTIC_REVIEW_RESERVE_SECONDS))
        budget_state = transport_budget.snapshot()
        if repair_timeout <= 0 or budget_state["remaining_events"] <= 0 or budget_state["remaining_payload_chars"] <= 0:
            base["outcome"] = "timed_out" if repair_timeout <= 0 else "budget_exhausted"
            event("publication_repair_outcome", {
                **base, "original_evidence": original_evidence, "original_review": original_review,
            })
            return publication, arguments, original_review
        budgets = {
            **transport_budget.snapshot(),
            "remaining_provider_events": transport_budget.snapshot()["remaining_events"],
            "max_output_chars": min(self.max_output_chars, MAX_RESPONSE_CHARS),
            "shared_deadline_remaining_seconds": repair_timeout,
            "tools_allowed": 0,
        }
        try:
            request = build_request(arguments, publication["answer"], original_review, budgets)
        except Exception as exc:
            base.update(outcome="context_unavailable", message=f"Repair context unavailable: {type(exc).__name__}")
            event("publication_repair_outcome", {
                **base, "original_evidence": original_evidence, "original_review": original_review,
            })
            return publication, arguments, original_review
        event("publication_repair_requested", {
            "authoritative": True, "timeout_seconds": repair_timeout,
            "repair_input": request, "original_evidence": original_evidence,
            "original_review": original_review,
        })

        local_cancel = threading.Event()
        repair_cancel = _RepairCancellation(cancel_event, local_cancel)
        timeout_fired = threading.Event()
        def expire_repair():
            timeout_fired.set()
            local_cancel.set()
        timer = threading.Timer(repair_timeout, expire_repair)
        timer.daemon = True
        started = time.monotonic()
        response = None
        failure = None
        timer.start()
        try:
            response = revise(request, repair_cancel, repair_timeout, transport_budget)
        except Exception as exc:
            failure = exc
        finally:
            timer.cancel()
            timer.join()
        attempted = {
            **base, "attempted": True, "repair_output": response,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "original_evidence": original_evidence, "original_review": original_review,
            "transport_budget": transport_budget.snapshot(),
        }
        if cancel_event.is_set():
            attempted["outcome"] = "cancelled"
            event("publication_repair_outcome", attempted)
            self._raise_transport_stop(run_id, deadline)
        if timeout_fired.is_set() or isinstance(failure, TimeoutError):
            attempted["outcome"] = "timed_out"
        elif failure is not None:
            if isinstance(failure, TransportBudgetExceeded):
                attempted.update(outcome="budget_exhausted",
                                 message="Publication revision exhausted the shared transport budget.")
            else:
                attempted.update(outcome="failed", message=f"Publication revision failed: {type(failure).__name__}")
        elif local_cancel.is_set():
            attempted.update(outcome="rejected", message="Provider stopped without a completed edit packet.")
        else:
            try:
                candidate_arguments, edits = apply_response(arguments, response)
                candidate = prepare_report(
                    refs=refs, facts=facts, calculations=calculations,
                    max_chars=self.max_output_chars, **candidate_arguments)
                validate_bindings(candidate, AnswerReview(self, run_id, company_id, question, refs))
            except Exception as exc:
                attempted.update(outcome="rejected", message=f"Candidate revalidation failed: {type(exc).__name__}")
            else:
                attempted.update(candidate_arguments=candidate_arguments,
                                 candidate_evidence={
                                     "schema_version": candidate.get("schema_version"),
                                     "rendered_draft": candidate.get("answer"),
                                 }, applied_edits=edits)
                try:
                    from .semantic_review import publication_review_scopes
                    candidate_review = self._semantic_review_answer(
                        run_id, company_id, question, candidate["answer"], refs,
                        cancel_event, deadline, authoritative=False,
                        transport_budget=transport_budget,
                        review_scopes=publication_review_scopes(candidate), publication=candidate,
                        claim=claim,
                    )
                except ChatError as exc:
                    attempted["outcome"] = ("cancelled" if exc.code == "cancelled" else "timed_out")
                    attempted["transport_budget"] = transport_budget.snapshot()
                    event("publication_repair_outcome", attempted)
                    raise
                attempted["candidate_review"] = candidate_review
                if review_is_acceptable(original_review, candidate_review):
                    attempted["outcome"] = "accepted"
                    attempted["transport_budget"] = transport_budget.snapshot()
                    event("publication_repair_outcome", attempted)
                    event("semantic_review", {
                        "authoritative": True, "review": candidate_review,
                        "publication_repair": "accepted",
                        "transport_budget": transport_budget.snapshot(),
                    })
                    event("final_answer", {
                        "text": candidate["answer"], "publication_schema": candidate["schema_version"],
                    })
                    return candidate, candidate_arguments, candidate_review
                if candidate_review.get("reason") == "transport_budget_exhausted":
                    attempted.update(outcome="budget_exhausted",
                                     message="Candidate review exhausted the shared transport budget.")
                elif candidate_review.get("reason") == "timed_out":
                    attempted.update(outcome="timed_out",
                                     message="Candidate semantic review timed out.")
                else:
                    attempted.update(outcome="rejected",
                                     message="Candidate semantic review was unassessed, still conflicted, or reduced coverage.")
        attempted["transport_budget"] = transport_budget.snapshot()
        event("publication_repair_outcome", attempted)
        return publication, arguments, original_review

    def _mechanical_review_answer(self, run_id, company_id, question, draft, refs, cancel_event, deadline,
                                  provider_event_count, *, claim: JobClaim):
        """Run the existing mechanical check and, if supported, one bounded revision."""
        event = lambda event_type, payload: self._event(
            run_id, event_type, payload, claim=claim)
        from .answer_review import AnswerReview

        try:
            reviewer = AnswerReview(self, run_id, company_id, question, refs)
            original = reviewer.check(draft)
        except Exception as exc:
            outcome = {
                "status": "needs_review", "attempted": False, "outcome": "check_failed",
                "original_flags": [], "final_flags": [],
                "message": f"Mechanical review could not run: {type(exc).__name__}",
            }
            event("answer_review_outcome", {
                "authoritative": True, "original_draft": draft, **outcome,
            })
            return draft, outcome

        base = {
            "status": "clean" if not original["flags"] else "needs_review",
            "scope": "numeric_reference_only", "semantic_verified": False,
            "algorithm_version": reviewer.algorithm_version,
            "attempted": False,
            "outcome": "clean" if not original["flags"] else "not_supported",
            "original_flags": original["flags"], "final_flags": original["flags"],
        }
        if not original["flags"]:
            event("answer_review_outcome", {
                "authoritative": True, "original_draft": draft, **base,
            })
            return draft, base
        revise = getattr(self.provider, "revise_answer", None)
        if not callable(revise):
            event("answer_review_outcome", {
                "authoritative": True, "original_draft": draft, **base,
            })
            return draft, base

        remaining = deadline - time.monotonic()
        cleanup_reserve = (SEMANTIC_REVIEW_RESERVE_SECONDS
                           if callable(getattr(self.provider, "review_research_answer", None))
                           else REPAIR_CLEANUP_RESERVE_SECONDS)
        repair_timeout = min(MAX_REPAIR_SECONDS, max(0.0, remaining - cleanup_reserve))
        if repair_timeout <= 0 or provider_event_count >= self.max_provider_events:
            base["outcome"] = "timed_out" if repair_timeout <= 0 else "budget_exhausted"
            event("answer_review_outcome", {
                "authoritative": True, "original_draft": draft, **base,
            })
            return draft, base
        try:
            request = reviewer.repair_request(draft, original)
        except Exception as exc:
            base.update(outcome='context_unavailable', message=f'Repair context unavailable: {type(exc).__name__}')
            event('answer_review_outcome', {'authoritative': True, 'original_draft': draft, **base})
            return draft, base
        request["budgets"] = {
            "remaining_provider_events": max(0, self.max_provider_events - provider_event_count),
            "max_output_chars": self.max_output_chars,
            "shared_deadline_remaining_seconds": repair_timeout,
            "tools_allowed": 0,
        }
        event("answer_review_requested", {
            "authoritative": True, "timeout_seconds": repair_timeout,
            "review_input": request, "original_draft": draft,
        })
        local_cancel = threading.Event()
        repair_cancel = _RepairCancellation(cancel_event, local_cancel)
        timeout_fired = threading.Event()
        def expire_repair():
            timeout_fired.set()
            local_cancel.set()
        timeout_timer = threading.Timer(repair_timeout, expire_repair)
        timeout_timer.daemon = True
        repair_started = time.monotonic()
        timeout_timer.start()
        revision = None
        failure = None
        try:
            revision = revise(request, repair_cancel, repair_timeout)
        except Exception as exc:
            failure = type(exc).__name__
        finally:
            timeout_timer.cancel()
            timeout_timer.join()

        if cancel_event.is_set():
            event("answer_review_outcome", {
                "authoritative": True, "original_draft": draft,
                "revision_output": revision, **base, "attempted": True,
                "outcome": "cancelled", "elapsed_seconds": round(time.monotonic() - repair_started, 3),
            })
            self._raise_transport_stop(run_id, deadline)
        attempted = {**base, "attempted": True, "elapsed_seconds": round(time.monotonic() - repair_started, 3)}
        if timeout_fired.is_set() or failure == 'TimeoutError':
            attempted["outcome"] = "timed_out"
        elif failure is not None:
            attempted["outcome"] = "failed"
            attempted["message"] = f"Revision failed: {failure}"
        elif local_cancel.is_set():
            attempted['outcome'] = 'rejected'
            attempted['message'] = 'Provider stopped without a completed revision; original retained.'
        elif not isinstance(revision, str) or not revision.strip() or len(revision) > self.max_output_chars:
            attempted["outcome"] = "rejected"
            attempted["message"] = "Revision was empty, invalid, or exceeded the output budget."
        else:
            try:
                candidate = reviewer.check(revision)
            except Exception as exc:
                attempted["outcome"] = "rejected"
                attempted["message"] = f"Revision revalidation failed: {type(exc).__name__}"
            else:
                attempted["candidate_flags"] = candidate["flags"]
                if reviewer.is_better(original, candidate):
                    attempted.update({
                        "outcome": "accepted",
                        "status": "clean" if not candidate["flags"] else "needs_review",
                        "final_flags": candidate["flags"],
                    })
                else:
                    attempted["outcome"] = "rejected"
        event("answer_review_outcome", {
            "authoritative": True, "original_draft": draft, "revision_output": revision,
            **attempted,
        })
        if attempted["outcome"] == "accepted":
            event("final_answer", {"text": revision})
            return revision, attempted
        return draft, attempted

    @staticmethod
    def _provider_event(incoming) -> tuple[str, dict]:
        if not isinstance(incoming, dict):
            raise ChatError("provider_protocol", "provider event must be an object", 502)
        if isinstance(incoming.get("type"), str):
            return incoming["type"], {key: value for key, value in incoming.items() if key != "type"}
        if len(incoming) == 1:
            event_type, payload = next(iter(incoming.items()))
            if isinstance(event_type, str) and isinstance(payload, dict):
                return event_type, payload
        raise ChatError("provider_protocol", "provider event has invalid shape", 502)

    @staticmethod
    def _authoritative_document_fact_result(research: ResearchTools, result: Any) -> Any:
        if not isinstance(result, dict) or not isinstance(result.get("fact"), dict):
            return result
        locator = result["fact"].get("locator")
        if not isinstance(locator, dict):
            return result
        descriptor = next((item for item in research.document_coverage.get("documents", [])
                           if isinstance(item, dict)
                           and item.get("document_version_id") == locator.get("document_version_id")), None)
        return with_provenance(result, descriptor) if descriptor is not None else result

    @staticmethod
    def _record_refs(value, refs: dict) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("claim_id"), str):
                refs["claim"][value["claim_id"]] = {
                    "claim_id": value["claim_id"], "source_id": value.get("source_id")
                }
            claim = value.get("claim")
            if isinstance(claim, dict) and isinstance(claim.get("claim_id"), str):
                refs["claim"][claim["claim_id"]] = {"claim_id": claim["claim_id"]}
            if isinstance(value.get("evidence_id"), str):
                refs["evidence"][value["evidence_id"]] = {
                    "evidence_id": value["evidence_id"], "source_id": value.get("source_id"),
                    "claim_id": value.get("claim_id"),
                    "document_version_id": value.get("locator", {}).get("document_version_id")
                    if isinstance(value.get("locator"), dict) else None,
                    "company_id": value.get("company_id"),
                }
            if (isinstance(value.get("passage_id"), str)
                    and isinstance(value.get("document_version_id"), str)
                    and isinstance(value.get("company_id"), str)):
                refs["passage"][value["passage_id"]] = {
                    "passage_id": value["passage_id"],
                    "document_version_id": value["document_version_id"],
                    "company_id": value["company_id"],
                }
            for child in value.values():
                ChatService._record_refs(child, refs)
        elif isinstance(value, list):
            for child in value:
                ChatService._record_refs(child, refs)

    @staticmethod
    def _record_calculation_ref(result: Any, refs: dict) -> None:
        if not isinstance(result, dict):
            return
        if isinstance(result.get("calculations"), list):
            for calculation in result["calculations"]:
                ChatService._record_calculation_ref(calculation, refs)
            return
        identifier = result.get("calculation_id")
        inputs = result.get("inputs")
        if (not isinstance(identifier, str) or not re.fullmatch(r"calc-[0-9a-f]{64}", identifier)
                or not isinstance(inputs, list) or not inputs
                or result.get("interpretation") != "unverified"):
            return
        refs["calculation"][identifier] = {
            "calculation_id": identifier,
            "company_id": result.get("company_id"),
            "schema_version": result.get("schema_version"),
        }

    @staticmethod
    def _validate_citations(answer: str, refs: dict) -> tuple[list[dict], dict]:
        citations, warnings, seen = [], [], set()
        for match in _CITATION.finditer(answer):
            kind, identifier = match.groups()
            if (kind, identifier) in seen:
                continue
            seen.add((kind, identifier))
            record = refs.get(kind, {}).get(identifier)
            citation = {"kind": kind, "id": identifier, "valid": record is not None}
            if record:
                for key in ("source_id", "claim_id", "document_version_id", "company_id", "schema_version"):
                    if record.get(key):
                        citation[key] = record[key]
            else:
                warnings.append(f"{kind} reference was not returned by a tool in this run: {identifier}")
            citations.append(citation)
        if not citations and answer.strip():
            warnings.append("No tool-backed citation was supplied; factual content is unverified.")
        status = "references_checked" if citations and all(item["valid"] for item in citations) else "unverified"
        warnings.insert(0, "References were mechanically checked only; the answer remains unreviewed and is not semantically approved.")
        return citations, {"status": status, "warnings": warnings}

    def _timeout_run(self, run_id: str, cancel_event: threading.Event,
                     claim: JobClaim) -> None:
        if claim is not None:
            # Do not release the global lease from a timer while a provider call may
            # still be alive. The worker keeps heartbeating until the call unwinds;
            # process death is then handled by lease expiry and PID confirmation.
            cancel_event.set()
            return
        self._finish(
            run_id, "failed", {"code": "timeout", "message": "run exceeded time limit"},
            claim=claim)
        cancel_event.set()

    def _fail_and_stop(self, run_id: str, cancel_event: threading.Event, code: str,
                       message: str, *, claim: JobClaim) -> None:
        """Persist the originating failure before using the event to stop transport."""
        self._finish(run_id, "failed", {"code": code, "message": message}, claim=claim)
        cancel_event.set()

    def _raise_transport_stop(self, run_id: str, deadline: float) -> None:
        """Classify an unattributed transport stop without overwriting an existing terminal result."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT status,cancel_requested,error_json FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is not None and row["status"] in TERMINAL:
            error = json.loads(row["error_json"]) if row["error_json"] else None
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                raise ChatError(error["code"], error.get("message") or "run failed", 409)
            if bool(row["cancel_requested"]):
                raise ChatError("cancelled", "run cancellation requested", 409)
            raise ChatError("provider_interrupted", "transport stopped after an unattributed terminal state", 502)
        if time.monotonic() >= deadline:
            raise ChatError("timeout", "run exceeded time limit", 504)
        raise ChatError("provider_interrupted", "provider transport stopped before completion", 502)

    def _finish(self, run_id: str, status: str, error: dict | None,
                *, claim: JobClaim | None) -> None:
        with closing(self._connect()) as connection, connection:
            if claim is not None and claim.run_id == run_id:
                try:
                    self.jobs.assert_claim(connection, claim)
                except LeaseLost:
                    return
                if status == "cancelled":
                    self._insert_event(connection, run_id, "cancelled", {}, claim=claim)
                    self.jobs.cancel_in_transaction(connection, claim.job_id)
                    connection.execute(
                        "UPDATE runs SET status='cancelled',cancel_requested=1,error_json=NULL,finished_at=? "
                        "WHERE run_id=? AND status IN ('queued','running')", (_now(), run_id))
                    return
                failure = error or {"code": "research_failed", "message": "research execution failed"}
                retryable = failure.get("code") in {"provider_error", "provider_interrupted", "timeout"}
                self._insert_event(
                    connection, run_id, "failed", {"error": failure}, claim=claim)
                job_status = self.jobs.fail_in_transaction(
                    connection, claim, failure, retryable=retryable)
                if job_status == "retry_wait":
                    connection.execute(
                        "UPDATE runs SET status='queued',error_json=NULL,started_at=NULL,finished_at=NULL "
                        "WHERE run_id=? AND status IN ('queued','running')", (run_id,))
                else:
                    connection.execute(
                        "UPDATE runs SET status='failed',error_json=?,finished_at=? "
                        "WHERE run_id=? AND status IN ('queued','running')",
                        (_json(failure), _now(), run_id))
                return
            job = self.jobs.for_run(connection, run_id)
            if job is not None and job["status"] not in {
                    "succeeded", "failed", "cancelled", "superseded"}:
                self.jobs.cancel_in_transaction(connection, job["job_id"])
            changed = connection.execute(
                "UPDATE runs SET status=?,error_json=?,finished_at=? WHERE run_id=? AND status IN ('queued','running')",
                (status, _json(error) if error else None, _now(), run_id),
            ).rowcount
            if changed:
                self._insert_event(
                    connection, run_id, status, {"error": error} if error else {}, claim=None)

    def close(self) -> None:
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
