import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from scripts.preview_frontend import make_server, validate_http_origin


class _Upstream(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        type(self).requests.append(self.path)
        raw = json.dumps({"path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_):
        pass


class PreviewFrontendTest(unittest.TestCase):
    def setUp(self):
        _Upstream.requests = []
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        origin = f"http://127.0.0.1:{self.upstream.server_port}"
        self.preview = make_server(api_origin=origin, reader_origin=origin, unified_reader=True)
        threading.Thread(target=self.preview.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.preview.server_port}"

    def tearDown(self):
        self.preview.shutdown()
        self.preview.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()

    def request(self, path, method="GET"):
        request = urllib.request.Request(self.base + path, method=method)
        return urllib.request.urlopen(request, timeout=3)

    def test_explicit_api_origin_and_new_read_routes(self):
        paths = [
            "/api/auth/status",
            "/api/chat/status",
            "/api/verifications?conversation_id=conv&limit=50",
            "/api/verification/candidate?conversation_id=conv&candidate_id=candidate",
        ]
        for path in paths:
            with self.request(path) as response:
                self.assertEqual(response.status, 200)
        self.assertEqual(_Upstream.requests, paths)

    def test_duplicate_unknown_and_malformed_queries_never_reach_upstream(self):
        for path in (
            "/api/chat/run?id=one&id=two",
            "/api/chat/run?unknown=x",
            "/api/chat/run?id=%ZZ",
            "/api/chat/run?id",
        ):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request(path)
            self.assertEqual(caught.exception.code, 403)
        self.assertEqual(_Upstream.requests, [])

    def test_all_writes_are_405_without_upstream_request(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request("/api/chat/run?id=x", method)
            self.assertEqual(caught.exception.code, 405)
        self.assertEqual(_Upstream.requests, [])

    def test_strict_origin_validation(self):
        self.assertEqual(validate_http_origin("http://127.0.0.1:15432/"), "http://127.0.0.1:15432")
        for value in (
            "http://user@127.0.0.1:15432",
            "http://127.0.0.1:15432/path",
            "http://127.0.0.1:15432?x=1",
            "http://127.0.0.1:15432/#fragment",
            "http://127.0.0.1:bad",
            " http://127.0.0.1:15432",
        ):
            with self.assertRaises(ValueError):
                validate_http_origin(value, "public URL")


if __name__ == "__main__":
    unittest.main()
