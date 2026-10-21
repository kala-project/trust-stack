"""Explicit trusted-LAN HTTP CLI policy and authenticated request flow."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

from workbench.build import build_database
from workbench.owner_auth import OwnerAuth
from workbench.server import EvidenceHTTPServer, main


PASSWORD = "temporary LAN password"


class LANHTTPCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.common = [
            "workbench.server", "--db", str(self.root / "evidence.sqlite3"),
            "--auth-dir", str(self.root / "auth"),
        ]

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *arguments):
        with patch.object(sys, "argv", self.common + list(arguments)):
            return main()

    def assert_rejected_before_auth(self, *arguments):
        with patch("workbench.server.OwnerAuth") as owner:
            with self.assertRaises(SystemExit):
                self.run_main(*arguments)
            owner.assert_not_called()

    def test_authenticated_wildcard_http_is_rejected_without_explicit_flag(self):
        self.assert_rejected_before_auth("--host", "0.0.0.0")

    def test_explicit_authenticated_lan_http_is_allowed(self):
        owner = MagicMock()
        server = MagicMock(server_port=8765)
        with patch("workbench.server.OwnerAuth", return_value=owner) as owner_type, \
                patch("workbench.server.EvidenceHTTPServer", return_value=server) as server_type:
            self.run_main("--host", "0.0.0.0", "--allow-lan-http")
        self.assertFalse(owner_type.call_args.kwargs["secure_cookie"])
        self.assertTrue(owner_type.call_args.kwargs["lan_http_cookie"])
        self.assertTrue(server_type.call_args.kwargs["allow_lan_http"])
        self.assertEqual(server_type.call_args.kwargs["host"], "0.0.0.0")
        self.assertIsNone(server_type.call_args.kwargs["ssl_context"])
        server.serve_forever.assert_called_once()

    def test_explicit_test_no_auth_does_not_open_password_store(self):
        server = MagicMock(server_port=8765)
        with patch("workbench.server.OwnerAuth") as owner_type, \
                patch("workbench.server.EvidenceHTTPServer", return_value=server) as server_type:
            self.run_main("--host", "0.0.0.0", "--no-auth", "--allow-insecure-no-auth")
        owner_type.assert_not_called()
        self.assertIsNone(server_type.call_args.kwargs["auth"])
        self.assertEqual(server_type.call_args.kwargs["host"], "0.0.0.0")
        server.serve_forever.assert_called_once()
        self.assertFalse((self.root / "auth").exists())

    def test_test_no_auth_requires_explicit_consistent_options(self):
        for arguments in [
            ("--allow-insecure-no-auth",),
            ("--allow-insecure-no-auth", "--host", "0.0.0.0"),
            ("--allow-insecure-no-auth", "--no-auth"),
            ("--allow-insecure-no-auth", "--init-owner"),
            ("--allow-insecure-no-auth", "--no-auth", "--host", "0.0.0.0", "--allow-lan-http"),
        ]:
            with self.subTest(arguments=arguments):
                self.assert_rejected_before_auth(*arguments)

    def test_lan_http_flag_rejects_incompatible_modes(self):
        invalid = [
            ("--allow-lan-http",),
            ("--allow-lan-http", "--host", "0.0.0.0", "--no-auth"),
            ("--allow-lan-http", "--host", "0.0.0.0", "--secure-cookie"),
            ("--allow-lan-http", "--host", "0.0.0.0", "--public-origin", "https://research.example"),
            ("--allow-lan-http", "--host", "0.0.0.0", "--tls-cert", "cert.pem", "--tls-key", "key.pem"),
            ("--allow-lan-http", "--init-owner"),
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                self.assert_rejected_before_auth(*arguments)


class LANHTTPAuthenticationFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "evidence.sqlite3"
        build_database(db_path=self.db)
        self.kdf = patch("workbench.owner_auth.PBKDF2_ITERATIONS", 1_000)
        self.kdf.start()
        self.auth = OwnerAuth(self.root / "auth" / "owner.sqlite3", create_bootstrap=False, lan_http_cookie=True)
        self.auth.initialize_local(PASSWORD)
        # This checks HTTP owner authentication, not external Copilot login.
        # Do not let /api/overview start a real SDK authorization probe.
        self.provider = MagicMock()
        self.provider.status.return_value = {
            "available": False, "provider": "test", "reason": "no model in authentication test"}
        self.server = EvidenceHTTPServer(
            0, self.db, host="0.0.0.0", auth=self.auth, allow_lan_http=True,
            provider=self.provider)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.kdf.stop()
        self.tmp.cleanup()

    def request(self, path, *, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        request_headers = dict(headers or {})
        encoded = None if body is None else json.dumps(body).encode()
        if encoded is not None:
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(encoded))
        try:
            connection.request(method, path, body=encoded, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_plain_http_login_api_logout_keeps_cookie_and_origin_guards(self):
        status, headers, _ = self.request(
            "/api/auth/login", method="POST", body={"password": PASSWORD},
            headers={"Origin": self.origin})
        self.assertEqual(status, 200)
        set_cookie = headers["Set-Cookie"]
        self.assertTrue(set_cookie.startswith("workbench_lan_session="))
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertNotIn("; Secure", set_cookie)
        cookie = set_cookie.split(";", 1)[0]

        self.assertEqual(self.request("/api/overview", headers={"Cookie": cookie})[0], 200)
        self.assertEqual(self.request("/api/overview", headers={"Cookie": "workbench_session=old-secure-cookie; " + cookie})[0], 200)
        self.assertEqual(self.request("/api/overview", headers={"Cookie": cookie.replace("workbench_lan_session=", "workbench_session=")})[0], 401)
        self.assertEqual(self.request(
            "/api/auth/logout", method="POST", body={},
            headers={"Origin": "http://attacker.example", "Cookie": cookie})[0], 403)
        self.assertEqual(self.request(
            "/api/auth/logout", method="POST", body={},
            headers={"Origin": self.origin, "Cookie": cookie})[0], 200)
        self.assertEqual(self.request("/api/overview", headers={"Cookie": cookie})[0], 401)
        self.provider.status.assert_called()
        self.provider.stream.assert_not_called()


if __name__ == "__main__":
    unittest.main()
