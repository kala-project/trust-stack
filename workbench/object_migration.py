"""Explicit, resumable copy/verification for immutable ObjectStore boundaries.

This module never changes application configuration and never deletes source bytes.
A manifest describes metadata-derived references; a separate progress file is only a
retry aid.  Completion is published only after every target object is re-read and
verified.  Callers must provide exact stores for every scope: there is no fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing
from typing import Iterable, Mapping

from .raw_storage import ObjectStore


VERSION = "object-migration/1"
_HEX = re.compile(r"[0-9a-f]{64}\Z")


class MigrationError(ValueError):
    pass


@dataclass(frozen=True)
class StoreBinding:
    """A store plus its non-secret, operator-reviewable immutable identity."""

    backend: str
    identity: str
    store: ObjectStore

    def __post_init__(self):
        if not isinstance(self.backend, str) or not self.backend or not isinstance(self.identity, str) or not self.identity:
            raise MigrationError("store backend and identity are required")
        if not isinstance(self.store, ObjectStore):
            raise MigrationError("store does not implement ObjectStore")


def _scope(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 500 or "\0" in value:
        raise MigrationError("invalid object scope")
    return value


def _reference(value: Mapping) -> dict:
    try:
        scope = _scope(value["scope"])
        digest, size = value["sha256"], value["bytes"]
        refs = value["metadata_references"]
    except (KeyError, TypeError) as exc:
        raise MigrationError("invalid object reference") from exc
    if not isinstance(digest, str) or _HEX.fullmatch(digest) is None:
        raise MigrationError("invalid object digest")
    if type(size) is not int or size <= 0:
        raise MigrationError("invalid object size")
    if not isinstance(refs, list) or not refs or any(not isinstance(item, str) or not item for item in refs):
        raise MigrationError("object requires source metadata references")
    return {"scope": scope, "sha256": digest, "bytes": size, "metadata_references": sorted(set(refs))}


def _binding(bindings: Mapping[str, StoreBinding], scope: str) -> StoreBinding:
    try:
        binding = bindings[scope]
    except KeyError as exc:
        raise MigrationError(f"no object store configured for scope {scope!r}") from exc
    if not isinstance(binding, StoreBinding):
        raise MigrationError("invalid store binding")
    return binding


def build_manifest(references: Iterable[Mapping], source: Mapping[str, StoreBinding],
                   target: Mapping[str, StoreBinding], *, generation: str, verify_source: bool = True) -> dict:
    """Build a dry-run manifest and optionally prove every source reference now.

    Duplicate metadata rows are folded only when scope, digest, and size agree.
    A digest reused with a conflicting size in one scope is rejected.
    """
    if not isinstance(generation, str) or not generation or len(generation) > 200:
        raise MigrationError("an explicit backup/migration generation is required")
    objects: dict[tuple[str, str], dict] = {}
    for raw in references:
        item = _reference(raw)
        key = item["scope"], item["sha256"]
        previous = objects.get(key)
        if previous is not None:
            if previous["bytes"] != item["bytes"]:
                raise MigrationError("metadata has conflicting object sizes")
            previous["metadata_references"] = sorted(set(previous["metadata_references"] + item["metadata_references"]))
            continue
        source_binding = _binding(source, item["scope"])
        target_binding = _binding(target, item["scope"])
        if source_binding.identity == target_binding.identity:
            raise MigrationError("source and target store identities must differ")
        if verify_source:
            source_binding.store.get_verified(item["sha256"], item["bytes"])
        objects[key] = {
            **item,
            "source_backend": source_binding.backend,
            "source_identity": source_binding.identity,
            "target_backend": target_binding.backend,
            "target_identity": target_binding.identity,
        }
    if not objects:
        raise MigrationError("object manifest must not be empty")
    return {"version": VERSION, "generation": generation, "objects": sorted(
        objects.values(), key=lambda value: (value["scope"], value["sha256"]))}


def validate_manifest(manifest: Mapping, source: Mapping[str, StoreBinding] | None = None,
                      target: Mapping[str, StoreBinding] | None = None) -> list[dict]:
    if not isinstance(manifest, Mapping) or manifest.get("version") != VERSION or not isinstance(manifest.get("generation"), str):
        raise MigrationError("invalid migration manifest")
    raw_objects = manifest.get("objects")
    if not isinstance(raw_objects, list) or not raw_objects:
        raise MigrationError("incomplete migration manifest")
    seen, objects = set(), []
    for raw in raw_objects:
        item = _reference(raw)
        key = item["scope"], item["sha256"]
        if key in seen:
            raise MigrationError("duplicate object manifest entry")
        seen.add(key)
        for side, bindings in (("source", source), ("target", target)):
            backend, identity = raw.get(f"{side}_backend"), raw.get(f"{side}_identity")
            if not isinstance(backend, str) or not backend or not isinstance(identity, str) or not identity:
                raise MigrationError("manifest store identity is missing")
            if bindings is not None:
                actual = _binding(bindings, item["scope"])
                if actual.backend != backend or actual.identity != identity:
                    raise MigrationError(f"{side} store identity changed since dry-run")
        objects.append(dict(raw))
    return objects


def _atomic_json(path: Path, value: Mapping) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        raise MigrationError("temporary progress path already exists")
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def copy_manifest(manifest: Mapping, source: Mapping[str, StoreBinding], target: Mapping[str, StoreBinding],
                  *, progress_path: Path | str | None = None) -> dict:
    """Idempotently copy all objects and publish completion only after target reads."""
    objects = validate_manifest(manifest, source, target)
    progress_file = None if progress_path is None else Path(progress_path)
    completed: set[str] = set()
    if progress_file is not None and progress_file.exists():
        try:
            progress = json.loads(progress_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MigrationError("migration progress cannot be read") from exc
        if progress.get("version") != VERSION or progress.get("generation") != manifest["generation"]:
            raise MigrationError("migration progress does not match manifest")
        if progress.get("complete") not in {False, True}:
            raise MigrationError("migration progress has an invalid completion state")
        if progress.get("complete") is True and progress.get("manifest_sha256") != manifest_sha256(manifest):
            raise MigrationError("completed progress belongs to a different manifest")
        completed = set(progress.get("verified", []))
    allowed = {item["scope"] + ":" + item["sha256"] for item in objects}
    if not completed <= allowed:
        raise MigrationError("migration progress contains unknown objects")
    for item in objects:
        key = item["scope"] + ":" + item["sha256"]
        destination = _binding(target, item["scope"]).store
        if key in completed:
            destination.get_verified(item["sha256"], item["bytes"])
            continue
        raw = _binding(source, item["scope"]).store.get_verified(item["sha256"], item["bytes"])
        destination.put_if_absent(item["sha256"], raw)
        destination.get_verified(item["sha256"], item["bytes"])
        completed.add(key)
        if progress_file is not None:
            _atomic_json(progress_file, {"version": VERSION, "generation": manifest["generation"],
                                         "complete": False, "verified": sorted(completed)})
    verify_manifest(manifest, source, target, verify_sources=True)
    result = {"version": VERSION, "generation": manifest["generation"], "complete": True,
              "verified": sorted(completed), "manifest_sha256": manifest_sha256(manifest)}
    if progress_file is not None:
        _atomic_json(progress_file, result)
    return result


def verify_manifest(manifest: Mapping, source: Mapping[str, StoreBinding], target: Mapping[str, StoreBinding],
                    *, verify_sources: bool = False) -> None:
    for item in validate_manifest(manifest, source, target):
        if verify_sources:
            _binding(source, item["scope"]).store.get_verified(item["sha256"], item["bytes"])
        _binding(target, item["scope"]).store.get_verified(item["sha256"], item["bytes"])


def manifest_sha256(manifest: Mapping) -> str:
    validate_manifest(manifest)
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def manifest_diff(baseline: Mapping, candidate: Mapping) -> dict:
    """Return the explicit second-inventory delta; this never performs cutover."""
    before = {(item["scope"], item["sha256"], item["bytes"])
              for item in validate_manifest(baseline)}
    after = {(item["scope"], item["sha256"], item["bytes"])
             for item in validate_manifest(candidate)}
    return {
        "baseline_generation": baseline["generation"],
        "candidate_generation": candidate["generation"],
        "added": [list(item) for item in sorted(after - before)],
        "missing": [list(item) for item in sorted(before - after)],
        "unchanged": len(before & after),
    }


def assert_cutover_ready(baseline: Mapping, candidate: Mapping, completion: Mapping,
                         source: Mapping[str, StoreBinding], target: Mapping[str, StoreBinding]) -> dict:
    """Prove a quiescent second manifest was copied; do not switch configuration."""
    delta = manifest_diff(baseline, candidate)
    if delta["missing"]:
        raise MigrationError("second inventory lost source references")
    if (not isinstance(completion, Mapping) or completion.get("complete") is not True
            or completion.get("generation") != candidate.get("generation")
            or completion.get("manifest_sha256") != manifest_sha256(candidate)):
        raise MigrationError("candidate manifest is not wholly copied and verified")
    verify_manifest(candidate, source, target, verify_sources=True)
    return delta


def references_from_sqlite(path: Path | str, *, kind: str, scope: str | None = None) -> list[dict]:
    """Enumerate supported durable references from a frozen SQLite snapshot."""
    path = Path(path)
    result = []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        if kind == "document_catalog":
            fixed = _scope(scope)
            rows = db.execute("SELECT document_version_id,source_sha256,byte_length FROM document_versions ORDER BY document_version_id")
            for row in rows:
                result.append({"scope": fixed, "sha256": row["source_sha256"], "bytes": row["byte_length"],
                               "metadata_references": [f"{path.name}:document_versions:{row['document_version_id']}"]})
        elif kind == "source_catalog":
            fixed = _scope(scope)
            rows = db.execute("SELECT archive_version_id,source_sha256,byte_length FROM source_versions ORDER BY archive_version_id")
            for row in rows:
                result.append({"scope": fixed, "sha256": row["source_sha256"], "bytes": row["byte_length"],
                               "metadata_references": [f"{path.name}:source_versions:{row['archive_version_id']}"]})
        elif kind == "native_registry":
            rows = db.execute("SELECT s.snapshot_id,s.owner,s.payload_json,s.payload_sha256,p.owner AS storage_owner,"
                              "p.backend,p.backend_identity FROM native_sources s "
                              "JOIN native_snapshot_storage p USING(snapshot_id) ORDER BY s.snapshot_id")
            for row in rows:
                if row["backend"] != "object_store":
                    continue
                raw_payload = row["payload_json"]
                if (row["storage_owner"] != row["owner"] or not row["backend_identity"]
                        or hashlib.sha256(raw_payload.encode()).hexdigest() != row["payload_sha256"]):
                    raise MigrationError("native snapshot provenance is invalid")
                try:
                    payload = json.loads(raw_payload)
                except (TypeError, ValueError) as exc:
                    raise MigrationError("native snapshot metadata is invalid") from exc
                owner = row["owner"]
                object_scope = "native:public" if owner == "" else "native:private:" + owner
                result.append({"scope": object_scope, "sha256": payload.get("sha256"), "bytes": payload.get("byte_length"),
                               "metadata_references": [f"{path.name}:native_sources:{row['snapshot_id']}"]})
        else:
            raise MigrationError("unsupported object reference catalog kind")
    return [_reference(item) for item in result]
