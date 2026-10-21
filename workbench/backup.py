"""Explicit local backup/restore for quiescent workspaces; no model or network IO.

SQLite metadata is snapshotted through SQLite. Raw objects (including native
SQLite source files) are copied byte-for-byte, never guessed from extensions.
A manifest is written last. A failed operation leaves an incomplete new directory,
never overwrites or deletes an existing destination. Not a scheduler or encryption.
"""
from contextlib import ExitStack, closing
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import sys
import time

from .object_migration import MigrationError, StoreBinding, references_from_sqlite


VERSION = "workbench-backup/1"


class BackupError(ValueError):
    pass


def _path(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise BackupError("invalid relative backup path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", ".."} for part in value.split("/")) or str(path) != value:
        raise BackupError("invalid relative backup path")
    if path.parts[0] in {"manifest.json", "manifest.pending.json"}:
        raise BackupError("reserved manifest path")
    return path


def _safe(path):
    path = Path(path).absolute()
    for item in [*reversed(path.parents), path]:
        if item.is_symlink():
            raise BackupError("symlinks are not allowed in backup paths")
    return path.resolve()


def _sync_directories(root):
    for path in [*sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True), root, root.parent]:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _stamp(path):
    value = path.stat(follow_symlinks=False)
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _sqlite_state(path):
    # data_version detects SQLite commits, not direct in-place file replacement.
    result = {}
    for candidate in [path, Path(str(path) + "-wal"), Path(str(path) + "-journal")]:
        _safe(candidate)
        if candidate.exists():
            result[candidate.name] = (_stamp(candidate), _hash(candidate))
    return result


def _copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with source.open("rb") as incoming, target.open("xb") as outgoing:
        os.chmod(target, 0o600)
        while chunk := incoming.read(1024 * 1024):
            outgoing.write(chunk)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def _check_database(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise BackupError("SQLite integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("SQLite foreign-key check failed")


def _inventory(spec):
    if not isinstance(spec, dict) or spec.get("version") != "workbench-backup-spec/1":
        raise BackupError("invalid backup specification")
    resources = spec.get("resources")
    if not isinstance(resources, list) or not resources:
        raise BackupError("explicit nonempty resources are required")
    for absent in spec.get("absent_optional", []):
        if _safe(absent).exists():
            raise BackupError("a resource declared absent exists; correct the specification")
    files, directories, roots = {}, set(), []
    for resource in resources:
        name = str(_path(resource["name"]))
        source = _safe(resource["source"])
        if not source.is_absolute() or not source.exists():
            raise BackupError("required backup resource is missing")
        if any(name == old or name.startswith(old + "/") or old.startswith(name + "/") for old, _ in roots):
            raise BackupError("overlapping backup resource names")
        roots.append((name, source))
        kind = resource["kind"]
        if kind in {"file", "sqlite"}:
            if not source.is_file():
                raise BackupError("resource must be a regular file")
            files[name] = (source, kind)
            continue
        if kind != "tree" or not source.is_dir():
            raise BackupError("invalid backup resource kind")
        databases = {str(_path(item)) for item in resource.get("databases", [])}
        excludes = {str(_path(item)) for item in resource.get("exclude", [])}
        if any(item in databases or any(db.startswith(item + "/") for db in databases) for item in excludes):
            raise BackupError("excluded paths cannot contain declared SQLite metadata")
        sidecars = {item + suffix for item in databases for suffix in ("-wal", "-shm", "-journal")}
        directories.add(name)
        found_databases = set()
        for item in sorted(source.rglob("*")):
            _safe(item)
            relative = item.relative_to(source).as_posix()
            if any(relative == excluded or relative.startswith(excluded + "/") for excluded in excludes):
                continue
            target = name + "/" + relative
            mode = item.stat(follow_symlinks=False).st_mode
            if stat.S_ISDIR(mode):
                directories.add(target)
            elif stat.S_ISREG(mode):
                if relative in sidecars:
                    continue
                if relative in databases:
                    found_databases.add(relative)
                files[target] = (item, "sqlite" if relative in databases else "file")
            else:
                raise BackupError("special files cannot be backed up")
        if found_databases != databases:
            raise BackupError("declared SQLite metadata is missing")
    return files, directories, roots


def _destination(output, roots):
    output = _safe(output)
    if not output.parent.is_dir():
        raise BackupError("destination parent must exist")
    for _, source in roots:
        if output == source or source in output.parents or output in source.parents:
            raise BackupError("destination overlaps a source resource")
    output.mkdir(mode=0o700)  # Exclusive reservation; never merge or overwrite.
    return output


def _object_references(spec, output):
    descriptors = spec.get("object_references", [])
    if not isinstance(descriptors, list):
        raise BackupError("object_references must be a list")
    references = []
    for descriptor in descriptors:
        if not isinstance(descriptor, dict) or set(descriptor) - {"resource", "kind", "scope"}:
            raise BackupError("invalid object reference descriptor")
        resource = str(_path(descriptor.get("resource")))
        database = _safe(output / resource)
        if not database.is_file():
            raise BackupError("object reference database is not a snapshotted resource")
        try:
            found = references_from_sqlite(database, kind=descriptor.get("kind"), scope=descriptor.get("scope"))
        except (MigrationError, sqlite3.Error) as exc:
            raise BackupError("object references cannot be enumerated from the frozen metadata") from exc
        for item in found:
            item["metadata_references"] = [resource + ":" + value.split(":", 1)[1]
                                           for value in item["metadata_references"]]
        references.extend(found)
    return references


def _copy_registered_objects(spec, output, manifest, object_stores):
    references = _object_references(spec, output)
    if not references:
        if object_stores:
            raise BackupError("object stores were supplied but no metadata reference catalogs were declared")
        return
    if not isinstance(object_stores, dict):
        raise BackupError("exact object store bindings are required")
    combined = {}
    for item in references:
        key = item["scope"], item["sha256"]
        old = combined.get(key)
        if old:
            if old["bytes"] != item["bytes"]:
                raise BackupError("metadata has conflicting object sizes")
            old["metadata_references"] = sorted(set(old["metadata_references"] + item["metadata_references"]))
        else:
            combined[key] = item
    manifest["objects"] = []
    for (scope, digest), item in sorted(combined.items()):
        binding = object_stores.get(scope)
        if not isinstance(binding, StoreBinding):
            raise BackupError(f"no exact object store binding for scope {scope!r}")
        try:
            raw = binding.store.get_verified(digest, item["bytes"])
        except Exception as exc:
            raise BackupError("a registered source object is missing or corrupt") from exc
        scope_id = hashlib.sha256(scope.encode()).hexdigest()
        name = f"object-payloads/{scope_id}/{digest}"
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with target.open("xb") as stream:
            os.chmod(target, 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if _hash(target) != digest:
            raise BackupError("copied object failed verification")
        manifest["files"].append({"path": name, "kind": "file", "bytes": item["bytes"], "sha256": digest})
        manifest["objects"].append({**item, "path": name, "source_backend": binding.backend,
                                    "source_identity": binding.identity,
                                    "target_backend": "backup-directory", "target_identity": str(output)})


def create_backup(spec, output, *, object_stores=None):
    files, directories, roots = _inventory(spec)
    output = _destination(output, roots)
    initial = {name: _stamp(path) for name, (path, kind) in files.items() if kind == "file"}
    manifest = {"version": VERSION, "specification": spec, "directories": sorted(directories), "files": []}
    with ExitStack() as stack:
        readers = {}
        for name, (source, kind) in files.items():
            if kind == "sqlite":
                db = stack.enter_context(closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)))
                readers[name] = (db, db.execute("PRAGMA data_version").fetchone()[0], _sqlite_state(source))
        for name in sorted(directories):
            (output / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, (source, kind) in sorted(files.items()):
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if kind == "sqlite":
                # Reserve the file, then snapshot committed pages including WAL.
                target.touch(mode=0o600, exist_ok=False)
                deadline = time.monotonic() + 60
                def progress(status, remaining, total):
                    if time.monotonic() > deadline:
                        raise BackupError("SQLite snapshot timed out; stop writers and retry")
                with closing(sqlite3.connect(target)) as writer:
                    readers[name][0].backup(writer, pages=1024, progress=progress, sleep=0.05)
                _check_database(target)
                with target.open("rb") as handle:
                    os.fsync(handle.fileno())
            else:
                _copy(source, target)
                if _stamp(source) != initial[name] or _hash(source) != _hash(target):
                    raise BackupError("source changed during backup")
            manifest["files"].append({"path": name, "kind": kind, "bytes": target.stat().st_size, "sha256": _hash(target)})
        _copy_registered_objects(spec, output, manifest, object_stores)
        current_files, current_dirs, current_roots = _inventory(spec)
        if files != current_files or directories != current_dirs or roots != current_roots:
            raise BackupError("resource inventory changed during backup")
        for name, (source, kind) in files.items():
            if kind == "file" and _stamp(source) != initial[name]:
                raise BackupError("source changed during backup")
            if kind == "sqlite":
                db, version, initial_state = readers[name]
                if db.execute("PRAGMA data_version").fetchone()[0] != version or _sqlite_state(source) != initial_state:
                    raise BackupError("SQLite changed during backup; stop writers and retry")
        for entry in manifest.get("objects", []):
            binding = object_stores.get(entry["scope"])
            if (not isinstance(binding, StoreBinding) or binding.backend != entry["source_backend"]
                    or binding.identity != entry["source_identity"]):
                raise BackupError("source object-store identity changed during backup")
            try:
                binding.store.get_verified(entry["sha256"], entry["bytes"])
            except Exception as exc:
                raise BackupError("a registered source object changed during backup") from exc
    _publish_manifest(output, manifest)
    return manifest


def _publish_manifest(root, manifest):
    pending = root / "manifest.pending.json"
    complete = root / "manifest.json"
    with pending.open("x", encoding="utf-8") as stream:
        os.chmod(pending, 0o600)
        json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    verify_backup(root, _manifest_name=pending.name)
    _sync_directories(root)
    published = False
    try:
        os.link(pending, complete)
        published = True
        pending.unlink()
        _sync_directories(root)
    except BaseException:
        if published:
            complete.unlink(missing_ok=True)
        raise


def verify_backup(root, *, _manifest_name="manifest.json"):
    root = _safe(root)
    manifest_path = _safe(root / _manifest_name)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("version") != VERSION or not isinstance(manifest.get("files"), list) or not manifest["files"]:
        raise BackupError("invalid or incomplete backup manifest")
    expected = {_manifest_name}
    for entry in manifest["files"]:
        name = str(_path(entry["path"]))
        if name in expected or entry["kind"] not in {"sqlite", "file"}:
            raise BackupError("duplicate path or invalid file kind")
        expected.add(name)
        path = _safe(root / name)
        if not path.is_file() or path.stat().st_size != entry["bytes"] or _hash(path) != entry["sha256"]:
            raise BackupError("backup file is missing or has changed")
        if entry["kind"] == "sqlite":
            _check_database(path)
    object_paths = set()
    for entry in manifest.get("objects", []):
        try:
            name = str(_path(entry["path"]))
            if (entry["sha256"] != _hash(root / name) or entry["bytes"] != (root / name).stat().st_size
                    or not isinstance(entry["scope"], str) or not entry["scope"]
                    or not isinstance(entry["metadata_references"], list) or not entry["metadata_references"]
                    or any(not isinstance(value, str) or not value for value in entry["metadata_references"])
                    or not isinstance(entry["source_backend"], str) or not entry["source_backend"]
                    or not isinstance(entry["source_identity"], str) or not entry["source_identity"]
                    or entry["target_backend"] != "backup-directory"
                    or not isinstance(entry["target_identity"], str) or not entry["target_identity"]):
                raise BackupError("backup object manifest does not match its payload")
        except (KeyError, TypeError, OSError) as exc:
            raise BackupError("invalid backup object manifest") from exc
        if name in object_paths:
            raise BackupError("duplicate backup object payload")
        object_paths.add(name)
    listed_object_paths = {entry["path"] for entry in manifest["files"] if entry["path"].startswith("object-payloads/")}
    if listed_object_paths != object_paths:
        raise BackupError("backup object closure is incomplete")
    expected_directories = {parent.as_posix() for name in expected for parent in PurePosixPath(name).parents if parent.as_posix() != "."}
    for name in manifest.get("directories", []):
        relative = _path(name)
        expected_directories.add(str(relative))
        expected_directories.update(parent.as_posix() for parent in relative.parents if parent.as_posix() != ".")
        if not _safe(root / str(_path(name))).is_dir():
            raise BackupError("backup directory is missing")
    actual, actual_directories = set(), set()
    for path in root.rglob("*"):
        _safe(path)
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
        elif path.is_dir():
            actual_directories.add(path.relative_to(root).as_posix())
        else:
            raise BackupError("special file in backup")
    if actual != expected or actual_directories != expected_directories:
        raise BackupError("unlisted files in backup")
    return manifest


def restore_backup(root, output, *, object_stores=None, isolated_object_target=False,
                   recovery_verifier=None, recovery_registrar=None):
    root = _safe(root)
    manifest = verify_backup(root)
    output = _destination(output, [("backup", root)])
    for name in manifest.get("directories", []):
        (output / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    for entry in manifest["files"]:
        _copy(root / entry["path"], output / entry["path"])
    objects = manifest.get("objects", [])
    if objects:
        if isolated_object_target is not True or not isinstance(object_stores, dict):
            raise BackupError("object restore requires an explicitly isolated new target namespace")
        for entry in objects:
            binding = object_stores.get(entry["scope"])
            if not isinstance(binding, StoreBinding) or binding.identity == entry["source_identity"]:
                raise BackupError("restore target is missing, not isolated, or matches the source identity")
            raw = (root / entry["path"]).read_bytes()
            try:
                binding.store.put_if_absent(entry["sha256"], raw)
                binding.store.get_verified(entry["sha256"], entry["bytes"])
            except Exception as exc:
                raise BackupError("restored object could not be written and verified") from exc
        if not callable(recovery_verifier):
            raise BackupError("isolated recovery requires a frozen reader/source identity verifier")
        try:
            proof = recovery_verifier(output, manifest, object_stores)
        except Exception as exc:
            raise BackupError("frozen reader/source identity verification failed") from exc
        if (not isinstance(proof, dict)
                or proof.get("frozen_reader_source_identities_verified") is not True):
            raise BackupError("frozen reader/source identity verification did not succeed")
        manifest = json.loads(json.dumps(manifest))
        manifest["recovery"] = {"model_enabled": False, "worker_enabled": False,
                                "source_backup": str(root), "verification": proof,
                                "target_identities": {scope: binding.identity for scope, binding in object_stores.items()}}
        if recovery_registrar is not None:
            if not callable(recovery_registrar):
                raise BackupError("recovery registrar is invalid")
            try:
                registered = recovery_registrar(output, manifest)
            except Exception as exc:
                raise BackupError("verified recovery storage registration failed") from exc
            if (not isinstance(registered, list) or not registered
                    or any(not isinstance(name, str) for name in registered)
                    or len(set(registered)) != len(registered)):
                raise BackupError("recovery registrar did not identify its metadata files")
            by_path = {entry["path"]: entry for entry in manifest["files"]}
            for name in registered:
                relative = str(_path(name))
                path = _safe(output / relative)
                if not path.is_file():
                    raise BackupError("recovery registration metadata is missing")
                entry = {"path": relative, "kind": "file", "bytes": path.stat().st_size,
                         "sha256": _hash(path)}
                if relative in by_path:
                    by_path[relative].update(entry)
                else:
                    manifest["files"].append(entry)
            manifest["files"].sort(key=lambda entry: entry["path"])
    # Publish completion only after all restored files have been verified.
    _publish_manifest(output, manifest)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--spec", type=Path, required=True)
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--writers-stopped", action="store_true", required=True,
                        help="Operator confirms all known writers are stopped; not an automatic service lock")
    verify = sub.add_parser("verify")
    verify.add_argument("backup", type=Path)
    restore = sub.add_parser("restore")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            manifest = create_backup(json.loads(args.spec.read_text()), args.output)
        elif args.command == "verify":
            manifest = verify_backup(args.backup)
        else:
            manifest = restore_backup(args.backup, args.output)
        print(json.dumps({"status": "ok", "operation": args.command, "files": len(manifest["files"])}))
        return 0
    except (BackupError, OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        # Avoid including original database contents or credentials in errors.
        print(json.dumps({"status": "failed", "error": type(exc).__name__,
                          "message": str(exc) if isinstance(exc, BackupError) else "Check the specification, source files, and new destination permissions; existing destinations are never overwritten."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
