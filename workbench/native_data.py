"""Registered native snapshots and conversation-private, append-only SQL results.

Registration is an ingestion API for configured sources, never a research tool.
Research targets do not determine data permissions: public registered snapshots
and this conversation's private snapshots form the available source scope.
"""
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from typing import Callable

from .native_sql import MAX_SOURCE_BYTES, NativeSqlError, digest, encoded, execute
from .raw_storage import ObjectStore, RawStorageError
from .snapshot_cache import SnapshotCacheError, VerifiedSnapshotCache


VERSION = "native-data-registry/1"
_UNSET = object()
_SCHEMA = """
CREATE TABLE IF NOT EXISTS native_sources (
 snapshot_id TEXT PRIMARY KEY,
 source_key TEXT NOT NULL,
 owner TEXT NOT NULL,
 revision INTEGER NOT NULL,
 payload_json TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL,
 UNIQUE(source_key,owner,revision)
);
CREATE TABLE IF NOT EXISTS native_queries (
 owner TEXT NOT NULL,
 query_id TEXT NOT NULL,
 origin_run_id TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL,
 PRIMARY KEY(owner,query_id)
);
CREATE TABLE IF NOT EXISTS native_query_uses (
 owner TEXT NOT NULL,
 run_id TEXT NOT NULL,
 query_id TEXT NOT NULL,
 PRIMARY KEY(owner,run_id,query_id),
 FOREIGN KEY(owner,query_id) REFERENCES native_queries(owner,query_id)
);
CREATE TABLE IF NOT EXISTS native_snapshot_storage (
 snapshot_id TEXT PRIMARY KEY,
 owner TEXT NOT NULL,
 backend TEXT NOT NULL CHECK(backend IN ('local_archive','object_store')),
 backend_identity TEXT NOT NULL
);
"""


def _text(value, name, maximum=200):
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or len(value) > maximum or "\0" in value):
        raise NativeSqlError("invalid_native_input", f"{name} must be bounded, nonempty text")
    return value


class NativeDataService:
    def __init__(self, root, *, public_object_store: ObjectStore | None = None,
                 private_object_store_factory: Callable[[str], ObjectStore] | None = None,
                 object_store_max_bytes: int | None = None, object_store_identity: str | None = None,
                 snapshot_cache_bytes: int = 512 * 1024 * 1024):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.archive = self.root / "snapshots"
        self.archive.mkdir(exist_ok=True, mode=0o700)
        self.public_object_store = public_object_store
        self.private_object_store_factory = private_object_store_factory
        configured = public_object_store is not None or private_object_store_factory is not None
        if configured:
            if (type(object_store_max_bytes) is not int or object_store_max_bytes <= 0
                    or not isinstance(object_store_identity, str) or not object_store_identity
                    or len(object_store_identity) > 500):
                raise NativeSqlError("invalid_object_store", "Snapshot object storage requires explicit identity and byte limit")
            if public_object_store is not None and not isinstance(public_object_store, ObjectStore):
                raise NativeSqlError("invalid_object_store", "Public snapshot store does not implement ObjectStore")
        self.object_store_max_bytes = object_store_max_bytes
        self.object_store_identity = object_store_identity
        self.cache = VerifiedSnapshotCache(
            self.root / "snapshot-cache", max_bytes=snapshot_cache_bytes,
            max_entry_bytes=min(MAX_SOURCE_BYTES, object_store_max_bytes or MAX_SOURCE_BYTES))
        self.database = self.root / "registry.sqlite"
        with closing(self._connect()) as db:
            os.chmod(self.database, 0o600)
            db.execute("BEGIN IMMEDIATE")
            storage_existed = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='native_snapshot_storage'"
            ).fetchone() is not None
            migration_version = db.execute("PRAGMA user_version").fetchone()[0]
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            # Only a registry that predates the provenance table is legacy. The
            # durable marker makes a later dropped table or row fail closed.
            if migration_version < 1:
                if not storage_existed:
                    db.execute(
                        "INSERT INTO native_snapshot_storage(snapshot_id,owner,backend,backend_identity) "
                        "SELECT snapshot_id,owner,'local_archive','legacy-local-snapshots' FROM native_sources")
                db.execute("PRAGMA user_version=1")
            for table in ("native_sources", "native_queries", "native_query_uses", "native_snapshot_storage"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} "
                        f"ON {table} BEGIN SELECT RAISE(ABORT,'native records are immutable'); END")
            db.commit()

    def _connect(self):
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _decode(row):
        raw = row["payload_json"]
        if hashlib.sha256(raw.encode()).hexdigest() != row["payload_sha256"]:
            raise NativeSqlError("native_record_integrity", "Persisted native record has changed")
        try:
            value = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise NativeSqlError("native_record_integrity", "Persisted native record is invalid") from exc
        if not isinstance(value, dict):
            raise NativeSqlError("native_record_integrity", "Persisted native record must be an object")
        return value

    def _store_for_owner(self, owner):
        try:
            store = self.public_object_store if owner == "" else (
                None if self.private_object_store_factory is None else self.private_object_store_factory(owner))
        except Exception as exc:
            raise NativeSqlError("native_source_unavailable", "Snapshot object store is unavailable") from exc
        if store is None:
            return None
        if not isinstance(store, ObjectStore):
            raise NativeSqlError("invalid_object_store", "Snapshot store does not implement ObjectStore")
        backend_limit = getattr(store, "max_object_bytes", self.object_store_max_bytes)
        if type(backend_limit) is not int or backend_limit <= 0:
            raise NativeSqlError("invalid_object_store", "Snapshot store must declare a positive byte limit")
        return store, min(MAX_SOURCE_BYTES, self.object_store_max_bytes, backend_limit)

    @staticmethod
    def _storage_error(exc, *, reading=False):
        if isinstance(exc, RawStorageError) and reading and exc.code in {"object_tampered", "source_hash_mismatch"}:
            return NativeSqlError("native_source_integrity", "Snapshot object bytes failed verification")
        return NativeSqlError("native_source_unavailable", "Snapshot object storage is unavailable")

    def _archive_file(self, path, *, storage):
        path = Path(path)
        if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal")):
            raise NativeSqlError("native_source_live", "Register a closed, checkpointed database snapshot")
        temporary = None
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as source:
                before = os.fstat(source.fileno())
                limit = MAX_SOURCE_BYTES if storage is None else storage[1]
                if not stat.S_ISREG(before.st_mode):
                    raise NativeSqlError("invalid_native_source", "A bounded regular database file is required")
                if before.st_size > limit:
                    raise NativeSqlError("native_source_limit", "Database exceeds its snapshot byte limit")
                hasher, count = hashlib.sha256(), 0
                with tempfile.NamedTemporaryFile(dir=self.archive, delete=False) as target:
                    temporary = Path(target.name)
                    while chunk := source.read(128 * 1024):
                        count += len(chunk)
                        if count > limit:
                            raise NativeSqlError("native_source_limit", "Database exceeds its snapshot byte limit")
                        target.write(chunk)
                        hasher.update(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                after = os.fstat(source.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise NativeSqlError("native_source_changed", "Database changed while being archived")
            checksum = hasher.hexdigest()
            if storage is None:
                destination = self.archive / (checksum + ".sqlite")
                os.chmod(temporary, 0o400)
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    with destination.open("rb") as existing:
                        if hashlib.file_digest(existing, "sha256").hexdigest() != checksum:
                            raise NativeSqlError("native_source_integrity", "Existing snapshot content has changed")
            else:
                store, _limit = storage
                try:
                    store.put_if_absent(checksum, temporary.read_bytes())
                except Exception as exc:
                    raise self._storage_error(exc) from exc
            return checksum, count
        except NativeSqlError:
            raise
        except OSError as exc:
            raise NativeSqlError("native_source_unavailable",
                                 f"Cannot archive database snapshot (OS error {exc.errno})") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _provenance(self, source):
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM native_snapshot_storage WHERE snapshot_id=?",
                             (source["snapshot_id"],)).fetchone()
        if row is None:
            raise NativeSqlError("native_record_integrity", "Snapshot storage provenance is missing")
        if row["owner"] != source["owner"] or row["backend"] not in {"local_archive", "object_store"}:
            raise NativeSqlError("native_record_integrity", "Snapshot storage provenance does not match its owner")
        if row["backend"] == "object_store" and row["backend_identity"] != self.object_store_identity:
            raise NativeSqlError("native_source_unavailable", "Snapshot requires its registered object-store identity")
        return row["backend"]

    @contextmanager
    def _open_snapshot_path(self, source, *, storage=_UNSET):
        if storage is _UNSET:
            backend = self._provenance(source)
            storage = None if backend == "local_archive" else self._store_for_owner(source["owner"])
            if backend == "object_store" and storage is None:
                raise NativeSqlError("native_source_unavailable", "Snapshot object store is not configured")
        if storage is None:
            path = self.archive / (source["sha256"] + ".sqlite")
            try:
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
                    info = os.fstat(handle.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_size != source["byte_length"]
                            or info.st_size > MAX_SOURCE_BYTES
                            or hashlib.file_digest(handle, "sha256").hexdigest() != source["sha256"]):
                        raise NativeSqlError("native_source_integrity", "Frozen source bytes do not match the registered snapshot")
                    yield path
            except NativeSqlError:
                raise
            except OSError as exc:
                raise NativeSqlError("native_source_unavailable", f"Cannot read snapshot (OS error {exc.errno})") from exc
            return
        store, limit = storage
        if source["byte_length"] > limit:
            raise NativeSqlError("native_source_limit", "Registered snapshot exceeds the configured backend limit")

        def load():
            try:
                return store.get_verified(source["sha256"], source["byte_length"])
            except Exception as exc:
                raise self._storage_error(exc, reading=True) from exc

        try:
            cache_scope = ("public" if source["owner"] == "" else
                           hashlib.sha256(source["owner"].encode("utf-8")).hexdigest())
            with self.cache.open(source["sha256"], source["byte_length"], load,
                                 scope=cache_scope) as path:
                yield path
        except NativeSqlError:
            raise
        except SnapshotCacheError as exc:
            if "integrity" in exc.code:
                code = "native_source_integrity"
            elif exc.code == "snapshot_cache_limit":
                code = "native_source_limit"
            else:
                code = "native_source_unavailable"
            raise NativeSqlError(code, exc.message) from exc

    def register_snapshot(self, path, *, source_key, title, origin, owner=None):
        """Administrative acquisition only; preserve exact supplied database bytes."""
        source_key, title = _text(source_key, "source_key"), _text(title, "title")
        owner = "" if owner is None else _text(owner, "owner")
        if (not isinstance(origin, dict) or set(origin) != {"kind", "description", "upstream"}
                or origin["kind"] not in {"provider_snapshot", "derived_dataset", "session_upload", "synthetic_fixture"}
                or not isinstance(origin["upstream"], list) or len(encoded(origin)) > 16_000):
            raise NativeSqlError("invalid_native_origin", "Declare native source kind, processing and upstream references")
        _text(origin["description"], "origin.description", 4_000)
        storage = self._store_for_owner(owner)
        checksum, size = self._archive_file(path, storage=storage)
        provisional = {"sha256": checksum, "byte_length": size, "owner": owner}
        with self._open_snapshot_path(provisional, storage=storage) as inspection_path:
            inspection = execute([{
                "alias": "source", "source_id": "registration-" + checksum,
                "path": str(inspection_path), "sha256": checksum,
            }], "SELECT name,type,sql FROM source.sqlite_schema "
                "WHERE type IN ('table','view') AND name NOT GLOB 'sqlite_*' ORDER BY name")
        base = {"source_key": source_key, "title": title, "owner": owner, "origin": origin,
                "sha256": checksum, "byte_length": size, "media_type": "application/vnd.sqlite3"}
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT * FROM native_sources WHERE source_key=? AND owner=? ORDER BY revision DESC LIMIT 1",
                (source_key, owner)).fetchone()
            old = self._decode(previous) if previous is not None else None
            if old is not None and all(old.get(key) == value for key, value in base.items()):
                return old
            source = {
                **base, "version": VERSION, "revision": 1 if old is None else old["revision"] + 1,
                "supersedes": None if old is None else old["snapshot_id"],
                "registered_at": datetime.now(timezone.utc).isoformat(),
                "schema": {"columns": inspection["columns"], "rows": inspection["rows"],
                           "truncated": inspection["truncated"], "sqlite_version": inspection["sqlite_version"]},
            }
            source["snapshot_id"] = "native-" + digest(source)
            raw = encoded(source).decode()
            db.execute("INSERT INTO native_sources VALUES(?,?,?,?,?,?)", (
                source["snapshot_id"], source_key, owner, source["revision"], raw, digest(source)))
            backend = "local_archive" if storage is None else "object_store"
            identity = "legacy-local-snapshots" if storage is None else self.object_store_identity
            db.execute("INSERT INTO native_snapshot_storage VALUES(?,?,?,?)",
                       (source["snapshot_id"], owner, backend, identity))
            db.commit()
            return source

    def source(self, snapshot_id, *, owner):
        _text(owner, "owner")
        _text(snapshot_id, "snapshot_id")
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM native_sources WHERE snapshot_id=? AND owner IN ('',?)",
                             (snapshot_id, owner)).fetchone()
        if row is None:
            raise NativeSqlError("native_source_unavailable", "Snapshot is not registered or not available to this conversation")
        source = self._decode(row)
        identity = {key: value for key, value in source.items() if key != "snapshot_id"}
        if (source.get("version") != VERSION or source.get("snapshot_id") != snapshot_id
                or "native-" + digest(identity) != snapshot_id
                or source.get("owner") != row["owner"] or source.get("source_key") != row["source_key"]
                or source.get("revision") != row["revision"]):
            raise NativeSqlError("native_record_integrity", "Snapshot identity or owner does not match")
        return source

    @contextmanager
    def open_snapshot(self, snapshot_id, *, owner):
        # Authorization and owner validation always precede provenance/store access.
        source = self.source(snapshot_id, owner=owner)
        with self._open_snapshot_path(source) as path:
            try:
                with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as handle:
                    yield source, handle
            except OSError as exc:
                raise NativeSqlError("native_source_unavailable", f"Cannot read snapshot (OS error {exc.errno})") from exc

    def capture_scope(self, *, owner):
        _text(owner, "owner")
        with closing(self._connect()) as db:
            rows = db.execute(
                "SELECT s.snapshot_id FROM native_sources s WHERE s.owner IN ('',?) AND NOT EXISTS "
                "(SELECT 1 FROM native_sources newer WHERE newer.source_key=s.source_key "
                "AND newer.owner=s.owner AND newer.revision>s.revision) ORDER BY s.owner,s.source_key",
                (owner,)).fetchall()
        return {"version": VERSION, "owner": owner, "snapshot_ids": [row[0] for row in rows]}

    def list_sources(self, *, owner, scope):
        if (not isinstance(scope, dict) or set(scope) != {"version", "owner", "snapshot_ids"}
                or scope["version"] != VERSION or scope["owner"] != owner
                or not isinstance(scope["snapshot_ids"], list)
                or any(not isinstance(item, str) for item in scope["snapshot_ids"])
                or len(set(scope["snapshot_ids"])) != len(scope["snapshot_ids"])):
            raise NativeSqlError("invalid_native_scope", "A frozen source scope owned by this conversation is required")
        return [self.source(identifier, owner=owner) for identifier in scope["snapshot_ids"]]

    def query(self, *, owner, run_id, scope, sources, sql, parameters=None, row_limit=500, cancelled=None):
        _text(owner, "owner")
        _text(run_id, "run_id")
        permitted = {item["snapshot_id"]: item for item in self.list_sources(owner=owner, scope=scope)}
        if (not isinstance(sources, list) or not 1 <= len(sources) <= 8
                or any(not isinstance(item, dict) or set(item) != {"snapshot_id", "alias"}
                       or not isinstance(item["snapshot_id"], str) for item in sources)):
            raise NativeSqlError("invalid_native_input", "Select registered snapshot IDs and SQL aliases, never paths")
        resolved = []
        with ExitStack() as snapshots:
            opened = {}
            for selected in sources:
                source = permitted.get(selected["snapshot_id"])
                if source is None:
                    raise NativeSqlError("native_source_out_of_scope", "Source is outside this research version's frozen source scope")
                path = opened.get(source["snapshot_id"])
                if path is None:
                    path = snapshots.enter_context(self._open_snapshot_path(source))
                    opened[source["snapshot_id"]] = path
                resolved.append({
                    "alias": selected["alias"], "source_id": source["snapshot_id"],
                    "path": str(path), "sha256": source["sha256"],
                })
            result = execute(resolved, sql, parameters, row_limit=row_limit, cancelled=cancelled)
        if cancelled is not None and cancelled():
            raise NativeSqlError("sql_cancelled", "Query cancelled before persistence")
        raw = encoded(result).decode()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM native_queries WHERE owner=? AND query_id=?",
                                  (owner, result["query_id"])).fetchone()
            if previous is not None:
                if self._decode(previous) != result:
                    raise NativeSqlError("native_record_integrity", "Query identity conflicts with its stored content")
            else:
                db.execute("INSERT INTO native_queries VALUES(?,?,?,?,?)",
                           (owner, result["query_id"], run_id, raw, digest(result)))
            db.execute("INSERT OR IGNORE INTO native_query_uses VALUES(?,?,?)", (owner, run_id, result["query_id"]))
            db.commit()
        return result

    def read_query(self, query_id, *, owner, run_id):
        _text(owner, "owner")
        _text(run_id, "run_id")
        _text(query_id, "query_id")
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT q.* FROM native_queries q JOIN native_query_uses u USING(owner,query_id) "
                "WHERE q.owner=? AND q.query_id=? AND u.run_id=?", (owner, query_id, run_id)).fetchone()
        if row is None:
            raise NativeSqlError("native_query_unavailable", "Query is not registered for this conversation and research version")
        result = self._decode(row)
        identity = {key: value for key, value in result.items() if key != "query_id"}
        if result.get("query_id") != query_id or "sql-" + digest(identity) != query_id:
            raise NativeSqlError("native_record_integrity", "Stored query identity does not match")
        for source in result["sources"]:
            descriptor = self.source(source["source_id"], owner=owner)
            if descriptor["sha256"] != source["sha256"]:
                raise NativeSqlError("native_record_integrity", "Query input differs from its registered snapshot")
        return result

    def replay(self, query_id, *, owner, run_id, cancelled=None):
        result = self.read_query(query_id, owner=owner, run_id=run_id)
        sources = []
        with ExitStack() as snapshots:
            opened = {}
            for recorded in result["sources"]:
                descriptor = self.source(recorded["source_id"], owner=owner)
                try:
                    path = opened.get(descriptor["snapshot_id"])
                    if path is None:
                        path = snapshots.enter_context(self._open_snapshot_path(descriptor))
                        opened[descriptor["snapshot_id"]] = path
                except NativeSqlError as exc:
                    if exc.code == "native_source_integrity" and self._provenance(descriptor) == "local_archive":
                        raise NativeSqlError("sql_source_integrity", "Frozen database content has changed") from exc
                    raise
                sources.append({**recorded, "path": str(path)})
            actual = execute(sources, result["sql"], result["parameters"],
                             row_limit=result["row_limit"], cancelled=cancelled, output_version=result["version"])
        if actual != result:
            raise NativeSqlError("native_replay_mismatch", "SQL replay differs from the recorded output or engine version")
        return {"query": result, "replay_matches": True}

    def cell(self, query_id, *, owner, run_id, row, column):
        result = self.read_query(query_id, owner=owner, run_id=run_id)
        if (type(row) is not int or type(column) is not int or row < 0 or column < 0
                or row >= len(result["rows"]) or column >= len(result["columns"])):
            raise NativeSqlError("native_cell_unavailable", "Cell position is outside the recorded query result")
        return {"query_id": query_id, "row": row, "column": column,
                "column_name": result["columns"][column]["name"], "value": result["rows"][row][column],
                "truncated": result["truncated"], "numeric_semantics": result["numeric_semantics"]}
