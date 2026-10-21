"""Immutable typed boundary for canonical structured research publications."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Literal, TypeVar

from .research_records import FrozenObject, JsonObject, JsonValue, freeze_object, thaw_object


VERSION: Literal["structured-research-report/1"] = "structured-research-report/1"
ReferenceKind = Literal["claim", "evidence", "passage", "calculation", "query"]
Classification = Literal["source_statement", "recorded_calculation", "interpretation", "evidence_gap"]
ValueStyle = Literal["recorded", "zh-CN"]
Display = Literal["table", "bar", "line", "scatter"]
ValueFormation = Literal["observed", "estimated", "assumed", "derived"]
PrincipalType = Literal["person", "organization", "collective", "system"]
_REFERENCE_KINDS: tuple[ReferenceKind, ...] = ("claim", "evidence", "passage", "calculation", "query")
_CLASSIFICATIONS: tuple[Classification, ...] = ("source_statement", "recorded_calculation", "interpretation", "evidence_gap")
_VALUE_STYLES: tuple[ValueStyle, ...] = ("recorded", "zh-CN")
_DISPLAYS: tuple[Display, ...] = ("table", "bar", "line", "scatter")
_VALUE_FORMATIONS: tuple[ValueFormation, ...] = ("observed", "estimated", "assumed", "derived")
_PRINCIPAL_TYPES: tuple[PrincipalType, ...] = ("person", "organization", "collective", "system")
T = TypeVar("T", bound=str)


def _choice(value: object, choices: tuple[T, ...], path: str) -> T:
    for choice in choices:
        if type(value) is str and value == choice:
            return choice
    raise PublicationContractError(f"{path} has an unsupported value")


class PublicationContractError(ValueError):
    """A canonical publication has an invalid structural contract."""



def _fail(path: str, message: str) -> PublicationContractError:
    return PublicationContractError(f"{path} {message}")


def _object(value: object, path: str) -> dict[object, object]:
    if type(value) is not dict:
        raise _fail(path, "must be an object")
    return dict(value)


def _exact_fields(
    value: object, path: str, required: frozenset[str], optional: frozenset[str] = frozenset(),
) -> dict[object, object]:
    mapping = _object(value, path)
    object_keys = set(mapping)
    if any(type(key) is not str for key in object_keys):
        raise _fail(path, "must have string fields")
    keys = {key for key in object_keys if isinstance(key, str)}
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise _fail(path, f"is missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise _fail(path, f"has unknown fields: {', '.join(sorted(str(key) for key in unknown))}")
    return mapping


def _string(value: object, path: str, *, empty: bool = False) -> str:
    if type(value) is not str or (not empty and not value):
        raise _fail(path, "must be a non-empty string" if not empty else "must be a string")
    return value


def _integer(value: object, path: str) -> int:
    if type(value) is not int:
        raise _fail(path, "must be an integer")
    return value


def _array(value: object, path: str) -> list[object]:
    if type(value) is not list:
        raise _fail(path, "must be an array")
    return list(value)


def _optional_string(value: object, path: str) -> str | None:
    if value is None:
        return None
    return _string(value, path, empty=True)


@dataclass(frozen=True, slots=True)
class Reference:
    kind: ReferenceKind
    id: str

    def __post_init__(self) -> None:
        _string(self.kind, "reference.kind")
        if self.kind not in _REFERENCE_KINDS:
            raise _fail("reference.kind", "has an unsupported value")
        _string(self.id, "reference.id")

    @classmethod
    def decode(cls, value: object, path: str = "reference") -> Reference:
        mapping = _exact_fields(value, path, frozenset({"kind", "id"}))
        return cls(_choice(mapping["kind"], _REFERENCE_KINDS, f"{path}.kind"), _string(mapping["id"], f"{path}.id"))

    def to_json(self) -> JsonObject:
        return {"kind": self.kind, "id": self.id}


def _attribution(value: object, path: str) -> FrozenObject | None:
    if value is None:
        return None
    mapping = _exact_fields(
        value, path, frozenset({"principal", "stated_at", "statement_reference"}),
    )
    principal = _exact_fields(
        mapping["principal"], f"{path}.principal", frozenset({"type", "identifier", "label"}),
    )
    _choice(principal["type"], _PRINCIPAL_TYPES, f"{path}.principal.type")
    _string(principal["identifier"], f"{path}.principal.identifier")
    _string(principal["label"], f"{path}.principal.label")
    stated_at = _string(mapping["stated_at"], f"{path}.stated_at")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stated_at) is None:
        raise _fail(f"{path}.stated_at", "must be an ISO UTC timestamp with second precision")
    try:
        parsed = datetime.fromisoformat(stated_at.replace("Z", "+00:00"))
        if parsed.tzinfo != timezone.utc:
            raise ValueError("not UTC")
    except ValueError as exc:
        raise _fail(f"{path}.stated_at", "must be a valid ISO UTC timestamp") from exc
    Reference.decode(mapping["statement_reference"], f"{path}.statement_reference")
    return freeze_object(value, path)


def _dimensions(value: object, path: str, binding_kind: str) -> FrozenObject:
    if binding_kind == "query_cell":
        mapping = _exact_fields(value, path, frozenset({
            "native_column", "native_type", "row", "column", "truncated",
        }))
        _string(mapping["native_column"], f"{path}.native_column")
        native_type = _string(mapping["native_type"], f"{path}.native_type")
        if native_type not in {"null", "integer", "real", "text", "blob"}:
            raise _fail(f"{path}.native_type", "has an unsupported value")
        row = _integer(mapping["row"], f"{path}.row")
        column = _integer(mapping["column"], f"{path}.column")
        if row < 0 or column < 0:
            raise _fail(path, "row and column must be non-negative")
        if type(mapping["truncated"]) is not bool:
            raise _fail(f"{path}.truncated", "must be a boolean")
    else:
        required = frozenset({
            "company_id", "metric", "period_start", "period_end", "period_kind", "basis",
        })
        mapping = _exact_fields(value, path, required, frozenset({"company_ids", "periods"}))
        for field in required:
            _optional_string(mapping[field], f"{path}.{field}")
        if "company_ids" in mapping:
            company_ids = _array(mapping["company_ids"], f"{path}.company_ids")
            for index, company_id in enumerate(company_ids):
                _string(company_id, f"{path}.company_ids[{index}]")
        if "periods" in mapping:
            periods = _array(mapping["periods"], f"{path}.periods")
            for index, period_value in enumerate(periods):
                period = _array(period_value, f"{path}.periods[{index}]")
                if len(period) != 2:
                    raise _fail(f"{path}.periods[{index}]", "must contain start and end")
                _optional_string(period[0], f"{path}.periods[{index}][0]")
                _optional_string(period[1], f"{path}.periods[{index}][1]")
    return freeze_object(value, path)


def _binding(value: object, path: str) -> FrozenObject:
    base = frozenset({
        "kind", "id", "value", "text", "unit", "reference", "dimensions",
        "value_formation", "attribution",
    })
    mapping = _object(value, path)
    kind = _string(mapping.get("kind"), f"{path}.kind")
    formation = _choice(mapping.get("value_formation"), _VALUE_FORMATIONS, f"{path}.value_formation")
    attribution = _attribution(mapping.get("attribution"), f"{path}.attribution")
    if kind == "calculation" and formation != "derived":
        raise _fail(f"{path}.value_formation", "must be derived for a calculation")
    if kind == "calculation" and attribution is not None:
        raise _fail(f"{path}.attribution", "must be null for a calculation")
    if kind in {"fact", "query_cell"} and formation == "derived":
        raise _fail(f"{path}.value_formation", "cannot be derived for a fact or query cell")
    if kind in {"fact", "calculation"}:
        mapping = _exact_fields(value, path, base, frozenset({"display_unit"}))
        identifier = _string(mapping["id"], f"{path}.id")
        _string(mapping["value"], f"{path}.value", empty=True)
        _string(mapping["text"], f"{path}.text", empty=True)
        _string(mapping["unit"], f"{path}.unit", empty=True)
        reference = Reference.decode(mapping["reference"], f"{path}.reference")
        expected_reference = "evidence" if kind == "fact" else "calculation"
        if reference.kind != expected_reference:
            raise _fail(f"{path}.reference.kind", f"must be {expected_reference}")
        if kind == "calculation" and reference.id != identifier:
            raise _fail(f"{path}.reference.id", "must match the calculation binding id")
        _dimensions(mapping["dimensions"], f"{path}.dimensions", kind)
        if "display_unit" in mapping:
            _string(mapping["display_unit"], f"{path}.display_unit", empty=True)
    elif kind == "query_cell":
        mapping = _exact_fields(
            value, path, base | frozenset({"row", "column", "rendering"}),
        )
        identifier = _string(mapping["id"], f"{path}.id")
        row = _integer(mapping["row"], f"{path}.row")
        column = _integer(mapping["column"], f"{path}.column")
        if row < 0 or column < 0:
            raise _fail(path, "row and column must be non-negative")
        _string(mapping["text"], f"{path}.text", empty=True)
        if mapping["unit"] is not None:
            raise _fail(f"{path}.unit", "must be null for a native query cell")
        Reference.decode(mapping["reference"], f"{path}.reference")
        reference = Reference.decode(mapping["reference"], f"{path}.reference")
        if reference.kind != "query" or reference.id != identifier:
            raise _fail(f"{path}.reference", "must identify the bound query")
        dimensions = _dimensions(mapping["dimensions"], f"{path}.dimensions", kind)
        dimensions_json = thaw_object(dimensions)
        if dimensions_json["row"] != row or dimensions_json["column"] != column:
            raise _fail(f"{path}.dimensions", "row and column must match the binding")
        _string(mapping["rendering"], f"{path}.rendering")
    else:
        raise _fail(f"{path}.kind", "has an unsupported value")
    return freeze_object(value, path)


@dataclass(frozen=True, slots=True)
class Span:
    start: int
    end: int
    text: str
    binding: FrozenObject | None

    def __post_init__(self) -> None:
        _integer(self.start, "span.start")
        _integer(self.end, "span.end")
        _string(self.text, "span.text", empty=True)
        if self.start < 0 or self.end < self.start or self.end - self.start != len(self.text):
            raise _fail("span", "has an invalid range for its text")
        if self.binding is not None:
            if type(self.binding) is not FrozenObject:
                raise _fail("span.binding", "must be a frozen object or null")
            checked = _binding(thaw_object(self.binding), "span.binding")
            if checked != self.binding:
                raise _fail("span.binding", "is not canonical")
            if thaw_object(self.binding)["text"] != self.text:
                raise _fail("span.binding.text", "must match span text")

    @classmethod
    def decode(cls, value: object, path: str) -> Span:
        mapping = _exact_fields(value, path, frozenset({"start", "end", "text", "binding"}))
        binding_value = mapping["binding"]
        binding = None if binding_value is None else _binding(binding_value, f"{path}.binding")
        return cls(
            _integer(mapping["start"], f"{path}.start"),
            _integer(mapping["end"], f"{path}.end"),
            _string(mapping["text"], f"{path}.text", empty=True),
            binding,
        )

    def to_json(self) -> JsonObject:
        return {
            "start": self.start, "end": self.end, "text": self.text,
            "binding": None if self.binding is None else thaw_object(self.binding),
        }


@dataclass(frozen=True, slots=True)
class Block:
    id: str
    classification: Classification
    start: int
    end: int
    text: str
    spans: tuple[Span, ...]
    support: tuple[Reference, ...]
    reasoning: str
    limitations: tuple[str, ...]
    title: str | None = None

    def __post_init__(self) -> None:
        _string(self.id, "block.id")
        _string(self.classification, "block.classification")
        if self.classification not in _CLASSIFICATIONS:
            raise _fail("block.classification", "has an unsupported value")
        _integer(self.start, "block.start")
        _integer(self.end, "block.end")
        _string(self.text, "block.text", empty=True)
        if self.start < 0 or self.end < self.start or self.end - self.start != len(self.text):
            raise _fail("block", "has an invalid range for its text")
        if type(self.spans) is not tuple or any(type(span) is not Span for span in self.spans):
            raise _fail("block.spans", "must be a tuple of Span values")
        if type(self.support) is not tuple or any(type(item) is not Reference for item in self.support):
            raise _fail("block.support", "must be a tuple of Reference values")
        _string(self.reasoning, "block.reasoning", empty=True)
        if type(self.limitations) is not tuple:
            raise _fail("block.limitations", "must be a tuple")
        for limitation in self.limitations:
            _string(limitation, "block.limitations[]")
        if self.title is not None:
            _string(self.title, "block.title")
        support = {reference.kind + ":" + reference.id for reference in self.support}
        for span in self.spans:
            if span.start < self.start or span.end > self.end:
                raise _fail("block.spans", "contains a span outside the block")
            relative_start = span.start - self.start
            relative_end = span.end - self.start
            if self.text[relative_start:relative_end] != span.text:
                raise _fail("block.spans", "contains text that does not match the block")
            if span.binding is not None:
                binding = thaw_object(span.binding)
                attribution = binding.get("attribution")
                if attribution is not None:
                    attribution_object = _object(attribution, "span.binding.attribution")
                    statement_reference = _object(
                        attribution_object.get("statement_reference"),
                        "span.binding.attribution.statement_reference",
                    )
                    key = (_string(statement_reference.get("kind"),
                                   "span.binding.attribution.statement_reference.kind") + ":"
                           + _string(statement_reference.get("id"),
                                     "span.binding.attribution.statement_reference.id"))
                    if key not in support:
                        raise _fail("block.support", "must include every binding attribution statement_reference")

    @classmethod
    def decode(cls, value: object, path: str) -> Block:
        required = frozenset({
            "id", "classification", "start", "end", "text", "spans", "support",
            "reasoning", "limitations",
        })
        mapping = _exact_fields(value, path, required, frozenset({"title"}))
        spans = tuple(
            Span.decode(item, f"{path}.spans[{index}]")
            for index, item in enumerate(_array(mapping["spans"], f"{path}.spans"))
        )
        support = tuple(
            Reference.decode(item, f"{path}.support[{index}]")
            for index, item in enumerate(_array(mapping["support"], f"{path}.support"))
        )
        limitations = tuple(
            _string(item, f"{path}.limitations[{index}]")
            for index, item in enumerate(_array(mapping["limitations"], f"{path}.limitations"))
        )
        title = _string(mapping["title"], f"{path}.title") if "title" in mapping else None
        return cls(
            _string(mapping["id"], f"{path}.id"),
            _choice(mapping["classification"], _CLASSIFICATIONS, f"{path}.classification"),
            _integer(mapping["start"], f"{path}.start"),
            _integer(mapping["end"], f"{path}.end"),
            _string(mapping["text"], f"{path}.text", empty=True),
            spans, support, _string(mapping["reasoning"], f"{path}.reasoning", empty=True),
            limitations, title,
        )

    def to_json(self) -> JsonObject:
        result: JsonObject = {
            "id": self.id, "classification": self.classification,
            "start": self.start, "end": self.end, "text": self.text,
            "spans": [span.to_json() for span in self.spans],
            "support": [reference.to_json() for reference in self.support],
            "reasoning": self.reasoning, "limitations": list(self.limitations),
        }
        if self.title is not None:
            result["title"] = self.title
        return result


def _time_axis(value: object, path: str) -> FrozenObject:
    mapping = _exact_fields(
        value, path, frozenset({"version", "field", "timezone", "gap_policy"}),
    )
    expected = {
        "version": "utc-period-axis/1", "field": "period_end",
        "timezone": "UTC", "gap_policy": "break",
    }
    for field, expected_value in expected.items():
        if mapping[field] != expected_value:
            raise _fail(f"{path}.{field}", f"must be {expected_value}")
    return freeze_object(value, path)


def _presentation(value: object, path: str, point_count: int) -> FrozenObject:
    mapping = _exact_fields(
        value, path,
        frozenset({"type", "after_block_id", "columns", "rows"}),
        frozenset({"unit", "note"}),
    )
    if mapping["type"] != "matrix":
        raise _fail(f"{path}.type", "must be matrix")
    _string(mapping["after_block_id"], f"{path}.after_block_id")
    for optional in ("unit", "note"):
        if optional in mapping:
            _string(mapping[optional], f"{path}.{optional}")
    columns = _array(mapping["columns"], f"{path}.columns")
    column_keys: set[str] = set()
    for index, value_column in enumerate(columns):
        column = _exact_fields(
            value_column, f"{path}.columns[{index}]", frozenset({"key", "label"}),
        )
        key = _string(column["key"], f"{path}.columns[{index}].key")
        _string(column["label"], f"{path}.columns[{index}].label")
        if key in column_keys:
            raise _fail(f"{path}.columns", "must have unique keys")
        column_keys.add(key)
    rows = _array(mapping["rows"], f"{path}.rows")
    row_keys: set[str] = set()
    used: list[int] = []
    for row_index, value_row in enumerate(rows):
        row_path = f"{path}.rows[{row_index}]"
        row = _exact_fields(value_row, row_path, frozenset({"key", "label", "cells"}))
        key = _string(row["key"], f"{row_path}.key")
        _string(row["label"], f"{row_path}.label")
        if key in row_keys:
            raise _fail(f"{path}.rows", "must have unique keys")
        row_keys.add(key)
        cells = _array(row["cells"], f"{row_path}.cells")
        if len(cells) != len(columns):
            raise _fail(f"{row_path}.cells", "must map every column")
        for cell_index, value_cell in enumerate(cells):
            cell_path = f"{row_path}.cells[{cell_index}]"
            cell = _exact_fields(value_cell, cell_path, frozenset({"point_index", "point_id"}))
            point_index = _integer(cell["point_index"], f"{cell_path}.point_index")
            if not 0 <= point_index < point_count:
                raise _fail(f"{cell_path}.point_index", "is outside the dataset")
            _string(cell["point_id"], f"{cell_path}.point_id")
            used.append(point_index)
    if sorted(used) != list(range(point_count)):
        raise _fail(path, "must map every dataset point exactly once")
    return freeze_object(value, path)


@dataclass(frozen=True, slots=True)
class DatasetPoint:
    label: str
    binding: FrozenObject

    def __post_init__(self) -> None:
        _string(self.label, "dataset point.label")
        if type(self.binding) is not FrozenObject:
            raise _fail("dataset point.binding", "must be a frozen object")
        checked = _binding(thaw_object(self.binding), "dataset point.binding")
        if checked != self.binding:
            raise _fail("dataset point.binding", "is not canonical")

    @classmethod
    def decode(cls, value: object, path: str) -> DatasetPoint:
        mapping = _exact_fields(value, path, frozenset({"label", "binding"}))
        return cls(
            _string(mapping["label"], f"{path}.label"),
            _binding(mapping["binding"], f"{path}.binding"),
        )

    def to_json(self) -> JsonObject:
        return {"label": self.label, "binding": thaw_object(self.binding)}


@dataclass(frozen=True, slots=True)
class Dataset:
    id: str
    title: str
    description: str
    display: Display
    points: tuple[DatasetPoint, ...]
    time_axis: FrozenObject | None = None
    support: tuple[Reference, ...] | None = None
    presentation: FrozenObject | None = None

    def __post_init__(self) -> None:
        _string(self.id, "dataset.id")
        _string(self.title, "dataset.title")
        _string(self.description, "dataset.description")
        _string(self.display, "dataset.display")
        if self.display not in _DISPLAYS:
            raise _fail("dataset.display", "has an unsupported value")
        if type(self.points) is not tuple or not self.points or any(
            type(point) is not DatasetPoint for point in self.points
        ):
            raise _fail("dataset.points", "must be a non-empty tuple of DatasetPoint values")
        if self.time_axis is not None:
            if type(self.time_axis) is not FrozenObject:
                raise _fail("dataset.time_axis", "must be a frozen object or null")
            _time_axis(thaw_object(self.time_axis), "dataset.time_axis")
        if self.support is not None and (
            type(self.support) is not tuple
            or any(type(reference) is not Reference for reference in self.support)
        ):
            raise _fail("dataset.support", "must be a tuple of Reference values or null")
        if self.presentation is not None:
            if type(self.presentation) is not FrozenObject:
                raise _fail("dataset.presentation", "must be a frozen object or null")
            _presentation(thaw_object(self.presentation), "dataset.presentation", len(self.points))

    @classmethod
    def decode(cls, value: object, path: str) -> Dataset:
        required = frozenset({"id", "title", "description", "display", "points"})
        mapping = _exact_fields(
            value, path, required, frozenset({"time_axis", "support", "presentation"}),
        )
        points = tuple(
            DatasetPoint.decode(item, f"{path}.points[{index}]")
            for index, item in enumerate(_array(mapping["points"], f"{path}.points"))
        )
        time_axis = _time_axis(mapping["time_axis"], f"{path}.time_axis") if "time_axis" in mapping else None
        support = None
        if "support" in mapping:
            support = tuple(
                Reference.decode(item, f"{path}.support[{index}]")
                for index, item in enumerate(_array(mapping["support"], f"{path}.support"))
            )
        presentation = (
            _presentation(mapping["presentation"], f"{path}.presentation", len(points))
            if "presentation" in mapping else None
        )
        return cls(
            _string(mapping["id"], f"{path}.id"),
            _string(mapping["title"], f"{path}.title"),
            _string(mapping["description"], f"{path}.description"),
            _choice(mapping["display"], _DISPLAYS, f"{path}.display"),
            points, time_axis, support, presentation,
        )

    def to_json(self) -> JsonObject:
        result: JsonObject = {
            "id": self.id, "title": self.title, "description": self.description,
            "display": self.display, "points": [point.to_json() for point in self.points],
        }
        if self.time_axis is not None:
            result["time_axis"] = thaw_object(self.time_axis)
        if self.support is not None:
            result["support"] = [reference.to_json() for reference in self.support]
        if self.presentation is not None:
            result["presentation"] = thaw_object(self.presentation)
        return result


@dataclass(frozen=True, slots=True)
class CanonicalPublication:
    title: str
    answer: str
    blocks: tuple[Block, ...]
    datasets: tuple[Dataset, ...]
    value_style: ValueStyle | None = None
    schema_version: Literal["structured-research-report/1"] = VERSION

    def __post_init__(self) -> None:
        _string(self.title, "publication.title")
        _string(self.answer, "publication.answer", empty=True)
        if type(self.blocks) is not tuple or not 1 <= len(self.blocks) <= 80 or any(type(block) is not Block for block in self.blocks):
            raise _fail("publication.blocks", "must be a tuple of Block values")
        if type(self.datasets) is not tuple or any(type(dataset) is not Dataset for dataset in self.datasets):
            raise _fail("publication.datasets", "must be a tuple of Dataset values")
        if self.schema_version != VERSION:
            raise _fail("publication.schema_version", "is unsupported")
        if self.value_style is not None and self.value_style not in _VALUE_STYLES:
            raise _fail("publication.value_style", "has an unsupported value")
        block_ids = [block.id for block in self.blocks]
        dataset_ids = [dataset.id for dataset in self.datasets]
        if len(set(block_ids)) != len(block_ids):
            raise _fail("publication.blocks", "must have unique ids")
        if len(set(dataset_ids)) != len(dataset_ids):
            raise _fail("publication.datasets", "must have unique ids")
        for block in self.blocks:
            if block.end > len(self.answer) or self.answer[block.start:block.end] != block.text:
                raise _fail(f"publication.blocks[{block.id}]", "does not match answer text")
            for span in block.spans:
                if span.end > len(self.answer) or self.answer[span.start:span.end] != span.text:
                    raise _fail(f"publication.blocks[{block.id}].spans", "does not match answer text")

    @classmethod
    def decode(cls, value: object) -> CanonicalPublication:
        required = frozenset({"schema_version", "title", "answer", "blocks", "datasets"})
        mapping = _exact_fields(value, "publication", required, frozenset({"value_style"}))
        schema_version = _choice(mapping["schema_version"], (VERSION,), "publication.schema_version")
        blocks = tuple(
            Block.decode(item, f"publication.blocks[{index}]")
            for index, item in enumerate(_array(mapping["blocks"], "publication.blocks"))
        )
        datasets = tuple(
            Dataset.decode(item, f"publication.datasets[{index}]")
            for index, item in enumerate(_array(mapping["datasets"], "publication.datasets"))
        )
        value_style = (
            _choice(mapping["value_style"], _VALUE_STYLES, "publication.value_style")
            if "value_style" in mapping else None
        )
        return cls(
            _string(mapping["title"], "publication.title"),
            _string(mapping["answer"], "publication.answer", empty=True),
            blocks, datasets, value_style, schema_version,
        )

    def to_json(self) -> JsonObject:
        blocks: list[JsonValue] = [block.to_json() for block in self.blocks]
        datasets: list[JsonValue] = [dataset.to_json() for dataset in self.datasets]
        result: JsonObject = {
            "schema_version": self.schema_version,
            "title": self.title,
            "answer": self.answer,
            "blocks": blocks,
            "datasets": datasets,
        }
        if self.value_style is not None:
            result["value_style"] = self.value_style
        return result
