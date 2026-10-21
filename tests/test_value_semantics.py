from copy import deepcopy
from pathlib import Path
import unittest

from workbench.publication_contracts import CanonicalPublication, PublicationContractError
from workbench.report_publications import ReportPublications, prepare, project
from workbench.report_reader import _evidence_bundle
from workbench.research_tools import ToolError


def value(kind, identifier, formation, attribution=None):
    return {
        "kind": kind,
        "id": identifier,
        "value_formation": formation,
        "attribution": attribution,
    }


def block(parts):
    return {
        "classification": "source_statement",
        "parts": parts,
        "support": [],
        "reasoning": "",
        "limitations": [],
    }


class NumericValueSemanticsTests(unittest.TestCase):
    def setUp(self):
        dimensions = {
            "company_id": "entity:test",
            "metric": "metric",
            "period_start": "2026-01-01",
            "period_end": "2026-12-31",
            "period_kind": "year",
            "basis": "test basis",
        }
        self.facts = {
            "observed": {**dimensions, "value": "10", "unit": "count", "evidence_id": "e-observed"},
            "estimated": {**dimensions, "value": "12", "unit": "count", "evidence_id": "e-estimated"},
            "assumed": {**dimensions, "value": "8", "unit": "count", "evidence_id": "e-assumed"},
        }
        self.calculations = {
            "derived": {**dimensions, "value": "2", "unit": "count"},
        }
        self.refs = {
            "evidence": {key: {} for key in ("e-observed", "e-estimated", "e-assumed")},
            "calculation": {"derived": {}},
            "passage": {"statement": {}},
            "query": {"query-1": {"record": {
                "query_id": "query-1", "columns": [{"name": "amount"}],
                "rows": [[7]], "truncated": False,
            }}},
        }
        self.attribution = {
            "principal": {
                "type": "organization",
                "identifier": "org:forecast-office",
                "label": "Forecast Office",
            },
            "stated_at": "2026-10-04T09:00:22Z",
            "statement_reference": {"kind": "passage", "id": "statement"},
        }

    def prepare(self, parts, **overrides):
        arguments = {
            "title": "数值语义测试",
            "blocks": [block(parts)],
            "refs": self.refs,
            "facts": self.facts,
            "calculations": self.calculations,
            "max_chars": 10000,
        }
        arguments.update(overrides)
        return prepare(**arguments)

    def test_construction_rejects_missing_formation_and_invalid_kind_combinations(self):
        with self.assertRaises(ToolError):
            self.prepare([{"kind": "fact", "id": "observed", "attribution": None}])
        for part in (
            value("calculation", "derived", "observed"),
            value("fact", "observed", "derived"),
            {**value("query_cell", "query-1", "derived"), "row": 0, "column": 0},
        ):
            with self.subTest(part=part), self.assertRaises(ToolError):
                self.prepare([part])

    def test_attribution_is_complete_current_run_and_not_allowed_on_calculation(self):
        for missing in ("principal", "stated_at", "statement_reference"):
            invalid = deepcopy(self.attribution)
            invalid.pop(missing)
            with self.subTest(missing=missing), self.assertRaises(ToolError):
                self.prepare([value("fact", "estimated", "estimated", invalid)])
        outside = deepcopy(self.attribution)
        outside["statement_reference"] = {"kind": "passage", "id": "not-in-run"}
        with self.assertRaises(ToolError) as raised:
            self.prepare([value("fact", "estimated", "estimated", outside)])
        self.assertEqual(raised.exception.code, "invalid_report_reference")
        with self.assertRaises(ToolError):
            self.prepare([value("calculation", "derived", "derived", self.attribution)])

    def test_all_formations_and_attribution_are_frozen_and_projected_to_reader_evidence(self):
        parts = [
            value("fact", "observed", "observed"),
            {"kind": "text", "text": " / "},
            value("fact", "estimated", "estimated", self.attribution),
            {"kind": "text", "text": " / "},
            value("fact", "assumed", "assumed"),
            {"kind": "text", "text": " / "},
            value("calculation", "derived", "derived"),
        ]
        publication = self.prepare(parts)
        bindings = [span["binding"] for span in publication["blocks"][0]["spans"] if span["binding"]]
        self.assertEqual([binding["value_formation"] for binding in bindings],
                         ["observed", "estimated", "assumed", "derived"])
        self.assertIsNone(bindings[0]["attribution"])
        self.assertEqual(bindings[1]["attribution"], self.attribution)
        self.assertIsNone(bindings[2]["attribution"])
        self.assertIsNone(bindings[3]["attribution"])
        self.assertIn(self.attribution["statement_reference"], publication["blocks"][0]["support"])

        citation_map = {(reference["kind"], reference["id"]): {"valid": True}
                        for reference in publication["blocks"][0]["support"]}
        _findings, anchors = project(publication, citation_map, {})
        numeric = [anchor for anchor in anchors if "value_formation" in anchor]
        estimated = next(anchor for anchor in numeric if anchor["value_formation"] == "estimated")
        evidence = _evidence_bundle(estimated, {})
        self.assertEqual(evidence["value_formation"], "estimated")
        self.assertEqual(evidence["attribution"], self.attribution)
        self.assertNotEqual(evidence["value_formation"], "observed")
        self.assertTrue(any(
            reference["kind"] == "passage" and reference["id"] == "statement"
            for reference in estimated["references"]
        ))

    def test_dataset_points_require_and_freeze_the_same_dimensions(self):
        point = {"label": "观察值", **value("fact", "observed", "observed")}
        publication = self.prepare(
            [{"kind": "text", "text": "数据如下。"}],
            datasets=[{
                "title": "数据", "description": "显式数据点", "display": "table",
                "points": [point],
            }],
        )
        binding = publication["datasets"][0]["points"][0]["binding"]
        self.assertEqual(binding["value_formation"], "observed")
        self.assertIsNone(binding["attribution"])
        invalid = deepcopy(point)
        invalid.pop("value_formation")
        with self.assertRaises(ToolError):
            self.prepare(
                [{"kind": "text", "text": "数据如下。"}],
                datasets=[{
                    "title": "数据", "description": "显式数据点", "display": "table",
                    "points": [invalid],
                }],
            )

    def test_formal_contract_rejects_missing_unknown_and_wrong_combinations(self):
        publication = self.prepare([value("fact", "observed", "observed")])
        binding = publication["blocks"][0]["spans"][0]["binding"]
        cases = []
        missing = deepcopy(publication)
        missing["blocks"][0]["spans"][0]["binding"].pop("value_formation")
        cases.append(missing)
        unknown = deepcopy(publication)
        unknown["blocks"][0]["spans"][0]["binding"]["management_guidance"] = True
        cases.append(unknown)
        wrong = deepcopy(publication)
        wrong_binding = wrong["blocks"][0]["spans"][0]["binding"]
        wrong_binding["value_formation"] = "derived"
        cases.append(wrong)
        for candidate in cases:
            with self.subTest(binding=binding), self.assertRaises(PublicationContractError):
                CanonicalPublication.decode(candidate)

    def test_reader_labels_keep_formation_and_attribution_separate(self):
        script = (Path(__file__).resolve().parents[1] / "web" / "report-reader.js").read_text()
        self.assertIn('evidenceCard("形成方式"', script)
        self.assertIn('evidenceCard("陈述归属"', script)
        self.assertIn("估计值：对未知或未来值的估计；不是已观察事实。", script)
        self.assertIn("这表示陈述归属，不表示系统认可为事实。", script)

    def test_provider_schema_closes_numeric_objects_and_requires_both_fields(self):
        definition = ReportPublications.definition(lambda **_kwargs: None)
        properties = definition["parameters"]["properties"]
        part_schemas = properties["blocks"]["items"]["properties"]["parts"]["items"]["oneOf"]
        for schema in part_schemas[1:]:
            self.assertFalse(schema["additionalProperties"])
            self.assertIn("value_formation", schema["required"])
            self.assertIn("attribution", schema["required"])
        point = properties["datasets"]["items"]["properties"]["points"]["items"]
        self.assertFalse(point["additionalProperties"])
        self.assertIn("value_formation", point["required"])
        self.assertIn("attribution", point["required"])


if __name__ == "__main__":
    unittest.main()
