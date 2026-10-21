"""Administrator-configured, append-only archival of exact HTTPS sources.

This is an L1 byte archive, not a crawler or an Agent network tool.  Successful
objects and the SQLite ledger are separate resources: a database failure may
leave an unreferenced content-addressed object, never a falsely committed fetch.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from workbench.official_sources import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_DOCUMENT_BYTES,
    MAX_REDIRECTS,
    OfficialSourceError,
    PinnedHTTPSHTTPTransport,
    _public_addresses,
    system_resolver,
)
from workbench.raw_storage import ObjectStore, RawObjectStore


APPLICATION_ID = 0x53415243  # SARC
SCHEMA_VERSION = 1
_SUPPORTED_MEDIA = {
    "text/html", "application/xhtml+xml", "application/pdf",
    "application/json", "text/json", "text/plain",
}
_REDIRECTS = {301, 302, 303, 307, 308}
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE archive_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE source_versions (
 archive_version_id TEXT PRIMARY KEY,
 source_id TEXT NOT NULL,
 source_version INTEGER NOT NULL CHECK(source_version > 0),
 supersedes_archive_version_id TEXT REFERENCES source_versions(archive_version_id),
 config_sha256 TEXT NOT NULL CHECK(length(config_sha256)=64),
 config_json TEXT NOT NULL,
 request_url TEXT NOT NULL,
 final_url TEXT NOT NULL,
 fetched_at TEXT NOT NULL,
 media_type TEXT NOT NULL,
 byte_length INTEGER NOT NULL CHECK(byte_length > 0),
 source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64),
 response_metadata_json TEXT NOT NULL,
 UNIQUE(source_id, source_version)
);
CREATE TABLE acquisition_attempts (
 attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
 source_id TEXT NOT NULL,
 config_sha256 TEXT NOT NULL CHECK(length(config_sha256)=64),
 config_json TEXT NOT NULL,
 attempted_at TEXT NOT NULL,
 method TEXT NOT NULL CHECK(method IN ('network','import_local')),
 request_url TEXT NOT NULL,
 final_url TEXT,
 http_status INTEGER,
 outcome TEXT NOT NULL CHECK(outcome IN ('success','failure')),
 error_code TEXT,
 error_message TEXT,
 response_metadata_json TEXT NOT NULL,
 archive_version_id TEXT REFERENCES source_versions(archive_version_id)
);
CREATE INDEX source_versions_history ON source_versions(source_id, source_version DESC);
CREATE INDEX acquisition_attempts_history ON acquisition_attempts(source_id, attempt_id DESC);
CREATE TRIGGER source_versions_no_update BEFORE UPDATE ON source_versions BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER source_versions_no_delete BEFORE DELETE ON source_versions BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER acquisition_attempts_no_update BEFORE UPDATE ON acquisition_attempts BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER acquisition_attempts_no_delete BEFORE DELETE ON acquisition_attempts BEGIN SELECT RAISE(ABORT,'append-only'); END;
"""


class SourceArchiveError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message

    def as_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _text(value: Any, name: str, maximum: int, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum or "\x00" in value:
        raise SourceArchiveError("invalid_config", f"{name} must contain 1..{maximum} safe characters")
    return value.strip()


def _safe_url_syntax(url: Any) -> str:
    url = _text(url, "url", 4000)
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError as exc:
        raise SourceArchiveError("unsafe_url", "source URL has an invalid port") from exc
    decoded = unquote(parts.path)
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.fragment or port not in (None, 443)
            or "\\" in decoded or any(piece in {".", ".."} for piece in decoded.split("/"))
            or re.search(r"%2f|%5c", parts.path, re.I)):
        raise SourceArchiveError("unsafe_url", "only exact HTTPS port 443 URLs without userinfo, fragments, or traversal are allowed")
    return url


def normalize_source_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SourceArchiveError("invalid_config", "source configuration must be an object")
    config = dict(value)
    source_id = _text(config.get("source_id"), "source_id", 200)
    if _ID_RE.fullmatch(source_id) is None:
        raise SourceArchiveError("invalid_config", "source_id contains unsupported characters")
    config["source_id"] = source_id
    config["title"] = _text(config.get("title"), "title", 1000)
    config["company_id"] = _text(config.get("company_id"), "company_id", 100, nullable=True)
    config["url"] = _safe_url_syntax(config.get("url"))
    allowed = config.get("allowed_urls", [config["url"]])
    if not isinstance(allowed, list) or not 1 <= len(allowed) <= 20:
        raise SourceArchiveError("invalid_config", "allowed_urls must contain 1..20 exact URLs")
    config["allowed_urls"] = sorted(set(_safe_url_syntax(item) for item in allowed))
    if config["url"] not in config["allowed_urls"]:
        raise SourceArchiveError("invalid_config", "the request URL must be included in allowed_urls")
    media = config.get("media_types", ["text/html", "application/pdf"])
    if (not isinstance(media, list) or not media or len(media) > len(_SUPPORTED_MEDIA)
            or any(item not in _SUPPORTED_MEDIA for item in media)):
        raise SourceArchiveError("invalid_config", "media_types contains an unsupported L1 representation")
    config["media_types"] = sorted(set(media))
    maximum = config.get("max_bytes", MAX_DOCUMENT_BYTES)
    timeout = config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if type(maximum) is not int or not 1 <= maximum <= MAX_DOCUMENT_BYTES:
        raise SourceArchiveError("invalid_config", "max_bytes is outside the 1..20 MiB archive limit")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < float(timeout) <= 60:
        raise SourceArchiveError("invalid_config", "timeout_seconds is outside the 0..60 second limit")
    config["max_bytes"] = maximum
    config["timeout_seconds"] = float(timeout)
    config["published_at"] = _text(config.get("published_at"), "published_at", 100, nullable=True)
    rights = config.get("rights_status", "local_only")
    config["rights_status"] = _text(rights, "rights_status", 100)
    if "rights" in config and not isinstance(config["rights"], dict):
        raise SourceArchiveError("invalid_config", "rights must be an object when supplied")
    return config


def load_source_config(path: os.PathLike[str] | str, source_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        sources = payload["sources"]
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SourceArchiveError("invalid_config", "source configuration file cannot be read") from exc
    if not isinstance(sources, list):
        raise SourceArchiveError("invalid_config", "sources must be a list")
    matched = [item for item in sources if isinstance(item, dict) and item.get("source_id") == source_id]
    if len(matched) != 1:
        raise SourceArchiveError("unknown_source", "source_id must identify exactly one configured source")
    return normalize_source_config(matched[0])


class SourceArchive(RawObjectStore):
    def __init__(self, root: os.PathLike[str] | str, *, transport=None, resolver=system_resolver,
                 cancel_event: threading.Event | None = None,
                 object_store: ObjectStore | None = None):
        super().__init__(
            root, error_type=SourceArchiveError, max_bytes=MAX_DOCUMENT_BYTES,
            object_store=object_store,
        )
        self.db_path = self.root / "source_acquisitions.sqlite3"
        self.resolver = resolver
        self.transport = transport or PinnedHTTPSHTTPTransport(resolver)
        self.cancel_event = cancel_event
        self._initialize()

    @staticmethod
    def _schema_fingerprint(connection: sqlite3.Connection) -> str:
        rows = list(connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"))
        return hashlib.sha256(_json([list(row) for row in rows]).encode()).hexdigest()

    @classmethod
    def _expected_fingerprint(cls) -> str:
        connection = sqlite3.connect(":memory:")
        try:
            connection.executescript(_SCHEMA)
            return cls._schema_fingerprint(connection)
        finally:
            connection.close()

    def _check_ledger_path(self) -> None:
        self._ensure_storage_dirs()
        if self.db_path.is_symlink() or (self.db_path.exists() and not self.db_path.is_file()):
            raise SourceArchiveError("unsafe_ledger_path", "acquisition ledger must be a regular non-symlink file")

    def _initialize(self) -> None:
        self._check_ledger_path()
        existed = self.db_path.exists()
        connection = sqlite3.connect(self.db_path, timeout=10)
        try:
            connection.execute("BEGIN IMMEDIATE")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if not tables:
                # executescript would commit the initialization lock first.
                # Execute complete statements inside the same transaction instead.
                statement = ""
                for line in _SCHEMA.splitlines(keepends=True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        connection.execute(statement)
                        statement = ""
                fingerprint = self._schema_fingerprint(connection)
                connection.executemany("INSERT INTO archive_meta VALUES(?,?)", (("schema_version", str(SCHEMA_VERSION)), ("schema_fingerprint", fingerprint)))
                connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                connection.commit()
            else:
                metadata = dict(connection.execute("SELECT key,value FROM archive_meta"))
                if (connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                        or connection.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
                        or metadata.get("schema_version") != str(SCHEMA_VERSION)
                        or metadata.get("schema_fingerprint") != self._schema_fingerprint(connection)
                        or metadata.get("schema_fingerprint") != self._expected_fingerprint()):
                    raise SourceArchiveError("unknown_archive_schema", "existing acquisition ledger schema is not recognized")
            connection.commit()
        except SourceArchiveError:
            raise
        except sqlite3.Error as exc:
            code = "unknown_archive_schema" if existed else "archive_unavailable"
            raise SourceArchiveError(code, "acquisition ledger cannot be initialized") from exc
        finally:
            connection.close()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        self._check_ledger_path()
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _config(self, config: Any) -> tuple[dict[str, Any], str, str]:
        normalized = normalize_source_config(config)
        encoded = _json(normalized)
        return normalized, encoded, hashlib.sha256(encoded.encode()).hexdigest()

    def _validate_target(self, url: str, config: dict[str, Any], deadline: float) -> None:
        if url not in config["allowed_urls"]:
            raise SourceArchiveError("redirect_not_allowed", "request or redirect target is not in allowed_urls")
        _safe_url_syntax(url)
        try:
            _public_addresses(urlsplit(url).hostname or "", self.resolver,
                              cancel_event=self.cancel_event, deadline=deadline)
        except OfficialSourceError as exc:
            raise SourceArchiveError(exc.code, exc.message) from exc

    @staticmethod
    def _media(headers: dict[str, str], config: dict[str, Any]) -> str:
        media = headers.get("content-type", "").split(";", 1)[0].strip().casefold()
        if media not in config["media_types"]:
            raise SourceArchiveError("unsupported_media_type", "response MIME type is not allowed by the source configuration")
        return media

    @staticmethod
    def _response_metadata(headers: dict[str, str]) -> dict[str, str]:
        allowed = ("content-type", "content-length", "etag", "last-modified", "location")
        return {key: str(headers[key])[:2000] for key in allowed if key in headers}

    def _fetch_network(self, config: dict[str, Any]) -> tuple[bytes, str, str, int, dict[str, str]]:
        deadline = time.monotonic() + config["timeout_seconds"]
        current = config["url"]
        for redirects in range(MAX_REDIRECTS + 1):
            self._validate_target(current, config, deadline)
            try:
                response = self.transport.get(current, timeout=max(0.001, deadline - time.monotonic()),
                                              max_bytes=config["max_bytes"], cancel_event=self.cancel_event,
                                              deadline=deadline)
            except OfficialSourceError as exc:
                raise SourceArchiveError(exc.code, exc.message) from exc
            except (http.client.HTTPException, OSError):
                raise SourceArchiveError("missing_retrieval", "source request failed") from None
            metadata = self._response_metadata(response.headers)
            if len(response.body) > config["max_bytes"]:
                error = SourceArchiveError("response_too_large", "response exceeded the configured byte limit")
                error.http_status, error.final_url, error.response_metadata = response.status, current, metadata
                raise error
            declared_length = response.headers.get("content-length")
            if (declared_length is not None
                    and (re.fullmatch(r"[0-9]+", declared_length) is None
                         or (declared_length.lstrip("0") or "0") != str(len(response.body)))):
                error = SourceArchiveError(
                    "incomplete_response", "response body did not match its declared Content-Length")
                error.http_status, error.final_url, error.response_metadata = response.status, current, metadata
                raise error
            if response.status in _REDIRECTS:
                if redirects == MAX_REDIRECTS:
                    raise SourceArchiveError("too_many_redirects", "response exceeded the redirect limit")
                location = response.headers.get("location")
                if not location:
                    raise SourceArchiveError("invalid_redirect", "redirect omitted its destination")
                redirected = urljoin(current, location)
                self._validate_target(redirected, config, deadline)
                current = redirected
                continue
            if response.status != 200:
                error = SourceArchiveError("http_error", f"source returned HTTP {response.status}")
                error.http_status, error.final_url, error.response_metadata = response.status, current, metadata
                raise error
            if not response.body:
                error = SourceArchiveError("empty_response", "source returned no bytes")
                error.http_status, error.final_url, error.response_metadata = response.status, current, metadata
                raise error
            try:
                media = self._media(response.headers, config)
            except SourceArchiveError as error:
                error.http_status, error.final_url, error.response_metadata = response.status, current, metadata
                raise
            return response.body, media, current, response.status, metadata
        raise AssertionError("unreachable")

    @staticmethod
    def _version_record(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "archive_version_id": row["archive_version_id"], "source_id": row["source_id"],
            "source_version": row["source_version"],
            "supersedes_archive_version_id": row["supersedes_archive_version_id"],
            "config_sha256": row["config_sha256"], "request_url": row["request_url"],
            "final_url": row["final_url"],
            "fetched_at": (None if json.loads(row["response_metadata_json"]).get("collection_time_known") is False
                           else row["fetched_at"]),
            "media_type": row["media_type"], "byte_length": row["byte_length"],
            "source_sha256": row["source_sha256"],
            "response_metadata": json.loads(row["response_metadata_json"]),
            "config": json.loads(row["config_json"]),
        }

    def _local(self, source_id: str, config_sha: str, precommit_validator=None) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM source_versions WHERE source_id=? AND config_sha256=? "
                "ORDER BY source_version DESC", (source_id, config_sha)).fetchall()
        finally:
            connection.close()
        for row in rows:
            raw = self._verify_object(row["source_sha256"], row["byte_length"])
            if precommit_validator is not None:
                try:
                    precommit_validator(raw, row["media_type"], row["final_url"])
                except SourceArchiveError as exc:
                    if exc.code != "source_identity_mismatch":
                        raise
                    continue
                except Exception as exc:
                    raise SourceArchiveError(
                        "source_validation_failed", "local source validation failed safely") from exc
            result = self._version_record(row)
            result["retrieval"] = "local"
            return result
        return None

    def _failure(self, config: dict[str, Any], encoded: str, config_sha: str, method: str,
                 error: SourceArchiveError, *, final_url: str | None = None,
                 status: int | None = None, metadata: dict[str, str] | None = None) -> None:
        connection = self._connect()
        try:
            connection.execute("INSERT INTO acquisition_attempts(source_id,config_sha256,config_json,attempted_at,method,request_url,final_url,http_status,outcome,error_code,error_message,response_metadata_json,archive_version_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                               (config["source_id"], config_sha, encoded, _now(), method, config["url"], final_url, status, "failure", error.code, error.message, _json(metadata or {})))
            connection.commit()
        except sqlite3.Error as exc:
            connection.rollback()
            raise SourceArchiveError("archive_write_failed", "failed acquisition and its ledger record could not be committed") from exc
        finally:
            connection.close()

    def _success(self, config: dict[str, Any], encoded: str, config_sha: str, raw: bytes,
                 media_type: str, final_url: str, method: str, status: int | None,
                 metadata: dict[str, str], fetched_at: str | None = None) -> dict[str, Any]:
        digest = hashlib.sha256(raw).hexdigest()
        self._store_object(digest, raw)
        when = fetched_at or _now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT * FROM source_versions WHERE source_id=? ORDER BY source_version DESC LIMIT 1", (config["source_id"],)).fetchone()
            # Reuse only the current revision. A -> B -> A is a third observation
            # of changed content, not a return value that leaves B as local latest.
            identity_fields = ("config_sha256", "source_sha256", "byte_length", "final_url", "media_type")
            same_as_latest = previous is not None and tuple(previous[key] for key in identity_fields) == (
                config_sha, digest, len(raw), final_url, media_type)
            row = previous if same_as_latest else None
            if row is None:
                version = 1 if previous is None else previous["source_version"] + 1
                supersedes = None if previous is None else previous["archive_version_id"]
                identity = _json({"source_id": config["source_id"], "source_version": version,
                                  "config_sha256": config_sha, "sha256": digest, "bytes": len(raw),
                                  "final_url": final_url, "media_type": media_type})
                version_id = "source-" + hashlib.sha256(identity.encode()).hexdigest()
                connection.execute("INSERT INTO source_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (version_id, config["source_id"], version, supersedes, config_sha, encoded,
                                    config["url"], final_url, when, media_type, len(raw), digest, _json(metadata)))
                row = connection.execute("SELECT * FROM source_versions WHERE archive_version_id=?", (version_id,)).fetchone()
            connection.execute("INSERT INTO acquisition_attempts(source_id,config_sha256,config_json,attempted_at,method,request_url,final_url,http_status,outcome,error_code,error_message,response_metadata_json,archive_version_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (config["source_id"], config_sha, encoded, _now(), method, config["url"], final_url,
                                status, "success", None, None, _json(metadata), row["archive_version_id"]))
            connection.commit()
            result = self._version_record(row)
            result["retrieval"] = method
            return result
        except sqlite3.Error as exc:
            connection.rollback()
            raise SourceArchiveError("archive_write_failed", "successful acquisition could not be committed to the ledger") from exc
        finally:
            connection.close()

    def fetch(self, config: Any, *, refresh: bool = False, precommit_validator=None) -> dict[str, Any]:
        config, encoded, config_sha = self._config(config)
        if precommit_validator is not None and not callable(precommit_validator):
            raise SourceArchiveError("invalid_arguments", "precommit_validator must be callable")
        if not refresh:
            local = self._local(config["source_id"], config_sha, precommit_validator)
            if local is not None:
                return local
        try:
            raw, media, final_url, status, metadata = self._fetch_network(config)
            if precommit_validator is not None:
                try:
                    precommit_validator(raw, media, final_url)
                except SourceArchiveError as error:
                    error.http_status = status
                    error.final_url = final_url
                    error.response_metadata = metadata
                    raise
                except Exception as exc:
                    error = SourceArchiveError(
                        "source_validation_failed", "downloaded source validation failed safely")
                    error.http_status = status
                    error.final_url = final_url
                    error.response_metadata = metadata
                    raise error from exc
        except SourceArchiveError as exc:
            self._failure(
                config, encoded, config_sha, "network", exc,
                final_url=getattr(exc, "final_url", None),
                status=getattr(exc, "http_status", None),
                metadata=getattr(exc, "response_metadata", None),
            )
            raise
        return self._success(config, encoded, config_sha, raw, media, final_url, "network", status, metadata)

    @staticmethod
    def _read_local_file(path: os.PathLike[str] | str, maximum: int) -> bytes:
        source = Path(path)
        try:
            before = source.lstat()
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                raise SourceArchiveError("unsafe_source_path", "local import must be a regular non-symlink file")
            fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                opened = os.fstat(handle.fileno())
                raw = handle.read(maximum + 1)
                after = os.fstat(handle.fileno())
        except SourceArchiveError:
            raise
        except OSError as exc:
            raise SourceArchiveError("source_unavailable", "local import cannot be read safely") from exc
        if len(raw) > maximum:
            raise SourceArchiveError("source_too_large", "local import exceeds the configured byte limit")
        if not raw:
            raise SourceArchiveError("invalid_file", "local import is empty")
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) or (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
            raise SourceArchiveError("source_changed", "local import changed while being read")
        return raw

    def import_local(self, config: Any, path: os.PathLike[str] | str, *, expected_sha256: str,
                     expected_size: int, media_type: str, fetched_at: str | None = None) -> dict[str, Any]:
        config, encoded, config_sha = self._config(config)
        if not isinstance(expected_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
            raise SourceArchiveError("invalid_manifest", "expected_sha256 must be a lowercase SHA-256 digest")
        if type(expected_size) is not int or expected_size < 1:
            raise SourceArchiveError("invalid_manifest", "expected_size must be a positive integer")
        if media_type not in config["media_types"]:
            raise SourceArchiveError("unsupported_media_type", "local media type is not allowed by the source configuration")
        if fetched_at is not None:
            try:
                timestamp = datetime.fromisoformat(fetched_at)
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise ValueError("timezone required")
            except (TypeError, ValueError):
                raise SourceArchiveError("invalid_manifest", "fetched_at must be a known timezone-aware timestamp") from None
        try:
            raw = self._read_local_file(path, config["max_bytes"])
            if len(raw) != expected_size:
                raise SourceArchiveError("source_size_mismatch", "local bytes do not match expected_size")
            if hashlib.sha256(raw).hexdigest() != expected_sha256:
                raise SourceArchiveError("source_hash_mismatch", "local bytes do not match expected_sha256")
        except SourceArchiveError as exc:
            self._failure(config, encoded, config_sha, "import_local", exc)
            raise
        return self._success(config, encoded, config_sha, raw, media_type, config["url"],
                             "import_local", None, {"collection_time_known": fetched_at is not None}, fetched_at=fetched_at)

    def read(self, source_id: str, *, source_version: int | None = None,
             archive_version_id: str | None = None) -> bytes:
        if (source_version is None) == (archive_version_id is None):
            raise SourceArchiveError("invalid_arguments", "select exactly one source_version or archive_version_id")
        connection = self._connect()
        try:
            if archive_version_id is not None:
                row = connection.execute("SELECT * FROM source_versions WHERE source_id=? AND archive_version_id=?", (source_id, archive_version_id)).fetchone()
            else:
                row = connection.execute("SELECT * FROM source_versions WHERE source_id=? AND source_version=?", (source_id, source_version)).fetchone()
        finally:
            connection.close()
        if row is None:
            raise SourceArchiveError("unknown_version", "requested successful source version does not exist")
        return self._verify_object(row["source_sha256"], row["byte_length"])

    def history(self, source_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            versions = [self._version_record(row) for row in connection.execute("SELECT * FROM source_versions WHERE source_id=? ORDER BY source_version", (source_id,))]
            attempts = [dict(row) for row in connection.execute("SELECT attempt_id,attempted_at,method,request_url,final_url,http_status,outcome,error_code,error_message,response_metadata_json,archive_version_id,config_sha256 FROM acquisition_attempts WHERE source_id=? ORDER BY attempt_id", (source_id,))]
        finally:
            connection.close()
        for attempt in attempts:
            attempt["response_metadata"] = json.loads(attempt.pop("response_metadata_json"))
        return {"source_id": source_id, "versions": versions, "attempts": attempts}

    def list_link_candidates(self, source_id: str, *, source_version: int,
                             limit: int = 100) -> dict[str, Any]:
        """Inspect one verified archived HTML version without fetching any links."""
        if type(source_version) is not int or source_version < 1:
            raise SourceArchiveError("invalid_arguments", "source_version must be an explicit positive integer")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise SourceArchiveError("invalid_arguments", "limit must be an integer from 1 through 200")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM source_versions WHERE source_id=? AND source_version=?",
                (source_id, source_version),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise SourceArchiveError("unknown_version", "requested successful source version does not exist")
        raw = self._verify_object(row["source_sha256"], row["byte_length"])
        if row["media_type"] not in {"text/html", "application/xhtml+xml"}:
            raise SourceArchiveError("unsupported_media_type", "link candidates require an archived HTML representation")
        from workbench.link_candidates import extract_link_candidates
        extracted = extract_link_candidates(
            raw, document_url=row["final_url"], limit=limit, validate_url=_safe_url_syntax)
        return {
            "source_id": row["source_id"],
            "source_version": row["source_version"],
            "archive_version_id": row["archive_version_id"],
            "source_sha256": row["source_sha256"],
            "byte_length": row["byte_length"],
            "media_type": row["media_type"],
            "final_url": row["final_url"],
            "config_sha256": row["config_sha256"],
            "candidate_status": "discovered_not_fetched_or_approved",
            **extracted,
        }

    def enroll(self, source_id: str, *, source_version: int, document_service=None) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM source_versions WHERE source_id=? AND source_version=?", (source_id, source_version)).fetchone()
        finally:
            connection.close()
        if row is None:
            raise SourceArchiveError("unknown_version", "requested successful source version does not exist")
        config = json.loads(row["config_json"])
        media = row["media_type"]
        if media not in {"text/html", "application/xhtml+xml", "application/pdf"}:
            raise SourceArchiveError("l2_unsupported_media_type", "this archived representation is not supported by DocumentService")
        if not config.get("company_id"):
            raise SourceArchiveError("missing_enrollment_subject", "DocumentService enrollment requires an explicit company_id; L1 archival does not")
        raw = self._verify_object(row["source_sha256"], row["byte_length"])
        if document_service is None:
            from workbench.documents import DocumentService
            document_service = DocumentService(self.root)
        try:
            return document_service.register_bytes(
                raw, company_id=config["company_id"], source_key=config["source_id"],
                title=config["title"], url=config["url"], published_at=config.get("published_at"),
                media_type="text/html" if media == "application/xhtml+xml" else media,
                rights_status=config["rights_status"], collected_at=self._version_record(row)["fetched_at"],
                expected_sha256=row["source_sha256"], expected_byte_length=row["byte_length"],
            )
        except SourceArchiveError:
            raise
        except ValueError as exc:
            if hasattr(exc, "code"):
                raise SourceArchiveError(exc.code, getattr(exc, "message", str(exc))) from exc
            raise
