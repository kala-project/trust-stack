from pathlib import Path
import subprocess
import sys
import unittest

from workbench.research_tools import ToolError as ResearchToolError
from workbench.tool_errors import ToolError


class ToolErrorTests(unittest.TestCase):
    def test_legacy_export_has_neutral_class_identity_and_value_error_behavior(self):
        self.assertIs(ResearchToolError, ToolError)
        self.assertTrue(issubclass(ToolError, ValueError))
        error = ToolError("invalid_arguments", "invalid value")
        self.assertEqual(str(error), "invalid value")
        self.assertEqual(error.code, "invalid_arguments")
        self.assertEqual(error.message, "invalid value")

    def test_as_dict_without_budget(self):
        error = ToolError("out_of_scope", "subject is unavailable")
        self.assertEqual(error.as_dict(), {
            "error": "out_of_scope",
            "message": "subject is unavailable",
        })

    def test_as_dict_copies_tool_budget(self):
        budget = {"remaining": 2, "limit": 5}
        error = ToolError("budget_exhausted", "tool budget is exhausted")
        error.tool_budget = budget

        result = error.as_dict()

        self.assertEqual(result["_tool_budget"], budget)
        self.assertIsNot(result["_tool_budget"], budget)
        result["_tool_budget"]["remaining"] = 0
        self.assertEqual(budget["remaining"], 2)

    def test_persistence_modules_do_not_load_research_tools(self):
        root = Path(__file__).resolve().parents[1]
        for module in ("workbench.research_inputs", "workbench.execution_scope"):
            with self.subTest(module=module):
                subprocess.run(
                    [sys.executable, "-c", (
                        "import importlib, sys; "
                        f"importlib.import_module({module!r}); "
                        "assert 'workbench.research_tools' not in sys.modules"
                    )],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                )


if __name__ == "__main__":
    unittest.main()
