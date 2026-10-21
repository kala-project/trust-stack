import hashlib
import json
import unittest

from workbench.data_contracts import (
    EvidenceRef,
    PageTextSpanLocator,
    SourceVersion,
    SqlRowLocator,
    TableCellLocator,
    TableTextSpanLocator,
    TextSpanLocator,
    evidence_ref_from_json,
    evidence_locator_from_json,
    source_version_from_json,
)


DIGEST = hashlib.sha256(b"exact provider bytes").hexdigest()


class DataContractTests(unittest.TestCase):
    def source(self, scope="public"):
        return SourceVersion("provider/report", "version-1", 1, DIGEST, 20, scope)

    def test_all_locator_variants_round_trip_through_http_json_shape(self):
        locators = (
            TextSpanLocator("text_span", 0, 8),
            PageTextSpanLocator("page_text_span", 2, 10, 20, 0, 30, "pdf_text"),
            TableTextSpanLocator("table_text_span", 5, 12, 3, 1),
            TableCellLocator("table_cell", 3, 1, 2),
            SqlRowLocator("sql_row", "filings", (("company_id", "ACME"), ("year", "2025"))),
        )
        for locator in locators:
            with self.subTest(locator=locator.type):
                reference = EvidenceRef(self.source(), locator, "parsed", "parser/1")
                wire_value = json.loads(json.dumps(reference.to_json()))
                self.assertEqual(evidence_ref_from_json(wire_value), reference)

        sql_wire = EvidenceRef(self.source(), locators[-1], "derived", "sql/1").to_json()
        self.assertEqual(sql_wire["locator"]["primary_key"], [
            ["company_id", "ACME"], ["year", "2025"],
        ])

    def test_public_and_private_source_scopes_are_explicit_and_round_trip(self):
        for scope in ("public", "private"):
            with self.subTest(scope=scope):
                source = self.source(scope)
                wire_value = json.loads(json.dumps(source.to_json()))
                self.assertEqual(source_version_from_json(wire_value), source)
                self.assertEqual(wire_value["scope"], scope)

    def test_construction_rejects_wrong_runtime_contract_types(self):
        locator = TextSpanLocator("text_span", 0, 1)
        with self.assertRaises(ValueError):
            EvidenceRef(self.source().to_json(), locator, "parsed", "parser/1")
        with self.assertRaises(ValueError):
            EvidenceRef(self.source(), {"type": "text_span", "start": 0, "end": 1},
                        "parsed", "parser/1")
        with self.assertRaises(ValueError):
            SourceVersion("source", "version", True, DIGEST, 20, "public")
        with self.assertRaises(ValueError):
            TextSpanLocator("text_span", False, 1)

    def test_decoders_reject_unknown_fields_types_discriminators_and_status(self):
        source = self.source().to_json()
        locator = TextSpanLocator("text_span", 0, 1).to_json()
        reference = EvidenceRef(self.source(), TextSpanLocator("text_span", 0, 1),
                                "parsed", "parser/1").to_json()

        invalid_sources = (
            {**source, "unknown": "field"},
            {**source, "byte_length": True},
            {**source, "version": False},
            {**source, "scope": "shared"},
        )
        for value in invalid_sources:
            with self.subTest(source=value), self.assertRaises(ValueError):
                source_version_from_json(value)

        invalid_locators = (
            {**locator, "unknown": "field"},
            {**locator, "type": "character_range"},
            {**locator, "start": False},
            {"type": "sql_row", "table": "filings", "primary_key": [["id", 7]]},
            {"type": "sql_row", "table": "filings", "primary_key": (("id", "7"),)},
        )
        for value in invalid_locators:
            with self.subTest(locator=value), self.assertRaises(ValueError):
                evidence_locator_from_json(value)

        invalid_references = (
            {**reference, "unknown": "field"},
            {**reference, "processing_status": "approved"},
            {**reference, "parser_version": ""},
            {**reference, "source": self.source()},
            {**reference, "locator": TextSpanLocator("text_span", 0, 1)},
        )
        for value in invalid_references:
            with self.subTest(reference=value), self.assertRaises(ValueError):
                evidence_ref_from_json(value)


if __name__ == "__main__":
    unittest.main()
