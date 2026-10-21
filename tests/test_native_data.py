import hashlib
import json
from contextlib import closing
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from workbench.native_data import NativeDataService
from workbench.native_sql import NativeSqlError
from workbench.raw_storage import RawStorageError
from workbench.snapshot_cache import SnapshotCacheError, VerifiedSnapshotCache


ORIGIN = {"kind": "synthetic_fixture",
          "description": "Synthetic operational records, not issuer financial data.", "upstream": []}


def _alias_query_worker(service, source, output):
    try:
        scope = service.capture_scope(owner="conversation-a")
        result = service.query(
            owner="conversation-a", run_id="alias-run", scope=scope,
            sources=[
                {"snapshot_id": source["snapshot_id"], "alias": "left_side"},
                {"snapshot_id": source["snapshot_id"], "alias": "right_side"},
            ],
            sql="SELECT l.amount,r.amount FROM left_side.facts l JOIN right_side.facts r USING(label)")
        output.put(("ok", result["rows"]))
    except BaseException as exc:
        output.put(("error", type(exc).__name__, str(exc)))


def _cache_population_worker(root, raw, start, release, output):
    cache = VerifiedSnapshotCache(root, max_bytes=len(raw), max_entry_bytes=len(raw))
    digest = hashlib.sha256(raw).hexdigest()
    start.wait()
    try:
        with cache.open(digest, len(raw), lambda: raw, scope="public"):
            output.put(("opened", digest))
            release.wait(10)
    except SnapshotCacheError as exc:
        output.put(("error", exc.code))


class ControlledObjectStore:
    """Owner-scoped protocol fixture; deliberately not an S3 implementation."""

    def __init__(self, label, *, max_bytes=256 * 1024 * 1024):
        self.label = label
        self.max_object_bytes = max_bytes
        self.objects = {}
        self.get_calls = 0
        self.put_calls = 0
        self.unavailable = False

    def put_if_absent(self, digest, raw):
        self.put_calls += 1
        if self.unavailable:
            raise RawStorageError("object_unavailable", "controlled store unavailable")
        if len(raw) > self.max_object_bytes or hashlib.sha256(raw).hexdigest() != digest:
            raise RawStorageError("invalid_file", "controlled store rejected bytes")
        existing = self.objects.setdefault(digest, raw)
        if existing != raw:
            raise RawStorageError("object_tampered", "controlled store conflict")

    def get_verified(self, digest, expected_size=None):
        self.get_calls += 1
        if self.unavailable:
            raise RawStorageError("object_unavailable", "controlled store unavailable")
        try:
            raw = self.objects[digest]
        except KeyError as exc:
            raise RawStorageError("object_tampered", "controlled object missing") from exc
        if ((expected_size is not None and len(raw) != expected_size)
                or hashlib.sha256(raw).hexdigest() != digest):
            raise RawStorageError("object_tampered", "controlled object failed verification")
        return raw


class NativeDataTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "provider.sqlite"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("CREATE TABLE unfamiliar(customer TEXT,units INTEGER); "
                             "INSERT INTO unfamiliar VALUES('Alpha',7),('Beta',13);")
        self.service = NativeDataService(self.root / "native")
        self.public = self.register()
        self.scope = self.service.capture_scope(owner="conversation-a")

    def register(self, **kwargs):
        return self.service.register_snapshot(
            self.path, source_key="configured-provider", title="Synthetic native source", origin=ORIGIN, **kwargs)

    def query(self, **kwargs):
        return self.service.query(
            owner="conversation-a", run_id="run-a", scope=self.scope,
            sources=[{"snapshot_id": self.public["snapshot_id"], "alias": "provider"}],
            sql="SELECT SUM(units),COUNT(*) FROM provider.unfamiliar", **kwargs)

    def test_original_bytes_and_native_schema_are_preserved(self):
        archived = self.service.archive / (self.public["sha256"] + ".sqlite")
        self.assertEqual(archived.read_bytes(), self.path.read_bytes())
        self.assertIn("units INTEGER", self.public["schema"]["rows"][0][2])
        self.assertEqual(self.public["origin"], ORIGIN)
        self.assertEqual(self.register(), self.public)
        self.assertNotIn(str(self.path), json.dumps(self.public))

    def test_public_context_and_private_material_have_distinct_permissions(self):
        private = self.register(owner="conversation-a")
        a = self.service.capture_scope(owner="conversation-a")
        b = self.service.capture_scope(owner="conversation-b")
        self.assertEqual(set(a["snapshot_ids"]), {self.public["snapshot_id"], private["snapshot_id"]})
        self.assertEqual(b["snapshot_ids"], [self.public["snapshot_id"]])
        with self.assertRaises(NativeSqlError):
            self.service.source(private["snapshot_id"], owner="conversation-b")
        with self.assertRaises(NativeSqlError):
            self.service.list_sources(owner="conversation-b", scope=a)
        # Research targets never filter the authorized native data source.
        self.assertEqual(self.query()["rows"], [[20, 2]])

    def test_query_persistence_restart_cell_references_and_replay(self):
        query = self.query()
        restarted = NativeDataService(self.service.root)
        self.assertEqual(restarted.read_query(query["query_id"], owner="conversation-a", run_id="run-a"), query)
        self.assertTrue(restarted.replay(query["query_id"], owner="conversation-a", run_id="run-a")["replay_matches"])
        cell = restarted.cell(query["query_id"], owner="conversation-a", run_id="run-a", row=0, column=0)
        self.assertEqual(cell["value"], 20)
        self.assertEqual(cell["column_name"], "SUM(units)")
        self.assertEqual(query, self.query())
        for owner, run in (("conversation-b", "run-a"), ("conversation-a", "other-run")):
            with self.subTest(owner=owner, run=run), self.assertRaises(NativeSqlError):
                restarted.read_query(query["query_id"], owner=owner, run_id=run)
        with self.assertRaises(NativeSqlError):
            restarted.cell(query["query_id"], owner="conversation-a", run_id="run-a", row=True, column=0)

    def test_revisions_do_not_change_frozen_scopes_or_previous_results(self):
        before = self.query()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO unfamiliar VALUES('Gamma',31)")
        changed = self.register()
        self.assertEqual(changed["revision"], 2)
        self.assertEqual(changed["supersedes"], self.public["snapshot_id"])
        current = self.service.capture_scope(owner="conversation-a")
        self.assertEqual(current["snapshot_ids"], [changed["snapshot_id"]])
        self.assertEqual(self.query(), before)
        with self.assertRaises(NativeSqlError) as raised:
            self.service.query(
                owner="conversation-a", run_id="run-a", scope=self.scope,
                sources=[{"snapshot_id": changed["snapshot_id"], "alias": "provider"}],
                sql="SELECT SUM(units) FROM provider.unfamiliar")
        self.assertEqual(raised.exception.code, "native_source_out_of_scope")
        after = self.service.query(
            owner="conversation-a", run_id="run-b", scope=current,
            sources=[{"snapshot_id": changed["snapshot_id"], "alias": "provider"}],
            sql="SELECT SUM(units) FROM provider.unfamiliar")
        self.assertEqual(after["rows"], [[51]])
        self.assertTrue(self.service.replay(before["query_id"], owner="conversation-a", run_id="run-a")["replay_matches"])

    def test_records_are_immutable_and_tampered_snapshots_cannot_be_replayed(self):
        query = self.query()
        with closing(sqlite3.connect(self.service.database)) as db, db:
            for table in ("native_sources", "native_queries", "native_query_uses"):
                with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                    db.execute(f"DELETE FROM {table}")
        archived = self.service.archive / (self.public["sha256"] + ".sqlite")
        archived.chmod(0o600)
        with closing(sqlite3.connect(archived)) as db, db:
            db.execute("UPDATE unfamiliar SET units=100")
        with self.assertRaises(NativeSqlError) as raised:
            self.service.replay(query["query_id"], owner="conversation-a", run_id="run-a")
        self.assertEqual(raised.exception.code, "sql_source_integrity")
        self.assertEqual(self.service.read_query(query["query_id"], owner="conversation-a", run_id="run-a"), query)

    def test_no_paths_or_live_database_journals_are_accepted_as_source_handles(self):
        with self.assertRaises(NativeSqlError):
            self.service.query(
                owner="conversation-a", run_id="run-a", scope=self.scope,
                sources=[{"path": str(self.path), "alias": "provider"}], sql="SELECT 1")
        Path(str(self.path) + "-wal").write_bytes(b"uncheckpointed")
        with self.assertRaises(NativeSqlError) as raised:
            self.register()
        self.assertEqual(raised.exception.code, "native_source_live")


class NativeObjectSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.database = self.root / "source.sqlite"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript("CREATE TABLE facts(label TEXT, amount INTEGER); "
                             "INSERT INTO facts VALUES('remote',17)")
        self.public_store = ControlledObjectStore("public")
        self.private_stores = {}
        self.factory_calls = []

    def private_store(self, owner):
        self.factory_calls.append(owner)
        return self.private_stores.setdefault(owner, ControlledObjectStore("private:" + owner))

    def service(self, **options):
        return NativeDataService(
            self.root / "native", public_object_store=self.public_store,
            private_object_store_factory=self.private_store,
            object_store_max_bytes=256 * 1024 * 1024,
            object_store_identity="controlled:native-snapshots:v1", **options)

    def register(self, service, *, owner=None, source_key="fixture"):
        return service.register_snapshot(
            self.database, source_key=source_key, title="Controlled snapshot",
            origin=ORIGIN, owner=owner)

    @staticmethod
    def query(service, source, *, owner="conversation-a", run_id="run-a"):
        scope = service.capture_scope(owner=owner)
        return service.query(
            owner=owner, run_id=run_id, scope=scope,
            sources=[{"snapshot_id": source["snapshot_id"], "alias": "remote"}],
            sql="SELECT SUM(amount) FROM remote.facts")

    def clear_cache(self, service):
        for path in service.cache.root.glob("*.sqlite"):
            path.unlink()

    def test_legacy_snapshot_without_provenance_stays_on_local_archive(self):
        local_root = self.root / "legacy-native"
        local = NativeDataService(local_root)
        snapshot = self.register(local)
        # Recreate the structural state of a registry from before the storage
        # provenance feature, rather than deleting a row from a new registry.
        with closing(sqlite3.connect(local.database)) as db, db:
            db.execute("DROP TABLE native_snapshot_storage")
            db.execute("PRAGMA user_version=0")
        configured = NativeDataService(
            local_root, public_object_store=self.public_store,
            private_object_store_factory=self.private_store,
            object_store_max_bytes=256 * 1024 * 1024,
            object_store_identity="controlled:native-snapshots:v1")
        result = self.query(configured, snapshot)
        self.assertEqual(result["rows"], [[17]])
        self.assertEqual(self.public_store.get_calls, 0)
        self.assertTrue((configured.archive / (snapshot["sha256"] + ".sqlite")).is_file())
        with closing(sqlite3.connect(configured.database)) as db:
            row = db.execute("SELECT backend FROM native_snapshot_storage WHERE snapshot_id=?",
                             (snapshot["snapshot_id"],)).fetchone()
            self.assertEqual(row, ("local_archive",))
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_missing_new_remote_provenance_never_falls_back_or_backfills_again(self):
        service = self.service()
        source = self.register(service)
        local = service.archive / (source["sha256"] + ".sqlite")
        local.write_bytes(self.database.read_bytes())  # Same valid hash is still unauthorized fallback.
        with closing(sqlite3.connect(service.database)) as db, db:
            db.execute("DROP TRIGGER native_snapshot_storage_no_delete")
            db.execute("DELETE FROM native_snapshot_storage WHERE snapshot_id=?", (source["snapshot_id"],))
        for candidate in (service, self.service(), self.service()):
            with self.subTest(candidate=id(candidate)), self.assertRaises(NativeSqlError) as raised:
                self.query(candidate, source)
            self.assertEqual(raised.exception.code, "native_record_integrity")
        with closing(sqlite3.connect(service.database)) as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM native_snapshot_storage WHERE snapshot_id=?", (source["snapshot_id"],)).fetchone())

    def test_dropped_provenance_table_after_migration_is_not_legacy(self):
        service = self.service()
        source = self.register(service)
        with closing(sqlite3.connect(service.database)) as db, db:
            db.execute("DROP TABLE native_snapshot_storage")
        restarted = self.service()
        with self.assertRaises(NativeSqlError) as raised:
            self.query(restarted, source)
        self.assertEqual(raised.exception.code, "native_record_integrity")
        with closing(sqlite3.connect(restarted.database)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_same_snapshot_under_two_aliases_finishes_in_bounded_subprocess(self):
        service = self.service()
        source = self.register(service)
        context = multiprocessing.get_context("fork")
        output = context.Queue()
        process = context.Process(target=_alias_query_worker, args=(service, source, output))
        process.start()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(2)
            self.fail("native self-join deadlocked while opening the second alias")
        self.assertEqual(process.exitcode, 0)
        self.assertEqual(output.get(timeout=1), ("ok", [[17, 17]]))

    def test_same_hash_public_and_private_use_distinct_authorized_stores_and_cache(self):
        service = self.service()
        public = self.register(service)
        private = self.register(service, owner="conversation-a")
        self.assertEqual(public["sha256"], private["sha256"])
        self.assertNotEqual(public["snapshot_id"], private["snapshot_id"])
        self.assertIn(public["sha256"], self.public_store.objects)
        self.assertIn(private["sha256"], self.private_stores["conversation-a"].objects)
        cached = {path.name for path in service.cache.root.glob("*.sqlite")}
        self.assertTrue(any(name.startswith("public-") for name in cached))
        private_scope = hashlib.sha256(b"conversation-a").hexdigest() + "-"
        self.assertTrue(any(name.startswith(private_scope) for name in cached))
        before = list(self.factory_calls)
        with self.assertRaises(NativeSqlError) as raised:
            with service.open_snapshot(private["snapshot_id"], owner="conversation-b"):
                pass
        self.assertEqual(raised.exception.code, "native_source_unavailable")
        self.assertEqual(self.factory_calls, before)  # Unknown owner rejected before store selection.

    def test_unavailable_or_tampered_remote_never_falls_back_to_local(self):
        service = self.service()
        source = self.register(service)
        self.assertFalse((service.archive / (source["sha256"] + ".sqlite")).exists())
        self.clear_cache(service)
        self.public_store.unavailable = True
        with self.assertRaises(NativeSqlError) as unavailable:
            self.query(service, source)
        self.assertEqual(unavailable.exception.code, "native_source_unavailable")
        self.public_store.unavailable = False
        self.public_store.objects[source["sha256"]] = b"x" * source["byte_length"]
        with self.assertRaises(NativeSqlError) as tampered:
            self.query(service, source)
        self.assertEqual(tampered.exception.code, "native_source_integrity")
        self.assertFalse((service.archive / (source["sha256"] + ".sqlite")).exists())

    def test_cache_rebuild_limit_active_use_and_replay_identity(self):
        size = self.database.stat().st_size
        service = self.service(snapshot_cache_bytes=size)
        first = self.register(service, source_key="first")
        query = self.query(service, first)
        self.clear_cache(service)
        before = self.public_store.get_calls
        self.assertEqual(self.query(service, first), query)
        self.assertGreater(self.public_store.get_calls, before)
        self.assertEqual(service.replay(query["query_id"], owner="conversation-a", run_id="run-a")["query"], query)

        second_path = self.root / "second.sqlite"
        with closing(sqlite3.connect(second_path)) as db, db:
            db.executescript("CREATE TABLE other(value INTEGER); INSERT INTO other VALUES(99)")
        self.database = second_path
        second = self.register(service, source_key="second")
        with service.open_snapshot(first["snapshot_id"], owner="conversation-a"):
            with self.assertRaises(NativeSqlError) as full:
                with service.open_snapshot(second["snapshot_id"], owner="conversation-a"):
                    pass
            self.assertEqual(full.exception.code, "native_source_limit")
        with service.open_snapshot(second["snapshot_id"], owner="conversation-a") as (_source, handle):
            self.assertEqual(handle.read(16), b"SQLite format 3\0")

    def test_cross_process_population_respects_one_entry_capacity(self):
        cache_root = self.root / "race-cache"
        raw_values = [b"a" * 4096, b"b" * 4096]
        context = multiprocessing.get_context("fork")
        start, release, output = context.Event(), context.Event(), context.Queue()
        processes = [context.Process(
            target=_cache_population_worker,
            args=(cache_root, raw, start, release, output)) for raw in raw_values]
        for process in processes:
            process.start()
        start.set()
        results = [output.get(timeout=8), output.get(timeout=8)]
        self.assertEqual(sorted(item[0] for item in results), ["error", "opened"])
        self.assertIn(("error", "snapshot_cache_limit"), results)
        self.assertLessEqual(sum(path.stat().st_size for path in cache_root.glob("*.sqlite")), 4096)
        release.set()
        for process in processes:
            process.join(5)
            self.assertEqual(process.exitcode, 0)

    def test_crash_left_temporary_is_cleaned_before_capacity_accounting(self):
        cache_root = self.root / "temporary-cache"
        cache_root.mkdir()
        abandoned = cache_root / ".snapshot-crashed"
        abandoned.write_bytes(b"x" * 8192)
        raw = b"current" * 512
        cache = VerifiedSnapshotCache(cache_root, max_bytes=len(raw), max_entry_bytes=len(raw))
        digest = hashlib.sha256(raw).hexdigest()
        with cache.open(digest, len(raw), lambda: raw, scope="public") as path:
            self.assertEqual(path.read_bytes(), raw)
            self.assertFalse(abandoned.exists())
        self.assertEqual(sum(path.stat().st_size for path in cache_root.glob("*.sqlite")), len(raw))

    def test_backend_limit_is_enforced_before_put(self):
        too_small = ControlledObjectStore("small", max_bytes=self.database.stat().st_size - 1)
        service = NativeDataService(
            self.root / "small-native", public_object_store=too_small,
            object_store_max_bytes=256 * 1024 * 1024,
            object_store_identity="controlled:small:v1")
        with self.assertRaises(NativeSqlError) as raised:
            self.register(service)
        self.assertEqual(raised.exception.code, "native_source_limit")
        self.assertEqual(too_small.put_calls, 0)


if __name__ == "__main__":
    unittest.main()
