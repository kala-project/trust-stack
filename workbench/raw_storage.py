"""Shared content-addressed storage for immutable source bytes."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Protocol, Type, runtime_checkable


class RawStorageError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message

    def as_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


@runtime_checkable
class ObjectStore(Protocol):
    """Minimal immutable-byte backend contract.

    A configured backend owns its complete namespace. Callers provide only a
    SHA-256 digest, never a bucket, path, URL, scope, or arbitrary object key.
    """

    def put_if_absent(self, digest: str, raw: bytes) -> None: ...

    def get_verified(self, digest: str, expected_size: int | None = None) -> bytes: ...


class LocalObjectStore:
    """Content-addressed files below ``root/objects`` with tamper-safe reads."""

    def __init__(self, root: os.PathLike[str] | str, *, max_bytes: int = 20 * 1024 * 1024):
        self.max_object_bytes = int(max_bytes)
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise RawStorageError("invalid_root", "document root must be a real directory")
        self.objects_path = self.root / "objects"
        self.objects_path.mkdir(mode=0o700, exist_ok=True)
        if self.objects_path.is_symlink() or not self.objects_path.is_dir():
            raise RawStorageError("invalid_root", "objects must be a real directory")
        root_info = self.root.stat()
        objects_info = self.objects_path.stat()
        self._root_identity = (root_info.st_dev, root_info.st_ino)
        self._objects_identity = (objects_info.st_dev, objects_info.st_ino)

    def _ensure_storage_dirs(self) -> None:
        try:
            root_info = self.root.lstat()
            objects_info = self.objects_path.lstat()
        except OSError as exc:
            raise RawStorageError("object_tampered", "document storage directory is missing") from exc
        if (
            stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode)
            or stat.S_ISLNK(objects_info.st_mode) or not stat.S_ISDIR(objects_info.st_mode)
            or (root_info.st_dev, root_info.st_ino) != self._root_identity
            or (objects_info.st_dev, objects_info.st_ino) != self._objects_identity
        ):
            raise RawStorageError("object_tampered", "document storage directory was replaced")

    def _object_path(self, digest: str) -> Path:
        self._ensure_storage_dirs()
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise RawStorageError("object_tampered", "invalid object identifier")
        return self.objects_path / digest

    def get_verified(self, digest: str, expected_size: int | None = None) -> bytes:
        path = self._object_path(digest)
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise RawStorageError("object_tampered", "stored source object is not a regular file")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags)
            with os.fdopen(fd, "rb") as handle:
                raw = handle.read(self.max_object_bytes + 1)
        except RawStorageError:
            raise
        except OSError as exc:
            raise RawStorageError("object_tampered", "stored source object is missing or unreadable") from exc
        if len(raw) > self.max_object_bytes or (expected_size is not None and len(raw) != expected_size):
            raise RawStorageError("object_tampered", "stored source object has an unexpected size")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise RawStorageError("object_tampered", "stored source object failed hash verification")
        return raw

    def put_if_absent(self, digest: str, raw: bytes) -> None:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= self.max_object_bytes:
            raise RawStorageError("invalid_file", f"source must contain 1..{self.max_object_bytes} bytes")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise RawStorageError("source_hash_mismatch", "object identifier does not match source bytes")
        destination = self._object_path(digest)
        if destination.exists() or destination.is_symlink():
            self.get_verified(digest, len(raw))
            return
        fd, temporary = tempfile.mkstemp(prefix=".incoming-", dir=self.objects_path)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                self.get_verified(digest, len(raw))
            try:
                directory_fd = os.open(self.objects_path, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


class RawObjectStore:
    """Local metadata root plus an injectable immutable-byte backend.

    With no backend supplied this preserves the historical local ``objects``
    directory and its inode protections. Remote backends never participate in
    those local-path checks; the catalog or ledger root remains local.
    """

    def __init__(self, root: os.PathLike[str] | str, *, error_type: Type[ValueError] = RawStorageError,
                 max_bytes: int = 20 * 1024 * 1024, object_store: ObjectStore | None = None):
        self._raw_error_type = error_type
        self.max_object_bytes = int(max_bytes)
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise self._error("invalid_root", "document root must be a real directory")
        root_info = self.root.stat()
        self._root_identity = (root_info.st_dev, root_info.st_ino)
        try:
            self.object_store: ObjectStore = (
                LocalObjectStore(self.root, max_bytes=self.max_object_bytes)
                if object_store is None else object_store
            )
        except RawStorageError as exc:
            raise self._error(exc.code, exc.message) from exc
        if not isinstance(self.object_store, ObjectStore):
            raise self._error("invalid_object_store", "object store does not implement the required interface")
        if isinstance(self.object_store, LocalObjectStore):
            # Backward-compatible local-only path surface used by existing tools/tests.
            self.objects_path = self.object_store.objects_path

    def _error(self, code: str, message: str) -> ValueError:
        return self._raw_error_type(code, message)

    def _translate(self, call):
        try:
            return call()
        except RawStorageError as exc:
            raise self._error(exc.code, exc.message) from exc

    def _ensure_storage_dirs(self) -> None:
        try:
            root_info = self.root.lstat()
        except OSError as exc:
            raise self._error("object_tampered", "document storage directory is missing") from exc
        if (stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode)
                or (root_info.st_dev, root_info.st_ino) != self._root_identity):
            raise self._error("object_tampered", "document storage directory was replaced")
        if isinstance(self.object_store, LocalObjectStore):
            self._translate(self.object_store._ensure_storage_dirs)

    def _object_path(self, digest: str) -> Path:
        if not isinstance(self.object_store, LocalObjectStore):
            raise self._error("object_path_unavailable", "external object stores do not expose local paths")
        return self._translate(lambda: self.object_store._object_path(digest))

    def _verify_object(self, digest: str, expected_size: int | None = None) -> bytes:
        return self._translate(lambda: self.object_store.get_verified(digest, expected_size))

    def _store_object(self, digest: str, raw: bytes) -> None:
        if not isinstance(raw, bytes) or not 0 < len(raw) <= self.max_object_bytes:
            raise self._error("invalid_file", f"source must contain 1..{self.max_object_bytes} bytes")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise self._error("source_hash_mismatch", "object identifier does not match source bytes")
        self._translate(lambda: self.object_store.put_if_absent(digest, raw))
