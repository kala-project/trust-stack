"""Security tests for the single-owner private workspace boundary."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import http.client
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from workbench.build import build_database
from workbench.owner_auth import AuthError, OwnerAuth
from workbench.server import EvidenceHTTPServer

PASSWORD = "correct horse 电池 staple"


class OwnerAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "private"
        patcher = patch("workbench.owner_auth.PBKDF2_ITERATIONS", 1_000)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def auth(self, **options):
        return OwnerAuth(self.root / "owner.sqlite3", **options)

    def bootstrap(self, auth):
        return auth.bootstrap_path.read_text(encoding="ascii").strip()

    def test_private_files_bootstrap_once_and_digest_only_sessions(self):
        auth = self.auth()
        self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(auth.db_path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(auth.bootstrap_path).st_mode & 0o777, 0o600)
        nonce = self.bootstrap(auth)
        auth.initialize(nonce, PASSWORD)
        self.assertFalse(auth.bootstrap_path.exists())
        with self.assertRaises(AuthError):
            auth.initialize(nonce, PASSWORD)
        token, _ = auth.login(PASSWORD, "127.0.0.1")
        with closing(sqlite3.connect(auth.db_path)) as db:
            stored = db.execute("SELECT token_digest FROM sessions").fetchone()[0]
        self.assertNotEqual(stored, token.encode())
        self.assertNotIn(token.encode(), auth.db_path.read_bytes())

    def test_wrong_nonce_does_not_delete_or_leak_bootstrap(self):
        auth = self.auth()
        nonce = self.bootstrap(auth)
        with patch("workbench.owner_auth.hashlib.pbkdf2_hmac") as kdf:
            with self.assertRaises(AuthError) as error:
                auth.initialize("wrong-bootstrap", PASSWORD)
            kdf.assert_not_called()
        self.assertEqual(error.exception.code, "initialization_unavailable")
        self.assertEqual(self.bootstrap(auth), nonce)

        auth.initialize(nonce, PASSWORD)
        with patch("workbench.owner_auth.hashlib.pbkdf2_hmac") as kdf:
            with self.assertRaises(AuthError) as initialized:
                auth.initialize(nonce, PASSWORD)
            kdf.assert_not_called()
        self.assertEqual(initialized.exception.status, 409)

    def test_unknown_existing_file_and_symlink_are_never_overwritten(self):
        self.root.mkdir(mode=0o700)
        bootstrap = self.root / "bootstrap.secret"
        bootstrap.write_text("user-owned")
        with self.assertRaises(RuntimeError):
            self.auth()
        self.assertEqual(bootstrap.read_text(), "user-owned")
        bootstrap.unlink()
        target = self.root / "target"
        target.write_text("keep")
        bootstrap.symlink_to(target)
        with self.assertRaises(RuntimeError):
            self.auth()
        self.assertEqual(target.read_text(), "keep")

    def test_database_path_collision_and_symlink_rejected(self):
        self.root.mkdir(mode=0o700)
        target = self.root / "target.sqlite3"
        target.write_text("keep")
        (self.root / "owner.sqlite3").symlink_to(target)
        with self.assertRaises(RuntimeError):
            self.auth()
        self.assertEqual(target.read_text(), "keep")

    def test_concurrent_first_store_creation_is_atomic_and_private(self):
        path = self.root / "owner.sqlite3"
        with ThreadPoolExecutor(max_workers=8) as pool:
            auths = list(pool.map(lambda _: OwnerAuth(path), range(8)))
        bootstrap_values = {auth.bootstrap_path.read_text(encoding="ascii") for auth in auths}
        self.assertEqual(len(bootstrap_values), 1)
        self.assertEqual(path.stat().st_nlink, 1)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)

    def test_concurrent_initialization_has_one_winner(self):
        auth = self.auth()
        nonce = self.bootstrap(auth)
        original = hashlib.pbkdf2_hmac
        calls = 0
        calls_lock = threading.Lock()

        def counted_kdf(*args, **kwargs):
            nonlocal calls
            with calls_lock:
                calls += 1
            return original(*args, **kwargs)

        def initialize():
            try:
                auth.initialize(nonce, PASSWORD)
                return True
            except AuthError:
                return False
        with patch("workbench.owner_auth.hashlib.pbkdf2_hmac", side_effect=counted_kdf):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(lambda _: initialize(), range(4)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(calls, 1)
        self.assertTrue(auth.has_owner())

    def test_concurrent_logins_have_bounded_kdf_and_peer_only_budgets(self):
        auth = self.auth()
        auth.initialize(self.bootstrap(auth), PASSWORD)
        original = hashlib.pbkdf2_hmac
        entered = threading.Event()
        release = threading.Event()
        state_lock = threading.Lock()
        active = maximum = 0

        def blocked_kdf(*args, **kwargs):
            nonlocal active, maximum
            with state_lock:
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    entered.set()
            release.wait(5)
            try:
                return original(*args, **kwargs)
            finally:
                with state_lock:
                    active -= 1

        def login(index):
            password = PASSWORD if index % 2 == 0 else "incorrect password"
            try:
                auth.login(password, f"peer-{index}")
                return 200
            except AuthError as error:
                return error.status

        with patch("workbench.owner_auth.hashlib.pbkdf2_hmac", side_effect=blocked_kdf):
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(login, index) for index in range(8)]
                self.assertTrue(entered.wait(5))
                release.set()
                statuses = [future.result(timeout=5) for future in futures]
        self.assertEqual(maximum, 2)
        self.assertEqual(statuses.count(429), 6)
        self.assertIn(200, statuses)
        self.assertIn(401, statuses)

        for index in range(5):
            with self.assertRaises(AuthError):
                auth.login("incorrect password", f"attacker-{index}")
        token, _ = auth.login(PASSWORD, "owner-peer")
        self.assertTrue(auth.authenticated(token))

    def test_hard_link_and_unknown_database_are_rejected_without_changes(self):
        self.root.mkdir(mode=0o700)
        evidence = self.root / "evidence.sqlite3"
        evidence.write_bytes(b"immutable evidence")
        os.chmod(evidence, 0o640)
        os.link(evidence, self.root / "owner.sqlite3")
        before = (evidence.read_bytes(), evidence.stat().st_mtime_ns,
                  evidence.stat().st_mode & 0o777)
        with self.assertRaises(RuntimeError):
            self.auth()
        after = (evidence.read_bytes(), evidence.stat().st_mtime_ns,
                 evidence.stat().st_mode & 0o777)
        self.assertEqual(after, before)

        (self.root / "owner.sqlite3").unlink()
        unknown = self.root / "owner.sqlite3"
        with closing(sqlite3.connect(unknown)) as db:
            db.execute("CREATE TABLE evidence(value TEXT)")
            db.execute("INSERT INTO evidence VALUES ('keep')")
            db.execute("PRAGMA journal_mode=DELETE")
            db.commit()
        os.chmod(unknown, 0o640)
        before = (unknown.read_bytes(), unknown.stat().st_mtime_ns,
                  unknown.stat().st_mode & 0o777)
        with self.assertRaises(RuntimeError):
            self.auth()
        after = (unknown.read_bytes(), unknown.stat().st_mtime_ns,
                 unknown.stat().st_mode & 0o777)
        self.assertEqual(after, before)
        with closing(sqlite3.connect(unknown)) as db:
            self.assertEqual(db.execute("SELECT value FROM evidence").fetchone()[0], "keep")
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_empty_database_and_corrupt_kdf_parameters_are_rejected_cheaply(self):
        self.root.mkdir(mode=0o700)
        database = self.root / "owner.sqlite3"
        database.touch(mode=0o600)
        with self.assertRaises(RuntimeError):
            self.auth()
        self.assertEqual(database.stat().st_size, 0)

        database.unlink()
        auth = self.auth()
        auth.initialize(self.bootstrap(auth), PASSWORD)
        with closing(sqlite3.connect(auth.db_path)) as db:
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute("UPDATE owner SET iterations=2147483647")
            db.commit()
        with patch("workbench.owner_auth.hashlib.pbkdf2_hmac") as kdf:
            with self.assertRaises(RuntimeError):
                auth.login(PASSWORD, "peer")
            kdf.assert_not_called()

    def test_logout_expiry_unicode_and_rate_limit(self):
        clock = [1_000]
        auth = self.auth(session_seconds=10, now=lambda: clock[0])
        auth.initialize(self.bootstrap(auth), PASSWORD)
        token, _ = auth.login(PASSWORD, "peer")
        self.assertTrue(auth.authenticated(token))
        auth.logout(token)
        self.assertFalse(auth.authenticated(token))
        token, _ = auth.login(PASSWORD, "peer")
        clock[0] += 11
        self.assertFalse(auth.authenticated(token))
        for _ in range(5):
            with self.assertRaises(AuthError):
                auth.login("incorrect password", "peer")
        with self.assertRaises(AuthError) as limited:
            auth.login(PASSWORD, "peer")
        self.assertEqual(limited.exception.status, 429)


class AuthenticatedServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.db = cls.root / "evidence.sqlite3"
        build_database(db_path=cls.db)
        cls.evidence_digest = hashlib.sha256(cls.db.read_bytes()).digest()
        cls.patcher = patch("workbench.owner_auth.PBKDF2_ITERATIONS", 1_000)
        cls.patcher.start()
        cls.auth = OwnerAuth(cls.root / "auth" / "owner.sqlite3")
        cls.nonce = cls.auth.bootstrap_path.read_text().strip()
        cls.auth.initialize(cls.nonce, PASSWORD)
        cls.server = EvidenceHTTPServer(0, cls.db, auth=cls.auth)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port
        cls.origin = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.patcher.stop()
        cls.tmp.cleanup()

    def request(self, path, *, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        request_headers = dict(headers or {})
        if body is not None:
            encoded = json.dumps(body).encode()
            request_headers.setdefault("Content-Type", "application/json")
            request_headers.setdefault("Content-Length", str(len(encoded)))
        else:
            encoded = None
        try:
            connection.request(method, path, body=encoded, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def login(self):
        status, headers, _ = self.request("/api/auth/login", method="POST", body={"password": PASSWORD},
                                          headers={"Origin": self.origin})
        self.assertEqual(status, 200)
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_only_health_status_and_login_assets_are_public(self):
        self.assertEqual(json.loads(self.request("/api/health")[2]), {"status": "ok"})
        status = json.loads(self.request("/api/auth/status")[2])
        self.assertFalse(status["authenticated"])
        for path in ["/api/overview", "/api/company?id=MSFT", "/api/evidence?id=x",
                     "/api/source?id=x", "/api/report?company=MSFT&format=json",
                     "/api/chat/status", "/api/research/tasks", "/api/wiki/page?company=MSFT",
                     "/api/documents/coverage?company=MSFT", "/api/sample-research/options",
                     "/api/reports?conversation_id=x", "/api/reports/reader?report_id=x",
                     "/api/reports/evidence?report_id=x&anchor_id=x",
                     "/api/reports/source?report_id=x&source_id=x",
                     "/api/reports/raw-source?report_id=x&source_id=x",
                     "/report-reader.js", "/report-reader-bindings.js"]:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 401)
        reader_status, reader_headers, reader_body = self.request(
            "/report-reader.html?report_id=report%20x&page=7&reading=two")
        self.assertEqual(reader_status, 303)
        self.assertEqual(reader_body, b"")
        self.assertEqual(reader_headers["Location"],
                         "/#/report-reader?report_id=report+x&page=7&reading=two")
        self.assertIn(b"login-form", self.request("/")[2])
        self.assertEqual(self.request("/app.js")[0], 401)
        self.assertEqual(self.request("/research-navigation.js")[0], 401)
        self.assertEqual(self.request("/auth.js")[0], 200)

    def test_login_cookie_authenticated_data_logout_and_no_leak(self):
        cookie = self.login()
        raw_cookie = self.request("/api/auth/login", method="POST", body={"password": PASSWORD},
                                  headers={"Origin": self.origin})[1]["Set-Cookie"]
        for attribute in ["HttpOnly", "SameSite=Strict", "Path=/"]:
            self.assertIn(attribute, raw_cookie)
        self.assertNotIn(PASSWORD.encode(), self.request("/api/auth/login", method="POST",
                         body={"password": "wrong password"}, headers={"Origin": self.origin})[2])
        self.assertEqual(self.request("/api/overview", headers={"Cookie": cookie})[0], 200)
        status, headers, _ = self.request("/api/auth/logout", method="POST", body={},
                                          headers={"Origin": self.origin, "Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("/api/overview", headers={"Cookie": cookie})[0], 401)

    def test_cross_origin_strict_body_and_unsupported_methods(self):
        self.assertEqual(self.request("/api/auth/login", method="POST", body={"password": PASSWORD},
                                      headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("/api/auth/login", method="POST", body={"password": PASSWORD, "extra": 1},
                                      headers={"Origin": self.origin})[0], 400)
        for method in ["PUT", "PATCH", "DELETE", "OPTIONS"]:
            self.assertEqual(self.request("/api/auth/login", method=method)[0], 405)

    def test_secure_cookie_mode_paths_do_not_collide_and_evidence_is_unchanged(self):
        secure = OwnerAuth(self.root / "secure-auth" / "owner.sqlite3", secure_cookie=True)
        self.assertIn("; Secure", secure.cookie("token"))
        with self.assertRaises(RuntimeError):
            EvidenceHTTPServer(0, self.db, auth=SimpleNamespace(db_path=self.db))
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).digest(), self.evidence_digest)


if __name__ == "__main__":
    unittest.main()
