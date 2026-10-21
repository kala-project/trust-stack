"""Strict server-owned configuration for public and conversation object storage."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from contextlib import closing
from typing import Callable, Mapping
from urllib.parse import urlsplit

from .object_migration import (
    MigrationError, StoreBinding, manifest_sha256, references_from_sqlite,
    validate_manifest,
)
from .raw_storage import LocalObjectStore, ObjectStore, RawStorageError
from .s3_storage import S3ObjectStore


_DEPLOYMENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_ENV_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_TOP_LEVEL = {
    "version", "backend", "deployment", "bucket", "endpoint_url", "region_name",
    "local_emulator", "credentials", "max_bytes",
}
_ENV_CREDENTIALS = {"access_key_id_env", "secret_access_key_env", "session_token_env"}
_IDENTITY_NAME = ".object-storage-identity.json"
_MIGRATION_PROOF_VERSION = "object-store-cutover-proof/2"


class StorageConfigError(RuntimeError):
    """A storage configuration is invalid or unsafe for the local metadata root."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _object(value, name: str) -> dict:
    if not isinstance(value, dict):
        raise StorageConfigError("invalid_storage_config", f"{name} must be an object")
    return value


def _optional_text(config: dict, name: str) -> str | None:
    value = config.get(name)
    if value is not None and (not isinstance(value, str) or not value):
        raise StorageConfigError("invalid_storage_config", f"{name} must be a non-empty string or null")
    return value


class StorageConfiguration:
    """Validated non-secret identity plus fixed public/private S3 store factories."""

    def __init__(self, payload: dict, *, environ: Mapping[str, str] | None = None,
                 store_factory: Callable[..., ObjectStore] = S3ObjectStore):
        config = _object(payload, "storage configuration")
        unknown = set(config) - _TOP_LEVEL
        if unknown:
            raise StorageConfigError("invalid_storage_config", "storage configuration contains unknown fields")
        if config.get("version") != 1 or type(config.get("version")) is not int:
            raise StorageConfigError("invalid_storage_config", "storage configuration version must be 1")
        if config.get("backend") != "s3":
            raise StorageConfigError("invalid_storage_config", "storage backend must be s3")
        deployment = config.get("deployment")
        if not isinstance(deployment, str) or _DEPLOYMENT_RE.fullmatch(deployment) is None:
            raise StorageConfigError("invalid_storage_config", "deployment is not a valid fixed namespace")
        bucket = config.get("bucket")
        if not isinstance(bucket, str):
            raise StorageConfigError("invalid_storage_config", "bucket must be a string")
        local_emulator = config.get("local_emulator", False)
        if type(local_emulator) is not bool:
            raise StorageConfigError("invalid_storage_config", "local_emulator must be a boolean")
        max_bytes = config.get("max_bytes", 20 * 1024 * 1024)
        if type(max_bytes) is not int:
            raise StorageConfigError("invalid_storage_config", "max_bytes must be an integer")
        endpoint_url = _optional_text(config, "endpoint_url")
        region_name = _optional_text(config, "region_name")
        try:
            S3ObjectStore._endpoint(endpoint_url, local_emulator)
            if local_emulator and urlsplit(endpoint_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
                raise ValueError("non-local emulator")
        except (ValueError, RawStorageError) as exc:
            raise StorageConfigError("invalid_storage_config", "storage endpoint is invalid for the selected mode") from exc
        credentials = _object(config.get("credentials"), "credentials")
        env = os.environ if environ is None else environ
        if set(credentials) == {"test_credentials"}:
            if credentials["test_credentials"] is not True or not local_emulator:
                raise StorageConfigError(
                    "invalid_storage_config",
                    "fixed test credentials are allowed only for an explicit local emulator",
                )
            resolved_credentials = {"access_key_id": "invalid-test-key", "secret_access_key": "invalid-test-secret"}
        else:
            unknown_credentials = set(credentials) - _ENV_CREDENTIALS
            required = {"access_key_id_env", "secret_access_key_env"}
            if unknown_credentials or not required.issubset(credentials):
                raise StorageConfigError(
                    "invalid_storage_config", "credentials must name the required credential environment variables"
                )
            resolved_credentials = {}
            for field in sorted(credentials):
                variable = credentials[field]
                if not isinstance(variable, str) or _ENV_RE.fullmatch(variable) is None:
                    raise StorageConfigError("invalid_storage_config", "credential environment variable name is invalid")
                value = env.get(variable)
                if not isinstance(value, str) or not value:
                    raise StorageConfigError("missing_storage_credentials", "a configured storage credential is unavailable")
                resolved_credentials[field.removesuffix("_env")] = value

        # S3ObjectStore performs bucket, endpoint, bounds, and prefix validation here,
        # before a lazy SDK client can be created.
        common = {
            "bucket": bucket,
            "endpoint_url": endpoint_url,
            "region_name": region_name,
            "local_emulator": local_emulator,
            "max_bytes": max_bytes,
            **resolved_credentials,
        }
        try:
            probe = store_factory(prefix=f"raw/public/{deployment}", **common)
        except RawStorageError as exc:
            raise StorageConfigError(exc.code, exc.message) from exc
        if not isinstance(probe, ObjectStore):
            raise StorageConfigError("invalid_storage_config", "configured store factory is invalid")

        self._payload = json.loads(json.dumps(config))
        self._common = common
        self._store_factory = store_factory
        self._public_store = probe
        self.max_object_bytes = max_bytes
        self.deployment = deployment
        canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        self.config_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def load(cls, path, *, environ: Mapping[str, str] | None = None,
             store_factory: Callable[..., ObjectStore] = S3ObjectStore) -> "StorageConfiguration":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise StorageConfigError("invalid_storage_config", "storage configuration cannot be read") from exc
        return cls(payload, environ=environ, store_factory=store_factory)

    @property
    def identity(self) -> dict:
        return {
            "version": 1,
            "backend": "s3",
            "deployment": self.deployment,
            "config_sha256": self.config_sha256,
            "public_namespace": f"raw/public/{self.deployment}",
            "private_namespace": f"raw/private/{self.deployment}",
        }

    def public_store(self) -> ObjectStore:
        return self._public_store

    def _scoped_store(self, prefix: str) -> ObjectStore:
        try:
            store = self._store_factory(prefix=prefix, **self._common)
        except RawStorageError as exc:
            raise StorageConfigError(exc.code, exc.message) from exc
        if not isinstance(store, ObjectStore):
            raise StorageConfigError("invalid_storage_config", "configured store factory is invalid")
        return store

    def private_store(self, conversation_id: str) -> ObjectStore:
        if not isinstance(conversation_id, str):
            raise StorageConfigError("invalid_storage_scope", "verified conversation id is required")
        scope = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()
        return self._scoped_store(f"raw/private/{self.deployment}/{scope}")

    def native_public_store(self) -> ObjectStore:
        return self._scoped_store(f"native-sql/public/{self.deployment}")

    def native_private_store(self, conversation_id: str) -> ObjectStore:
        if not isinstance(conversation_id, str):
            raise StorageConfigError("invalid_storage_scope", "verified conversation id is required")
        scope = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()
        return self._scoped_store(f"native-sql/private/{self.deployment}/{scope}")

    @property
    def native_store_identity(self) -> str:
        return f"s3:{self.config_sha256}:native-sql:{self.deployment}"

    def register_public_root(self, root) -> None:
        self._register_identity(Path(root), scope="public")

    def register_private_root(self, root, *, public_root=None) -> None:
        self._register_identity(Path(root), scope="private", public_root=public_root)

    def register_recovered_root(self, root, *, scope: str, recovery: Mapping,
                                objects: list[Mapping], public_root=None) -> None:
        """Register a whole-set-verified isolated restore without its live source."""
        invalid = lambda message: StorageConfigError("storage_recovery_proof_invalid", message)
        if scope not in {"public", "private"}:
            raise invalid("recovery scope must be public or private")
        if not isinstance(recovery, Mapping) or not isinstance(objects, list):
            raise invalid("verified recovery manifest is required")
        verification = recovery.get("verification")
        targets = recovery.get("target_identities")
        if (not isinstance(verification, Mapping)
                or verification.get("frozen_reader_source_identities_verified") is not True
                or type(verification.get("verified_objects")) is not int
                or verification["verified_objects"] != len(objects)
                or type(verification.get("historical_incomplete_artifacts")) is not int
                or verification["historical_incomplete_artifacts"] < 0
                or not isinstance(targets, Mapping)):
            raise invalid("whole backup object set was not verified")

        # configured_bindings always includes public, including native-only
        # recoveries whose empty public catalog contributes no raw object.
        expected_targets = {
            "public": f"s3:{self.config_sha256}:raw/public/{self.deployment}",
        }
        for item in objects:
            if not isinstance(item, Mapping) or not isinstance(item.get("scope"), str):
                raise invalid("recovery object manifest is invalid")
            object_scope = item["scope"]
            if object_scope == "public":
                identity = f"s3:{self.config_sha256}:raw/public/{self.deployment}"
            elif object_scope.startswith("private:"):
                owner = object_scope.removeprefix("private:")
                if not owner:
                    raise invalid("recovery private scope is invalid")
                identity = (f"s3:{self.config_sha256}:raw/private/{self.deployment}/"
                            f"{hashlib.sha256(owner.encode('utf-8')).hexdigest()}")
            elif object_scope == "native:public":
                identity = f"s3:{self.config_sha256}:native-sql/public/{self.deployment}"
            elif object_scope.startswith("native:private:"):
                owner = object_scope.removeprefix("native:private:")
                if not owner:
                    raise invalid("recovery native scope is invalid")
                identity = (f"s3:{self.config_sha256}:native-sql/private/{self.deployment}/"
                            f"{hashlib.sha256(owner.encode('utf-8')).hexdigest()}")
            else:
                raise invalid("recovery object scope is unsupported")
            expected_targets[object_scope] = identity
        if dict(targets) != expected_targets:
            raise invalid("recovery target identities do not match the configured namespace")

        root = Path(root).expanduser().resolve()
        identity_path = root / _IDENTITY_NAME
        old_prefix = None
        if identity_path.exists() or identity_path.is_symlink():
            if identity_path.is_symlink() or not identity_path.is_file() or identity_path.stat().st_size > 16384:
                raise invalid("copied source registration is invalid")
            try:
                copied = json.loads(identity_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise invalid("copied source registration cannot be verified") from exc
            if (not isinstance(copied, dict) or copied.get("version") != 1
                    or copied.get("backend") != "s3" or copied.get("scope") != scope):
                raise invalid("copied source registration does not match the recovery scope")
            old_hash, old_deployment = copied.get("config_sha256"), copied.get("deployment")
            if not isinstance(old_hash, str) or not isinstance(old_deployment, str):
                raise invalid("copied source registration identity is incomplete")
            old_prefix = f"s3:{old_hash}:raw/{scope}/{old_deployment}"

        prefix = "public" if scope == "public" else "private:"
        relevant = [item for item in objects if item["scope"] == prefix or (
            scope == "private" and item["scope"].startswith(prefix))]
        source_identities = [item.get("source_identity") for item in relevant]
        if not relevant:
            if old_prefix is None:
                raise invalid("empty recovery scope has no copied source registration")
            source_identities = [old_prefix]
        if any(not isinstance(value, str) or not value for value in source_identities):
            raise invalid("recovery source identities are invalid")
        source_identities = sorted(set(source_identities))
        if old_prefix is not None and any(
                value != old_prefix and not value.startswith(old_prefix + "/")
                for value in source_identities):
            raise invalid("copied registration differs from backup source identities")

        registration = {
            "source_identities": source_identities,
            "verified_objects": verification["verified_objects"],
            "historical_incomplete_artifacts": verification.get("historical_incomplete_artifacts", 0),
        }
        self._register_identity(root, scope=scope, public_root=public_root,
                                recovery_registration=registration)

    def register_migrated_root(self, root, *, scope: str, proof: Mapping,
                               original_identity: str, public_root=None) -> None:
        """Register a local root only after rechecking its complete manifest closure.

        Proof JSON is reviewable operator evidence, not authorization.  Registration
        therefore re-derives every live catalog reference and re-reads every relevant
        source and configured target object before recording the identity.
        """
        invalid = lambda message: StorageConfigError("storage_migration_proof_invalid", message)
        if scope not in {"public", "private"}:
            raise invalid("migration scope must be public or private")
        if (not isinstance(original_identity, str) or not original_identity
                or len(original_identity) > 1000):
            raise invalid("preserved original identity is required")
        if not isinstance(proof, Mapping) or proof.get("version") != _MIGRATION_PROOF_VERSION:
            raise invalid("reviewed migration proof with a complete manifest is required")
        for name in ("baseline_manifest_sha256", "candidate_manifest_sha256", "completion_manifest_sha256"):
            value = proof.get(name)
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise invalid("migration proof hashes are invalid")
        delta, mapping = proof.get("delta"), proof.get("identity_mapping")
        candidate, completion = proof.get("candidate_manifest"), proof.get("completion")
        try:
            objects = validate_manifest(candidate)
            candidate_hash = manifest_sha256(candidate)
        except MigrationError as exc:
            raise invalid("migration proof candidate manifest is invalid") from exc
        if (candidate_hash != proof["candidate_manifest_sha256"]
                or proof["completion_manifest_sha256"] != candidate_hash
                or not isinstance(completion, Mapping) or completion.get("complete") is not True
                or completion.get("generation") != candidate.get("generation")
                or completion.get("manifest_sha256") != candidate_hash
                or not isinstance(delta, Mapping) or delta.get("missing") != []
                or not isinstance(mapping, Mapping) or not mapping):
            raise invalid("migration proof is incomplete or internally inconsistent")

        manifest_mapping = {}
        for item in objects:
            pair = {name: item[name] for name in (
                "source_backend", "source_identity", "target_backend", "target_identity")}
            previous = manifest_mapping.setdefault(item["scope"], pair)
            if previous != pair:
                raise invalid("manifest has inconsistent namespace identity mapping")
        if dict(mapping) != manifest_mapping:
            raise invalid("proof identity mapping does not match its candidate manifest")

        root = Path(root)
        if root.is_symlink():
            raise invalid("registered migration root must not be a symbolic link")
        root = root.expanduser().resolve()
        prefix = "public" if scope == "public" else "private:"
        relevant = [item for item in objects if item["scope"] == prefix or (
            scope == "private" and item["scope"].startswith(prefix))]
        if not relevant:
            raise invalid("candidate manifest does not contain the registered scope")

        if scope == "public":
            stores = {"public": root}
            accepted_originals = {"local:" + str(root)}
        else:
            if not root.is_dir():
                raise invalid("private migration root does not exist")
            stores = {}
            for child in root.iterdir():
                if child.name == _IDENTITY_NAME:
                    continue
                if child.is_symlink() or not child.is_dir():
                    raise invalid("private migration root contains an invalid entry")
                stores["local:" + str(child.resolve())] = child.resolve()
            accepted_originals = {"local:" + str(root), *stores}
        if original_identity not in accepted_originals:
            raise invalid("preserved original identity does not identify the registered root")
        registered_sources = ({"local:" + str(root)} if scope == "public" else set(stores))
        relevant_ids = {id(item) for item in relevant}
        if any(item["source_identity"] in registered_sources and id(item) not in relevant_ids
               for item in objects):
            raise invalid("candidate manifest contains an extra source reference for this root")

        current, source_bindings, target_bindings = {}, {}, {}
        used_private_stores = set()
        for item in relevant:
            object_scope = item["scope"]
            if scope == "public":
                store_root = stores["public"]
                expected_source = "local:" + str(store_root)
                target_store = self.public_store()
                expected_target = f"s3:{self.config_sha256}:raw/public/{self.deployment}"
            else:
                expected_source = item["source_identity"]
                store_root = stores.get(expected_source)
                owner = object_scope.removeprefix("private:")
                expected_child = root / hashlib.sha256(owner.encode("utf-8")).hexdigest()
                if store_root is None or store_root != expected_child.resolve():
                    raise invalid("private proof does not map an existing conversation root")
                used_private_stores.add(store_root)
                target_store = self.private_store(owner)
                expected_target = (f"s3:{self.config_sha256}:raw/private/{self.deployment}/"
                                   f"{hashlib.sha256(owner.encode('utf-8')).hexdigest()}")
            if (item["source_backend"] != "local" or item["source_identity"] != expected_source
                    or item["target_backend"] != "s3" or item["target_identity"] != expected_target):
                raise invalid("proof does not map this preserved source to its configured namespace")
            source_bindings[object_scope] = StoreBinding("local", expected_source, LocalObjectStore(store_root))
            target_bindings[object_scope] = StoreBinding("s3", expected_target, target_store)

        catalog_stores = [root] if scope == "public" else sorted(used_private_stores)
        for store_root in catalog_stores:
            object_scope = "public"
            if scope == "private":
                identities = [item["scope"] for item in relevant
                              if item["source_identity"] == "local:" + str(store_root)]
                if len(set(identities)) != 1:
                    raise invalid("private root is not mapped to exactly one conversation")
                object_scope = identities[0]
            for filename, kind in (("catalog.sqlite3", "document_catalog"),
                                   ("source_acquisitions.sqlite3", "source_catalog")):
                catalog = store_root / filename
                if catalog.is_symlink():
                    raise invalid("source catalog must not be a symbolic link")
                if catalog.is_file():
                    try:
                        refs = references_from_sqlite(catalog, kind=kind, scope=object_scope)
                    except (MigrationError, sqlite3.Error) as exc:
                        raise invalid("source catalog cannot be verified") from exc
                    for ref in refs:
                        key = ref["scope"], ref["sha256"], ref["bytes"]
                        current.setdefault(key, set()).update(
                            value.split(":", 1)[-1] for value in ref["metadata_references"])
        manifested = {}
        for item in relevant:
            key = item["scope"], item["sha256"], item["bytes"]
            # Backup freezing replaces the catalog filename with its resource path;
            # table/row provenance remains stable and is the live closure key.
            manifested.setdefault(key, set()).update(
                value.split(":", 1)[-1] for value in item["metadata_references"])
        if current != manifested:
            raise invalid("candidate manifest does not equal the current catalog object closure")
        if scope == "private":
            nonempty = {path for path in stores.values() if self._has_local_objects(path, scope="public")}
            if nonempty != used_private_stores:
                raise invalid("proof does not cover every existing private object root")
        try:
            for item in relevant:
                source_bindings[item["scope"]].store.get_verified(item["sha256"], item["bytes"])
                target_bindings[item["scope"]].store.get_verified(item["sha256"], item["bytes"])
        except (RawStorageError, OSError) as exc:
            raise invalid("source or target object closure cannot be verified") from exc

        registration = {
            "proof_version": _MIGRATION_PROOF_VERSION,
            "baseline_manifest_sha256": proof["baseline_manifest_sha256"],
            "candidate_manifest_sha256": candidate_hash,
            "original_identity": original_identity,
            "source_identities": sorted({item["source_identity"] for item in relevant}),
        }
        self._register_identity(root, scope=scope, public_root=public_root,
                                migration_registration=registration)

    @staticmethod
    def _registered_identity_matches(actual, expected) -> bool:
        if actual == expected:
            return True
        if not isinstance(actual, dict):
            return False
        if set(actual) == set(expected) | {"recovery_registration"}:
            registration = actual.get("recovery_registration")
            return ({key: actual.get(key) for key in expected} == expected
                    and isinstance(registration, dict)
                    and set(registration) == {"source_identities", "verified_objects",
                                             "historical_incomplete_artifacts"}
                    and isinstance(registration.get("source_identities"), list)
                    and registration["source_identities"]
                    and all(isinstance(value, str) and value
                            for value in registration["source_identities"])
                    and type(registration.get("verified_objects")) is int
                    and registration["verified_objects"] > 0
                    and type(registration.get("historical_incomplete_artifacts")) is int
                    and registration["historical_incomplete_artifacts"] >= 0)
        if set(actual) != set(expected) | {"migration_registration"}:
            return False
        registration = actual.get("migration_registration")
        return ({key: actual.get(key) for key in expected} == expected
                and isinstance(registration, dict)
                and set(registration) == {"proof_version", "baseline_manifest_sha256",
                                         "candidate_manifest_sha256", "original_identity",
                                         "source_identities"}
                and registration.get("proof_version") == _MIGRATION_PROOF_VERSION
                and isinstance(registration.get("source_identities"), list)
                and registration["source_identities"]
                and all(isinstance(value, str) and value
                        for value in registration["source_identities"])
                and all(isinstance(registration.get(key), str) and registration[key]
                        for key in ("baseline_manifest_sha256", "candidate_manifest_sha256", "original_identity")))

    def _register_identity(self, root: Path, *, scope: str, public_root=None,
                           migration_registration=None, recovery_registration=None) -> None:
        if root.is_symlink():
            raise StorageConfigError("storage_identity_invalid", "storage root must not be a symbolic link")
        root = root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        identity_path = root / _IDENTITY_NAME
        expected = {**self.identity, "scope": scope}
        if public_root is not None:
            expected["public_metadata_root"] = str(Path(public_root).expanduser().resolve())
        if identity_path.exists() or identity_path.is_symlink():
            if identity_path.is_symlink() or not identity_path.is_file() or identity_path.stat().st_size > 16384:
                raise StorageConfigError("storage_identity_invalid", "storage identity must be a bounded regular file")
            try:
                actual = json.loads(identity_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise StorageConfigError("storage_identity_invalid", "storage identity cannot be verified") from exc
            if not self._registered_identity_matches(actual, expected):
                if recovery_registration is None:
                    raise StorageConfigError(
                        "storage_identity_mismatch", "configured storage identity differs from the registered backend or namespace"
                    )
            else:
                return
        if migration_registration is not None:
            expected["migration_registration"] = migration_registration
        elif recovery_registration is not None:
            expected["recovery_registration"] = recovery_registration
        elif self._has_local_objects(root, scope=scope):
            raise StorageConfigError(
                "storage_migration_required",
                "non-empty local object storage cannot be switched to S3 without an explicit migration",
            )
        encoded = (json.dumps(expected, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".storage-identity-", dir=root)
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            if recovery_registration is not None and identity_path.exists():
                os.replace(temporary, identity_path)
                temporary = None
            else:
                try:
                    os.link(temporary, identity_path)
                except FileExistsError:
                    self._register_identity(root, scope=scope, public_root=public_root,
                                            migration_registration=migration_registration,
                                            recovery_registration=recovery_registration)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _has_local_objects(root: Path, *, scope: str) -> bool:
        stores = [root] if scope == "public" else [path for path in root.iterdir() if path.is_dir() or path.is_symlink()]
        for store in stores:
            objects = store / "objects"
            if store.is_symlink() or objects.is_symlink() or (objects.exists() and (
                    not objects.is_dir() or any(objects.iterdir()))):
                return True
            # Missing raw bytes are corruption, not an empty store that may be silently switched.
            for filename, table in (("catalog.sqlite3", "document_versions"),
                                    ("source_acquisitions.sqlite3", "source_versions")):
                catalog = store / filename
                if catalog.is_symlink():
                    return True
                if not catalog.exists():
                    continue
                try:
                    with closing(sqlite3.connect(catalog.as_uri() + "?mode=ro", uri=True)) as db:
                        if db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                            return True
                except sqlite3.Error:
                    return True
        return False


def verify_local_storage_identity(root, *, scope: str) -> None:
    """Reject local startup when a root has already been registered to remote storage."""
    identity_path = Path(root).expanduser().resolve() / _IDENTITY_NAME
    if identity_path.exists() or identity_path.is_symlink():
        raise StorageConfigError(
            "storage_identity_mismatch", f"{scope} storage is registered to a configured backend; --storage-config is required"
        )
