"""Contract tests for the standalone structured comparison-table helper."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node is required for the reader tables contract")
class ReportReaderTablesContractTests(unittest.TestCase):
    def test_explicit_point_mapping_and_fail_closed_validation(self):
        result = subprocess.run(
            ["node", "tests/run_report_reader_tables_contract.js"],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("16 checks", result.stdout)


if __name__ == "__main__":
    unittest.main()
