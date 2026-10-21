"""Deterministic custom-sample comparisons over one frozen evidence snapshot.

This service never calls a model and never mutates the evidence database.  Each
saved result is an immutable JSON snapshot in a distinct SQLite database.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
import hashlib
import json
import os
import tempfile
from pathlib import Path
import re
import sqlite3
from typing import Any

SCHEMA_VERSION = "sample-research/1.0"
PRODUCER = "workbench.sample_research"
ALGORITHM_VERSION = "strict-comparability/1.1"
METRICS = {"revenue": "Revenue", "operating_income": "Operating income"}
PERIOD_KINDS = {"quarter": "Quarter", "year": "Year"}
MONETARY_UNITS = {"USD millions", "USD billions"}
MAX_DECIMAL_DIGITS = 1000
MAX_DECIMAL_EXPONENT = 1000
_ID = re.compile(r"sample-[0-9a-f]{24}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


class SampleResearchError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code, self.message, self.status_code = code, message, status_code

    def as_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _iso_date(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value) != 10:
        raise SampleResearchError("invalid_scope", f"{field} must be a real date in YYYY-MM-DD format")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise SampleResearchError("invalid_scope", f"{field} must be a valid real date") from exc
    if parsed.isoformat() != value:
        raise SampleResearchError("invalid_scope", f"{field} must be a real date in YYYY-MM-DD format")
    return value


class SampleResearchService:
    def __init__(self, evidence_db: Path | str, result_db: Path | str, *, document_service=None) -> None:
        self.document_service = document_service
        self.evidence_db = Path(evidence_db).resolve()
        self.result_db = Path(result_db).resolve()
        if self.evidence_db == self.result_db:
            raise RuntimeError("sample result database must be distinct from evidence")
        self.result_db.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _evidence(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"file:{self.evidence_db}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def _results(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.result_db, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialize(self) -> None:
        # Publish only a complete database. Opening the destination first exposes
        # an empty file before BEGIN and lets a concurrent initializer reject it.
        if not self.result_db.exists():
            fd, temporary = tempfile.mkstemp(prefix='.sample-init-', dir=self.result_db.parent)
            os.close(fd)
            try:
                with closing(sqlite3.connect(temporary)) as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    connection.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                    connection.execute('''CREATE TABLE result_versions (
                        result_id TEXT NOT NULL,
                        version INTEGER NOT NULL CHECK(version > 0),
                        content_hash TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        result_json TEXT NOT NULL,
                        PRIMARY KEY(result_id, version),
                        UNIQUE(result_id, content_hash)
                    )''')
                    connection.executemany('INSERT INTO metadata(key,value) VALUES(?,?)', [
                        ('producer', PRODUCER), ('schema_version', SCHEMA_VERSION)])
                    connection.commit()
                try:
                    os.link(temporary, self.result_db)
                except FileExistsError:
                    pass  # Another initializer won; validate, never overwrite it.
            finally:
                os.unlink(temporary)
        try:
            with closing(self._results()) as connection:
                metadata = dict(connection.execute('SELECT key,value FROM metadata'))
                columns = tuple(row[1] for row in connection.execute('PRAGMA table_info(result_versions)'))
                expected = ('result_id', 'version', 'content_hash', 'created_at', 'result_json')
                if metadata != {'producer': PRODUCER, 'schema_version': SCHEMA_VERSION} or columns != expected:
                    raise RuntimeError('unknown sample research database; refusing to modify')
        except sqlite3.Error as exc:
            raise RuntimeError('unknown sample research database; refusing to modify') from exc

    @staticmethod
    def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
        return dict(connection.execute("SELECT key,value FROM metadata ORDER BY key"))

    def options(self) -> dict[str, Any]:
        with closing(self._evidence()) as connection:
            connection.execute("BEGIN")
            companies = [dict(row) for row in connection.execute(
                "SELECT company_id AS id,name,ticker FROM companies ORDER BY company_id")]
            rows = connection.execute(
                "SELECT company_id,metric,period_start,period_end,period_kind,unit,basis "
                "FROM facts WHERE metric IN ('revenue','operating_income') "
                "ORDER BY company_id,metric,period_end,fact_id").fetchall()
            metadata = self._metadata(connection)
        availability: dict[str, list[dict[str, Any]]] = {item["id"]: [] for item in companies}
        for row in rows:
            availability[row["company_id"]].append({key: row[key] for key in (
                "metric", "period_start", "period_end", "period_kind", "unit", "basis")})
        return {
            "mode": "custom_sample_not_industry",
            "minimum_members": 2,
            "companies": companies,
            "metrics": [{"id": key, "label": label} for key, label in METRICS.items()],
            "period_kinds": [{"id": key, "label": label} for key, label in PERIOD_KINDS.items()],
            "availability": availability,
            "evidence_snapshot": metadata,
            "notice": "This is a user-defined sample, not an industry proxy; a sample total is calculated only for complete, strictly comparable inputs.",
        }

    def _validate_scope(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict) or set(request) != {
            "company_ids", "metric", "period_kind", "start_date", "end_date"
        }:
            raise SampleResearchError("invalid_scope", "Request fields do not match the controlled sample definition")
        company_ids = request["company_ids"]
        if (not isinstance(company_ids, list) or not 2 <= len(company_ids) <= 20
                or any(not isinstance(item, str) or not item or len(item) > 80 for item in company_ids)
                or len(set(company_ids)) != len(company_ids)):
            raise SampleResearchError("invalid_scope", "Select at least two distinct registered companies")
        metric, period_kind = request["metric"], request["period_kind"]
        if (not isinstance(metric, str) or not isinstance(period_kind, str)
                or metric not in METRICS or period_kind not in PERIOD_KINDS):
            raise SampleResearchError("invalid_scope", "The metric or period basis is not supported")
        start, end = _iso_date(request["start_date"], "start_date"), _iso_date(request["end_date"], "end_date")
        if start > end:
            raise SampleResearchError("invalid_scope", "The start date cannot be later than the end date")
        return {"company_ids": company_ids, "metric": metric, "period_kind": period_kind,
                "start_date": start, "end_date": end,
                "selection_rule": "period_end is within the selected real-date range and period_kind matches exactly"}

    @staticmethod
    def _input(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "fact_id": row["fact_id"], "metric": row["metric"], "value": row["value"],
            "unit": row["unit"], "period_start": row["period_start"],
            "period_end": row["period_end"], "period_kind": row["period_kind"],
            "basis": row["basis"], "evidence": {
                "evidence_id": row["evidence_id"], "quote": row["quote"],
                "locator": {"start": row["start"], "end": row["end"],
                            "table_index": row["table_index"], "row_index": row["row_index"]},
                "source": {"source_id": row["source_id"], "title": row["title"],
                           "url": row["url"], "published_at": row["published_at"],
                           "collected_at": row["collected_at"], "sha256": row["sha256"],
                           "rights_status": row["rights_status"]},
            },
        }

    @staticmethod
    def _semantic_key(item: dict[str, Any]) -> tuple[Any, ...]:
        return tuple(item[key] for key in (
            "value", "unit", "period_start", "period_end", "period_kind", "basis"))

    @staticmethod
    def _verify_evidence_row(row: sqlite3.Row) -> None:
        required = ("evidence_id", "source_id", "quote", "source_sha256", "sha256", "source_text")
        if any(row[key] is None for key in required):
            raise RuntimeError("orphan fact/evidence/source chain in frozen evidence")
        if row["evidence_company_count"] != 1 or row["source_company_count"] != 1:
            raise RuntimeError("cross-company fact/evidence/source chain in frozen evidence")
        if (_SHA256.fullmatch(row["sha256"]) is None
                or row["source_sha256"] != row["sha256"]):
            raise RuntimeError("invalid source sha256 in frozen evidence")
        start, end = row["start"], row["end"]
        if (type(start) is not int or type(end) is not int or start < 0 or end < start
                or end > len(row["source_text"])
                or row["source_text"][start:end] != row["quote"]):
            raise RuntimeError("invalid quote locator in frozen evidence")
        table_index, row_index = row["table_index"], row["row_index"]
        if ((table_index is None) != (row_index is None)
                or table_index is not None
                and (type(table_index) is not int or type(row_index) is not int
                     or table_index < 0 or row_index < 0)):
            raise RuntimeError("invalid table locator in frozen evidence")

    @staticmethod
    def _sum_decimals(items: list[dict[str, Any]]) -> Decimal:
        numbers: list[Decimal] = []
        max_integer_digits = max_fraction_digits = 0
        for item in items:
            raw = item["value"]
            if not isinstance(raw, str) or not raw or len(raw) > MAX_DECIMAL_DIGITS + 20:
                raise RuntimeError("invalid decimal in frozen fact")
            try:
                number = Decimal(raw)
            except InvalidOperation as exc:
                raise RuntimeError("invalid decimal in frozen fact") from exc
            sign, digits, exponent = number.as_tuple()
            if (not number.is_finite() or not isinstance(exponent, int)
                    or len(digits) > MAX_DECIMAL_DIGITS
                    or abs(exponent) > MAX_DECIMAL_EXPONENT
                    or abs(number.adjusted()) > MAX_DECIMAL_EXPONENT):
                raise RuntimeError("invalid or oversized decimal in frozen fact")
            numbers.append(number)
            max_integer_digits = max(max_integer_digits, number.adjusted() + 1, 0)
            max_fraction_digits = max(max_fraction_digits, -exponent, 0)
        # Integer width + common fractional scale + enough carry digits for all addends.
        precision = max(1, max_integer_digits + max_fraction_digits + len(str(len(numbers))) + 1)
        with localcontext() as context:
            context.prec = precision
            context.Emax = MAX_DECIMAL_EXPONENT * 2
            context.Emin = -MAX_DECIMAL_EXPONENT * 2
            return sum(numbers, Decimal(0))

    def _build(self, scope: dict[str, Any]) -> dict[str, Any]:
        placeholders = ",".join("?" for _ in scope["company_ids"])
        with closing(self._evidence()) as connection:
            connection.execute("BEGIN")
            metadata = self._metadata(connection)
            company_rows = connection.execute(
                f"SELECT company_id,name,ticker FROM companies WHERE company_id IN ({placeholders})",
                scope["company_ids"],
            ).fetchall()
            companies = {row["company_id"]: dict(row) for row in company_rows}
            unknown = [item for item in scope["company_ids"] if item not in companies]
            if unknown:
                raise SampleResearchError("unknown_member", "The sample contains an unregistered company; no result was created")
            rows = connection.execute(
                f"SELECT f.*,e.quote,e.start,e.end,e.table_index,e.row_index,e.source_sha256,"
                "s.source_id,s.title,s.url,s.published_at,s.collected_at,s.sha256,s.rights_status,"
                "s.text AS source_text,"
                "(SELECT count(DISTINCT f2.company_id) FROM facts f2 "
                " WHERE f2.evidence_id=f.evidence_id) AS evidence_company_count,"
                "(SELECT count(DISTINCT f3.company_id) FROM facts f3 JOIN evidence e3 "
                " ON e3.evidence_id=f3.evidence_id WHERE e3.source_id=e.source_id) "
                " AS source_company_count "
                "FROM facts f LEFT JOIN evidence e ON e.evidence_id=f.evidence_id "
                "LEFT JOIN sources s ON s.source_id=e.source_id "
                f"WHERE f.company_id IN ({placeholders}) AND f.metric=? AND f.period_kind=? "
                "AND f.period_end>=? AND f.period_end<=? ORDER BY f.company_id,f.period_end,f.fact_id",
                [*scope["company_ids"], scope["metric"], scope["period_kind"],
                 scope["start_date"], scope["end_date"]],
            ).fetchall()

        by_company = {item: [] for item in scope["company_ids"]}
        for row in rows:
            self._verify_evidence_row(row)
            by_company[row["company_id"]].append(self._input(row))
        members, unmatched = [], []
        for company_id in scope["company_ids"]:
            inputs = by_company[company_id]
            groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
            for item in inputs:
                groups.setdefault(self._semantic_key(item), []).append(item)
            if not groups:
                status, reason, selected = "missing", "No frozen fact for this metric exists in the selected range; missing values are not treated as zero.", None
            elif len(groups) > 1:
                status, reason, selected = "ambiguous", "Multiple periods, values, or bases matched; no input was selected arbitrarily.", None
            else:
                selected = inputs[0]
                status, reason = "available", "One semantic fact matched; all equivalent sources are retained below."
            member = {"company": companies[company_id], "status": status, "reason": reason,
                      "selected": selected, "inputs": inputs}
            members.append(member)
            if status != "available":
                unmatched.append({"company_id": company_id, "status": status, "reason": reason})

        selected = [item["selected"] for item in members if item["selected"] is not None]
        blockers = []
        if len(selected) != len(members):
            blockers.append("Not every frozen member has one unique input; missing values must not be treated as zero or removed from the denominator.")
        comparison_fields = ("unit", "period_start", "period_end", "period_kind", "basis")
        for field in comparison_fields:
            values = [item[field] for item in selected]
            if (len(values) == len(members)
                    and (any(not isinstance(value, str) or not value.strip() for value in values)
                         or len(set(values)) != 1)):
                blockers.append(f"Member {field} values are incomplete or inconsistent; implicit conversion or addition is prohibited.")
        if (len(selected) == len(members)
                and any(item["unit"] not in MONETARY_UNITS for item in selected)):
            blockers.append("Member units are not a supported currency unit and must not be added.")
        aggregate: dict[str, Any]
        if blockers:
            aggregate = {"status": "disabled", "value": None, "unit": None,
                         "formula": "sample_total = sum(each frozen member’s unique comparable fact)",
                         "inputs": [], "blockers": blockers}
        else:
            total = self._sum_decimals(selected)
            aggregate = {"status": "computed", "value": format(total, "f"),
                         "unit": selected[0]["unit"],
                         "formula": "sample_total = " + " + ".join(item["value"] for item in selected),
                         "inputs": [item["fact_id"] for item in selected], "blockers": []}

        raw_sources, raw_gaps = [], []
        for member in members:
            company_id = member['company']['company_id']
            needed = {item['evidence']['source']['sha256'] for item in member['inputs']}
            if not needed:
                continue
            versions = []
            if self.document_service is not None:
                try:
                    versions = self.document_service.get_document_coverage(company_id=company_id)['documents']
                except Exception:
                    # Freeze a visible gap; never fetch the publication URL or expose internal errors.
                    versions = []
            for digest in sorted(needed):
                matching = [v for v in versions if v.get('source_sha256') == digest]
                if matching:
                    raw_sources.append({'company_id': company_id, 'source_sha256': digest,
                                        'document_version_id': matching[0]['document_version_id']})
                else:
                    raw_gaps.append({'company_id': company_id, 'source_sha256': digest,
                                     'reason': 'No verifiable original was frozen for this result; a publication-page link is not an original-source version.'})

        request_key = {key: scope[key] for key in (
            "company_ids", "metric", "period_kind", "start_date", "end_date")}
        return {
            "schema_version": SCHEMA_VERSION, "algorithm_version": ALGORITHM_VERSION,
            "kind": "custom_sample_comparison", "claim_boundary": "User-defined sample; not representative of an industry or the industry as a whole",
            "scope": scope, "evidence_snapshot": metadata,
            "frozen_raw_sources": raw_sources, "raw_source_gaps": raw_gaps,
            "frozen_members": [companies[item] for item in scope["company_ids"]],
            "excluded_members": [], "unmatched_members": unmatched, "members": members,
            "coverage": {"requested_members": len(members),
                         "members_with_unique_input": len(selected),
                         "aggregation_denominator": len(members)},
            "aggregate": aggregate,
            "limitations": [
                "Use only facts in the frozen evidence store that match the selected metric, period type, and date range.",
                "Missing operating income remains missing and is never replaced with revenue.",
                "Years and quarters, different periods, units/currencies, or statistical bases are not mixed by default.",
                "Unmatched members remain in the sample definition and denominator.",
            ],
            "request_fingerprint": _digest(request_key),
        }

    @staticmethod
    def _decode_saved(row: sqlite3.Row) -> dict[str, Any]:
        try:
            saved = json.loads(row["result_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("corrupt saved sample result") from exc
        if not isinstance(saved, dict):
            raise RuntimeError("corrupt saved sample result")
        metadata = {
            "result_id": row["result_id"], "version": row["version"],
            "content_hash": row["content_hash"], "created_at": row["created_at"],
        }
        if (any(saved.get(key) != value for key, value in metadata.items())
                or set(metadata) - set(saved)):
            raise RuntimeError("saved sample result metadata mismatch")
        payload = {key: value for key, value in saved.items() if key not in metadata}
        if (_SHA256.fullmatch(row["content_hash"]) is None
                or _digest(payload) != row["content_hash"]):
            raise RuntimeError("saved sample result content hash mismatch")
        if (type(row["version"]) is not int or row["version"] < 1
                or not isinstance(row["created_at"], str)):
            raise RuntimeError("invalid saved sample result metadata")
        try:
            timestamp = datetime.fromisoformat(row["created_at"])
        except ValueError as exc:
            raise RuntimeError("invalid saved sample result timestamp") from exc
        if timestamp.tzinfo is None:
            raise RuntimeError("invalid saved sample result timestamp")
        fingerprint = payload.get("request_fingerprint")
        if (not isinstance(fingerprint, str) or _SHA256.fullmatch(fingerprint) is None
                or row["result_id"] != "sample-" + fingerprint[:24]):
            raise RuntimeError("saved sample result identity mismatch")
        return saved

    def create(self, request: Any) -> dict[str, Any]:
        scope = self._validate_scope(request)
        result = self._build(scope)
        result_id = "sample-" + result["request_fingerprint"][:24]
        content_hash = _digest(result)
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with closing(self._results()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing_rows = connection.execute(
                "SELECT result_id,version,content_hash,created_at,result_json "
                "FROM result_versions WHERE result_id=? ORDER BY version", (result_id,),
            ).fetchall()
            decoded = [(row, self._decode_saved(row)) for row in existing_rows]
            existing = next((saved for row, saved in decoded
                             if row["content_hash"] == content_hash), None)
            if existing is not None:
                connection.commit()
                return existing
            version = (existing_rows[-1]["version"] + 1) if existing_rows else 1
            saved = {**result, "result_id": result_id, "version": version,
                     "content_hash": content_hash, "created_at": created_at}
            connection.execute(
                "INSERT INTO result_versions VALUES(?,?,?,?,?)",
                (result_id, version, content_hash, created_at, _canonical(saved)),
            )
            connection.commit()
        return saved

    def read_raw_source(self, result_id, version, company_id, source_sha256) -> bytes:
        if type(version) is not int or version < 1:
            raise SampleResearchError('invalid_version', 'A saved version is required for download')
        if not isinstance(source_sha256, str) or re.fullmatch(r'[0-9a-f]{64}', source_sha256) is None:
            raise SampleResearchError('invalid_source_hash', 'The original-source digest is invalid')
        result = self.get(result_id, version)
        selected = next((item for item in result.get('frozen_raw_sources', [])
                         if item['company_id'] == company_id and item['source_sha256'] == source_sha256), None)
        if selected is None:
            raise SampleResearchError('source_not_in_snapshot', 'The original source does not belong to this sample version and member', 404)
        if self.document_service is None:
            raise SampleResearchError('raw_source_unavailable', 'The frozen original-source service is temporarily unavailable', 503)
        raw = self.document_service.read_raw_version(company_id=company_id, document_version_id=selected['document_version_id'])
        if hashlib.sha256(raw).hexdigest() != source_sha256:
            raise SampleResearchError('source_snapshot_mismatch', 'The original source does not match the frozen digest', 409)
        return raw

    def get(self, result_id: Any, version: int | None = None) -> dict[str, Any]:
        if not isinstance(result_id, str) or _ID.fullmatch(result_id) is None:
            raise SampleResearchError("invalid_result_id", "The result ID is invalid")
        if version is not None and (type(version) is not int or version < 1):
            raise SampleResearchError("invalid_version", "The result version is invalid")
        with closing(self._results()) as connection:
            if version is None:
                row = connection.execute(
                    "SELECT result_id,version,content_hash,created_at,result_json FROM result_versions "
                    "WHERE result_id=? ORDER BY version DESC LIMIT 1", (result_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT result_id,version,content_hash,created_at,result_json FROM result_versions "
                    "WHERE result_id=? AND version=?", (result_id, version),
                ).fetchone()
        if row is None:
            raise SampleResearchError("result_not_found", "The sample result was not found", 404)
        return self._decode_saved(row)
