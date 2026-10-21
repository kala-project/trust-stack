"""Local, single-user Wiki change loop over the read-only evidence artifact.

This is deliberately not an identity/role approval system and performs no
financial semantic verification.  Wiki state is stored in a separate SQLite
file; the evidence artifact and raw inputs are never written.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any
import uuid


MAX_TITLE_CHARS = 200
MAX_STATEMENTS = 100
MAX_STATEMENT_CHARS = 4_000
MAX_CLAIMS_PER_STATEMENT = 50
MAX_ID_CHARS = 200
KINDS = {"evidence_summary", "hypothesis", "open_question"}
DECISIONS = {"approve", "reject"}
LIMITATIONS = [
    "Locally approved by one operator; this is not production identity or role approval.",
    "All page semantics remain unreviewed; citation lint is not financial semantic verification.",
    "Hypotheses and open questions are unverified and must not be presented as facts.",
    "Source rights remain constrained by the evidence artifact; approval does not permit redistribution.",
]

_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS wiki_meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS page_pointers (
    company_id TEXT PRIMARY KEY, current_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    company_id TEXT NOT NULL, version INTEGER NOT NULL, title TEXT NOT NULL,
    statements_json TEXT NOT NULL, content_hash TEXT NOT NULL,
    evidence_version_json TEXT NOT NULL, created_at TEXT NOT NULL,
    change_id TEXT NOT NULL UNIQUE,
    PRIMARY KEY(company_id, version)
);
CREATE TABLE IF NOT EXISTS changesets (
    change_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, base_version INTEGER NOT NULL,
    title TEXT NOT NULL, statements_json TEXT NOT NULL, content_hash TEXT NOT NULL,
    evidence_version_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT, change_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('created','approved','rejected','stale','published')),
    content_hash TEXT NOT NULL, detail_json TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY(change_id) REFERENCES changesets(change_id)
);
CREATE INDEX IF NOT EXISTS changes_company_idx ON changesets(company_id, created_at, change_id);
CREATE INDEX IF NOT EXISTS events_change_idx ON events(change_id, event_id);
CREATE TRIGGER IF NOT EXISTS versions_no_update BEFORE UPDATE ON versions BEGIN SELECT RAISE(ABORT, 'versions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS versions_no_delete BEFORE DELETE ON versions BEGIN SELECT RAISE(ABORT, 'versions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS changes_no_update BEFORE UPDATE ON changesets BEGIN SELECT RAISE(ABORT, 'changesets are append-only'); END;
CREATE TRIGGER IF NOT EXISTS changes_no_delete BEFORE DELETE ON changesets BEGIN SELECT RAISE(ABORT, 'changesets are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
"""


class WikiError(ValueError):
    """Stable service error suitable for a future HTTP adapter."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _required_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise WikiError("invalid_arguments", f"{name} must be a string")
    result = value.strip()
    if not result or len(result) > maximum:
        raise WikiError("invalid_arguments", f"{name} must contain 1..{maximum} characters")
    return result


def _integer(value: Any, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise WikiError("invalid_arguments", f"{name} must be an integer >= {minimum}")
    return value


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


def _manifest_value(value: Any) -> list[Any]:
    """Encode SQLite scalar types without JSON type collisions."""
    if value is None:
        return ["null", None]
    if isinstance(value, bytes):
        return ["blob", value.hex()]
    if isinstance(value, int):
        return ["integer", value]
    if isinstance(value, float):
        return ["real", repr(value)]
    return ["text", value]


class WikiService:
    """Versioned local research notes with hash-bound manual acknowledgement."""

    def __init__(self, evidence_db, wiki_db):
        evidence_db = Path(evidence_db).resolve()
        wiki_db = Path(wiki_db).resolve()
        if _database_paths_alias(evidence_db, wiki_db):
            raise WikiError("database_path_collision", "evidence and wiki databases must be distinct")
        self.evidence_db = evidence_db
        self.wiki_db = wiki_db
        if not self.evidence_db.is_file():
            raise WikiError("evidence_unavailable", "evidence database does not exist", 503)
        self.wiki_db.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.wiki_db)
        try:
            connection.executescript(_SCHEMA)
            with connection:
                row = connection.execute("SELECT value FROM wiki_meta WHERE key='schema_version'").fetchone()
                if row is None:
                    connection.execute("INSERT INTO wiki_meta VALUES('schema_version','local-wiki/1.0.0')")
                elif row[0] != "local-wiki/1.0.0":
                    raise WikiError("wiki_schema_unknown", "wiki database schema is not supported", 503)
        finally:
            connection.close()
        try:
            os.chmod(self.wiki_db, 0o600)
        except OSError:
            pass  # Best-effort local hardening; deployment policy remains out of scope.

    def _wiki(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.wiki_db, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _evidence(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(f"file:{self.evidence_db}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            return connection
        except sqlite3.Error as exc:
            raise WikiError("evidence_unavailable", "evidence database cannot be read", 503) from exc

    @staticmethod
    def _manifest(connection: sqlite3.Connection) -> dict:
        try:
            metadata = dict(connection.execute("SELECT key,value FROM metadata ORDER BY key"))
            schema = list(connection.execute(
                "SELECT name,sql FROM sqlite_schema "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ))
            tables = []
            for table_name, table_sql in schema:
                quoted_name = '"' + table_name.replace('"', '""') + '"'
                columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted_name})")]
                quoted_columns = ['"' + column.replace('"', '""') + '"' for column in columns]
                order = ",".join(quoted_columns)
                rows = [
                    [_manifest_value(value) for value in row]
                    for row in connection.execute(f"SELECT * FROM {quoted_name} ORDER BY {order}")
                ]
                tables.append({"name": table_name, "sql": table_sql, "columns": columns, "rows": rows})
        except sqlite3.Error as exc:
            raise WikiError("evidence_unavailable", "evidence database schema cannot be read", 503) from exc
        required = ("schema_version", "producer", "build_version", "generated_at", "input_hashes")
        if any(not metadata.get(key) for key in required):
            raise WikiError("evidence_unavailable", "evidence build manifest is incomplete", 503)
        manifest = {"tables": tables}
        return {
            "generated_at": metadata["generated_at"],
            "schema_version": metadata["schema_version"],
            "build_version": metadata["build_version"],
            "manifest_hash": hashlib.sha256(_json(manifest).encode()).hexdigest(),
        }

    def _snapshot(self, company_id: Any, statements: list[dict] | None = None) -> dict:
        company_id = _required_text(company_id, "company_id", 100)
        connection = self._evidence()
        try:
            connection.execute("BEGIN")
            if connection.execute("SELECT 1 FROM companies WHERE company_id=?", (company_id,)).fetchone() is None:
                raise WikiError("company_not_found", "company does not exist", 404)
            version = self._manifest(connection)
            if statements is not None:
                claim_ids = sorted({claim_id for item in statements for claim_id in item["claim_ids"]})
                claims = {}
                if claim_ids:
                    placeholders = ",".join("?" for _ in claim_ids)
                    for row in connection.execute(
                        f"SELECT claim_id,company_id,evidence_ids_json FROM claims WHERE claim_id IN ({placeholders})",
                        claim_ids,
                    ):
                        claims[row["claim_id"]] = row
                missing = [claim_id for claim_id in claim_ids if claim_id not in claims]
                if missing:
                    raise WikiError("invalid_claim_reference", "one or more claim references do not exist")
                if any(row["company_id"] != company_id for row in claims.values()):
                    raise WikiError("foreign_claim_reference", "claim references must belong to the page company")
                for claim_id, row in claims.items():
                    try:
                        evidence_ids = json.loads(row["evidence_ids_json"])
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise WikiError("invalid_claim_reference", f"claim {claim_id} has invalid evidence") from exc
                    if not isinstance(evidence_ids, list) or not evidence_ids:
                        raise WikiError("invalid_claim_reference", f"claim {claim_id} has no source-root evidence")
                    placeholders = ",".join("?" for _ in evidence_ids)
                    evidence_rows = list(connection.execute(
                        f"""SELECT e.evidence_id,e.quote,e.start,e.end,s.source_id
                            FROM evidence e JOIN sources s ON s.source_id=e.source_id
                            WHERE e.evidence_id IN ({placeholders})""", evidence_ids
                    ))
                    if len(evidence_rows) != len(set(evidence_ids)) or any(
                        not item["quote"] or item["end"] <= item["start"] or
                        item["source_id"].lower().startswith(("wiki:", "wiki-"))
                        for item in evidence_rows
                    ):
                        raise WikiError("invalid_claim_reference", f"claim {claim_id} lacks a valid non-Wiki quote locator")
            connection.commit()
            return version
        except sqlite3.Error as exc:
            raise WikiError("evidence_unavailable", "evidence snapshot could not be read", 503) from exc
        finally:
            connection.close()

    @staticmethod
    def _normalize_statements(statements: Any) -> list[dict]:
        if not isinstance(statements, list) or len(statements) > MAX_STATEMENTS:
            raise WikiError("invalid_arguments", f"statements must be a list with at most {MAX_STATEMENTS} items")
        normalized = []
        for index, item in enumerate(statements):
            if not isinstance(item, dict) or set(item) != {"kind", "text", "claim_ids"}:
                raise WikiError("invalid_arguments", f"statements[{index}] has unknown or missing fields")
            if item["kind"] not in KINDS:
                raise WikiError("invalid_arguments", f"statements[{index}].kind is invalid")
            text = _required_text(item["text"], f"statements[{index}].text", MAX_STATEMENT_CHARS)
            claim_ids = item["claim_ids"]
            if not isinstance(claim_ids, list) or len(claim_ids) > MAX_CLAIMS_PER_STATEMENT:
                raise WikiError("invalid_arguments", f"statements[{index}].claim_ids must be a bounded list")
            clean_ids = []
            for claim_id in claim_ids:
                clean_ids.append(_required_text(claim_id, f"statements[{index}].claim_ids", MAX_ID_CHARS))
            if len(clean_ids) != len(set(clean_ids)):
                raise WikiError("invalid_arguments", f"statements[{index}].claim_ids contains duplicates")
            if item["kind"] == "evidence_summary" and not clean_ids:
                raise WikiError("invalid_arguments", "evidence_summary requires at least one claim reference")
            normalized.append({"kind": item["kind"], "text": text, "claim_ids": clean_ids})
        return normalized

    @staticmethod
    def _current(connection: sqlite3.Connection, company_id: str) -> int:
        row = connection.execute("SELECT current_version FROM page_pointers WHERE company_id=?", (company_id,)).fetchone()
        return row[0] if row else 0

    @staticmethod
    def _event(connection: sqlite3.Connection, change_id: str, event_type: str,
               content_hash: str, detail: dict | None = None) -> None:
        connection.execute(
            "INSERT INTO events(change_id,event_type,content_hash,detail_json,created_at) VALUES(?,?,?,?,?)",
            (change_id, event_type, content_hash, _json(detail or {}), _now()),
        )

    def get_page(self, company_id, version=None) -> dict:
        company_id = _required_text(company_id, "company_id", 100)
        current_evidence = self._snapshot(company_id)
        if version is not None:
            version = _integer(version, "version")
        connection = self._wiki()
        try:
            current = self._current(connection, company_id)
            selected = current if version is None else version
            versions = [row[0] for row in connection.execute(
                "SELECT version FROM versions WHERE company_id=? ORDER BY version", (company_id,)
            )]
            if selected == 0:
                if version not in (None, 0) or current != 0 and version == 0:
                    raise WikiError("page_version_not_found", "page version does not exist", 404)
                return {"company_id": company_id, "version": 0, "title": "Research notes",
                        "statements": [], "versions": versions, "freshness_status": "current",
                        "semantic_status": "unreviewed", "limitations": list(LIMITATIONS)}
            row = connection.execute(
                "SELECT * FROM versions WHERE company_id=? AND version=?", (company_id, selected)
            ).fetchone()
            if row is None:
                raise WikiError("page_version_not_found", "page version does not exist", 404)
            frozen_evidence = json.loads(row["evidence_version_json"])
            return {"company_id": company_id, "version": row["version"], "title": row["title"],
                    "statements": json.loads(row["statements_json"]), "content_hash": row["content_hash"],
                    "data_version": frozen_evidence, "versions": versions,
                    "freshness_status": "current" if frozen_evidence == current_evidence else "needs_review",
                    "semantic_status": "unreviewed", "limitations": list(LIMITATIONS)}
        finally:
            connection.close()

    def create_change(self, company_id, base_version, title, statements) -> dict:
        company_id = _required_text(company_id, "company_id", 100)
        base_version = _integer(base_version, "base_version")
        title = _required_text(title, "title", MAX_TITLE_CHARS)
        statements = self._normalize_statements(statements)
        evidence_version = self._snapshot(company_id, statements)
        body = {"company_id": company_id, "base_version": base_version, "title": title,
                "statements": statements, "evidence_version": evidence_version}
        content_hash = hashlib.sha256(_json(body).encode()).hexdigest()
        change_id = "chg-" + uuid.uuid4().hex
        connection = self._wiki()
        try:
            with connection:
                if self._current(connection, company_id) != base_version:
                    raise WikiError("stale_base", "base_version is not current", 409)
                connection.execute(
                    """INSERT INTO changesets(change_id,company_id,base_version,title,statements_json,
                       content_hash,evidence_version_json,created_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (change_id, company_id, base_version, title, _json(statements), content_hash,
                     _json(evidence_version), _now()),
                )
                self._event(connection, change_id, "created", content_hash)
            return self.get_change(change_id)
        finally:
            connection.close()

    @staticmethod
    def _change_dict(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
        events = list(connection.execute(
            "SELECT event_type,detail_json,created_at FROM events WHERE change_id=? ORDER BY event_id", (row["change_id"],)
        ))
        event_type = events[-1]["event_type"] if events else "created"
        status = {"created": "awaiting_approval", "approved": "approved", "rejected": "rejected",
                  "stale": "stale", "published": "applied"}[event_type]
        return {"change_id": row["change_id"], "company_id": row["company_id"],
                "base_version": row["base_version"], "title": row["title"],
                "statements": json.loads(row["statements_json"]), "content_hash": row["content_hash"],
                "data_version": json.loads(row["evidence_version_json"]), "status": status,
                "created_at": row["created_at"],
                "events": [{"type": event["event_type"], "detail": json.loads(event["detail_json"]),
                            "created_at": event["created_at"]} for event in events],
                "limitations": list(LIMITATIONS)}

    def get_change(self, change_id) -> dict:
        change_id = _required_text(change_id, "change_id", MAX_ID_CHARS)
        connection = self._wiki()
        try:
            row = connection.execute("SELECT * FROM changesets WHERE change_id=?", (change_id,)).fetchone()
            if row is None:
                raise WikiError("change_not_found", "change does not exist", 404)
            return self._change_dict(connection, row)
        finally:
            connection.close()

    def list_changes(self, company_id) -> list[dict]:
        company_id = _required_text(company_id, "company_id", 100)
        self._snapshot(company_id)
        connection = self._wiki()
        try:
            return [self._change_dict(connection, row) for row in connection.execute(
                "SELECT * FROM changesets WHERE company_id=? ORDER BY created_at,change_id", (company_id,)
            )]
        finally:
            connection.close()

    def _mark_stale(self, connection: sqlite3.Connection, row: sqlite3.Row, reason: str) -> None:
        last = connection.execute(
            "SELECT event_type FROM events WHERE change_id=? ORDER BY event_id DESC LIMIT 1", (row["change_id"],)
        ).fetchone()
        if last is None or last[0] != "stale":
            self._event(connection, row["change_id"], "stale", row["content_hash"], {"reason": reason})

    def review(self, change_id, expected_hash, decision) -> dict:
        change_id = _required_text(change_id, "change_id", MAX_ID_CHARS)
        expected_hash = _required_text(expected_hash, "expected_hash", 64)
        if decision not in DECISIONS:
            raise WikiError("invalid_arguments", "decision must be approve or reject")
        connection = self._wiki()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM changesets WHERE change_id=?", (change_id,)).fetchone()
                if row is None:
                    raise WikiError("change_not_found", "change does not exist", 404)
                if expected_hash != row["content_hash"]:
                    raise WikiError("hash_mismatch", "expected_hash does not match immutable change content", 409)
                current_status = self._change_dict(connection, row)["status"]
                if current_status != "awaiting_approval":
                    raise WikiError("change_not_reviewable", "change is no longer awaiting review", 409)
                current_evidence = self._snapshot(row["company_id"])
                if self._current(connection, row["company_id"]) != row["base_version"]:
                    self._mark_stale(connection, row, "base_version_changed")
                    result_error = WikiError("stale_change", "page baseline changed; create a new change", 409)
                elif current_evidence != json.loads(row["evidence_version_json"]):
                    self._mark_stale(connection, row, "evidence_version_changed")
                    result_error = WikiError("stale_change", "evidence data version changed; create a new change", 409)
                else:
                    self._event(connection, change_id, "approved" if decision == "approve" else "rejected",
                                row["content_hash"], {"acknowledgement": "local_manual_operator"})
                    result_error = None
            if result_error:
                raise result_error
            return self.get_change(change_id)
        finally:
            connection.close()

    def publish(self, change_id, expected_hash) -> dict:
        change_id = _required_text(change_id, "change_id", MAX_ID_CHARS)
        expected_hash = _required_text(expected_hash, "expected_hash", 64)
        connection = self._wiki()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute("SELECT * FROM changesets WHERE change_id=?", (change_id,)).fetchone()
                if row is None:
                    raise WikiError("change_not_found", "change does not exist", 404)
                if expected_hash != row["content_hash"]:
                    raise WikiError("hash_mismatch", "expected_hash does not match immutable change content", 409)
                if self._change_dict(connection, row)["status"] != "approved":
                    raise WikiError("approval_required", "change requires a current local manual approval", 409)
                evidence_version = self._snapshot(row["company_id"])
                if evidence_version != json.loads(row["evidence_version_json"]):
                    self._mark_stale(connection, row, "evidence_version_changed")
                    result_error = WikiError("stale_change", "evidence data version changed; approval cannot be reused", 409)
                elif self._current(connection, row["company_id"]) != row["base_version"]:
                    self._mark_stale(connection, row, "base_version_changed")
                    result_error = WikiError("stale_change", "page baseline changed; approval cannot be reused", 409)
                else:
                    new_version = row["base_version"] + 1
                    connection.execute(
                        """INSERT INTO versions(company_id,version,title,statements_json,content_hash,
                           evidence_version_json,created_at,change_id) VALUES(?,?,?,?,?,?,?,?)""",
                        (row["company_id"], new_version, row["title"], row["statements_json"],
                         row["content_hash"], row["evidence_version_json"], _now(), change_id),
                    )
                    if row["base_version"] == 0:
                        try:
                            connection.execute("INSERT INTO page_pointers VALUES(?,?)", (row["company_id"], new_version))
                        except sqlite3.IntegrityError:
                            self._mark_stale(connection, row, "base_version_changed")
                            result_error = WikiError("stale_change", "page baseline changed; approval cannot be reused", 409)
                        else:
                            result_error = None
                    else:
                        cursor = connection.execute(
                            "UPDATE page_pointers SET current_version=? WHERE company_id=? AND current_version=?",
                            (new_version, row["company_id"], row["base_version"]),
                        )
                        if cursor.rowcount != 1:
                            self._mark_stale(connection, row, "base_version_changed")
                            result_error = WikiError("stale_change", "page baseline changed; approval cannot be reused", 409)
                        else:
                            result_error = None
                    if result_error is None:
                        self._event(connection, change_id, "published", row["content_hash"], {"version": new_version})
            if result_error:
                raise result_error
            return self.get_page(row["company_id"], new_version)
        except sqlite3.IntegrityError as exc:
            raise WikiError("stale_change", "concurrent publication changed the baseline", 409) from exc
        finally:
            connection.close()
