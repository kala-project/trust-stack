"""Append-only, conversation-owned research inputs, independent of event payloads."""
from contextlib import closing
import hashlib
import json

from .execution_scope import ExecutionScopes
from .tool_errors import ToolError

CONTRACT_VERSION = "research-inputs/1"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_input_contracts (
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
 version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_inputs (
 conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
 input_id TEXT NOT NULL,
 origin_run_id TEXT NOT NULL REFERENCES runs(run_id),
 payload_json TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL,
 PRIMARY KEY(conversation_id,input_id)
);
CREATE TABLE IF NOT EXISTS research_input_uses (
 run_id TEXT NOT NULL REFERENCES runs(run_id),
 conversation_id TEXT NOT NULL,
 input_id TEXT NOT NULL,
 PRIMARY KEY(run_id,input_id),
 FOREIGN KEY(conversation_id,input_id) REFERENCES research_inputs(conversation_id,input_id)
);
"""


class InputError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def canonical_derivation(result):
    if not isinstance(result, dict) or not isinstance(result.get("fact"), dict):
        raise InputError("invalid_research_input", "A recorded input must contain a typed fact")
    return {key: value for key, value in result.items()
            if not key.startswith("_") and key != "reused_from"}


def _encoded(result):
    return json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ResearchInputStore:
    """No source acquisition, financial interpretation, or model execution."""
    def __init__(self, connect):
        self.connect = connect
        self.scopes = ExecutionScopes(connect)
        with closing(connect()) as connection:
            connection.executescript(_SCHEMA)
            for table in ("research_input_contracts", "research_inputs", "research_input_uses"):
                for action in ("UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                        f"BEFORE {action} ON {table} BEGIN "
                        "SELECT RAISE(ABORT,'research inputs are immutable'); END")
            connection.commit()

    @staticmethod
    def begin_run(connection, run_id):
        connection.execute("INSERT INTO research_input_contracts VALUES(?,?)", (run_id, CONTRACT_VERSION))

    @staticmethod
    def _has_contract(connection, run_id):
        row = connection.execute(
            "SELECT version FROM research_input_contracts WHERE run_id=?", (run_id,)).fetchone()
        if row is not None and row["version"] != CONTRACT_VERSION:
            raise InputError("input_contract_unsupported", "Research input contract is not supported")
        return row is not None

    def has_contract(self, run_id):
        with closing(self.connect()) as connection:
            return self._has_contract(connection, run_id)

    @staticmethod
    def _owner(connection, run_id):
        row = connection.execute(
            "SELECT r.conversation_id,c.company_id FROM runs r "
            "JOIN conversations c USING(conversation_id) WHERE r.run_id=?", (run_id,)).fetchone()
        if row is None:
            raise InputError("input_out_of_scope", "Research run does not exist")
        try:
            scope = ExecutionScopes.read_in(connection, row["conversation_id"])
        except ToolError as exc:
            raise InputError(exc.code, exc.message) from exc
        return {**dict(row), "company_ids": scope["company_ids"], "scope_id": scope["scope_id"]}

    @staticmethod
    def _decode(row):
        raw = row["payload_json"]
        if hashlib.sha256(raw.encode()).hexdigest() != row["payload_sha256"]:
            raise InputError("input_integrity_mismatch", "Stored research input content has changed")
        try:
            result = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise InputError("input_integrity_mismatch", "Stored research input is invalid") from exc
        if canonical_derivation(result) != result or result["fact"].get("fact_id") != row["input_id"]:
            raise InputError("input_integrity_mismatch", "Stored research input identity has changed")
        return result

    @classmethod
    def _read(cls, connection, run_id, input_id):
        if not isinstance(input_id, str) or not input_id:
            raise InputError("invalid_research_input", "Input reference must be a nonempty string")
        owner = cls._owner(connection, run_id)
        row = connection.execute(
            "SELECT i.* FROM research_input_uses u JOIN research_inputs i "
            "ON i.conversation_id=u.conversation_id AND i.input_id=u.input_id "
            "WHERE u.run_id=? AND u.conversation_id=? AND u.input_id=?",
            (run_id, owner["conversation_id"], input_id)).fetchone()
        if row is None:
            raise InputError("research_input_missing", "Input is not registered for this research version")
        origin = cls._owner(connection, row["origin_run_id"])
        if origin["conversation_id"] != owner["conversation_id"]:
            raise InputError("input_out_of_scope", "Input origin belongs to a different conversation")
        result = cls._decode(row)
        if result["fact"].get("company_id") not in owner["company_ids"]:
            raise InputError("input_out_of_scope", "Input company differs from its research scope")
        if row["origin_run_id"] != run_id:
            result["reused_from"] = {"run_id": row["origin_run_id"], "fact_id": input_id}
        return result

    def read(self, run_id, input_id, *, consumer_run_id=None):
        with closing(self.connect()) as connection:
            connection.execute("BEGIN")
            owner = self._owner(connection, run_id)
            if consumer_run_id is not None:
                consumer = self._owner(connection, consumer_run_id)
                if owner["conversation_id"] != consumer["conversation_id"]:
                    raise InputError("input_out_of_scope", "An input cannot cross conversations")
            result = self._read(connection, run_id, input_id)
            if consumer_run_id is not None and consumer_run_id != run_id and "reused_from" not in result:
                result["reused_from"] = {"run_id": run_id, "fact_id": input_id}
            return result

    def lookup(self, consumer_run_id, input_id):
        """Resolve a declared input in this conversation, without searching tool events."""
        if not isinstance(input_id, str) or not input_id:
            raise InputError("invalid_research_input", "Input reference must be a nonempty string")
        with closing(self.connect()) as connection:
            connection.execute("BEGIN")
            owner = self._owner(connection, consumer_run_id)
            row = connection.execute(
                "SELECT u.run_id FROM research_input_uses u JOIN research_inputs i "
                "ON i.conversation_id=u.conversation_id AND i.input_id=u.input_id "
                "WHERE u.conversation_id=? AND u.input_id=? "
                "ORDER BY (u.run_id=i.origin_run_id) DESC,u.rowid LIMIT 1",
                (owner["conversation_id"], input_id)).fetchone()
            if row is None:
                raise InputError("research_input_missing", "Input is not registered in this conversation")
            source_run_id = row["run_id"]
            if not self._has_contract(connection, source_run_id):
                raise InputError("input_integrity_mismatch", "Registered input lacks its run contract")
            result = self._read(connection, source_run_id, input_id)
            if consumer_run_id != source_run_id and "reused_from" not in result:
                result["reused_from"] = {"run_id": source_run_id, "fact_id": input_id}
            return source_run_id, result

    def catalog(self, run_id, *, after="", limit=50):
        if (not isinstance(after, str) or len(after) > 200
                or type(limit) is not int or not 1 <= limit <= 50):
            raise InputError("invalid_arguments", "Input catalog requires a bounded cursor and limit 1..50")
        with closing(self.connect()) as connection:
            connection.execute("BEGIN")
            owner = self._owner(connection, run_id)
            rows = connection.execute(
                "SELECT * FROM research_inputs WHERE conversation_id=? AND input_id>? ORDER BY input_id LIMIT ?",
                (owner["conversation_id"], after, limit + 1)).fetchall()
            summaries = []
            for row in rows[:limit]:
                fact = self._decode(row)["fact"]
                if fact.get("company_id") not in owner["company_ids"]:
                    raise InputError("input_out_of_scope", "Stored input company differs from its conversation")
                summaries.append({
                    "input_id": row["input_id"], "origin_run_id": row["origin_run_id"],
                    **{key: fact.get(key) for key in ("metric", "unit", "period_start", "period_end", "period_kind", "basis")},
                    "document_version_id": fact.get("locator", {}).get("document_version_id"),
                })
            return {"inputs": summaries, "next_cursor": rows[limit - 1]["input_id"] if len(rows) > limit else None,
                    "limitation": "Catalog entries are recorded inputs, not validation of current source bytes or semantic interpretation."}

    @classmethod
    def record(cls, connection, run_id, result):
        """Called in the same transaction as the corresponding authoritative event."""
        if not cls._has_contract(connection, run_id):
            return
        owner = cls._owner(connection, run_id)
        state = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if state["status"] not in {"queued", "running"}:
            raise InputError("inactive_research", "Stopped research cannot register inputs")
        payload = canonical_derivation(result)
        fact = payload["fact"]
        input_id = fact.get("fact_id")
        if (not isinstance(input_id, str) or not input_id
                or fact.get("company_id") not in owner["company_ids"]):
            raise InputError("invalid_research_input", "Input identity or company is invalid")
        raw = _encoded(payload)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        previous = connection.execute(
            "SELECT * FROM research_inputs WHERE conversation_id=? AND input_id=?",
            (owner["conversation_id"], input_id)).fetchone()
        if previous is not None:
            if cls._decode(previous) != payload:
                raise InputError("input_conflict", "An existing research input cannot be reinterpreted in place")
        reuse = result.get("reused_from")
        if reuse is not None:
            if (not isinstance(reuse, dict) or set(reuse) != {"run_id", "fact_id"}
                    or not isinstance(reuse["run_id"], str)
                    or reuse["fact_id"] != input_id or reuse["run_id"] == run_id
                    or cls._owner(connection, reuse["run_id"])["conversation_id"] != owner["conversation_id"]):
                raise InputError("input_out_of_scope", "Input reuse origin is invalid")
            if previous is not None and previous["origin_run_id"] != reuse["run_id"]:
                raise InputError("input_conflict", "Input reuse does not name its original derivation")
            if cls._has_contract(connection, reuse["run_id"]):
                original = cls._read(connection, reuse["run_id"], input_id)
                if "reused_from" in original or original != payload:
                    raise InputError("input_conflict", "Input reuse differs from its original derivation")
        if previous is None:
            connection.execute("INSERT INTO research_inputs VALUES(?,?,?,?,?)", (
                owner["conversation_id"], input_id, reuse["run_id"] if reuse else run_id, raw, digest))
        connection.execute("INSERT OR IGNORE INTO research_input_uses VALUES(?,?,?)",
                           (run_id, owner["conversation_id"], input_id))
