"""Create the minimal evidence registry required by a clean public checkout."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from .repository import PRODUCER, SCHEMA_VERSION, initialize

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "var" / "evidence.sqlite3"
COMPANIES = (
    ("MSFT", "Microsoft Corporation", "MSFT", "Public research entity registry entry."),
    ("NVDA", "NVIDIA Corporation", "NVDA", "Public research entity registry entry."),
    ("CRM", "Salesforce, Inc.", "CRM", "Public research entity registry entry."),
)


def initialize_public_database(path: Path = DEFAULT_DB) -> Path:
    """Atomically create an empty, valid evidence store without bundled source files."""
    path = Path(path).resolve()
    if path.exists():
        raise RuntimeError(f"database already exists; refusing to replace it: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp = Path(temporary)
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        connection = sqlite3.connect(temp)
        try:
            with connection:
                initialize(connection, {
                    "schema_version": SCHEMA_VERSION,
                    "producer": PRODUCER,
                    "build_version": "public-empty/1",
                    "generated_at": generated_at,
                    "catalog_schema_version": "public-registry/1",
                    "input_hashes": json.dumps({}, separators=(",", ":")),
                })
                connection.executemany("INSERT INTO companies VALUES(?,?,?,?)", COMPANIES)
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("SQLite integrity check failed")
        finally:
            connection.close()
        os.replace(temp, path)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return path


def main() -> int:
    try:
        path = initialize_public_database()
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        print(f"initialization failed: {exc}")
        return 1
    print(f"initialized {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
