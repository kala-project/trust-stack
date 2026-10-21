#!/usr/bin/env python3
"""Opt-in live SDK contract against the fixed localhost S3Mock, never live reports."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.documents import DocumentService
from workbench.raw_storage import RawStorageError
from workbench.s3_storage import S3ObjectStore
from workbench.source_archive import SourceArchive


def store(prefix):
    return S3ObjectStore(bucket='trust-stack-dev', prefix=prefix,
                         endpoint_url='http://127.0.0.1:19091', local_emulator=True,
                         region_name='us-east-1', access_key_id='local-contract-only',
                         secret_access_key='local-contract-only-not-a-real-secret',
                         connect_timeout=2, read_timeout=3, max_attempts=1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=['write', 'read', 'unavailable'])
    parser.add_argument('--evidence-dir', required=True)
    args = parser.parse_args()
    evidence = Path(args.evidence_dir)
    evidence.mkdir(parents=True, exist_ok=True)
    manifest = evidence / 's3-sdk-manifest.json'
    if args.phase == 'write':
        if manifest.exists():
            raise RuntimeError('refusing to replace existing SDK evidence')
        raw = b'<!doctype html><html><body>S3 SDK contract, not research evidence.</body></html>'
        digest = hashlib.sha256(raw).hexdigest()
        suffix = 'sdk-contract-' + uuid.uuid4().hex
        backend = store('raw/public/' + suffix)
        backend.put_if_absent(digest, raw)
        backend.put_if_absent(digest, raw)
        assert backend.get_verified(digest, len(raw)) == raw
        try:
            backend.client.put_object(Bucket=backend.bucket, Key=backend._key(digest),
                                      Body=b'conflicting bytes', IfNoneMatch='*')
        except Exception as exc:
            assert backend._is_conflict(exc, 412)
        else:
            raise AssertionError('conditional overwrite was not rejected')
        assert backend.get_verified(digest) == raw
        private = store('raw/private/' + suffix)
        try:
            private.get_verified(digest)
        except RawStorageError as exc:
            assert exc.code == 'object_tampered'
        else:
            raise AssertionError('private prefix unexpectedly contains public object')
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            docs = DocumentService(temp / 'docs', object_store=backend)
            version = docs.register_bytes(raw, company_id='TEST', source_key='sdk-contract',
                                          title='Contract only', expected_sha256=digest)
            assert docs.read_raw_version(company_id='TEST', document_version_id=version['document_version_id']) == raw
            assert not (temp / 'docs/objects').exists()
            path = temp / 'source.html'
            path.write_bytes(raw)
            archive = SourceArchive(temp / 'archive', object_store=backend)
            config = dict(source_id='sdk-contract', title='Contract only', company_id='TEST',
                          url='https://example.com/contract', allowed_urls=['https://example.com/contract'],
                          published_at='2026-10-02', rights_status='local_only',
                          media_types=['text/html'], max_bytes=1000, timeout_seconds=2)
            archive.import_local(config, path, expected_sha256=digest, expected_size=len(raw), media_type='text/html')
            assert archive.read('sdk-contract', source_version=1) == raw
            assert not (temp / 'archive/objects').exists()
        result = dict(prefix=backend.prefix, digest=digest, size=len(raw),
                      sdk_put_get=True, duplicate_put=True, conditional_conflict=True,
                      prefix_separation=True, document_archive_roundtrip=True,
                      production_iam_tested=False)
        manifest.write_text(json.dumps(result, indent=2) + '\n')
    else:
        result = json.loads(manifest.read_text())
        backend = store(result['prefix'])
        if args.phase == 'read':
            raw = backend.get_verified(result['digest'], result['size'])
            assert hashlib.sha256(raw).hexdigest() == result['digest']
            result['restart_read_verified'] = True
        else:
            try:
                backend.get_verified(result['digest'], result['size'])
            except RawStorageError as exc:
                assert exc.code == 'object_store_unavailable'
            else:
                raise AssertionError('expected stopped emulator to be unavailable')
            result['unavailable_no_local_fallback'] = True
    (evidence / f's3-sdk-{args.phase}.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
