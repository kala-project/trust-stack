"""Explicit report bindings and immutable publication records, separate from Markdown inference."""
from contextlib import closing
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from datetime import date, datetime, timezone
import hashlib
import json
import math
import re

from .research_tools import ToolError
from .publication_contracts import CanonicalPublication, PublicationContractError


VERSION = "structured-research-report/1"
TIME_AXIS_VERSION = "utc-period-axis/1"
KINDS = ("source_statement", "recorded_calculation", "interpretation", "evidence_gap")
REFERENCE_KINDS = ("claim", "evidence", "passage", "calculation", "query")
VALUE_FORMATIONS = ("observed", "estimated", "assumed", "derived")
PRINCIPAL_TYPES = ("person", "organization", "collective", "system")
_DISPLAY_UNIT_LABELS = {"USD millions": "USD millions", "USD million": "USD millions", "USD": "USD",
                        "USD billions": "USD billions", "USD/share": "USD/share",
                        "%": "%", "percent": "%", "percentage points": "percentage points", "x": "x"}
_MARKER = re.compile(r"\[(?:claim|evidence|passage|calculation|fact|query):")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS report_publications(
 message_id TEXT PRIMARY KEY REFERENCES messages(message_id),
 run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
 schema_version TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 payload_sha256 TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS report_publications_no_update
BEFORE UPDATE ON report_publications BEGIN SELECT RAISE(ABORT,'publications are immutable'); END;
CREATE TRIGGER IF NOT EXISTS report_publications_no_delete
BEFORE DELETE ON report_publications BEGIN SELECT RAISE(ABORT,'publications are immutable'); END;
"""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _text(value, name, maximum=4000, empty=False):
    if (not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip())
            or _MARKER.search(value)):
        raise ToolError("invalid_report", f"{name} must be bounded text without manually embedded citation markers")
    return value


def _reference(value, refs):
    if (not isinstance(value, dict) or set(value) != {"kind", "id"}
            or value.get("kind") not in REFERENCE_KINDS or not isinstance(value.get("id"), str)
            or value["id"] not in refs.get(value["kind"], {})):
        raise ToolError("invalid_report_reference", "Every report reference must have been obtained in this run")
    return dict(value)


def _attribution(value, refs):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"principal", "stated_at", "statement_reference"}:
        raise ToolError("invalid_report_attribution", "Attribution needs principal, stated_at and statement_reference")
    principal = value.get("principal")
    if (not isinstance(principal, dict) or set(principal) != {"type", "identifier", "label"}
            or principal.get("type") not in PRINCIPAL_TYPES):
        raise ToolError("invalid_report_attribution", "Attribution principal needs a supported type, stable identifier and label")
    identifier = _text(principal.get("identifier"), "attribution.principal.identifier", 200)
    label = _text(principal.get("label"), "attribution.principal.label", 200)
    stated_at = value.get("stated_at")
    if not isinstance(stated_at, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stated_at):
        raise ToolError("invalid_report_attribution", "attribution.stated_at must be an ISO UTC timestamp with second precision")
    try:
        parsed = datetime.fromisoformat(stated_at.replace("Z", "+00:00"))
        if parsed.tzinfo != timezone.utc:
            raise ValueError("not UTC")
    except ValueError as exc:
        raise ToolError("invalid_report_attribution", "attribution.stated_at must be a valid ISO UTC timestamp") from exc
    return {
        "principal": {"type": principal["type"], "identifier": identifier, "label": label},
        "stated_at": stated_at,
        "statement_reference": _reference(value.get("statement_reference"), refs),
    }


def _value_semantics(part, refs):
    formation = part.get("value_formation")
    if formation not in VALUE_FORMATIONS:
        raise ToolError("invalid_report_value", "Every value needs an explicit supported value_formation")
    kind = part.get("kind")
    attribution = _attribution(part.get("attribution"), refs)
    if kind == "calculation":
        if formation != "derived":
            raise ToolError("invalid_report_value", "A calculation binding must have value_formation=derived")
        if attribution is not None:
            raise ToolError("invalid_report_attribution", "A calculation result cannot carry statement attribution")
    elif kind in {"fact", "query_cell"} and formation == "derived":
        raise ToolError("invalid_report_value", "A fact or query cell cannot have value_formation=derived")
    return formation, attribution


def _value_text(value, unit, value_style="recorded"):
    if value_style == "recorded":
        return f"{value} {unit}"
    # Only display aliases, never amount conversion or rounding. Canonical
    # value/unit remain in the binding and every calculation record.
    try:
        number = Decimal(value)
        if not number.is_finite() or len(number.as_tuple().digits) + abs(number.as_tuple().exponent) > 4000:
            raise ValueError("display number exceeds bounds")
        shown = format(number, ",f")
    except (InvalidOperation, ValueError) as exc:
        raise ToolError("invalid_report_value", "Localized display needs a finite bounded decimal") from exc
    label = _DISPLAY_UNIT_LABELS.get(unit, unit)
    return f"{shown}{'' if label == '%' else ' '}{label}"


def _bound_value(part, facts, calculations, refs, value_style="recorded"):
    kind, identifier = part.get("kind"), part.get("id")
    if not isinstance(identifier, str):
        raise ToolError("invalid_report_value", "A value needs an actual fact or calculation ID")
    value_formation, attribution = _value_semantics(part, refs)
    if kind == "query_cell":
        from .native_tools import cell_binding
        _reference({"kind": "query", "id": identifier}, refs)
        return {**cell_binding(part, refs["query"][identifier]["record"]),
                "value_formation": value_formation, "attribution": attribution}
    record = (facts if kind == "fact" else calculations).get(identifier)
    if not isinstance(record, dict):
        raise ToolError("invalid_report_value", "The requested value was not obtained in this research")
    reference = ({"kind": "evidence", "id": record.get("evidence_id")} if kind == "fact"
                 else {"kind": "calculation", "id": identifier})
    reference = _reference(reference, refs)
    if not isinstance(record.get("value"), str) or not isinstance(record.get("unit"), str):
        raise ToolError("invalid_report_value", "The selected record has no typed value and unit")
    return {"kind": kind, "id": identifier, "value": record["value"], "unit": record["unit"],
            "value_formation": value_formation, "attribution": attribution,
            "reference": reference,
            "dimensions": _dimensions(record),
            "text": _value_text(record["value"], record["unit"], value_style),
            **({"display_unit": _DISPLAY_UNIT_LABELS.get(record["unit"], record["unit"])}
               if value_style == "zh-CN" else {})}


def _dimensions(record):
    context = record.get("arithmetic", {}).get("output_context", record)
    return {**{key: context.get(key) for key in (
        "company_id", "metric", "period_start", "period_end", "period_kind", "basis")},
        **({"company_ids": deepcopy(context["company_ids"])} if "company_ids" in context else {}),
        **({"periods": deepcopy(context["periods"])} if "periods" in context else {})}


def _iso_date(value, name):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ToolError("incompatible_time_axis", f"{name} needs a complete ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ToolError("incompatible_time_axis", f"{name} is not a valid calendar date") from exc


def _time_axis(points, requested):
    """Validate the deliberately narrow first real-time-series contract."""
    if requested != "period_end":
        raise ToolError("invalid_report_dataset", "A line time_axis must explicitly select period_end")
    records = []
    for point in points:
        binding, dimensions = point["binding"], point["binding"]["dimensions"]
        if binding["kind"] != "fact":
            raise ToolError("incompatible_time_axis", "A real time series accepts direct fact points only")
        required = {key: dimensions.get(key) for key in ("company_id", "metric", "basis", "period_kind")}
        if any(not isinstance(value, str) or not value.strip() for value in required.values()):
            raise ToolError("incompatible_time_axis", "Every time-series fact needs company, metric, basis and period kind")
        if required["period_kind"] not in {"year", "quarter"}:
            raise ToolError("incompatible_time_axis", "A time series accepts only annual or quarterly facts")
        if "periods" in dimensions:
            raise ToolError("incompatible_time_axis", "Cross-period records are not time-series observations")
        start = _iso_date(dimensions.get("period_start"), "period_start")
        end = _iso_date(dimensions.get("period_end"), "period_end")
        if start > end:
            raise ToolError("incompatible_time_axis", "A fact period cannot end before it starts")
        if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", binding["value"].strip()):
            raise ToolError("incompatible_time_axis", "A time-series value needs plain finite decimal notation")
        try:
            exact = Decimal(binding["value"])
            numeric = float(exact)
        except (InvalidOperation, ValueError, OverflowError) as exc:
            raise ToolError("incompatible_time_axis", "A time-series value must be a finite decimal") from exc
        if (not exact.is_finite() or not math.isfinite(numeric) or abs(numeric) > 9007199254740991
                or (numeric == 0 and exact != 0)):
            raise ToolError("incompatible_time_axis", "A time-series value is outside the safe chart range")
        records.append((start, end, required, binding["unit"]))
    for key in ("company_id", "metric", "basis", "period_kind"):
        if len({record[2][key] for record in records}) != 1:
            raise ToolError("incompatible_time_axis", f"Time-series facts have inconsistent {key}")
    if len({record[3] for record in records}) != 1:
        raise ToolError("incompatible_time_axis", "Time-series facts have inconsistent unit")
    ordered = sorted(records, key=lambda record: record[1])
    if len({record[1] for record in ordered}) != len(ordered):
        raise ToolError("incompatible_time_axis", "Time-series facts have duplicate period_end dates")
    if any(current[0] <= previous[1] for previous, current in zip(ordered, ordered[1:])):
        raise ToolError("incompatible_time_axis", "Time-series fact periods overlap")
    return {"version": TIME_AXIS_VERSION, "field": "period_end", "timezone": "UTC", "gap_policy": "break"}


def _matrix_presentation(value, *, dataset_index, point_count, block_count, allowed_units):
    """Validate an explicit point-to-cell map without inferring semantics from labels."""
    if not isinstance(value, dict) or set(value) - {"columns", "rows", "after_block_id", "unit", "note"}:
        raise ToolError("invalid_report_dataset", "Dataset presentation has unknown fields")
    if not {"columns", "rows", "after_block_id"} <= set(value):
        raise ToolError("invalid_report_dataset", "Dataset presentation needs columns, rows and after_block_id")
    after_block_id = value["after_block_id"]
    if after_block_id not in {f"report-block-{index}" for index in range(1, block_count + 1)}:
        raise ToolError("invalid_report_dataset", "Dataset presentation refers to an unknown narrative block")
    columns, rows = value["columns"], value["rows"]
    if (not isinstance(columns, list) or not 1 <= len(columns) <= 20
            or not isinstance(rows, list) or not 1 <= len(rows) <= 50):
        raise ToolError("invalid_report_dataset", "Matrix presentation needs 1..20 columns and 1..50 rows")

    def axis_item(item, name):
        if not isinstance(item, dict) or set(item) != {"key", "label"}:
            raise ToolError("invalid_report_dataset", f"Every matrix {name} needs an explicit key and label")
        key = _text(item["key"], f"presentation.{name}.key", 80)
        label = _text(item["label"], f"presentation.{name}.label", 200)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", key):
            raise ToolError("invalid_report_dataset", f"Matrix {name} keys must be stable identifiers")
        if any(char in label for char in "\r\n|"):
            raise ToolError("invalid_report_dataset", f"Matrix {name} labels cannot contain table separators")
        return {"key": key, "label": label}

    prepared_columns = [axis_item(column, "column") for column in columns]
    if len({column["key"] for column in prepared_columns}) != len(prepared_columns):
        raise ToolError("invalid_report_dataset", "Matrix column keys must be unique")
    prepared_rows, used = [], []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"key", "label", "cells"}:
            raise ToolError("invalid_report_dataset", "Every matrix row needs a key, label and cells")
        prepared_row = axis_item({"key": row["key"], "label": row["label"]}, "row")
        cells = row["cells"]
        if not isinstance(cells, list) or len(cells) != len(prepared_columns):
            raise ToolError("invalid_report_dataset", (
                f"Matrix row {prepared_row['key']} must map {len(prepared_columns)} numeric cells, one per column. "
                "The row label is rendered separately as the leading header; do not duplicate it in columns."
            ))
        prepared_cells = []
        for cell in cells:
            if not isinstance(cell, dict) or set(cell) != {"point_index"} or type(cell["point_index"]) is not int:
                raise ToolError("invalid_report_dataset", "Every numeric matrix cell must map one point_index")
            point_index = cell["point_index"]
            if point_index < 0 or point_index >= point_count:
                raise ToolError("invalid_report_dataset", "Matrix cell point_index is outside the bound dataset")
            used.append(point_index)
            prepared_cells.append({"point_index": point_index,
                                   "point_id": f"report-dataset-{dataset_index}-point-{point_index + 1}"})
        prepared_rows.append({**prepared_row, "cells": prepared_cells})
    if len({row["key"] for row in prepared_rows}) != len(prepared_rows):
        raise ToolError("invalid_report_dataset", "Matrix row keys must be unique")
    if sorted(used) != list(range(point_count)):
        raise ToolError("invalid_report_dataset", "Matrix must map every bound point exactly once")
    unit = _text(value["unit"], "presentation.unit", 200) if "unit" in value else None
    if unit is not None and unit not in allowed_units:
        raise ToolError("invalid_report_dataset", "Matrix unit must match one unit saved on every bound point")
    return {
        "type": "matrix", "after_block_id": after_block_id,
        "columns": prepared_columns, "rows": prepared_rows,
        **({"unit": unit} if unit is not None else {}),
        **({"note": _text(value["note"], "presentation.note", 1000)} if "note" in value else {}),
    }


def prepare(*, title, blocks, refs, facts, calculations, max_chars, datasets=None, value_style="recorded"):
    if not isinstance(value_style, str) or value_style not in {"recorded", "zh-CN"}:
        raise ToolError("invalid_report", "value_style must be recorded or zh-CN")
    title = _text(title, "title", 200)
    if "\n" in title or "\r" in title:
        raise ToolError("invalid_report", "Report title must fit one line")
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 80:
        raise ToolError("invalid_report", "A report needs 1..80 explicit blocks")
    if datasets is None:
        datasets = []
    if not isinstance(datasets, list) or len(datasets) > 10:
        raise ToolError("invalid_report", "Report datasets must contain at most ten definitions")
    blocks = deepcopy(blocks)
    narrative_block_count = len(blocks)
    prepared_datasets = []
    for index, dataset in enumerate(datasets):
        if (not isinstance(dataset, dict)
                or set(dataset) - {"title", "description", "display", "points", "presentation", "time_axis", "support"}
                or not {"title", "description", "display", "points"} <= set(dataset)
                or dataset["display"] not in ("table", "bar", "line", "scatter")
                or not isinstance(dataset["points"], list) or not 1 <= len(dataset["points"]) <= 50):
            raise ToolError("invalid_report_dataset", "Dataset needs an explicit title, purpose, display and 1..50 bound points")
        dataset_title = _text(dataset["title"], "dataset.title", 200)
        description = _text(dataset["description"], "dataset.description", 1000)
        extra_support = dataset.get("support", [])
        if not isinstance(extra_support, list) or len(extra_support) > 16:
            raise ToolError("invalid_report_dataset", "Dataset description support needs at most 16 current-run references")
        extra_support = [_reference(item, refs) for item in extra_support]
        points = []
        point_parts = []
        parts = [{"kind": "text", "text": f"### {dataset_title}\n\n{description}\n\n| Item | Value and source |\n|---|---|"}]
        for point in dataset["points"]:
            if (not isinstance(point, dict) or set(point) != {
                    "label", "kind", "id", "value_formation", "attribution"}
                    or point["kind"] not in ("fact", "calculation")):
                raise ToolError("invalid_report_dataset", "Every data point needs a label, typed record reference, value formation and attribution")
            label = _text(point["label"], "point.label", 200)
            if any(char in label for char in "\r\n|"):
                raise ToolError("invalid_report_dataset", "Point labels cannot contain table separators")
            binding = _bound_value(point, facts, calculations, refs, value_style)
            points.append({"label": label, "binding": binding})
            numeric_part = {key: deepcopy(point[key]) for key in (
                "kind", "id", "value_formation", "attribution")}
            point_parts.append(numeric_part)
            parts.extend([{"kind": "text", "text": f"\n| {label} | "},
                          deepcopy(numeric_part), {"kind": "text", "text": " |"}])
        if dataset["display"] in {"bar", "line", "scatter"}:
            if len(points) < 2:
                raise ToolError("invalid_report_dataset", "A comparative chart needs at least two points")
            for key in ("unit", "basis", "period_kind"):
                values = [point["binding"]["unit"] if key == "unit" else point["binding"]["dimensions"][key]
                          for point in points]
                if not all(isinstance(value, str) and value for value in values) or len(set(values)) != 1:
                    raise ToolError("incompatible_chart", f"Chart inputs have missing or inconsistent {key}; use an explicit table or reconcile inputs")
        time_axis = None
        if "time_axis" in dataset:
            if dataset["display"] != "line":
                raise ToolError("invalid_report_dataset", "time_axis is only valid for a line dataset")
            time_axis = _time_axis(points, dataset["time_axis"])
            parts[0]["text"] = (f"### {dataset_title}\n\n{description}\n\n"
                                "Timeline: fact period end dates (true UTC date distances); missing periods are not imputed and lines do not span gaps.\n\n"
                                "| Item | Value and source |\n|---|---|")
        presentation = None
        if "presentation" in dataset:
            if dataset["display"] != "table":
                raise ToolError("invalid_report_dataset", "Matrix presentation is only valid for an explicit table")
            point_units = [{point["binding"]["unit"], point["binding"].get("display_unit")}
                           - {None} for point in points]
            presentation = _matrix_presentation(
                dataset["presentation"], dataset_index=index + 1,
                point_count=len(points), block_count=narrative_block_count,
                allowed_units=set.intersection(*point_units))
            metadata = ""
            if "unit" in presentation:
                metadata += f'\n\nUnit: {presentation["unit"]}'
            if "note" in presentation:
                metadata += f'\n\nNote: {presentation["note"]}'
            column_labels = " | ".join(column["label"] for column in presentation["columns"])
            separators = "|".join("---" for _ in range(len(presentation["columns"]) + 1))
            parts = [{
                "kind": "text",
                "text": (f"### {dataset_title}\n\n{description}{metadata}\n\n"
                         f"| Item | {column_labels} |\n|{separators}|"),
            }]
            for matrix_row in presentation["rows"]:
                parts.append({"kind": "text", "text": f'\n| {matrix_row["label"]} | '})
                for cell_index, cell in enumerate(matrix_row["cells"]):
                    separator = " | " if cell_index + 1 < len(matrix_row["cells"]) else " |"
                    parts.extend([point_parts[cell["point_index"]], {"kind": "text", "text": separator}])
        prepared_datasets.append({"id": f"report-dataset-{index + 1}", "title": dataset_title,
                                  "description": description, "display": dataset["display"], "points": points,
                                  **({"time_axis": time_axis} if time_axis is not None else {}),
                                  **({"support": deepcopy(extra_support)} if "support" in dataset else {}),
                                  **({"presentation": presentation} if presentation is not None else {})})
        blocks.append({"classification": "recorded_calculation" if any(
            point["binding"]["kind"] == "calculation" for point in points) else "source_statement",
                       "parts": parts, "support": extra_support,
                       "reasoning": "", "limitations": ["Chart comparisons are explicitly selected by the researcher and do not establish economic comparability of the metrics."]})
    if len(blocks) > 80:
        raise ToolError("report_size_limit", "Report blocks and datasets exceed the combined publication limit")
    answer = "# " + title + "\n\n"
    prepared = []
    for index, block in enumerate(blocks):
        if (not isinstance(block, dict) or set(block) - {"title"} != {"classification", "parts", "support", "reasoning", "limitations"}
                or block.get("classification") not in KINDS
                or not isinstance(block["parts"], list) or not 1 <= len(block["parts"]) <= 160
                or not isinstance(block["support"], list) or len(block["support"]) > 16
                or not isinstance(block["limitations"], list) or len(block["limitations"]) > 12):
            raise ToolError("invalid_report", "Report block does not match the explicit publication contract")
        block_title = _text(block["title"], "block.title", 120) if "title" in block else None
        if block_title and ("\n" in block_title or "\r" in block_title):
            raise ToolError("invalid_report", "Report block title must fit one line")
        reasoning = _text(block["reasoning"], "reasoning", empty=True)
        limits = [_text(item, "limitation", 1000) for item in block["limitations"]]
        support = [_reference(item, refs) for item in block["support"]]
        start = len(answer)
        spans = []
        for part in block["parts"]:
            if not isinstance(part, dict):
                raise ToolError("invalid_report", "Report parts must be typed objects")
            if part.get("kind") == "text" and set(part) == {"kind", "text"}:
                rendered = _text(part["text"], "part.text")
                binding = None
            elif part.get("kind") in {"fact", "calculation"} and set(part) == {
                    "kind", "id", "value_formation", "attribution"}:
                binding = _bound_value(part, facts, calculations, refs, value_style)
                rendered = binding["text"]
                if binding["reference"] not in support:
                    support.append(binding["reference"])
                statement_reference = (binding["attribution"] or {}).get("statement_reference")
                if statement_reference is not None and statement_reference not in support:
                    support.append(statement_reference)
            elif part.get("kind") == "query_cell" and set(part) == {
                    "kind", "id", "row", "column", "value_formation", "attribution"}:
                binding = _bound_value(part, facts, calculations, refs, value_style)
                rendered = binding["text"]
                if binding["reference"] not in support:
                    support.append(binding["reference"])
                statement_reference = (binding["attribution"] or {}).get("statement_reference")
                if statement_reference is not None and statement_reference not in support:
                    support.append(statement_reference)
            else:
                raise ToolError("invalid_report", "Part needs text, a fact/calculation reference, or an exact native query cell")
            span_start = len(answer)
            if len(answer) + len(rendered) > max_chars:
                raise ToolError("report_size_limit", "Report exceeds the publication text limit")
            answer += rendered
            spans.append({"start": span_start, "end": len(answer), "text": rendered, "binding": binding})
            if binding:
                reference = binding["reference"]
                answer += f' [{reference["kind"]}:{reference["id"]}]'
        for reference in support:
            marker = f'[{reference["kind"]}:{reference["id"]}]'
            if marker not in answer[start:]:
                answer += " " + marker
        explanations = ([f"Reasoning (analytical judgment, not independently verified): {reasoning}"] if reasoning else [])
        explanations.extend(f"Limitation: {item}" for item in limits)
        for explanation in explanations:
            answer += "\n\n"
            span_start = len(answer)
            answer += explanation
            spans.append({"start": span_start, "end": len(answer), "text": explanation, "binding": None})
        end = len(answer)
        prepared.append({
            "id": f"report-block-{index + 1}", "classification": block["classification"],
            "start": start, "end": end, "text": answer[start:end], "spans": spans,
            "support": support, "reasoning": reasoning, "limitations": limits,
        })
        if block_title is not None:
            prepared[-1]["title"] = block_title
        answer += "\n\n"
    # Remove only our block separator, not source text covered by saved spans.
    answer = answer[:-2]
    if len(answer) > max_chars or len(_json(prepared).encode()) > 700_000:
        raise ToolError("report_size_limit", "Structured report exceeds the publication budget")
    try:
        publication = CanonicalPublication.decode({
            "schema_version": VERSION, "title": title, "answer": answer, "blocks": prepared,
            "datasets": prepared_datasets,
            **({"value_style": value_style} if value_style != "recorded" else {}),
        })
    except PublicationContractError as exc:
        raise ToolError("invalid_report", str(exc)) from exc
    return publication.to_json()


def reference_dependencies(publication, node_id):
    """Read declared support and binding.reference, never recursively infer citations.

    A fact/query_cell binding identifies a value, not an additional evidence object.
    Its explicit reference is the citation identity used by every consumer.
    """
    if node_id == "report-title":
        return ()
    targets = {
        block.get("block_id") or f"report-block-{index}": block
        for index, block in enumerate(publication.get("blocks", []), 1)
    }
    targets.update({f"report-dataset-{index}": dataset
                    for index, dataset in enumerate(publication.get("datasets", []), 1)})
    if node_id not in targets:
        raise ToolError("invalid_report_reference", "Unknown publication content node")
    target = targets[node_id]
    values = []
    def add(reference):
        if (not isinstance(reference, dict)
                or reference.get("kind") not in {"claim", "evidence", "passage", "query", "calculation"}
                or not isinstance(reference.get("id"), str) or not reference["id"]):
            raise ToolError("invalid_report_reference", "Publication has an invalid explicit evidence reference")
        values.append((reference["kind"], reference["id"]))
    for reference in target.get("support", []):
        add(reference)
    for item in [*target.get("spans", []), *target.get("points", [])]:
        binding = item.get("binding")
        if binding is not None:
            add(binding.get("reference") if isinstance(binding, dict) else None)
            attribution = binding.get("attribution") if isinstance(binding, dict) else None
            if attribution is not None:
                add(attribution.get("statement_reference"))
    return tuple(dict.fromkeys(values))


def validate_bindings(publication, reviewer, replay_native_query=None):
    """Check declared records, not the language used to describe their significance."""
    native = {}
    replay_native_query = (replay_native_query if replay_native_query is not None else
                           getattr(reviewer, "replay_native_query", None))
    style = publication.get("value_style", "recorded")
    if not isinstance(style, str) or style not in {"recorded", "zh-CN"}:
        raise ToolError("invalid_report", "Unknown publication value style")

    def check_value(binding):
        kind = binding.get("kind")
        if kind not in {"fact", "calculation"}:
            raise ToolError("report_binding_mismatch", "Unknown quantitative binding")
        record = reviewer.indexes["facts" if kind == "fact" else "calculations"].get(binding["id"])
        reference = ({"kind": "evidence", "id": record.get("evidence_id")} if record and kind == "fact"
                     else {"kind": "calculation", "id": binding["id"]})
        if (record is None or record.get("value") != binding["value"] or record.get("unit") != binding["unit"]
                or _dimensions(record) != binding["dimensions"] or reference != binding.get("reference")
                or (style == "zh-CN" and binding.get("display_unit") != _DISPLAY_UNIT_LABELS.get(record["unit"], record["unit"]))
                or binding["text"] != _value_text(record["value"], record["unit"], style)):
            raise ToolError("report_binding_mismatch", "Report value differs from the verified source or computation")
        authoritative = reviewer.read_reference_record(reference["kind"], reference["id"])
        if authoritative.state != "available" or authoritative.record is None:
            raise ToolError("invalid_report_reference", "Report value lacks an authoritative tool result")

    for block in publication["blocks"]:
        for reference in block["support"]:
            authoritative = reviewer.read_reference_record(reference["kind"], reference["id"])
            record = authoritative.record
            if authoritative.state != "available" or record is None:
                raise ToolError("invalid_report_reference", "Report source lacks an authoritative tool result")
            if reference["kind"] == "query" and reference["id"] not in native:
                from .native_sql import NativeSqlError
                if not callable(replay_native_query):
                    raise ToolError(
                        "native_source_unavailable",
                        "Native query replay capability is unavailable")
                try:
                    replay = replay_native_query(reference["id"])
                except NativeSqlError as exc:
                    raise ToolError(exc.code, exc.message) from exc
                canonical = {key: value for key, value in record.items() if not key.startswith("_")}
                if replay["query"] != canonical:
                    raise ToolError("native_record_integrity", "Native result differs from its authoritative tool event")
                native[reference["id"]] = replay["query"]
        for span in block["spans"]:
            binding = span["binding"]
            if binding is None:
                continue
            if binding["kind"] == "query_cell":
                from .native_tools import cell_binding
                replayed = {**cell_binding(binding, native[binding["id"]]),
                            "value_formation": binding["value_formation"],
                            "attribution": deepcopy(binding["attribution"])}
                if replayed != binding:
                    raise ToolError("report_binding_mismatch", "Native cell differs from its persisted, replayed result")
                continue
            check_value(binding)
    # Charts consume these independent bindings, not the generated prose spans.
    for dataset in publication.get("datasets", []):
        for point in dataset["points"]:
            check_value(point["binding"])


class ReportPublications:
    def __init__(self, connect):
        self.connect = connect
        with closing(connect()) as connection:
            connection.executescript(_SCHEMA)

    @staticmethod
    def save(connection, *, message_id, run_id, publication, semantic_review):
        payload = {**publication, "semantic_review": deepcopy(semantic_review)}
        raw = _json(payload)
        connection.execute("INSERT INTO report_publications VALUES(?,?,?,?,?)",
                           (message_id, run_id, VERSION, raw, hashlib.sha256(raw.encode()).hexdigest()))

    def read(self, answer):
        with closing(self.connect()) as connection:
            row = connection.execute("SELECT * FROM report_publications WHERE message_id=?",
                                     (answer["message_id"],)).fetchone()
        if row is None:
            if answer.get("publication_schema") is not None:
                raise ToolError("publication_missing", "Structured publication is missing; legacy inference is not permitted")
            return None
        if (row["run_id"] != answer["run_id"] or row["schema_version"] != VERSION
                or hashlib.sha256(row["payload_json"].encode()).hexdigest() != row["payload_sha256"]):
            raise ToolError("publication_integrity_mismatch", "Saved publication identity or checksum differs")
        try:
            payload = json.loads(row["payload_json"])
        except (ValueError, TypeError) as exc:
            raise ToolError("publication_integrity_mismatch", "Saved publication is invalid") from exc
        if payload.get("schema_version") != VERSION or payload.get("answer") != answer["answer"]:
            raise ToolError("publication_integrity_mismatch", "Saved report differs from its original published text")
        return payload

    @staticmethod
    def definition(handler):
        reference = {"type": "object", "additionalProperties": False, "required": ["kind", "id"],
                     "properties": {"kind": {"type": "string", "enum": list(REFERENCE_KINDS)},
                                    "id": {"type": "string", "minLength": 1, "maxLength": 200}}}
        attribution = {"oneOf": [
            {"type": "null"},
            {"type": "object", "additionalProperties": False,
             "required": ["principal", "stated_at", "statement_reference"],
             "properties": {
                 "principal": {"type": "object", "additionalProperties": False,
                               "required": ["type", "identifier", "label"],
                               "properties": {
                                   "type": {"type": "string", "enum": list(PRINCIPAL_TYPES)},
                                   "identifier": {"type": "string", "minLength": 1, "maxLength": 200},
                                   "label": {"type": "string", "minLength": 1, "maxLength": 200}}},
                 "stated_at": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"},
                 "statement_reference": reference,
             }},
        ]}
        value_semantics = {
            "value_formation": {"type": "string", "enum": list(VALUE_FORMATIONS)},
            "attribution": attribution,
        }
        return {
            "name": "publish_research_report",
            "description": (
                "Submit the complete integrated report as explicit blocks, not a Markdown report to be reverse-engineered. "
                "Use text parts for narrative and fact/calculation parts for quantitative values; the server inserts exact "
                "values and units. Use value_style='zh-CN' for Chinese grouped numbers and known unit labels without changing amount, precision or canonical units. "
                "Never type those values or citation markers into narrative. Bind support explicitly; "
                "explain inference in reasoning and record limitations. Classification is your claim, not verification. "
                "Give each narrative block a concise descriptive title about the research question, not a generic classification. "
                "Separate independent company, fiscal-period, and source-date statements into focused blocks with their own actual support, "
                "rather than one long paragraph with a pooled list of unrelated references. A genuine cross-company comparison must still "
                "retain support for every compared subject; never drop necessary evidence merely to shorten a review request. "
                "Include unresolved questions as evidence_gap blocks. Re-submit the entire report to revise before this "
                "run ends. Optionally submit ordered datasets with explicit point labels and fact/calculation IDs. Every numeric part and point must declare value_formation: observed for an occurred/measured source record, estimated for an unknown/future estimate, assumed for an analysis/scenario input, or derived for a reproducible calculation. Calculations must be derived with null attribution; facts and query cells cannot be derived. Attribution identifies an explicit speaker, UTC statement time and exact current-run statement reference, not system endorsement; use null when nobody explicitly stated the value and never guess a speaker from a title, URL or company ID. "
                "Choose table, bar, line or scatter and explain its purpose. For a cross-period or cross-subject table, explicitly "
                "map every bound point exactly once into presentation columns/rows and place it after report-block-N, where N is the "
                "one-based position of the submitted narrative block; never rely "
                "on labels to imply cell relationships. Charts require consistent units, basis and period kinds. "
                "Use dataset.support for nonnumeric description or footnote claims not established by the bound points, using actual sources obtained in this run. "
                "Do not infer that an original document is absent merely because it is not cited in the report. "
                "A real line chart must explicitly set time_axis='period_end' and use only direct facts for one company, metric, unit, basis and annual/quarterly period kind; labels never define time. "
                "A successful tool call prepares publication; completion commits the immutable report version. "
                "After submission, do not emit another report in free text."
            ),
            "parameters": {"type": "object", "additionalProperties": False, "required": ["title", "blocks"],
                "properties": {
                    "title": {"type": "string", "minLength": 1, "maxLength": 200},
                    "value_style": {"type": "string", "enum": ["recorded", "zh-CN"], "default": "recorded"},
                    "datasets": {"type": "array", "maxItems": 10, "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["title", "description", "display", "points"],
                        "properties": {
                            "title": {"type": "string", "minLength": 1, "maxLength": 200},
                            "description": {"type": "string", "minLength": 1, "maxLength": 1000},
                            "support": {"type": "array", "maxItems": 16, "items": reference,
                                        "description": "Additional current-run sources for the title, description, column meaning or footnotes. Point references are added automatically; values alone do not prove source footnotes."},
                            "display": {"type": "string", "enum": ["table", "bar", "line", "scatter"]},
                            "time_axis": {"type": "string", "enum": ["period_end"],
                                          "description": "Explicit real UTC date axis for a direct-fact line; omitted legacy lines remain tables."},
                            "points": {"type": "array", "minItems": 1, "maxItems": 50, "items": {
                                "type": "object", "additionalProperties": False,
                                "required": ["label", "kind", "id", "value_formation", "attribution"],
                                "properties": {"label": {"type": "string", "minLength": 1, "maxLength": 200},
                                               "kind": {"type": "string", "enum": ["fact", "calculation"]},
                                               "id": {"type": "string", "minLength": 1, "maxLength": 200},
                                               **value_semantics},
                            }},
                            "presentation": {"type": "object", "additionalProperties": False,
                                "description": "Numeric data columns only: rows[].label is rendered separately as the leading row header. Do not add a period/company label column. Every row needs exactly one point-index cell per data column; map each point exactly once.",
                                "required": ["columns", "rows", "after_block_id"],
                                "properties": {
                                    "after_block_id": {"type": "string", "pattern": "^report-block-[1-9][0-9]*$",
                                                       "description": "Exact target report-block-N; N is the 1-based narrative block position."},
                                    "unit": {"type": "string", "minLength": 1, "maxLength": 200,
                                             "description": "Optional unit shared by all points: use the exact canonical unit (for example USD millions) or its server-known localized display unit under value_style='zh-CN'. Never change scale or invent a translation. Omit for mixed-unit columns."},
                                    "note": {"type": "string", "minLength": 1, "maxLength": 1000},
                                    "columns": {"type": "array", "minItems": 1, "maxItems": 20, "items": {
                                        "type": "object", "additionalProperties": False, "required": ["key", "label"],
                                        "properties": {"key": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$", "maxLength": 80},
                                                       "label": {"type": "string", "minLength": 1, "maxLength": 200}}}},
                                    "rows": {"type": "array", "minItems": 1, "maxItems": 50, "items": {
                                        "type": "object", "additionalProperties": False, "required": ["key", "label", "cells"],
                                        "properties": {"key": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$", "maxLength": 80},
                                                       "label": {"type": "string", "minLength": 1, "maxLength": 200},
                                                       "cells": {"type": "array", "minItems": 1, "maxItems": 20, "items": {
                                                           "type": "object", "additionalProperties": False, "required": ["point_index"],
                                                           "properties": {"point_index": {"type": "integer", "minimum": 0}}}}}}},
                                }},
                        },
                    }},
                    "blocks": {"type": "array", "minItems": 1, "maxItems": 80, "items": {
                        "type": "object", "additionalProperties": False,
                        "required": ["classification", "parts", "support", "reasoning", "limitations"],
                        "properties": {
                            "title": {"type": "string", "minLength": 1, "maxLength": 120},
                            "classification": {"type": "string", "enum": list(KINDS)},
                            "parts": {"type": "array", "minItems": 1, "maxItems": 160, "items": {"oneOf": [
                                {"type": "object", "additionalProperties": False, "required": ["kind", "text"],
                                 "properties": {"kind": {"type": "string", "enum": ["text"]},
                                                "text": {"type": "string", "minLength": 1, "maxLength": 4000}}},
                                {"type": "object", "additionalProperties": False,
                                 "required": ["kind", "id", "value_formation", "attribution"],
                                 "properties": {"kind": {"type": "string", "enum": ["fact", "calculation"]},
                                                "id": {"type": "string", "minLength": 1, "maxLength": 200},
                                                **value_semantics}},
                                {"type": "object", "additionalProperties": False,
                                 "required": ["kind", "id", "row", "column", "value_formation", "attribution"],
                                 "properties": {"kind": {"type": "string", "enum": ["query_cell"]},
                                                "id": {"type": "string", "minLength": 1, "maxLength": 200},
                                                "row": {"type": "integer", "minimum": 0},
                                                "column": {"type": "integer", "minimum": 0},
                                                **value_semantics}},
                            ]}},
                            "support": {"type": "array", "maxItems": 16, "items": reference},
                            "reasoning": {"type": "string", "maxLength": 4000},
                            "limitations": {"type": "array", "maxItems": 12,
                                           "items": {"type": "string", "minLength": 1, "maxLength": 1000}},
                        },
                    }},
                }},
            "handler": handler,
        }


def project(publication, citation_map, trace_map):
    """Build UI bindings only from the frozen report structure, never prose semantics."""
    findings, anchors = [], []
    text = publication["answer"]
    for block_index, block in enumerate(publication["blocks"]):
        if _is_dataset_rendering_block(publication, block, block_index):
            continue
        references = [{**ref, "valid": citation_map.get((ref["kind"], ref["id"]), {}).get("valid") is True}
                      for ref in block["support"]]
        valid = all(item["valid"] for item in references)
        trace_ids = list(dict.fromkeys(trace_map[(ref["kind"], ref["id"])] for ref in references
                                      if (ref["kind"], ref["id"]) in trace_map))
        status = "source_linked" if references and valid else "unreferenced" if not references else "reference_invalid"
        finding = {
            "id": block["id"], "text": block["text"], "classification": block["classification"],
            "status": status, "label": "Source linked" if status == "source_linked" else "Needs review",
            "references": references, "trace_ids": trace_ids, "review_flags": [],
            "explanation": "The evidence relationship was explicitly submitted by the researcher; a source link does not equal semantic approval.",
            "reasoning": block["reasoning"], "limitations": block["limitations"],
        }
        if "title" in block:
            finding["title"] = block["title"]
        findings.append(finding)
        spans = [{"start": block["start"], "end": block["end"], "text": block["text"], "binding": None}]
        spans.extend(span for span in block["spans"] if span["binding"] is not None)
        for ordinal, span in enumerate(spans):
            binding = span["binding"]
            refs = ([binding["reference"]] + ([binding["attribution"]["statement_reference"]]
                    if binding and binding["attribution"] is not None else [])) if binding else block["support"]
            traces = [trace_map[(ref["kind"], ref["id"])] for ref in refs if (ref["kind"], ref["id"]) in trace_map]
            calculated = binding is not None and binding["kind"] in {"calculation", "query_cell"}
            if text[span["start"]:span["end"]] != span["text"]:
                raise ToolError("publication_integrity_mismatch", "Explicit report span differs from published text")
            anchors.append({
                "id": f'{block["id"]}-part-{ordinal + 1}', "finding_id": block["id"],
                "start": span["start"], "end": span["end"],
                "utf16_start": len(text[:span["start"]].encode("utf-16-le")) // 2,
                "utf16_end": len(text[:span["end"]].encode("utf-16-le")) // 2,
                "text": span["text"], "kind": "number" if binding and (
                    binding["kind"] != "query_cell" or binding["dimensions"].get("native_type") in ("integer", "real")
                    or ("native_type" not in binding["dimensions"] and type(binding["value"]) in (int, float))) else "assertion",
                "support_status": ("calculated" if calculated else "supported") if binding and valid else "unresolved",
                "classification": ("recorded_calculation" if calculated else "direct_statement") if binding else "inference",
                "references": [{**ref, "valid": citation_map.get((ref["kind"], ref["id"]), {}).get("valid") is True}
                               for ref in refs],
                "trace_ids": traces, "calculation_id": binding["id"] if binding and binding["kind"] == "calculation" else None,
                **({"value_formation": binding["value_formation"],
                    "attribution": deepcopy(binding["attribution"])} if binding else {}),
                "warnings": [], "evidence": [{
                    "reference": binding["reference"], "trace_id": traces[0] if traces else None,
                    "match": "explicit_record_binding", "dimensions": binding["dimensions"],
                }] if binding else [],
                "explanation": "The exact record bound at publication time." if binding else "Researcher-submitted statement; semantic review is pending.",
            })
    return findings, anchors


def _is_dataset_rendering_block(publication, block, block_index):
    """Identify a compiler-appended dataset rendering from its frozen structural binding."""
    blocks, datasets = publication.get("blocks", []), publication.get("datasets", [])
    if (not isinstance(block_index, int) or not isinstance(blocks, list) or not isinstance(datasets, list)
            or not datasets or len(blocks) < len(datasets)):
        return False
    dataset_index = block_index - (len(blocks) - len(datasets))
    if dataset_index < 0 or dataset_index >= len(datasets) or blocks[block_index] is not block:
        return False

    dataset = datasets[dataset_index]
    block_text = block.get("text")
    points = dataset.get("points") if isinstance(dataset, dict) else None
    if not isinstance(block_text, str) or not isinstance(points, list):
        return False
    presentation = dataset.get("presentation")
    if presentation is None:
        axis_text = ("Timeline: fact period end dates (true UTC date distances); missing periods are not imputed and lines do not span gaps.\n\n"
                     if isinstance(dataset.get("time_axis"), dict) else "")
        prefix = (f'### {dataset.get("title")}\n\n{dataset.get("description")}\n\n{axis_text}'
                  '| Item | Value and source |\n|---|---|')
        point_indexes = list(range(len(points)))
    else:
        if not isinstance(presentation, dict):
            return False
        columns, rows = presentation.get("columns"), presentation.get("rows")
        if not isinstance(columns, list) or not isinstance(rows, list):
            return False
        try:
            labels = " | ".join(column["label"] for column in columns)
            separators = "|".join("---" for _ in range(len(columns) + 1))
            metadata = (f'\n\nUnit: {presentation["unit"]}' if "unit" in presentation else "")
            metadata += f'\n\nNote: {presentation["note"]}' if "note" in presentation else ""
            prefix = (f'### {dataset["title"]}\n\n{dataset["description"]}{metadata}\n\n'
                      f'| Item | {labels} |\n|{separators}|')
            point_indexes = [cell["point_index"] for row in rows for cell in row["cells"]]
        except (KeyError, TypeError):
            return False
    if not block_text.startswith(prefix) or sorted(point_indexes) != list(range(len(points))):
        return False
    expected_bindings = [points[index].get("binding") for index in point_indexes]
    actual_bindings = [span.get("binding") for span in block.get("spans", []) if span.get("binding") is not None]
    return all(binding is not None for binding in expected_bindings) and actual_bindings == expected_bindings


def _review_summary(claims, *, assessed):
    """Summarize only exact saved review claims; mixed never hides its negative members."""
    claims = [deepcopy(claim) for claim in claims]
    if not assessed or not claims:
        return {"status": "not_assessed", "semantic_verified": False, "claims": claims}
    statuses = {
        "supported" if claim.get("support_status") == "supported_by_evidence"
        else claim.get("support_status")
        for claim in claims
    }
    status = next(iter(statuses)) if len(statuses) == 1 and next(iter(statuses)) in {
        "supported", "unsupported", "conflicting",
    } else "mixed"
    summary = {"status": status, "semantic_verified": False, "claims": claims}
    if len(claims) == 1:
        claim = claims[0]
        summary.update({
            "claim_id": claim.get("id"), "support_status": claim.get("support_status"),
            "evidence_refs": deepcopy(claim.get("evidence_refs", [])), "reason": claim.get("reason"),
            "reviewed_text": claim.get("exact_text"),
        })
    return summary


def _review_claims_within(answer, start, end, claims):
    """Return only saved claims whose exact, validated range is inside one scope."""
    if (not isinstance(answer, str) or type(start) is not int or type(end) is not int
            or not 0 <= start < end <= len(answer)):
        return []
    return [claim for claim in claims if isinstance(claim, dict)
            and type(claim.get("start")) is int and type(claim.get("end")) is int
            and start <= claim["start"] < claim["end"] <= end
            and answer[claim["start"]:claim["end"]] == claim.get("exact_text")]


def _dataset_review_projection(publication, dataset_index):
    """Transfer review of one compiler-generated table to its typed replacement."""
    semantic_review = publication.get("semantic_review")
    assessed = isinstance(semantic_review, dict) and semantic_review.get("status") == "assessed"
    blocks, datasets = publication.get("blocks", []), publication.get("datasets", [])
    block_index = len(blocks) - len(datasets) + dataset_index
    block = blocks[block_index] if 0 <= block_index < len(blocks) else None
    if not isinstance(block, dict) or not _is_dataset_rendering_block(publication, block, block_index):
        return _review_summary([], assessed=False), [], {}

    answer = publication.get("answer", "")
    claims = (_review_claims_within(answer, block["start"], block["end"],
                                    semantic_review.get("claims", [])) if assessed else [])

    flags = [{
        "code": "semantic_" + claim["support_status"], "severity": "high",
        "message": claim.get("reason") or "Available evidence cannot support this dataset content.",
        "action": "Retain the entire dataset only in the review appendix, not as main-content analysis.",
    } for claim in claims if claim.get("support_status") in {"unsupported", "conflicting"}]

    presentation = datasets[dataset_index].get("presentation")
    if isinstance(presentation, dict):
        point_indexes = [cell["point_index"] for row in presentation["rows"] for cell in row["cells"]]
    else:
        point_indexes = list(range(len(datasets[dataset_index]["points"])))
    typed_spans = [span for span in block.get("spans", []) if span.get("binding") is not None]
    point_claims = {}
    for claim in claims:
        overlapping = [position for position, span in enumerate(typed_spans)
                       if claim["start"] < span["end"] and claim["end"] > span["start"]]
        if len(overlapping) == 1:
            point_claims.setdefault(point_indexes[overlapping[0]], []).append(claim)
    point_reviews = {index: _review_summary(items, assessed=True) for index, items in point_claims.items()}
    return _review_summary(claims, assessed=assessed), flags, point_reviews


def project_datasets(publication, trace_map):
    result = []
    for dataset_index, dataset in enumerate(publication.get("datasets", [])):
        semantic_review, review_flags, point_reviews = _dataset_review_projection(publication, dataset_index)
        rows = []
        for index, point in enumerate(dataset["points"]):
            binding = point["binding"]
            dimensions, reference = binding["dimensions"], binding["reference"]
            rows.append({
                "id": f'{dataset["id"]}-point-{index + 1}', "label": point["label"],
                "value": binding["value"], "unit": binding["unit"], "basis": dimensions["basis"],
                **({"display_text": binding["text"], "display_unit": binding["display_unit"]}
                   if publication.get("value_style") == "zh-CN" else {}),
                "period": {"start": dimensions["period_start"], "end": dimensions["period_end"],
                           "kind": dimensions["period_kind"],
                           **({"ranges": deepcopy(dimensions["periods"])} if "periods" in dimensions else {})},
                "reference": reference, "trace_id": trace_map.get((reference["kind"], reference["id"])),
                "value_formation": binding["value_formation"],
                "attribution": deepcopy(binding["attribution"]),
                **({"semantic_review": point_reviews[index]} if index in point_reviews else {}),
                **({"record_kind": binding["kind"], "company_id": dimensions["company_id"],
                    "metric": dimensions["metric"]} if "time_axis" in dataset else {}),
            })
        projected = {
            "id": dataset["id"], "title": dataset["title"], "description": dataset["description"],
            "rows": rows, "unit": rows[0]["unit"], "basis": rows[0]["basis"],
            "columns": [{"key": "label", "label": "Item"}, {"key": "value", "label": "Value"}],
            "chart": ({"type": dataset["display"], "x": "period.end", "y": "value",
                       "time_axis": deepcopy(dataset["time_axis"])} if "time_axis" in dataset else
                      {"type": dataset["display"], "x": "label", "y": "value"}) if dataset["display"] != "table" else None,
            "limitations": ["Dataset explicitly selected by the researcher; value binding does not constitute semantic or economic-comparability approval."],
            "semantic_review": semantic_review,
            "review_flags": review_flags,
        }
        if dataset.get("support"):
            description_block = publication["blocks"][len(publication["blocks"]) - len(publication["datasets"]) + dataset_index]
            context_text = f'{dataset["title"]}\n\n{dataset["description"]}'
            presentation = dataset.get("presentation", {})
            if "unit" in presentation:
                context_text += f'\n\nUnit: {presentation["unit"]}'
            if "note" in presentation:
                context_text += f'\n\nNote: {presentation["note"]}'
            start = description_block["start"] + len("### ")
            end = start + len(context_text)
            if publication["answer"][start:end] != context_text:
                raise ToolError("publication_integrity_mismatch", "Dataset description differs from its canonical span")
            description_claims = _review_claims_within(
                publication["answer"], start, end, semantic_review.get("claims", []))
            refs = deepcopy(dataset["support"])
            trace_ids = [trace_map.get((ref["kind"], ref["id"])) for ref in refs]
            projected["description_anchor"] = {
                "id": f'{dataset["id"]}-description', "finding_id": dataset["id"],
                "start": start, "end": end, "text": context_text, "kind": "statement",
                "utf16_start": len(publication["answer"][:start].encode("utf-16-le")) // 2,
                "utf16_end": len(publication["answer"][:end].encode("utf-16-le")) // 2,
                "classification": "direct_statement", "support_status": "source_linked" if all(trace_ids) else "unresolved",
                "references": refs, "trace_ids": [trace_id for trace_id in trace_ids if trace_id],
                "warnings": [], "evidence": [], "calculation_id": None,
                "explanation": "The chart description is bound to sources explicitly selected by the publisher; a valid citation does not automatically establish the description.",
                "semantic_review": _review_summary(description_claims,
                    assessed=isinstance(publication.get("semantic_review"), dict)
                    and publication["semantic_review"].get("status") == "assessed"),
            }
        if "presentation" in dataset:
            presentation = deepcopy(dataset["presentation"])
            for matrix_row in presentation["rows"]:
                for cell in matrix_row["cells"]:
                    point_index = cell.pop("point_index")
                    point_id = rows[point_index]["id"]
                    if cell.get("point_id") != point_id:
                        raise ToolError("publication_integrity_mismatch", "Matrix cell differs from its bound dataset point")
                    cell["anchor_id"] = f"{point_id}-cell-value"
            projected["presentation"] = presentation
        result.append(projected)
    return result
