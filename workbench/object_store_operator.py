"""Operator-only assembly for complete ObjectStore backup, migration, and recovery.

This module is deliberately outside runtime composition.  Every path, storage
configuration, output, and quiescence assertion is explicit.  It neither starts
services nor changes active configuration, and never deletes source data.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Mapping

from .backup import BackupError, create_backup, restore_backup, verify_backup
from .object_migration import (
    MigrationError, StoreBinding, assert_cutover_ready, build_manifest,
    copy_manifest, manifest_sha256, references_from_sqlite,
)
from .raw_storage import LocalObjectStore
from .runtime_config import RuntimePaths
from .storage_config import StorageConfiguration, StorageConfigError


VERSION = "object-store-operator/1"
PROOF_VERSION = "object-store-cutover-proof/2"
_OWNER = re.compile(r"[^\x00]{1,200}\Z")


class OperatorError(RuntimeError):
    pass


def _ro(path: Path):
    return sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)


def _tables(path: Path) -> set[str]:
    with closing(_ro(path)) as db:
        return {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}


def _valid_owner(value: object, *, public=False) -> str:
    if public and value == "":
        return ""
    if not isinstance(value, str) or _OWNER.fullmatch(value) is None or value != value.strip():
        raise OperatorError("metadata contains a malformed storage owner")
    return value


def _conversations(chat: Path) -> tuple[str, ...]:
    if not chat.is_file() or "conversations" not in _tables(chat):
        raise OperatorError("chat database is missing the conversations namespace")
    with closing(_ro(chat)) as db:
        values = tuple(row[0] for row in db.execute(
            "SELECT conversation_id FROM conversations ORDER BY conversation_id"))
    if len(values) != len(set(values)):
        raise OperatorError("duplicate conversation namespace")
    return tuple(_valid_owner(value) for value in values)


def _native_owners(registry: Path, conversations: set[str]) -> tuple[str, ...]:
    if not registry.is_file():
        return ()
    required = {"native_sources", "native_snapshot_storage"}
    if not required <= _tables(registry):
        raise OperatorError("native registry is incomplete")
    with closing(_ro(registry)) as db:
        rows = db.execute(
            "SELECT s.owner,p.owner,p.backend,p.backend_identity "
            "FROM native_sources s LEFT JOIN native_snapshot_storage p USING(snapshot_id) "
            "ORDER BY s.snapshot_id"
        ).fetchall()
    owners = set()
    for source_owner, storage_owner, backend, identity in rows:
        owner = _valid_owner(source_owner, public=True)
        if storage_owner != owner or backend not in {"local_archive", "object_store"} or not identity:
            raise OperatorError("native source storage provenance is incomplete")
        if owner and owner not in conversations:
            raise OperatorError("native source owner is not an authorized conversation")
        if backend == "object_store":
            owners.add(owner)
    return tuple(sorted(owners))


def _store_identity(config: StorageConfiguration, scope: str) -> str:
    base = f"s3:{config.config_sha256}"
    if scope == "public":
        return f"{base}:raw/public/{config.deployment}"
    if scope.startswith("private:"):
        owner = scope.removeprefix("private:")
        return f"{base}:raw/private/{config.deployment}/{hashlib.sha256(owner.encode()).hexdigest()}"
    if scope == "native:public":
        return f"{base}:native-sql/public/{config.deployment}"
    if scope.startswith("native:private:"):
        owner = scope.removeprefix("native:private:")
        return f"{base}:native-sql/private/{config.deployment}/{hashlib.sha256(owner.encode()).hexdigest()}"
    raise OperatorError("unexpected object namespace")


def _validate_native_backend_identity(registry: Path, config: StorageConfiguration) -> None:
    if not registry.is_file():
        return
    with closing(_ro(registry)) as db:
        identities = {row[0] for row in db.execute(
            "SELECT DISTINCT backend_identity FROM native_snapshot_storage WHERE backend='object_store'")}
    if identities and identities != {config.native_store_identity}:
        raise OperatorError("native source backend identity differs from the configured source store")


def configured_bindings(config: StorageConfiguration, *, conversations=(), native_owners=()) -> dict[str, StoreBinding]:
    """Build exact non-secret bindings for only metadata-authorized namespaces."""
    if not isinstance(config, StorageConfiguration):
        raise OperatorError("a validated storage configuration is required")
    conversation_set = set(conversations)
    owner_set = set(native_owners)
    for value in conversation_set | (owner_set - {""}):
        _valid_owner(value)
    if not (owner_set - {""}) <= conversation_set:
        raise OperatorError("native source owner is not an authorized conversation")
    result = {
        "public": StoreBinding("s3", _store_identity(config, "public"), config.public_store()),
    }
    for owner in sorted(conversation_set):
        scope = "private:" + owner
        result[scope] = StoreBinding("s3", _store_identity(config, scope), config.private_store(owner))
    for owner in sorted(owner_set):
        scope = "native:public" if owner == "" else "native:private:" + owner
        store = config.native_public_store() if owner == "" else config.native_private_store(owner)
        result[scope] = StoreBinding("s3", _store_identity(config, scope), store)
    return result


def assemble_configured_bindings(paths: RuntimePaths, config: StorageConfiguration) -> dict[str, StoreBinding]:
    """Derive every configured binding from authoritative runtime metadata."""
    conversations = _conversations(paths.chat)
    native_owners = _native_owners(paths.native_registry, set(conversations))
    _validate_native_backend_identity(paths.native_registry, config)
    return configured_bindings(config, conversations=conversations, native_owners=native_owners)


def local_bindings(paths: RuntimePaths, *, conversations=(), native_owners=()) -> dict[str, StoreBinding]:
    """Build bindings for existing local raw stores; local native archives stay tree resources."""
    result = {
        "public": StoreBinding("local", "local:" + str(paths.documents_root),
                               LocalObjectStore(paths.documents_root)),
    }
    for owner in conversations:
        _valid_owner(owner)
        location = paths.materials_root / hashlib.sha256(owner.encode()).hexdigest()
        result["private:" + owner] = StoreBinding("local", "local:" + str(location),
                                                   LocalObjectStore(location))
    if native_owners:
        raise OperatorError("object-backed native sources require their registered configured source store")
    return result


def _resource(name: str, source: Path, kind: str, **extra) -> dict:
    return {"name": name, "source": str(source), "kind": kind, **extra}


def build_backup_spec(paths: RuntimePaths, *, rebuild_raw_root: Path,
                      preserve_rebuild_files: tuple[Path, ...] | list[Path] = (),
                      terms_pages: tuple[Path, ...] | list[Path] = ()) -> tuple[dict, dict]:
    """Inventory exact runtime metadata and authorized material/native roots read-only."""
    if not isinstance(paths, RuntimePaths):
        raise OperatorError("validated runtime paths are required")
    conversations = _conversations(paths.chat)
    conversation_set = set(conversations)
    resources = []
    for name, path in (("evidence", paths.evidence), ("chat", paths.chat),
                       ("wiki", paths.wiki), ("sample", paths.sample)):
        if not path.is_file():
            raise OperatorError(f"required application database is missing: {name}")
        resources.append(_resource(f"state/{name}.sqlite3", path, "sqlite"))
    if paths.auth is not None:
        if not paths.auth.is_file():
            raise OperatorError("configured authentication database is missing")
        resources.append(_resource("state/auth.sqlite3", paths.auth, "sqlite"))

    references = []
    if paths.documents_root.is_dir():
        databases = [name for name in ("catalog.sqlite3", "source_acquisitions.sqlite3")
                     if (paths.documents_root / name).is_file()]
        if "catalog.sqlite3" not in databases:
            raise OperatorError("documents root exists without its catalog")
        resources.append(_resource("state/documents", paths.documents_root, "tree", databases=databases))
        references.append({"resource": "state/documents/catalog.sqlite3",
                           "kind": "document_catalog", "scope": "public"})
        if "source_acquisitions.sqlite3" in databases:
            references.append({"resource": "state/documents/source_acquisitions.sqlite3",
                               "kind": "source_catalog", "scope": "public"})
    else:
        raise OperatorError("documents root is missing")

    expected_material_dirs = {hashlib.sha256(owner.encode()).hexdigest(): owner for owner in conversations}
    actual_material_dirs = set()
    if paths.materials_root.exists():
        if not paths.materials_root.is_dir() or paths.materials_root.is_symlink():
            raise OperatorError("private materials root is invalid")
        actual_material_dirs = {item.name for item in paths.materials_root.iterdir() if item.is_dir()}
        unexpected = actual_material_dirs - set(expected_material_dirs)
        if unexpected:
            raise OperatorError("private materials contain an unexpected conversation namespace")
        unexpected_files = [item for item in paths.materials_root.iterdir()
                            if not item.is_dir() and item.name != ".object-storage-identity.json"]
        if unexpected_files:
            raise OperatorError("private materials root contains an unexpected entry")
    material_owners = []
    for digest in sorted(actual_material_dirs):
        owner = expected_material_dirs[digest]
        root = paths.materials_root / digest
        databases = [name for name in ("catalog.sqlite3", "source_acquisitions.sqlite3")
                     if (root / name).is_file()]
        if "catalog.sqlite3" not in databases:
            raise OperatorError("conversation materials root has no catalog")
        name = "state/materials/" + digest
        resources.append(_resource(name, root, "tree", databases=databases))
        references.append({"resource": name + "/catalog.sqlite3", "kind": "document_catalog",
                           "scope": "private:" + owner})
        if "source_acquisitions.sqlite3" in databases:
            references.append({"resource": name + "/source_acquisitions.sqlite3", "kind": "source_catalog",
                               "scope": "private:" + owner})
        material_owners.append(owner)

    native_owners = _native_owners(paths.native_registry, conversation_set)
    if paths.native_registry.is_file():
        resources.append(_resource("state/native", paths.native_registry.parent, "tree",
                                   databases=[paths.native_registry.name], exclude=["snapshot-cache"]))
        references.append({"resource": "state/native/" + paths.native_registry.name,
                           "kind": "native_registry"})
    elif paths.native_registry.parent.exists():
        raise OperatorError("native data root exists without its registry")

    raw = Path(rebuild_raw_root).resolve()
    if not raw.is_dir() or raw.is_symlink():
        raise OperatorError("explicit raw rebuild root is missing or invalid")
    requested = tuple((Path(item), "preserved") for item in preserve_rebuild_files) + tuple(
        (Path(item), "terms") for item in terms_pages)
    preserved = []
    for supplied, category in requested:
        candidate = supplied.expanduser()
        try:
            relative = candidate.absolute().relative_to(raw)
        except ValueError as exc:
            raise OperatorError("preserved rebuild files must be inside the raw rebuild root") from exc
        if relative == Path(".") or any(part in {"", ".", ".."} for part in relative.parts):
            raise OperatorError("preserved rebuild file path is invalid")
        current = raw
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise OperatorError("preserved rebuild files and their parents must not be symbolic links")
        if not current.is_file():
            raise OperatorError("preserved rebuild file must be a regular file")
        preserved.append((relative.as_posix(), current.resolve(), category))
    if len({relative for relative, _, _ in preserved}) != len(preserved):
        raise OperatorError("preserved rebuild files must be distinct")
    resources.append(_resource("rebuild/data-raw-core", raw, "tree",
                               exclude=sorted(relative for relative, _, _ in preserved)))
    for relative, item, category in sorted(preserved):
        resources.append(_resource(f"rebuild/{category}/" + relative, item, "file"))

    spec = {"version": "workbench-backup-spec/1", "resources": resources,
            "object_references": references}
    inventory = {"version": VERSION, "databases": sorted(
        resource["source"] for resource in resources if resource["kind"] == "sqlite") + sorted(
        str(Path(resource["source"]) / database) for resource in resources
        for database in resource.get("databases", [])),
        "conversations": list(conversations), "material_owners": material_owners,
        "native_object_owners": list(native_owners), "object_scopes": sorted(
            {descriptor.get("scope") for descriptor in references if descriptor.get("scope")}
            | ({"native:public" if owner == "" else "native:private:" + owner for owner in native_owners})),
        "cache_excluded": [str(paths.native_registry.parent / "snapshot-cache")]
                         if paths.native_registry.is_file() else [],
        "rebuild_resources": ["rebuild/data-raw-core", *[
            f"rebuild/{category}/" + relative for relative, _, category in sorted(preserved)]]}
    return spec, inventory


def references_from_backup(backup_root: Path | str) -> list[dict]:
    manifest = verify_backup(backup_root)
    objects = manifest.get("objects", [])
    if not objects:
        raise OperatorError("backup has no frozen object inventory")
    return [{key: item[key] for key in ("scope", "sha256", "bytes", "metadata_references")}
            for item in objects]


def verify_quiescence(*, writers_stopped: bool, writer_pids=(), lock_path: Path | None = None):
    """Validate operator attestation and observable barriers without claiming to stop writers."""
    if writers_stopped is not True:
        raise OperatorError("explicit --writers-stopped attestation is required")
    for raw in writer_pids:
        try:
            pid = int(raw)
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except (ValueError, PermissionError) as exc:
            raise OperatorError("writer process identity cannot be verified") from exc
        raise OperatorError(f"attested writer process is still active: pid {pid}")
    lock = None
    if lock_path is not None:
        path = Path(lock_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.open("a+")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock.close()
            raise OperatorError("operator quiescence lock is held by another process") from exc
        lock.seek(0)
        lock.truncate()
        lock.write(json.dumps({"pid": os.getpid(), "version": VERSION}) + "\n")
        lock.flush()
    return lock


def build_cutover_proof(baseline, candidate, completion, source, target) -> dict:
    delta = assert_cutover_ready(baseline, candidate, completion, source, target)
    mapping = {}
    for item in candidate["objects"]:
        scope = item["scope"]
        pair = {"source_backend": item["source_backend"], "source_identity": item["source_identity"],
                "target_backend": item["target_backend"], "target_identity": item["target_identity"]}
        if scope in mapping and mapping[scope] != pair:
            raise OperatorError("manifest has inconsistent namespace identity mapping")
        mapping[scope] = pair
    return {"version": PROOF_VERSION, "baseline_manifest_sha256": manifest_sha256(baseline),
            "candidate_manifest_sha256": manifest_sha256(candidate), "candidate_generation": candidate["generation"],
            "completion_manifest_sha256": completion["manifest_sha256"], "delta": delta,
            "identity_mapping": mapping, "candidate_manifest": candidate,
            "completion": completion}


def recovery_verifier(restored_root: Path, manifest: Mapping, bindings: Mapping[str, StoreBinding]) -> dict:
    """Read-only integrity/closure verification for frozen readers and all restored objects."""
    root = Path(restored_root)
    spec = manifest.get("specification", {})
    chat_names = [item["name"] for item in spec.get("resources", [])
                  if item.get("kind") == "sqlite" and Path(item.get("source", "")).name == "chat.sqlite3"]
    if len(chat_names) != 1:
        raise OperatorError("restored snapshot does not identify exactly one chat database")
    chat = root / chat_names[0]
    reader_count = artifact_count = historical_incomplete = 0
    if {"report_reader_versions", "report_artifact_snapshots"} & _tables(chat):
        with closing(_ro(chat)) as db:
            db.row_factory = sqlite3.Row
            if "report_reader_versions" in _tables(chat):
                rows = db.execute("SELECT * FROM report_reader_versions ORDER BY report_id").fetchall()
                for row in rows:
                    try:
                        payload = json.loads(row["payload_json"])
                    except (TypeError, ValueError) as exc:
                        raise OperatorError("frozen reader payload is invalid") from exc
                    recorded = payload.pop("payload_sha256", None)
                    actual = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":")).encode()).hexdigest()
                    if recorded != row["payload_sha256"] or actual != recorded:
                        raise OperatorError("frozen reader payload hash is not reproducible")
                    reader_count += 1
                    artifact = db.execute("SELECT * FROM report_artifact_snapshots WHERE report_id=?",
                                          (row["report_id"],)).fetchone()
                    if artifact is None:
                        historical_incomplete += 1
                        continue
                    from .answer_artifacts import validate_artifact_snapshot
                    value = json.loads(artifact["artifact_json"])
                    validate_artifact_snapshot(value)
                    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    if hashlib.sha256(encoded.encode()).hexdigest() != artifact["artifact_sha256"]:
                        raise OperatorError("frozen artifact hash is not reproducible")
                    for key in ("artifact_id", "conversation_id", "message_id", "run_id"):
                        if value.get(key) != artifact[key]:
                            raise OperatorError("frozen artifact/source identity closure is broken")
                    if row["artifact_id"] != artifact["artifact_id"]:
                        raise OperatorError("reader artifact closure is broken")
                    artifact_count += 1
    verified_objects = 0
    for item in manifest.get("objects", []):
        binding = bindings.get(item["scope"])
        if not isinstance(binding, StoreBinding):
            raise OperatorError("recovery object binding is incomplete")
        binding.store.get_verified(item["sha256"], item["bytes"])
        verified_objects += 1
    return {"frozen_reader_source_identities_verified": True, "frozen_readers": reader_count,
            "artifact_closure_verified": artifact_count,
            "historical_incomplete_artifacts": historical_incomplete,
            "coverage_complete": historical_incomplete == 0, "verified_objects": verified_objects,
            "coverage_limitations": ([] if historical_incomplete == 0 else [
                f"{historical_incomplete} historical frozen reader records predate complete artifact snapshots; "
                "their exact payload bytes and hashes were preserved and verified, but artifact closure is unavailable."])}


def _paths(args) -> RuntimePaths:
    return RuntimePaths(evidence=args.evidence, chat=args.chat, wiki=args.wiki, sample=args.sample,
                        documents_root=args.documents_root, native_registry=args.native_registry,
                        auth=args.auth)


def _load_config(path: Path | None):
    return None if path is None else StorageConfiguration.load(path)


def _json_write(path: Path, value) -> None:
    if path.exists() or path.is_symlink():
        raise OperatorError("output file already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _normalize_recovered_materials(root: Path, manifest: dict) -> str:
    """Move the legacy backup layout to the runtime-derived materials path."""
    legacy = "state/materials"
    paths = RuntimePaths.resolve(root / "state/evidence.sqlite3",
                                 chat_db=root / "state/chat.sqlite3")
    canonical = paths.materials_root.relative_to(root).as_posix()
    source, target = root / legacy, root / canonical
    if not source.exists():
        return canonical
    if source.is_symlink() or not source.is_dir() or target.exists() or target.is_symlink():
        raise OperatorError("restored private metadata layout cannot be normalized")

    def normalized(value):
        if not isinstance(value, str):
            return value
        return canonical + value[len(legacy):] if value == legacy or value.startswith(legacy + "/") else value

    source.rename(target)
    for entry in manifest.get("files", []):
        entry["path"] = normalized(entry.get("path"))
    manifest["directories"] = [normalized(value) for value in manifest.get("directories", [])]
    specification = manifest.get("specification", {})
    for resource in specification.get("resources", []):
        resource["name"] = normalized(resource.get("name"))
    for reference in specification.get("object_references", []):
        reference["resource"] = normalized(reference.get("resource"))
    for item in manifest.get("objects", []):
        item["metadata_references"] = [normalized(value) for value in item.get("metadata_references", [])]
    return canonical


def register_recovered_metadata(root: Path, manifest: dict,
                                config: StorageConfiguration) -> list[str]:
    """Normalize and bind restored metadata to the verified recovery target."""
    private_root_name = _normalize_recovered_materials(root, manifest)
    objects = manifest.get("objects", [])
    recovery = manifest.get("recovery")
    registered = []
    resources = manifest.get("specification", {}).get("resources", [])
    resource_names = {item.get("name") for item in resources if isinstance(item, Mapping)}
    if "state/documents" in resource_names:
        config.register_recovered_root(root / "state/documents", scope="public",
                                       recovery=recovery, objects=objects)
        registered.append("state/documents/.object-storage-identity.json")
    if (any(str(item.get("scope", "")).startswith("private:") for item in objects)
            and any(name == private_root_name or (
                isinstance(name, str) and name.startswith(private_root_name + "/"))
                for name in resource_names)):
        config.register_recovered_root(root / private_root_name, scope="private",
                                       public_root=root / "state/documents",
                                       recovery=recovery, objects=objects)
        registered.append(private_root_name + "/.object-storage-identity.json")
    if not registered:
        raise OperatorError("recovery has no public or private metadata root to register")
    return registered


def _runtime_paths_args(parser):
    for name in ("evidence", "chat", "wiki", "sample", "documents-root", "native-registry"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--auth", type=Path)


def _common_paths(parser):
    _runtime_paths_args(parser)
    parser.add_argument("--raw-rebuild-root", type=Path, required=True)
    parser.add_argument("--preserve-rebuild-file", type=Path, action="append", default=[])
    parser.add_argument("--terms-page", type=Path, action="append", default=[],
                        help="compatibility alias for --preserve-rebuild-file")


def _bindings_for_manifest(args, value, *, target=False):
    scopes = {item["scope"] for item in value.get("objects", [])}
    conversations = sorted(scope.removeprefix("private:") for scope in scopes if scope.startswith("private:"))
    native = sorted("" if scope == "native:public" else scope.removeprefix("native:private:")
                    for scope in scopes if scope.startswith("native:"))
    option = args.target_storage_config if target else args.source_storage_config
    config = _load_config(option)
    if target:
        if config is None:
            raise OperatorError("target storage configuration is required")
        return configured_bindings(config, conversations=conversations, native_owners=native)
    if config is not None:
        return configured_bindings(config, conversations=conversations, native_owners=native)
    return local_bindings(_paths(args), conversations=conversations, native_owners=native)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inventory = commands.add_parser("inventory")
    _common_paths(inventory)
    inventory.add_argument("--spec-output", type=Path, required=True)
    inventory.add_argument("--inventory-output", type=Path, required=True)
    backup = commands.add_parser("backup")
    _common_paths(backup)
    backup.add_argument("--source-storage-config", type=Path)
    backup.add_argument("--output", type=Path, required=True)
    backup.add_argument("--writers-stopped", action="store_true", required=True)
    backup.add_argument("--writer-pid", action="append", default=[])
    backup.add_argument("--quiescence-lock", type=Path)
    manifest = commands.add_parser("manifest")
    manifest.add_argument("--backup", type=Path, required=True)
    _runtime_paths_args(manifest)
    manifest.add_argument("--source-storage-config", type=Path)
    manifest.add_argument("--target-storage-config", type=Path, required=True)
    manifest.add_argument("--generation", required=True)
    manifest.add_argument("--output", type=Path, required=True)
    copy = commands.add_parser("copy")
    copy.add_argument("--manifest", type=Path, required=True)
    _runtime_paths_args(copy)
    copy.add_argument("--source-storage-config", type=Path)
    copy.add_argument("--target-storage-config", type=Path, required=True)
    copy.add_argument("--progress", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--target-storage-config", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    proof = commands.add_parser("proof")
    proof.add_argument("--baseline", type=Path, required=True)
    proof.add_argument("--candidate", type=Path, required=True)
    proof.add_argument("--completion", type=Path, required=True)
    _runtime_paths_args(proof)
    proof.add_argument("--source-storage-config", type=Path)
    proof.add_argument("--target-storage-config", type=Path, required=True)
    proof.add_argument("--output", type=Path, required=True)
    proof.add_argument("--writers-stopped", action="store_true", required=True)
    proof.add_argument("--writer-pid", action="append", default=[])
    proof.add_argument("--quiescence-lock", type=Path)
    register = commands.add_parser("register")
    register.add_argument("--storage-config", type=Path, required=True)
    register.add_argument("--root", type=Path, required=True)
    register.add_argument("--scope", choices=("public", "private"), required=True)
    register.add_argument("--public-root", type=Path)
    register.add_argument("--proof", type=Path, required=True)
    register.add_argument("--original-identity", required=True)
    register.add_argument("--writers-stopped", action="store_true", required=True)
    register.add_argument("--writer-pid", action="append", default=[])
    register.add_argument("--quiescence-lock", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command in {"inventory", "backup"}:
            paths = _paths(args)
            spec, details = build_backup_spec(paths, rebuild_raw_root=args.raw_rebuild_root,
                                              preserve_rebuild_files=args.preserve_rebuild_file,
                                              terms_pages=args.terms_page)
            if args.command == "inventory":
                _json_write(args.spec_output, spec)
                _json_write(args.inventory_output, details)
                result = details
            else:
                lock = verify_quiescence(writers_stopped=args.writers_stopped,
                                         writer_pids=args.writer_pid, lock_path=args.quiescence_lock)
                try:
                    conversations = details["conversations"]
                    native = details["native_object_owners"]
                    config = _load_config(args.source_storage_config)
                    bindings = (local_bindings(paths, conversations=details["material_owners"], native_owners=native)
                                if config is None else assemble_configured_bindings(paths, config))
                    result = create_backup(spec, args.output, object_stores=bindings)
                finally:
                    if lock is not None:
                        lock.close()
        elif args.command == "manifest":
            frozen = references_from_backup(args.backup)
            backup_manifest = verify_backup(args.backup)
            source = _bindings_for_manifest(args, {"objects": backup_manifest["objects"]})
            target = _bindings_for_manifest(args, {"objects": backup_manifest["objects"]}, target=True)
            result = build_manifest(frozen, source, target, generation=args.generation)
            _json_write(args.output, result)
        elif args.command == "copy":
            value = json.loads(args.manifest.read_text(encoding="utf-8"))
            source = _bindings_for_manifest(args, value)
            target = _bindings_for_manifest(args, value, target=True)
            result = copy_manifest(value, source, target, progress_path=args.progress)
        elif args.command == "proof":
            lock = verify_quiescence(writers_stopped=args.writers_stopped,
                                     writer_pids=args.writer_pid, lock_path=args.quiescence_lock)
            try:
                baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
                candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
                completion = json.loads(args.completion.read_text(encoding="utf-8"))
                source = _bindings_for_manifest(args, candidate)
                target = _bindings_for_manifest(args, candidate, target=True)
                result = build_cutover_proof(baseline, candidate, completion, source, target)
                _json_write(args.output, result)
            finally:
                if lock is not None:
                    lock.close()
        elif args.command == "register":
            lock = verify_quiescence(writers_stopped=args.writers_stopped,
                                     writer_pids=args.writer_pid, lock_path=args.quiescence_lock)
            try:
                config = StorageConfiguration.load(args.storage_config)
                value = json.loads(args.proof.read_text(encoding="utf-8"))
                config.register_migrated_root(args.root, scope=args.scope, proof=value,
                                              original_identity=args.original_identity,
                                              public_root=args.public_root)
                result = {"registered": True}
            finally:
                if lock is not None:
                    lock.close()
        else:
            backup_manifest = verify_backup(args.backup)
            scopes = sorted({item["scope"] for item in backup_manifest.get("objects", [])})
            conversations = sorted({scope.split(":", 1)[1] for scope in scopes if scope.startswith("private:")})
            native = sorted({"" if scope == "native:public" else scope.removeprefix("native:private:")
                             for scope in scopes if scope.startswith("native:")})
            target_config = _load_config(args.target_storage_config)
            target = configured_bindings(target_config, conversations=conversations,
                                         native_owners=native)
            result = restore_backup(
                args.backup, args.output, object_stores=target, isolated_object_target=True,
                recovery_verifier=recovery_verifier,
                recovery_registrar=lambda root, manifest: register_recovered_metadata(
                    root, manifest, target_config))
        print(json.dumps({"status": "ok", "operation": args.command,
                          "objects": len(result.get("objects", [])) if isinstance(result, dict) else 0}))
        return 0
    except (OperatorError, BackupError, MigrationError, StorageConfigError,
            OSError, sqlite3.Error, ValueError, KeyError) as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
