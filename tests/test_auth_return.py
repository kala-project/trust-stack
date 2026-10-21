"""Client-side allowlisted login return-path contract."""
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node is required for the auth return contract")
class AuthReturnContractTests(unittest.TestCase):
    def test_only_allowlisted_same_origin_destinations_are_restored(self):
        result = subprocess.run(
            ["node", "tests/run_auth_return_contract.js"],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("14 checks", result.stdout)


if __name__ == "__main__":
    unittest.main()
