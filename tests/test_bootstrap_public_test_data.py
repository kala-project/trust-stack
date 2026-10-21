import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.bootstrap_public_test_data import bootstrap, _load_bundle, _validate_download
from workbench.official_sources import NetworkResponse
from workbench.source_archive import SourceArchive, load_source_config


class PublicTestDataBootstrapTests(unittest.TestCase):
    def test_repository_bundle_is_valid_and_covers_three_scenarios(self):
        root = Path(__file__).resolve().parents[1]
        bundle = _load_bundle(root / "config/public-test-data.json")
        self.assertEqual(bundle["bundle_id"], "real-analyst-three-scenarios/1")
        self.assertEqual({item["company_id"] for item in bundle["sources"]},
                         {"MSFT", "NVDA", "CRM"})
        self.assertEqual({item["company_id"] for item in bundle["scenarios"]},
                         {"MSFT", "NVDA", "CRM"})
        self.assertTrue(all(item["analyst"] and item["question"]
                            for item in bundle["scenarios"]))
        self.assertEqual(len(bundle["sources"]), 19)
        for item in bundle["sources"]:
            normalized = load_source_config(
                root / "config/public-test-data.json", item["source_id"])
            self.assertEqual(normalized["url"], item["url"])
        self.assertEqual(
            {company: sum(item["company_id"] == company for item in bundle["sources"])
             for company in ("MSFT", "NVDA", "CRM")},
            {"MSFT": 8, "NVDA": 5, "CRM": 6})
        self.assertEqual(sum("exact_sha256" in item["bootstrap_validation"]
                             for item in bundle["sources"]), 5)
        self.assertEqual(sum("required_text" in item["bootstrap_validation"]
                             for item in bundle["sources"]), 14)
        self.assertTrue(all(item["rights_status"] == "review_required"
                            for item in bundle["sources"]))
        transcript = next(item for item in bundle["sources"]
                          if item["source_id"] == "nvda-fy2025-q4-call-fool-globe-transcript")
        self.assertIn("republication", transcript["publisher"])
        self.assertIn("secondary", transcript["title"])

    def test_exact_document_rejects_different_bytes(self):
        raw = b"%PDF-1.7 pinned document"
        source = {
            "source_id": "pinned",
            "bootstrap_validation": {"exact_sha256": "0" * 64},
        }
        result = {"source_sha256": hashlib.sha256(raw).hexdigest(),
                  "byte_length": len(raw), "media_type": "application/pdf"}
        with self.assertRaisesRegex(ValueError, "differ from the pinned"):
            _validate_download(source, result, raw)

    def test_publisher_page_accepts_drift_but_requires_identity_text(self):
        raw = b"<html>Microsoft Fourth Quarter 2025 updated page</html>"
        digest = hashlib.sha256(raw).hexdigest()
        source = {
            "source_id": "dynamic",
            "bootstrap_validation": {
                "reference_sha256": "0" * 64,
                "required_text": ["Microsoft", "Fourth Quarter", "2025"],
                "minimum_bytes": 20,
                "maximum_bytes": 200,
                "minimum_visible_chars": 10,
            },
        }
        checked = _validate_download(
            source, {"source_sha256": digest, "byte_length": len(raw),
                     "media_type": "text/html"}, raw)
        self.assertTrue(checked["changed_from_reference"])
        self.assertEqual(checked["identity_policy"], "publisher_text")
        source["bootstrap_validation"]["required_text"].append("missing")
        with self.assertRaisesRegex(ValueError, "missing required identity"):
            _validate_download(
                source, {"source_sha256": digest, "byte_length": len(raw),
                         "media_type": "text/html"}, raw)

    def test_challenge_page_is_rejected_even_when_it_contains_all_markers(self):
        raw = (b"<!doctype html><html><body>Just a moment - Microsoft Fourth Quarter "
               b"2025 - verify you are human</body></html>")
        digest = hashlib.sha256(raw).hexdigest()
        source = {
            "source_id": "challenge",
            "bootstrap_validation": {
                "reference_sha256": "0" * 64,
                "required_text": ["Microsoft", "Fourth Quarter", "2025"],
                "minimum_bytes": 20,
                "maximum_bytes": 200,
                "minimum_visible_chars": 10,
            },
        }
        with self.assertRaisesRegex(ValueError, "access-challenge"):
            _validate_download(
                source, {"source_sha256": digest, "byte_length": len(raw),
                         "media_type": "text/html"}, raw)

    def test_identity_markers_in_script_do_not_count_as_visible_report_text(self):
        raw = (b"<!doctype html><html><script>Microsoft Fourth Quarter 2025</script>"
               b"<body>Please continue through the publisher gateway.</body></html>")
        digest = hashlib.sha256(raw).hexdigest()
        source = {
            "source_id": "hidden-markers",
            "bootstrap_validation": {
                "reference_sha256": "0" * 64,
                "required_text": ["Microsoft", "Fourth Quarter", "2025"],
                "minimum_bytes": 20, "maximum_bytes": 300,
                "minimum_visible_chars": 10,
            },
        }
        with self.assertRaisesRegex(ValueError, "missing required identity"):
            _validate_download(
                source, {"source_sha256": digest, "byte_length": len(raw),
                         "media_type": "text/html"}, raw)

    def test_adjacent_period_page_cannot_pass_another_period_identity(self):
        fy2025 = (b"<!doctype html><html><body>Productivity 106,820; Cloud 87,464; "
                  b"Computing 50,838</body></html>")
        digest = hashlib.sha256(fy2025).hexdigest()
        fy2024_identity = {
            "source_id": "msft-fy2024-segments",
            "bootstrap_validation": {
                "reference_sha256": "0" * 64,
                "required_text": ["77,728", "105,362", "62,032"],
                "minimum_bytes": 20, "maximum_bytes": 300,
                "minimum_visible_chars": 10,
            },
        }
        with self.assertRaisesRegex(ValueError, "missing required identity"):
            _validate_download(
                fy2024_identity,
                {"source_sha256": digest, "byte_length": len(fy2025),
                 "media_type": "text/html"}, fy2025)

    def test_empty_root_downloads_validates_enrolls_and_then_reuses_local(self):
        raw = b"<!doctype html><html><body>ACME fiscal 2026 results</body></html>"
        digest = hashlib.sha256(raw).hexdigest()
        value = {
            "schema_version": "public-test-data-bundle/1",
            "bundle_id": "test/1",
            "sources": [{
                "source_id": "acme-results", "company_id": "ACME",
                "title": "ACME results", "url": "https://example.com/results",
                "published_at": "2026-01-01", "media_types": ["text/html"],
                "rights_status": "review_required", "timeout_seconds": 2,
                "bootstrap_validation": {
                    "reference_sha256": digest,
                    "required_text": ["ACME", "fiscal 2026", "results"],
                    "minimum_bytes": 20,
                    "maximum_bytes": 200,
                    "minimum_visible_chars": 10,
                },
            }],
        }

        class Transport:
            calls = 0

            def get(self, url, **kwargs):
                self.calls += 1
                return NetworkResponse(200, {"content-type": "text/html"}, raw)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "documents"
            path = Path(directory) / "bundle.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            transport = Transport()
            factory = lambda archive_root: SourceArchive(
                archive_root, transport=transport, resolver=lambda host: ["93.184.216.34"])
            with mock.patch("scripts.bootstrap_public_test_data.SourceArchive", side_effect=factory):
                code, first = bootstrap(
                    root=root, config_path=path, refresh=False, enroll=True)
                code2, second = bootstrap(
                    root=root, config_path=path, refresh=False, enroll=True)
            self.assertEqual((code, code2), (0, 0))
            self.assertEqual(transport.calls, 1)
            self.assertEqual(first["sources"][0]["status"], "enrolled")
            self.assertEqual(first["sources"][0]["retrieval"], "network")
            self.assertEqual(second["sources"][0]["retrieval"], "local")
            self.assertEqual(first["sources"][0]["document_version_id"],
                             second["sources"][0]["document_version_id"])
            self.assertEqual(first["sources"][0]["parse_status"], "parsed")
            self.assertEqual(first["summary"]["searchable"], 1)

    def test_partial_failure_keeps_success_and_does_not_archive_challenge(self):
        good = b"<!doctype html><html><body>ACME valid results</body></html>"
        bad = b"<!doctype html><html><body>Just a moment ACME bad results</body></html>"
        sources = []
        for source_id, marker, raw in (("good", "valid", good), ("bad", "bad", bad)):
            sources.append({
                "source_id": source_id, "company_id": "ACME", "title": source_id,
                "url": f"https://example.com/{source_id}", "published_at": "2026-01-01",
                "media_types": ["text/html"], "rights_status": "review_required",
                "bootstrap_validation": {
                    "reference_sha256": hashlib.sha256(raw).hexdigest(),
                    "required_text": ["ACME", marker, "results"],
                    "minimum_bytes": 20, "maximum_bytes": 200,
                    "minimum_visible_chars": 10,
                },
            })
        value = {"schema_version": "public-test-data-bundle/1",
                 "bundle_id": "partial/1", "sources": sources}

        class Transport:
            def __init__(self):
                self.responses = [good, bad]

            def get(self, url, **kwargs):
                return NetworkResponse(
                    200, {"content-type": "text/html"}, self.responses.pop(0))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "documents"
            path = Path(directory) / "bundle.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            transport = Transport()
            factory = lambda archive_root: SourceArchive(
                archive_root, transport=transport, resolver=lambda host: ["93.184.216.34"])
            with mock.patch("scripts.bootstrap_public_test_data.SourceArchive", side_effect=factory):
                code, report = bootstrap(
                    root=root, config_path=path, refresh=False, enroll=True)
            self.assertEqual(code, 2)
            self.assertEqual(report["summary"], {
                "requested": 2, "succeeded": 1, "failed": 1,
                "validated_bytes": len(good), "searchable": 1})
            archive = SourceArchive(root, transport=Transport(),
                                    resolver=lambda host: ["93.184.216.34"])
            self.assertEqual(len(archive.history("good")["versions"]), 1)
            self.assertEqual(len(archive.history("bad")["versions"]), 0)
            self.assertEqual(archive.history("bad")["attempts"][0]["outcome"], "failure")

    def test_registered_but_unparsed_document_fails_readiness(self):
        raw = b"%PDF-1.7 test document"
        digest = hashlib.sha256(raw).hexdigest()
        value = {"schema_version": "public-test-data-bundle/1", "bundle_id": "pdf/1",
                 "sources": [{
                     "source_id": "pdf", "company_id": "ACME", "title": "PDF",
                     "url": "https://example.com/report.pdf", "media_types": ["application/pdf"],
                     "rights_status": "review_required",
                     "bootstrap_validation": {"exact_sha256": digest},
                 }]}

        class Archive:
            def __init__(self, root):
                pass

            def fetch(self, config, **kwargs):
                kwargs["precommit_validator"](raw, "application/pdf", config["url"])
                return {"source_version": 1, "source_sha256": digest,
                        "byte_length": len(raw), "media_type": "application/pdf",
                        "retrieval": "network"}

            def read(self, source_id, *, source_version):
                return raw

            def enroll(self, source_id, *, source_version):
                return {"document_version_id": "d" * 64, "parse_status": "unsupported_pdf"}

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with mock.patch("scripts.bootstrap_public_test_data.SourceArchive", Archive):
                code, report = bootstrap(
                    root=Path(directory) / "documents", config_path=path,
                    refresh=False, enroll=True)
        self.assertEqual(code, 2)
        self.assertEqual(report["sources"][0]["status"], "registered_unusable")
        self.assertEqual(report["sources"][0]["error"], "document_not_searchable")
        self.assertEqual(report["summary"]["searchable"], 0)

    def test_invalid_or_duplicate_manifest_is_rejected(self):
        value = {
            "schema_version": "public-test-data-bundle/1",
            "sources": [
                {"source_id": "same", "bootstrap_validation": {"exact_sha256": "0" * 64}},
                {"source_id": "same", "bootstrap_validation": {"exact_sha256": "0" * 64}},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "unique"):
                _load_bundle(path)


if __name__ == "__main__":
    unittest.main()
