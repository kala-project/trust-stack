import hashlib
import io
from pathlib import Path
import tempfile
import unittest

from workbench.documents import DocumentError, DocumentService
from workbench.raw_storage import RawStorageError
from workbench.s3_storage import S3ObjectStore
from workbench.source_archive import SourceArchive, SourceArchiveError


class ClientError(Exception):
    def __init__(self, code, status):
        super().__init__(f"unsafe upstream detail {code}")
        self.response = {
            "Error": {"Code": code, "Message": "secret upstream detail"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class Body(io.BytesIO):
    def __init__(self, raw):
        super().__init__(raw)
        self.was_closed = False

    def close(self):
        self.was_closed = True
        super().close()


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.put_calls = []
        self.get_calls = []
        self.put_failures = []
        self.get_failure = None
        self.bodies = []

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)
        if self.put_failures:
            failure = self.put_failures.pop(0)
            if failure is not None:
                raise failure
        key = (kwargs["Bucket"], kwargs["Key"])
        if key in self.objects:
            raise ClientError("PreconditionFailed", 412)
        self.objects[key] = kwargs["Body"]
        return {"ETag": "not-used-for-integrity"}

    def get_object(self, **kwargs):
        self.get_calls.append(kwargs)
        if self.get_failure is not None:
            raise self.get_failure
        key = (kwargs["Bucket"], kwargs["Key"])
        if key not in self.objects:
            raise ClientError("NoSuchKey", 404)
        body = Body(self.objects[key])
        self.bodies.append(body)
        return {"Body": body, "ETag": "not-a-sha256"}


class MemoryObjectStore:
    def __init__(self):
        self.objects = {}
        self.puts = []
        self.gets = []

    def put_if_absent(self, digest, raw):
        self.puts.append((digest, raw))
        existing = self.objects.setdefault(digest, raw)
        if existing != raw:
            raise RawStorageError("object_tampered", "stored source object failed hash verification")

    def get_verified(self, digest, expected_size=None):
        self.gets.append((digest, expected_size))
        try:
            raw = self.objects[digest]
        except KeyError as exc:
            raise RawStorageError("object_tampered", "stored source object is missing or unreadable") from exc
        if expected_size is not None and len(raw) != expected_size:
            raise RawStorageError("object_tampered", "stored source object has an unexpected size")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise RawStorageError("object_tampered", "stored source object failed hash verification")
        return raw


class S3ObjectStoreTests(unittest.TestCase):
    def store(self, client=None, **changes):
        values = {
            "bucket": "research-originals",
            "prefix": "raw/public",
            "client": client or FakeS3Client(),
            "endpoint_url": "https://s3.example.test",
            "max_bytes": 64,
        }
        values.update(changes)
        return S3ObjectStore(**values)

    def assert_code(self, code, call):
        with self.assertRaises(RawStorageError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_conditional_put_and_verified_read_close_body(self):
        client = FakeS3Client()
        store = self.store(client)
        raw = b"immutable source"
        digest = hashlib.sha256(raw).hexdigest()
        store.put_if_absent(digest, raw)
        self.assertEqual(client.put_calls, [{
            "Bucket": "research-originals",
            "Key": f"raw/public/sha256/{digest}",
            "Body": raw,
            "IfNoneMatch": "*",
        }])
        self.assertEqual(store.get_verified(digest, len(raw)), raw)
        self.assertTrue(client.bodies[-1].was_closed)

    def test_precondition_conflict_reads_and_verifies_existing_bytes(self):
        client = FakeS3Client()
        store = self.store(client)
        raw = b"same bytes"
        digest = hashlib.sha256(raw).hexdigest()
        key = (store.bucket, store._key(digest))
        client.objects[key] = raw
        client.put_failures = [ClientError("PreconditionFailed", 412)]
        store.put_if_absent(digest, raw)
        self.assertEqual(len(client.put_calls), 1)
        self.assertEqual(len(client.get_calls), 1)
        self.assertTrue(client.bodies[-1].was_closed)

        client.objects[key] = b"tampered!"
        client.put_failures = [ClientError("PreconditionFailed", 412)]
        self.assert_code("object_tampered", lambda: store.put_if_absent(digest, raw))

    def test_409_retries_are_bounded_then_existing_bytes_are_verified(self):
        client = FakeS3Client()
        store = self.store(client, conflict_retries=2)
        raw = b"eventually visible"
        digest = hashlib.sha256(raw).hexdigest()
        client.objects[(store.bucket, store._key(digest))] = raw
        client.put_failures = [ClientError("ConditionalRequestConflict", 409)] * 3
        store.put_if_absent(digest, raw)
        self.assertEqual(len(client.put_calls), 3)
        self.assertEqual(len(client.get_calls), 1)

    def test_missing_timeout_tamper_size_and_max_bytes_fail_closed(self):
        client = FakeS3Client()
        store = self.store(client, max_bytes=8)
        raw = b"12345678"
        digest = hashlib.sha256(raw).hexdigest()
        self.assert_code("object_tampered", lambda: store.get_verified(digest, len(raw)))

        client.get_failure = TimeoutError("password=do-not-leak")
        error = self.assert_code("object_store_unavailable", lambda: store.get_verified(digest))
        self.assertNotIn("do-not-leak", str(error))
        client.get_failure = None

        client.objects[(store.bucket, store._key(digest))] = b"87654321"
        self.assert_code("object_tampered", lambda: store.get_verified(digest, 8))
        self.assertTrue(client.bodies[-1].was_closed)

        too_large = b"123456789"
        self.assert_code(
            "invalid_file",
            lambda: store.put_if_absent(hashlib.sha256(too_large).hexdigest(), too_large),
        )
        oversized_digest = hashlib.sha256(too_large).hexdigest()
        client.objects[(store.bucket, store._key(oversized_digest))] = too_large
        self.assert_code("object_tampered", lambda: store.get_verified(oversized_digest))
        self.assertTrue(client.bodies[-1].was_closed)

    def test_config_validation_and_prefix_isolation(self):
        self.assert_code(
            "invalid_storage_config",
            lambda: self.store(prefix="../private"),
        )
        self.assert_code(
            "invalid_storage_config",
            lambda: self.store(endpoint_url="http://s3.example.test"),
        )
        self.assert_code(
            "invalid_storage_config",
            lambda: S3ObjectStore(
                bucket="research-originals", prefix="raw/private/session-a",
                endpoint_url="http://localhost:9090", local_emulator=True,
            ),
        )
        local = S3ObjectStore(
            bucket="research-originals", prefix="raw/private/session-a",
            endpoint_url="http://127.0.0.1:9090", local_emulator=True,
            access_key_id="explicit", secret_access_key="explicit-secret",
            client=FakeS3Client(),
        )
        digest = "a" * 64
        public = self.store(prefix="raw/public")
        self.assertNotEqual(public._key(digest), local._key(digest))
        self.assertEqual(local._key(digest), f"raw/private/session-a/sha256/{digest}")
        self.assert_code("object_tampered", lambda: local._key("../../arbitrary"))


class ObjectStoreInjectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_default_document_storage_remains_local_and_compatible(self):
        service = DocumentService(self.root / "local-documents")
        raw = b"<!doctype html><html><body>Local compatibility</body></html>"
        record = service.register_bytes(
            raw, company_id="ACME", source_key="local", title="Local",
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        )
        self.assertEqual((service.objects_path / record["source_sha256"]).read_bytes(), raw)
        self.assertEqual(
            service.read_raw_version(
                company_id="ACME", document_version_id=record["document_version_id"]
            ),
            raw,
        )

    def test_document_service_uses_injected_store_and_keeps_catalog_local(self):
        backend = MemoryObjectStore()
        root = self.root / "remote-documents"
        service = DocumentService(root, object_store=backend)
        raw = b"<!doctype html><html><body>Remote bytes</body></html>"
        record = service.register_bytes(
            raw, company_id="ACME", source_key="remote", title="Remote",
            expected_sha256=hashlib.sha256(raw).hexdigest(),
        )
        self.assertTrue((root / "catalog.sqlite3").is_file())
        self.assertFalse((root / "objects").exists())
        self.assertEqual(backend.puts, [(record["source_sha256"], raw)])
        self.assertEqual(
            service.read_raw_version(
                company_id="ACME", document_version_id=record["document_version_id"]
            ),
            raw,
        )
        backend.objects[record["source_sha256"]] = b"tampered"
        with self.assertRaises(DocumentError) as caught:
            service.read_raw_version(
                company_id="ACME", document_version_id=record["document_version_id"]
            )
        self.assertEqual(caught.exception.code, "object_tampered")

    def test_source_archive_uses_injected_store_and_keeps_ledger_local(self):
        backend = MemoryObjectStore()
        root = self.root / "remote-archive"
        archive = SourceArchive(root, object_store=backend)
        raw = b"<!doctype html><html><body>Archive bytes</body></html>"
        path = self.root / "source.html"
        path.write_bytes(raw)
        config = {
            "source_id": "issuer-report", "title": "Issuer report", "company_id": "ACME",
            "url": "https://example.com/report", "allowed_urls": ["https://example.com/report"],
            "published_at": "2026-01-01", "rights_status": "local_only",
            "media_types": ["text/html"], "max_bytes": 1000, "timeout_seconds": 2,
        }
        record = archive.import_local(
            config, path, expected_sha256=hashlib.sha256(raw).hexdigest(),
            expected_size=len(raw), media_type="text/html",
        )
        self.assertTrue((root / "source_acquisitions.sqlite3").is_file())
        self.assertFalse((root / "objects").exists())
        self.assertEqual(archive.read("issuer-report", source_version=1), raw)
        backend.objects.pop(record["source_sha256"])
        with self.assertRaises(SourceArchiveError) as caught:
            archive.read("issuer-report", source_version=1)
        self.assertEqual(caught.exception.code, "object_tampered")


if __name__ == "__main__":
    unittest.main()
