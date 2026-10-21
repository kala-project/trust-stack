"""Value identities must never be confused with their declared evidence identities."""
from copy import deepcopy
import unittest

from workbench.report_publications import reference_dependencies
from workbench.tool_errors import ToolError
from workbench.verification_executor import VerificationApplication


class PublicationDependencyTests(unittest.TestCase):
    def test_mixed_value_shapes_use_explicit_reference_only(self):
        bindings = [
            {"kind": "fact", "id": "fact-a", "reference": {"kind": "evidence", "id": "source-a"}},
            {"kind": "query_cell", "id": "sql-a", "reference": {"kind": "query", "id": "sql-a"}},
            {"kind": "calculation", "id": "calc-a", "reference": {"kind": "calculation", "id": "calc-a"}},
        ]
        # Non-reference metadata may have the same dictionary shape. It is not a citation.
        bindings[0]["dimensions"] = {"kind": "passage", "id": "not-a-reference"}
        publication = {"blocks": [{"support": [{"kind": "passage", "id": "quoted"}],
                                   "spans": [{"binding": b} for b in bindings]}],
                       "datasets": [{"support": [{"kind": "passage", "id": "method"}],
                                     "points": [{"binding": b} for b in bindings]}]}
        before = deepcopy(publication)
        expected = (("passage", "quoted"), ("evidence", "source-a"),
                    ("query", "sql-a"), ("calculation", "calc-a"))
        self.assertEqual(reference_dependencies(publication, "report-block-1"), expected)
        self.assertEqual(VerificationApplication._node_references(publication, "report-block-1"), expected)
        self.assertEqual(reference_dependencies(publication, "report-dataset-1"),
                         (("passage", "method"), *expected[1:]))
        self.assertEqual(reference_dependencies(publication, "report-title"), ())
        self.assertEqual(publication, before)

    def test_missing_or_invalid_declared_reference_fails_closed(self):
        for binding in ({"kind": "fact", "id": "f"},
                        {"kind": "query_cell", "id": "q", "reference": {"kind": "fact", "id": "f"}}):
            with self.subTest(binding=binding), self.assertRaises(ToolError):
                reference_dependencies({"blocks": [{"support": [], "spans": [{"binding": binding}]}]},
                                       "report-block-1")
        with self.assertRaises(ToolError):
            reference_dependencies({"blocks": []}, "report-block-1")


if __name__ == "__main__":
    unittest.main()
