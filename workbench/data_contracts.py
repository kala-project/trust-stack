"""Standalone immutable contracts shared across source, data, and research layers.

These records describe identity and location, not semantic truth. They deliberately
contain no storage handles, SQL connections, parser implementations, or HTTP types.
Locator variants are closed: callers must choose the format that matches the source.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal, TypeAlias, TypeGuard, TypedDict


class SourceVersionJSON(TypedDict):
    source_id: str
    version_id: str
    version: int | None
    sha256: str
    byte_length: int
    scope: Literal["public", "private"]


class TextSpanLocatorJSON(TypedDict):
    type: Literal["text_span"]
    start: int
    end: int


class PageTextSpanLocatorJSON(TypedDict):
    type: Literal["page_text_span"]
    page: int
    start: int
    end: int
    page_start: int
    page_end: int
    basis: str


class TableTextSpanLocatorJSON(TypedDict):
    type: Literal["table_text_span"]
    start: int
    end: int
    table_index: int
    row_index: int | None


class TableCellLocatorJSON(TypedDict):
    type: Literal["table_cell"]
    table_index: int
    row_index: int
    column_index: int | None


class SqlRowLocatorJSON(TypedDict):
    type: Literal["sql_row"]
    table: str
    primary_key: list[list[str]]


EvidenceLocatorJSON: TypeAlias = (
    TextSpanLocatorJSON | PageTextSpanLocatorJSON | TableTextSpanLocatorJSON
    | TableCellLocatorJSON | SqlRowLocatorJSON
)
ProcessingStatus: TypeAlias = Literal[
    "raw", "parsed", "parse_failed", "unsupported", "derived"
]


class EvidenceRefJSON(TypedDict):
    source: SourceVersionJSON
    locator: EvidenceLocatorJSON
    processing_status: ProcessingStatus
    parser_version: str | None


def _is_object(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(type(key) is str for key in value)


def _object_with_fields(
    value: object, fields: frozenset[str], contract: str,
) -> dict[str, object]:
    if not _is_object(value) or frozenset(value) != fields:
        raise ValueError(f"invalid {contract} fields")
    return value


def _string(value: object, contract: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"invalid {contract}")
    return value


def _integer(value: object, contract: str, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"invalid {contract}")
    return value


def _nullable_integer(value: object, contract: str, *, minimum: int) -> int | None:
    if value is None:
        return None
    return _integer(value, contract, minimum=minimum)


@dataclass(frozen=True, slots=True)
class SourceVersion:
    """Identity of exact provider bytes within one access-controlled source scope."""

    source_id: str
    version_id: str
    version: int | None
    sha256: str
    byte_length: int
    scope: Literal["public", "private"]

    def __post_init__(self) -> None:
        if (type(self.source_id) is not str or not self.source_id
                or type(self.version_id) is not str or not self.version_id
                or (self.version is not None
                    and (type(self.version) is not int or self.version < 1))
                or type(self.sha256) is not str
                or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None
                or type(self.byte_length) is not int or self.byte_length < 1
                or type(self.scope) is not str
                or self.scope not in {"public", "private"}):
            raise ValueError("invalid source version contract")

    def to_json(self) -> SourceVersionJSON:
        return {
            "source_id": self.source_id,
            "version_id": self.version_id,
            "version": self.version,
            "sha256": self.sha256,
            "byte_length": self.byte_length,
            "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class TextSpanLocator:
    type: Literal["text_span"]
    start: int
    end: int

    def __post_init__(self) -> None:
        if (type(self.type) is not str or self.type != "text_span"
                or type(self.start) is not int
                or type(self.end) is not int or self.start < 0 or self.end < self.start):
            raise ValueError("invalid text span locator")

    def to_json(self) -> TextSpanLocatorJSON:
        return {"type": self.type, "start": self.start, "end": self.end}


@dataclass(frozen=True, slots=True)
class PageTextSpanLocator:
    type: Literal["page_text_span"]
    page: int
    start: int
    end: int
    page_start: int
    page_end: int
    basis: str

    def __post_init__(self) -> None:
        if (type(self.type) is not str or self.type != "page_text_span"
                or type(self.page) is not int or self.page < 1
                or type(self.start) is not int or type(self.end) is not int
                or self.start < 0 or self.end < self.start
                or type(self.page_start) is not int or type(self.page_end) is not int
                or self.page_start < 0 or self.page_end < self.page_start
                or type(self.basis) is not str or not self.basis):
            raise ValueError("invalid page text span locator")

    def to_json(self) -> PageTextSpanLocatorJSON:
        return {
            "type": self.type, "page": self.page, "start": self.start, "end": self.end,
            "page_start": self.page_start, "page_end": self.page_end, "basis": self.basis,
        }


@dataclass(frozen=True, slots=True)
class TableTextSpanLocator:
    type: Literal["table_text_span"]
    start: int
    end: int
    table_index: int
    row_index: int | None = None

    def __post_init__(self) -> None:
        if (type(self.type) is not str or self.type != "table_text_span"
                or type(self.start) is not int
                or type(self.end) is not int or self.start < 0 or self.end < self.start
                or type(self.table_index) is not int or self.table_index < 0
                or (self.row_index is not None
                    and (type(self.row_index) is not int or self.row_index < 0))):
            raise ValueError("invalid table text span locator")

    def to_json(self) -> TableTextSpanLocatorJSON:
        return {
            "type": self.type, "start": self.start, "end": self.end,
            "table_index": self.table_index, "row_index": self.row_index,
        }


@dataclass(frozen=True, slots=True)
class TableCellLocator:
    type: Literal["table_cell"]
    table_index: int
    row_index: int
    column_index: int | None = None

    def __post_init__(self) -> None:
        if (type(self.type) is not str or self.type != "table_cell"
                or type(self.table_index) is not int
                or type(self.row_index) is not int or self.table_index < 0 or self.row_index < 0
                or (self.column_index is not None
                    and (type(self.column_index) is not int or self.column_index < 0))):
            raise ValueError("invalid table cell locator")

    def to_json(self) -> TableCellLocatorJSON:
        return {
            "type": self.type, "table_index": self.table_index,
            "row_index": self.row_index, "column_index": self.column_index,
        }


@dataclass(frozen=True, slots=True)
class SqlRowLocator:
    type: Literal["sql_row"]
    table: str
    primary_key: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if (type(self.type) is not str or self.type != "sql_row"
                or type(self.table) is not str or not self.table
                or type(self.primary_key) is not tuple or not self.primary_key
                or any(type(item) is not tuple or len(item) != 2
                       or any(type(value) is not str or not value for value in item)
                       for item in self.primary_key)):
            raise ValueError("invalid SQL row locator")

    def to_json(self) -> SqlRowLocatorJSON:
        return {
            "type": self.type,
            "table": self.table,
            "primary_key": [[name, value] for name, value in self.primary_key],
        }


EvidenceLocator: TypeAlias = (
    TextSpanLocator | PageTextSpanLocator | TableTextSpanLocator
    | TableCellLocator | SqlRowLocator
)


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Typed evidence linkage; processing status is not an approval claim."""

    source: SourceVersion
    locator: EvidenceLocator
    processing_status: ProcessingStatus
    parser_version: str | None = None

    def __post_init__(self) -> None:
        if type(self.source) is not SourceVersion:
            raise ValueError("invalid evidence source")
        if type(self.locator) not in {
            TextSpanLocator, PageTextSpanLocator, TableTextSpanLocator,
            TableCellLocator, SqlRowLocator,
        }:
            raise ValueError("invalid evidence locator")
        if type(self.processing_status) is not str or self.processing_status not in {
                "raw", "parsed", "parse_failed", "unsupported", "derived"}:
            raise ValueError("invalid evidence processing status")
        if self.parser_version is not None and (
                type(self.parser_version) is not str or not self.parser_version):
            raise ValueError("invalid evidence parser version")

    def to_json(self) -> EvidenceRefJSON:
        locator: EvidenceLocatorJSON
        if isinstance(self.locator, TextSpanLocator):
            locator = self.locator.to_json()
        elif isinstance(self.locator, PageTextSpanLocator):
            locator = self.locator.to_json()
        elif isinstance(self.locator, TableTextSpanLocator):
            locator = self.locator.to_json()
        elif isinstance(self.locator, TableCellLocator):
            locator = self.locator.to_json()
        else:
            locator = self.locator.to_json()
        return {
            "source": self.source.to_json(),
            "locator": locator,
            "processing_status": self.processing_status,
            "parser_version": self.parser_version,
        }


def source_version_from_json(value: object) -> SourceVersion:
    item = _object_with_fields(value, frozenset({
        "source_id", "version_id", "version", "sha256", "byte_length", "scope",
    }), "source version")
    scope_value = item["scope"]
    if type(scope_value) is not str:
        raise ValueError("invalid source scope")
    if scope_value == "public":
        scope: Literal["public", "private"] = "public"
    elif scope_value == "private":
        scope = "private"
    else:
        raise ValueError("invalid source scope")
    return SourceVersion(
        _string(item["source_id"], "source id"),
        _string(item["version_id"], "version id"),
        _nullable_integer(item["version"], "source version", minimum=1),
        _string(item["sha256"], "source sha256"),
        _integer(item["byte_length"], "source byte length", minimum=1),
        scope,
    )


def _string_pairs(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("invalid SQL row primary key")
    pairs: list[tuple[str, str]] = []
    for pair in value:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("invalid SQL row primary key")
        pairs.append((
            _string(pair[0], "SQL row primary key name"),
            _string(pair[1], "SQL row primary key value"),
        ))
    return tuple(pairs)


def evidence_locator_from_json(value: object) -> EvidenceLocator:
    if not _is_object(value):
        raise ValueError("invalid evidence locator")
    locator_type = value.get("type")
    if type(locator_type) is not str:
        raise ValueError("invalid evidence locator type")
    if locator_type == "text_span":
        item = _object_with_fields(
            value, frozenset({"type", "start", "end"}), "text span locator")
        return TextSpanLocator(
            "text_span", _integer(item["start"], "text span start", minimum=0),
            _integer(item["end"], "text span end", minimum=0))
    if locator_type == "page_text_span":
        item = _object_with_fields(value, frozenset({
            "type", "page", "start", "end", "page_start", "page_end", "basis",
        }), "page text span locator")
        return PageTextSpanLocator(
            "page_text_span", _integer(item["page"], "page", minimum=1),
            _integer(item["start"], "page text span start", minimum=0),
            _integer(item["end"], "page text span end", minimum=0),
            _integer(item["page_start"], "page start", minimum=0),
            _integer(item["page_end"], "page end", minimum=0),
            _string(item["basis"], "page locator basis"))
    if locator_type == "table_text_span":
        item = _object_with_fields(value, frozenset({
            "type", "start", "end", "table_index", "row_index",
        }), "table text span locator")
        return TableTextSpanLocator(
            "table_text_span", _integer(item["start"], "table text span start", minimum=0),
            _integer(item["end"], "table text span end", minimum=0),
            _integer(item["table_index"], "table index", minimum=0),
            _nullable_integer(item["row_index"], "row index", minimum=0))
    if locator_type == "table_cell":
        item = _object_with_fields(value, frozenset({
            "type", "table_index", "row_index", "column_index",
        }), "table cell locator")
        return TableCellLocator(
            "table_cell", _integer(item["table_index"], "table index", minimum=0),
            _integer(item["row_index"], "row index", minimum=0),
            _nullable_integer(item["column_index"], "column index", minimum=0))
    if locator_type == "sql_row":
        item = _object_with_fields(
            value, frozenset({"type", "table", "primary_key"}), "SQL row locator")
        return SqlRowLocator(
            "sql_row", _string(item["table"], "SQL table"),
            _string_pairs(item["primary_key"]))
    raise ValueError("invalid evidence locator type")


def evidence_ref_from_json(value: object) -> EvidenceRef:
    item = _object_with_fields(value, frozenset({
        "source", "locator", "processing_status", "parser_version",
    }), "evidence reference")
    status_value = item["processing_status"]
    if type(status_value) is not str:
        raise ValueError("invalid evidence processing status")
    if status_value == "raw":
        status: ProcessingStatus = "raw"
    elif status_value == "parsed":
        status = "parsed"
    elif status_value == "parse_failed":
        status = "parse_failed"
    elif status_value == "unsupported":
        status = "unsupported"
    elif status_value == "derived":
        status = "derived"
    else:
        raise ValueError("invalid evidence processing status")
    parser_value = item["parser_version"]
    parser_version = None if parser_value is None else _string(
        parser_value, "evidence parser version")
    return EvidenceRef(
        source_version_from_json(item["source"]),
        evidence_locator_from_json(item["locator"]),
        status,
        parser_version,
    )
