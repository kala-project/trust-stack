"""Read-only API-shaped access to the deterministic evidence database."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .data_contracts import EvidenceRef, SourceVersion, TableTextSpanLocator, TextSpanLocator
from .repository import PRODUCER, SCHEMA_VERSION

MODE = "deterministic_evidence_preview"
GENERAL_LIMITATIONS = [
    "Deterministic rules example, not Agent or LLM research; Wiki drafts are unreviewed.",
    "Sources are for local evaluation; no public redistribution permission is recorded.",
    "Coverage is limited to loaded material and does not imply that all disclosures or external data were checked.",
    "Pre-rebuild databases are archived by content hash, but no historical query API is available.",
]


class EvidenceService:
    """Return dictionaries matching the vertical-slice API contract."""

    def __init__(self, db_path) -> None:
        self.db_path = Path(db_path).resolve()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    @staticmethod
    def _begin_read(connection: sqlite3.Connection) -> None:
        """Pin subsequent reads on this connection to one SQLite snapshot."""
        connection.execute("BEGIN")

    @staticmethod
    def _json(raw: str, label: str, expected_type: type):
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid JSON in {label}") from exc
        if not isinstance(value, expected_type):
            raise RuntimeError(f"invalid JSON shape in {label}")
        return value

    @staticmethod
    def _source(row: sqlite3.Row) -> dict:
        return {
            "source_id": row["source_id"], "title": row["title"], "url": row["url"],
            "published_at": row["published_at"], "collected_at": row["collected_at"],
            "sha256": row["sha256"], "rights_status": row["rights_status"],
            "bytes": row["bytes"],
        }

    @staticmethod
    def _fact(row: sqlite3.Row) -> dict:
        return {key: row[key] for key in (
            "fact_id", "company_id", "metric", "value", "unit", "period_start",
            "period_end", "period_kind", "basis", "evidence_id", "activity_id",
        )}

    @staticmethod
    def _claim(row: sqlite3.Row) -> dict:
        result = {key: row[key] for key in (
            "claim_id", "company_id", "text", "kind", "verification_status", "explanation",
        )}
        for key in ("evidence_ids", "fact_ids", "activity_ids", "limitations"):
            result[key] = json.loads(row[key + "_json"])
        return result

    @staticmethod
    def _activity(row: sqlite3.Row) -> dict:
        result = {key: row[key] for key in ("activity_id", "type", "method", "created_at")}
        for key in ("inputs", "outputs", "parameters"):
            result[key] = json.loads(row[key + "_json"])
        return result

    @staticmethod
    def _evidence(row: sqlite3.Row) -> dict:
        locator = {"type": "text_span", "start": row["start"], "end": row["end"]}
        if row["table_index"] is not None:
            locator["table_index"] = row["table_index"]
        if row["row_index"] is not None:
            locator["row_index"] = row["row_index"]
        return {
            "evidence_id": row["evidence_id"], "source_id": row["source_id"],
            "quote": row["quote"], "locator": locator,
            "source_sha256": row["source_sha256"],
        }

    @staticmethod
    def _company_or_keyerror(connection: sqlite3.Connection, company_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM companies WHERE company_id=?", (company_id,)).fetchone()
        if row is None:
            raise KeyError(company_id)
        return row

    def overview(self) -> dict:
        with closing(self._connect()) as connection:
            companies = [dict(row) for row in connection.execute(
                "SELECT company_id AS id,name,ticker,description FROM companies ORDER BY company_id"
            )]
            sources = [self._source(row) for row in connection.execute("SELECT * FROM sources ORDER BY source_id")]
            stats = {
                "sources": connection.execute("SELECT count(*) FROM sources").fetchone()[0],
                "facts": connection.execute("SELECT count(*) FROM facts").fetchone()[0],
                "claims": connection.execute("SELECT count(*) FROM claims").fetchone()[0],
            }
        limitations = list(GENERAL_LIMITATIONS)
        if any(item["published_at"] is None for item in sources):
            limitations.append("Some sources have no reliable publication date; the value remains null and is not inferred from the fiscal period or retrieval time.")
        return {
            "mode": MODE, "companies": companies, "sources": sources, "stats": stats,
            "limitations": limitations,
            "agent": {"enabled": False, "reason": "This slice is rules-based and non-LLM."},
        }

    def entity_catalog(self) -> list[dict]:
        """Registry identities only; membership does not establish data coverage."""
        aliases = self._json(
            (Path(__file__).resolve().parent.parent / "config" / "entity-aliases.json").read_text(),
            "configured entity aliases", dict)
        for identifier, item in aliases.items():
            if (not isinstance(identifier, str) or not isinstance(item, dict)
                    or set(item) != {"name", "aliases"} or not isinstance(item["name"], str)
                    or not isinstance(item["aliases"], list)
                    or any(not isinstance(value, str) or not value.strip() for value in item["aliases"])):
                raise RuntimeError("invalid configured entity alias record")
        with closing(self._connect()) as connection:
            result = []
            for row in connection.execute("SELECT company_id,name,ticker FROM companies ORDER BY company_id"):
                configured = aliases.get(row["company_id"])
                if configured is not None and configured["name"] != row["name"]:
                    raise RuntimeError("configured entity identity differs from the data registry")
                result.append({"candidate_id": row["company_id"], "name": row["name"],
                               "aliases": list(dict.fromkeys([
                                   row["company_id"], row["ticker"], row["name"],
                                   *(configured["aliases"] if configured else [])]))})
            return result

    def validate_store(self) -> dict:
        """Fully scan local structure and links; this does not verify source truth."""
        expected_columns = {
            "metadata": ("key", "value"),
            "companies": ("company_id", "name", "ticker", "description"),
            "sources": ("source_id", "source_version_id", "title", "url", "published_at",
                        "published_at_basis", "collected_at", "sha256", "rights_status", "bytes",
                        "parser_version", "text", "tables_json"),
            "activities": ("activity_id", "type", "method", "inputs_json", "outputs_json",
                           "parameters_json", "created_at"),
            "evidence": ("evidence_id", "source_id", "quote", "start", "end", "table_index",
                         "row_index", "source_sha256", "parser_version"),
            "facts": ("fact_id", "company_id", "metric", "value", "unit", "period_start",
                      "period_end", "period_kind", "basis", "evidence_id", "activity_id"),
            "claims": ("claim_id", "company_id", "text", "kind", "verification_status",
                       "explanation", "evidence_ids_json", "fact_ids_json", "activity_ids_json",
                       "limitations_json"),
        }
        try:
            with closing(self._connect()) as connection:
                self._begin_read(connection)
                integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
                if integrity != ["ok"]:
                    raise RuntimeError(f"SQLite integrity check failed: {'; '.join(integrity)}")
                if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise RuntimeError("SQLite foreign key check failed")

                tables = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )}
                if tables != set(expected_columns):
                    raise RuntimeError("unexpected evidence store tables")
                for table, expected in expected_columns.items():
                    actual = tuple(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))
                    if actual != expected:
                        raise RuntimeError(f"unexpected schema for {table}")

                metadata = dict(connection.execute("SELECT key,value FROM metadata"))
                if metadata.get("producer") != PRODUCER or metadata.get("schema_version") != SCHEMA_VERSION:
                    raise RuntimeError("unknown evidence store producer/schema")
                required_metadata = {"producer", "schema_version", "build_version", "generated_at",
                                     "catalog_schema_version", "input_hashes"}
                if set(metadata) != required_metadata:
                    raise RuntimeError("unexpected evidence store metadata")
                input_hashes = self._json(metadata["input_hashes"], "metadata.input_hashes", dict)

                source_rows = list(connection.execute("SELECT * FROM sources"))
                sources = {row["source_id"]: row for row in source_rows}
                source_tables = {}
                digest_pattern = re.compile(r"[0-9a-f]{64}")
                for row in source_rows:
                    source_id, digest = row["source_id"], row["sha256"]
                    if not isinstance(digest, str) or digest_pattern.fullmatch(digest) is None:
                        raise RuntimeError(f"invalid source digest: {source_id}")
                    source_version = "srcv-" + hashlib.sha256(
                        f"{source_id}\x1f{digest}".encode()
                    ).hexdigest()[:20]
                    if row["source_version_id"] != source_version or row["bytes"] < 0:
                        raise RuntimeError(f"invalid source identity: {source_id}")
                    tables_json = self._json(row["tables_json"], f"source {source_id} tables", list)
                    indexed = {}
                    for table in tables_json:
                        if not isinstance(table, dict) or set(table) != {"index", "rows", "start", "end"}:
                            raise RuntimeError(f"invalid table metadata: {source_id}")
                        if not isinstance(table["index"], int) or not isinstance(table["rows"], list):
                            raise RuntimeError(f"invalid table metadata: {source_id}")
                        start, end = table["start"], table["end"]
                        if (not isinstance(start, int) or not isinstance(end, int) or start < 0
                                or end < start or end > len(row["text"]) or table["index"] in indexed):
                            raise RuntimeError(f"invalid table range: {source_id}")
                        if any(not isinstance(item, list) or any(not isinstance(cell, str) for cell in item)
                               for item in table["rows"]):
                            raise RuntimeError(f"invalid table rows: {source_id}")
                        indexed[table["index"]] = table
                    source_tables[source_id] = indexed
                if input_hashes != {key: row["sha256"] for key, row in sources.items()}:
                    raise RuntimeError("metadata input hashes do not match sources")

                evidence_rows = list(connection.execute("SELECT * FROM evidence"))
                evidence_ids = {row["evidence_id"] for row in evidence_rows}
                for row in evidence_rows:
                    source = sources.get(row["source_id"])
                    if source is None:
                        raise RuntimeError(f"missing evidence source: {row['evidence_id']}")
                    if (row["source_sha256"] != source["sha256"]
                            or row["parser_version"] != source["parser_version"]):
                        raise RuntimeError(f"evidence source version mismatch: {row['evidence_id']}")
                    start, end = row["start"], row["end"]
                    if (start < 0 or end < start or end > len(source["text"])
                            or row["quote"] != source["text"][start:end]):
                        raise RuntimeError(f"evidence quote/range mismatch: {row['evidence_id']}")
                    table_index, row_index = row["table_index"], row["row_index"]
                    if table_index is None:
                        if row_index is not None:
                            raise RuntimeError(f"evidence row has no table: {row['evidence_id']}")
                    else:
                        table = source_tables[row["source_id"]].get(table_index)
                        if table is None or start < table["start"] or end > table["end"]:
                            raise RuntimeError(f"evidence table range mismatch: {row['evidence_id']}")
                        if row_index is not None and (row_index < 0 or row_index >= len(table["rows"])):
                            raise RuntimeError(f"evidence table row mismatch: {row['evidence_id']}")

                company_ids = {row[0] for row in connection.execute("SELECT company_id FROM companies")}
                fact_rows = list(connection.execute("SELECT * FROM facts"))
                fact_ids = {row["fact_id"] for row in fact_rows}
                activity_rows = list(connection.execute("SELECT * FROM activities"))
                activity_ids = {row["activity_id"] for row in activity_rows}
                for row in fact_rows:
                    if (row["company_id"] not in company_ids or row["evidence_id"] not in evidence_ids
                            or (row["activity_id"] is not None and row["activity_id"] not in activity_ids)):
                        raise RuntimeError(f"missing fact reference: {row['fact_id']}")
                claim_ids = {row[0] for row in connection.execute("SELECT claim_id FROM claims")}
                source_version_ids = {row["source_version_id"] for row in source_rows}
                reference_ids = (set(sources) | source_version_ids | evidence_ids | fact_ids
                                 | activity_ids | claim_ids)
                id_prefixes = ("srcv-", "ev-", "fact-", "act-", "claim-")

                def validate_nested_references(value, label):
                    if isinstance(value, dict):
                        for key, item in value.items():
                            validate_nested_references(item, f"{label}.{key}")
                    elif isinstance(value, list):
                        for item in value:
                            validate_nested_references(item, label)
                    elif isinstance(value, str) and value.startswith(id_prefixes) and value not in reference_ids:
                        raise RuntimeError(f"missing activity reference in {label}: {value}")

                for row in activity_rows:
                    inputs = self._json(row["inputs_json"], f"activity {row['activity_id']} inputs", list)
                    outputs = self._json(row["outputs_json"], f"activity {row['activity_id']} outputs", list)
                    parameters = self._json(
                        row["parameters_json"], f"activity {row['activity_id']} parameters", dict
                    )
                    if any(not isinstance(item, str) or item not in reference_ids for item in inputs):
                        raise RuntimeError(f"missing activity input reference: {row['activity_id']}")
                    validate_nested_references(outputs, f"activity {row['activity_id']} outputs")
                    validate_nested_references(parameters, f"activity {row['activity_id']} parameters")

                for row in connection.execute("SELECT * FROM claims"):
                    if row["company_id"] not in company_ids:
                        raise RuntimeError(f"missing claim company reference: {row['claim_id']}")
                    for field, available in (("evidence_ids", evidence_ids), ("fact_ids", fact_ids),
                                             ("activity_ids", activity_ids)):
                        values = self._json(
                            row[field + "_json"], f"claim {row['claim_id']} {field}", list
                        )
                        if any(not isinstance(value, str) or value not in available for value in values):
                            raise RuntimeError(f"missing claim reference: {row['claim_id']} {field}")
                    limitations = self._json(
                        row["limitations_json"], f"claim {row['claim_id']} limitations", list
                    )
                    if any(not isinstance(value, str) for value in limitations):
                        raise RuntimeError(f"invalid claim limitations: {row['claim_id']}")
        except (sqlite3.Error, OSError) as exc:
            raise RuntimeError(f"evidence store validation failed: {exc}") from exc
        return {"status": "ok", "scope": "local_structure_and_internal_consistency",
                "source_truthfulness_verified": False}

    def company(self, company_id: str) -> dict:
        with closing(self._connect()) as connection:
            company = self._company_or_keyerror(connection, company_id)
            claims = [self._claim(row) for row in connection.execute(
                "SELECT * FROM claims WHERE company_id=? ORDER BY claim_id", (company_id,)
            )]
            facts = [self._fact(row) for row in connection.execute(
                "SELECT * FROM facts WHERE company_id=? ORDER BY period_end,metric,fact_id", (company_id,)
            )]
        company_dict = {key: company[key] for key in ("company_id", "name", "ticker")}
        company_dict["id"] = company_dict.pop("company_id")
        sections = [
            {
                "heading": "Rules-based evidence summary",
                "body": "Generated from validated sources by versioned rules; not LLM-generated and not yet reviewed by a person.",
                "claim_ids": [claim["claim_id"] for claim in claims],
            },
            {
                "heading": "Limitations",
                "body": "Segment presentation bases are not combined; source_reported does not mean PDF or independent calculation verification.",
                "claim_ids": [],
            },
        ]
        return {
            "company": company_dict, "claims": claims, "facts": facts,
            "wiki": {
                "version": "rules-wiki/1.0.0", "title": f"{company['name']} evidence draft",
                "status": "draft", "mode": "rules_based_draft", "sections": sections,
            },
            "updates": [{"title": "Deterministic draft", "body": "Not LLM-generated and not reviewed.", "severity": "info"}],
            "limitations": list(GENERAL_LIMITATIONS),
        }

    def _evidence_with_connection(self, connection: sqlite3.Connection, claim_id: str) -> dict:
        """Build one evidence chain without changing the caller's read snapshot."""
        claim_row = connection.execute("SELECT * FROM claims WHERE claim_id=?", (claim_id,)).fetchone()
        if claim_row is None:
            raise KeyError(claim_id)
        claim = self._claim(claim_row)
        facts = []
        for fact_id in claim["fact_ids"]:
            row = connection.execute("SELECT * FROM facts WHERE fact_id=?", (fact_id,)).fetchone()
            if row is not None:
                facts.append(self._fact(row))
        activity_ids = list(dict.fromkeys(
            claim["activity_ids"] + [fact["activity_id"] for fact in facts if fact["activity_id"]]
        ))
        activities = []
        for activity_id in activity_ids:
            row = connection.execute("SELECT * FROM activities WHERE activity_id=?", (activity_id,)).fetchone()
            if row is not None:
                activities.append(self._activity(row))
        evidence_ids = list(dict.fromkeys(
            claim["evidence_ids"] + [fact["evidence_id"] for fact in facts]
        ))
        evidence = []
        source_ids = []
        for evidence_id in evidence_ids:
            row = connection.execute("SELECT * FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone()
            if row is not None:
                item = self._evidence(row)
                evidence.append(item)
                source_ids.append(item["source_id"])
        sources = []
        for source_id in dict.fromkeys(source_ids):
            row = connection.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if row is not None:
                sources.append(self._source(row))
        return {"claim": claim, "facts": facts, "activities": activities, "sources": sources, "evidence": evidence}

    def evidence(self, claim_id: str) -> dict:
        with closing(self._connect()) as connection:
            self._begin_read(connection)
            return self._evidence_with_connection(connection, claim_id)

    def evidence_reference(self, evidence_id: str) -> dict:
        """Return one legacy SQL evidence row plus its strict shared reference."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT e.*,s.source_version_id,s.bytes FROM evidence e "
                "JOIN sources s USING(source_id) WHERE e.evidence_id=?", (evidence_id,)
            ).fetchone()
            if row is None:
                raise KeyError(evidence_id)
            locator = (TableTextSpanLocator(
                "table_text_span", row["start"], row["end"],
                row["table_index"], row["row_index"])
                if row["table_index"] is not None else
                TextSpanLocator("text_span", row["start"], row["end"]))
            reference = EvidenceRef(
                SourceVersion(row["source_id"], row["source_version_id"], None,
                              row["source_sha256"], row["bytes"], "public"),
                locator, "parsed", row["parser_version"],
            )
            return {"evidence": self._evidence(row), "evidence_ref": reference.to_json()}

    def source(self, source_id: str) -> dict:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM sources WHERE source_id=?", (source_id,)).fetchone()
            if row is None:
                raise KeyError(source_id)
            warning = None
            if row["published_at"] is None:
                warning = "No reliable publication date was identified; the value remains null and is not inferred from the fiscal period or retrieval time."
            return {
                "source": self._source(row), "text": row["text"],
                "tables": json.loads(row["tables_json"]),
                "parser_version": row["parser_version"], "warning": warning,
            }

    def report(self, company_id: str) -> dict:
        """Return a frozen JSON snapshot manifest; never bundles full source text."""
        with closing(self._connect()) as connection:
            self._begin_read(connection)
            company = self._company_or_keyerror(connection, company_id)
            metadata = dict(connection.execute("SELECT key,value FROM metadata"))
            claims = [self._claim(row) for row in connection.execute(
                "SELECT * FROM claims WHERE company_id=? ORDER BY claim_id", (company_id,)
            )]
            facts = [self._fact(row) for row in connection.execute(
                "SELECT * FROM facts WHERE company_id=? ORDER BY fact_id", (company_id,)
            )]
            payloads = [
                self._evidence_with_connection(connection, claim["claim_id"]) for claim in claims
            ]
            # Facts are independently usable research inputs, not only claim children.
            # Capture their lineage in this same read transaction, including facts
            # for which no pre-authored claim exists.
            fact_evidence = [self._evidence(row) for row in connection.execute(
                "SELECT DISTINCT e.* FROM evidence e JOIN facts f USING(evidence_id) WHERE f.company_id=?", (company_id,)
            )]
            fact_sources = [self._source(row) for row in connection.execute(
                "SELECT DISTINCT s.* FROM sources s JOIN evidence e USING(source_id) "
                "JOIN facts f USING(evidence_id) WHERE f.company_id=?", (company_id,)
            )]
            fact_activities = [self._activity(row) for row in connection.execute(
                "SELECT DISTINCT a.* FROM activities a JOIN facts f USING(activity_id) WHERE f.company_id=?", (company_id,)
            )]
        sources = {
            source["source_id"]: source for payload in payloads for source in payload["sources"]
        }
        evidence = {
            item["evidence_id"]: item for payload in payloads for item in payload["evidence"]
        }
        activities = {
            item["activity_id"]: item for payload in payloads for item in payload["activities"]
        }
        sources.update({item["source_id"]: item for item in fact_sources})
        evidence.update({item["evidence_id"]: item for item in fact_evidence})
        activities.update({item["activity_id"]: item for item in fact_activities})
        evidence_chains = [{
            "claim_id": payload["claim"]["claim_id"],
            "fact_ids": [item["fact_id"] for item in payload["facts"]],
            "evidence_ids": [item["evidence_id"] for item in payload["evidence"]],
            "activity_ids": [item["activity_id"] for item in payload["activities"]],
            "source_ids": [item["source_id"] for item in payload["sources"]],
        } for payload in payloads]
        return {
            "company": {"id": company["company_id"], "name": company["name"], "ticker": company["ticker"]},
            "generated_at": metadata["generated_at"], "status": "unreviewed",
            "mode": MODE, "input_hashes": json.loads(metadata["input_hashes"]),
            "manifest": {
                "schema_version": metadata["schema_version"],
                "build_version": metadata["build_version"],
                "source_text_included": False, "public_redistribution": False,
                "snapshot_is_approval": False,
            },
            "limitations": list(GENERAL_LIMITATIONS) + [
                "This report is a single database-read snapshot, not an approval or publication.",
                "Full source text is not included; public redistribution has not been approved.",
            ],
            "claims": claims, "facts": facts,
            "sources": [sources[key] for key in sorted(sources)],
            "evidence": [evidence[key] for key in sorted(evidence)],
            "activities": [activities[key] for key in sorted(activities)],
            "evidence_chains": evidence_chains,
        }
