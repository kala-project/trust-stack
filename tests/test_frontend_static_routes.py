"""Exact static allowlist regression; temporary files and unavailable model only."""
from pathlib import Path
import http.client
import tempfile
import threading
import unittest
from unittest.mock import patch
from workbench.init_public import initialize_public_database
from workbench.chat import _UnavailableProvider
from workbench.server import EvidenceHTTPServer, STATIC

class FrontendStaticRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory();cls.root=Path(cls.tmp.name)
        (cls.root/'web').mkdir()
        for filename,_ in STATIC.values():
            p=cls.root/'web'/filename;p.parent.mkdir(exist_ok=True,parents=True);p.write_text('test static '+filename)
        initialize_public_database(cls.root/'evidence.sqlite3')
        cls.server=EvidenceHTTPServer(0,cls.root/'evidence.sqlite3',provider=_UnavailableProvider())
        cls.patch=patch('workbench.server.ROOT',cls.root);cls.patch.start()
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join(5);cls.patch.stop();cls.tmp.cleanup()
    def get(self,path):
        c=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            c.request('GET',path);r=c.getresponse();return r.status,dict(r.getheaders()),r.read()
        finally:c.close()
    def test_exact_frontend_resources(self):
        for path in ['/workbench-app.js','/workbench-reader.js','/workbench-shared.js','/workbench-base.css','/workbench-reader.css','/workbench-app.css','/theme-init.js','/report-reader.html?report_id=report_test&reading=two&page=2']:
            with self.subTest(path=path):
                status,headers,raw=self.get(path);self.assertEqual(status,200);self.assertTrue(raw)
                self.assertEqual(headers['Cache-Control'],'no-store')
                self.assertIn("script-src 'self'",headers['Content-Security-Policy'])
    def test_no_arbitrary_assets_or_query_bypass(self):
        for path in ['/workbench-unknown.js','/assets/../../package.json','/node_modules/react/package.json','/.vite/manifest.json','/legacy.html','/report-reader-legacy.html']:
            with self.subTest(path=path):self.assertEqual(self.get(path)[0],404)
        for path in ['/workbench-app.js?anything=1','/report-reader.html?report_id=x&reading=bad','/report-reader.html?report_id=x&page=0']:
            with self.subTest(path=path):self.assertEqual(self.get(path)[0],400)

if __name__=='__main__':unittest.main()
