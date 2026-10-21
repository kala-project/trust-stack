"""Shared, validated application configuration; no HTTP or service construction.

Public contract: resolve RuntimePaths before any writable service starts, then
pass a RuntimeConfig to the composition root. Neither operation creates files,
loads credentials, starts a provider, nor binds a port. Paths remain internal to
application assembly and are never a client or model tool contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from .storage_config import StorageConfiguration
from .verification_executor import VerificationExecutionConfig

__all__ = ["RuntimePaths", "RuntimeConfig"]

_ROOT = Path(__file__).resolve().parent.parent


def _distinct_databases(paths: dict[str, Path]) -> None:
    items = list(paths.items())
    for index, (first_name, first) in enumerate(items):
        for second_name, second in items[index + 1:]:
            alias = first == second
            if not alias:
                try:
                    alias = first.exists() and second.exists() and os.path.samefile(first, second)
                except OSError:
                    alias = False
            if alias:
                raise RuntimeError(f"{first_name} and {second_name} databases must be distinct")


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved data identities shared by API and worker, including unused stores.

    Even a worker must reject aliases with wiki/sample/auth when those paths are
    supplied: not opening a database in this process does not make aliasing safe.
    Optional document/native services are opened only if their registry exists.
    """

    evidence: Path
    chat: Path
    wiki: Path
    sample: Path
    documents_root: Path
    native_registry: Path
    auth: Path | None = None

    def __post_init__(self) -> None:
        for name in ("evidence", "chat", "wiki", "sample", "documents_root", "native_registry", "auth"):
            value = getattr(self, name)
            if value is None and name == "auth":
                continue
            if not isinstance(value, (str, Path)) or not str(value).strip():
                raise ValueError(f"{name} must be a nonempty filesystem path")
            object.__setattr__(self, name, Path(value).resolve())
        databases = {
            "evidence": self.evidence, "chat": self.chat, "wiki": self.wiki,
            "sample": self.sample, "documents_catalog": self.documents_catalog,
            "native_registry": self.native_registry,
        }
        if self.auth is not None:
            databases["auth"] = self.auth
        _distinct_databases(databases)

    @classmethod
    def resolve(cls, db_path=None, *, chat_db=None, wiki_db=None, sample_db=None,
                documents_root=None, auth_path=None) -> RuntimePaths:
        """Apply the existing application defaults and reject aliases without writes."""
        if db_path is not None and (not isinstance(db_path, (str, Path)) or not str(db_path).strip()):
            raise ValueError("db_path must be a nonempty filesystem path")
        evidence = Path(db_path) if db_path is not None else _ROOT / "var/evidence.sqlite3"
        return cls(
            evidence=evidence,
            chat=chat_db if chat_db is not None else evidence.with_name("chat.sqlite3"),
            wiki=wiki_db if wiki_db is not None else evidence.with_name("wiki.sqlite3"),
            sample=sample_db if sample_db is not None else evidence.with_name("sample_research.sqlite3"),
            documents_root=documents_root if documents_root is not None else evidence.with_name("documents"),
            native_registry=evidence.with_name("native-data") / "registry.sqlite",
            auth=auth_path,
        )

    @property
    def documents_catalog(self) -> Path:
        return self.documents_root / "catalog.sqlite3"

    @property
    def materials_root(self) -> Path:
        return self.chat.with_name(self.chat.name + ".materials")


@dataclass(frozen=True)
class RuntimeConfig:
    """Validated common configuration; HTTP transport options stay in the API.

    Storage accepts the existing strict JSON path or a prevalidated configuration
    (used by controlled integrations). It is excluded from repr to avoid exposing
    credential-bearing implementation details. Construction itself is read-only.
    """

    paths: RuntimePaths
    verification_config: VerificationExecutionConfig | None = None
    enable_public_acquisition: bool = False
    storage_config: StorageConfiguration | str | Path | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.paths, RuntimePaths):
            raise TypeError("paths must be validated RuntimePaths")
        if (self.verification_config is not None
                and not isinstance(self.verification_config, VerificationExecutionConfig)):
            raise TypeError("verification_config must be VerificationExecutionConfig")
        if type(self.enable_public_acquisition) is not bool:
            raise ValueError("enable_public_acquisition must be a boolean")
        if self.storage_config is not None and not isinstance(
                self.storage_config, (StorageConfiguration, str, Path)):
            raise TypeError("storage_config must be a configuration path or StorageConfiguration")
        if isinstance(self.storage_config, str) and not self.storage_config.strip():
            raise ValueError("storage_config path must not be empty")
