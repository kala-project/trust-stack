"""Strict, read-only research tools scoped to one company."""

from __future__ import annotations

from datetime import date
from copy import deepcopy
from decimal import Decimal, DivisionByZero, Inexact, InvalidOperation, ROUND_HALF_UP, localcontext
import hashlib
import json
import re
import threading
from typing import Any

from .documents import DocumentError
from .document_facts import extract_table_fact, pdf_table_provenance, raw_table_decimal
from .research_capabilities import EvidenceReader
from .service import EvidenceService
from .tool_errors import ToolError


MAX_QUERY_CHARS = 500
MAX_RESULT_ITEMS = 50
MAX_SNIPPET_CHARS = 1_200
MAX_CATALOG_TABLES = 50
MAX_TABLE_ROWS = 25
MAX_TABLE_COLUMNS = 20
MAX_COVERAGE_PERIODS = 20
_LIMITATIONS = [
    "Only already-loaded material for the selected company was searched.",
    "References identify provenance; they are not semantic or human approval.",
]
_CALCULATION_SCHEMA_VERSION = "recorded-calculation-v1"
_CALCULATION_RULE_VERSION = "strict-frozen-facts-v1"
_ADDITIVE_METRICS = frozenset({
    "payments_of_dividends",
    "payments_for_repurchase_of_common_stock",
})
_DOCUMENT_FACT_ORIGIN = "frozen_document_table_cell-v1"

def rounded_rational(value, places=6):
    """Round an exact rational half away from zero, without float intermediates."""
    numerator = abs(value.numerator) * 10 ** places
    whole, remainder = divmod(numerator, value.denominator)
    if remainder * 2 >= value.denominator:
        whole += 1
    if places == 0:
        return ("-" if value < 0 and whole else "") + str(whole)
    digits = str(whole).rjust(places + 1, "0")
    return ("-" if value < 0 and whole else "") + digits[:-places] + "." + digits[-places:]


def _text(value: Any, name: str, *, maximum: int = MAX_QUERY_CHARS) -> str:
    if not isinstance(value, str):
        raise ToolError("invalid_arguments", f"{name} must be a string")
    value = value.strip()
    if not value or len(value) > maximum:
        raise ToolError("invalid_arguments", f"{name} must contain 1..{maximum} characters")
    return value


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name, maximum=200)


def _is_terms_source(source: dict) -> bool:
    haystack = " ".join(str(source.get(key, "")) for key in ("source_id", "title", "url")).lower()
    return any(token in haystack for token in ("terms of use", "terms-of-use", "/terms", "privacy policy"))


class ResearchTools:
    """Company-scoped facade over EvidenceService; it never accepts SQL or paths."""

    def __init__(self, evidence_db, company_id: str, document_service=None, *, frozen_snapshot=None,
                 evidence_reader: EvidenceReader | None = None):
        # evidence_db is a compatibility adapter only. Application assembly injects
        # the narrow report reader and ResearchTools never opens SQLite itself.
        self.service = evidence_reader if evidence_reader is not None else EvidenceService(evidence_db)
        if not isinstance(self.service, EvidenceReader):
            raise TypeError("evidence_reader must provide report(company_id)")
        self.document_service = document_service
        self.company_id = _text(company_id, "company_id", maximum=100)
        try:
            snapshot = self.service.report(self.company_id)
        except KeyError as exc:
            raise ToolError("company_not_found", "selected company does not exist") from exc
        if frozen_snapshot is not None:
            evidence = frozen_snapshot.get("evidence")
            version = frozen_snapshot.get("data_version")
            if (not isinstance(evidence, dict) or evidence.get("company_id") != company_id
                    or not isinstance(version, dict)
                    or not all(key in evidence for key in (
                        "sources", "evidence", "claims", "facts", "evidence_chains", "activities"))
                    or not all(key in version for key in (
                        "generated_at", "schema_version", "build_version", "input_hashes"))):
                raise ToolError("invalid_frozen_snapshot", "Previous research data cannot be restored")
            snapshot = {
                **snapshot, **deepcopy(evidence), "generated_at": version["generated_at"],
                "manifest": {"schema_version": version["schema_version"], "build_version": version["build_version"]},
                "input_hashes": deepcopy(version["input_hashes"]),
            }
        self._company = snapshot
        self.data_version = {
            "generated_at": snapshot["generated_at"],
            "schema_version": snapshot["manifest"]["schema_version"],
            "build_version": snapshot["manifest"]["build_version"],
            "input_hashes": snapshot["input_hashes"],
        }
        self.document_coverage = {"available": False, "reason": "document service unavailable"}
        self.document_version_ids: tuple[str, ...] = ()
        self._document_lock = threading.RLock()
        self._snapshot_addition_callback = None
        self._public_acquisition = None
        self._acquisition_cancelled = lambda: False
        self._acquisition_attempts = {}
        self._acquisition_lock = threading.RLock()
        if frozen_snapshot is not None:
            documents = frozen_snapshot.get("documents")
            if not isinstance(documents, dict) or not isinstance(documents.get("document_version_ids"), list):
                raise ToolError("invalid_frozen_snapshot", "Previous document coverage is missing")
            self.document_coverage = deepcopy(documents["coverage"])
            self.document_version_ids = tuple(documents["document_version_ids"])
        elif self.document_service is not None:
            try:
                coverage = self.document_service.get_document_coverage(company_id=self.company_id)
            except DocumentError as exc:
                raise ToolError(exc.code, exc.message) from exc
            self.document_coverage = {"available": True, **coverage}
            self.document_version_ids = tuple(
                item["document_version_id"] for item in coverage.get("documents", [])
                if isinstance(item, dict) and isinstance(item.get("document_version_id"), str)
            )
        self._facts = {item["fact_id"]: item for item in snapshot["facts"]}
        self._calculations: dict[str, dict] = {}
        self._registered_fact_ids = frozenset(self._facts)
        self._claims = {item["claim_id"]: item for item in snapshot["claims"]}
        facts = self._facts
        evidence = {item["evidence_id"]: item for item in snapshot["evidence"]}
        sources = {item["source_id"]: item for item in snapshot["sources"]}
        activities = {item["activity_id"]: item for item in snapshot["activities"]}
        self._chains: dict[str, dict] = {}
        for chain in snapshot["evidence_chains"]:
            claim_id = chain["claim_id"]
            self._chains[claim_id] = {
                "claim": self._claims[claim_id],
                "facts": [facts[item] for item in chain["fact_ids"] if item in facts],
                "evidence": [evidence[item] for item in chain["evidence_ids"] if item in evidence],
                "sources": [sources[item] for item in chain["source_ids"] if item in sources],
                "activities": [activities[item] for item in chain["activity_ids"] if item in activities],
            }

    def enable_document_derivation(self, snapshot_addition_callback) -> bool:
        """Enable current-run PDF derivation only with a controller-owned grant callback."""
        if self.document_service is None or not callable(snapshot_addition_callback):
            return False
        self._snapshot_addition_callback = snapshot_addition_callback
        return True

    def enable_public_acquisition(self, acquisition, snapshot_addition_callback, *, cancelled=None) -> bool:
        """Controller-only opt-in; never enable for an inherited frozen snapshot."""
        if (self.document_service is None or acquisition is None
                or not callable(snapshot_addition_callback)):
            return False
        self._public_acquisition = acquisition
        self._acquisition_grant = snapshot_addition_callback
        self._acquisition_cancelled = cancelled or (lambda: False)
        return True

    def ensure_public_annual_report(self, *, fiscal_year) -> dict:
        from .source_acquisition import AcquisitionError
        if self._public_acquisition is None:
            raise ToolError("public_acquisition_unavailable", "Configured acquisition is not enabled for this run")
        if type(fiscal_year) is not int or not 1990 <= fiscal_year <= 2100:
            raise ToolError("invalid_fiscal_year", "fiscal_year must be an integer from 1990 through 2100")
        # Keep network serialization separate from the document/grant locks.
        # A failed company/year request must not become an unbounded retry loop.
        with self._acquisition_lock:
            if self._acquisition_cancelled():
                raise ToolError("cancelled", "run cancellation requested")
            previous = self._acquisition_attempts.get(fiscal_year)
            if isinstance(previous, ToolError):
                raise ToolError(previous.code, previous.message)
            if previous is not None:
                return deepcopy(previous)
            try:
                result = self._public_acquisition.ensure_annual(
                    company_id=self.company_id, fiscal_year=fiscal_year)
                record = result["document"]
                if record.get("parse_status") != "parsed":
                    raise ToolError("acquisition_parse_failed", "Annual report is not parsed")
                descriptor = self.prepare_document_addition(record)
                if self._acquisition_cancelled():
                    raise ToolError("cancelled", "run cancellation requested")
                self._acquisition_grant({
                    "type": "data_snapshot_addition", "company_id": self.company_id,
                    "document_version_ids": [record["document_version_id"]], "document": record,
                    "source": {
                        "kind": "configured_public_acquisition", "company_id": self.company_id,
                        "fiscal_year": fiscal_year, "source_sha256": record["source_sha256"],
                        "byte_length": record["byte_length"], "acquisition": result["acquisition"],
                    },
                })
                if descriptor["document_version_id"] not in self.document_version_ids:
                    raise ToolError("snapshot_grant_failed", "Acquired document was not granted to this run")
                value = {"status": "available", "company_id": self.company_id,
                         "fiscal_year": fiscal_year, "document": descriptor,
                         "acquisition": result["acquisition"],
                         "interpretation": "Archived and granted to this run; verify claims using document search and source rows. Availability is not numeric validation."}
                self._acquisition_attempts[fiscal_year] = deepcopy(value)
                return value
            except (AcquisitionError, ToolError) as exc:
                error = ToolError(exc.code, exc.message)
                self._acquisition_attempts[fiscal_year] = error
                raise error from exc

    def prepare_document_addition(self, record: dict) -> dict:
        """Validate one newly archived company document without widening run scope."""
        if self.document_service is None or not isinstance(record, dict):
            raise ToolError("invalid_snapshot_addition", "document addition is unavailable")
        version_id = record.get("document_version_id")
        if (record.get("company_id") != self.company_id or not isinstance(version_id, str)
                or re.fullmatch(r"[0-9a-f]{64}", version_id) is None):
            raise ToolError("invalid_snapshot_addition", "document addition is foreign or invalid")
        try:
            raw = self.document_service.read_raw_version(
                company_id=self.company_id, document_version_id=version_id)
        except DocumentError as exc:
            raise ToolError(exc.code, exc.message) from exc
        digest = hashlib.sha256(raw).hexdigest()
        if record.get("source_sha256") != digest or record.get("byte_length") != len(raw):
            raise ToolError("invalid_snapshot_addition", "archived bytes differ from acquisition metadata")
        descriptor = {key: record.get(key) for key in (
            "document_version_id", "source_key", "source_version", "title", "source_url",
            "source_sha256", "published_at", "collected_at", "media_type", "rights_status",
            "parse_status", "parse_error", "supersedes_document_version_id",
        )}
        if record.get("source_scope") == "session_material":
            descriptor.update(source_scope="session_material", conversation_id=record.get("conversation_id"))
        if record.get("parse_status") == "parsed":
            try:
                frozen = self.document_service.read_frozen_version(
                    company_id=self.company_id, document_version_id=version_id)
            except DocumentError as exc:
                raise ToolError(exc.code, exc.message) from exc
            if any(frozen.get(key) != descriptor.get(key) for key in descriptor):
                raise ToolError("invalid_snapshot_addition", "document metadata differs from frozen archive")
            descriptor["parser_version"] = frozen["parser_version"]
            descriptor["tables"] = [{
                "table_index": table["table_index"], "table_sha256": table["table_sha256"],
            } for table in frozen.get("tables", [])]
        return descriptor

    def add_document(self, descriptor: dict) -> None:
        """Make one prevalidated descriptor visible to this run without refreshing live coverage."""
        version_id = descriptor.get("document_version_id") if isinstance(descriptor, dict) else None
        if not isinstance(version_id, str):
            raise ToolError("invalid_snapshot_addition", "validated document descriptor is missing")
        with self._document_lock:
            if version_id in self.document_version_ids:
                return
            documents = self.document_coverage.get("documents")
            if not isinstance(documents, list):
                raise ToolError("invalid_snapshot_addition", "run document coverage is unavailable")
            documents.append(descriptor)
            self.document_version_ids = (*self.document_version_ids, version_id)
            self.document_coverage["enrolled"] = int(self.document_coverage.get("enrolled", 0)) + 1
            status_key = {"parsed": "parsed", "parse_failed": "failed"}.get(
                descriptor.get("parse_status"), "unsupported")
            self.document_coverage[status_key] = int(self.document_coverage.get(status_key, 0)) + 1

    def definitions(self) -> list[dict]:
        """Provider protocol tool definitions with closed JSON schemas."""
        definitions = [
            {
                "name": "discover_research_catalog",
                "description": "Discover the question-independent catalog of loaded metrics, periods, dimensions, frozen documents, and bounded table headers. Catalog presence is not proof that a question is answerable.",
                "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
                "handler": self.discover_research_catalog,
            },
            {
                "name": "analyze_period_coverage",
                "description": "Report present, missing-from-this-snapshot, or ambiguous frozen facts for each exact requested period. Different bases and scopes remain separate; missing is not issuer non-disclosure.",
                "parameters": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "metric": {"type": "string", "minLength": 1, "maxLength": 200},
                        "period_ends": {"type": "array", "minItems": 1, "maxItems": MAX_COVERAGE_PERIODS, "items": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"}},
                    },
                    "required": ["metric", "period_ends"],
                },
                "handler": self.analyze_period_coverage,
            },
            {
                "name": "search_evidence",
                "description": "Search curated claim evidence snippets for the selected company. Prefer search_documents for full-text source research when available.",
                "parameters": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "query": {"type": "string", "minLength": 1, "maxLength": MAX_QUERY_CHARS},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["query"],
                },
                "handler": self.search_evidence,
            },
            {
                "name": "get_company_facts",
                "description": "Get loaded structured facts with fact_id for the selected company. Metric names are exact identities: revenue is not operating_income. Omit period initially to discover fiscal dates; year and quarter facts are not interchangeable, and this tool does NOT accept fiscal labels such as FY2025 Q4. Check scope, segment, basis, period_kind and source row before using returned values.",
                "parameters": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "metric": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Exact registered metric; do not invent aliases. Available for this snapshot: " + ", ".join(sorted({fact["metric"] for fact in self._facts.values()}))},
                        "period": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Exact ISO date (YYYY-MM-DD) matching period_start/period_end, or period_kind returned by this tool. Omit when unknown; fiscal-year labels and prose do not match."},
                    },
                },
                "handler": self.get_company_facts,
            },
            {
                "name": "get_claim_evidence",
                "description": "Get the complete loaded evidence chain for one claim.",
                "parameters": {
                    "type": "object", "additionalProperties": False,
                    "properties": {"claim_id": {"type": "string", "minLength": 1, "maxLength": 200}},
                    "required": ["claim_id"],
                },
                "handler": self.get_claim_evidence,
            },
            {
                "name": "calculate_yoy",
                "description": "Calculate year-over-year percent change from two compatible loaded facts.",
                "parameters": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "current_fact_id": {"type": "string", "minLength": 1, "maxLength": 200},
                        "prior_fact_id": {"type": "string", "minLength": 1, "maxLength": 200},
                    },
                    "required": ["current_fact_id", "prior_fact_id"],
                },
                "handler": self.calculate_yoy,
            },
            {
                "name": "calculate_facts",
                "description": "Record composable exact arithmetic from frozen fact IDs or earlier calculation IDs in this run. Sum accepts 2..50 same-period inputs without a metric whitelist. Same-period differences may use different metrics; time comparisons require corresponding periods. Units, basis and scope must match; presentation_difference explicitly compares different bases. Recurring ratios are allowed: the exact fraction is retained and only display is rounded (default 12 places; percent_change, percent_ratio and percentage_point_difference default to 2). percent_ratio computes same-period numerator / denominator * 100 for margins or shares; ratio returns an x multiple, while percent_change measures temporal growth. percentage_point_difference subtracts two percentage (%) inputs and reports percentage points, not relative growth; use it for gaps between growth rates or margins. It preserves all selected periods without treating their difference as temporal growth. Later calculations use the exact value, not displayed rounding. The researcher must explain economic meaning and avoid double counting.",
                "parameters": {
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "operation": {"type": "string", "enum": ["difference", "percent_change", "ratio", "sum", "percent_ratio", "percentage_point_difference"]},
                        "fact_ids": {
                            "type": "array", "minItems": 2, "maxItems": 50,
                            "items": {"type": "string", "minLength": 1, "maxLength": 200},
                        },
                        "comparison_mode": {
                            "type": "string", "enum": ["comparable", "presentation_difference"],
                            "default": "comparable",
                        },
                        "decimal_places": {"type": "integer", "minimum": 0, "maximum": 24},
                    },
                    "required": ["operation", "fact_ids"],
                },
                "handler": self.calculate_facts,
            },
        ]
        if self.document_service is not None:
            definitions.extend([
                *([{
                    "name": "derive_pdf_tables",
                    "description": "Derive bounded bbox tables from an already authorized PDF version. First use search_documents/read_passage to find the actual physical page, exact table title, visible year labels, exact row labels, and any required year-column positions. Selectors only locate source content: they cannot supply numeric values, infer period meaning, approve semantics, or change source rights. Existing derived selectors are retained and exact duplicates are idempotent. On success use the returned document_version_id for catalog, table reading, fact extraction, calculation, and publication.",
                    "parameters": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "base_document_version_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                            "tables": {
                                "type": "array", "minItems": 1, "maxItems": 20,
                                "items": {
                                    "type": "object", "additionalProperties": False,
                                    "properties": {
                                        "physical_page": {"type": "integer", "minimum": 1},
                                        "title": {"type": "string", "minLength": 1, "maxLength": 300},
                                        "years": {"type": "array", "minItems": 1, "maxItems": 8, "uniqueItems": True, "items": {"type": "string", "pattern": "^\\d{4}$"}},
                                        "year_column_indices": {"type": "array", "minItems": 1, "maxItems": 8, "uniqueItems": True, "items": {"type": "integer", "minimum": 0}, "description": "Optional strictly increasing 0-based positions in the complete source year-header row, one per requested year in the same order. Use this only to disambiguate repeated year labels after inspecting the source layout. Each selected position must contain its corresponding year. Omit it when labels are unique; repeated labels without explicit positions are rejected rather than guessed."},
                                        "row_labels": {"type": "array", "minItems": 1, "maxItems": 30, "uniqueItems": True, "items": {"type": "string", "minLength": 1, "maxLength": 300}},
                                        "unit_line": {"type": "string", "minLength": 1, "maxLength": 300},
                                        "fiscal_period_header": {"type": "string", "minLength": 1, "maxLength": 300, "description": "Optional exact complete one- or two-line fiscal header, including the year row; a phrase above the years alone is not a complete header. Read the original page context first. Omit when it cannot be matched; do not invent header text."},
                                    },
                                    "required": ["physical_page", "title", "years", "row_labels"],
                                },
                            },
                        },
                        "required": ["base_document_version_id", "tables"],
                    },
                    "handler": self.derive_pdf_tables,
                }] if self._snapshot_addition_callback is not None else []),
                {
                    "name": "read_document_table",
                    "description": "Read bounded rows and typed numeric candidates from one exact table in a frozen in-snapshot document version. Returned candidates are untrusted until extracted with explicit metadata proof or interpretation.",
                    "parameters": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "document_version_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                            "table_index": {"type": "integer", "minimum": 0},
                            "row_offset": {"type": "integer", "minimum": 0},
                            "row_limit": {"type": "integer", "minimum": 1, "maximum": MAX_TABLE_ROWS},
                        },
                        "required": ["document_version_id", "table_index"],
                    },
                    "handler": self.read_document_table,
                },
                {
                    "name": "extract_document_table_fact",
                    "description": "Create a calculation-eligible typed fact only from an actual numeric cell in a frozen in-snapshot document table. Copy the exact row_index and column_index from read_document_table cell.locator, not the position in its returned slice. expected_value is numeric_candidate.value (signed decimal) or the exact raw_value. metric/unit/period/basis must be complete. Preserve actual source period dates, including 52/53-week fiscal years; NEVER replace them with calendar-year dates to bypass a calculation error. table_proven REQUIRES all three *_evidence_text arguments as literal substrings of the SELECTED header rows or row label; prose outside those rows is not table proof. If those labels do not fully prove metadata, use explicit_interpretation and disclose the interpretation instead of repeatedly retrying table_proven.",
                    "parameters": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "document_version_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                            "table_index": {"type": "integer", "minimum": 0},
                            "row_index": {"type": "integer", "minimum": 0},
                            "column_index": {"type": "integer", "minimum": 0},
                            "header_row_indices": {"type": "array", "minItems": 1, "maxItems": 5, "items": {"type": "integer", "minimum": 0}},
                            "row_label_column_index": {"type": "integer", "minimum": 0},
                            "expected_value": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Copy numeric_candidate.value, e.g. -28107, or the selected cell's exact raw_value, e.g. (28,107). Never calculate this value."},
                            "metric": {"type": "string", "minLength": 1, "maxLength": 200},
                            "unit": {"type": "string", "minLength": 1, "maxLength": 200},
                            "period_start": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                            "period_end": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                            "period_kind": {"type": "string", "enum": ["instant", "quarter", "half_year", "nine_months", "year", "other"]},
                            "basis": {"type": "string", "minLength": 1, "maxLength": 200},
                            "metadata_mode": {"type": "string", "enum": ["table_proven", "explicit_interpretation"], "description": "Use table_proven only with all three evidence texts literally present in selected headers/row label. Otherwise explicit_interpretation preserves an unverified assumption; it does not claim source-proven metadata."},
                            "unit_evidence_text": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Required for table_proven: exact unit text from selected header rows or row label. Omit for explicit_interpretation."},
                            "period_evidence_text": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Required for table_proven: exact period text from selected header rows or row label. Omit for explicit_interpretation."},
                            "basis_evidence_text": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Required for table_proven: exact basis text from selected header rows or row label, not an explanation you wrote. Omit for explicit_interpretation."},
                        },
                        "required": ["document_version_id", "table_index", "row_index", "column_index", "header_row_indices", "row_label_column_index", "expected_value", "metric", "unit", "period_start", "period_end", "period_kind", "basis", "metadata_mode"],
                    },
                    "handler": self.extract_document_table_fact,
                },
                {
                    "name": "search_documents",
                    "description": "Search full-text passages in the frozen document versions selected for this company and run. A no-match result is not proof of non-disclosure.",
                    "parameters": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "query": {"type": "string", "minLength": 1, "maxLength": 2_000},
                            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                            "document_version_ids": {
                                "type": "array", "minItems": 1, "maxItems": 50,
                                "items": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                            },
                        },
                        "required": ["query"],
                    },
                    "handler": self.search_documents,
                },
                {
                    "name": "read_passage",
                    "description": "Read an exact citable passage plus navigation context and validated nearby citable passages from the same frozen document. If a claim is visible only in context, use exact_quote to select a passage whose quote contains that exact text.",
                    "parameters": {
                        "type": "object", "additionalProperties": False,
                        "properties": {
                            "passage_id": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                            "context_chars": {"type": "integer", "minimum": 0, "maximum": 10_000},
                            "exact_quote": {"type": "string", "minLength": 1, "maxLength": 1_200},
                        },
                        "required": ["passage_id"],
                    },
                    "handler": self.read_passage,
                },
                {
                    "name": "get_document_coverage",
                    "description": "Report frozen enrolled, parsed, failed, and unsupported document coverage for the selected company.",
                    "parameters": {"type": "object", "additionalProperties": False, "properties": {}},
                    "handler": self.get_document_coverage,
                },
            ])
        if self._public_acquisition is not None:
            definitions.append({
                "name": "ensure_public_annual_report",
                "description": "Fill a missing annual report for this company and fiscal year using configured public sources. Local-first, official downloads only; AAPL/MSFT/GOOG/AMZN/META/NVDA/TSLA/NFLX only. No quarterly reports or arbitrary URLs. Failure is a coverage gap, not non-disclosure; do not retry the same year in this run.",
                "parameters": {"type": "object", "additionalProperties": False,
                               "properties": {"fiscal_year": {"type": "integer", "minimum": 1990, "maximum": 2100}},
                               "required": ["fiscal_year"]},
                "handler": self.ensure_public_annual_report,
            })
        return definitions

    tools = definitions

    @staticmethod
    def _raw_table_decimal(value: Any) -> tuple[Decimal, str | None]:
        try:
            return raw_table_decimal(value)
        except DocumentError as exc:
            raise ToolError(exc.code, exc.message) from exc

    def _snapshot_table(self, document_version_id: Any, table_index: Any) -> dict:
        if self.document_service is None:
            raise ToolError("document_service_unavailable", "document coverage is unavailable")
        document_version_id = _text(document_version_id, "document_version_id", maximum=64)
        if document_version_id not in self.document_version_ids:
            raise ToolError(
                "not_found_or_out_of_scope",
                "document version is outside this run's company snapshot",
            )
        if type(table_index) is not int or table_index < 0:
            raise ToolError("invalid_arguments", "table_index must be a non-negative integer")
        descriptor = next(
            item for item in self.document_coverage.get("documents", [])
            if item.get("document_version_id") == document_version_id
        )
        manifest = next(
            (item for item in descriptor.get("tables", []) if item.get("table_index") == table_index),
            None,
        )
        if manifest is None:
            raise ToolError("table_not_found", "table is not in this run's frozen document manifest")
        try:
            frozen = self.document_service.read_frozen_version(
                company_id=self.company_id, document_version_id=document_version_id,
            )
        except DocumentError as exc:
            raise ToolError(exc.code, exc.message) from exc
        table = next(
            (item for item in frozen.get("tables", []) if item.get("table_index") == table_index),
            None,
        )
        if (table is None or table.get("table_sha256") != manifest.get("table_sha256")
                or frozen.get("parser_version") != descriptor.get("parser_version")):
            raise ToolError(
                "frozen_table_mismatch",
                "archived bytes do not reproduce the table in this run's frozen manifest",
            )
        return {**frozen, **table}

    def discover_research_catalog(self) -> dict:
        """Return inventory and dimensions, never a claim of question answerability."""
        metric_groups: dict[str, list[dict]] = {}
        for fact in self._facts.values():
            metric_groups.setdefault(fact["metric"], []).append(fact)
        metrics = []
        for metric, facts in sorted(metric_groups.items())[:MAX_RESULT_ITEMS]:
            periods_start = sorted({item.get("period_start") for item in facts if item.get("period_start")})
            periods_end = sorted({item.get("period_end") for item in facts if item.get("period_end")})
            metrics.append({
                "metric": metric,
                "fact_count": len(facts),
                "period_starts": periods_start[:MAX_RESULT_ITEMS],
                "period_ends": periods_end[:MAX_RESULT_ITEMS],
                "periods_truncated": len(periods_start) > MAX_RESULT_ITEMS or len(periods_end) > MAX_RESULT_ITEMS,
                "period_kinds": sorted({item.get("period_kind") for item in facts if item.get("period_kind")})[:MAX_RESULT_ITEMS],
                "units": sorted({item.get("unit") for item in facts if item.get("unit")})[:MAX_RESULT_ITEMS],
                "bases": sorted({item.get("basis") for item in facts if item.get("basis")})[:MAX_RESULT_ITEMS],
                "scopes": sorted({item.get("scope") for item in facts if item.get("scope")})[:MAX_RESULT_ITEMS],
                "origins": sorted({
                    _DOCUMENT_FACT_ORIGIN if item["fact_id"] not in self._registered_fact_ids else "registered_snapshot_fact"
                    for item in facts
                }),
            })
        documents = []
        table_count = 0
        all_documents = self.document_coverage.get("documents", [])
        total_tables = sum(len(item.get("tables", [])) for item in all_documents)
        for descriptor in all_documents[:MAX_RESULT_ITEMS]:
            document = {key: descriptor.get(key) for key in (
                "document_version_id", "source_key", "source_version", "title", "source_url",
                "source_sha256", "published_at", "parse_status", "parser_version",
            )}
            document["tables"] = []
            for manifest in descriptor.get("tables", []):
                if table_count >= MAX_CATALOG_TABLES:
                    break
                table = self._snapshot_table(
                    descriptor["document_version_id"], manifest["table_index"]
                )
                rows = table.get("rows", [])
                document["tables"].append({
                    "table_index": table["table_index"],
                    "table_sha256": table["table_sha256"],
                    "locator": table["locator"],
                    "row_count": len(rows),
                    "column_count": max((len(row) for row in rows), default=0),
                    "header_preview": [row[:MAX_TABLE_COLUMNS] for row in rows[:3]],
                    "header_preview_truncated": len(rows) > 3 or any(len(row) > MAX_TABLE_COLUMNS for row in rows[:3]),
                })
                table_count += 1
            documents.append(document)
        return {
            "company_id": self.company_id,
            "data_version": self.data_version,
            "metrics": metrics,
            "documents": documents,
            "document_coverage": {key: self.document_coverage.get(key) for key in (
                "available", "enrolled", "parsed", "failed", "unsupported", "reason"
            ) if key in self.document_coverage},
            "truncated": (len(metric_groups) > MAX_RESULT_ITEMS
                          or len(all_documents) > MAX_RESULT_ITEMS
                          or table_count < total_tables),
            "interpretation": "This is an inventory of frozen available data, not a statement that any user question is answerable or complete.",
            "limitations": list(_LIMITATIONS),
        }

    def analyze_period_coverage(self, *, metric, period_ends) -> dict:
        metric = _text(metric, "metric", maximum=200)
        if (not isinstance(period_ends, list) or not 1 <= len(period_ends) <= MAX_COVERAGE_PERIODS
                or len(set(period_ends)) != len(period_ends)):
            raise ToolError("invalid_arguments", "period_ends must contain 1..20 unique ISO dates")
        normalized_periods = []
        for value in period_ends:
            value = _text(value, "period_end", maximum=10)
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ToolError("invalid_arguments", "every period_end must be an ISO calendar date") from exc
            normalized_periods.append(value)
        metric_facts = [item for item in self._facts.values() if item.get("metric").casefold() == metric.casefold()]
        periods = []
        identity_keys = ("basis", "unit", "period_kind", "scope", "segment", "geography")
        for period_end in normalized_periods:
            matches = [item for item in metric_facts if item.get("period_end") == period_end]
            grouped: dict[tuple, list[dict]] = {}
            for fact in matches:
                grouped.setdefault(tuple(fact.get(key) for key in identity_keys), []).append(fact)
            groups = []
            ordered_groups = sorted(grouped.items(), key=lambda item: repr(item[0]))
            for identity, facts in ordered_groups[:MAX_RESULT_ITEMS]:
                groups.append({
                    "dimensions": dict(zip(identity_keys, identity)),
                    "status": "present" if len(facts) == 1 else "ambiguous",
                    "facts": facts[:MAX_RESULT_ITEMS],
                    "candidate_count": len(facts),
                })
            periods.append({
                "period_end": period_end,
                "status": "missing" if not grouped else "ambiguous" if any(len(facts) != 1 for facts in grouped.values()) else "present",
                "basis_groups": groups,
                "multiple_bases_or_scopes": len(grouped) > 1,
                "truncated": len(ordered_groups) > MAX_RESULT_ITEMS or any(len(facts) > MAX_RESULT_ITEMS for facts in grouped.values()),
                "meaning": (
                    "No matching frozen fact is currently available; this is unknown coverage, not issuer non-disclosure."
                    if not groups else
                    "Candidates are separated by basis, unit, period kind, and scope; do not silently unify them."
                ),
            })
        return {
            "company_id": self.company_id,
            "data_version": self.data_version,
            "metric": metric,
            "periods": periods,
            "complete_for_requested_periods": all(item["status"] == "present" for item in periods),
            "completeness_scope": "only the exact requested periods and currently frozen facts",
            "limitations": list(_LIMITATIONS),
        }

    def derive_pdf_tables(self, *, base_document_version_id, tables) -> dict:
        callback = self._snapshot_addition_callback
        if self.document_service is None or callback is None:
            raise ToolError("document_derivation_unavailable", "PDF table derivation is unavailable")
        base_document_version_id = _text(
            base_document_version_id, "base_document_version_id", maximum=64
        )
        with self._document_lock:
            if base_document_version_id not in self.document_version_ids:
                raise ToolError(
                    "not_found_or_out_of_scope",
                    "base document version is outside this run's company snapshot",
                )
        try:
            record = self.document_service.derive_pdf_tables(
                company_id=self.company_id,
                base_document_version_id=base_document_version_id,
                table_selectors=tables,
            )
        except DocumentError as exc:
            raise ToolError(exc.code, exc.message) from exc
        version_id = record["document_version_id"]
        with self._document_lock:
            already_in_scope = version_id in self.document_version_ids
        callback_result = None
        if not already_in_scope:
            callback_result = callback({
                "type": "data_snapshot_addition",
                "company_id": self.company_id,
                "document_version_ids": [version_id],
                "document": record,
                "source": {
                    "company_id": self.company_id,
                    "kind": "pdf_table_derivation",
                    "base_document_version_id": base_document_version_id,
                    "source_sha256": record["source_sha256"],
                    "byte_length": record["byte_length"],
                },
            })
        with self._document_lock:
            descriptor = next((item for item in self.document_coverage.get("documents", [])
                               if item.get("document_version_id") == version_id), None)
        if descriptor is None:
            raise ToolError(
                "snapshot_grant_failed", "derived document version was not granted to this run"
            )
        return {
            "status": "already_in_scope" if already_in_scope else "derived_and_granted",
            "base_document_version_id": base_document_version_id,
            "document_version_id": version_id,
            "source_sha256": descriptor.get("source_sha256"),
            "parser_version": descriptor.get("parser_version"),
            "tables": deepcopy(descriptor.get("tables", [])),
            "snapshot_callback_result": callback_result,
            "interpretation": (
                "The returned tables preserve source coordinates and remain semantically unapproved; "
                "numeric cells require read_document_table and extract_document_table_fact."
            ),
        }

    def read_document_table(self, *, document_version_id, table_index, row_offset=0, row_limit=10) -> dict:
        if type(row_offset) is not int or row_offset < 0:
            raise ToolError("invalid_arguments", "row_offset must be a non-negative integer")
        if type(row_limit) is not int or not 1 <= row_limit <= MAX_TABLE_ROWS:
            raise ToolError("invalid_arguments", f"row_limit must be an integer from 1 to {MAX_TABLE_ROWS}")
        table = self._snapshot_table(document_version_id, table_index)
        rows = table.get("rows", [])
        selected = []
        for row_index, row in enumerate(rows[row_offset:row_offset + row_limit], row_offset):
            cells = []
            for column_index, raw in enumerate(row[:MAX_TABLE_COLUMNS]):
                cell = {"raw_value": raw, "locator": {
                    "document_version_id": table["document_version_id"],
                    "table_index": table["table_index"], "row_index": row_index,
                    "column_index": column_index,
                }}
                try:
                    numeric, raw_unit = self._raw_table_decimal(raw)
                except ToolError:
                    pass
                else:
                    cell["numeric_candidate"] = {
                        "value": format(numeric, "f"), "raw_unit_marker": raw_unit,
                        "status": "candidate_not_trusted_fact",
                    }
                cells.append(cell)
            selected.append({
                "row_index": row_index,
                "row_locator": {"start": table["row_spans"][row_index][0], "end": table["row_spans"][row_index][1]},
                "cells": cells,
                "columns_truncated": len(row) > MAX_TABLE_COLUMNS,
            })
        pdf_provenance = pdf_table_provenance(
            table, row_indices=[item["row_index"] for item in selected])
        return {
            "company_id": self.company_id,
            "document_version": {
                **{key: table.get(key) for key in (
                    "document_version_id", "source_sha256", "parser_version", "title", "source_url"
                )},
                **({"rights_status": table.get("rights_status")} if pdf_provenance is not None else {}),
            },
            "table_index": table["table_index"], "table_sha256": table["table_sha256"],
            "table_locator": table["locator"], "row_count": len(rows),
            "rows": selected, "row_offset": row_offset,
            "truncated": row_offset + len(selected) < len(rows),
            **({"pdf_provenance": pdf_provenance} if pdf_provenance is not None else {}),
            "interpretation": (
                "Numeric values are cell-tied candidates only; use extract_document_table_fact before calculation. Copy each cell.locator row_index/column_index exactly (zero-based full-table coordinates, not slice positions) and numeric_candidate.value as expected_value. Preserve the negative sign of parenthesized cash outflows."
                + (" Formal PDF parsing and coordinates do not establish semantic approval, economic meaning, period mapping, or additional source authorization."
                   if pdf_provenance is not None else "")
            ),
        }

    def extract_document_table_fact(
        self, *, document_version_id, table_index, row_index, column_index,
        header_row_indices, row_label_column_index, expected_value, metric, unit,
        period_start, period_end, period_kind, basis, metadata_mode,
        unit_evidence_text=None, period_evidence_text=None, basis_evidence_text=None,
    ) -> dict:
        table = self._snapshot_table(document_version_id, table_index)
        try:
            result = extract_table_fact(
                table, self.company_id, row_index=row_index, column_index=column_index,
                header_row_indices=header_row_indices, row_label_column_index=row_label_column_index,
                expected_value=expected_value, metric=metric, unit=unit, period_start=period_start,
                period_end=period_end, period_kind=period_kind, basis=basis, metadata_mode=metadata_mode,
                unit_evidence_text=unit_evidence_text, period_evidence_text=period_evidence_text,
                basis_evidence_text=basis_evidence_text,
            )
        except DocumentError as exc:
            raise ToolError(exc.code, exc.message) from exc
        self._facts[result["fact"]["fact_id"]] = result["fact"]
        return result

    def _chain(self, claim_id: str) -> dict:
        return self._chains[claim_id]

    def _chains_for_company(self):
        for claim_id in self._claims:
            yield self._chain(claim_id)

    @staticmethod
    def _source_summary(source: dict) -> dict:
        return {key: source.get(key) for key in ("source_id", "title", "url", "published_at", "rights_status")}

    def search_evidence(self, *, query, limit=8) -> dict:
        query = _text(query, "query")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ToolError("invalid_arguments", "limit must be an integer from 1 to 20")
        tokens = [item for item in re.findall(r"[\w.%+-]+", query.casefold()) if len(item) > 1]
        if not tokens:
            raise ToolError("invalid_arguments", "query has no searchable terms")
        hits: dict[str, tuple[int, dict]] = {}
        for chain in self._chains_for_company():
            claim = chain["claim"]
            sources = {item["source_id"]: item for item in chain["sources"] if not _is_terms_source(item)}
            for item in chain["evidence"]:
                source = sources.get(item["source_id"])
                if source is None or not item.get("locator") or not item.get("quote"):
                    continue
                haystack = f'{item["quote"]} {claim["text"]} {source["title"]}'.casefold()
                score = sum(haystack.count(token) for token in tokens)
                if score <= 0:
                    continue
                result = {
                    "evidence_id": item["evidence_id"],
                    "claim_id": claim["claim_id"],
                    "source_id": item["source_id"],
                    "source": self._source_summary(source),
                    "snippet": item["quote"][:MAX_SNIPPET_CHARS],
                    "locator": item["locator"],
                }
                previous = hits.get(item["evidence_id"])
                if previous is None or score > previous[0]:
                    hits[item["evidence_id"]] = (score, result)
        results = [value for _, value in sorted(hits.values(), key=lambda pair: (-pair[0], pair[1]["evidence_id"]))[:limit]]
        return {"company_id": self.company_id, "data_version": self.data_version,
                "query": query, "results": results, "limitations": list(_LIMITATIONS)}

    def get_document_coverage(self) -> dict:
        """Return the creation-time selection rather than silently reporting no documents."""
        return {"company_id": self.company_id, **self.document_coverage,
                "document_version_ids": list(self.document_version_ids)}

    def search_documents(self, *, query, limit=8, document_version_ids=None) -> dict:
        if self.document_service is None:
            raise ToolError("document_service_unavailable", "document coverage is unavailable")
        query = _text(query, "query", maximum=2_000)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ToolError("invalid_arguments", "limit must be an integer from 1 to 50")
        selected = list(self.document_version_ids)
        if document_version_ids is not None:
            if not isinstance(document_version_ids, (list, tuple)) or not document_version_ids:
                raise ToolError("invalid_arguments", "document_version_ids must be a non-empty list")
            if any(not isinstance(item, str) for item in document_version_ids):
                raise ToolError("invalid_arguments", "document_version_ids contains an invalid identifier")
            requested = list(dict.fromkeys(document_version_ids))
            if not set(requested).issubset(self.document_version_ids):
                raise ToolError("not_found_or_out_of_scope", "document version is outside this run's company snapshot")
            selected = requested
        if not selected:
            return {
                "company_id": self.company_id, "results": [],
                "coverage": self.document_coverage,
                "interpretation": "No enrolled document versions were selected; document coverage is available but this does not mean the company made no disclosure.",
                "version_scope": "explicit", "as_of": None,
            }
        try:
            return self.document_service.search_documents(
                company_id=self.company_id, query=query, limit=limit,
                document_version_ids=selected,
            )
        except DocumentError as exc:
            raise ToolError(exc.code, exc.message) from exc

    def read_passage(self, *, passage_id, context_chars=500, exact_quote=None) -> dict:
        if self.document_service is None:
            raise ToolError("document_service_unavailable", "document coverage is unavailable")
        try:
            result = self.document_service.read_passage(
                company_id=self.company_id, passage_id=passage_id, context_chars=context_chars,
                exact_quote=exact_quote,
            )
        except DocumentError as exc:
            raise ToolError(exc.code, exc.message) from exc
        if (result.get("company_id") != self.company_id
                or result.get("document_version_id") not in self.document_version_ids):
            raise ToolError("not_found_or_out_of_scope", "passage is outside this run's company snapshot")
        if any(
            item.get("company_id") != self.company_id
            or item.get("document_version_id") not in self.document_version_ids
            for item in result.get("context_passages", [])
        ):
            raise ToolError("not_found_or_out_of_scope", "context passage is outside this run's company snapshot")
        return result

    def get_company_facts(self, *, metric=None, period=None) -> dict:
        metric = _optional_text(metric, "metric")
        period = _optional_text(period, "period")
        facts = list(self._facts.values())
        if metric is not None:
            facts = [item for item in facts if item["metric"].casefold() == metric.casefold()]
        if period is not None:
            facts = [item for item in facts if period in (item.get("period_start"), item.get("period_end"), item.get("period_kind"))]
        evidence_sources = {item["evidence_id"]: item["source_id"]
                            for item in self._company["evidence"]}
        results = []
        for fact in facts[:MAX_RESULT_ITEMS]:
            result = dict(fact)
            result["source_id"] = evidence_sources.get(fact["evidence_id"], fact.get("source_id"))
            results.append(result)
        return {
            "company_id": self.company_id, "data_version": self.data_version, "facts": results,
            "available_metrics": sorted({fact["metric"] for fact in self._facts.values()}),
            "match_status": "matched" if results else "no_matching_registered_facts",
            "empty_result_meaning": None if results else "No registered metric/period matched these exact parameters; this is not zero, absent disclosure, or absent raw data. Use available_metrics rather than guessing aliases.",
            "truncated": len(facts) > MAX_RESULT_ITEMS, "limitations": list(_LIMITATIONS),
        }

    def get_claim_evidence(self, *, claim_id) -> dict:
        claim_id = _text(claim_id, "claim_id", maximum=200)
        claim = self._claims.get(claim_id)
        if claim is None:
            raise ToolError("not_found_or_out_of_scope", "claim does not exist for the selected company")
        chain = self._chain(claim_id)
        sources = [item for item in chain["sources"] if not _is_terms_source(item)]
        allowed_sources = {item["source_id"] for item in sources}
        evidence = [item for item in chain["evidence"] if item["source_id"] in allowed_sources]
        trimmed_evidence = []
        for item in evidence[:MAX_RESULT_ITEMS]:
            result = dict(item)
            quote = result.get("quote", "")
            if len(quote) > MAX_SNIPPET_CHARS:
                result["quote"] = quote[:MAX_SNIPPET_CHARS]
                result["quote_truncated"] = True
            trimmed_evidence.append(result)
        return {
            "company_id": self.company_id, "data_version": self.data_version, "claim": chain["claim"],
            "facts": chain["facts"][:MAX_RESULT_ITEMS], "evidence": trimmed_evidence,
            "sources": [self._source_summary(item) for item in sources[:MAX_RESULT_ITEMS]],
            "truncated": any(len(items) > MAX_RESULT_ITEMS for items in (chain["facts"], evidence, sources)),
            "limitations": list(dict.fromkeys(_LIMITATIONS + chain["claim"].get("limitations", []))),
        }

    def calculate_yoy(self, *, current_fact_id, prior_fact_id) -> dict:
        current_id = _text(current_fact_id, "current_fact_id", maximum=200)
        prior_id = _text(prior_fact_id, "prior_fact_id", maximum=200)
        current = self._facts.get(current_id)
        prior = self._facts.get(prior_id)
        if (current is None or prior is None
                or current.get("company_id") != self.company_id
                or prior.get("company_id") != self.company_id):
            raise ToolError("not_found_or_out_of_scope", "both facts must belong to the selected company")
        for key in ("metric", "unit", "period_kind", "basis"):
            if current[key] != prior[key]:
                raise ToolError("incompatible_facts", f"facts have different {key}")
        self._require_same_optional_scope((current, prior))
        try:
            current_end = date.fromisoformat(current["period_end"])
            prior_end = date.fromisoformat(prior["period_end"])
        except (TypeError, ValueError) as exc:
            raise ToolError("invalid_fact", "fact period_end is not a calendar date") from exc
        elapsed_days = (current_end - prior_end).days
        same_calendar_quarter = (current_end.month - 1) // 3 == (prior_end.month - 1) // 3
        if current_end.year != prior_end.year + 1 or elapsed_days not in (365, 366) or not same_calendar_quarter:
            raise ToolError("incompatible_facts", "facts must represent corresponding periods one year apart")
        current_value, prior_value = self._decimal_values((current, prior))
        if prior_value == 0:
            raise ToolError("division_by_zero", "prior fact value is zero")
        with localcontext() as context:
            context.prec = self._decimal_precision((current, prior))
            value = ((current_value - prior_value) / abs(prior_value) * Decimal(100)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        result = self._calculation_record(
            operation="percent_change", facts=(current, prior), comparison_mode="comparable",
            formula="(current_fact - prior_fact) / abs(prior_fact) * 100",
            value=value, unit="%", rule="strict_one_year_corresponding_periods",
            limitations=[
                "Percent change uses the absolute value of the prior fact as its baseline.",
                "The arithmetic passed mechanical checks; its interpretation remains unverified.",
            ],
        )
        result.update({
            "calculation": "year_over_year_percent_change",
            "current_fact": current, "prior_fact": prior,
        })
        return result

    def calculate_facts(self, *, operation, fact_ids, comparison_mode="comparable", decimal_places=None) -> dict:
        from .arithmetic import calculate
        return calculate(self, operation=operation, fact_ids=fact_ids,
                         comparison_mode=comparison_mode, decimal_places=decimal_places)

    def input_definitions(self, store, run_id, validate):
        from .research_inputs import InputError

        def catalog(after="", limit=50):
            try:
                result = store.catalog(run_id, after=after, limit=limit)
            except InputError as exc:
                raise ToolError(exc.code, exc.message) from exc
            for item in result["inputs"]:
                item["in_frozen_snapshot"] = item["document_version_id"] in self.document_version_ids
            return result

        def read(input_ids):
            if not isinstance(input_ids, list) or not 1 <= len(input_ids) <= 20:
                raise ToolError("invalid_arguments", "read_research_inputs requires 1..20 input IDs")
            restored = []
            try:
                for identifier in input_ids:
                    source_run_id, result = store.lookup(run_id, identifier)
                    validate(result, source_run_id)
                    restored.append(result)
            except InputError as exc:
                raise ToolError(exc.code, exc.message) from exc
            self._facts.update({item["fact"]["fact_id"]: item["fact"] for item in restored})
            return {"restored_document_facts": restored,
                    "interpretation": "Input reuse is source-checked, not semantically verified."}

        return [{
            "name": "list_research_inputs",
            "description": "List this conversation's persisted document-cell inputs by stable ID. Entries outside the current frozen snapshot are marked, not silently substituted. Catalog entries are not current source verification. Follow next_cursor for additional entries.",
            "parameters": {"type": "object", "additionalProperties": False, "properties": {
                "after": {"type": "string", "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            }},
            "handler": catalog,
        }, {
            "name": "read_research_inputs",
            "description": "Reuse 1..20 persisted document-cell inputs in this conversation. Revalidates current frozen scope, original bytes, parser and extraction interpretation before loading any inputs. Returned fact IDs can feed any compatible calculation; this is not limited to confirmed models. Legacy inputs absent from the catalog must be read from their original source again.",
            "parameters": {"type": "object", "additionalProperties": False, "required": ["input_ids"],
                           "properties": {"input_ids": {"type": "array", "minItems": 1, "maxItems": 20,
                                                       "items": {"type": "string", "minLength": 1, "maxLength": 200}}}},
            "handler": read,
        }]

    def replay_arithmetic(self, record) -> dict:
        from .arithmetic import replay
        if "parameters" in record:
            return replay(self, record)
        return self._legacy_calculate_facts(
            operation=record.get("operation"), fact_ids=[fact["fact_id"] for fact in record["inputs"]],
            comparison_mode=record.get("comparison_mode", "comparable"))

    def _legacy_calculate_facts(self, *, operation, fact_ids, comparison_mode="comparable") -> dict:
        """Read-only compatibility for reports created before composable arithmetic."""
        operation = _text(operation, "operation", maximum=30)
        if operation not in {"difference", "percent_change", "ratio", "sum"}:
            raise ToolError("invalid_arguments", "operation is not supported")
        comparison_mode = _text(comparison_mode, "comparison_mode", maximum=40)
        if comparison_mode not in {"comparable", "presentation_difference"}:
            raise ToolError("invalid_arguments", "comparison_mode is not supported")
        if not isinstance(fact_ids, list) or len(fact_ids) != 2:
            raise ToolError("invalid_arguments", "fact_ids must contain exactly two fact IDs")
        identifiers = [_text(item, "fact_id", maximum=200) for item in fact_ids]
        if len(set(identifiers)) != len(identifiers):
            raise ToolError("incompatible_facts", "duplicate facts cannot be calculated")
        facts = tuple(self._facts.get(identifier) for identifier in identifiers)
        if any(fact is None or fact.get("company_id") != self.company_id for fact in facts):
            raise ToolError("not_found_or_out_of_scope", "all facts must belong to the selected company")

        left, right = facts
        values = self._decimal_values(facts)
        if operation == "ratio":
            if comparison_mode != "comparable":
                raise ToolError("incompatible_facts", "ratio does not support presentation_difference")
            self._require_same(facts, ("unit", "basis", "period_start", "period_end", "period_kind"))
            self._require_same_optional_scope(facts)
            if values[1] == 0:
                raise ToolError("division_by_zero", "ratio denominator fact value is zero")
            formula = "fact[0] / fact[1]"
            rule = "same_exact_period_unit_basis_scope_exact_decimal_ratio"
            unit = "x"
            with localcontext() as context:
                context.prec = self._decimal_precision(facts)
                context.traps[Inexact] = True
                try:
                    value = values[0] / values[1]
                except (DivisionByZero, Inexact, InvalidOperation) as exc:
                    raise ToolError(
                        "invalid_calculation",
                        "ratio does not have an exact finite decimal representation",
                    ) from exc
            extra_limitations = [
                "This ratio is an exact arithmetic quotient of the two named same-period facts; its economic interpretation remains unverified.",
            ]
        elif operation == "sum":
            if comparison_mode != "comparable":
                raise ToolError("incompatible_facts", "sum does not support presentation_difference")
            if {left.get("metric"), right.get("metric")} != _ADDITIVE_METRICS:
                raise ToolError("incompatible_facts", "sum is restricted to dividend and common-stock-repurchase cash payments")
            self._require_same(facts, ("unit", "basis", "period_start", "period_end", "period_kind"))
            self._require_same_optional_scope(facts)
            formula = "fact[0] + fact[1]"
            rule = "controlled_additive_cash_payments_same_exact_period"
            unit = left["unit"]
            with localcontext() as context:
                context.prec = self._decimal_precision(facts)
                value = values[0] + values[1]
            extra_limitations = [
                "This sum combines cash-flow dividend and common-stock-repurchase payments only; it is not an issuer-reported capital-return total.",
            ]
        elif comparison_mode == "presentation_difference":
            if operation != "difference":
                raise ToolError("incompatible_facts", "presentation_difference supports difference only")
            self._require_same(facts, ("metric", "unit", "period_start", "period_end", "period_kind"))
            self._require_same_optional_scope(facts)
            if left.get("basis") == right.get("basis"):
                raise ToolError("incompatible_facts", "presentation_difference requires different bases")
            formula = "fact[0] - fact[1]"
            rule = "same_period_different_basis_presentation_difference"
            unit = left["unit"]
            with localcontext() as context:
                context.prec = self._decimal_precision(facts)
                value = values[0] - values[1]
            extra_limitations = [
                "This is a same-period presentation difference between different bases; it is NOT growth and does not establish a reconciliation cause.",
            ]
        else:
            self._require_same(facts, ("metric", "unit", "period_kind", "basis"))
            self._require_same_optional_scope(facts)
            self._require_corresponding_periods(left, right)
            unit = "%" if operation == "percent_change" else left["unit"]
            if operation == "difference":
                formula = "fact[0] - fact[1]"
                rule = "corresponding_period_cumulative_difference"
                with localcontext() as context:
                    context.prec = self._decimal_precision(facts)
                    value = values[0] - values[1]
                extra_limitations = []
            else:
                if values[1] == 0:
                    raise ToolError("division_by_zero", "baseline fact value is zero")
                formula = "(fact[0] - fact[1]) / abs(fact[1]) * 100"
                rule = "corresponding_period_cumulative_percent_change_absolute_baseline"
                with localcontext() as context:
                    context.prec = self._decimal_precision(facts)
                    try:
                        value = ((values[0] - values[1]) / abs(values[1]) * Decimal(100)).quantize(
                            Decimal("0.01"), rounding=ROUND_HALF_UP
                        )
                    except (DivisionByZero, InvalidOperation) as exc:
                        raise ToolError("invalid_calculation", "percent change could not be represented") from exc
                extra_limitations = [
                    "Percent change is signed and uses abs(fact[1]) as the baseline denominator.",
                ]
        return self._calculation_record(
            operation=operation, facts=facts, comparison_mode=comparison_mode,
            formula=formula, value=value, unit=unit, rule=rule,
            limitations=extra_limitations + [
                "The arithmetic passed mechanical checks; its interpretation remains unverified."
            ],
        )

    @staticmethod
    def _require_same(facts: tuple[dict, ...], keys: tuple[str, ...]) -> None:
        for key in keys:
            first = facts[0].get(key)
            if not isinstance(first, str) or not first:
                raise ToolError("invalid_fact", f"fact lacks {key}")
            if any(fact.get(key) != first for fact in facts[1:]):
                raise ToolError("incompatible_facts", f"facts have different {key}")

    @staticmethod
    def _require_same_optional_scope(facts: tuple[dict, ...]) -> None:
        for key in ("scope", "segment", "geography"):
            values = [fact.get(key) for fact in facts]
            if any(value != values[0] for value in values[1:]):
                raise ToolError("incompatible_facts", f"facts have different {key}")

    @staticmethod
    def _require_corresponding_periods(current: dict, prior: dict) -> str:
        try:
            current_start = date.fromisoformat(current["period_start"])
            current_end = date.fromisoformat(current["period_end"])
            prior_start = date.fromisoformat(prior["period_start"])
            prior_end = date.fromisoformat(prior["period_end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolError("invalid_fact", "fact period is not a complete calendar date range") from exc
        year_gap = current_end.year - prior_end.year
        ordered = (year_gap >= 1 and current_start > prior_end
                   and prior_start <= prior_end and current_start <= current_end)
        if (ordered and current_start.year - prior_start.year == year_gap
                and (current_start.month, current_start.day) == (prior_start.month, prior_start.day)
                and (current_end.month, current_end.day) == (prior_end.month, prior_end.day)):
            return "calendar_anniversary"
        # Fiscal years ending on a fixed weekday legitimately drift across calendar
        # dates. Only accept full 52/53-week years in the same annual window;
        # never relabel the evidence dates merely to satisfy the calculator.
        week_based = (
            ordered and current.get("period_kind") == prior.get("period_kind") == "year"
            and (current_end - current_start).days + 1 in {364, 371}
            and (prior_end - prior_start).days + 1 in {364, 371}
            and current_start.weekday() == prior_start.weekday()
            and current_end.weekday() == prior_end.weekday()
            and abs((current_start - prior_start).days - 365.2425 * year_gap) <= 7
            and abs((current_end - prior_end).days - 365.2425 * year_gap) <= 7
        )
        if week_based:
            return "week_based_fiscal_year"
        raise ToolError("incompatible_facts", "facts must represent corresponding periods in different years (calendar anniversaries or complete 52/53-week fiscal years); preserve actual source dates, never invent normalized dates")

    @staticmethod
    def _decimal_values(facts: tuple[dict, ...]) -> tuple[Decimal, ...]:
        values = []
        for fact in facts:
            raw = fact.get("value")
            if (not isinstance(raw, str) or len(raw) > 1_000
                    or re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", raw) is None):
                raise ToolError("invalid_fact", "fact value is not a finite bounded decimal string")
            try:
                value = Decimal(raw)
            except (InvalidOperation, ValueError) as exc:
                raise ToolError("invalid_fact", "fact value is not decimal") from exc
            if not value.is_finite():
                raise ToolError("invalid_fact", "fact value must be finite")
            values.append(value)
        return tuple(values)

    @staticmethod
    def _decimal_precision(facts: tuple[dict, ...]) -> int:
        return max(50, sum(len(fact["value"].lstrip("+-").replace(".", "")) for fact in facts) + 20)

    def _calculation_record(self, *, operation: str, facts: tuple[dict, ...],
                            comparison_mode: str, formula: str, value: Decimal,
                            unit: str, rule: str, limitations: list[str], parameters: dict | None = None) -> dict:
        if not value.is_finite():
            raise ToolError("invalid_calculation", "calculation result must be finite")
        if any(not isinstance(fact.get("evidence_id"), str) or not fact["evidence_id"]
               for fact in facts):
            raise ToolError("invalid_fact", "calculation input lacks evidence identity")
        inputs = [dict(fact) for fact in facts]
        identity = {
            "schema_version": _CALCULATION_SCHEMA_VERSION,
            "rule_version": _CALCULATION_RULE_VERSION,
            "rule": rule,
            "operation": operation,
            "comparison_mode": comparison_mode,
            "company_id": self.company_id,
            "data_version": self.data_version,
            "inputs": inputs,
        }
        if parameters is not None:
            identity["parameters"] = deepcopy(parameters)
        execution_scope = None
        if len(getattr(self, "company_ids", (self.company_id,))) > 1:
            from .execution_scope import describe
            execution_scope = describe(self.company_ids)
            identity["execution_scope"] = execution_scope
        digest = hashlib.sha256(json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        return {
            "calculation_id": f"calc-{digest}",
            "schema_version": _CALCULATION_SCHEMA_VERSION,
            "operation": operation,
            "formula": formula,
            "value": format(value, "f"),
            "unit": unit,
            "inputs": inputs,
            "evidence_ids": list(dict.fromkeys(fact["evidence_id"] for fact in facts)),
            "data_version": self.data_version,
            "company_id": self.company_id,
            "comparison_mode": comparison_mode,
            "limitations": list(dict.fromkeys(
                (["Only already-loaded material within the explicit research scope was used.", _LIMITATIONS[1]]
                 if execution_scope else _LIMITATIONS) + limitations)),
            "interpretation": "unverified",
            **({"execution_scope": execution_scope} if execution_scope else {}),
            **({"parameters": deepcopy(parameters)} if parameters is not None else {}),
        }
