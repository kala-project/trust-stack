#!/usr/bin/env python3
"""Append configured issuer identities to an existing evidence store; never add facts."""
from __future__ import annotations
import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3


def register(db_path: Path, config_path: Path) -> list[str]:
    configured = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(configured, dict) or not configured:
        raise ValueError("entity configuration must be a nonempty object")
    for identifier, record in configured.items():
        if (not isinstance(identifier, str) or not re.fullmatch(r"[A-Z][A-Z0-9._:-]{0,99}", identifier)
                or not isinstance(record, dict) or set(record) != {"name", "aliases"}
                or not isinstance(record["name"], str) or not record["name"].strip()
                or not isinstance(record["aliases"], list)
                or any(not isinstance(a, str) or not a.strip() for a in record["aliases"])):
            raise ValueError("invalid configured entity identity")
    added = []
    # mode=rw deliberately refuses a mistyped/nonexistent store.
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=rw", uri=True)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        for identifier, record in configured.items():
            old = db.execute("SELECT name,ticker FROM companies WHERE company_id=?", (identifier,)).fetchone()
            if old is not None:
                if old != (record["name"], identifier):
                    raise ValueError(f"existing identity mismatch: {identifier}")
                continue
            db.execute("INSERT INTO companies(company_id,name,ticker,description) VALUES(?,?,?,?)",
                       (identifier, record["name"], identifier,
                        "Configured issuer identity only; document and numerical coverage must be checked separately."))
            added.append(identifier)
    return added


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps({"added": register(args.db, args.config)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
