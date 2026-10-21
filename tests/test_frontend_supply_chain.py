"""Controlled metadata tests, not real package provenance evidence."""
import copy
import unittest
from scripts.audit_frontend_lock import validate_entry

class FrontendSupplyChainTests(unittest.TestCase):
    def setUp(self):
        self.entry = {"version":"1.2.3", "resolved":"https://registry.npmjs.org/react/-/react-1.2.3.tgz", "integrity":"sha512-example"}
        self.metadata = {"time":{"1.2.3":"2026-08-01T00:00:00Z"}, "versions":{"1.2.3":{"name":"react","version":"1.2.3","license":"MIT","dist":{"tarball":self.entry["resolved"],"integrity":self.entry["integrity"]}}}}
    def test_old_exact_metadata_matches(self):
        self.assertEqual(validate_entry("react",self.entry,self.metadata)["version"],"1.2.3")
    def test_young_package_rejected(self):
        self.metadata["time"]["1.2.3"]="2026-09-10T00:00:00Z"
        with self.assertRaises(ValueError): validate_entry("react",self.entry,self.metadata)
    def test_non_registry_and_integrity_changes_rejected(self):
        for field,value in [("resolved","https://untrusted.example/a.tgz"),("integrity","sha512-other")]:
            entry=copy.deepcopy(self.entry); entry[field]=value
            with self.assertRaises(ValueError): validate_entry("react",entry,self.metadata)
    def test_unknown_package_rejected(self):
        with self.assertRaises(ValueError): validate_entry("unknown-widget",self.entry,self.metadata)
    def test_prerelease_and_deprecated_rejected(self):
        entry=copy.deepcopy(self.entry); entry["version"]="1.2.3-beta.1"
        with self.assertRaises(ValueError): validate_entry("react",entry,self.metadata)
        self.metadata["versions"]["1.2.3"]["deprecated"]="do not use"
        with self.assertRaises(ValueError): validate_entry("react",self.entry,self.metadata)

if __name__ == "__main__": unittest.main()
