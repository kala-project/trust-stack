"""Typed, append-only author tool receipts and complete research snapshots.

The JSON codec in this module is the explicit boundary for heterogeneous tool payloads.
It validates JSON shape and finiteness; it does not claim that every tool payload has a
separate domain type.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import sqlite3
from typing import TypeAlias


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]

CONTRACT_VERSION = "research-records/1"
RECEIPT_VERSION = "author-tool-receipt/1"
SNAPSHOT_VERSION = "research-snapshot/1"
MAX_JSON_CHARS = 1_000_000


class ResearchRecordError(ValueError):
    """A formal author record is missing, malformed, out of scope, or modified."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class FrozenArray:
    values: tuple["FrozenJson", ...]

    def __post_init__(self) -> None:
        if type(self.values) is not tuple:
            raise ResearchRecordError("invalid_record_json", "FrozenArray values must be a tuple")
        for value in self.values:
            _validate_frozen(value)


@dataclass(frozen=True, slots=True)
class FrozenObject:
    items: tuple[tuple[str, "FrozenJson"], ...]

    def __post_init__(self) -> None:
        if type(self.items) is not tuple:
            raise ResearchRecordError("invalid_record_json", "FrozenObject items must be a tuple")
        previous: str | None = None
        for pair in self.items:
            if type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str:
                raise ResearchRecordError(
                    "invalid_record_json", "FrozenObject entries must be string-keyed pairs")
            key, value = pair
            if previous is not None and key <= previous:
                raise ResearchRecordError(
                    "invalid_record_json", "FrozenObject keys must be unique and canonically ordered")
            _validate_frozen(value)
            previous = key


FrozenJson: TypeAlias = JsonScalar | FrozenArray | FrozenObject


@dataclass(frozen=True, slots=True)
class ResearchIdentity:
    run_id: str
    conversation_id: str
    company_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _identifier(self.run_id, "run_id")
        _identifier(self.conversation_id, "conversation_id")
        _scope_raw(self.company_ids)


@dataclass(frozen=True, slots=True)
class AuthorToolReceipt:
    run_id: str
    revision: int
    call_id: str
    tool_name: str
    arguments: FrozenObject
    result: FrozenObject
    receipt_sha256: str
    created_at: str
    record_version: str = RECEIPT_VERSION

    def __post_init__(self) -> None:
        _identifier(self.run_id, "run_id")
        _revision(self.revision)
        _identifier(self.call_id, "call_id")
        _identifier(self.tool_name, "tool_name")
        if type(self.arguments) is not FrozenObject or type(self.result) is not FrozenObject:
            raise ResearchRecordError(
                "invalid_record_json", "Receipt arguments and result must be frozen objects")
        _timestamp(self.created_at)
        if self.record_version != RECEIPT_VERSION:
            raise ResearchRecordError(
                "research_record_unsupported", "Tool receipt version is unsupported")
        arguments_raw = canonical_json(thaw_object(self.arguments), "arguments")
        result_raw = canonical_json(thaw_object(self.result), "result")
        envelope = canonical_json({
            "record_version": RECEIPT_VERSION,
            "run_id": self.run_id,
            "revision": self.revision,
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "arguments_sha256": _digest(arguments_raw),
            "result_sha256": _digest(result_raw),
        }, "receipt")
        _matching_digest(self.receipt_sha256, envelope, "receipt")

    def arguments_object(self) -> JsonObject:
        return thaw_object(self.arguments)

    def result_object(self) -> JsonObject:
        return thaw_object(self.result)


@dataclass(frozen=True, slots=True)
class ResearchSnapshot:
    run_id: str
    revision: int
    snapshot: FrozenObject
    snapshot_sha256: str
    created_at: str
    record_version: str = SNAPSHOT_VERSION

    def __post_init__(self) -> None:
        _identifier(self.run_id, "run_id")
        _revision(self.revision)
        if type(self.snapshot) is not FrozenObject:
            raise ResearchRecordError("invalid_record_json", "Snapshot must be a frozen object")
        _timestamp(self.created_at)
        if self.record_version != SNAPSHOT_VERSION:
            raise ResearchRecordError(
                "research_record_unsupported", "Snapshot version is unsupported")
        raw = canonical_json(thaw_object(self.snapshot), "snapshot")
        _matching_digest(self.snapshot_sha256, raw, "snapshot")

    def snapshot_object(self) -> JsonObject:
        return thaw_object(self.snapshot)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_record_contracts(
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
 version TEXT NOT NULL,
 conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
 scope_json TEXT NOT NULL,
 scope_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS author_tool_receipts(
 run_id TEXT NOT NULL REFERENCES research_record_contracts(run_id),
 revision INTEGER NOT NULL CHECK(revision >= 1),
 call_id TEXT NOT NULL,
 tool_name TEXT NOT NULL,
 arguments_json TEXT NOT NULL,
 result_json TEXT NOT NULL,
 receipt_sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(run_id,revision),
 UNIQUE(run_id,call_id)
);
CREATE TABLE IF NOT EXISTS research_snapshots(
 run_id TEXT NOT NULL REFERENCES research_record_contracts(run_id),
 revision INTEGER NOT NULL CHECK(revision >= 1),
 snapshot_json TEXT NOT NULL,
 snapshot_sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(run_id,revision)
);
"""


def _validate_frozen(value: object) -> None:
    if value is None or type(value) in {bool, int, str}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ResearchRecordError("invalid_record_json", "Frozen JSON numbers must be finite")
        return
    if type(value) in {FrozenArray, FrozenObject}:
        return  # Construction already recursively validated these immutable containers.
    raise ResearchRecordError("invalid_record_json", "Frozen JSON contains a non-JSON value")


def _freeze(value: object, path: str = "$") -> FrozenJson:
    if value is None:
        return None
    if type(value) is bool:
        return bool(value)
    if type(value) is int:
        return int(value)
    if type(value) is str:
        return str(value)
    if type(value) is float:
        number = float(value)
        if not math.isfinite(number):
            raise ResearchRecordError("invalid_record_json", f"{path} must be finite")
        return number
    if type(value) is list:
        sequence = list(value)
        return FrozenArray(tuple(_freeze(item, f"{path}[]") for item in sequence))
    if type(value) is dict:
        mapping = dict(value)
        items: list[tuple[str, FrozenJson]] = []
        for key, item in mapping.items():
            if type(key) is not str:
                raise ResearchRecordError("invalid_record_json", f"{path} has a non-string key")
            items.append((key, _freeze(item, f"{path}.{key}")))
        items.sort(key=lambda pair: pair[0])
        return FrozenObject(tuple(items))
    raise ResearchRecordError("invalid_record_json", f"{path} is not a JSON value")


def freeze_object(value: object, field: str) -> FrozenObject:
    frozen = _freeze(value, field)
    if not isinstance(frozen, FrozenObject):
        raise ResearchRecordError("invalid_record_json", f"{field} must be a JSON object")
    return frozen


def _thaw(value: FrozenJson) -> JsonValue:
    if isinstance(value, FrozenObject):
        return {key: _thaw(item) for key, item in value.items}
    if isinstance(value, FrozenArray):
        return [_thaw(item) for item in value.values]
    return value


def thaw_object(value: FrozenObject) -> JsonObject:
    return {key: _thaw(item) for key, item in value.items}


def canonical_json(value: object, field: str) -> str:
    frozen = freeze_object(value, field)
    raw = json.dumps(
        thaw_object(frozen), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
    if len(raw) > MAX_JSON_CHARS:
        raise ResearchRecordError("record_too_large", f"{field} exceeds the formal record limit")
    return raw


def _digest(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _identifier(value: object, field: str) -> str:
    if type(value) is not str or not value or len(value) > 300:
        raise ResearchRecordError(
            "invalid_record_identity", f"{field} must be a bounded nonempty string")
    return value


def _revision(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ResearchRecordError(
            "record_integrity_mismatch", "Record revision must be a positive integer")
    return value


def _timestamp(value: object) -> str:
    timestamp = _identifier(value, "created_at")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResearchRecordError(
            "invalid_record_identity", "created_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ResearchRecordError(
            "invalid_record_identity", "created_at must identify a UTC instant")
    return timestamp


def _matching_digest(value: object, raw: str, field: str) -> str:
    if (type(value) is not str or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            or value != _digest(raw)):
        raise ResearchRecordError(
            "record_integrity_mismatch", f"{field} checksum differs")
    return value


def _scope_raw(company_ids: object) -> str:
    if type(company_ids) is not tuple or not company_ids:
        raise ResearchRecordError(
            "invalid_record_scope", "Run scope must be a nonempty tuple of company identifiers")
    checked: list[str] = []
    for item in company_ids:
        try:
            checked.append(_identifier(item, "company_id"))
        except ResearchRecordError as exc:
            raise ResearchRecordError(
                "invalid_record_scope", "Run scope contains an invalid company identifier") from exc
    if len(set(checked)) != len(checked):
        raise ResearchRecordError(
            "invalid_record_scope", "Run scope company identifiers must be distinct")
    return canonical_json({"company_ids": sorted(checked)}, "scope")


def _decode_object(raw: object, digest: object, field: str) -> FrozenObject:
    if type(raw) is not str or type(digest) is not str or _digest(raw) != digest:
        raise ResearchRecordError(
            "record_integrity_mismatch", f"Stored {field} checksum differs")
    try:
        value: object = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ResearchRecordError(
            "record_integrity_mismatch", f"Stored {field} is invalid") from exc
    if canonical_json(value, field) != raw:
        raise ResearchRecordError(
            "record_integrity_mismatch", f"Stored {field} is not canonical")
    return freeze_object(value, field)


def _row(value: object, field: str) -> sqlite3.Row:
    if not isinstance(value, sqlite3.Row):
        raise ResearchRecordError("record_integrity_mismatch", f"Stored {field} row has an invalid shape")
    return value


def _text(value: object, field: str) -> str:
    if type(value) is not str:
        raise ResearchRecordError("record_integrity_mismatch", f"Stored {field} is not text")
    return value


def _next_revision(connection: sqlite3.Connection, table: str, run_id: str) -> int:
    fetched: object = connection.execute(
        f"SELECT coalesce(max(revision),0)+1 FROM {table} WHERE run_id=?", (run_id,)
    ).fetchone()
    row = _row(fetched, table)
    value: object = row[0]
    return _revision(value)


class ResearchRecordStore:
    """Strict store; callers supply an existing transaction for every append."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]):
        self._connect = connect
        with closing(connect()) as connection, connection:
            connection.executescript(_SCHEMA)
            for table in ("research_record_contracts", "author_tool_receipts", "research_snapshots"):
                for action in ("UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} "
                        f"BEFORE {action} ON {table} BEGIN "
                        "SELECT RAISE(ABORT,'research records are immutable'); END")

    @staticmethod
    def begin_run(connection: sqlite3.Connection, run_id: str, conversation_id: str,
                  company_ids: tuple[str, ...]) -> None:
        checked_run_id = _identifier(run_id, "run_id")
        checked_conversation_id = _identifier(conversation_id, "conversation_id")
        scope_raw = _scope_raw(company_ids)
        connection.execute("INSERT INTO research_record_contracts VALUES(?,?,?,?,?)", (
            checked_run_id, CONTRACT_VERSION, checked_conversation_id,
            scope_raw, _digest(scope_raw),
        ))

    @staticmethod
    def _contract(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        fetched: object = connection.execute(
            "SELECT * FROM research_record_contracts WHERE run_id=?", (run_id,)
        ).fetchone()
        if fetched is None:
            raise ResearchRecordError(
                "research_record_missing", "Run has no formal research record contract")
        row = _row(fetched, "research contract")
        if row["version"] != CONTRACT_VERSION:
            raise ResearchRecordError(
                "research_record_unsupported", "Research record contract is unsupported")
        scope = _decode_object(row["scope_json"], row["scope_sha256"], "scope")
        scope_value = thaw_object(scope)
        company_ids = scope_value.get("company_ids")
        if type(company_ids) is not list:
            raise ResearchRecordError(
                "record_integrity_mismatch", "Stored research scope has an invalid shape")
        expected_scope = _scope_raw(tuple(company_ids))
        if expected_scope != row["scope_json"] or type(row["conversation_id"]) is not str:
            raise ResearchRecordError(
                "record_integrity_mismatch", "Stored research scope differs from its contract")
        return row

    @classmethod
    def append_receipt(cls, connection: sqlite3.Connection, *, run_id: str, call_id: str,
                       tool_name: str, arguments: object, result: object,
                       created_at: str) -> AuthorToolReceipt:
        contract = cls._contract(connection, run_id)
        checked_call_id = _identifier(call_id, "call_id")
        checked_tool_name = _identifier(tool_name, "tool_name")
        checked_created_at = _timestamp(created_at)
        state = connection.execute(
            "SELECT status,conversation_id FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if state is None or state["conversation_id"] != contract["conversation_id"]:
            raise ResearchRecordError(
                "invalid_record_scope", "Run ownership differs from its formal contract")
        if state["status"] not in {"queued", "running"}:
            raise ResearchRecordError(
                "inactive_research", "Stopped research cannot append a tool receipt")
        arguments_raw = canonical_json(arguments, "arguments")
        result_raw = canonical_json(result, "result")
        revision = _next_revision(connection, "author_tool_receipts", run_id)
        envelope = canonical_json({
            "record_version": RECEIPT_VERSION, "run_id": run_id, "revision": revision,
            "call_id": checked_call_id, "tool_name": checked_tool_name,
            "arguments_sha256": _digest(arguments_raw), "result_sha256": _digest(result_raw),
        }, "receipt")
        receipt_sha256 = _digest(envelope)
        connection.execute("INSERT INTO author_tool_receipts VALUES(?,?,?,?,?,?,?,?)", (
            run_id, revision, checked_call_id, checked_tool_name, arguments_raw, result_raw,
            receipt_sha256, checked_created_at,
        ))
        return AuthorToolReceipt(
            run_id, revision, checked_call_id, checked_tool_name,
            freeze_object(arguments, "arguments"), freeze_object(result, "result"),
            receipt_sha256, checked_created_at,
        )

    @classmethod
    def append_snapshot(cls, connection: sqlite3.Connection, *, run_id: str,
                        snapshot: object, created_at: str) -> ResearchSnapshot:
        contract = cls._contract(connection, run_id)
        checked_created_at = _timestamp(created_at)
        state = connection.execute(
            "SELECT status,conversation_id FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if state is None or state["conversation_id"] != contract["conversation_id"]:
            raise ResearchRecordError(
                "invalid_record_scope", "Run ownership differs from its formal contract")
        if state["status"] not in {"queued", "running"}:
            raise ResearchRecordError(
                "inactive_research", "Stopped research cannot append a snapshot")
        raw = canonical_json(snapshot, "snapshot")
        revision = _next_revision(connection, "research_snapshots", run_id)
        digest = _digest(raw)
        connection.execute("INSERT INTO research_snapshots VALUES(?,?,?,?,?)", (
            run_id, revision, raw, digest, checked_created_at,
        ))
        return ResearchSnapshot(
            run_id, revision, freeze_object(snapshot, "snapshot"), digest, checked_created_at)

    @classmethod
    def read_snapshot_in(cls, connection: sqlite3.Connection, run_id: str) -> ResearchSnapshot:
        cls._contract(connection, run_id)
        rows = connection.execute(
            "SELECT * FROM research_snapshots WHERE run_id=? ORDER BY revision", (run_id,)
        ).fetchall()
        if not rows:
            raise ResearchRecordError(
                "source_snapshot_unavailable", "Run has no formal research snapshot")
        for expected, candidate in enumerate(rows, 1):
            if candidate["revision"] != expected:
                raise ResearchRecordError(
                    "record_integrity_mismatch", "Snapshot revisions are not contiguous")
        row = rows[-1]
        revision = _revision(row["revision"])
        digest = _text(row["snapshot_sha256"], "snapshot checksum")
        frozen = _decode_object(row["snapshot_json"], digest, "snapshot")
        return ResearchSnapshot(
            run_id, revision, frozen, digest, _text(row["created_at"], "created_at"))

    def read_identity(self, run_id: str) -> ResearchIdentity:
        with closing(self._connect()) as connection:
            row = self._contract(connection, run_id)
            scope = thaw_object(_decode_object(row["scope_json"], row["scope_sha256"], "scope"))
            values = scope["company_ids"]
            if not isinstance(values, list):
                raise ResearchRecordError("record_integrity_mismatch", "Invalid research scope")
            company_ids = tuple(_identifier(value, "company_id") for value in values)
            identity = ResearchIdentity(
                run_id, _identifier(row["conversation_id"], "conversation_id"), company_ids)
            state: object = connection.execute(
                "SELECT conversation_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if state is None or _row(state, "run")["conversation_id"] != identity.conversation_id:
                raise ResearchRecordError("invalid_record_scope", "Run ownership differs from its formal contract")
            return identity

    def read_snapshot(self, run_id: str) -> ResearchSnapshot:
        with closing(self._connect()) as connection:
            return self.read_snapshot_in(connection, run_id)

    def read_receipts(self, run_id: str) -> tuple[AuthorToolReceipt, ...]:
        with closing(self._connect()) as connection:
            self._contract(connection, run_id)
            rows = connection.execute(
                "SELECT * FROM author_tool_receipts WHERE run_id=? ORDER BY revision", (run_id,)
            ).fetchall()
            receipts: list[AuthorToolReceipt] = []
            expected = 1
            for row in rows:
                if row["revision"] != expected:
                    raise ResearchRecordError(
                        "record_integrity_mismatch", "Tool receipt revisions are not contiguous")
                arguments_raw = _text(row["arguments_json"], "arguments")
                result_raw = _text(row["result_json"], "result")
                arguments = _decode_object(arguments_raw, _digest(arguments_raw), "arguments")
                result = _decode_object(result_raw, _digest(result_raw), "result")
                receipts.append(AuthorToolReceipt(
                    run_id, expected,
                    _text(row["call_id"], "call_id"), _text(row["tool_name"], "tool_name"),
                    arguments, result, _text(row["receipt_sha256"], "receipt checksum"),
                    _text(row["created_at"], "created_at"),
                ))
                expected += 1
            return tuple(receipts)

    def assert_scope(self, run_id: str, conversation_id: str,
                     company_ids: tuple[str, ...]) -> None:
        with closing(self._connect()) as connection:
            row = self._contract(connection, run_id)
            expected = _scope_raw(company_ids)
            if row["conversation_id"] != conversation_id or row["scope_json"] != expected:
                raise ResearchRecordError(
                    "invalid_record_scope", "Formal research record scope differs")
