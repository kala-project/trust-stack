#!/usr/bin/env python3
"""Real HTTP/S3/process integration with an explicitly synthetic provider, NOT live-model acceptance."""
import argparse
import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.build import build_database
from workbench.documents import DocumentService
from workbench.server import EvidenceHTTPServer
from tests.test_chat import FakeProvider


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if Path(args.output).exists():
        raise RuntimeError('refusing to replace existing acceptance evidence')
    with tempfile.TemporaryDirectory(prefix='s3-wiring-') as temporary:
        root = Path(temporary)
        evidence, chat, documents = root/'evidence.sqlite3', root/'chat.sqlite3', root/'documents'
        build_database(db_path=evidence)
        DocumentService(documents)  # Empty local catalog, no originals to migrate.
        payload = json.loads((ROOT/'config/s3-dev.example.json').read_text())
        payload['deployment'] = 'wiring-contract-' + uuid.uuid4().hex
        config = root/'storage.json'
        config.write_text(json.dumps(payload))
        server = EvidenceHTTPServer(0, evidence, chat_db=chat, documents_root=documents,
                                    provider=FakeProvider(), execution_mode='durable',
                                    storage_config=config, enable_public_acquisition=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def request(path, data=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            try:
                conn.request('POST' if data is not None else 'GET', path,
                             body=json.dumps(data) if data is not None else None,
                             headers={'Origin': f'http://127.0.0.1:{server.server_port}', 'Content-Type': 'application/json'})
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()
        try:
            raw = b'<html><body>Public SDK wiring contract only</body></html>'
            version = server.documents.register_bytes(raw, company_id='MSFT', source_key='contract-only',
                         title='Synthetic contract only', expected_sha256=hashlib.sha256(raw).hexdigest())
            assert server.chat.source_acquisition.documents.object_store is server.documents.object_store
            assert server.chat.source_acquisition.archive.object_store is server.documents.object_store
            status, conversation = request('/api/chat/conversations', {'company_id': 'MSFT'})
            assert status == 201
            cid = conversation['conversation_id']
            status, uploaded = request('/api/research/materials', dict(conversation_id=cid,
                        filename='contract.txt', content_base64=base64.b64encode(b'Private SDK contract only').decode(),
                        allow_model_use=True))
            assert status == 201, (status, uploaded)
            status, second = request('/api/chat/conversations', {'company_id': 'MSFT'})
            assert status == 201
            status, materials = request('/api/research/materials?conversation_id='+second['conversation_id'])
            assert status == 200 and materials['materials'] == []
            status, submitted = request('/api/chat/messages', dict(conversation_id=cid,
                                         message='Synthetic storage contract only', request_id='s3-wiring-contract'))
            assert status == 202 and submitted['status'] == 'queued'
            script = '''import json,os,sys
from workbench.worker import create_worker_service,DurableWorker
from tests.test_chat import FakeProvider
s=create_worker_service(sys.argv[1],sys.argv[2],documents_root=sys.argv[3],storage_config=sys.argv[4],provider=FakeProvider('Synthetic queue contract only'))
try:
 assert s.materials.raw(sys.argv[5],sys.argv[6])[0] == b'Private SDK contract only'
 assert s.document_service.read_raw_version(company_id='MSFT',document_version_id=sys.argv[7]) == b'<html><body>Public SDK wiring contract only</body></html>'
 assert DurableWorker(s,heartbeat_seconds=.1).run_once()
 print(json.dumps({'pid':os.getpid(),'public_and_private_sdk_read':True}))
finally:
 s.close()
'''
            result = subprocess.run([sys.executable, '-c', script, str(evidence), str(chat), str(documents),
                                     str(config), cid, uploaded['document_version_id'], version['document_version_id']],
                                    cwd=ROOT, capture_output=True, text=True, timeout=35)
            assert result.returncode == 0, result.stderr
            child = json.loads(result.stdout)
            assert child['pid'] != os.getpid()
            status, run = request('/api/chat/run?id='+submitted['run_id'])
            assert status == 200 and run['status'] == 'completed', run
            assert not any((documents/'objects').iterdir())
            private_root = chat.with_name(chat.name+'.materials')/hashlib.sha256(cid.encode()).hexdigest()
            assert not (private_root/'objects').exists()
            final = dict(real_s3_sdk=True, real_http_upload=True, separate_process=child,
                         public_private_isolation=True, no_local_raw_copy=True,
                         durable_job_completed=True, live_model=False, production_iam=False,
                         namespace=payload['deployment'])
            Path(args.output).write_text(json.dumps(final,indent=2)+'\n')
            print(json.dumps(final,indent=2))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)


if __name__ == '__main__':
    main()
