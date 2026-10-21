"""Explicit network binding keeps Host and Origin protections."""
import http.client
from pathlib import Path
import tempfile
import threading
import unittest

from workbench.build import build_database
from workbench.server import EvidenceHTTPServer


class NetworkBindingTests(unittest.TestCase):
    def test_explicit_wildcard_binding_preserves_request_guards(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "evidence.sqlite3"
            build_database(db_path=db)
            server = EvidenceHTTPServer(0, db, host="0.0.0.0")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertEqual(server.server_address[0], "0.0.0.0")
                port = server.server_port
                for headers, expected in [
                    ({}, 200),
                    ({"Host": f"127.0.0.1:{port}", "Origin": f"http://127.0.0.1:{port}"}, 200),
                    ({"Host": f"attacker.example:{port}"}, 403),
                    ({"Origin": "http://attacker.example"}, 403),
                    ({"Sec-Fetch-Site": "cross-site"}, 403),
                ]:
                    with self.subTest(headers=headers):
                        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                        try:
                            connection.request("GET", "/api/health", headers=headers)
                            response = connection.getresponse()
                            self.assertEqual(response.status, expected)
                            response.read()
                        finally:
                            connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)
