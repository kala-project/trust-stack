from copy import deepcopy
from dataclasses import FrozenInstanceError
import unittest

from workbench.publication_contracts import (
    Block,
    CanonicalPublication,
    PublicationContractError,
    Reference,
    Span,
    VERSION,
)
from workbench.report_publications import prepare


def block(parts: list[dict[str, object]]) -> dict[str, object]:
    return {
        "classification": "source_statement",
        "parts": parts,
        "support": [],
        "reasoning": "",
        "limitations": [],
    }


def simple_publication() -> dict[str, object]:
    return prepare(
        title="Simple",
        blocks=[block([{"kind": "text", "text": "plain text"}])],
        refs={},
        facts={},
        calculations={},
        max_chars=4000,
    )


def query_publication() -> dict[str, object]:
    query = {
        "query_id": "query-1",
        "columns": [{"name": "amount"}],
        "rows": [[7]],
        "truncated": False,
    }
    return prepare(
        title="Native",
        blocks=[block([{
            "kind": "query_cell", "id": "query-1", "row": 0, "column": 0,
            "value_formation": "observed", "attribution": None,
        }])],
        refs={"query": {"query-1": {"record": query}}},
        facts={},
        calculations={},
        max_chars=4000,
    )


def fact_dataset_publication(*, value_style: str = "recorded") -> dict[str, object]:
    facts = {
        "f-1": {
            "value": "10.00",
            "unit": "CNY",
            "evidence_id": "e-1",
            "company_id": "subject-1",
            "metric": "revenue",
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "period_kind": "year",
            "basis": "reported",
        },
        "f-2": {
            "value": "20.00",
            "unit": "CNY",
            "evidence_id": "e-2",
            "company_id": "subject-1",
            "metric": "profit",
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
            "period_kind": "year",
            "basis": "reported",
        },
    }
    dataset = {
        "title": "Matrix",
        "description": "Producer-shaped dataset",
        "display": "table",
        "points": [
            {"label": "Revenue", "kind": "fact", "id": "f-1",
             "value_formation": "observed", "attribution": None},
            {"label": "Profit", "kind": "fact", "id": "f-2",
             "value_formation": "observed", "attribution": None},
        ],
        "support": [{"kind": "passage", "id": "p-1"}],
        "presentation": {
            "after_block_id": "report-block-1",
            "columns": [{"key": "value", "label": "Value"}],
            "rows": [
                {"key": "revenue", "label": "Revenue", "cells": [{"point_index": 0}]},
                {"key": "profit", "label": "Profit", "cells": [{"point_index": 1}]},
            ],
            "unit": "CNY",
            "note": "Recorded values",
        },
    }
    return prepare(
        title="Facts",
        blocks=[block([{"kind": "text", "text": "Facts: "},
                       {"kind": "fact", "id": "f-1",
                        "value_formation": "observed", "attribution": None}])],
        refs={"evidence": {"e-1": {}, "e-2": {}}, "passage": {"p-1": {}}},
        facts=facts,
        calculations={},
        max_chars=10000,
        datasets=[dataset],
        value_style=value_style,
    )


class CanonicalPublicationTests(unittest.TestCase):
    def test_prepare_preserves_bound_trailing_whitespace(self) -> None:
        raw = prepare(title="Whitespace", blocks=[block([
            {"kind": "text", "text": "Ending space \n"},
        ])], refs={}, facts={}, calculations={}, max_chars=4000)
        publication = CanonicalPublication.decode(raw)
        span = publication.blocks[0].spans[0]
        self.assertEqual(span.text, "Ending space \n")
        self.assertEqual(publication.answer[span.start:span.end], span.text)

    def test_real_prepare_shapes_round_trip_without_losing_fields(self) -> None:
        for produced in (
            simple_publication(), query_publication(), fact_dataset_publication(),
            fact_dataset_publication(value_style="zh-CN"),
        ):
            with self.subTest(title=produced["title"], style=produced.get("value_style")):
                publication = CanonicalPublication.decode(produced)
                self.assertEqual(publication.to_json(), produced)
                self.assertIsInstance(publication.blocks, tuple)
                self.assertIsInstance(publication.datasets, tuple)
                self.assertEqual(publication.schema_version, VERSION)

    def test_decoded_values_are_immutable_and_do_not_alias_input(self) -> None:
        raw = fact_dataset_publication()
        publication = CanonicalPublication.decode(raw)
        raw["title"] = "mutated"
        raw["blocks"][0]["spans"][0]["text"] = "mutated"
        raw["datasets"][0]["points"][0]["binding"]["value"] = "999"
        self.assertEqual(publication.title, "Facts")
        self.assertNotEqual(publication.blocks[0].spans[0].text, "mutated")
        self.assertEqual(publication.to_json()["datasets"][0]["points"][0]["binding"]["value"], "10.00")
        with self.assertRaises(FrozenInstanceError):
            publication.title = "forbidden"
        with self.assertRaises(FrozenInstanceError):
            publication.blocks[0].text = "forbidden"

    def test_empty_text_span_is_preserved(self) -> None:
        raw = simple_publication()
        first_block = raw["blocks"][0]
        position = first_block["start"]
        first_block["spans"].insert(0, {
            "start": position,
            "end": position,
            "text": "",
            "binding": None,
        })
        publication = CanonicalPublication.decode(raw)
        self.assertEqual(publication.blocks[0].spans[0].text, "")
        self.assertEqual(publication.to_json(), raw)

    def test_rejects_unknown_and_missing_top_level_or_block_fields(self) -> None:
        mutations: list[dict[str, object]] = []
        unknown_top = simple_publication()
        unknown_top["extra"] = True
        mutations.append(unknown_top)
        missing_top = simple_publication()
        del missing_top["answer"]
        mutations.append(missing_top)
        unknown_block = simple_publication()
        unknown_block["blocks"][0]["extra"] = True
        mutations.append(unknown_block)
        missing_block = simple_publication()
        del missing_block["blocks"][0]["reasoning"]
        mutations.append(missing_block)
        for raw in mutations:
            with self.subTest(raw=raw), self.assertRaises(PublicationContractError):
                CanonicalPublication.decode(raw)

    def test_rejects_wrong_enums_bool_as_integer_and_unknown_binding_fields(self) -> None:
        cases: list[dict[str, object]] = []
        bad_version = simple_publication()
        bad_version["schema_version"] = "structured-research-report/2"
        cases.append(bad_version)
        bad_classification = simple_publication()
        bad_classification["blocks"][0]["classification"] = "opinion"
        cases.append(bad_classification)
        bad_style = simple_publication()
        bad_style["value_style"] = "auto"
        cases.append(bad_style)
        bool_range = simple_publication()
        bool_range["blocks"][0]["start"] = False
        cases.append(bool_range)
        unknown_binding = fact_dataset_publication()
        unknown_binding["datasets"][0]["points"][0]["binding"]["extra"] = "no"
        cases.append(unknown_binding)
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(PublicationContractError):
                CanonicalPublication.decode(raw)

    def test_rejects_duplicate_ids_and_answer_or_span_mismatch(self) -> None:
        duplicate_block = simple_publication()
        duplicate_block["blocks"].append(deepcopy(duplicate_block["blocks"][0]))
        duplicate_dataset = fact_dataset_publication()
        duplicate_dataset["datasets"].append(deepcopy(duplicate_dataset["datasets"][0]))
        answer_mismatch = simple_publication()
        answer_mismatch["answer"] = answer_mismatch["answer"][:-1] + "X"
        span_mismatch = simple_publication()
        span_mismatch["blocks"][0]["spans"][0]["text"] = "wrong text"
        for raw in (duplicate_block, duplicate_dataset, answer_mismatch, span_mismatch):
            with self.subTest(raw=raw), self.assertRaises(PublicationContractError):
                CanonicalPublication.decode(raw)

    def test_direct_construction_validates_typed_fields_and_mutable_containers(self) -> None:
        with self.assertRaises(PublicationContractError):
            Reference("unknown", "id")
        with self.assertRaises(PublicationContractError):
            Span(True, 1, "x", None)
        with self.assertRaises(PublicationContractError):
            Block("b", "source_statement", 0, 0, "", [], (), "", ())
        with self.assertRaises(PublicationContractError):
            CanonicalPublication("title", "", [], (), schema_version=VERSION)


if __name__ == "__main__":
    unittest.main()
