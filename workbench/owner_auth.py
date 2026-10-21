"""Single-owner authentication for one private workbench deployment."""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import closing
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import threading
import time
import unicodedata
from urllib.parse import quote

PBKDF2_ITERATIONS = 600_000
PASSWORD_MIN = 12
PASSWORD_MAX = 256
SESSION_SECONDS = 12 * 60 * 60
COOKIE_NAME = "workbench_session"
KDF_CONCURRENCY = 2
_DB_APPLICATION_ID = 0x57424155  # "WBAU"
_DB_SCHEMA_VERSION = 1
_KDF_SLOTS = threading.BoundedSemaphore(KDF_CONCURRENCY)


class AuthError(RuntimeError):
    """Safe authentication error with an HTTP status."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def normalize_password(password: object) -> str:
    """Validate a Unicode password and return its NFC representation."""
    if not isinstance(password, str):
        raise AuthError("invalid_credentials")
    normalized = unicodedata.normalize("NFC", password)
    if not PASSWORD_MIN <= len(normalized) <= PASSWORD_MAX:
        raise AuthError("invalid_credentials")
    if len(normalized.encode("utf-8")) > 1024:
        raise AuthError("invalid_credentials")
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in normalized):
        raise AuthError("invalid_credentials")
    return normalized


def _digest_token(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def _valid_digest(value: object) -> bool:
    return isinstance(value, bytes) and len(value) == hashlib.sha256().digest_size


class FailureLimiter:
    """Atomically reserves a bounded number of attempts for each direct peer."""

    def __init__(self, *, window: int = 60, peer_limit: int = 5,
                 global_limit: int | None = None):
        # global_limit remains accepted for callers from older releases. Failure
        # history is intentionally never global: one peer must not lock out owner.
        del global_limit
        self.window = window
        self.peer_limit = peer_limit
        self._peer: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, values: deque[float], now: float) -> None:
        while values and values[0] <= now - self.window:
            values.popleft()

    def reserve(self, peer: str) -> bool:
        """Check and consume one attempt in the same critical section."""
        now = time.monotonic()
        with self._lock:
            values = self._peer[peer]
            self._trim(values, now)
            if len(values) >= self.peer_limit:
                return False
            values.append(now)
            return True

    def success(self, peer: str) -> None:
        with self._lock:
            self._peer.pop(peer, None)


class OwnerAuth:
    """SQLite-backed credentials and revocable, digest-only sessions for one owner."""

    def __init__(self, db_path: Path, *, bootstrap_path: Path | None = None,
                 session_seconds: int = SESSION_SECONDS, secure_cookie: bool = False,
                 lan_http_cookie: bool = False, create_bootstrap: bool = True, now=time.time):
        self.db_path = Path(db_path)
        self.directory = self.db_path.parent
        self.bootstrap_path = Path(bootstrap_path or self.directory / "bootstrap.secret")
        self.session_seconds = session_seconds
        if secure_cookie and lan_http_cookie:
            raise ValueError("LAN HTTP cookie cannot be Secure")
        self.secure_cookie = secure_cookie
        # A previous Secure cookie cannot be overwritten from HTTP in a browser.
        self.cookie_name = "workbench_lan_session" if lan_http_cookie else COOKIE_NAME
        self.now = now
        self.limiter = FailureLimiter()
        self.bootstrap_limiter = FailureLimiter()
        self._prepare_directory()
        db_existed = self.db_path.exists() or self.db_path.is_symlink()
        self._wait_for_safe_database_target()
        self._ensure_safe_target(self.bootstrap_path)
        if not db_existed and self.bootstrap_path.exists():
            raise RuntimeError("refusing an existing bootstrap file without its store")
        self._open_or_create_store(db_existed)
        if create_bootstrap:
            self.ensure_bootstrap()

    @property
    def _schema_statements(self) -> tuple[str, ...]:
        return (
            f"""CREATE TABLE owner (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                salt BLOB NOT NULL CHECK(typeof(salt) = 'blob' AND length(salt) = 16),
                password_hash BLOB NOT NULL CHECK(typeof(password_hash) = 'blob' AND length(password_hash) = 32),
                iterations INTEGER NOT NULL CHECK(typeof(iterations) = 'integer' AND iterations = {PBKDF2_ITERATIONS}),
                created_at INTEGER NOT NULL CHECK(typeof(created_at) = 'integer')
            )""",
            """CREATE TABLE bootstrap (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                secret_digest BLOB NOT NULL CHECK(typeof(secret_digest) = 'blob' AND length(secret_digest) = 32),
                created_at INTEGER NOT NULL CHECK(typeof(created_at) = 'integer')
            )""",
            """CREATE TABLE sessions (
                token_digest BLOB PRIMARY KEY CHECK(typeof(token_digest) = 'blob' AND length(token_digest) = 32),
                created_at INTEGER NOT NULL CHECK(typeof(created_at) = 'integer'),
                expires_at INTEGER NOT NULL CHECK(typeof(expires_at) = 'integer'),
                revoked_at INTEGER CHECK(revoked_at IS NULL OR typeof(revoked_at) = 'integer')
            )""",
        )

    def _prepare_directory(self) -> None:
        if not self.directory.exists():
            try:
                self.directory.mkdir(parents=True, mode=0o700)
            except FileExistsError:
                pass
        info = self.directory.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("auth directory must be a real directory")
        if info.st_mode & 0o077:
            raise RuntimeError("auth directory permissions must be 0700")
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise RuntimeError("auth directory must be owned by this user")

    @staticmethod
    def _ensure_safe_target(path: Path) -> None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1):
            raise RuntimeError(f"unsafe authentication path: {path.name}")
        if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
            raise RuntimeError(f"unsafe authentication path owner: {path.name}")

    def _wait_for_safe_database_target(self) -> None:
        """Allow only the brief second link made by our own atomic publisher."""
        prefix = f".{self.db_path.name}."
        for _ in range(100):
            try:
                info = self.db_path.lstat()
            except FileNotFoundError:
                return
            if info.st_nlink == 1:
                self._ensure_safe_target(self.db_path)
                return
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                break
            publishing = False
            for candidate in self.directory.iterdir():
                if not (candidate.name.startswith(prefix) and candidate.name.endswith(".tmp")):
                    continue
                try:
                    temporary_info = candidate.lstat()
                except FileNotFoundError:
                    continue
                if (temporary_info.st_dev, temporary_info.st_ino) == (info.st_dev, info.st_ino):
                    publishing = True
                    break
            if not publishing:
                break
            time.sleep(0.001)
        self._ensure_safe_target(self.db_path)

    @staticmethod
    def _canonical_sql(value: str) -> str:
        return " ".join(value.rstrip(";").split())

    def _readonly_connection(self, path: Path) -> sqlite3.Connection:
        absolute = str(path.absolute())
        uri = f"file:{quote(absolute, safe='/')}?mode=ro"
        return sqlite3.connect(uri, uri=True, timeout=1, isolation_level=None)

    def _validate_existing_store(self, path: Path | None = None) -> None:
        target = path or self.db_path
        if target == self.db_path:
            self._wait_for_safe_database_target()
        else:
            self._ensure_safe_target(target)
        if target.stat().st_size == 0:
            raise RuntimeError("refusing an empty authentication database")
        try:
            with closing(self._readonly_connection(target)) as db:
                if db.execute("PRAGMA application_id").fetchone()[0] != _DB_APPLICATION_ID:
                    raise RuntimeError("database was not produced by OwnerAuth")
                if db.execute("PRAGMA user_version").fetchone()[0] != _DB_SCHEMA_VERSION:
                    raise RuntimeError("unsupported authentication database schema")
                rows = db.execute(
                    "SELECT type, name, tbl_name, sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' OR name LIKE 'sqlite_autoindex_%' "
                    "ORDER BY type, name"
                ).fetchall()
                tables = {name: sql for kind, name, _table, sql in rows if kind == "table"}
                indexes = [(name, table, sql) for kind, name, table, sql in rows if kind == "index"]
                expected = {
                    statement.split()[2]: self._canonical_sql(statement)
                    for statement in self._schema_statements
                }
                actual = {name: self._canonical_sql(sql) for name, sql in tables.items() if sql}
                if actual != expected or indexes != [("sqlite_autoindex_sessions_1", "sessions", None)]:
                    raise RuntimeError("authentication database schema mismatch")
                owner = db.execute(
                    "SELECT salt, password_hash, iterations, created_at FROM owner WHERE singleton=1"
                ).fetchone()
                if owner and not (len(owner) == 4 and isinstance(owner[0], bytes)
                                  and len(owner[0]) == 16 and _valid_digest(owner[1])
                                  and type(owner[2]) is int and owner[2] == PBKDF2_ITERATIONS
                                  and type(owner[3]) is int):
                    raise RuntimeError("corrupt owner credential record")
                bootstrap = db.execute(
                    "SELECT secret_digest, created_at FROM bootstrap WHERE singleton=1"
                ).fetchone()
                if bootstrap and not (_valid_digest(bootstrap[0]) and type(bootstrap[1]) is int):
                    raise RuntimeError("corrupt bootstrap record")
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("corrupt authentication database")
        except sqlite3.DatabaseError as error:
            raise RuntimeError("invalid authentication database") from error

    def _create_store_atomically(self) -> None:
        temporary = self.directory / f".{self.db_path.name}.{secrets.token_hex(16)}.tmp"
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        os.close(descriptor)
        try:
            with closing(sqlite3.connect(temporary, isolation_level=None)) as db:
                db.execute(f"PRAGMA application_id={_DB_APPLICATION_ID}")
                db.execute(f"PRAGMA user_version={_DB_SCHEMA_VERSION}")
                db.execute("BEGIN IMMEDIATE")
                for statement in self._schema_statements:
                    db.execute(statement)
                db.commit()
            with open(temporary, "rb") as stream:
                os.fsync(stream.fileno())
            try:
                os.link(temporary, self.db_path, follow_symlinks=False)
            except FileExistsError:
                self._validate_existing_store()
                return
            # End the two-link publication window before any slower directory sync.
            temporary.unlink()
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _open_or_create_store(self, existed: bool) -> None:
        if existed:
            self._validate_existing_store()
        else:
            self._create_store_atomically()
            self._validate_existing_store()
        os.chmod(self.db_path, 0o600)
        with closing(sqlite3.connect(self.db_path, timeout=10, isolation_level=None)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10, isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def has_owner(self) -> bool:
        with closing(self._connect()) as db:
            return db.execute("SELECT 1 FROM owner WHERE singleton=1").fetchone() is not None

    def ensure_bootstrap(self) -> bool:
        """Create a one-time file only when this store has no owner or nonce."""
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM owner WHERE singleton=1").fetchone():
                db.execute("DELETE FROM bootstrap")
                db.commit()
                return False
            row = db.execute("SELECT secret_digest FROM bootstrap WHERE singleton=1").fetchone()
            if row:
                self._ensure_safe_target(self.bootstrap_path)
                if (not self.bootstrap_path.is_file()
                        or self.bootstrap_path.stat().st_mode & 0o077):
                    db.rollback()
                    raise RuntimeError("bootstrap record exists but its private file is unavailable")
                db.commit()
                return False
            self._ensure_safe_target(self.bootstrap_path)
            if self.bootstrap_path.exists():
                db.rollback()
                raise RuntimeError("refusing to overwrite an existing bootstrap file")
            secret = secrets.token_urlsafe(32)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(self.bootstrap_path, flags, 0o600)
            try:
                os.write(descriptor, (secret + "\n").encode("ascii"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                db.execute("INSERT INTO bootstrap VALUES (1, ?, ?)",
                           (_digest_token(secret), int(self.now())))
                db.commit()
            except BaseException:
                db.rollback()
                self._unlink_own_bootstrap(_digest_token(secret))
                raise
            return True

    def _unlink_own_bootstrap(self, expected_digest: bytes) -> None:
        try:
            info = self.bootstrap_path.lstat()
            if (stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
                    or info.st_nlink != 1):
                return
            raw = self.bootstrap_path.read_bytes()
            if (len(raw) <= 256
                    and hmac.compare_digest(_digest_token(raw.decode("ascii").strip()), expected_digest)):
                self.bootstrap_path.unlink()
        except (FileNotFoundError, OSError, UnicodeError, ValueError):
            return

    @staticmethod
    def _password_record(password: str) -> tuple[bytes, bytes]:
        salt = secrets.token_bytes(16)
        value = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
        return salt, value

    @staticmethod
    def _supplied_bootstrap_digest(value: object) -> bytes:
        if isinstance(value, str) and len(value) <= 128 and value.isascii():
            return _digest_token(value)
        return bytes(hashlib.sha256().digest_size)

    def initialize(self, bootstrap_secret: object, password: object) -> None:
        normalized = normalize_password(password)
        supplied = self._supplied_bootstrap_digest(bootstrap_secret)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM owner WHERE singleton=1").fetchone():
                db.rollback()
                raise AuthError("initialization_unavailable", 409)
            if not self.bootstrap_limiter.reserve("bootstrap"):
                db.rollback()
                raise AuthError("try_again_later", 429)
            row = db.execute("SELECT secret_digest FROM bootstrap WHERE singleton=1").fetchone()
            valid = bool(row and _valid_digest(row[0])
                         and hmac.compare_digest(row[0], supplied))
            if not valid:
                db.rollback()
                raise AuthError("initialization_unavailable", 403)
            salt, password_hash = self._password_record(normalized)
            db.execute("INSERT INTO owner VALUES (1, ?, ?, ?, ?)",
                       (salt, password_hash, PBKDF2_ITERATIONS, int(self.now())))
            db.execute("DELETE FROM bootstrap WHERE singleton=1")
            db.commit()
        self.bootstrap_limiter.success("bootstrap")
        self._unlink_own_bootstrap(row[0])

    def initialize_local(self, password: object) -> None:
        normalized = normalize_password(password)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM owner WHERE singleton=1").fetchone():
                db.rollback()
                raise AuthError("initialization_unavailable", 409)
            salt, password_hash = self._password_record(normalized)
            db.execute("INSERT INTO owner VALUES (1, ?, ?, ?, ?)",
                       (salt, password_hash, PBKDF2_ITERATIONS, int(self.now())))
            row = db.execute("SELECT secret_digest FROM bootstrap WHERE singleton=1").fetchone()
            db.execute("DELETE FROM bootstrap")
            db.commit()
        if row:
            self._unlink_own_bootstrap(row[0])

    def login(self, password: object, peer: str) -> tuple[str, int]:
        if not self.limiter.reserve(peer):
            raise AuthError("try_again_later", 429)
        try:
            normalized = normalize_password(password)
        except AuthError:
            raise AuthError("invalid_credentials", 401)
        if not _KDF_SLOTS.acquire(blocking=False):
            raise AuthError("try_again_later", 429)
        try:
            with closing(self._connect()) as db:
                row = db.execute(
                    "SELECT salt, password_hash, iterations FROM owner WHERE singleton=1"
                ).fetchone()
                if row:
                    if not (isinstance(row[0], bytes) and len(row[0]) == 16
                            and _valid_digest(row[1]) and type(row[2]) is int
                            and row[2] == PBKDF2_ITERATIONS):
                        raise RuntimeError("corrupt owner credential record")
                    candidate = hashlib.pbkdf2_hmac(
                        "sha256", normalized.encode("utf-8"), row[0], PBKDF2_ITERATIONS
                    )
                    valid = hmac.compare_digest(candidate, row[1])
                else:
                    hashlib.pbkdf2_hmac(
                        "sha256", normalized.encode("utf-8"), bytes(16), PBKDF2_ITERATIONS
                    )
                    valid = False
                if not valid:
                    raise AuthError("invalid_credentials", 401)
                token = secrets.token_urlsafe(32)
                expires = int(self.now()) + self.session_seconds
                db.execute("DELETE FROM sessions WHERE expires_at <= ? OR revoked_at IS NOT NULL",
                           (int(self.now()),))
                db.execute("INSERT INTO sessions VALUES (?, ?, ?, NULL)",
                           (_digest_token(token), int(self.now()), expires))
        finally:
            _KDF_SLOTS.release()
        self.limiter.success(peer)
        return token, expires

    def authenticated(self, token: str | None) -> bool:
        if not token or len(token) > 128:
            return False
        try:
            digest = _digest_token(token)
        except (UnicodeEncodeError, AttributeError):
            return False
        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT token_digest FROM sessions "
                "WHERE token_digest=? AND revoked_at IS NULL AND expires_at>?",
                (digest, int(self.now())),
            ).fetchone()
        return bool(row and _valid_digest(row[0]) and hmac.compare_digest(row[0], digest))

    def logout(self, token: str | None) -> None:
        if not token:
            return
        try:
            digest = _digest_token(token)
        except (UnicodeEncodeError, AttributeError):
            return
        with closing(self._connect()) as db:
            db.execute("UPDATE sessions SET revoked_at=? WHERE token_digest=? AND revoked_at IS NULL",
                       (int(self.now()), digest))

    def cookie(self, token: str, max_age: int | None = None) -> str:
        value = f"{self.cookie_name}={token}; Path=/; HttpOnly; SameSite=Strict"
        if max_age is not None:
            value += f"; Max-Age={max_age}"
        if self.secure_cookie:
            value += "; Secure"
        return value
