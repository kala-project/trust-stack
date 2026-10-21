"""Temporary data only: integrity, raw bytes and non-overwriting restoration."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from workbench import backup
from workbench.object_migration import StoreBinding
from workbench.raw_storage import LocalObjectStore


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "original"
        self.source.mkdir()
        self.database = self.source / "catalog.sqlite3"
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE records(id INTEGER PRIMARY KEY, body TEXT)")
            db.execute("INSERT INTO records VALUES(1,'真实原件')")
            db.commit()
        (self.source / "objects").mkdir()
        (self.source / "objects/raw").write_bytes(b"\x00\xff original\r\n")
        (self.source / "empty").mkdir()
        # A native SQLite original must NOT be rewritten using backup().
        (self.source / "objects/source.sqlite").write_bytes(self.database.read_bytes())
        self.spec = {"version": "workbench-backup-spec/1", "resources": [
            {"name": "state/documents", "source": str(self.source), "kind": "tree", "databases": ["catalog.sqlite3"]}]}
        self.output = self.root / "backup"

    def test_roundtrip_preserves_originals_and_database_rows(self):
        before = self.database.read_bytes()
        manifest = backup.create_backup(self.spec, self.output)
        restored = self.root / "restored"
        backup.restore_backup(self.output, restored)
        self.assertEqual(backup.verify_backup(restored), manifest)
        self.assertEqual((restored / "state/documents/objects/raw").read_bytes(), b"\x00\xff original\r\n")
        self.assertEqual((restored / "state/documents/objects/source.sqlite").read_bytes(), before)
        self.assertTrue((restored / "state/documents/empty").is_dir())
        self.assertEqual(self.database.read_bytes(), before)
        with closing(sqlite3.connect(restored / "state/documents/catalog.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT * FROM records").fetchall(), [(1, "真实原件")])

    def test_committed_wal_is_included_without_copying_sidecars(self):
        with closing(sqlite3.connect(self.database)) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("INSERT INTO records VALUES(2,'WAL record')")
            writer.commit()
            backup.create_backup(self.spec, self.output)
            with closing(sqlite3.connect(self.output / "state/documents/catalog.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM records").fetchone()[0], 2)
            self.assertFalse(list(self.output.rglob("*-wal")))
            self.assertFalse(list(self.output.rglob("*-shm")))

    def test_no_existing_destination_is_overwritten(self):
        backup.create_backup(self.spec, self.output)
        manifest = (self.output / "manifest.json").read_bytes()
        for operation in [lambda: backup.create_backup(self.spec, self.output),
                          lambda: backup.restore_backup(self.output, self.source)]:
            with self.assertRaises(FileExistsError):
                operation()
        self.assertEqual((self.output / "manifest.json").read_bytes(), manifest)

    def test_tamper_missing_and_extra_files_prevent_restore(self):
        backup.create_backup(self.spec, self.output)
        path = self.output / "state/documents/objects/raw"
        original = path.read_bytes()
        path.write_bytes(b"changed")
        with self.assertRaises(backup.BackupError):
            backup.restore_backup(self.output, self.root / "bad")
        self.assertFalse((self.root / "bad").exists())
        path.unlink()
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(self.output)
        path.write_bytes(original)
        (self.output / "extra").write_text("unlisted")
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(self.output)

    def test_source_symlink_and_symlink_destination_refused(self):
        link = self.source / "objects/link"
        link.symlink_to(self.database)
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.spec, self.output)
        link.unlink()
        self.output.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.spec, self.output)

    def test_overlapping_source_and_output_refused(self):
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.spec, self.source / "backup")
        self.spec["resources"].append({"name": "state/documents/nested", "source": str(self.database), "kind": "file"})
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.spec, self.output)

    def test_manifest_path_traversal_and_duplicates_refused(self):
        backup.create_backup(self.spec, self.output)
        manifest_path = self.output / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        original = manifest["files"][0]["path"]
        for value in ["../outside", "/absolute", "x/../../outside", "x\\outside", "manifest.json", "x//y"]:
            manifest["files"][0]["path"] = value
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(backup.BackupError):
                backup.restore_backup(self.output, self.root / "restored")
            self.assertFalse((self.root / "restored").exists())
        manifest["files"][0]["path"] = original
        manifest["files"].append(manifest["files"][0])
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(self.output)

    def test_sqlite_commit_during_backup_refuses_completion(self):
        copy = backup._copy
        def changing_copy(source, target):
            copy(source, target)
            with closing(sqlite3.connect(self.database)) as writer:
                writer.execute("UPDATE records SET body='changed'")
                writer.commit()
        with patch.object(backup, "_copy", side_effect=changing_copy):
            with self.assertRaisesRegex(backup.BackupError, "SQLite changed"):
                backup.create_backup(self.spec, self.output)
        self.assertFalse((self.output / "manifest.json").exists())

    def test_raw_change_during_backup_refuses_completion(self):
        copy = backup._copy
        def changing_copy(source, target):
            copy(source, target)
            source.write_bytes(b"changed")
        with patch.object(backup, "_copy", side_effect=changing_copy):
            with self.assertRaisesRegex(backup.BackupError, "source changed"):
                backup.create_backup(self.spec, self.output)
        self.assertFalse((self.output / "manifest.json").exists())

    def test_missing_resource_and_corrupt_database_refused(self):
        self.database.unlink()
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.spec, self.output)
        self.database.write_bytes(b"not a database")
        with self.assertRaises(sqlite3.DatabaseError):
            backup.create_backup(self.spec, self.output)
        self.assertFalse((self.output / "manifest.json").exists())

    def test_empty_tree_and_unlisted_directory(self):
        empty = self.root / "empty"
        empty.mkdir()
        self.spec["resources"].append({"name": "other/deep/empty", "source": str(empty), "kind": "tree"})
        backup.create_backup(self.spec, self.output)
        backup.restore_backup(self.output, self.root / "restored")
        (self.output / "unlisted-empty").mkdir()
        with self.assertRaises(backup.BackupError):
            backup.verify_backup(self.output)

    def test_dotdot_destination_is_canonicalized_before_overlap_check(self):
        with self.assertRaises(backup.BackupError):
            backup.create_backup(self.spec, self.source / "empty" / ".." / "new-backup")
        self.assertFalse((self.source / "new-backup").exists())

    def test_sqlite_backup_has_a_deadline(self):
        with patch.object(backup.time, "monotonic", side_effect=[0, 61]):
            with self.assertRaisesRegex(backup.BackupError, "timed out"):
                backup.create_backup(self.spec, self.output)
        self.assertFalse((self.output / "manifest.json").exists())

    def test_cli_requires_operator_confirmation_and_can_restore(self):
        from contextlib import redirect_stdout, redirect_stderr
        from io import StringIO
        spec = self.root / "spec.json"
        spec.write_text(json.dumps(self.spec))
        with redirect_stderr(StringIO()), self.assertRaises(SystemExit):
            backup.main(["create", "--spec", str(spec), "--output", str(self.output)])
        with redirect_stdout(StringIO()):
            self.assertEqual(backup.main(["create", "--spec", str(spec), "--output", str(self.output), "--writers-stopped"]), 0)
            self.assertEqual(backup.main(["verify", str(self.output)]), 0)
            self.assertEqual(backup.main(["restore", str(self.output), "--output", str(self.root / "restored")]), 0)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.output / "manifest.json").stat().st_mode & 0o777, 0o600)

    def test_optional_absence_is_checked_not_silently_assumed(self):
        optional = self.root / "private-materials"
        self.spec['absent_optional'] = [str(optional)]
        backup.create_backup(self.spec, self.output)
        optional.mkdir()
        with self.assertRaisesRegex(backup.BackupError, "declared absent exists"):
            backup.create_backup(self.spec, self.root / "second-backup")
        self.assertFalse((self.root / "second-backup").exists())

    def test_direct_sqlite_in_place_change_is_detected(self):
        copy = backup._copy
        def changing_copy(source, target):
            copy(source, target)
            raw = self.database.read_bytes()
            self.database.write_bytes(raw.replace('真实原件'.encode(), '修改原件'.encode()))
        with patch.object(backup, "_copy", side_effect=changing_copy):
            with self.assertRaisesRegex(backup.BackupError, "SQLite changed"):
                backup.create_backup(self.spec, self.output)
        self.assertFalse((self.output / "manifest.json").exists())

    def test_failed_final_sync_never_leaves_completion_marker(self):
        backup.create_backup(self.spec, self.output)
        for label, operation in [
            ("create", lambda target: backup.create_backup(self.spec, target)),
            ("restore", lambda target: backup.restore_backup(self.output, target)),
        ]:
            target = self.root / (label + "-failed")
            with patch.object(backup, "_sync_directories", side_effect=[None, OSError("injected sync failure")]):
                with self.assertRaises(OSError):
                    operation(target)
            self.assertFalse((target / "manifest.json").exists())
            with self.assertRaises(FileNotFoundError):
                backup.verify_backup(target)

    def test_failed_final_validation_never_leaves_completion_marker(self):
        with patch.object(backup, "verify_backup", side_effect=backup.BackupError("injected verification failure")):
            with self.assertRaises(backup.BackupError):
                backup.create_backup(self.spec, self.output)
        self.assertFalse((self.output / "manifest.json").exists())
        self.assertTrue((self.output / "manifest.pending.json").exists())

    def _remote_catalog(self):
        catalog = self.source / "remote.sqlite3"
        raw = b"registered remote original"
        digest = hashlib.sha256(raw).hexdigest()
        with closing(sqlite3.connect(catalog)) as db:
            db.execute("CREATE TABLE document_versions(document_version_id TEXT PRIMARY KEY, source_sha256 TEXT, byte_length INTEGER)")
            db.execute("INSERT INTO document_versions VALUES('doc-remote',?,?)", (digest, len(raw)))
            db.commit()
        store = LocalObjectStore(self.root / "remote-store")
        store.put_if_absent(digest, raw)
        self.spec["resources"].append({"name": "state/remote.sqlite3", "source": str(catalog), "kind": "sqlite"})
        self.spec["object_references"] = [{"resource": "state/remote.sqlite3", "kind": "document_catalog", "scope": "public"}]
        return raw, digest, {"public": StoreBinding("controlled", "source/public/frozen", store)}

    def test_registered_remote_objects_are_backed_up_from_frozen_metadata_and_restored_isolated(self):
        raw, digest, sources = self._remote_catalog()
        manifest = backup.create_backup(self.spec, self.output, object_stores=sources)
        self.assertEqual(manifest["objects"][0]["sha256"], digest)
        self.assertIn("state/remote.sqlite3:document_versions:doc-remote",
                      manifest["objects"][0]["metadata_references"])
        target_store = LocalObjectStore(self.root / "isolated-object-target")
        targets = {"public": StoreBinding("controlled", "recovery/new-prefix", target_store)}
        restored = self.root / "isolated-restored"
        def verify_recovery(root, manifest, bindings):
            with closing(sqlite3.connect(root / "state/remote.sqlite3")) as db:
                row = db.execute("SELECT document_version_id,source_sha256 FROM document_versions").fetchone()
            self.assertEqual(row, ("doc-remote", digest))
            self.assertEqual(bindings["public"].store.get_verified(digest, len(raw)), raw)
            return {"frozen_reader_source_identities_verified": True,
                    "document_version_id": row[0], "source_sha256": row[1]}
        restored_manifest = backup.restore_backup(
            self.output, restored, object_stores=targets, isolated_object_target=True,
            recovery_verifier=verify_recovery)
        self.assertEqual(target_store.get_verified(digest, len(raw)), raw)
        self.assertFalse(restored_manifest["recovery"]["model_enabled"])
        self.assertFalse(restored_manifest["recovery"]["worker_enabled"])
        self.assertEqual(backup.verify_backup(restored), restored_manifest)

    def test_object_restore_refuses_source_identity_and_existing_root(self):
        _raw, _digest, sources = self._remote_catalog()
        backup.create_backup(self.spec, self.output, object_stores=sources)
        with self.assertRaisesRegex(backup.BackupError, "isolated"):
            backup.restore_backup(self.output, self.root / "unsafe", object_stores=sources,
                                  isolated_object_target=True, recovery_verifier=lambda *_: {})
        existing = self.root / "existing-restore"
        existing.mkdir()
        with self.assertRaises(FileExistsError):
            backup.restore_backup(self.output, existing, object_stores={})

    def test_missing_registered_object_prevents_completion(self):
        _raw, digest, sources = self._remote_catalog()
        (sources["public"].store.objects_path / digest).unlink()
        with self.assertRaisesRegex(backup.BackupError, "missing or corrupt"):
            backup.create_backup(self.spec, self.output, object_stores=sources)
        self.assertFalse((self.output / "manifest.json").exists())

    def test_snapshot_cache_is_explicitly_excluded(self):
        cache = self.source / "snapshot-cache"
        cache.mkdir()
        (cache / "only-copy").write_bytes(b"expendable")
        self.spec["resources"][0]["exclude"] = ["snapshot-cache"]
        backup.create_backup(self.spec, self.output)
        self.assertFalse((self.output / "state/documents/snapshot-cache").exists())
        backup.verify_backup(self.output)

    def test_foreign_key_violation_is_not_silently_accepted(self):
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE child(parent INTEGER REFERENCES records(id))")
            db.execute("INSERT INTO child VALUES(99)")
            db.commit()
        with self.assertRaisesRegex(backup.BackupError, "foreign-key"):
            backup.create_backup(self.spec, self.output)


if __name__ == "__main__":
    unittest.main()
