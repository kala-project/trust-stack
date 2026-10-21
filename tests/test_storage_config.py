import base64
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from workbench.build import build_database
from workbench.chat import ChatError, ChatService
from workbench.documents import DocumentService
from workbench.native_data import NativeDataService
from workbench.server import EvidenceHTTPServer
from workbench.source_acquisition import PublicSourceAcquisition
from workbench.storage_config import StorageConfiguration, StorageConfigError
from workbench.worker import create_worker_service
from tests.test_chat import FakeProvider


class MemoryObjectStore:
    objects = {}
    created = []

    def __init__(self, *, prefix, **options):
        self.prefix = prefix
        self.options = options
        self.created.append(self)

    def put_if_absent(self, digest, raw):
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("bad digest")
        key = (self.prefix, digest)
        existing = self.objects.setdefault(key, raw)
        if existing != raw:
            raise ValueError("conflict")

    def get_verified(self, digest, expected_size=None):
        raw = self.objects[(self.prefix, digest)]
        if expected_size is not None and len(raw) != expected_size:
            raise ValueError("bad size")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("tampered")
        return raw


class StorageConfigTests(unittest.TestCase):
    def setUp(self):
        MemoryObjectStore.objects = {}
        MemoryObjectStore.created = []
        self.payload = {
            "version": 1,
            "backend": "s3",
            "deployment": "test-deployment",
            "bucket": "valid-test-bucket",
            "endpoint_url": "http://localhost:9090",
            "region_name": "us-east-1",
            "local_emulator": True,
            "credentials": {"test_credentials": True},
        }

    def config(self, **changes):
        payload = {**self.payload, **changes}
        return StorageConfiguration(payload, environ={}, store_factory=MemoryObjectStore)

    def test_strict_config_rejects_unknown_types_namespace_and_missing_credentials(self):
        cases = [
            {**self.payload, "unknown": True},
            {**self.payload, "local_emulator": 1},
            {**self.payload, "deployment": "raw/private/escape"},
            {**self.payload, "credentials": {"test_credentials": False}},
            {
                **self.payload,
                "local_emulator": False,
                "endpoint_url": "https://s3.example.test",
                "credentials": {
                    "access_key_id_env": "S3_ACCESS_KEY",
                    "secret_access_key_env": "S3_SECRET_KEY",
                },
            },
        ]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(StorageConfigError):
                StorageConfiguration(payload, environ={}, store_factory=MemoryObjectStore)
        self.assertEqual(MemoryObjectStore.created, [])

    def test_environment_credentials_are_resolved_without_entering_identity(self):
        payload = {
            **self.payload,
            "local_emulator": False,
            "endpoint_url": "https://s3.example.test",
            "credentials": {
                "access_key_id_env": "S3_ACCESS_KEY",
                "secret_access_key_env": "S3_SECRET_KEY",
                "session_token_env": "S3_SESSION_TOKEN",
            },
        }
        config = StorageConfiguration(
            payload,
            environ={"S3_ACCESS_KEY": "access", "S3_SECRET_KEY": "secret", "S3_SESSION_TOKEN": "token"},
            store_factory=MemoryObjectStore,
        )
        encoded = json.dumps(config.identity)
        self.assertNotIn("access", encoded)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("token", encoded)
        self.assertEqual(config.public_store().options["access_key_id"], "access")

    def test_private_namespace_is_only_sha256_of_verified_conversation_id(self):
        config = self.config()
        first = config.private_store("conversation-one")
        second = config.private_store("conversation-two")
        self.assertEqual(
            first.prefix,
            "raw/private/test-deployment/" + hashlib.sha256(b"conversation-one").hexdigest(),
        )
        self.assertNotEqual(first.prefix, second.prefix)
        digest = hashlib.sha256(b"private").hexdigest()
        first.put_if_absent(digest, b"private")
        with self.assertRaises(KeyError):
            second.get_verified(digest)

    def test_native_snapshot_namespaces_are_separate_and_owner_scoped(self):
        config = self.config(max_bytes=37 * 1024 * 1024)
        public = config.native_public_store()
        private = config.native_private_store("conversation-one")
        other = config.native_private_store("conversation-two")
        self.assertEqual(public.prefix, "native-sql/public/test-deployment")
        self.assertEqual(
            private.prefix,
            "native-sql/private/test-deployment/" + hashlib.sha256(b"conversation-one").hexdigest())
        self.assertNotEqual(private.prefix, other.prefix)
        self.assertEqual(config.max_object_bytes, 37 * 1024 * 1024)
        self.assertIn(config.config_sha256, config.native_store_identity)

    def test_identity_mismatch_and_nonempty_local_store_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "documents"
            local = DocumentService(root)
            source = Path(temporary) / "source.html"
            source.write_bytes(b"<!doctype html><html><body>legacy local bytes</body></html>")
            local.register_file(source, company_id="MSFT", source_key="legacy", title="Legacy")
            with self.assertRaisesRegex(StorageConfigError, "explicit migration") as caught:
                self.config().register_public_root(root)
            self.assertEqual(caught.exception.code, "storage_migration_required")

            materials = Path(temporary) / "chat.sqlite3.materials"
            legacy_objects = materials / hashlib.sha256(b"legacy-conversation").hexdigest() / "objects"
            legacy_objects.mkdir(parents=True)
            (legacy_objects / hashlib.sha256(b"private legacy").hexdigest()).write_bytes(b"private legacy")
            with self.assertRaises(StorageConfigError) as private_caught:
                self.config().register_private_root(materials)
            self.assertEqual(private_caught.exception.code, "storage_migration_required")

            empty = Path(temporary) / "empty"
            first = self.config()
            first.register_public_root(empty)
            changed = self.config(deployment="different-deployment")
            with self.assertRaises(StorageConfigError) as mismatch:
                changed.register_public_root(empty)
            self.assertEqual(mismatch.exception.code, "storage_identity_mismatch")


    def test_missing_local_bytes_still_require_explicit_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "documents"
            docs = DocumentService(root)
            raw = b"<html><body>missing original</body></html>"
            docs.register_bytes(raw, company_id="MSFT", source_key="old", title="Old",
                                expected_sha256=hashlib.sha256(raw).hexdigest())
            for path in (root / "objects").iterdir():
                path.unlink()  # Only this test's temporary source objects.
            with self.assertRaises(StorageConfigError) as caught:
                self.config().register_public_root(root)
            self.assertEqual(caught.exception.code, "storage_migration_required")
            self.assertFalse((root / ".object-storage-identity.json").exists())

    def test_identity_symlink_and_nonlocal_emulator_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "documents"
            config = self.config()
            config.register_public_root(root)
            identity = root / ".object-storage-identity.json"
            target = root / "moved-identity"
            identity.rename(target)
            identity.symlink_to(target)
            with self.assertRaises(StorageConfigError):
                config.register_public_root(root)
        with self.assertRaises(StorageConfigError):
            self.config(endpoint_url="https://remote.example")


class StorageWiringTests(unittest.TestCase):
    def setUp(self):
        MemoryObjectStore.objects = {}
        MemoryObjectStore.created = []
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.evidence = self.root / "evidence.sqlite3"
        build_database(db_path=self.evidence)
        self.documents_root = self.root / "documents"
        DocumentService(self.documents_root)
        self.payload = {
            "version": 1,
            "backend": "s3",
            "deployment": "wiring-test",
            "bucket": "valid-test-bucket",
            "endpoint_url": "http://localhost:9090",
            "local_emulator": True,
            "credentials": {"test_credentials": True},
        }

    def tearDown(self):
        self.temporary.cleanup()

    def config(self, **changes):
        return StorageConfiguration(
            {**self.payload, **changes}, environ={}, store_factory=MemoryObjectStore)

    def test_public_acquisition_documents_and_archive_share_remote_store(self):
        store = self.config().public_store()
        acquisition = PublicSourceAcquisition(self.documents_root, object_store=store)
        raw = b"<html><body>shared remote source bytes</body></html>"
        version = acquisition.documents.register_bytes(
            raw, company_id="MSFT", source_key="wiring", title="Wiring contract",
            media_type="text/html")
        digest = hashlib.sha256(raw).hexdigest()
        self.assertEqual(store.get_verified(digest, len(raw)), raw)
        self.assertEqual(acquisition.documents.read_raw_version(
            company_id="MSFT", document_version_id=version["document_version_id"]), raw)
        self.assertEqual(acquisition.archive.object_store.get_verified(digest, len(raw)), raw)
        self.assertFalse(any((self.documents_root / "objects").iterdir()))

    def test_session_materials_use_isolated_stores_after_owner_validation(self):
        config = self.config()
        public = DocumentService(self.documents_root, object_store=config.public_store())
        chat = ChatService(
            self.evidence, self.root / "chat.sqlite3", provider=FakeProvider(),
            document_service=public, material_object_store_factory=config.private_store,
        )
        try:
            first = chat.create_conversation("MSFT")["conversation_id"]
            second = chat.create_conversation("MSFT")["conversation_id"]
            uploaded = chat.materials.upload(
                first, "notes.txt", base64.b64encode(b"private session bytes").decode(), True)
            self.assertEqual(chat.materials.raw(first, uploaded["document_version_id"])[0],
                             b"private session bytes")
            self.assertEqual(chat.materials.list(second)["materials"], [])
            private_prefixes = {prefix for prefix, _digest in MemoryObjectStore.objects
                                if prefix.startswith("raw/private/")}
            self.assertEqual(private_prefixes, {
                "raw/private/wiring-test/" + hashlib.sha256(first.encode()).hexdigest()
            })
            before = len(MemoryObjectStore.created)
            with self.assertRaises(ChatError):
                chat.materials._store("00000000-0000-0000-0000-000000000000", create=True)
            self.assertEqual(len(MemoryObjectStore.created), before)
        finally:
            chat.close()

    def test_api_and_worker_reject_different_identity_for_same_roots(self):
        first = self.config()
        server = EvidenceHTTPServer(
            0, self.evidence, chat_db=self.root / "chat.sqlite3",
            documents_root=self.documents_root, provider=FakeProvider(),
            storage_config=first,
        )
        server.server_close()
        with self.assertRaises(StorageConfigError) as caught:
            create_worker_service(
                self.evidence, self.root / "chat.sqlite3", documents_root=self.documents_root,
                provider=FakeProvider(), storage_config=self.config(deployment="other-wiring-test"),
            )
        self.assertEqual(caught.exception.code, "storage_identity_mismatch")

    def test_worker_rejects_different_public_metadata_root_for_same_chat(self):
        config = self.config()
        first = create_worker_service(
            self.evidence, self.root / "chat.sqlite3", documents_root=self.documents_root,
            provider=FakeProvider(), storage_config=config)
        first.close()
        other = self.root / "other-documents"
        DocumentService(other)
        with self.assertRaises(StorageConfigError) as caught:
            create_worker_service(self.evidence, self.root / "chat.sqlite3", documents_root=other,
                                  provider=FakeProvider(), storage_config=config)
        self.assertEqual(caught.exception.code, "storage_identity_mismatch")

    def test_worker_accepts_registered_identity_and_uses_remote_public_store(self):
        config = self.config()
        service = create_worker_service(
            self.evidence, self.root / "chat.sqlite3", documents_root=self.documents_root,
            provider=FakeProvider(), storage_config=config,
        )
        try:
            raw = b"<html><body>worker remote source bytes</body></html>"
            version = service.document_service.register_bytes(
                raw, company_id="MSFT", source_key="worker-wiring", title="Worker wiring",
                media_type="text/html")
            self.assertEqual(config.public_store().get_verified(
                hashlib.sha256(raw).hexdigest(), len(raw)), raw)
            self.assertEqual(service.document_service.read_raw_version(
                company_id="MSFT", document_version_id=version["document_version_id"]), raw)
            self.assertFalse(any((self.documents_root / "objects").iterdir()))
        finally:
            service.close()

    def test_runtime_injects_dedicated_native_snapshot_stores(self):
        NativeDataService(self.root / "native-data")
        config = self.config(max_bytes=64 * 1024 * 1024)
        service = create_worker_service(
            self.evidence, self.root / "chat.sqlite3", documents_root=self.documents_root,
            provider=FakeProvider(), storage_config=config)
        try:
            native = service.native_data
            self.assertEqual(native.public_object_store.prefix, "native-sql/public/wiring-test")
            self.assertEqual(native.object_store_max_bytes, 64 * 1024 * 1024)
            private = native.private_object_store_factory("verified-owner")
            self.assertEqual(
                private.prefix,
                "native-sql/private/wiring-test/" + hashlib.sha256(b"verified-owner").hexdigest())
        finally:
            service.close()


if __name__ == "__main__":
    unittest.main()
