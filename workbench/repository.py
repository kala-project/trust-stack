"""SQLite schema and small write helpers for deterministic evidence artifacts."""

import json
import sqlite3
from pathlib import Path

SCHEMA_VERSION = "evidence-slice/1.1.0"
LEGACY_SCHEMA_VERSIONS = {"evidence-slice/1.0.0"}
PRODUCER = "workbench.build"

SCHEMA_SQL = """
PRAGMA foreign_keys=ON;
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE companies (
    company_id TEXT PRIMARY KEY, name TEXT NOT NULL, ticker TEXT NOT NULL,
    description TEXT NOT NULL
);
CREATE TABLE sources (
    source_id TEXT PRIMARY KEY, source_version_id TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL, url TEXT NOT NULL, published_at TEXT,
    published_at_basis TEXT NOT NULL, collected_at TEXT NOT NULL,
    sha256 TEXT NOT NULL, rights_status TEXT NOT NULL, bytes INTEGER NOT NULL,
    parser_version TEXT NOT NULL, text TEXT NOT NULL, tables_json TEXT NOT NULL
);
CREATE TABLE activities (
    activity_id TEXT PRIMARY KEY, type TEXT NOT NULL, method TEXT NOT NULL,
    inputs_json TEXT NOT NULL, outputs_json TEXT NOT NULL,
    parameters_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE evidence (
    evidence_id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(source_id),
    quote TEXT NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
    table_index INTEGER, row_index INTEGER, source_sha256 TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    CHECK (start >= 0 AND end >= start)
);
CREATE TABLE facts (
    fact_id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(company_id),
    metric TEXT NOT NULL, value TEXT NOT NULL, unit TEXT NOT NULL,
    period_start TEXT, period_end TEXT NOT NULL,
    period_kind TEXT NOT NULL CHECK(period_kind IN ('quarter','year')),
    basis TEXT NOT NULL, evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    activity_id TEXT REFERENCES activities(activity_id)
);
CREATE TABLE claims (
    claim_id TEXT PRIMARY KEY, company_id TEXT NOT NULL REFERENCES companies(company_id),
    text TEXT NOT NULL, kind TEXT NOT NULL,
    verification_status TEXT NOT NULL, explanation TEXT NOT NULL,
    evidence_ids_json TEXT NOT NULL, fact_ids_json TEXT NOT NULL,
    activity_ids_json TEXT NOT NULL, limitations_json TEXT NOT NULL
);
CREATE INDEX facts_company_idx ON facts(company_id);
CREATE INDEX claims_company_idx ON claims(company_id);
CREATE INDEX evidence_source_idx ON evidence(source_id);
"""


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def initialize(connection: sqlite3.Connection, metadata: dict[str, str]) -> None:
    connection.executescript(SCHEMA_SQL)
    connection.executemany("INSERT INTO metadata(key,value) VALUES(?,?)", sorted(metadata.items()))


def inspect_existing(path: Path) -> None:
    """Accept only this producer's current or explicitly supported prior schema."""
    if not path.exists():
        return
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = dict(connection.execute("SELECT key,value FROM metadata"))
    except (sqlite3.Error, OSError) as exc:
        raise RuntimeError("existing evidence database is unknown; refusing to overwrite") from exc
    finally:
        if "connection" in locals():
            connection.close()
    known_schemas = {SCHEMA_VERSION, *LEGACY_SCHEMA_VERSIONS}
    if rows.get("schema_version") not in known_schemas or rows.get("producer") != PRODUCER:
        raise RuntimeError("existing evidence database has an unknown schema/producer; refusing to overwrite")


def insert_source(connection: sqlite3.Connection, source: dict) -> None:
    connection.execute(
        """INSERT INTO sources VALUES
        (:source_id,:source_version_id,:title,:url,:published_at,:published_at_basis,
         :collected_at,:sha256,:rights_status,:bytes,:parser_version,:text,:tables_json)""",
        source,
    )


def insert_activity(connection: sqlite3.Connection, activity: dict) -> None:
    row = dict(activity)
    for key in ("inputs", "outputs", "parameters"):
        row[key + "_json"] = json_text(row.pop(key))
    connection.execute(
        "INSERT INTO activities VALUES(:activity_id,:type,:method,:inputs_json,:outputs_json,:parameters_json,:created_at)",
        row,
    )


def insert_evidence(connection: sqlite3.Connection, evidence: dict) -> None:
    locator = evidence["locator"]
    connection.execute(
        "INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?)",
        (
            evidence["evidence_id"], evidence["source_id"], evidence["quote"],
            locator["start"], locator["end"], locator.get("table_index"),
            locator.get("row_index"), evidence["source_sha256"],
            evidence["parser_version"],
        ),
    )


def insert_fact(connection: sqlite3.Connection, fact: dict) -> None:
    connection.execute(
        """INSERT INTO facts VALUES
        (:fact_id,:company_id,:metric,:value,:unit,:period_start,:period_end,
         :period_kind,:basis,:evidence_id,:activity_id)""",
        fact,
    )


def insert_claim(connection: sqlite3.Connection, claim: dict) -> None:
    row = dict(claim)
    for key in ("evidence_ids", "fact_ids", "activity_ids", "limitations"):
        row[key + "_json"] = json_text(row.pop(key))
    connection.execute(
        """INSERT INTO claims VALUES
        (:claim_id,:company_id,:text,:kind,:verification_status,:explanation,
         :evidence_ids_json,:fact_ids_json,:activity_ids_json,:limitations_json)""",
        row,
    )
