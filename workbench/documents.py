"""Bounded local document enrollment and full-text retrieval.

Raw versions are append-only and content-addressed. Parsed text and its FTS index
are derived data; callers never receive filesystem paths.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from typing import Any, Iterable

from workbench.parser import PARSER_VERSION, ParsedDocument, ParsedTable, ParseError, parse_html
from workbench.pdf_table_candidates import (
    CandidateExtractionError,
    CandidateParseError,
    build_replay_manifest,
    extract_bbox_layout_page,
    parse_bbox_table_candidate,
)
from workbench.data_contracts import EvidenceRef, PageTextSpanLocator, SourceVersion, TextSpanLocator
from workbench.raw_storage import ObjectStore
from workbench.source_registry import SourceRegistry


MAX_BYTES = 20 * 1024 * 1024
MAX_QUERY_CHARS = 2_000
MAX_CONTEXT_CHARS = 10_000
MAX_EXACT_QUOTE_CHARS = 1_200
MAX_CONTEXT_PASSAGES = 16
CHUNK_CHARS = 1_200
CHUNK_OVERLAP = 200
SCHEMA_VERSION = "document-service/1.1.0"
LEGACY_SCHEMA_VERSION = "document-service/1.0.0"
APPLICATION_ID = 0x444F4353  # DOCS
PARSER_UNSUPPORTED_PDF = "unsupported_pdf"
PDF_PARSER_PREFIX = "pdf-pdftotext-text/1"
PDF_TABLE_PARSER_PREFIX = "pdf-pdftotext-text+bbox-tables/1"
PDF_TABLE_PROFILE_VERSION = "pdf-table-profile/1"
PDF_MAX_TEXT_BYTES = 2_000_000
PDF_TIMEOUT_SECONDS = 8
PDF_CPU_SECONDS = 4
PDF_MEMORY_BYTES = 512 * 1024 * 1024
_COMPANY_RE = re.compile(r"[A-Z0-9][A-Z0-9._:-]{0,99}\Z")
_MEDIA_TYPES = {
    "text/html": "text/html",
    "application/xhtml+xml": "text/html",
    "application/pdf": "application/pdf",
}
_ALIASES = {
    "回购": ("回购", "repurchased", "repurchases", "buyback"),
    "buyback": ("buyback", "repurchased", "repurchases", "回购"),
    "repurchased": ("repurchased", "repurchases", "buyback", "回购"),
    "repurchases": ("repurchases", "repurchased", "buyback", "回购"),
    "分红": ("分红", "dividend", "dividends", "capital return", "capital returns"),
    "dividend": ("dividend", "dividends", "分红", "capital return", "capital returns"),
    "dividends": ("dividends", "dividend", "分红", "capital return", "capital returns"),
}

_SCHEMA_V1 = """
PRAGMA foreign_keys=ON;
CREATE TABLE catalog_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE document_versions (
    document_version_id TEXT PRIMARY KEY,
    company_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    source_version INTEGER NOT NULL CHECK(source_version > 0),
    supersedes_document_version_id TEXT,
    source_sha256 TEXT NOT NULL CHECK(length(source_sha256) = 64),
    byte_length INTEGER NOT NULL CHECK(byte_length >= 0),
    title TEXT NOT NULL,
    source_url TEXT,
    published_at TEXT,
    media_type TEXT NOT NULL CHECK(media_type IN ({media_types})),
    rights_status TEXT NOT NULL,
    collected_at TEXT,
    registered_at TEXT NOT NULL,
    parse_status TEXT NOT NULL CHECK(parse_status IN ('parsed','parse_failed','unsupported_pdf')),
    parse_error TEXT,
    UNIQUE(company_id, source_key, source_version),
    FOREIGN KEY(supersedes_document_version_id) REFERENCES document_versions(document_version_id)
);
CREATE INDEX document_versions_source_idx
    ON document_versions(company_id, source_key, source_version DESC);
CREATE TABLE parsed_documents (
    document_version_id TEXT PRIMARY KEY,
    parser_version TEXT NOT NULL,
    text TEXT NOT NULL,
    FOREIGN KEY(document_version_id) REFERENCES document_versions(document_version_id) ON DELETE CASCADE
);
CREATE TABLE parsed_tables (
    document_version_id TEXT NOT NULL,
    table_index INTEGER NOT NULL,
    start_char INTEGER NOT NULL,
    end_char INTEGER NOT NULL,
    rows_json TEXT NOT NULL,
    row_spans_json TEXT NOT NULL,
    PRIMARY KEY(document_version_id, table_index),
    FOREIGN KEY(document_version_id) REFERENCES parsed_documents(document_version_id) ON DELETE CASCADE
);
CREATE TABLE passages (
    passage_id TEXT PRIMARY KEY,
    document_version_id TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    start_char INTEGER NOT NULL,
    end_char INTEGER NOT NULL,
    quote TEXT NOT NULL,
    FOREIGN KEY(document_version_id) REFERENCES parsed_documents(document_version_id) ON DELETE CASCADE
);
CREATE INDEX passages_document_idx ON passages(document_version_id, start_char);
CREATE TRIGGER document_versions_no_update BEFORE UPDATE ON document_versions
BEGIN SELECT RAISE(ABORT, 'document versions are append-only'); END;
CREATE TRIGGER document_versions_no_delete BEFORE DELETE ON document_versions
BEGIN SELECT RAISE(ABORT, 'document versions are append-only'); END;
"""

_SCHEMA_ADDITIONS_V2 = """
CREATE TABLE parsed_document_identities (
    document_version_id TEXT PRIMARY KEY,
    identity_sha256 TEXT NOT NULL CHECK(length(identity_sha256) = 64),
    identity_json TEXT NOT NULL,
    FOREIGN KEY(document_version_id) REFERENCES parsed_documents(document_version_id) ON DELETE CASCADE
);
CREATE TABLE parsed_table_provenance (
    document_version_id TEXT NOT NULL,
    table_index INTEGER NOT NULL,
    provenance_json TEXT NOT NULL,
    PRIMARY KEY(document_version_id, table_index),
    FOREIGN KEY(document_version_id, table_index)
        REFERENCES parsed_tables(document_version_id, table_index) ON DELETE CASCADE
);
"""
_SCHEMA = _SCHEMA_V1 + _SCHEMA_ADDITIONS_V2

_REQUIRED_TABLES = {
    "catalog_meta", "document_versions", "parsed_documents", "parsed_tables", "passages",
    "parsed_document_identities", "parsed_table_provenance",
}
_LEGACY_REQUIRED_TABLES = _REQUIRED_TABLES - {
    "parsed_document_identities", "parsed_table_provenance",
}


class DocumentError(ValueError):
    """Stable boundary error for CLI/tool adapters."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value: Any, name: str, maximum: int, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise DocumentError("invalid_arguments", f"{name} must be a string")
    value = value.strip()
    if not value or len(value) > maximum or "\x00" in value:
        raise DocumentError("invalid_arguments", f"{name} must contain 1..{maximum} safe characters")
    return value


def _company(value: Any) -> str:
    if not isinstance(value, str) or _COMPANY_RE.fullmatch(value) is None:
        raise DocumentError(
            "invalid_company_id",
            "company_id must be an uppercase identifier using A-Z, digits, dot, underscore, colon, or hyphen",
        )
    return value


def _chunks(text: str) -> Iterable[tuple[int, int]]:
    if not text:
        return
    ranges: list[tuple[int, int]] = []
    page_start = 0
    for index, char in enumerate(text):
        if char == "\f":
            ranges.append((page_start, index))
            page_start = index + 1
    ranges.append((page_start, len(text)))
    for lower, upper in ranges:
        start = lower
        while start < upper:
            end = min(upper, start + CHUNK_CHARS)
            if end > start:
                yield start, end
            if end == upper:
                break
            start = max(lower, end - CHUNK_OVERLAP)


def _passage_id(version_id: str, parser_version: str, start: int, end: int) -> str:
    value = f"{version_id}\0{parser_version}\0{start}\0{end}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _alias_terms(text: str) -> list[str]:
    lowered = text.casefold()
    terms: list[str] = []
    for key, aliases in _ALIASES.items():
        if key.casefold() in lowered:
            terms.extend(aliases)
    return list(dict.fromkeys(terms))


def _fts_source(text: str) -> str:
    aliases = _alias_terms(text)
    return text if not aliases else text + "\n" + " ".join(aliases)


def _expanded_query(query: str) -> tuple[str, list[str]]:
    # Supplying only quoted phrases makes FTS operators punctuation, never syntax.
    words = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
    if not words:
        raise DocumentError("invalid_arguments", "query must contain searchable letters or numbers")
    terms = list(words)
    lowered = query.casefold()
    for key, aliases in _ALIASES.items():
        if key.casefold() in lowered:
            terms.extend(aliases)
    terms = list(dict.fromkeys(term for term in terms if term))
    expression = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
    return expression, terms


def _tool_sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _pdf_page_locator(text: str, start: int, end: int) -> dict[str, Any] | None:
    if "\f" not in text:
        return None
    page = text.count("\f", 0, start) + 1
    page_start = text.rfind("\f", 0, start) + 1
    next_break = text.find("\f", start)
    page_end = len(text) if next_break == -1 else next_break
    return {
        "page": page,
        "page_start": start - page_start,
        "page_end": min(end, page_end) - page_start,
        "basis": "pdftotext form-feed page boundary",
    }


def _limit_pdf_process() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (PDF_CPU_SECONDS, PDF_CPU_SECONDS + 1))
    resource.setrlimit(resource.RLIMIT_AS, (PDF_MEMORY_BYTES, PDF_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (PDF_MAX_TEXT_BYTES + 4096, PDF_MAX_TEXT_BYTES + 4096))


def _parse_pdf_text(raw: bytes) -> ParsedDocument:
    pdftotext = shutil.which("pdftotext")
    bwrap = shutil.which("bwrap")
    if not pdftotext or not bwrap:
        raise ParseError("PDF parsing requires approved system pdftotext and bubblewrap")
    if not pdftotext.startswith("/usr/bin/") or not bwrap.startswith("/usr/bin/"):
        raise ParseError("PDF parser paths must be approved system binaries")
    parser_version = (
        f"{PDF_PARSER_PREFIX};pdftotext={Path(pdftotext).name}:"
        f"{_tool_sha256(pdftotext)[:16]};bwrap={Path(bwrap).name}:{_tool_sha256(bwrap)[:16]}"
    )
    with tempfile.TemporaryDirectory(prefix="pdf-parse-") as directory:
        root = Path(directory)
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir(mode=0o700)
        output_dir.mkdir(mode=0o700)
        (input_dir / "document.pdf").write_bytes(raw)
        command = [
            bwrap, "--unshare-all", "--die-with-parent",
            "--ro-bind", "/usr", "/usr",
            "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64",
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
        ]
        for host, guest in (("/etc/fonts", "/etc/fonts"), ("/var/cache/fontconfig", "/var/cache/fontconfig")):
            if Path(host).exists():
                command.extend(["--ro-bind", host, guest])
        command.extend([
            "--ro-bind", str(input_dir), "/input",
            "--bind", str(output_dir), "/output",
            "--",
            pdftotext, "-enc", "UTF-8", "-layout",
            "/input/document.pdf", "/output/document.txt",
        ])
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=PDF_TIMEOUT_SECONDS,
                check=False, preexec_fn=_limit_pdf_process,
            )
        except subprocess.TimeoutExpired as exc:
            raise ParseError("PDF text extraction exceeded the time limit") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "pdftotext failed").strip().splitlines()
            raise ParseError("PDF text extraction failed: " + (detail[0][:300] if detail else "unknown error"))
        output = output_dir / "document.txt"
        try:
            if output.stat().st_size > PDF_MAX_TEXT_BYTES:
                raise ParseError("PDF extracted text exceeds the 2-million-byte limit")
            text = output.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise ParseError("PDF text extraction did not produce UTF-8") from exc
        except OSError as exc:
            raise ParseError("PDF text extraction did not produce readable output") from exc
    if "\x00" in text:
        raise ParseError("PDF extracted text contains NUL bytes")
    if not text.strip():
        raise ParseError("PDF has no extractable text; OCR is not supported")
    return ParsedDocument(text=text, tables=[], parser_version=parser_version)


def _pdf_table_profile(value: Any) -> dict[str, Any]:
    """Validate and detach one explicit, non-semantic PDF table parse profile."""
    if not isinstance(value, dict) or set(value) != {"profile_version", "tables"}:
        raise DocumentError(
            "invalid_pdf_table_profile",
            "pdf_table_profile must contain only profile_version and tables",
        )
    if value.get("profile_version") != PDF_TABLE_PROFILE_VERSION:
        raise DocumentError(
            "invalid_pdf_table_profile",
            f"profile_version must be {PDF_TABLE_PROFILE_VERSION}",
        )
    tables = value.get("tables")
    if not isinstance(tables, list) or not 1 <= len(tables) <= 20:
        raise DocumentError("invalid_pdf_table_profile", "tables must contain 1..20 table selectors")
    required = {"physical_page", "title", "years", "row_labels"}
    allowed = required | {"unit_line", "fiscal_period_header", "year_column_indices"}
    normalized = []
    for table in tables:
        if not isinstance(table, dict) or not required.issubset(table) or not set(table).issubset(allowed):
            raise DocumentError(
                "invalid_pdf_table_profile",
                "each table selector must contain page, title, years, row labels, and only supported optional selectors",
            )
        normalized.append({key: deepcopy(table[key]) for key in (
            "physical_page", "title", "years", "row_labels", "unit_line",
            "fiscal_period_header", "year_column_indices",
        ) if key in table})
    try:
        detached = json.loads(_json({"profile_version": value["profile_version"], "tables": normalized}))
    except (TypeError, ValueError) as exc:
        raise DocumentError("invalid_pdf_table_profile", "pdf_table_profile must be JSON data") from exc
    return detached


def _pdf_page_span(text: str, physical_page: int) -> tuple[int, int]:
    starts = [0]
    starts.extend(index + 1 for index, char in enumerate(text) if char == "\f")
    if starts[-1] == len(text):
        starts.pop()
    if not isinstance(physical_page, int) or isinstance(physical_page, bool) or physical_page < 1:
        raise ParseError("PDF table profile physical_page must be a positive integer")
    if physical_page > len(starts):
        raise ParseError("PDF table profile selects a page absent from extracted full text")
    start = starts[physical_page - 1]
    boundary = text.find("\f", start)
    return start, len(text) if boundary < 0 else boundary


def _parse_pdf_with_tables(
    raw: bytes, profile_value: dict[str, Any],
) -> tuple[ParsedDocument, dict[str, Any], list[dict[str, Any]]]:
    """Parse full PDF text plus strictly selected bbox tables without approving semantics."""
    profile = _pdf_table_profile(profile_value)
    source_sha256 = hashlib.sha256(raw).hexdigest()
    text_document = _parse_pdf_text(raw)
    tables: list[ParsedTable] = []
    provenances: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    tool_identities: list[str] = []
    try:
        for index, selector in enumerate(profile["tables"]):
            physical_page = selector["physical_page"]
            extraction = extract_bbox_layout_page(raw, physical_page=physical_page)
            if (extraction.get("source_sha256") != source_sha256
                    or extraction.get("physical_page") != physical_page):
                raise CandidateExtractionError("bbox extraction identity does not match source and page")
            tool_identity = extraction.get("tool_identity")
            if not isinstance(tool_identity, str) or not tool_identity:
                raise CandidateExtractionError("bbox extraction omitted its tool identity")
            xml = extraction.get("xml")
            if not isinstance(xml, bytes):
                raise CandidateExtractionError("bbox extraction omitted its XML bytes")
            candidate = parse_bbox_table_candidate(
                xml,
                source_sha256=source_sha256,
                physical_page=physical_page,
                title=selector["title"],
                years=selector["years"],
                row_labels=selector["row_labels"],
                unit_line=selector.get("unit_line"),
                fiscal_period_header=selector.get("fiscal_period_header"),
                year_column_indices=selector.get("year_column_indices"),
            )
            manifest_selectors = {key: deepcopy(selector.get(key)) for key in (
                "title", "years", "row_labels", "unit_line", "fiscal_period_header"
            )}
            if "year_column_indices" in selector:
                manifest_selectors["year_column_indices"] = deepcopy(
                    selector["year_column_indices"]
                )
            manifest = build_replay_manifest(
                extraction=extraction,
                bbox_layout_sha256=hashlib.sha256(xml).hexdigest(),
                candidate=candidate,
                selectors=manifest_selectors,
            )
            start, end = _pdf_page_span(text_document.text, physical_page)
            rows = [["", *selector["years"]]] + [
                [row["label"], *[cell["raw_text"] for cell in row["cells"]]]
                for row in candidate["rows"]
            ]
            row_spans = [(start, end) for _row in rows]
            tables.append(ParsedTable(index, rows, start, end, row_spans))
            provenances.append({
                "capability_status": "formally_parsed",
                "semantic_status": "unapproved",
                "authorization_status": "unchanged",
                "locator_basis": "physical page bbox; text spans cover the containing page",
                "manifest": manifest,
                "coordinate_table": candidate,
            })
            manifests.append(manifest)
            tool_identities.append(tool_identity)
    except (CandidateExtractionError, CandidateParseError, ValueError) as exc:
        raise ParseError(f"PDF table profile could not be reproduced: {exc}") from exc
    identity = {
        "identity_version": "pdf-table-parse-identity/1",
        "raw_pdf_sha256": source_sha256,
        "text_parser_version": text_document.parser_version,
        "profile": profile,
        "bbox_tool_identities": tool_identities,
        "table_manifests": manifests,
    }
    identity_sha256 = hashlib.sha256(_json(identity).encode("utf-8")).hexdigest()
    parsed = ParsedDocument(
        text=text_document.text,
        tables=tables,
        parser_version=f"{PDF_TABLE_PARSER_PREFIX};identity={identity_sha256}",
    )
    return parsed, identity, provenances


class DocumentService:
    """L2 parse/index/query facade over an injected L1 source registry."""

    def __init__(self, root: os.PathLike[str] | str, *, object_store: ObjectStore | None = None,
                 source_registry: SourceRegistry | None = None, source_scope="public"):
        self.source_registry = source_registry or SourceRegistry(
            root, error_type=DocumentError, max_bytes=MAX_BYTES,
            object_store=object_store, scope=source_scope)
        self.root = self.source_registry.root
        self.max_object_bytes = self.source_registry.max_object_bytes
        # Legacy local operator/test surface. Query and research APIs never expose it.
        if self.source_registry.legacy_objects_path is not None:
            self.objects_path = self.source_registry.legacy_objects_path
        self.db_path = self.root / "catalog.sqlite3"
        self._frozen_replay_cache: ContextVar[dict | None] = ContextVar(
            f"frozen_replay_cache_{id(self)}", default=None
        )
        self._initialize()

    @contextmanager
    def frozen_replay_scope(self):
        """Reuse successful frozen parses only inside one bounded caller scope."""
        existing = self._frozen_replay_cache.get()
        if existing is not None:
            yield
            return
        token = self._frozen_replay_cache.set({})
        try:
            yield
        finally:
            self._frozen_replay_cache.reset(token)

    def _connect(self) -> sqlite3.Connection:
        self.source_registry.ensure_storage()
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    # Compatibility aliases for old operator tests and internal replay code only.
    def _ensure_storage_dirs(self) -> None:
        self.source_registry.ensure_storage()

    def _verify_object(self, digest: str, expected_size: int | None = None) -> bytes:
        return self.source_registry.read_digest(digest, expected_size)

    def _object_path(self, digest: str):
        return self.source_registry.legacy_object_path(digest)

    @staticmethod
    def _probe_fts(connection: sqlite3.Connection) -> None:
        try:
            connection.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(value, tokenize='unicode61')")
            connection.execute("DROP TABLE temp.fts5_probe")
        except sqlite3.Error as exc:
            raise DocumentError("fts5_unavailable", "SQLite FTS5 with unicode61 is required") from exc

    @classmethod
    def _install_schema(cls, connection: sqlite3.Connection) -> None:
        connection.executescript(_SCHEMA.format(media_types=cls.catalog_media_types))
        connection.execute(
            "CREATE VIRTUAL TABLE passages_fts USING fts5("
            "passage_id UNINDEXED, content, tokenize='unicode61')"
        )

    @staticmethod
    def _schema_fingerprint(connection: sqlite3.Connection) -> str:
        rows = list(connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ))
        return hashlib.sha256(_json([list(row) for row in rows]).encode("utf-8")).hexdigest()

    @classmethod
    def _expected_schema_fingerprint(cls, schema: str = _SCHEMA) -> str:
        expected = sqlite3.connect(":memory:")
        try:
            expected.executescript(schema.format(media_types=cls.catalog_media_types))
            expected.execute(
                "CREATE VIRTUAL TABLE passages_fts USING fts5("
                "passage_id UNINDEXED, content, tokenize='unicode61')"
            )
            return cls._schema_fingerprint(expected)
        finally:
            expected.close()

    @classmethod
    def _migrate_legacy_schema(cls, connection: sqlite3.Connection) -> None:
        """Add only derived parse-identity tables to one exact v1 catalog."""
        # executescript otherwise commits each DDL statement independently. Keep
        # additions and their schema identity atomic so interruption preserves v1.
        connection.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA_ADDITIONS_V2)
        fingerprint = cls._schema_fingerprint(connection)
        connection.execute(
            "UPDATE catalog_meta SET value=? WHERE key='schema_version'", (SCHEMA_VERSION,)
        )
        connection.execute(
            "UPDATE catalog_meta SET value=? WHERE key='schema_fingerprint'", (fingerprint,)
        )
        connection.execute("PRAGMA user_version=2")
        connection.commit()

    def _initialize(self) -> None:
        existed = self.db_path.exists()
        connection = sqlite3.connect(self.db_path)
        try:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            self._probe_fts(connection)
            if not tables:
                self._install_schema(connection)
                fingerprint = self._schema_fingerprint(connection)
                connection.executemany(
                    "INSERT INTO catalog_meta VALUES(?,?)",
                    (("schema_version", SCHEMA_VERSION), ("schema_fingerprint", fingerprint)),
                )
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute("PRAGMA user_version=2")
                connection.commit()
            else:
                app_id = connection.execute("PRAGMA application_id").fetchone()[0]
                user_version = connection.execute("PRAGMA user_version").fetchone()[0]
                try:
                    metadata = dict(connection.execute("SELECT key,value FROM catalog_meta"))
                except sqlite3.Error as exc:
                    raise DocumentError("unknown_catalog_schema", "existing catalog schema is not recognized") from exc
                fts_tables = {
                    name for name in tables if name == "passages_fts" or name.startswith("passages_fts_")
                }
                stored_fingerprint = metadata.get("schema_fingerprint")
                actual_fingerprint = self._schema_fingerprint(connection)
                legacy_allowed = _LEGACY_REQUIRED_TABLES | fts_tables
                if (
                    app_id == APPLICATION_ID and user_version == 1
                    and metadata.get("schema_version") == LEGACY_SCHEMA_VERSION
                    and stored_fingerprint == actual_fingerprint
                    and stored_fingerprint == self._expected_schema_fingerprint(_SCHEMA_V1)
                    and "passages_fts" in tables and tables == legacy_allowed
                ):
                    self._migrate_legacy_schema(connection)
                    tables = {
                        row[0] for row in connection.execute(
                            "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                        )
                    }
                    metadata = dict(connection.execute("SELECT key,value FROM catalog_meta"))
                    user_version = connection.execute("PRAGMA user_version").fetchone()[0]
                    stored_fingerprint = metadata.get("schema_fingerprint")
                    actual_fingerprint = self._schema_fingerprint(connection)
                    fts_tables = {
                        name for name in tables
                        if name == "passages_fts" or name.startswith("passages_fts_")
                    }
                allowed = _REQUIRED_TABLES | fts_tables
                if (
                    app_id != APPLICATION_ID or user_version != 2
                    or metadata.get("schema_version") != SCHEMA_VERSION
                    or stored_fingerprint != actual_fingerprint
                    or stored_fingerprint != self._expected_schema_fingerprint()
                    or not _REQUIRED_TABLES.issubset(tables)
                    or "passages_fts" not in tables or tables != allowed
                ):
                    raise DocumentError("unknown_catalog_schema", "existing catalog schema is not recognized")
                columns = {row[1] for row in connection.execute("PRAGMA table_info(document_versions)")}
                if columns != {
                    "document_version_id", "company_id", "source_key", "source_version",
                    "supersedes_document_version_id", "source_sha256", "byte_length", "title",
                    "source_url", "published_at", "media_type", "rights_status", "collected_at",
                    "registered_at", "parse_status", "parse_error",
                }:
                    raise DocumentError("unknown_catalog_schema", "existing catalog schema is not recognized")
        except DocumentError:
            raise
        except sqlite3.Error as exc:
            code = "unknown_catalog_schema" if existed else "catalog_unavailable"
            raise DocumentError(code, "document catalog cannot be initialized") from exc
        finally:
            connection.close()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _read_input(path: os.PathLike[str] | str) -> bytes:
        source = Path(path)
        try:
            before = source.lstat()
        except OSError as exc:
            raise DocumentError("source_unavailable", "source file cannot be read") from exc
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise DocumentError("unsafe_source_path", "source must be a regular non-symlink file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(source, flags)
            with os.fdopen(fd, "rb") as handle:
                opened = os.fstat(handle.fileno())
                raw = handle.read(MAX_BYTES + 1)
                after = os.fstat(handle.fileno())
        except OSError as exc:
            raise DocumentError("source_unavailable", "source file cannot be read safely") from exc
        if len(raw) > MAX_BYTES:
            raise DocumentError("source_too_large", "source exceeds the 20 MiB limit")
        if not stat.S_ISREG(opened.st_mode) or (
            opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns
        ) != (
            before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        ) or (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
            raise DocumentError("source_changed", "source changed while it was being read")
        return raw

    @staticmethod
    def _validate_media(raw: bytes, requested: str | None) -> str:
        normalized = None
        if requested is not None:
            if not isinstance(requested, str) or requested.casefold().strip() not in _MEDIA_TYPES:
                raise DocumentError("unsupported_media_type", "media_type must be HTML or PDF")
            normalized = _MEDIA_TYPES[requested.casefold().strip()]
        is_pdf = raw.startswith(b"%PDF-")
        is_html = False
        try:
            # Recognize the standard UTF-8 BOM for detection only. Store/hash
            # the original bytes unchanged; do not repair arbitrary encodings.
            decoded = raw.decode("utf-8-sig")
            is_html = re.match(r"\s*(?:<!doctype\s+html\b|<html\b)", decoded, re.IGNORECASE) is not None
        except UnicodeDecodeError:
            decoded = None
        detected = "application/pdf" if is_pdf else "text/html" if is_html else None
        if detected is None:
            raise DocumentError("invalid_source_type", "source does not have a valid HTML or PDF signature")
        if normalized is not None and normalized != detected:
            raise DocumentError("media_type_mismatch", "declared media_type does not match source signature")
        if detected == "text/html" and decoded is None:
            raise DocumentError("invalid_source_type", "HTML must be strict UTF-8")
        return detected

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "document_version_id": row["document_version_id"],
            "company_id": row["company_id"],
            "source_key": row["source_key"],
            "source_version": row["source_version"],
            "supersedes_document_version_id": row["supersedes_document_version_id"],
            "source_sha256": row["source_sha256"],
            "byte_length": row["byte_length"],
            "title": row["title"],
            "source_url": row["source_url"],
            "published_at": row["published_at"],
            "media_type": row["media_type"],
            "rights_status": row["rights_status"],
            "collected_at": row["collected_at"],
            "registered_at": row["registered_at"],
            "parse_status": row["parse_status"],
            "parse_error": row["parse_error"],
        }

    def register_file(
        self, path, *, company_id, source_key, title, url=None, published_at=None,
        media_type=None, rights_status="local_only", collected_at=None,
        expected_sha256=None, expected_byte_length=None, pdf_table_profile=None,
    ) -> dict[str, Any]:
        return self.register_bytes(
            self._read_input(path), company_id=company_id, source_key=source_key, title=title,
            url=url, published_at=published_at, media_type=media_type, rights_status=rights_status,
            collected_at=collected_at, expected_sha256=expected_sha256,
            expected_byte_length=expected_byte_length, pdf_table_profile=pdf_table_profile)

    def derive_pdf_tables(
        self, *, company_id, base_document_version_id, table_selectors,
    ) -> dict[str, Any]:
        """Append a PDF parse version using only selectors and archived base metadata."""
        company_id = _company(company_id)
        if (not isinstance(base_document_version_id, str)
                or re.fullmatch(r"[0-9a-f]{64}", base_document_version_id) is None):
            raise DocumentError("invalid_arguments", "base_document_version_id is invalid")
        requested = _pdf_table_profile({
            "profile_version": PDF_TABLE_PROFILE_VERSION,
            "tables": table_selectors,
        })
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT v.*,i.identity_sha256,i.identity_json FROM document_versions v "
                "LEFT JOIN parsed_document_identities i USING(document_version_id) "
                "WHERE v.company_id=? AND v.document_version_id=?",
                (company_id, base_document_version_id),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise DocumentError(
                "document_not_found", "base document version does not exist for this company"
            )
        if row["media_type"] != "application/pdf":
            raise DocumentError("invalid_source_type", "PDF tables can be derived only from a PDF version")

        raw = self._verify_object(row["source_sha256"], row["byte_length"])
        parse_identity = None
        identity_value = {
            "company_id": row["company_id"], "source_key": row["source_key"],
            "source_version": row["source_version"], "source_sha256": row["source_sha256"],
            "title": row["title"], "url": row["source_url"],
            "published_at": row["published_at"], "media_type": row["media_type"],
            "rights_status": row["rights_status"], "collected_at": row["collected_at"],
        }
        if row["identity_json"] is not None:
            try:
                parse_identity = json.loads(row["identity_json"])
                identity_sha256 = hashlib.sha256(
                    _json(parse_identity).encode("utf-8")
                ).hexdigest()
            except (TypeError, ValueError) as exc:
                raise DocumentError(
                    "historical_parser_unavailable", "base PDF parse identity is invalid"
                ) from exc
            if identity_sha256 != row["identity_sha256"]:
                raise DocumentError(
                    "historical_parser_unavailable", "base PDF parse identity was changed"
                )
            identity_value["parse_identity_sha256"] = identity_sha256
        if hashlib.sha256(_json(identity_value).encode("utf-8")).hexdigest() != base_document_version_id:
            raise DocumentError("object_tampered", "base document version metadata was changed")

        existing_tables = []
        if parse_identity is not None:
            # Reproduce all existing formal tables before carrying their selectors forward.
            self.read_frozen_version(
                company_id=company_id, document_version_id=base_document_version_id
            )
            try:
                existing_profile = _pdf_table_profile(parse_identity["profile"])
            except (KeyError, TypeError) as exc:
                raise DocumentError(
                    "historical_parser_unavailable", "base PDF table profile is invalid"
                ) from exc
            existing_tables = existing_profile["tables"]

        merged_tables = []
        seen = set()
        for selector in [*existing_tables, *requested["tables"]]:
            selector_key = _json(selector)
            if selector_key not in seen:
                seen.add(selector_key)
                merged_tables.append(selector)
        if len(merged_tables) > 20:
            raise DocumentError(
                "invalid_pdf_table_profile", "combined PDF table selectors exceed the 20-table limit"
            )
        return self.register_bytes(
            raw,
            company_id=company_id,
            source_key=row["source_key"],
            title=row["title"],
            url=row["source_url"],
            published_at=row["published_at"],
            media_type=row["media_type"],
            rights_status=row["rights_status"],
            collected_at=row["collected_at"],
            expected_sha256=row["source_sha256"],
            expected_byte_length=row["byte_length"],
            pdf_table_profile={
                "profile_version": PDF_TABLE_PROFILE_VERSION,
                "tables": merged_tables,
            },
        )

    def register_bytes(
        self, raw: bytes, *, company_id, source_key, title, url=None, published_at=None,
        media_type=None, rights_status="local_only", collected_at=None,
        expected_sha256=None, expected_byte_length=None, pdf_table_profile=None,
    ) -> dict[str, Any]:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
            raise DocumentError("invalid_file", f"Document must contain 1..{MAX_BYTES} bytes")
        media_type = self._validate_media(raw, media_type)
        registration = self.source_registry.prepare(
            raw, company_id=company_id, source_key=source_key, title=title, url=url,
            published_at=published_at, media_type=media_type, rights_status=rights_status,
            collected_at=collected_at, expected_sha256=expected_sha256,
            expected_byte_length=expected_byte_length)
        company_id, source_key = registration.company_id, registration.source_key
        digest = registration.sha256
        title, url = registration.title, registration.url
        published_at, rights_status = registration.published_at, registration.rights_status
        collected_at = registration.collected_at

        parsed = None
        parse_identity = None
        table_provenances: list[dict[str, Any]] = []
        parse_status = PARSER_UNSUPPORTED_PDF if media_type == "application/pdf" else "parse_failed"
        parse_error = None
        if pdf_table_profile is not None and media_type != "application/pdf":
            raise DocumentError(
                "invalid_pdf_table_profile", "pdf_table_profile is accepted only for PDF sources"
            )
        try:
            if pdf_table_profile is None:
                parsed = self._parse_document(raw, media_type)
            else:
                parsed, parse_identity, table_provenances = _parse_pdf_with_tables(
                    raw, pdf_table_profile
                )
            parse_status = "parsed"
        except ParseError as exc:
            if pdf_table_profile is not None:
                # Expose bounded selector diagnostics, not sandbox stderr or filesystem paths.
                # Without this, a researcher cannot distinguish a wrong page from an
                # incomplete header and can only repeat blind extraction attempts.
                reason = (f". Selector mismatch: {str(exc.__cause__)[:500]}"
                          if isinstance(exc.__cause__, CandidateParseError) else "")
                raise DocumentError(
                    "pdf_table_parse_failed",
                    "Explicit PDF table profile could not be reproduced; no document version was appended" + reason,
                ) from exc
            parse_status = "parse_failed"
            parse_error = str(exc)[:2_000]

        metadata = (digest, len(raw), title, url, published_at, media_type, rights_status, collected_at)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            duplicates = connection.execute(
                "SELECT * FROM document_versions WHERE company_id=? AND source_key=? AND "
                "source_sha256=? AND byte_length=? AND title=? AND source_url IS ? AND published_at IS ? "
                "AND media_type=? AND rights_status=? AND collected_at IS ? ORDER BY source_version",
                (company_id, source_key, *metadata),
            ).fetchall()
            if duplicates:
                if parsed is None and pdf_table_profile is None:
                    connection.rollback()
                    return self._record(duplicates[0])
                if parsed is not None:
                    duplicate_ids = [row["document_version_id"] for row in duplicates]
                    placeholders = ",".join("?" for _ in duplicate_ids)
                    existing = connection.execute(
                        f"SELECT v.* FROM document_versions v JOIN parsed_documents d "
                        f"USING(document_version_id) WHERE v.document_version_id IN ({placeholders}) "
                        f"AND d.parser_version=? ORDER BY v.source_version",
                        [*duplicate_ids, parsed.parser_version],
                    ).fetchone()
                    if existing is not None:
                        connection.rollback()
                        return self._record(existing)
            registered_at = _now()
            parse_identity_sha256 = (hashlib.sha256(_json(parse_identity).encode("utf-8")).hexdigest()
                                     if parse_identity is not None else None)
            source = self.source_registry.append(
                connection, registration, parse_status=parse_status, parse_error=parse_error,
                registered_at=registered_at, parse_identity_sha256=parse_identity_sha256)
            version_id = source.version_id
            if parsed is not None:
                connection.execute(
                    "INSERT INTO parsed_documents VALUES(?,?,?)",
                    (version_id, parsed.parser_version, parsed.text),
                )
                if parse_identity is not None:
                    identity_json = _json(parse_identity)
                    connection.execute(
                        "INSERT INTO parsed_document_identities VALUES(?,?,?)",
                        (version_id, hashlib.sha256(identity_json.encode("utf-8")).hexdigest(),
                         identity_json),
                    )
                for table in parsed.tables:
                    connection.execute(
                        "INSERT INTO parsed_tables VALUES(?,?,?,?,?,?)",
                        (version_id, table.index, table.start, table.end, _json(table.rows),
                         _json(table.row_spans)),
                    )
                    if parse_identity is not None:
                        connection.execute(
                            "INSERT INTO parsed_table_provenance VALUES(?,?,?)",
                            (version_id, table.index, _json(table_provenances[table.index])),
                        )
                for start, end in _chunks(parsed.text):
                    passage_id = _passage_id(version_id, parsed.parser_version, start, end)
                    quote = parsed.text[start:end]
                    connection.execute(
                        "INSERT INTO passages VALUES(?,?,?,?,?,?)",
                        (passage_id, version_id, parsed.parser_version, start, end, quote),
                    )
                    connection.execute(
                        "INSERT INTO passages_fts(passage_id,content) VALUES(?,?)",
                        (passage_id, _fts_source(quote)),
                    )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM document_versions WHERE document_version_id=?", (version_id,)
            ).fetchone()
            return self._record(row)
        except sqlite3.Error as exc:
            connection.rollback()
            raise DocumentError("catalog_write_failed", "document registration could not be committed") from exc
        finally:
            connection.close()

    @staticmethod
    def _parse_document(raw, media_type):
        if media_type == "text/html":
            return parse_html(raw)
        if media_type == "application/pdf":
            return _parse_pdf_text(raw)
        raise ParseError("unsupported document representation")

    @staticmethod
    def _parse_frozen_document(raw: bytes, media_type: str, parser_version: str) -> ParsedDocument:
        """Replay bytes only with the implementation named by the persisted identity."""
        if media_type == "text/html" and parser_version == PARSER_VERSION:
            parser = parse_html
        elif (media_type == "application/pdf"
              and parser_version.startswith(PDF_PARSER_PREFIX + ";")):
            parser = _parse_pdf_text
        else:
            raise DocumentError(
                "historical_parser_unsupported",
                "the persisted parser identity is not supported for frozen replay",
            )
        try:
            parsed = parser(raw)
        except (ParseError, OSError) as exc:
            raise DocumentError(
                "historical_parser_unavailable",
                "the persisted parser implementation is unavailable for frozen replay",
            ) from exc
        if parsed.parser_version != parser_version:
            raise DocumentError(
                "historical_parser_unavailable",
                "the available parser implementation does not match the persisted identity",
            )
        return parsed

    def _selection_clause(
        self, connection: sqlite3.Connection, company_id: str,
        document_version_ids: list[str] | tuple[str, ...] | None,
    ) -> tuple[str, list[Any]]:
        if document_version_ids is None:
            return (
                "v.company_id=? AND NOT EXISTS (SELECT 1 FROM document_versions newer "
                "WHERE newer.company_id=v.company_id AND newer.source_key=v.source_key "
                "AND newer.source_version>v.source_version)",
                [company_id],
            )
        if not isinstance(document_version_ids, (list, tuple)) or not document_version_ids:
            raise DocumentError("invalid_arguments", "document_version_ids must be a non-empty list")
        ids = list(dict.fromkeys(document_version_ids))
        if any(not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{64}", item) is None for item in ids):
            raise DocumentError("invalid_arguments", "document_version_ids contains an invalid identifier")
        placeholders = ",".join("?" for _ in ids)
        count = connection.execute(
            f"SELECT count(*) FROM document_versions WHERE company_id=? AND document_version_id IN ({placeholders})",
            [company_id, *ids],
        ).fetchone()[0]
        if count != len(ids):
            raise DocumentError("document_not_found", "a selected document version is absent or belongs to another company")
        return f"v.company_id=? AND v.document_version_id IN ({placeholders})", [company_id, *ids]

    def _verify_rows(self, rows: Iterable[sqlite3.Row]) -> None:
        seen: set[str] = set()
        for row in rows:
            if row["source_sha256"] not in seen:
                self._verify_object(row["source_sha256"], row["byte_length"])
                seen.add(row["source_sha256"])

    @staticmethod
    def _descriptor(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "document_version_id": row["document_version_id"], "source_key": row["source_key"],
            "source_version": row["source_version"], "title": row["title"],
            "source_url": row["source_url"], "source_sha256": row["source_sha256"],
            "published_at": row["published_at"], "collected_at": row["collected_at"],
            "media_type": row["media_type"], "rights_status": row["rights_status"],
            "parse_status": row["parse_status"], "parse_error": row["parse_error"],
            "supersedes_document_version_id": row["supersedes_document_version_id"],
        }

    @staticmethod
    def _table_record(*, parser_version: str, table_index: int, start: int, end: int,
                      rows: list, row_spans: list,
                      provenance: dict[str, Any] | None = None) -> dict[str, Any]:
        table = {
            "parser_version": parser_version, "table_index": table_index,
            "locator": {"start": start, "end": end}, "rows": rows,
            "row_spans": [list(span) for span in row_spans],
        }
        if provenance is not None:
            table["provenance"] = provenance
        table["table_sha256"] = hashlib.sha256(_json(table).encode("utf-8")).hexdigest()
        return table

    @classmethod
    def _table_from_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        return cls._table_record(
            parser_version=row["parser_version"], table_index=row["table_index"],
            start=row["start_char"], end=row["end_char"],
            rows=json.loads(row["rows_json"]), row_spans=json.loads(row["row_spans_json"]),
            provenance=(json.loads(row["provenance_json"])
                        if "provenance_json" in row.keys() and row["provenance_json"] is not None
                        else None),
        )

    def _coverage(self, connection: sqlite3.Connection, clause: str, params: list[Any]) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT v.* FROM document_versions v WHERE " + clause +
            " ORDER BY v.source_key,v.source_version", params,
        ).fetchall()
        self._verify_rows(rows)
        counts = {"enrolled": len(rows), "parsed": 0, "failed": 0, "unsupported": 0}
        for row in rows:
            if row["parse_status"] == "parsed":
                counts["parsed"] += 1
            elif row["parse_status"] == "parse_failed":
                counts["failed"] += 1
            else:
                counts["unsupported"] += 1
        documents = []
        for row in rows:
            descriptor = self._descriptor(row)
            parsed = connection.execute(
                "SELECT parser_version FROM parsed_documents WHERE document_version_id=?",
                (row["document_version_id"],),
            ).fetchone()
            if parsed is not None:
                tables = connection.execute(
                    "SELECT t.*,d.parser_version,p.provenance_json FROM parsed_tables t "
                    "JOIN parsed_documents d USING(document_version_id) "
                    "LEFT JOIN parsed_table_provenance p USING(document_version_id,table_index) "
                    "WHERE t.document_version_id=? ORDER BY t.table_index",
                    (row["document_version_id"],),
                ).fetchall()
                descriptor["parser_version"] = parsed["parser_version"]
                descriptor["tables"] = [{
                    "table_index": item["table_index"],
                    "table_sha256": self._table_from_row(item)["table_sha256"],
                } for item in tables]
            documents.append(descriptor)
        return {**counts, "documents": documents}

    def get_document_coverage(self, *, company_id) -> dict[str, Any]:
        company_id = _company(company_id)
        connection = self._connect()
        try:
            clause, params = self._selection_clause(connection, company_id, None)
            return self._coverage(connection, clause, params)
        finally:
            connection.close()

    @staticmethod
    def _tables(connection: sqlite3.Connection, version_id: str, start: int, end: int) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT t.*,p.provenance_json FROM parsed_tables t "
            "LEFT JOIN parsed_table_provenance p USING(document_version_id,table_index) "
            "WHERE t.document_version_id=? AND t.end_char>? AND t.start_char<? "
            "ORDER BY t.table_index", (version_id, start, end),
        )
        return [{
            "table_index": row["table_index"],
            "locator": {"start": row["start_char"], "end": row["end_char"]},
            "rows": json.loads(row["rows_json"]),
            "row_spans": json.loads(row["row_spans_json"]),
            **({"provenance": json.loads(row["provenance_json"])}
               if row["provenance_json"] is not None else {}),
        } for row in rows]

    def read_table(self, *, company_id, document_version_id, table_index) -> dict[str, Any]:
        """Read one parsed table from an exact, integrity-checked document version."""
        company_id = _company(company_id)
        if (not isinstance(document_version_id, str)
                or re.fullmatch(r"[0-9a-f]{64}", document_version_id) is None):
            raise DocumentError("invalid_arguments", "document_version_id is invalid")
        if type(table_index) is not int or table_index < 0:
            raise DocumentError("invalid_arguments", "table_index must be a non-negative integer")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT v.*,d.parser_version,t.table_index,t.start_char,t.end_char,t.rows_json,t.row_spans_json,"
                "p.provenance_json FROM document_versions v "
                "JOIN parsed_documents d USING(document_version_id) "
                "JOIN parsed_tables t USING(document_version_id) "
                "LEFT JOIN parsed_table_provenance p USING(document_version_id,table_index) "
                "WHERE v.company_id=? AND v.document_version_id=? AND t.table_index=?",
                (company_id, document_version_id, table_index),
            ).fetchone()
            if row is None:
                raise DocumentError("table_not_found", "table does not exist in this document version")
            self._verify_rows([row])
            return {**self._descriptor(row), **self._table_from_row(row)}
        finally:
            connection.close()

    def read_frozen_version(self, *, company_id, document_version_id) -> dict[str, Any]:
        """Reparse integrity-checked archive bytes instead of trusting mutable parsed rows."""
        company_id = _company(company_id)
        if (not isinstance(document_version_id, str)
                or re.fullmatch(r"[0-9a-f]{64}", document_version_id) is None):
            raise DocumentError("invalid_arguments", "document_version_id is invalid")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT v.*,d.parser_version,i.identity_sha256,i.identity_json "
                "FROM document_versions v "
                "LEFT JOIN parsed_documents d USING(document_version_id) "
                "LEFT JOIN parsed_document_identities i USING(document_version_id) "
                "WHERE v.company_id=? AND v.document_version_id=?",
                (company_id, document_version_id),
            ).fetchone()
            if row is None:
                raise DocumentError("document_not_found", "document version does not exist for this company")
            raw = self._verify_object(row["source_sha256"], row["byte_length"])
            if row["parser_version"] is None:
                raise DocumentError(
                    "historical_parser_missing",
                    "document version has no persisted parser identity to replay",
                )
            cache = self._frozen_replay_cache.get()
            cache_key = (
                company_id, row["document_version_id"], row["source_sha256"],
                row["byte_length"], row["media_type"], row["parser_version"],
                row["identity_sha256"],
                hashlib.sha256((row["identity_json"] or "").encode("utf-8")).hexdigest(),
                tuple(row[key] for key in row.keys() if key != "identity_json"),
            )
            if cache is not None and cache_key in cache:
                # The catalog identity and raw bytes were re-read and hash-checked above.
                # Only the deterministic parse of that exact byte/parser identity is reused.
                return deepcopy(cache[cache_key])
            parse_identity = None
            provenances: list[dict[str, Any] | None]
            if row["identity_json"] is None:
                parsed = self._parse_frozen_document(
                    raw, row["media_type"], row["parser_version"]
                )
                provenances = [None] * len(parsed.tables)
            else:
                try:
                    parse_identity = json.loads(row["identity_json"])
                    identity_json = _json(parse_identity)
                    identity_sha256 = hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
                    if identity_sha256 != row["identity_sha256"]:
                        raise ValueError("persisted identity hash differs")
                    identity_value = {
                        "company_id": row["company_id"], "source_key": row["source_key"],
                        "source_version": row["source_version"],
                        "source_sha256": row["source_sha256"], "title": row["title"],
                        "url": row["source_url"], "published_at": row["published_at"],
                        "media_type": row["media_type"], "rights_status": row["rights_status"],
                        "collected_at": row["collected_at"],
                        "parse_identity_sha256": identity_sha256,
                    }
                    if hashlib.sha256(_json(identity_value).encode("utf-8")).hexdigest() != row["document_version_id"]:
                        raise ValueError("document version does not bind persisted parse identity")
                    if (row["media_type"] != "application/pdf"
                            or parse_identity.get("raw_pdf_sha256") != row["source_sha256"]
                            or row["parser_version"] !=
                            f"{PDF_TABLE_PARSER_PREFIX};identity={identity_sha256}"):
                        raise ValueError("persisted PDF source or parser identity differs")
                    parsed, replayed_identity, replayed_provenances = _parse_pdf_with_tables(
                        raw, parse_identity["profile"]
                    )
                    if replayed_identity != parse_identity or parsed.parser_version != row["parser_version"]:
                        raise ValueError("replayed PDF parse identity differs")
                    provenances = list(replayed_provenances)
                except (DocumentError, ParseError, KeyError, TypeError, ValueError) as exc:
                    raise DocumentError(
                        "historical_parser_unavailable",
                        "the persisted PDF table source and parse identity cannot be reproduced",
                    ) from exc
            tables = [self._table_record(
                parser_version=parsed.parser_version, table_index=table.index,
                start=table.start, end=table.end, rows=table.rows, row_spans=table.row_spans,
                provenance=provenances[index],
            ) for index, table in enumerate(parsed.tables)]
            result = {
                **self._descriptor(row), "parser_version": parsed.parser_version,
                "text": parsed.text, "tables": tables,
            }
            if parse_identity is not None:
                result["parse_identity"] = parse_identity
            if cache is not None:
                cache[cache_key] = deepcopy(result)
            return result
        finally:
            connection.close()

    def _result(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = {
            "passage_id": row["passage_id"], "document_version_id": row["document_version_id"],
            "company_id": row["company_id"], "title": row["title"],
            "source_url": row["source_url"], "source_sha256": row["source_sha256"],
            "parser_version": row["parser_version"], "quote": row["quote"],
            "locator": {"start": row["start_char"], "end": row["end_char"]},
            "published_at": row["published_at"], "collected_at": row["collected_at"],
            "source_key": row["source_key"], "source_version": row["source_version"],
            "media_type": row["media_type"], "rights_status": row["rights_status"],
        }
        result["tables"] = self._tables(
            connection, row["document_version_id"], row["start_char"], row["end_char"]
        )
        page = _pdf_page_locator(row["text"], row["start_char"], row["end_char"]) if "text" in row.keys() else None
        if page is not None:
            result["locator"]["page"] = page["page"]
            result["locator"]["page_start"] = page["page_start"]
            result["locator"]["page_end"] = page["page_end"]
            result["locator"]["page_basis"] = page["basis"]
            locator = PageTextSpanLocator(
                "page_text_span", page["page"], row["start_char"], row["end_char"],
                page["page_start"], page["page_end"], page["basis"])
        else:
            locator = TextSpanLocator("text_span", row["start_char"], row["end_char"])
        result["evidence_ref"] = EvidenceRef(
            SourceVersion(row["source_key"], row["document_version_id"], row["source_version"],
                          row["source_sha256"], row["byte_length"], self.source_registry.scope),
            locator, "parsed", row["parser_version"],
        ).to_json()
        return result

    def search_documents(
        self, *, company_id, query, limit=8, document_version_ids=None,
    ) -> dict[str, Any]:
        company_id = _company(company_id)
        query = _text(query, "query", MAX_QUERY_CHARS)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise DocumentError("invalid_arguments", "limit must be an integer from 1 to 50")
        expression, terms = _expanded_query(query)
        connection = self._connect()
        try:
            clause, params = self._selection_clause(connection, company_id, document_version_ids)
            coverage = self._coverage(connection, clause, params)
            rows = connection.execute(
                "SELECT p.*,v.company_id,v.title,v.source_url,v.source_sha256,v.byte_length,"
                "v.published_at,v.collected_at,v.source_key,v.source_version,v.media_type,v.rights_status,"
                "d.text,bm25(passages_fts) AS score FROM passages_fts "
                "JOIN passages p ON p.passage_id=passages_fts.passage_id "
                "JOIN document_versions v ON v.document_version_id=p.document_version_id "
                "JOIN parsed_documents d ON d.document_version_id=p.document_version_id "
                "WHERE passages_fts MATCH ? AND " + clause +
                " ORDER BY score,p.passage_id LIMIT ?",
                [expression, *params, limit],
            ).fetchall()
            self._verify_rows(rows)
            results = [self._result(connection, row) for row in rows]
            return {
                "results": results, "coverage": coverage,
                "expanded_query": {"fts5": expression, "terms": terms},
                "interpretation": (
                    "Matches are passages from the selected enrolled versions."
                    if results else
                    "No matching passage was found in the selected enrolled versions; this does not mean the company did not disclose it."
                ),
                "version_scope": "explicit" if document_version_ids is not None else "current_per_source",
                "as_of": None,
            }
        except sqlite3.OperationalError as exc:
            raise DocumentError("search_failed", "full-text search could not be completed") from exc
        finally:
            connection.close()

    def read_passage(
        self, *, company_id, passage_id, context_chars=500, exact_quote=None,
    ) -> dict[str, Any]:
        company_id = _company(company_id)
        if not isinstance(passage_id, str) or re.fullmatch(r"[0-9a-f]{64}", passage_id) is None:
            raise DocumentError("invalid_arguments", "passage_id is invalid")
        if type(context_chars) is not int or not 0 <= context_chars <= MAX_CONTEXT_CHARS:
            raise DocumentError("invalid_arguments", f"context_chars must be an integer from 0 to {MAX_CONTEXT_CHARS}")
        if exact_quote is not None:
            if (not isinstance(exact_quote, str) or not exact_quote
                    or len(exact_quote) > MAX_EXACT_QUOTE_CHARS or "\x00" in exact_quote):
                raise DocumentError(
                    "invalid_arguments",
                    f"exact_quote must contain 1..{MAX_EXACT_QUOTE_CHARS} characters",
                )
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT p.*,v.company_id,v.title,v.source_url,v.source_sha256,v.byte_length,"
                "v.published_at,v.collected_at,v.source_key,v.source_version,v.media_type,v.rights_status,"
                "d.text FROM passages p JOIN document_versions v ON v.document_version_id=p.document_version_id "
                "JOIN parsed_documents d ON d.document_version_id=p.document_version_id "
                "WHERE p.passage_id=? AND v.company_id=?", (passage_id, company_id),
            ).fetchone()
            if row is None:
                raise DocumentError("passage_not_found", "passage does not exist for this company")
            self._verify_rows([row])
            result = self._result(connection, row)
            context_start = max(0, row["start_char"] - context_chars)
            context_end = min(len(row["text"]), row["end_char"] + context_chars)
            # Context is navigation-only. Expand inside the verified frozen
            # text so it cannot begin or end in the middle of an ASCII word.
            while (context_start > 0 and context_start < len(row["text"])
                   and row["text"][context_start - 1].isascii()
                   and row["text"][context_start].isascii()
                   and row["text"][context_start - 1].isalnum()
                   and row["text"][context_start].isalnum()):
                context_start -= 1
            while (context_end > 0 and context_end < len(row["text"])
                   and row["text"][context_end - 1].isascii()
                   and row["text"][context_end].isascii()
                   and row["text"][context_end - 1].isalnum()
                   and row["text"][context_end].isalnum()):
                context_end += 1
            context_rows = connection.execute(
                "SELECT p.*,v.company_id,v.title,v.source_url,v.source_sha256,v.byte_length,"
                "v.published_at,v.collected_at,v.source_key,v.source_version,v.media_type,v.rights_status "
                "FROM passages p JOIN document_versions v "
                "ON v.document_version_id=p.document_version_id "
                "WHERE p.document_version_id=? AND p.start_char < ? AND p.end_char > ? "
                "ORDER BY p.start_char,p.passage_id LIMIT ?",
                (row["document_version_id"], context_end, context_start, MAX_CONTEXT_PASSAGES),
            ).fetchall()
            self._verify_rows(context_rows)

            def candidate(item: sqlite3.Row) -> dict[str, Any]:
                return {
                    "passage_id": item["passage_id"],
                    "document_version_id": item["document_version_id"],
                    "company_id": item["company_id"],
                    "quote": item["quote"],
                    "locator": {"start": item["start_char"], "end": item["end_char"]},
                }

            context_passages = [candidate(item) for item in context_rows]
            result.update({
                "context": row["text"][context_start:context_end],
                "context_locator": {"start": context_start, "end": context_end},
                "offset": row["start_char"] - context_start,
                "truncated": context_start > 0 or context_end < len(row["text"]),
                "context_passages": context_passages,
                "citation_guidance": (
                    "quote is the exact citable text for passage_id. context is navigation only; "
                    "cite a context_passages passage_id only when that candidate's quote supports "
                    "the claim. Use exact_quote to select candidates containing exact text."
                ),
            })
            if exact_quote is not None:
                occurrences = []
                position = context_start
                while True:
                    position = row["text"].find(exact_quote, position, context_end)
                    if position < 0:
                        break
                    occurrence_end = position + len(exact_quote)
                    supporting = [
                        item for item in context_passages
                        if item["locator"]["start"] <= position
                        and item["locator"]["end"] >= occurrence_end
                    ]
                    occurrences.append({
                        "exact_quote": exact_quote,
                        "locator": {"start": position, "end": occurrence_end},
                        "supporting_passages": supporting,
                    })
                    position += max(1, len(exact_quote))
                result["exact_citation_selection"] = {
                    "status": "matched" if occurrences else "not_found_in_context",
                    "occurrences": occurrences,
                    "interpretation": (
                        "Only supporting_passages contain the exact requested text and are citable."
                        if occurrences else
                        "The exact requested text was not found in this bounded context; do not cite "
                        "the requested passage for it."
                    ),
                }
            return result
        finally:
            connection.close()

    def read_raw_version(self, *, company_id, document_version_id) -> bytes:
        """Return verified bytes, never an object-store path."""
        company_id = _company(company_id)
        if not isinstance(document_version_id, str):
            raise DocumentError("invalid_arguments", "document_version_id is invalid")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT source_key,source_version,source_sha256,byte_length FROM document_versions "
                "WHERE company_id=? AND document_version_id=?", (company_id, document_version_id),
            ).fetchone()
            if row is None:
                raise DocumentError("document_not_found", "document version does not exist for this company")
            return self.source_registry.read(SourceVersion(
                row["source_key"], document_version_id, row["source_version"],
                row["source_sha256"], row["byte_length"], self.source_registry.scope))
        finally:
            connection.close()
    catalog_media_types = "'text/html','application/pdf'"
