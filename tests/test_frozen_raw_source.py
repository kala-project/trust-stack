import hashlib
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

from tests.test_recorded_calculations import CalculationProvider, wait
from workbench.build import build_database
from workbench.documents import DocumentService
from workbench.server import EvidenceHTTPServer

ROOT=Path(__file__).resolve().parents[1]


class FrozenRawSourceTests(unittest.TestCase):
    def test_http_returns_exact_frozen_bytes_as_attachment_and_rejects_scope_and_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); build_database(db_path=root/'evidence.sqlite3')
            documents=DocumentService(root/'documents')
            source=ROOT/'data/raw/microsoft-fy2025-q4-press-release.html'
            raw=source.read_bytes(); sha=hashlib.sha256(raw).hexdigest()
            documents.register_file(source,company_id='MSFT',source_key='release',title='Original source')
            server=EvidenceHTTPServer(0,root/'evidence.sqlite3',chat_db=root/'chat.sqlite3',provider=CalculationProvider())
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                chat=server.chat; cid=chat.create_conversation('MSFT')['conversation_id']
                run=chat.submit(cid,'微软收入增加多少？');self.assertEqual(wait(chat,run['run_id'])['status'],'completed')
                answer=chat.get_conversation(cid)['messages'][-1]
                def url(digest=sha,conversation=cid):
                    return f'http://127.0.0.1:{server.server_port}/api/research/raw-source?'+urlencode({'conversation_id':conversation,'message_id':answer['message_id'],'source_sha256':digest})
                with urlopen(url(),timeout=5) as response:
                    self.assertEqual(response.status,200)
                    self.assertTrue(response.headers['Content-Type'].startswith('text/html'))
                    self.assertEqual(response.headers['X-Content-Type-Options'],'nosniff')
                    self.assertEqual(response.headers['X-Verified-Source-SHA256'],sha)
                    self.assertIn(f'attachment; filename="original-{sha[:12]}.html"',response.headers['Content-Disposition'])
                    self.assertEqual(response.read(),raw)
                artifact=server.answer_artifacts.get(cid,answer['message_id'])
                self.assertTrue(any(doc['source_sha256']==sha for doc in artifact['audit']['frozen_raw_sources']))
                other=chat.create_conversation('AAPL')['conversation_id']
                with self.assertRaises(HTTPError) as caught:urlopen(url(conversation=other),timeout=5)
                self.assertEqual(caught.exception.code,404);caught.exception.close()
                newer=root/'newer.html';newer.write_text('<!doctype html><html><body>New edition</body></html>')
                documents.register_file(newer,company_id='MSFT',source_key='release',title='New source')
                with self.assertRaises(HTTPError) as caught:urlopen(url(hashlib.sha256(newer.read_bytes()).hexdigest()),timeout=5)
                self.assertEqual(caught.exception.code,404);caught.exception.close()
                with urlopen(url(),timeout=5) as response:self.assertEqual(response.read(),raw)
                stored=documents.objects_path/sha;stored.chmod(0o644);stored.write_bytes(b'tampered')
                with self.assertRaises(HTTPError) as caught:urlopen(url(),timeout=5)
                self.assertEqual(caught.exception.code,503);caught.exception.close()
            finally:
                server.shutdown();server.server_close();thread.join(3)
