from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import tempfile
import unittest

from workbench.backup import BackupError, create_backup, restore_backup, verify_backup
from workbench.object_store_operator import (
    OperatorError, assemble_configured_bindings, build_backup_spec,
    build_cutover_proof, configured_bindings, main, recovery_verifier,
    register_recovered_metadata, verify_quiescence,
)
from workbench.documents import DocumentService
from workbench.object_migration import StoreBinding, build_manifest, manifest_sha256
from workbench.runtime_config import RuntimePaths
from workbench.storage_config import StorageConfiguration, StorageConfigError
from workbench.raw_storage import LocalObjectStore


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.owner = "conversation-private-1"
        self.paths = RuntimePaths(
            evidence=self.root / "evidence.sqlite3", chat=self.root / "chat.sqlite3",
            wiki=self.root / "wiki.sqlite3", sample=self.root / "sample.sqlite3",
            documents_root=self.root / "documents",
            native_registry=self.root / "native-data/registry.sqlite",
        )
        for path in (self.paths.evidence, self.paths.wiki, self.paths.sample):
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE durable_row(id TEXT PRIMARY KEY, value TEXT)")
                db.execute("INSERT INTO durable_row VALUES('row','preserved')")
                db.commit()
        with closing(sqlite3.connect(self.paths.chat)) as db:
            db.executescript("""
                CREATE TABLE conversations(conversation_id TEXT PRIMARY KEY);
                CREATE TABLE verification_candidates(candidate_id TEXT PRIMARY KEY, payload TEXT);
                CREATE TABLE verification_findings(finding_id TEXT PRIMARY KEY, payload TEXT);
                CREATE TABLE report_reader_versions(
                  report_id TEXT PRIMARY KEY, artifact_id TEXT, payload_sha256 TEXT, payload_json TEXT);
                CREATE TABLE report_artifact_snapshots(
                  report_id TEXT PRIMARY KEY, artifact_id TEXT, conversation_id TEXT,
                  message_id TEXT, run_id TEXT, artifact_sha256 TEXT, artifact_json TEXT);
            """)
            db.execute("INSERT INTO conversations VALUES(?)", (self.owner,))
            db.execute("INSERT INTO verification_candidates VALUES('candidate-1','candidate bytes')")
            db.execute("INSERT INTO verification_findings VALUES('finding-1','verification bytes')")
            payload = {"report": {"message_id": "historical-message"}}
            digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                separators=(",", ":")).encode()).hexdigest()
            payload["payload_sha256"] = digest
            db.execute("INSERT INTO report_reader_versions VALUES(?,?,?,?)",
                       ("report-historical", None, digest, json.dumps(
                           payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
            db.commit()
        self.paths.documents_root.mkdir()
        self._document_catalog(self.paths.documents_root / "catalog.sqlite3")
        materials = self.paths.materials_root / hashlib.sha256(self.owner.encode()).hexdigest()
        materials.mkdir(parents=True)
        self._document_catalog(materials / "catalog.sqlite3")
        self.raw = self.root / "raw"
        self.raw.mkdir()
        (self.raw / "financial.html").write_bytes(b"raw15 preserved")
        for name in ("apple-website-terms.html", "microsoft-terms-of-use.html"):
            (self.raw / name).write_bytes(name.encode())

    @staticmethod
    def _document_catalog(path):
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE document_versions(document_version_id TEXT PRIMARY KEY, source_sha256 TEXT, byte_length INTEGER)")
            db.commit()

    def _config(self, deployment, secrets=None):
        base = self.root / ("stores-" + deployment)
        def factory(*, prefix, **options):
            return LocalObjectStore(base / prefix)
        payload = {
            "version": 1, "backend": "s3", "deployment": deployment,
            "bucket": "valid-test-bucket", "endpoint_url": "http://localhost:9090",
            "region_name": "us-east-1", "local_emulator": True,
            "credentials": {"test_credentials": True},
        }
        if secrets is not None:
            payload.update(local_emulator=False, endpoint_url="https://s3.example.test",
                           credentials={"access_key_id_env": "ACCESS", "secret_access_key_env": "SECRET"})
        return StorageConfiguration(payload, environ=secrets or {}, store_factory=factory)

    def _terms(self):
        return [self.raw / "apple-website-terms.html", self.raw / "microsoft-terms-of-use.html"]

    def _add_native_object(self, config, *, owner):
        raw = b"native sqlite exact bytes"
        digest = hashlib.sha256(raw).hexdigest()
        payload = json.dumps({"sha256": digest, "byte_length": len(raw)}, separators=(",", ":"))
        self.paths.native_registry.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(self.paths.native_registry)) as db:
            db.executescript("""
                CREATE TABLE native_sources(snapshot_id TEXT PRIMARY KEY, owner TEXT, payload_json TEXT, payload_sha256 TEXT);
                CREATE TABLE native_snapshot_storage(snapshot_id TEXT PRIMARY KEY, owner TEXT, backend TEXT, backend_identity TEXT);
            """)
            db.execute("INSERT INTO native_sources VALUES(?,?,?,?)",
                       ("native-1", owner, payload, hashlib.sha256(payload.encode()).hexdigest()))
            db.execute("INSERT INTO native_snapshot_storage VALUES(?,?,?,?)",
                       ("native-1", owner, "object_store", config.native_store_identity))
            db.commit()
        store = config.native_public_store() if owner == "" else config.native_private_store(owner)
        store.put_if_absent(digest, raw)
        return raw, digest

    def test_full_object_aware_snapshot_and_isolated_restore_preserve_rows_hashes_and_scopes(self):
        source_config = self._config("source")
        public_raw, private_raw = (b"<html>public document</html>",
                                   b"<html>private material</html>")
        public_digest = hashlib.sha256(public_raw).hexdigest()
        private_digest = hashlib.sha256(private_raw).hexdigest()
        private_root = self.paths.materials_root / hashlib.sha256(self.owner.encode()).hexdigest()
        self.paths.documents_catalog.unlink()
        (private_root / "catalog.sqlite3").unlink()
        public_documents = DocumentService(self.paths.documents_root,
                                           object_store=source_config.public_store())
        private_documents = DocumentService(private_root,
                                            object_store=source_config.private_store(self.owner))
        source_config.register_public_root(self.paths.documents_root)
        source_config.register_private_root(self.paths.materials_root,
                                            public_root=self.paths.documents_root)
        public_version = public_documents.register_bytes(
            public_raw, company_id="TEST", source_key="public", title="Public",
            media_type="text/html")
        private_version = private_documents.register_bytes(
            private_raw, company_id="TEST", source_key="private", title="Private",
            media_type="text/html")
        native_raw, native_digest = self._add_native_object(source_config, owner=self.owner)

        spec, inventory = build_backup_spec(
            self.paths, rebuild_raw_root=self.raw, terms_pages=self._terms())
        self.assertIn(str(self.paths.chat), inventory["databases"])
        self.assertEqual(inventory["rebuild_resources"], [
            "rebuild/data-raw-core", "rebuild/terms/apple-website-terms.html",
            "rebuild/terms/microsoft-terms-of-use.html"])
        bindings = assemble_configured_bindings(self.paths, source_config)
        self.assertEqual(set(bindings), {"public", "private:" + self.owner,
                                        "native:private:" + self.owner})
        backup = self.root / "backup"
        manifest = create_backup(spec, backup, object_stores=bindings)
        self.assertEqual({item["scope"] for item in manifest["objects"]},
                         {"public", "private:" + self.owner, "native:private:" + self.owner})
        source_hashes = {path.relative_to(backup).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in backup.rglob("*") if path.is_file()}

        # Disaster recovery must not consult the now-unavailable registered source.
        shutil.rmtree(self.root / "stores-source")
        target_config = self._config("recovery")
        targets = configured_bindings(target_config, conversations=[self.owner], native_owners=[self.owner])
        restored = self.root / "restored"
        recovered = restore_backup(
            backup, restored, object_stores=targets, isolated_object_target=True,
            recovery_verifier=recovery_verifier,
            recovery_registrar=lambda root, value: register_recovered_metadata(
                root, value, target_config))
        proof = recovered["recovery"]["verification"]
        self.assertTrue(proof["frozen_reader_source_identities_verified"])
        self.assertFalse(proof["coverage_complete"])
        self.assertEqual(proof["historical_incomplete_artifacts"], 1)
        with closing(sqlite3.connect(restored / "state/chat.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT payload FROM verification_candidates").fetchone()[0], "candidate bytes")
            self.assertEqual(db.execute("SELECT payload FROM verification_findings").fetchone()[0], "verification bytes")
            saved = db.execute("SELECT payload_sha256,payload_json FROM report_reader_versions").fetchone()
            self.assertEqual(saved[0], json.loads(saved[1])["payload_sha256"])
        self.assertEqual(targets["public"].store.get_verified(public_digest, len(public_raw)), public_raw)
        self.assertEqual(targets["private:" + self.owner].store.get_verified(private_digest, len(private_raw)), private_raw)
        self.assertEqual(targets["native:private:" + self.owner].store.get_verified(native_digest, len(native_raw)), native_raw)
        recovered_public = DocumentService(restored / "state/documents",
                                           object_store=target_config.public_store())
        restored_paths = RuntimePaths.resolve(
            restored / "state/evidence.sqlite3", chat_db=restored / "state/chat.sqlite3",
            wiki_db=restored / "state/wiki.sqlite3", sample_db=restored / "state/sample.sqlite3",
            documents_root=restored / "state/documents")
        recovered_private = DocumentService(
            restored_paths.materials_root / hashlib.sha256(self.owner.encode()).hexdigest(),
            object_store=target_config.private_store(self.owner))
        self.assertEqual(recovered_public.read_raw_version(
            company_id="TEST", document_version_id=public_version["document_version_id"]), public_raw)
        self.assertEqual(recovered_private.read_raw_version(
            company_id="TEST", document_version_id=private_version["document_version_id"]), private_raw)
        target_config.register_public_root(restored_paths.documents_root)
        target_config.register_private_root(restored_paths.materials_root,
                                            public_root=restored_paths.documents_root)
        restored_manifest = verify_backup(restored)
        self.assertEqual(restored_manifest["recovery"]["verification"], proof)
        self.assertFalse((restored / "state/materials").exists())
        self.assertTrue(all("state/materials" not in entry["path"]
                            for entry in restored_manifest["files"]))
        self.assertTrue(all("state/materials" not in reference
                            for item in restored_manifest["objects"]
                            for reference in item["metadata_references"]))
        self.assertTrue(all("state/materials" not in resource["name"]
                            for resource in restored_manifest["specification"]["resources"]))
        self.assertTrue(all("state/materials" not in reference["resource"]
                            for reference in restored_manifest["specification"]["object_references"]))
        self.assertTrue(all("state/materials" not in directory
                            for directory in restored_manifest["directories"]))
        self.assertEqual(source_hashes, {
            path.relative_to(backup).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in backup.rglob("*") if path.is_file()})
        self.assertEqual((restored / "rebuild/data-raw-core/financial.html").read_bytes(), b"raw15 preserved")
        self.assertFalse((restored / "rebuild/data-raw-core/apple-website-terms.html").exists())

    def test_native_only_restore_registers_empty_public_catalog_with_exact_proof(self):
        source_config = self._config("native-source")
        source_config.register_public_root(self.paths.documents_root)
        native_raw, native_digest = self._add_native_object(source_config, owner="")
        spec, _inventory = build_backup_spec(
            self.paths, rebuild_raw_root=self.raw, terms_pages=self._terms())
        backup = self.root / "native-backup"
        manifest = create_backup(spec, backup,
                                 object_stores=assemble_configured_bindings(self.paths, source_config))
        self.assertEqual([item["scope"] for item in manifest["objects"]], ["native:public"])
        source_hashes = {path.relative_to(backup).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in backup.rglob("*") if path.is_file()}
        shutil.rmtree(self.root / "stores-native-source")

        target_config = self._config("native-recovery")
        targets = configured_bindings(target_config, native_owners=[""])
        wrong_config = self._config("wrong-recovery")
        with self.assertRaisesRegex(BackupError, "storage registration failed"):
            restore_backup(
                backup, self.root / "wrong-target-restore", object_stores=targets,
                isolated_object_target=True, recovery_verifier=recovery_verifier,
                recovery_registrar=lambda root, value: register_recovered_metadata(
                    root, value, wrong_config))

        def partial_verifier(root, value, bindings):
            proof = recovery_verifier(root, value, bindings)
            proof["verified_objects"] = 0
            return proof

        with self.assertRaisesRegex(BackupError, "storage registration failed"):
            restore_backup(
                backup, self.root / "partial-restore", object_stores=targets,
                isolated_object_target=True, recovery_verifier=partial_verifier,
                recovery_registrar=lambda root, value: register_recovered_metadata(
                    root, value, target_config))

        restored = self.root / "native-restored"
        recovered = restore_backup(
            backup, restored, object_stores=targets, isolated_object_target=True,
            recovery_verifier=recovery_verifier,
            recovery_registrar=lambda root, value: register_recovered_metadata(
                root, value, target_config))
        self.assertEqual(recovered["recovery"]["verification"]["verified_objects"], 1)
        self.assertEqual(targets["native:public"].store.get_verified(
            native_digest, len(native_raw)), native_raw)
        target_config.register_public_root(restored / "state/documents")
        identity = json.loads((restored / "state/documents/.object-storage-identity.json").read_text())
        self.assertEqual(identity["recovery_registration"]["source_identities"], [
            f"s3:{source_config.config_sha256}:raw/public/native-source"])
        self.assertEqual(verify_backup(restored), recovered)
        self.assertEqual(source_hashes, {
            path.relative_to(backup).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in backup.rglob("*") if path.is_file()})

    def test_rejects_unexpected_namespace_malformed_owner_wrong_identity_and_incomplete_snapshot(self):
        unexpected = self.paths.materials_root / "not-an-authorized-hash"
        unexpected.mkdir()
        with self.assertRaisesRegex(OperatorError, "unexpected conversation"):
            build_backup_spec(self.paths, rebuild_raw_root=self.raw, terms_pages=self._terms())
        unexpected.rmdir()
        config = self._config("source")
        self._add_native_object(config, owner="unknown-conversation")
        with self.assertRaisesRegex(OperatorError, "authorized conversation"):
            build_backup_spec(self.paths, rebuild_raw_root=self.raw, terms_pages=self._terms())
        with closing(sqlite3.connect(self.paths.native_registry)) as db:
            db.execute("UPDATE native_sources SET owner=?", (self.owner,))
            db.execute("UPDATE native_snapshot_storage SET owner=?,backend_identity='wrong-source-identity'",
                       (self.owner,))
            db.commit()
        build_backup_spec(self.paths, rebuild_raw_root=self.raw, terms_pages=self._terms())
        with self.assertRaisesRegex(OperatorError, "backend identity"):
            assemble_configured_bindings(self.paths, config)
        self.paths.native_registry.unlink()
        self.paths.native_registry.parent.rmdir()
        with closing(sqlite3.connect(self.paths.chat)) as db:
            db.execute("INSERT INTO conversations VALUES(?)", (" bad-owner ",))
            db.commit()
        with self.assertRaisesRegex(OperatorError, "malformed"):
            build_backup_spec(self.paths, rebuild_raw_root=self.raw, terms_pages=self._terms())
        with closing(sqlite3.connect(self.paths.chat)) as db:
            db.execute("DELETE FROM conversations WHERE conversation_id=' bad-owner '")
            db.commit()
        self.paths.wiki.unlink()
        with self.assertRaisesRegex(OperatorError, "required application database"):
            build_backup_spec(self.paths, rebuild_raw_root=self.raw, terms_pages=self._terms())

    def test_generic_preserved_rebuild_file_is_singled_out_and_unsafe_paths_are_rejected(self):
        unusual = self.raw / "unrecognized-vendor-export.bin"
        unusual.write_bytes(b"raw rebuild input")
        spec, inventory = build_backup_spec(
            self.paths, rebuild_raw_root=self.raw, preserve_rebuild_files=[unusual])
        self.assertIn("rebuild/preserved/unrecognized-vendor-export.bin",
                      inventory["rebuild_resources"])
        core = next(item for item in spec["resources"] if item["name"] == "rebuild/data-raw-core")
        self.assertEqual(core["exclude"], [unusual.name])
        outside = self.root / "outside-secret"
        outside.write_bytes(b"secret")
        with self.assertRaisesRegex(OperatorError, "inside"):
            build_backup_spec(self.paths, rebuild_raw_root=self.raw,
                              preserve_rebuild_files=[outside])
        linked = self.raw / "linked-secret"
        linked.symlink_to(outside)
        with self.assertRaisesRegex(OperatorError, "symbolic"):
            build_backup_spec(self.paths, rebuild_raw_root=self.raw,
                              preserve_rebuild_files=[linked])

    def test_credentials_do_not_enter_inventory_binding_identity_or_errors(self):
        config = self._config("secretless", {"ACCESS": "wrong-access-secret", "SECRET": "wrong-secret-value"})
        spec, inventory = build_backup_spec(self.paths, rebuild_raw_root=self.raw, terms_pages=self._terms())
        bindings = assemble_configured_bindings(self.paths, config)
        encoded = json.dumps({"spec": spec, "inventory": inventory,
                              "identities": [item.identity for item in bindings.values()]})
        self.assertNotIn("wrong-access-secret", encoded)
        self.assertNotIn("wrong-secret-value", encoded)

    def test_writer_attestation_and_existing_restore_root_refused(self):
        with self.assertRaisesRegex(OperatorError, "attestation"):
            verify_quiescence(writers_stopped=False)
        with self.assertRaisesRegex(OperatorError, "still active"):
            verify_quiescence(writers_stopped=True, writer_pids=[os.getpid()])
        source = self.root / "standalone.sqlite3"
        with closing(sqlite3.connect(source)) as db:
            db.execute("CREATE TABLE row(id INTEGER PRIMARY KEY)")
        backup = self.root / "standalone-backup"
        create_backup({"version": "workbench-backup-spec/1", "resources": [
            {"name": "state.sqlite3", "source": str(source), "kind": "sqlite"}]}, backup)
        existing = self.root / "existing"
        existing.mkdir()
        with self.assertRaises(FileExistsError):
            restore_backup(backup, existing)

    def test_final_proof_rejects_an_attested_active_writer(self):
        arguments = [
            "proof", "--baseline", str(self.root / "baseline.json"),
            "--candidate", str(self.root / "candidate.json"),
            "--completion", str(self.root / "completion.json"),
            "--evidence", str(self.paths.evidence), "--chat", str(self.paths.chat),
            "--wiki", str(self.paths.wiki), "--sample", str(self.paths.sample),
            "--documents-root", str(self.paths.documents_root),
            "--native-registry", str(self.paths.native_registry),
            "--target-storage-config", str(self.root / "target.json"),
            "--output", str(self.root / "proof.json"), "--writers-stopped",
            "--writer-pid", str(os.getpid()),
        ]
        self.assertEqual(main(arguments), 2)
        self.assertFalse((self.root / "proof.json").exists())

    def _public_proof(self, config, root, raw=b"preserved object"):
        digest = hashlib.sha256(raw).hexdigest()
        with closing(sqlite3.connect(root / "catalog.sqlite3")) as db:
            db.execute("DELETE FROM document_versions")
            db.execute("INSERT INTO document_versions VALUES('registered',?,?)", (digest, len(raw)))
            db.commit()
        source_store = LocalObjectStore(root)
        source_store.put_if_absent(digest, raw)
        config.public_store().put_if_absent(digest, raw)
        source_identity = "local:" + str(root.resolve())
        target_identity = f"s3:{config.config_sha256}:raw/public/{config.deployment}"
        source = {"public": StoreBinding("local", source_identity, source_store)}
        target = {"public": StoreBinding("s3", target_identity, config.public_store())}
        manifest = build_manifest([{
            "scope": "public", "sha256": digest, "bytes": len(raw),
            "metadata_references": ["state/documents/catalog.sqlite3:document_versions:registered"],
        }], source, target, generation="registration")
        completion = {"version": "object-migration/1", "generation": "registration",
                      "complete": True, "verified": ["public:" + digest],
                      "manifest_sha256": manifest_sha256(manifest)}
        return build_cutover_proof(manifest, manifest, completion, source, target), source_identity, digest

    def test_reviewed_registration_preserves_original_identity_and_rejects_bad_proof(self):
        config = self._config("registration")
        local = self.paths.documents_root
        proof, original, _ = self._public_proof(config, local)
        with self.assertRaises(StorageConfigError):
            config.register_public_root(local)
        config.register_migrated_root(local, scope="public", proof=proof, original_identity=original)
        identity = json.loads((local / ".object-storage-identity.json").read_text())
        self.assertEqual(identity["migration_registration"]["original_identity"], original)
        config.register_public_root(local)
        bad = {**proof, "delta": {"missing": [["public", "lost", 1]]}}
        other = self.root / "other"
        other.mkdir()
        with self.assertRaises(StorageConfigError):
            config.register_migrated_root(other, scope="public", proof=bad,
                                          original_identity="local:" + str(other))

    def test_registration_rejects_reused_root_extra_catalog_reference_and_missing_target(self):
        config = self._config("negative-registration")
        proof, original, digest = self._public_proof(config, self.paths.documents_root)
        other = self.root / "different-documents"
        other.mkdir()
        self._document_catalog(other / "catalog.sqlite3")
        with self.assertRaisesRegex(StorageConfigError, "preserved source"):
            config.register_migrated_root(other, scope="public", proof=proof,
                                          original_identity="local:" + str(other))

        extra_raw = b"catalog reference added after proof"
        extra_digest = hashlib.sha256(extra_raw).hexdigest()
        LocalObjectStore(self.paths.documents_root).put_if_absent(extra_digest, extra_raw)
        with closing(sqlite3.connect(self.paths.documents_catalog)) as db:
            db.execute("INSERT INTO document_versions VALUES('extra',?,?)",
                       (extra_digest, len(extra_raw)))
            db.commit()
        with self.assertRaisesRegex(StorageConfigError, "current catalog"):
            config.register_migrated_root(self.paths.documents_root, scope="public", proof=proof,
                                          original_identity=original)
        with closing(sqlite3.connect(self.paths.documents_catalog)) as db:
            db.execute("DELETE FROM document_versions WHERE document_version_id='extra'")
            db.commit()
        target_object = (self.root / "stores-negative-registration/raw/public/negative-registration/objects"
                         / digest)
        target_object.unlink()
        with self.assertRaisesRegex(StorageConfigError, "target object closure"):
            config.register_migrated_root(self.paths.documents_root, scope="public", proof=proof,
                                          original_identity=original)
        target_object.write_bytes(b"tampered target")
        with self.assertRaisesRegex(StorageConfigError, "target object closure"):
            config.register_migrated_root(self.paths.documents_root, scope="public", proof=proof,
                                          original_identity=original)

    def test_private_registration_rejects_a_proof_covering_only_one_nonempty_root(self):
        config = self._config("partial-private")
        second_owner = "conversation-private-2"
        roots = {}
        for owner, raw in ((self.owner, b"first private"), (second_owner, b"second private")):
            child = self.paths.materials_root / hashlib.sha256(owner.encode()).hexdigest()
            child.mkdir(parents=True, exist_ok=True)
            catalog = child / "catalog.sqlite3"
            if not catalog.exists():
                self._document_catalog(catalog)
            digest = hashlib.sha256(raw).hexdigest()
            with closing(sqlite3.connect(catalog)) as db:
                db.execute("DELETE FROM document_versions")
                db.execute("INSERT INTO document_versions VALUES('private',?,?)", (digest, len(raw)))
                db.commit()
            LocalObjectStore(child).put_if_absent(digest, raw)
            roots[owner] = (child, raw, digest)

        child, raw, digest = roots[self.owner]
        config.private_store(self.owner).put_if_absent(digest, raw)
        object_scope = "private:" + self.owner
        source_identity = "local:" + str(child.resolve())
        target_identity = (f"s3:{config.config_sha256}:raw/private/{config.deployment}/"
                           f"{hashlib.sha256(self.owner.encode()).hexdigest()}")
        source = {object_scope: StoreBinding("local", source_identity, LocalObjectStore(child))}
        target = {object_scope: StoreBinding("s3", target_identity, config.private_store(self.owner))}
        manifest = build_manifest([{
            "scope": object_scope, "sha256": digest, "bytes": len(raw),
            "metadata_references": ["state/materials/catalog.sqlite3:document_versions:private"],
        }], source, target, generation="partial-private")
        completion = {"version": "object-migration/1", "generation": "partial-private",
                      "complete": True, "verified": [object_scope + ":" + digest],
                      "manifest_sha256": manifest_sha256(manifest)}
        proof = build_cutover_proof(manifest, manifest, completion, source, target)
        with self.assertRaisesRegex(StorageConfigError, "every existing private"):
            config.register_migrated_root(
                self.paths.materials_root, scope="private", proof=proof,
                original_identity="local:" + str(self.paths.materials_root.resolve()))


if __name__ == "__main__":
    unittest.main()
