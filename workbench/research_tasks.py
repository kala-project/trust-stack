"""Persistent, object-neutral research tasks and immutable scope versions."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
import threading
import uuid

from .chat import ChatError
from .report_reader import load_reader_payload, report_id_for
from .research_intent import INTENT_PROMPT_VERSION, IntentSchemaError, build_intent_prompt
from .research_scope import _coverage_available, company_subject, resolve_model_intent, selected_companies
from .research_tools import ResearchTools, ToolError
from .service import EvidenceService


_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS research_tasks (
 task_id TEXT PRIMARY KEY,
 question TEXT NOT NULL,
 context_conversation_id TEXT REFERENCES conversations(conversation_id),
 request_id TEXT NOT NULL UNIQUE,
 request_fingerprint TEXT NOT NULL,
 current_scope_version INTEGER NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_scope_versions (
 task_id TEXT NOT NULL REFERENCES research_tasks(task_id),
 scope_version INTEGER NOT NULL,
 resolution_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(task_id,scope_version)
);
CREATE TABLE IF NOT EXISTS research_task_runs (
 task_id TEXT NOT NULL,
 scope_version INTEGER NOT NULL,
 conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
 run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
 created_at TEXT NOT NULL,
 PRIMARY KEY(task_id,scope_version),
 FOREIGN KEY(task_id,scope_version) REFERENCES research_scope_versions(task_id,scope_version)
);
CREATE TRIGGER IF NOT EXISTS research_scope_versions_no_update
BEFORE UPDATE ON research_scope_versions BEGIN SELECT RAISE(ABORT,'scope versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_scope_versions_no_delete
BEFORE DELETE ON research_scope_versions BEGIN SELECT RAISE(ABORT,'scope versions are immutable'); END;
"""

_ORIGINS = {"user", "machine_acceptance"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _valid_text(value, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ChatError(f"invalid_{name}", f"{name} must be a string")
    value = value.strip()
    if not value or len(value) > maximum:
        raise ChatError(f"invalid_{name}", f"{name} must contain 1..{maximum} characters")
    return value


def _reader_report_fields(conversation_id, message_id, answer_status=None, reader_payload=None) -> dict:
    if answer_status is not None and answer_status != "answered":
        return {}
    if not conversation_id or not message_id:
        return {}
    report_id = report_id_for(str(conversation_id), str(message_id))
    fields = {
        "reader_report_id": report_id,
        "reader_href": f"/report-reader.html?report_id={report_id}",
    }
    if reader_payload:
        title = reader_payload.get("report", {}).get("title") if isinstance(reader_payload, dict) else None
        if isinstance(title, str) and title.strip():
            fields["reader_title"] = title.strip()
    return fields


def _reader_report(row) -> dict | None:
    if row is None:
        return None
    fields = _reader_report_fields(row["conversation_id"], row["message_id"], "answered")
    if not fields:
        return None
    return {
        **fields,
        "conversation_id": row["conversation_id"],
        "message_id": row["message_id"],
        "run_id": row["run_id"],
        "created_at": row["created_at"],
    }


def _run_summary(chat, row):
    """Small persisted run/decision projection; no events or model-plan payloads."""
    if row is None or row["run_id"] is None:
        return None
    summary = {
        "run_id": row["run_id"], "conversation_id": row["run_conversation_id"],
        "status": row["run_status"], "created_at": row["run_created_at"],
        **chat._termination_fields({"status": row["run_status"],
                                    "error_json": row["error_json"],
                                    "cancel_requested": row["cancel_requested"]}),
    }
    if row["decision_id"] is not None:
        summary["decision"] = {
            "decision_id": row["decision_id"], "kind": row["decision_kind"],
            "question": row["decision_question"], "explanation": row["decision_explanation"],
            "status": row["decision_status"] or "pending",
        }
    return summary


class ResearchTaskService:
    """Task store sharing the chat database so run linkage can be atomic."""

    def __init__(self, chat_service, *, intent_resolver=None, deterministic_scope_resolver=None,
                 intent_timeout_seconds=60.0):
        self.chat = chat_service
        # deterministic_scope_resolver is an explicit legacy-test seam. Production
        # never installs it and has no semantic word-rule fallback.
        self._deterministic_scope_resolver = deterministic_scope_resolver
        self._intent_resolver = intent_resolver or getattr(chat_service.provider, "resolve_research_intent", None)
        self._intent_timeout_seconds = float(intent_timeout_seconds)
        self._request_locks_guard = threading.Lock()
        self._request_locks = {}
        # Retry-intent singleflight is deliberately process-local. It avoids a
        # duplicate paid intent call within one API process without holding a
        # SQLite transaction across that call. Multiple API processes require
        # an external/global fence for the same guarantee.
        self._retry_locks_guard = threading.Lock()
        self._retry_locks = {}
        with closing(self.chat._connect()) as connection:
            connection.executescript(_SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(research_tasks)")}
            if "origin" not in columns:
                connection.execute("BEGIN IMMEDIATE")
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(research_tasks)")}
                if "origin" not in columns:
                    connection.execute(
                        "ALTER TABLE research_tasks ADD COLUMN origin TEXT NOT NULL DEFAULT 'user' "
                        "CHECK(origin IN ('user','machine_acceptance'))"
                    )
                    connection.execute(
                        "UPDATE research_tasks SET origin='machine_acceptance' "
                        "WHERE substr(request_id,1,16)='research-matrix-'"
                    )
                connection.commit()

    def _context_company(self, context_conversation_id) -> str | None:
        if context_conversation_id is None:
            return None
        context_conversation_id = _valid_text(context_conversation_id, "context_conversation_id", 200)
        with closing(self.chat._connect()) as connection:
            row = connection.execute(
                "SELECT company_id FROM conversations WHERE conversation_id=?", (context_conversation_id,)
            ).fetchone()
        if row is None:
            raise ChatError("context_not_found", "The specified research context does not exist.", 404)
        return row["company_id"]

    def _coverage(self, company_id: str) -> dict:
        try:
            tools = ResearchTools(self.chat.evidence_db, company_id, self.chat.document_service)
        except ToolError:
            return {
                "facts": {"available": False, "count": 0},
                "documents": {"available": False, "reason": "company data unavailable"},
            }
        return {
            "facts": {"available": bool(tools._facts), "count": len(tools._facts)},
            "documents": tools.document_coverage,
        }

    def _intent_context(self, context_conversation_id):
        context_company = self._context_company(context_conversation_id)
        if context_conversation_id is None:
            return context_company, []
        with closing(self.chat._connect()) as connection:
            rows = connection.execute(
                "SELECT role,content FROM messages WHERE conversation_id=? "
                "AND status='completed' ORDER BY created_at DESC,rowid DESC LIMIT 12",
                (context_conversation_id,),
            ).fetchall()
        history = [{"role": row["role"], "content": row["content"][:4000]}
                   for row in reversed(rows) if row["role"] in ("user", "assistant")]
        return context_company, history

    @staticmethod
    def _intent_failure(question, prompt, context_conversation_id, code, message):
        return {
            "status": "model_unavailable" if code == "intent_model_unavailable" else "needs_review",
            "code": code, "message": message,
            "scope": {
                "subjects": [{"kind": "unknown", "id": None, "label": "Awaiting model parsing"}],
                "period_text": None, "metrics": [], "basis": [], "unit": None,
                "directions": [question], "assumptions": [],
                "uncertainties": [message], "original_intent": question,
                "geography": None, "sample_definition_ref": None, "material_selection": None,
                "ambiguities": [], "gaps": [message],
                "resolution_method": "llm_structured_intent_v1",
                "context_conversation_id": context_conversation_id,
                "relationship_to_context": "unresolved", "human_selection": None,
                "intent_metadata": {"prompt": prompt, "prompt_version": INTENT_PROMPT_VERSION,
                                    "provider": "unavailable", "model": None, "result": None,
                                    "error_code": code},
            },
            "options": [], "coverage": {"facts": None, "documents": None},
        }

    def intake(self, question, context_conversation_id=None) -> dict:
        question = _valid_text(question, "question", self.chat.max_user_chars)
        context_company, history = self._intent_context(context_conversation_id)
        if self._deterministic_scope_resolver is not None:
            return self._deterministic_scope_resolver(
                question, context_company_id=context_company,
                context_conversation_id=context_conversation_id,
                coverage_lookup=self._coverage,
            )
        context_ids = (self.chat.execution_scope_for({"conversation_id": context_conversation_id})["company_ids"]
                       if context_conversation_id else [])
        if len(context_ids) > 1:
            context_company = None
        try:
            entities = EvidenceService(self.chat.evidence_db).entity_catalog()
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            raise ChatError("entity_catalog_unavailable", "The entity catalog could not be read; the built-in company list was not used as a substitute.", 503) from exc
        catalog = {item["candidate_id"]: {"label": item["name"], "aliases": item["aliases"]} for item in entities}
        prompt = build_intent_prompt(question, current_company_id=context_company, history=history,
                                     entity_catalog=entities, current_company_ids=context_ids)
        if self._intent_resolver is None:
            return self._intent_failure(question, prompt, context_conversation_id,
                                        "intent_model_unavailable",
                                        "The research-intent model is currently unavailable. The original question was preserved and parsing can be retried later.")
        try:
            candidate = self._intent_resolver(prompt, timeout_seconds=self._intent_timeout_seconds)
            if isinstance(candidate, dict) and isinstance(candidate.get("provenance"), dict):
                candidate = {**candidate, "provenance": {
                    **candidate["provenance"], "prompt_version": INTENT_PROMPT_VERSION,
                }}
            return resolve_model_intent(
                candidate, context_company_id=context_company,
                context_conversation_id=context_conversation_id,
                coverage_lookup=self._coverage,
                entity_catalog=catalog,
                context_company_ids=context_ids,
            )
        except (TimeoutError, RuntimeError, OSError):
            return self._intent_failure(question, prompt, context_conversation_id,
                                        "intent_model_unavailable",
                                        "The research-intent model call failed or timed out. The original question was preserved and parsing can be retried later.")
        except (IntentSchemaError, ValueError, TypeError, KeyError):
            return self._intent_failure(question, prompt, context_conversation_id,
                                        "intent_schema_invalid",
                                        "The research-intent model returned an invalid structure. Keyword rules were not used to guess, and the original question was preserved.")

    @staticmethod
    def _fingerprint(question: str, context_conversation_id: str | None) -> str:
        bound = _json({"question": question, "context_conversation_id": context_conversation_id})
        return hashlib.sha256(bound.encode("utf-8")).hexdigest()

    def create(self, question, context_conversation_id, request_id, origin="user") -> dict:
        question = _valid_text(question, "question", self.chat.max_user_chars)
        request_id = _valid_text(request_id, "request_id", 200)
        if not isinstance(origin, str) or origin not in _ORIGINS:
            raise ChatError("invalid_origin", "origin must be user or machine_acceptance")
        if context_conversation_id is not None:
            context_conversation_id = _valid_text(context_conversation_id, "context_conversation_id", 200)
        fingerprint = self._fingerprint(question, context_conversation_id)
        # Check idempotency before the network call. No transaction or SQLite lock is
        # held while the model resolves intent.
        with closing(self.chat._connect()) as connection:
            existing = connection.execute(
                "SELECT task_id,request_fingerprint,origin FROM research_tasks WHERE request_id=?",
                (request_id,),
            ).fetchone()
        if existing is not None:
            if existing["request_fingerprint"] != fingerprint or existing["origin"] != origin:
                raise ChatError("idempotency_conflict", "request_id is already bound to a different research request.", 409)
            return self.get(existing["task_id"])
        with self._request_locks_guard:
            entry = self._request_locks.get(request_id)
            if entry is None:
                entry = {"lock": threading.Lock(), "fingerprint": fingerprint, "origin": origin, "users": 0}
                self._request_locks[request_id] = entry
            elif entry["fingerprint"] != fingerprint or entry["origin"] != origin:
                raise ChatError("idempotency_conflict", "request_id is already bound to a different research request.", 409)
            entry["users"] += 1
        entry["lock"].acquire()
        try:
            with closing(self.chat._connect()) as connection:
                existing = connection.execute(
                    "SELECT task_id,request_fingerprint,origin FROM research_tasks WHERE request_id=?", (request_id,)
                ).fetchone()
            if existing is not None:
                if existing["request_fingerprint"] != fingerprint or existing["origin"] != origin:
                    raise ChatError("idempotency_conflict", "request_id is already bound to a different research request.", 409)
                return self.get(existing["task_id"])

            # The per-service key is held during the model call, but no SQLite
            # transaction or writer lock is. Same-key retries wait and reuse it.
            resolution = self.intake(question, context_conversation_id)
            task_id, created_at = f"task_{uuid.uuid4().hex}", _now()
            with closing(self.chat._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT task_id,request_fingerprint,origin FROM research_tasks WHERE request_id=?", (request_id,)
                ).fetchone()
                if existing is not None:
                    connection.rollback()
                    if existing["request_fingerprint"] != fingerprint or existing["origin"] != origin:
                        raise ChatError("idempotency_conflict", "request_id is already bound to a different research request.", 409)
                    return self.get(existing["task_id"])
                connection.execute(
                    "INSERT INTO research_tasks(task_id,question,context_conversation_id,request_id,"
                    "request_fingerprint,current_scope_version,created_at,origin) VALUES(?,?,?,?,?,?,?,?)",
                    (task_id, question, context_conversation_id, request_id, fingerprint, 1, created_at, origin),
                )
                connection.execute("INSERT INTO research_scope_versions VALUES(?,?,?,?)",
                                   (task_id, 1, _json(resolution), created_at))
                connection.commit()
            return self.get(task_id)
        finally:
            entry["lock"].release()
            with self._request_locks_guard:
                entry["users"] -= 1
                if entry["users"] == 0 and self._request_locks.get(request_id) is entry:
                    del self._request_locks[request_id]

    def _row(self, connection, task_id: str):
        return connection.execute(
            "SELECT t.*,s.resolution_json FROM research_tasks t "
            "JOIN research_scope_versions s ON s.task_id=t.task_id AND s.scope_version=t.current_scope_version "
            "WHERE t.task_id=?", (task_id,),
        ).fetchone()

    @staticmethod
    def _bridge(task_id: str, scope_version: int, company_id: str, row, company_ids=None) -> dict:
        return {
            "task_id": task_id, "scope_version": scope_version, "company_id": company_id,
            "conversation_id": row["conversation_id"], "run_id": row["run_id"],
            "company_ids": list(company_ids) if company_ids is not None else [company_id],
        }

    def get(self, task_id, include_internal=False) -> dict:
        task_id = _valid_text(task_id, "task_id", 200)
        if type(include_internal) is not bool:
            raise ChatError("invalid_include_internal", "include_internal must be a boolean")
        with closing(self.chat._connect()) as connection:
            row = self._row(connection, task_id)
            if row is None:
                raise ChatError("task_not_found", "Research task not found.", 404)
            resolution = json.loads(row["resolution_json"])
            runs = connection.execute(
                "SELECT tr.scope_version,tr.conversation_id,tr.run_id,tr.created_at,r.status "
                "FROM research_task_runs tr JOIN runs r ON r.run_id=tr.run_id "
                "WHERE tr.task_id=? ORDER BY tr.scope_version", (task_id,),
            ).fetchall()
            latest = connection.execute(
                "SELECT r.run_id,r.conversation_id AS run_conversation_id,r.status AS run_status,"
                "r.created_at AS run_created_at,r.error_json,r.cancel_requested,"
                "d.decision_id,d.kind AS decision_kind,d.question AS decision_question,"
                "d.explanation AS decision_explanation,a.action AS decision_status FROM runs r "
                "LEFT JOIN research_decisions d ON d.run_id=r.run_id "
                "LEFT JOIN research_decision_answers a ON a.decision_id=d.decision_id "
                "WHERE r.conversation_id IN (SELECT conversation_id FROM research_task_runs WHERE task_id=?) "
                "ORDER BY r.created_at DESC,r.rowid DESC LIMIT 1", (task_id,),
            ).fetchone()
            latest_report = connection.execute(
                "SELECT m.conversation_id,m.message_id,m.run_id,m.created_at FROM messages m "
                "JOIN report_publications p ON p.message_id=m.message_id "
                "WHERE m.conversation_id IN (SELECT conversation_id FROM research_task_runs WHERE task_id=?) "
                "AND m.role='assistant' AND m.status='completed' "
                "ORDER BY m.created_at DESC,m.rowid DESC LIMIT 1", (task_id,),
            ).fetchone()
        if not include_internal:
            metadata = resolution.get("scope", {}).get("intent_metadata")
            if isinstance(metadata, dict):
                resolution["scope"]["intent_metadata"] = {
                    key: metadata[key] for key in ("prompt_version", "provider", "model", "error_code")
                    if key in metadata
                }
        return {
            "task_id": row["task_id"], "question": row["question"],
            "context_conversation_id": row["context_conversation_id"],
            "origin": row["origin"],
            "created_at": row["created_at"], "current_scope_version": row["current_scope_version"],
            **resolution,
            "runs": [{key: item[key] for key in ("scope_version", "conversation_id", "run_id", "status", "created_at")}
                     for item in runs],
            "latest_run": _run_summary(self.chat, latest),
            "latest_report": _reader_report(latest_report),
        }

    def list(self, include_internal=False) -> dict:
        if type(include_internal) is not bool:
            raise ChatError("invalid_include_internal", "include_internal must be a boolean")
        with closing(self.chat._connect()) as connection:
            rows = connection.execute(
                "SELECT t.task_id,t.question,t.origin,t.created_at,t.current_scope_version,s.resolution_json,"
                "r.status AS run_status,r.error_json,r.started_at,r.finished_at,r.conversation_id AS run_conversation_id,"
                "r.run_id,r.created_at AS run_created_at,r.cancel_requested,"
                "d.decision_id,d.kind AS decision_kind,d.question AS decision_question,"
                "d.explanation AS decision_explanation,a.action AS decision_status,"
                "m.message_id AS answer_message_id,m.content AS answer_content,m.verification_json,"
                "p.message_id AS published_message_id FROM research_tasks t "
                "JOIN research_scope_versions s ON s.task_id=t.task_id AND s.scope_version=t.current_scope_version "
                "LEFT JOIN research_task_runs tr ON tr.task_id=t.task_id AND tr.scope_version=t.current_scope_version "
                "LEFT JOIN runs r ON r.run_id=(SELECT newer.run_id FROM runs newer "
                "WHERE newer.conversation_id=tr.conversation_id ORDER BY newer.created_at DESC,newer.rowid DESC LIMIT 1) "
                "LEFT JOIN research_decisions d ON d.run_id=r.run_id "
                "LEFT JOIN research_decision_answers a ON a.decision_id=d.decision_id "
                "LEFT JOIN messages m ON m.run_id=r.run_id AND m.role='assistant' AND m.status='completed' "
                "LEFT JOIN report_publications p ON p.message_id=m.message_id "
                "WHERE (? OR t.origin='user') ORDER BY t.created_at DESC,t.task_id",
                (include_internal,),
            ).fetchall()
        tasks = []
        for row in rows:
            resolution = json.loads(row["resolution_json"])
            answer = " ".join((row["answer_content"] or "").split())
            if len(answer) > 180:
                answer = answer[:179].rstrip() + "…"
            error = json.loads(row["error_json"]) if row["error_json"] else {}
            failure_reason = error.get("message", "") if isinstance(error, dict) else ""
            if not isinstance(failure_reason, str):
                failure_reason = ""
            verification = json.loads(row["verification_json"]) if row["verification_json"] else {}
            # A persisted review warning is real; absence of one is not proof of safety.
            risk_status = "needs_review" if isinstance(verification, dict) and verification.get("status") == "needs_review" else "not_checked"
            if answer and row["run_status"] == "completed":
                answer_status = "answered"
            elif answer:
                answer_status = "partial"
            else:
                answer_status = "unanswered"
            tasks.append({
                "task_id": row["task_id"], "question": row["question"], "created_at": row["created_at"],
                "origin": row["origin"],
                "current_scope_version": row["current_scope_version"], "status": resolution["status"],
                "code": resolution["code"], "message": resolution["message"],
                "subjects": resolution["scope"]["subjects"], "run_status": row["run_status"],
                "period_text": resolution["scope"].get("period_text"),
                "started_at": row["started_at"], "finished_at": row["finished_at"],
                "answer_status": answer_status, "answer_summary": answer,
                "failure_reason": failure_reason, "risk_status": risk_status,
                "latest_run": _run_summary(self.chat, row),
                **_reader_report_fields(
                    row["run_conversation_id"], row["published_message_id"], answer_status,
                    load_reader_payload(self.chat, conversation_id=row["run_conversation_id"],
                                        message_id=row["published_message_id"])
                    if row["run_conversation_id"] and row["published_message_id"] else None,
                ),
            })
        return {"tasks": tasks}

    def resolve(self, task_id, expected_scope_version, company_id) -> dict:
        task_id = _valid_text(task_id, "task_id", 200)
        company_id = _valid_text(company_id, "company_id", 100)
        if type(expected_scope_version) is not int or expected_scope_version < 1:
            raise ChatError("invalid_scope_version", "expected_scope_version must be a positive integer.")
        with closing(self.chat._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._row(connection, task_id)
            if row is None:
                raise ChatError("task_not_found", "Research task not found.", 404)
            if row["current_scope_version"] != expected_scope_version:
                raise ChatError("scope_version_conflict", "The research scope has changed. Refresh and try again.", 409)
            previous = json.loads(row["resolution_json"])
            allowed = {option["company_id"] for option in previous["options"]}
            if previous["status"] != "needs_clarification" or company_id not in allowed:
                raise ChatError("invalid_scope_choice", "This scope cannot be resolved with this company selection.", 409)
            coverage = self._coverage(company_id)
            ready = _coverage_available(coverage)
            scope = dict(previous["scope"])
            scope.update({
                "subjects": [company_subject(company_id, {
                    item["candidate_id"]: {"label": item["name"]}
                    for item in EvidenceService(self.chat.evidence_db).entity_catalog()})], "ambiguities": [],
                "human_selection": {"company_id": company_id, "from_scope_version": expected_scope_version},
            })
            resolution = {
                "status": "ready" if ready else "missing_data",
                "code": "scope_ready" if ready else "company_data_unavailable",
                "message": ("The single-company research scope was frozen using the user’s selection." if ready else "The scope is resolved, but materials are currently unavailable."),
                "scope": scope, "options": [], "coverage": coverage,
            }
            version = expected_scope_version + 1
            created_at = _now()
            connection.execute(
                "INSERT INTO research_scope_versions VALUES(?,?,?,?)",
                (task_id, version, _json(resolution), created_at),
            )
            changed = connection.execute(
                "UPDATE research_tasks SET current_scope_version=? WHERE task_id=? AND current_scope_version=?",
                (version, task_id, expected_scope_version),
            ).rowcount
            if changed != 1:
                raise ChatError("scope_version_conflict", "The research scope has changed. Refresh and try again.", 409)
            connection.commit()
        return self.get(task_id)

    @staticmethod
    def _retryable_intent_failure(resolution: dict) -> bool:
        return (resolution.get("status"), resolution.get("code")) in {
            ("model_unavailable", "intent_model_unavailable"),
            ("needs_review", "intent_schema_invalid"),
        }

    @staticmethod
    def _intent_retry_from(resolution: dict) -> int | None:
        metadata = resolution.get("scope", {}).get("intent_metadata")
        value = metadata.get("retry_from_scope_version") if isinstance(metadata, dict) else None
        return value if type(value) is int else None

    def retry_intent(self, task_id, expected_scope_version) -> dict:
        """Append one intake result for a persisted failed intent scope."""
        task_id = _valid_text(task_id, "task_id", 200)
        if type(expected_scope_version) is not int or expected_scope_version < 1:
            raise ChatError("invalid_scope_version", "expected_scope_version must be a positive integer.")

        key = (task_id, expected_scope_version)
        with self._retry_locks_guard:
            entry = self._retry_locks.get(key)
            if entry is None:
                entry = {"lock": threading.Lock(), "users": 0}
                self._retry_locks[key] = entry
            entry["users"] += 1
        entry["lock"].acquire()
        try:
            with closing(self.chat._connect()) as connection:
                row = self._row(connection, task_id)
                if row is None:
                    raise ChatError("task_not_found", "Research task not found.", 404)
                current = json.loads(row["resolution_json"])
                if (row["current_scope_version"] == expected_scope_version + 1
                        and self._intent_retry_from(current) == expected_scope_version):
                    # Lost-ack replay returns the durable next version without a paid call.
                    return self.get(task_id)
                if row["current_scope_version"] != expected_scope_version:
                    raise ChatError("scope_version_conflict", "The research scope has changed. Refresh and try again.", 409)
                if not self._retryable_intent_failure(current):
                    raise ChatError("intent_retry_not_allowed", "The current research scope does not allow reparsing the original question.", 409)
                question = row["question"]
                context_conversation_id = row["context_conversation_id"]

            # Reuse the original persisted input and normal intake path. No SQLite
            # transaction or writer lock is held while the model is awaited.
            resolution = self.intake(question, context_conversation_id)
            scope = dict(resolution.get("scope") or {})
            metadata = dict(scope.get("intent_metadata") or {})
            metadata["retry_from_scope_version"] = expected_scope_version
            scope["intent_metadata"] = metadata
            resolution = {**resolution, "scope": scope}
            version = expected_scope_version + 1
            created_at = _now()
            with closing(self.chat._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = self._row(connection, task_id)
                if row is None:
                    raise ChatError("task_not_found", "Research task not found.", 404)
                latest = json.loads(row["resolution_json"])
                if (row["current_scope_version"] == version
                        and self._intent_retry_from(latest) == expected_scope_version):
                    connection.rollback()
                    return self.get(task_id)
                if row["current_scope_version"] != expected_scope_version:
                    raise ChatError("scope_version_conflict", "The research scope has changed. Refresh and try again.", 409)
                if not self._retryable_intent_failure(latest):
                    raise ChatError("intent_retry_not_allowed", "The current research scope does not allow reparsing the original question.", 409)
                connection.execute(
                    "INSERT INTO research_scope_versions VALUES(?,?,?,?)",
                    (task_id, version, _json(resolution), created_at),
                )
                changed = connection.execute(
                    "UPDATE research_tasks SET current_scope_version=? WHERE task_id=? AND current_scope_version=?",
                    (version, task_id, expected_scope_version),
                ).rowcount
                if changed != 1:
                    raise ChatError("scope_version_conflict", "The research scope has changed. Refresh and try again.", 409)
                connection.commit()
            return self.get(task_id)
        finally:
            entry["lock"].release()
            with self._retry_locks_guard:
                entry["users"] -= 1
                if entry["users"] == 0 and self._retry_locks.get(key) is entry:
                    del self._retry_locks[key]

    def start(self, task_id, expected_scope_version) -> dict:
        task_id = _valid_text(task_id, "task_id", 200)
        if type(expected_scope_version) is not int or expected_scope_version < 1:
            raise ChatError("invalid_scope_version", "expected_scope_version must be a positive integer.")
        initial = self.get(task_id)
        if initial["current_scope_version"] != expected_scope_version:
            raise ChatError("scope_version_conflict", "The research scope has changed. Refresh and try again.", 409)
        if initial["status"] != "ready":
            raise ChatError("task_not_ready", "The research task does not yet have a clear, executable scope.", 409)
        company_ids = selected_companies(initial)
        if not company_ids:
            raise ChatError("unsupported_scope", "This execution requires a clearly defined set of registered subjects.", 409)
        # Navigation label only; every data tool uses the complete immutable set.
        company_id = company_ids[0]
        # A committed association is authoritative even if the provider later goes
        # offline; idempotent replay must still return the original bridge.
        existing = next((item for item in initial["runs"]
                         if item["scope_version"] == expected_scope_version), None)
        if existing is not None:
            return self._bridge(task_id, expected_scope_version, company_id, existing, company_ids)

        def guard(connection):
            row = self._row(connection, task_id)
            if row is None:
                raise ChatError("task_not_found", "Research task not found.", 404)
            if row["current_scope_version"] != expected_scope_version:
                raise ChatError("scope_version_conflict", "The research scope has changed. Refresh and try again.", 409)
            current = json.loads(row["resolution_json"])
            current_companies = selected_companies(current)
            if current["status"] != "ready" or current_companies != company_ids:
                raise ChatError("task_not_ready", "The research scope is no longer executable. Refresh.", 409)
            existing = connection.execute(
                "SELECT conversation_id,run_id FROM research_task_runs WHERE task_id=? AND scope_version=?",
                (task_id, expected_scope_version),
            ).fetchone()
            if existing is not None:
                return self._bridge(task_id, expected_scope_version, company_id, existing, company_ids)
            return None

        def link(connection, run):
            connection.execute(
                "INSERT INTO research_task_runs VALUES(?,?,?,?,?)",
                (task_id, expected_scope_version, run["conversation_id"], run["run_id"], _now()),
            )
            self.chat._insert_intent_context(
                connection, run["run_id"], initial, initial["question"]
            )

        run = self.chat.create_and_submit(
            company_id, initial["question"], transaction_guard=guard, transaction_hook=link, company_ids=company_ids)
        return {
            "task_id": task_id, "scope_version": expected_scope_version, "company_id": company_id,
            "conversation_id": run["conversation_id"], "run_id": run["run_id"],
            "company_ids": list(company_ids),
        }
