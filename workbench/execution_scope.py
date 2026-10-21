"""Strict persisted execution membership for research conversations."""
from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
from typing import Literal, TypeGuard, TypedDict

from .tool_errors import ToolError


VERSION: Literal["research-execution-scope/1"] = "research-execution-scope/1"
_MAX_COMPANIES = 8
_MAX_COMPANY_ID_CHARS = 100
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_scope_contracts(
 conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id),
 version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversation_scopes(
 conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id),
 scope_id TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS conversation_scope_contracts_no_update
BEFORE UPDATE ON conversation_scope_contracts BEGIN SELECT RAISE(ABORT,'scope contracts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS conversation_scope_contracts_no_delete
BEFORE DELETE ON conversation_scope_contracts BEGIN SELECT RAISE(ABORT,'scope contracts are immutable'); END;
CREATE TRIGGER IF NOT EXISTS conversation_scopes_no_update
BEFORE UPDATE ON conversation_scopes BEGIN SELECT RAISE(ABORT,'execution scopes are immutable'); END;
CREATE TRIGGER IF NOT EXISTS conversation_scopes_no_delete
BEFORE DELETE ON conversation_scopes BEGIN SELECT RAISE(ABORT,'execution scopes are immutable'); END;
"""


class ExecutionScopePayloadJSON(TypedDict):
    version: Literal["research-execution-scope/1"]
    company_ids: list[str]


class ExecutionScopeJSON(ExecutionScopePayloadJSON):
    scope_id: str
    payload_sha256: str


def _invalid(message: str) -> ToolError:
    return ToolError("invalid_execution_scope", message)


def _is_company_collection(value: object) -> TypeGuard[list[object] | tuple[object, ...]]:
    return type(value) in (list, tuple)


def normalize(company_ids: object) -> tuple[str, ...]:
    """Validate an explicit company collection and return its canonical ordering."""
    if not _is_company_collection(company_ids):
        raise _invalid("Research scope company_ids must be a list or tuple")
    if not 1 <= len(company_ids) <= _MAX_COMPANIES:
        raise _invalid("Research scope needs 1..8 company IDs")
    checked: list[str] = []
    for company_id in company_ids:
        if (type(company_id) is not str or not company_id or company_id != company_id.strip()
                or len(company_id) > _MAX_COMPANY_ID_CHARS):
            raise _invalid("Research scope contains an invalid company ID")
        checked.append(company_id)
    if len(set(checked)) != len(checked):
        raise _invalid("Research scope company IDs must be distinct")
    return tuple(sorted(checked))


def _payload_json(company_ids: tuple[str, ...]) -> ExecutionScopePayloadJSON:
    return {"version": VERSION, "company_ids": list(company_ids)}


def _canonical_payload(company_ids: tuple[str, ...]) -> str:
    return json.dumps(
        _payload_json(company_ids), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class ExecutionScope:
    """Validated immutable domain scope; ``company_id`` display fields are not membership."""

    company_ids: tuple[str, ...]
    scope_id: str
    payload_sha256: str
    version: Literal["research-execution-scope/1"] = VERSION

    def __post_init__(self) -> None:
        if type(self.company_ids) is not tuple:
            raise _invalid("Research scope membership must be an immutable tuple")
        canonical_ids = normalize(self.company_ids)
        if canonical_ids != self.company_ids:
            raise _invalid("Research scope company IDs must use canonical ordering")
        if self.version != VERSION:
            raise _invalid("Research scope version is unsupported")
        if type(self.payload_sha256) is not str or _DIGEST.fullmatch(self.payload_sha256) is None:
            raise _invalid("Research scope payload hash is invalid")
        expected = hashlib.sha256(_canonical_payload(canonical_ids).encode()).hexdigest()
        if self.payload_sha256 != expected or self.scope_id != f"scope-{expected}":
            raise _invalid("Research scope identity or payload hash does not match its membership")

    @classmethod
    def create(cls, company_ids: object) -> ExecutionScope:
        canonical_ids = normalize(company_ids)
        digest = hashlib.sha256(_canonical_payload(canonical_ids).encode()).hexdigest()
        return cls(canonical_ids, f"scope-{digest}", digest)

    def payload_json(self) -> ExecutionScopePayloadJSON:
        return _payload_json(self.company_ids)

    def to_json(self) -> ExecutionScopeJSON:
        return {
            **self.payload_json(),
            "scope_id": self.scope_id,
            "payload_sha256": self.payload_sha256,
        }


def describe(company_ids: object) -> ExecutionScopeJSON:
    return ExecutionScope.create(company_ids).to_json()


def decode_scope(value: object) -> ExecutionScope:
    """Decode the complete external JSON form without aliases or unknown fields."""
    if type(value) is not dict or set(value) != {
        "version", "company_ids", "scope_id", "payload_sha256",
    }:
        raise _invalid("Research scope JSON fields are invalid")
    version = value.get("version")
    company_ids = value.get("company_ids")
    scope_id = value.get("scope_id")
    payload_sha256 = value.get("payload_sha256")
    if version != VERSION or type(scope_id) is not str or type(payload_sha256) is not str:
        raise _invalid("Research scope JSON values are invalid")
    if type(company_ids) is not list:
        raise _invalid("Research scope JSON company_ids must be an array")
    scope = ExecutionScope.create(company_ids)
    if list(scope.company_ids) != company_ids:
        raise _invalid("Research scope JSON company_ids must use canonical ordering")
    if scope.scope_id != scope_id or scope.payload_sha256 != payload_sha256:
        raise _invalid("Research scope JSON identity or hash does not match")
    return scope


def _row(value: object, field: str) -> sqlite3.Row:
    if not isinstance(value, sqlite3.Row):
        raise ToolError("execution_scope_corrupt", f"Research scope {field} row is invalid")
    return value


def _text(value: object, field: str) -> str:
    if type(value) is not str:
        raise ToolError("execution_scope_corrupt", f"Research scope {field} is not text")
    return value


def _decode_payload(raw_value: object) -> ExecutionScope:
    raw = _text(raw_value, "payload")
    try:
        value: object = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ToolError("execution_scope_corrupt", "Research scope record is invalid") from exc
    if type(value) is not dict or set(value) != {"version", "company_ids"}:
        raise ToolError("execution_scope_corrupt", "Research scope schema differs from its contract")
    if value.get("version") != VERSION:
        raise ToolError("execution_scope_corrupt", "Research scope schema differs from its contract")
    try:
        scope = ExecutionScope.create(value.get("company_ids"))
    except ToolError as exc:
        raise ToolError("execution_scope_corrupt", "Research scope membership is invalid") from exc
    if _canonical_payload(scope.company_ids) != raw:
        raise ToolError("execution_scope_corrupt", "Research scope payload is not canonical")
    return scope


class ExecutionScopes:
    def __init__(self, connect: Callable[[], sqlite3.Connection]):
        self._connect = connect
        with closing(connect()) as connection:
            connection.executescript(_SCHEMA)

    @staticmethod
    def create(connection: sqlite3.Connection, conversation_id: str,
               company_ids: object) -> ExecutionScopeJSON:
        scope = ExecutionScope.create(company_ids)
        fetched: object = connection.execute(
            "SELECT company_id FROM conversations WHERE conversation_id=?", (conversation_id,)
        ).fetchone()
        if fetched is None:
            raise ToolError("scope_owner_missing", "Research conversation does not exist")
        owner = _row(fetched, "owner")
        owner_company_id = _text(owner["company_id"], "owner company ID")
        if owner_company_id not in scope.company_ids:
            raise _invalid("Persisted conversation owner must belong to the explicit scope")
        raw = _canonical_payload(scope.company_ids)
        connection.execute(
            "INSERT INTO conversation_scope_contracts VALUES(?,?)", (conversation_id, VERSION))
        connection.execute(
            "INSERT INTO conversation_scopes VALUES(?,?,?,?)",
            (conversation_id, scope.scope_id, raw, scope.payload_sha256),
        )
        return scope.to_json()

    @staticmethod
    def read_in(connection: sqlite3.Connection, conversation_id: str) -> ExecutionScopeJSON:
        owner_value: object = connection.execute(
            "SELECT company_id FROM conversations WHERE conversation_id=?", (conversation_id,)
        ).fetchone()
        if owner_value is None:
            raise ToolError("scope_owner_missing", "Research conversation does not exist")
        owner = _row(owner_value, "owner")
        contract_value: object = connection.execute(
            "SELECT version FROM conversation_scope_contracts WHERE conversation_id=?", (conversation_id,)
        ).fetchone()
        scope_value: object = connection.execute(
            "SELECT * FROM conversation_scopes WHERE conversation_id=?", (conversation_id,)
        ).fetchone()
        if contract_value is None or scope_value is None:
            raise ToolError(
                "execution_scope_missing",
                "Explicit research scope contract and membership are required",
            )
        contract = _row(contract_value, "contract")
        row = _row(scope_value, "membership")
        if contract["version"] != VERSION:
            raise ToolError("execution_scope_corrupt", "Research scope contract version is unsupported")
        scope = _decode_payload(row["payload_json"])
        stored_hash = _text(row["payload_sha256"], "payload hash")
        stored_scope_id = _text(row["scope_id"], "scope ID")
        raw = _text(row["payload_json"], "payload")
        owner_company_id = _text(owner["company_id"], "owner company ID")
        if (hashlib.sha256(raw.encode()).hexdigest() != stored_hash
                or scope.scope_id != stored_scope_id
                or scope.payload_sha256 != stored_hash
                or owner_company_id not in scope.company_ids):
            raise ToolError("execution_scope_corrupt", "Research scope hash or owner does not match")
        return scope.to_json()

    def read(self, conversation_id: str) -> ExecutionScopeJSON:
        with closing(self._connect()) as connection:
            return self.read_in(connection, conversation_id)
