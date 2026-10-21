"""Bounded immutable object storage over the boto3 S3 client contract."""
from __future__ import annotations

import hashlib
import ipaddress
import re
from urllib.parse import urlsplit

from workbench.raw_storage import RawStorageError


_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_PREFIX_PART_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class S3ObjectStore:
    """A single server-configured bucket/prefix for content-addressed bytes.

    The prefix is the authorization/isolation boundary (for example
    ``raw/public`` or ``raw/private/<server-owned-id>``). Runtime callers can
    supply only a digest and bytes; they cannot select keys, scopes, or URLs.
    """

    def __init__(
        self, *, bucket: str, prefix: str, client=None, endpoint_url: str | None = None,
        region_name: str | None = None, access_key_id: str | None = None,
        secret_access_key: str | None = None, session_token: str | None = None,
        local_emulator: bool = False, max_bytes: int = 20 * 1024 * 1024,
        connect_timeout: float = 3.0, read_timeout: float = 10.0,
        max_attempts: int = 3, conflict_retries: int = 2,
    ):
        self.bucket = self._bucket(bucket)
        self.prefix = self._prefix(prefix)
        self.max_object_bytes = self._positive_int(max_bytes, "max_bytes", maximum=1024 * 1024 * 1024)
        self.connect_timeout = self._timeout(connect_timeout, "connect_timeout")
        self.read_timeout = self._timeout(read_timeout, "read_timeout")
        self.max_attempts = self._positive_int(max_attempts, "max_attempts", maximum=10)
        if type(conflict_retries) is not int or not 0 <= conflict_retries <= 3:
            raise RawStorageError("invalid_storage_config", "conflict_retries must be between 0 and 3")
        self.conflict_retries = conflict_retries
        self.endpoint_url = self._endpoint(endpoint_url, local_emulator)

        supplied = (access_key_id is not None, secret_access_key is not None)
        if supplied[0] != supplied[1] or (session_token is not None and not all(supplied)):
            raise RawStorageError("invalid_storage_config", "S3 credentials must be supplied as a complete set")
        if local_emulator and client is None and not all(supplied):
            raise RawStorageError(
                "invalid_storage_config",
                "local S3 emulation requires explicit credentials; ambient discovery is disabled",
            )
        self._client = client
        self._client_options = {
            "endpoint_url": self.endpoint_url,
            "region_name": region_name,
            "aws_access_key_id": access_key_id,
            "aws_secret_access_key": secret_access_key,
            "aws_session_token": session_token,
        }
        self._local_emulator = local_emulator

    @staticmethod
    def _bucket(value: str) -> str:
        if (not isinstance(value, str) or _BUCKET_RE.fullmatch(value) is None
                or ".." in value or value.startswith("xn--")):
            raise RawStorageError("invalid_storage_config", "S3 bucket name is invalid")
        try:
            ipaddress.ip_address(value)
        except ValueError:
            return value
        raise RawStorageError("invalid_storage_config", "S3 bucket name must not be an IP address")

    @staticmethod
    def _prefix(value: str) -> str:
        if not isinstance(value, str) or value != value.strip("/"):
            raise RawStorageError("invalid_storage_config", "S3 prefix must be an isolated relative namespace")
        parts = value.split("/")
        if (len(parts) < 2 or parts[0] != "raw" or parts[1] not in {"public", "private"}
                or any(_PREFIX_PART_RE.fullmatch(part) is None for part in parts)):
            raise RawStorageError(
                "invalid_storage_config", "S3 prefix must be an isolated raw public/private namespace"
            )
        return "/".join(parts)

    @staticmethod
    def _positive_int(value, name: str, *, maximum: int) -> int:
        if type(value) is not int or not 1 <= value <= maximum:
            raise RawStorageError("invalid_storage_config", f"{name} is outside its supported range")
        return value

    @staticmethod
    def _timeout(value, name: str) -> float:
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not 0 < float(value) <= 120):
            raise RawStorageError("invalid_storage_config", f"{name} is outside the 0..120 second range")
        return float(value)

    @staticmethod
    def _endpoint(value: str | None, local_emulator: bool) -> str | None:
        if value is None:
            if local_emulator:
                raise RawStorageError("invalid_storage_config", "local S3 emulation requires an explicit endpoint")
            return None
        if not isinstance(value, str) or len(value) > 2048:
            raise RawStorageError("invalid_storage_config", "S3 endpoint is invalid")
        parts = urlsplit(value)
        if (not parts.hostname or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or parts.path not in ("", "/")):
            raise RawStorageError("invalid_storage_config", "S3 endpoint is invalid")
        if parts.scheme == "https":
            return value.rstrip("/")
        local_hosts = {"localhost", "127.0.0.1", "::1"}
        if parts.scheme == "http" and local_emulator and parts.hostname in local_hosts:
            return value.rstrip("/")
        raise RawStorageError(
            "invalid_storage_config",
            "S3 endpoints require HTTPS; HTTP is allowed only for an explicit localhost emulator",
        )

    @property
    def client(self):
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:
                raise RawStorageError("object_store_unavailable", "the configured S3 client is unavailable") from exc
            options = {key: value for key, value in self._client_options.items() if value is not None}
            options["config"] = Config(
                connect_timeout=self.connect_timeout,
                read_timeout=self.read_timeout,
                retries={"max_attempts": self.max_attempts, "mode": "standard"},
                s3={"addressing_style": "path" if self._local_emulator else "auto"},
            )
            try:
                self._client = boto3.client("s3", **options)
            except Exception as exc:
                raise RawStorageError("object_store_unavailable", "the configured S3 client could not be created") from exc
        return self._client

    @staticmethod
    def _digest(value: str) -> str:
        if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
            raise RawStorageError("object_tampered", "invalid object identifier")
        return value

    def _key(self, digest: str) -> str:
        return f"{self.prefix}/sha256/{self._digest(digest)}"

    @staticmethod
    def _error_status(exc: Exception) -> tuple[str, int | None]:
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return "", None
        error = response.get("Error")
        metadata = response.get("ResponseMetadata")
        code = str(error.get("Code", "")) if isinstance(error, dict) else ""
        status = metadata.get("HTTPStatusCode") if isinstance(metadata, dict) else None
        return code, status if isinstance(status, int) else None

    @classmethod
    def _is_conflict(cls, exc: Exception, status: int) -> bool:
        code, actual = cls._error_status(exc)
        names = {"PreconditionFailed"} if status == 412 else {"Conflict", "ConditionalRequestConflict"}
        return actual == status or code in names or code == str(status)

    @classmethod
    def _is_missing(cls, exc: Exception) -> bool:
        code, status = cls._error_status(exc)
        return status == 404 or code in {"404", "NoSuchKey", "NotFound", "NoSuchBucket"}

    def put_if_absent(self, digest: str, raw: bytes) -> None:
        digest = self._digest(digest)
        if not isinstance(raw, bytes) or not 0 < len(raw) <= self.max_object_bytes:
            raise RawStorageError("invalid_file", f"source must contain 1..{self.max_object_bytes} bytes")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise RawStorageError("source_hash_mismatch", "object identifier does not match source bytes")
        key = self._key(digest)
        for attempt in range(self.conflict_retries + 1):
            try:
                self.client.put_object(Bucket=self.bucket, Key=key, Body=raw, IfNoneMatch="*")
                return
            except Exception as exc:
                if self._is_conflict(exc, 412):
                    self.get_verified(digest, len(raw))
                    return
                if self._is_conflict(exc, 409):
                    if attempt < self.conflict_retries:
                        continue
                    self.get_verified(digest, len(raw))
                    return
                raise RawStorageError("object_store_unavailable", "S3 conditional object write failed") from exc

    def get_verified(self, digest: str, expected_size: int | None = None) -> bytes:
        digest = self._digest(digest)
        if (expected_size is not None
                and (type(expected_size) is not int or not 0 <= expected_size <= self.max_object_bytes)):
            raise RawStorageError("object_tampered", "stored source object has an unexpected size")
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(digest))
        except Exception as exc:
            if self._is_missing(exc):
                raise RawStorageError("object_tampered", "stored source object is missing or unreadable") from exc
            raise RawStorageError("object_store_unavailable", "S3 object read failed") from exc
        body = response.get("Body") if isinstance(response, dict) else None
        if body is None or not callable(getattr(body, "read", None)):
            raise RawStorageError("object_store_unavailable", "S3 object read returned an invalid body")
        chunks: list[bytes] = []
        count = 0
        try:
            while count <= self.max_object_bytes:
                chunk = body.read(min(128 * 1024, self.max_object_bytes + 1 - count))
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise RawStorageError("object_store_unavailable", "S3 object read returned invalid bytes")
                chunks.append(chunk)
                count += len(chunk)
        except RawStorageError:
            raise
        except Exception as exc:
            raise RawStorageError("object_store_unavailable", "S3 object body could not be read") from exc
        finally:
            try:
                body.close()
            except Exception:
                pass
        raw = b"".join(chunks)
        if len(raw) > self.max_object_bytes or (expected_size is not None and len(raw) != expected_size):
            raise RawStorageError("object_tampered", "stored source object has an unexpected size")
        if hashlib.sha256(raw).hexdigest() != digest:
            raise RawStorageError("object_tampered", "stored source object failed hash verification")
        return raw
