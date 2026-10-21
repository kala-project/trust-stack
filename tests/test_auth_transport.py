"""Transport security and bounded HTTP worker tests."""
from email.message import Message
import socket
import ssl
import sys
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from workbench.build import build_database
from workbench.server import Handler, EvidenceHTTPServer, _server_tls_context, _valid_https_origin, main


class MainTransportTests(unittest.TestCase):
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
        self.assertFalse((self.root / "auth").exists())

    def test_authenticated_wildcard_and_unpaired_tls_rejected_before_auth(self):
        self.assert_rejected_before_auth("--host", "0.0.0.0")
        self.assert_rejected_before_auth("--host", "0.0.0.0", "--tls-cert", str(self.root / "cert.pem"))

    def test_no_auth_is_loopback_only(self):
        self.assert_rejected_before_auth("--no-auth", "--host", "0.0.0.0")

    def test_proxy_origin_is_strict_paired_and_loopback_only(self):
        invalid = [
            "http://research.example", "https://user@research.example",
            "https://research.example/", "https://research.example/path",
            "https://research.example?query", "https://research.example#fragment",
        ]
        for origin in invalid:
            with self.subTest(origin=origin):
                self.assert_rejected_before_auth("--public-origin", origin, "--secure-cookie")
        self.assert_rejected_before_auth("--public-origin", "https://research.example")
        self.assert_rejected_before_auth("--secure-cookie")
        self.assert_rejected_before_auth(
            "--host", "0.0.0.0", "--public-origin", "https://research.example", "--secure-cookie")
        self.assertTrue(_valid_https_origin("https://research.example:8443"))

    def test_loopback_plaintext_entry_remains_available(self):
        owner = MagicMock()
        server = MagicMock()
        server.server_port = 8765
        with patch("workbench.server.OwnerAuth", return_value=owner) as owner_type, \
                patch("workbench.server.EvidenceHTTPServer", return_value=server):
            self.run_main()
        owner_type.assert_called_once()
        self.assertFalse(owner_type.call_args.kwargs["secure_cookie"])
        server.serve_forever.assert_called_once()
        server.server_close.assert_called_once()

    def test_native_tls_automatically_uses_secure_cookie(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        owner = MagicMock()
        server = MagicMock(server_port=443)
        arguments = ("--host", "0.0.0.0", "--tls-cert", str(self.root / "cert.pem"),
                     "--tls-key", str(self.root / "key.pem"))
        with patch("workbench.server._server_tls_context", return_value=context), \
                patch("workbench.server.OwnerAuth", return_value=owner) as owner_type, \
                patch("workbench.server.EvidenceHTTPServer", return_value=server) as server_type:
            self.run_main(*arguments)
        self.assertTrue(owner_type.call_args.kwargs["secure_cookie"])
        self.assertIs(server_type.call_args.kwargs["ssl_context"], context)

    def test_init_owner_does_not_apply_listen_transport_rules(self):
        auth = MagicMock()
        with patch("workbench.server.OwnerAuth", return_value=auth), \
                patch("workbench.server.getpass.getpass", side_effect=["long enough password", "long enough password"]):
            self.run_main("--init-owner", "--host", "0.0.0.0")
        auth.initialize_local.assert_called_once_with("long enough password")


class HTTPServerTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "evidence.sqlite3"
        build_database(db_path=self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_authenticated_wildcard_constructor_requires_tls_but_auth_none_is_compatible(self):
        auth = MagicMock(db_path=Path(self.tmp.name) / "owner.sqlite3")
        with self.assertRaises(RuntimeError):
            EvidenceHTTPServer(0, self.db, host="0.0.0.0", auth=auth)
        server = EvidenceHTTPServer(0, self.db, host="0.0.0.0", auth=None)
        try:
            self.assertEqual(server.server_address[0], "0.0.0.0")
        finally:
            server.server_close()

    def test_tls_context_is_server_only_and_tls12_minimum(self):
        context_type = MagicMock()
        context = MagicMock()
        context_type.return_value = context
        with patch("workbench.server.ssl.SSLContext", context_type):
            result = _server_tls_context("temporary-cert.pem", "temporary-key.pem")
        context_type.assert_called_once_with(ssl.PROTOCOL_TLS_SERVER)
        self.assertEqual(context.minimum_version, ssl.TLSVersion.TLSv1_2)
        context.load_cert_chain.assert_called_once_with(
            certfile="temporary-cert.pem", keyfile="temporary-key.pem")
        self.assertIs(result, context)

    def test_native_tls_origin_is_https_and_ignores_forwarded_proto(self):
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(public_origin=None, transport_scheme="https")
        handler.headers = Message()
        handler.headers["Host"] = "research.example"
        handler.headers["Origin"] = "https://research.example"
        handler.headers["X-Forwarded-Proto"] = "http"
        self.assertTrue(handler._same_origin())
        handler.headers.replace_header("Origin", "http://research.example")
        handler.headers.replace_header("X-Forwarded-Proto", "https")
        self.assertFalse(handler._same_origin())

    def test_worker_limit_closes_excess_without_starting_thread(self):
        server = EvidenceHTTPServer(0, self.db, max_workers=1)
        request, peer = socket.socketpair()
        self.addCleanup(peer.close)
        try:
            self.assertTrue(server._worker_slots.acquire(blocking=False))
            with patch.object(server, "shutdown_request", wraps=server.shutdown_request) as shutdown, \
                    patch("http.server.ThreadingHTTPServer.process_request") as delegate:
                server.process_request(request, ("127.0.0.1", 1))
            shutdown.assert_called_once_with(request)
            delegate.assert_not_called()
            server._worker_slots.release()
        finally:
            server.server_close()

    def test_worker_slot_released_when_handler_raises(self):
        server = EvidenceHTTPServer(0, self.db, max_workers=1)
        request, peer = socket.socketpair()
        self.addCleanup(peer.close)
        try:
            with patch.object(server, "finish_request", side_effect=RuntimeError("handler failed")), \
                    patch.object(server, "handle_error"):
                server.process_request(request, ("127.0.0.1", 1))
                deadline = time.monotonic() + 2
                acquired = False
                while time.monotonic() < deadline:
                    acquired = server._worker_slots.acquire(blocking=False)
                    if acquired:
                        break
                    time.sleep(0.01)
                self.assertTrue(acquired, "worker slot leaked after handler exception")
                server._worker_slots.release()
        finally:
            server.server_close()


if __name__ == "__main__":
    unittest.main()
