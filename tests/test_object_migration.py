"""Object migration proves namespace isolation, resumability, and full verification."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from workbench.object_migration import (
    MigrationError, StoreBinding, assert_cutover_ready, build_manifest, copy_manifest,
    manifest_diff, references_from_sqlite, verify_manifest,
)
from workbench.raw_storage import LocalObjectStore, RawStorageError


class FailingStore:
    max_object_bytes = 1024 * 1024

    def __init__(self, delegate, fail_after=0):
        self.delegate, self.remaining = delegate, fail_after

    def get_verified(self, digest, expected_size=None):
        return self.delegate.get_verified(digest, expected_size)

    def put_if_absent(self, digest, raw):
        if self.remaining == 0:
            raise RawStorageError("injected", "interrupted")
        self.remaining -= 1
        self.delegate.put_if_absent(digest, raw)


class ObjectMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_public = LocalObjectStore(self.root / "source-public")
        self.source_private = LocalObjectStore(self.root / "source-private")
        self.target_public = LocalObjectStore(self.root / "target-public")
        self.target_private = LocalObjectStore(self.root / "target-private")
        self.public = b"public original"
        self.private = b"private original"
        self.public_hash = hashlib.sha256(self.public).hexdigest()
        self.private_hash = hashlib.sha256(self.private).hexdigest()
        self.source_public.put_if_absent(self.public_hash, self.public)
        self.source_private.put_if_absent(self.private_hash, self.private)
        self.references = [
            {"scope": "public", "sha256": self.public_hash, "bytes": len(self.public),
             "metadata_references": ["documents.sqlite:document_versions:doc-1"]},
            {"scope": "private:conversation-a", "sha256": self.private_hash, "bytes": len(self.private),
             "metadata_references": ["materials.sqlite:document_versions:material-1"]},
        ]
        self.sources = {
            "public": StoreBinding("local", "source/public", self.source_public),
            "private:conversation-a": StoreBinding("local", "source/private/a", self.source_private),
        }
        self.targets = {
            "public": StoreBinding("s3", "target/public/new-prefix", self.target_public),
            "private:conversation-a": StoreBinding("s3", "target/private/a/new-prefix", self.target_private),
        }

    def test_dry_run_records_scope_identity_hash_size_and_metadata(self):
        manifest = build_manifest(self.references, self.sources, self.targets, generation="generation-1")
        self.assertEqual(len(manifest["objects"]), 2)
        entry = manifest["objects"][1]
        self.assertEqual(entry["source_identity"], "source/public")
        self.assertEqual(entry["target_backend"], "s3")
        self.assertIn("metadata_references", entry)
        self.assertEqual(entry["sha256"], self.public_hash)
        self.assertEqual(entry["bytes"], len(self.public))

    def test_copy_is_idempotent_and_completion_follows_whole_verification(self):
        manifest = build_manifest(self.references, self.sources, self.targets, generation="generation-1")
        progress = self.root / "progress.json"
        result = copy_manifest(manifest, self.sources, self.targets, progress_path=progress)
        self.assertTrue(result["complete"])
        self.assertTrue(json.loads(progress.read_text())["complete"])
        self.assertEqual(copy_manifest(manifest, self.sources, self.targets, progress_path=progress), result)
        verify_manifest(manifest, self.sources, self.targets, verify_sources=True)

    def test_missing_and_corrupt_source_are_rejected_during_dry_run(self):
        missing = [dict(self.references[0], sha256="0" * 64)]
        with self.assertRaises(RawStorageError):
            build_manifest(missing, self.sources, self.targets, generation="missing")
        (self.source_public.objects_path / self.public_hash).write_bytes(b"corrupt")
        with self.assertRaises(RawStorageError):
            build_manifest([self.references[0]], self.sources, self.targets, generation="corrupt")

    def test_wrong_private_namespace_and_changed_identity_fail_closed(self):
        wrong = dict(self.sources)
        wrong["private:conversation-a"] = StoreBinding("local", "source/private/b", LocalObjectStore(self.root / "wrong"))
        with self.assertRaises(RawStorageError):
            build_manifest(self.references, wrong, self.targets, generation="wrong-private")
        manifest = build_manifest(self.references, self.sources, self.targets, generation="identity")
        changed = dict(self.targets)
        changed["public"] = StoreBinding("s3", "other-prefix", self.target_public)
        with self.assertRaisesRegex(MigrationError, "identity changed"):
            copy_manifest(manifest, self.sources, changed)

    def test_interrupted_copy_resumes_without_false_completion(self):
        manifest = build_manifest(self.references, self.sources, self.targets, generation="interrupted")
        progress = self.root / "progress.json"
        interrupted_targets = dict(self.targets)
        interrupted_targets["public"] = StoreBinding(
            "s3", "target/public/new-prefix", FailingStore(self.target_public, fail_after=0))
        with self.assertRaises(RawStorageError):
            copy_manifest(manifest, self.sources, interrupted_targets, progress_path=progress)
        if progress.exists():
            self.assertFalse(json.loads(progress.read_text())["complete"])
        result = copy_manifest(manifest, self.sources, self.targets, progress_path=progress)
        self.assertTrue(result["complete"])

    def test_quiescent_second_diff_requires_whole_candidate_completion(self):
        baseline = build_manifest([self.references[0]], self.sources, self.targets, generation="baseline")
        candidate = build_manifest(self.references, self.sources, self.targets, generation="quiescent")
        delta = manifest_diff(baseline, candidate)
        self.assertEqual(len(delta["added"]), 1)
        self.assertEqual(delta["missing"], [])
        with self.assertRaisesRegex(MigrationError, "not wholly"):
            assert_cutover_ready(baseline, candidate, {"complete": False}, self.sources, self.targets)
        completion = copy_manifest(candidate, self.sources, self.targets)
        self.assertEqual(assert_cutover_ready(baseline, candidate, completion, self.sources, self.targets), delta)

    def test_native_registry_uses_provenance_owner_and_excludes_legacy_cache_source(self):
        database = self.root / "native.sqlite"
        payload = json.dumps({"sha256": self.private_hash, "byte_length": len(self.private)})
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("CREATE TABLE native_sources(snapshot_id TEXT, owner TEXT, payload_json TEXT, payload_sha256 TEXT)")
            db.execute("CREATE TABLE native_snapshot_storage(snapshot_id TEXT, owner TEXT, backend TEXT, backend_identity TEXT)")
            db.execute("INSERT INTO native_sources VALUES('remote','conversation-a',?,?)",
                       (payload, hashlib.sha256(payload.encode()).hexdigest()))
            db.execute("INSERT INTO native_snapshot_storage VALUES('remote','conversation-a','object_store','configured-native')")
            db.execute("INSERT INTO native_sources VALUES('legacy','',?,?)",
                       (payload, hashlib.sha256(payload.encode()).hexdigest()))
            db.execute("INSERT INTO native_snapshot_storage VALUES('legacy','','local_archive','legacy-local-snapshots')")
        references = references_from_sqlite(database, kind="native_registry")
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0]["scope"], "native:private:conversation-a")
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("UPDATE native_snapshot_storage SET owner='conversation-b' WHERE snapshot_id='remote'")
        with self.assertRaisesRegex(MigrationError, "provenance"):
            references_from_sqlite(database, kind="native_registry")

    def test_extra_reference_must_be_bound_and_verified(self):
        raw = b"late append"
        digest = hashlib.sha256(raw).hexdigest()
        references = self.references + [{"scope": "private:conversation-b", "sha256": digest, "bytes": len(raw),
                                          "metadata_references": ["extra.sqlite:document_versions:late"]}]
        with self.assertRaisesRegex(MigrationError, "no object store"):
            build_manifest(references, self.sources, self.targets, generation="second-diff")


if __name__ == "__main__":
    unittest.main()
