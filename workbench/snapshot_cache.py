"""Bounded, reconstructable local files for verified immutable SQL snapshots."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from typing import Callable, Iterator


class SnapshotCacheError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


class VerifiedSnapshotCache:
    """Materialize verified bytes atomically and protect in-use files from eviction.

    Cache keys are SHA-256 digests only. Advisory locks provide download
    de-duplication and active-use protection across service processes sharing the
    cache directory. Cache contents are always re-hashed before use.
    """

    _LOCK_TIMEOUT_SECONDS = 10.0

    def __init__(self, root, *, max_bytes: int, max_entry_bytes: int):
        if type(max_bytes) is not int or type(max_entry_bytes) is not int or max_bytes <= 0 or max_entry_bytes <= 0:
            raise ValueError("snapshot cache limits must be positive integers")
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("snapshot cache root must be a real directory")
        self.max_bytes = max_bytes
        self.max_entry_bytes = min(max_entry_bytes, max_bytes)
        self._thread_lock = threading.Lock()
        self._publication_lock = self.root / ".publication.lock"

    @staticmethod
    def _validate(digest: str, expected_size: int) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", digest or "") is None:
            raise SnapshotCacheError("snapshot_cache_integrity", "invalid snapshot cache identifier")
        if type(expected_size) is not int or expected_size <= 0:
            raise SnapshotCacheError("snapshot_cache_integrity", "invalid snapshot cache size")

    def _paths(self, key: str) -> tuple[Path, Path]:
        return self.root / (key + ".sqlite"), self.root / (key + ".lock")

    @classmethod
    def _lock(cls, path: Path, operation: int):
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        handle = os.fdopen(descriptor, "a+b")
        try:
            deadline = time.monotonic() + cls._LOCK_TIMEOUT_SECONDS
            explicitly_nonblocking = bool(operation & fcntl.LOCK_NB)
            while True:
                try:
                    fcntl.flock(handle.fileno(), operation | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if explicitly_nonblocking:
                        raise
                    if time.monotonic() >= deadline:
                        raise SnapshotCacheError("snapshot_cache_busy", "snapshot cache lock wait timed out")
                    time.sleep(0.01)
        except BaseException:
            handle.close()
            raise
        return handle

    def _cleanup_temporaries(self) -> None:
        for path in self.root.glob(".snapshot-*"):
            try:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                    path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _verified(path: Path, digest: str, expected_size: int) -> bool:
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
                return False
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as handle:
                return hashlib.file_digest(handle, "sha256").hexdigest() == digest
        except OSError:
            return False

    def _evict(self, needed: int, *, keep: str) -> None:
        entries = []
        total = 0
        for path in self.root.glob("*.sqlite"):
            try:
                info = path.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode):
                total += info.st_size
                entries.append((info.st_mtime_ns, path, info.st_size))
        for _mtime, path, size in sorted(entries):
            if total + needed <= self.max_bytes:
                break
            key = path.stem
            if key == keep:
                continue
            _data, lock_path = self._paths(key)
            try:
                lock = self._lock(lock_path, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (SnapshotCacheError, BlockingIOError, OSError):
                continue
            try:
                try:
                    path.unlink()
                    total -= size
                except FileNotFoundError:
                    pass
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                lock.close()
        if total + needed > self.max_bytes:
            raise SnapshotCacheError("snapshot_cache_limit", "snapshot cache has no evictable capacity")

    def _populate(self, path: Path, key: str, digest: str, expected_size: int, loader: Callable[[], bytes]) -> None:
        raw = loader()
        if not isinstance(raw, bytes) or len(raw) != expected_size or len(raw) > self.max_entry_bytes:
            raise SnapshotCacheError("snapshot_source_integrity", "snapshot source returned an unexpected size")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise SnapshotCacheError("snapshot_source_integrity", "snapshot source failed hash verification")
        descriptor, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=self.root)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o400)
            # Account at the last possible point before serialized publication.
            self._evict(expected_size, keep=key)
            os.replace(temporary, path)
            temporary = None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    @contextmanager
    def open(self, digest: str, expected_size: int, loader: Callable[[], bytes], *, scope: str) -> Iterator[Path]:
        self._validate(digest, expected_size)
        if scope != "public" and re.fullmatch(r"[0-9a-f]{64}", scope or "") is None:
            raise SnapshotCacheError("snapshot_cache_integrity", "invalid snapshot cache scope")
        if expected_size > self.max_entry_bytes:
            raise SnapshotCacheError("snapshot_cache_limit", "snapshot exceeds the configured local cache limit")
        key = scope + "-" + digest
        path, lock_path = self._paths(key)

        # A verified hit needs only an active-reader lock. Taking EX first would
        # block a second alias of a snapshot behind this process's first alias.
        shared = self._lock(lock_path, fcntl.LOCK_SH)
        if self._verified(path, digest, expected_size):
            try:
                try:
                    os.utime(path, None, follow_symlinks=False)
                except OSError:
                    pass
                yield path
            finally:
                fcntl.flock(shared.fileno(), fcntl.LOCK_UN)
                shared.close()
            return
        fcntl.flock(shared.fileno(), fcntl.LOCK_UN)
        shared.close()

        # Global-before-key is the sole population order. Reader release never
        # takes the global lock, so eviction can safely skip active entries.
        with self._thread_lock:
            publication = self._lock(self._publication_lock, fcntl.LOCK_EX)
            try:
                self._cleanup_temporaries()
                exclusive = self._lock(lock_path, fcntl.LOCK_EX)
                try:
                    if not self._verified(path, digest, expected_size):
                        try:
                            path.unlink()
                        except FileNotFoundError:
                            pass
                        self._populate(path, key, digest, expected_size, loader)
                    self._evict(0, keep=key)
                    try:
                        os.utime(path, None, follow_symlinks=False)
                    except OSError:
                        pass
                    fcntl.flock(exclusive.fileno(), fcntl.LOCK_SH)
                except BaseException:
                    fcntl.flock(exclusive.fileno(), fcntl.LOCK_UN)
                    exclusive.close()
                    raise
            finally:
                fcntl.flock(publication.fileno(), fcntl.LOCK_UN)
                publication.close()
        try:
            if not self._verified(path, digest, expected_size):
                raise SnapshotCacheError("snapshot_cache_integrity", "cached snapshot changed before use")
            yield path
        finally:
            fcntl.flock(exclusive.fileno(), fcntl.LOCK_UN)
            exclusive.close()
