from pathlib import Path
import tempfile
import unittest

from workbench.init_public import initialize_public_database
from workbench.service import EvidenceService


class PublicInitializationTests(unittest.TestCase):
    def test_clean_checkout_database_is_valid_empty_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.sqlite3"
            self.assertEqual(initialize_public_database(path), path.resolve())
            service = EvidenceService(path)
            self.assertEqual(service.validate_store()["status"], "ok")
            overview = service.overview()
            self.assertEqual([item["id"] for item in overview["companies"]], ["CRM", "MSFT", "NVDA"])
            self.assertEqual(overview["stats"], {"sources": 0, "facts": 0, "claims": 0})
            with self.assertRaisesRegex(RuntimeError, "refusing to replace"):
                initialize_public_database(path)


if __name__ == "__main__":
    unittest.main()
