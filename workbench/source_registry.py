"""L1 registration and verified-read boundary for exact provider bytes.

The registry owns source identity and immutable byte persistence.  It does not import
or select parsers, indexes, query engines, research tools, or transports.  A catalog
transaction is supplied by the L2 facade so source and derived rows can share the
existing database and commit atomically.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import sqlite3
from typing import Any, Literal, Type

from .data_contracts import SourceVersion
from .raw_storage import ObjectStore, RawObjectStore


_COMPANY_RE = re.compile(r"[A-Z0-9][A-Z0-9._:-]{0,99}\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class SourceRegistration:
    company_id: str
    source_key: str
    title: str
    url: str | None
    published_at: str | None
    media_type: str
    rights_status: str
    collected_at: str | None
    sha256: str
    byte_length: int


class SourceRegistry:
    """Public L1 capability; no path or backend handle is returned to consumers."""

    def __init__(self, root, *, error_type: Type[ValueError], max_bytes: int,
                 object_store: ObjectStore | None = None,
                 scope: Literal["public", "private"] = "public"):
        if scope not in {"public", "private"}:
            raise ValueError("source scope must be public or private")
        self.scope = scope
        self._error_type = error_type
        self._store = RawObjectStore(
            root, error_type=error_type, max_bytes=max_bytes, object_store=object_store)
        self.max_object_bytes = self._store.max_object_bytes

    @property
    def root(self):
        """Compatibility metadata for the owning catalog; never returned by reads."""
        return self._store.root

    def _error(self, code: str, message: str):
        return self._error_type(code, message)

    def _text(self, value: Any, name: str, maximum: int, *, nullable: bool = False):
        if value is None and nullable:
            return None
        if not isinstance(value, str):
            raise self._error("invalid_arguments", f"{name} must be a string")
        value = value.strip()
        if not value or len(value) > maximum or "\x00" in value:
            raise self._error(
                "invalid_arguments", f"{name} must contain 1..{maximum} safe characters")
        return value

    def prepare(self, raw: bytes, *, company_id, source_key, title, url=None,
                published_at=None, media_type, rights_status="local_only",
                collected_at=None, expected_sha256=None,
                expected_byte_length=None) -> SourceRegistration:
        """Validate metadata and durably store exact bytes before success is possible."""
        if not isinstance(raw, bytes) or not 0 < len(raw) <= self.max_object_bytes:
            raise self._error(
                "invalid_file", f"Document must contain 1..{self.max_object_bytes} bytes")
        if not isinstance(company_id, str) or _COMPANY_RE.fullmatch(company_id) is None:
            raise self._error(
                "invalid_company_id",
                "company_id must be an uppercase identifier using A-Z, digits, dot, underscore, colon, or hyphen",
            )
        source_key = self._text(source_key, "source_key", 500)
        title = self._text(title, "title", 1_000)
        url = self._text(url, "url", 4_000, nullable=True)
        published_at = self._text(published_at, "published_at", 100, nullable=True)
        media_type = self._text(media_type, "media_type", 100)
        rights_status = self._text(rights_status, "rights_status", 100)
        collected_at = self._text(collected_at, "collected_at", 100, nullable=True)
        digest = hashlib.sha256(raw).hexdigest()
        if expected_sha256 is not None and expected_sha256 != digest:
            raise self._error("source_hash_mismatch", "source bytes do not match the import manifest")
        if expected_byte_length is not None and (
                type(expected_byte_length) is not int or expected_byte_length != len(raw)):
            raise self._error("source_size_mismatch", "source size does not match the import manifest")
        self._store._store_object(digest, raw)
        return SourceRegistration(
            company_id, source_key, title, url, published_at, media_type,
            rights_status, collected_at, digest, len(raw))

    @staticmethod
    def identity_json(registration: SourceRegistration, source_version: int,
                      *, parse_identity_sha256: str | None = None) -> str:
        value = {
            "company_id": registration.company_id,
            "source_key": registration.source_key,
            "source_version": source_version,
            "source_sha256": registration.sha256,
            "title": registration.title,
            "url": registration.url,
            "published_at": registration.published_at,
            "media_type": registration.media_type,
            "rights_status": registration.rights_status,
            "collected_at": registration.collected_at,
        }
        if parse_identity_sha256 is not None:
            value["parse_identity_sha256"] = parse_identity_sha256
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def append(self, connection: sqlite3.Connection, registration: SourceRegistration,
               *, parse_status: str, parse_error: str | None, registered_at: str,
               parse_identity_sha256: str | None = None) -> SourceVersion:
        """Append one source identity inside the caller's open catalog transaction."""
        previous = connection.execute(
            "SELECT document_version_id,source_version FROM document_versions "
            "WHERE company_id=? AND source_key=? ORDER BY source_version DESC LIMIT 1",
            (registration.company_id, registration.source_key),
        ).fetchone()
        version = 1 if previous is None else previous["source_version"] + 1
        supersedes = None if previous is None else previous["document_version_id"]
        identity = self.identity_json(
            registration, version, parse_identity_sha256=parse_identity_sha256)
        version_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        connection.execute(
            "INSERT INTO document_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (version_id, registration.company_id, registration.source_key, version,
             supersedes, registration.sha256, registration.byte_length,
             registration.title, registration.url, registration.published_at,
             registration.media_type, registration.rights_status,
             registration.collected_at, registered_at, parse_status, parse_error),
        )
        return SourceVersion(
            registration.source_key, version_id, version, registration.sha256,
            registration.byte_length, self.scope)

    def read(self, source: SourceVersion) -> bytes:
        """Return verified bytes for an already-authorized typed source identity."""
        if source.scope != self.scope:
            raise self._error("source_scope_mismatch", "source belongs to another privacy scope")
        return self._store._verify_object(source.sha256, source.byte_length)

    # Narrow compatibility methods retained only for the existing L2 facade/tools.
    def ensure_storage(self) -> None:
        self._store._ensure_storage_dirs()

    def read_digest(self, digest: str, byte_length: int | None = None) -> bytes:
        if not isinstance(digest, str) or _DIGEST_RE.fullmatch(digest) is None:
            raise self._error("object_tampered", "invalid object identifier")
        return self._store._verify_object(digest, byte_length)

    @property
    def legacy_objects_path(self):
        return getattr(self._store, "objects_path", None)

    def legacy_object_path(self, digest: str):
        return self._store._object_path(digest)
