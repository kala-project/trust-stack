"""L2 document-cell extraction and replay; no chat, model, or report dependency."""
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from .documents import DocumentError, _text


ORIGIN = "frozen_document_table_cell-v1"
LIMITATIONS = [
    "Only already-loaded material for the selected company was searched.",
    "References identify provenance; they are not semantic or human approval.",
]
_RAW_NUMBER = re.compile(
    r"^\s*(?P<open>\()?\s*(?:[$€£¥]\s*)?(?P<number>[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|[+-]?\.\d+)\s*(?P<percent>%?)\s*(?P<close>\))?\s*$"
)


def pdf_table_provenance(table, *, row_index=None, column_index=None, row_indices=None):
    """Project verified PDF coordinates without copying the whole candidate table."""
    provenance = table.get("provenance")
    if not isinstance(provenance, dict):
        return None
    coordinate = provenance.get("coordinate_table")
    manifest = provenance.get("manifest")
    if not isinstance(coordinate, dict) or not isinstance(manifest, dict):
        return None
    common = {
        "schema_version": "pdf-table-cell-locator/1",
        **{key: deepcopy(provenance.get(key)) for key in (
            "capability_status", "semantic_status", "authorization_status", "locator_basis"
        )},
        "manifest": deepcopy(manifest),
        **{key: deepcopy(coordinate.get(key)) for key in (
            "source_sha256", "parser_version", "structure_hash", "physical_page", "page_bbox",
            "title", "unit_line", "fiscal_period_header", "year_header",
        ) if key in coordinate},
    }
    rows = coordinate.get("rows")
    columns = coordinate.get("columns")
    if not isinstance(rows, list) or not isinstance(columns, list):
        raise DocumentError("unresolved_table_cell", "PDF coordinate table is incomplete")

    def target(full_row_index, full_column_index):
        # Formal PDF tables synthesize row 0 / column 0 from the selected header
        # and row label. Numeric source coordinates therefore begin at 1 / 1.
        source_row_index, source_column_index = full_row_index - 1, full_column_index - 1
        if (source_row_index < 0 or source_column_index < 0
                or source_row_index >= len(rows) or source_column_index >= len(columns)):
            raise DocumentError("unresolved_table_cell", "PDF cell lacks a verified coordinate mapping")
        source_row = rows[source_row_index]
        cells = source_row.get("cells") if isinstance(source_row, dict) else None
        if not isinstance(cells, list) or source_column_index >= len(cells):
            raise DocumentError("unresolved_table_cell", "PDF cell lacks a verified coordinate mapping")
        return {
            "row_index": full_row_index,
            "column_index": full_column_index,
            "header": deepcopy(columns[source_column_index]),
            "row": {key: deepcopy(source_row.get(key)) for key in (
                "label", "original_label_words", "original_row_words", "bbox"
            ) if key in source_row},
            "cell": deepcopy(cells[source_column_index]),
        }

    if row_index is not None and column_index is not None:
        common["target"] = target(row_index, column_index)
    elif row_indices is not None:
        common["visible_rows"] = [
            {
                "row_index": index,
                "row": {key: deepcopy(rows[index - 1].get(key)) for key in (
                    "label", "original_label_words", "original_row_words", "bbox"
                ) if key in rows[index - 1]},
                "cells": [deepcopy(cell) for cell in rows[index - 1].get("cells", [])],
            }
            for index in row_indices if 0 < index <= len(rows)
        ]
        common["columns"] = deepcopy(columns)
    return common


def raw_table_decimal(value):
    if not isinstance(value, str) or len(value) > 200:
        raise DocumentError("unresolved_table_cell", "table cell is not a bounded text value")
    match = _RAW_NUMBER.fullmatch(value)
    if match is None or bool(match["open"]) != bool(match["close"]):
        raise DocumentError("unresolved_table_cell", "table cell must contain one numeric presentation without embedded unit words")
    try:
        number = Decimal(match["number"].replace(",", ""))
    except InvalidOperation as exc:
        raise DocumentError("unresolved_table_cell", "table cell is not a finite decimal") from exc
    if match["open"]:
        if number < 0:
            raise DocumentError("unresolved_table_cell", "table cell has conflicting negative signs")
        number = -number
    if not number.is_finite():
        raise DocumentError("unresolved_table_cell", "table cell is not a finite decimal")
    return number, "%" if match["percent"] else None


def extract_table_fact(
    table, company_id, *, row_index, column_index, header_row_indices,
    row_label_column_index, expected_value, metric, unit, period_start,
    period_end, period_kind, basis, metadata_mode, unit_evidence_text=None,
    period_evidence_text=None, basis_evidence_text=None,
):
    rows = table.get("rows", [])
    if (type(row_index) is not int or type(column_index) is not int
            or row_index < 0 or column_index < 0 or row_index >= len(rows)
            or column_index >= len(rows[row_index])):
        raise DocumentError("unresolved_table_cell", "target cell does not exist")
    if (not isinstance(header_row_indices, list) or not 1 <= len(header_row_indices) <= 5
            or any(type(item) is not int or item < 0 or item >= row_index for item in header_row_indices)
            or len(set(header_row_indices)) != len(header_row_indices)):
        raise DocumentError("invalid_arguments", "header_row_indices must identify 1..5 unique rows before the target")
    if (type(row_label_column_index) is not int or row_label_column_index < 0
            or row_label_column_index >= len(rows[row_index]) or row_label_column_index == column_index):
        raise DocumentError("unresolved_table_cell", "row label cell does not exist or is the target cell")
    raw_value = rows[row_index][column_index]
    row_label = rows[row_index][row_label_column_index].strip()
    if not row_label:
        raise DocumentError("unresolved_table_cell", "row label is empty")
    cell_value, raw_unit_marker = raw_table_decimal(raw_value)
    expected_value = _text(expected_value, "expected_value", 200)
    if expected_value == raw_value.strip():
        expected = cell_value
    elif re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", expected_value):
        expected = Decimal(expected_value)
    else:
        raise DocumentError("invalid_arguments", "expected_value must be a signed decimal or this cell's exact raw_value")
    if not expected.is_finite() or expected != cell_value:
        raise DocumentError("numeric_mismatch", "expected_value does not equal the selected table cell")
    metric, unit, basis = (_text(value, name, 200)
                           for value, name in ((metric, "metric"), (unit, "unit"), (basis, "basis")))
    period_kind = _text(period_kind, "period_kind", 30)
    if period_kind not in {"instant", "quarter", "half_year", "nine_months", "year", "other"}:
        raise DocumentError("invalid_arguments", "period_kind is not supported")
    if not isinstance(period_start, str) or not isinstance(period_end, str):
        raise DocumentError("missing_period", "period_start and period_end must be complete ISO dates")
    period_start, period_end = period_start.strip(), period_end.strip()
    try:
        start_date, end_date = date.fromisoformat(period_start), date.fromisoformat(period_end)
    except ValueError as exc:
        raise DocumentError("missing_period", "period_start and period_end must be complete ISO dates") from exc
    if start_date > end_date or (period_kind == "instant" and start_date != end_date):
        raise DocumentError("missing_period", "period range is inconsistent with period_kind")
    if not isinstance(metadata_mode, str) or metadata_mode not in {"table_proven", "explicit_interpretation"}:
        raise DocumentError("invalid_arguments", "metadata_mode is not supported")
    headers = [rows[index] for index in header_row_indices]
    evidence_pool = [cell for row in headers for cell in row] + [row_label]
    table_provenance = table.get("provenance")
    coordinate = table_provenance.get("coordinate_table") if isinstance(table_provenance, dict) else None
    if isinstance(coordinate, dict):
        # These are verified source strings only. Presence supports the existing
        # table_proven string check; it does not approve economic meaning or dates.
        evidence_pool.extend(
            item["raw_text"] for item in (
                coordinate.get("unit_line"), coordinate.get("fiscal_period_header")
            ) if isinstance(item, dict) and isinstance(item.get("raw_text"), str)
        )
    evidence = {}
    for name, supplied in (("unit", unit_evidence_text), ("period", period_evidence_text), ("basis", basis_evidence_text)):
        if metadata_mode == "table_proven":
            supplied = _text(supplied, f"{name}_evidence_text", 200)
            if not any(supplied in candidate for candidate in evidence_pool):
                raise DocumentError("unresolved_metadata", f"{name} evidence text is absent from selected headers/row label")
            evidence[name] = {"status": "table_proven", "text": supplied}
        else:
            evidence[name] = {"status": "explicit_interpretation", "text": None}
    locator = {
        "type": "document_table_cell", "document_version_id": table["document_version_id"],
        "source_sha256": table["source_sha256"], "parser_version": table["parser_version"],
        "table_index": table["table_index"], "table_sha256": table["table_sha256"],
        "row_index": row_index, "column_index": column_index,
        "row_span": {"start": table["row_spans"][row_index][0], "end": table["row_spans"][row_index][1]},
        "header_row_indices": list(header_row_indices), "row_label_column_index": row_label_column_index,
    }
    pdf_provenance = pdf_table_provenance(
        table, row_index=row_index, column_index=column_index)
    if pdf_provenance is not None:
        locator["pdf_provenance"] = pdf_provenance
    identity = {
        "origin": ORIGIN, "company_id": company_id, "metric": metric, "value": format(cell_value, "f"), "unit": unit,
        "period_start": period_start, "period_end": period_end, "period_kind": period_kind, "basis": basis,
        "raw_value": raw_value, "row_label": row_label, "headers": headers, "metadata": evidence, "locator": locator,
    }
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    fact = {
        **{key: value for key, value in identity.items() if key != "metadata"},
        "fact_id": f"docfact-{digest}", "evidence_id": f"doccell-{digest}",
        "source_id": table["document_version_id"], "raw_unit_marker": raw_unit_marker,
        "metadata_provenance": evidence,
    }
    if pdf_provenance is not None:
        fact["source_rights_status"] = table.get("rights_status")
    return {
        "company_id": company_id, "fact": fact,
        "document_version": {
            **{key: table.get(key) for key in (
                "document_version_id", "source_sha256", "parser_version", "title", "source_url")},
            **({"rights_status": table.get("rights_status")} if pdf_provenance is not None else {}),
        },
        "table_sha256": table["table_sha256"],
        "interpretation": (
            "Table labels prove the supplied metadata strings; semantic mapping still requires review."
            if metadata_mode == "table_proven" else
            "Metric, unit, period, and basis are explicit requested interpretations, not issuer-proven table metadata."
        ),
        "limitations": list(LIMITATIONS),
    }


def with_provenance(result, descriptor):
    fact = result["fact"]
    locator = fact["locator"]
    states = {item["status"] for item in fact["metadata_provenance"].values()}
    return {**result, "historical_provenance": {
        "schema_version": "document-table-cell-provenance/1",
        "source": deepcopy(descriptor),
        "published_table": {
            "table_index": locator["table_index"], "table_sha256": locator["table_sha256"],
            "locator": {key: locator[key] for key in (
                "row_index", "column_index", "row_span", "header_row_indices", "row_label_column_index")},
            **({"pdf_provenance": deepcopy(locator["pdf_provenance"])}
               if "pdf_provenance" in locator else {}),
        },
        "interpretation": {"state": next(iter(states)) if len(states) == 1 else "unsupported",
                           "semantic_verified": False, "statement": result["interpretation"]},
    }}


def validate_document_fact(*, snapshot, company_id, documents, result, expected_fact=None):
    """Re-execute the recorded L2 operation, not a second hand-written validator."""
    if (not isinstance(snapshot, dict) or snapshot.get("authoritative") is not True
            or snapshot.get("truncated") is True or not isinstance(result, dict)
            or not isinstance(result.get("fact"), dict)):
        raise DocumentError("citation_source_mismatch", "document-cell derivation is incomplete")
    fact = result["fact"]
    locator, metadata = fact.get("locator"), fact.get("metadata_provenance")
    if (not isinstance(locator, dict) or not isinstance(metadata, dict)
            or set(metadata) != {"unit", "period", "basis"}
            or any(not isinstance(item, dict) for item in metadata.values())
            or (expected_fact is not None and fact != expected_fact)):
        raise DocumentError("citation_source_mismatch", "document-cell input or metadata differs")
    versions = snapshot.get("documents")
    if (not isinstance(versions, dict) or not isinstance(versions.get("coverage"), dict)
            or not isinstance(versions["coverage"].get("documents"), list)
            or not isinstance(versions.get("document_version_ids"), list)):
        raise DocumentError("citation_source_mismatch", "frozen document coverage is incomplete")
    descriptor = next((item for item in versions["coverage"]["documents"]
                       if isinstance(item, dict) and item.get("document_version_id") == locator.get("document_version_id")), None)
    if (descriptor is None or locator.get("document_version_id") not in versions.get("document_version_ids", [])
            or fact.get("company_id") != company_id):
        raise DocumentError("citation_source_mismatch", "document-cell source is outside the frozen scope")
    if documents is None:
        raise DocumentError("source_snapshot_unavailable", "document source service is unavailable")
    try:
        try:
            raw = documents.read_raw_version(company_id=company_id, document_version_id=locator["document_version_id"])
            frozen = documents.read_frozen_version(company_id=company_id, document_version_id=locator["document_version_id"])
        except DocumentError as exc:
            raise DocumentError("citation_source_mismatch", f"Frozen document source is unavailable: {exc.message}") from exc
        fields = ("document_version_id", "source_key", "source_version", "title", "source_url",
                  "source_sha256", "published_at", "collected_at", "media_type", "rights_status",
                  "parse_status", "parse_error", "supersedes_document_version_id", "parser_version")
        if (hashlib.sha256(raw).hexdigest() != descriptor.get("source_sha256")
                or any(frozen.get(key) != descriptor.get(key) for key in fields)):
            raise DocumentError("citation_source_mismatch", "frozen source bytes or metadata differ")
        table = next((item for item in frozen["tables"] if item["table_index"] == locator.get("table_index")), None)
        if (table is None or not any(item.get("table_index") == table["table_index"]
                                    and item.get("table_sha256") == table["table_sha256"]
                                    for item in descriptor.get("tables", []))):
            raise DocumentError("citation_source_mismatch", "published table identity differs")
        replayed = extract_table_fact(
            {**frozen, **table}, company_id,
            **{key: locator.get(key) for key in ("row_index", "column_index", "header_row_indices", "row_label_column_index")},
            **{key: fact.get(key) for key in ("metric", "unit", "basis", "period_start", "period_end", "period_kind")},
            expected_value=fact.get("value"), metadata_mode=metadata["unit"].get("status"),
            **{f"{key}_evidence_text": item.get("text") for key, item in metadata.items()},
        )
        recorded = {key: value for key, value in result.items() if not key.startswith("_") and key != "reused_from"}
        replayed = with_provenance(replayed, descriptor)
        if replayed != recorded:
            raise DocumentError("citation_source_mismatch", "recorded extraction does not match its replay")
    except DocumentError as exc:
        raise DocumentError("citation_source_mismatch", exc.message) from exc
    exact_table = {key: table.get(key) for key in ("table_index", "table_sha256", "parser_version", "locator", "rows", "row_spans")}
    if "pdf_provenance" in locator:
        exact_table["provenance"] = deepcopy(locator["pdf_provenance"])
    exact_table["target_cell"] = {
        "row_index": locator["row_index"], "column_index": locator["column_index"],
        "raw_value": fact["raw_value"], "value": fact["value"], "row_label": fact["row_label"],
        "headers": fact["headers"], "row_header": fact["row_label"],
        "column_headers": [row[locator["column_index"]] for row in fact["headers"]
                           if locator["column_index"] < len(row) and row[locator["column_index"]].strip()],
        "unit_header": next((cell for row in fact["headers"] for cell in row
                             if metadata["unit"]["status"] == "table_proven"
                             and metadata["unit"]["text"] in cell), None),
    }
    summary = {
        "fact_id": fact["fact_id"], "evidence_id": fact["evidence_id"],
        "source_id": locator["document_version_id"], "document_version_id": locator["document_version_id"],
        **{key: descriptor.get(key) for key in ("title", "source_url", "published_at", "collected_at", "rights_status", "source_sha256")},
        "quote": fact["raw_value"], "locator": locator, "table": exact_table,
        "interpretation": replayed["historical_provenance"]["interpretation"],
    }
    if "reused_from" in result:
        summary["reused_from"] = dict(result["reused_from"])
    if descriptor.get("source_scope") == "session_material":
        summary.update(source_scope="session_material", conversation_id=descriptor.get("conversation_id"))
    return fact, summary
