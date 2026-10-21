"""Model-facing matrix guidance must agree with strict publication validation."""
from copy import deepcopy
import unittest

from workbench.report_publications import ReportPublications, _matrix_presentation
from workbench.tool_errors import ToolError


class MatrixPublicationGuidanceTests(unittest.TestCase):
    def presentation(self):
        return {"after_block_id": "report-block-1", "unit": "USD millions",
                "columns": [{"key": "revenue", "label": "Revenue"}],
                "rows": [{"key": "fy2025", "label": "FY2025", "cells": [{"point_index": 0}]}]}

    def test_schema_distinguishes_row_header_and_canonical_unit(self):
        definition = ReportPublications.definition(lambda **kwargs: kwargs)
        schema = definition["parameters"]["properties"]["datasets"]["items"]["properties"]["presentation"]
        self.assertIn("rows[].label", schema["description"])
        self.assertIn("rendered separately", schema["description"])
        self.assertIn("canonical unit", schema["properties"]["unit"]["description"])

    def test_duplicate_period_column_reports_an_actionable_error_without_repairing_data(self):
        value = self.presentation()
        value["columns"].insert(0, {"key": "period", "label": "Fiscal year"})
        original = deepcopy(value)
        with self.assertRaisesRegex(ToolError, "2 numeric cells.*row label is rendered separately"):
            _matrix_presentation(value, dataset_index=1, point_count=1, block_count=1,
                                 allowed_units={"USD millions"})
        self.assertEqual(value, original)

    def test_row_header_needs_no_fake_point_and_units_must_be_known(self):
        value = self.presentation()
        result = _matrix_presentation(value, dataset_index=1, point_count=1, block_count=1,
                                      allowed_units={"USD millions"})
        self.assertEqual(result["rows"][0]["label"], "FY2025")
        self.assertEqual(len(result["columns"]), 1)
        value["unit"] = "百万美元"
        localized = _matrix_presentation(value, dataset_index=1, point_count=1, block_count=1,
                                         allowed_units={"USD millions", "百万美元"})
        self.assertEqual(localized["unit"], "百万美元")
        value["unit"] = "USD billions"
        with self.assertRaisesRegex(ToolError, "unit must match"):
            _matrix_presentation(value, dataset_index=1, point_count=1, block_count=1,
                                 allowed_units={"USD millions"})


if __name__ == "__main__":
    unittest.main()
