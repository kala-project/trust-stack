import contextlib
import hashlib
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import traceback
import unittest
from unittest import mock

from workbench.documents import DocumentService
from workbench.official_sources import NetworkResponse
from workbench.source_archive import SourceArchive, SourceArchiveError
from workbench.source_cli import main as source_main


PUBLIC = ["93.184.216.34"]
URL = "https://example.com/report"
HTML1 = b"<!doctype html><html><body>Archived one</body></html>"
HTML2 = b"<!doctype html><html><body>Archived two</body></html>"


def response(body=HTML1, status=200, content_type="text/html", **headers):
    return NetworkResponse(status, {"content-type": content_type, **headers}, body)


class QueueTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.lock = threading.Lock()

    def get(self, url, **kwargs):
        with self.lock:
            self.calls.append((url, kwargs))
            if not self.responses:
                raise AssertionError("unexpected network access")
            result = self.responses.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result


class SourceArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "documents"

    def tearDown(self):
        self.temporary.cleanup()

    def config(self, **changes):
        value = {
            "source_id": "issuer-report", "title": "Issuer report", "company_id": "ACME",
            "url": URL, "allowed_urls": [URL], "published_at": "2026-01-01",
            "rights_status": "review_required", "media_types": ["text/html", "application/pdf"],
            "max_bytes": 1000, "timeout_seconds": 2,
        }
        value.update(changes)
        return value

    def archive(self, transport, resolver=lambda host: PUBLIC):
        return SourceArchive(self.root, transport=transport, resolver=resolver)

    def assert_code(self, code, call):
        with self.assertRaises(SourceArchiveError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def import_bytes(self, archive, raw, *, media_type="text/html", config=None):
        source = Path(self.temporary.name) / f"source-{hashlib.sha256(raw).hexdigest()}.bin"
        source.write_bytes(raw)
        return archive.import_local(
            config or self.config(max_bytes=max(1000, len(raw))), source,
            expected_sha256=hashlib.sha256(raw).hexdigest(), expected_size=len(raw),
            media_type=media_type,
        )

    def test_prefer_local_refresh_versions_history_and_old_read(self):
        transport = QueueTransport([response(HTML1), response(HTML1), response(HTML2)])
        first_service = self.archive(transport)
        first = first_service.fetch(self.config())
        self.assertEqual(first["source_version"], 1)
        self.assertEqual(first["retrieval"], "network")

        # A distinct process-like instance uses the ledger and verifies bytes without DNS/HTTP.
        second = SourceArchive(self.root, transport=QueueTransport([]),
                               resolver=lambda host: (_ for _ in ()).throw(AssertionError("DNS used")))
        local = second.fetch(self.config())
        self.assertEqual(local["archive_version_id"], first["archive_version_id"])
        self.assertEqual(local["retrieval"], "local")

        same = first_service.fetch(self.config(), refresh=True)
        changed = first_service.fetch(self.config(), refresh=True)
        self.assertEqual(same["archive_version_id"], first["archive_version_id"])
        self.assertEqual(changed["source_version"], 2)
        self.assertEqual(first_service.read("issuer-report", source_version=1), HTML1)
        self.assertEqual(first_service.read("issuer-report", source_version=2), HTML2)
        history = first_service.history("issuer-report")
        self.assertEqual(len(history["versions"]), 2)
        self.assertEqual(len(history["attempts"]), 3)
        self.assertTrue(all(item["outcome"] == "success" for item in history["attempts"]))

    def test_precommit_validation_rejects_network_bytes_and_reuses_older_valid_local(self):
        transport = QueueTransport([response(HTML1), response(HTML2), response(HTML2)])
        archive = self.archive(transport)

        def require_one(raw, media_type, final_url):
            if b"Archived one" not in raw:
                raise SourceArchiveError("source_identity_mismatch", "identity mismatch")

        first = archive.fetch(self.config(), precommit_validator=require_one)
        self.assertEqual(first["source_version"], 1)
        with self.assertRaisesRegex(SourceArchiveError, "identity mismatch"):
            archive.fetch(self.config(), refresh=True, precommit_validator=require_one)
        history = archive.history("issuer-report")
        self.assertEqual(len(history["versions"]), 1)
        self.assertEqual([item["outcome"] for item in history["attempts"]],
                         ["success", "failure"])
        self.assertEqual(history["attempts"][1]["http_status"], 200)
        self.assertEqual(history["attempts"][1]["final_url"], URL)
        self.assertEqual(history["attempts"][1]["response_metadata"]["content-type"],
                         "text/html")

        # A bad version archived by an older/unvalidated caller cannot poison
        # bootstrap-style local selection; the validated older version wins.
        unvalidated = archive.fetch(self.config(), refresh=True)
        self.assertEqual(unvalidated["source_version"], 2)
        local = archive.fetch(self.config(), precommit_validator=require_one)
        self.assertEqual(local["source_version"], 1)
        self.assertEqual(local["retrieval"], "local")
        self.assertEqual(len(transport.responses), 0)

    def test_content_reversion_keeps_latest_correct_without_duplicating_bytes(self):
        archive = self.archive(QueueTransport([response(HTML1), response(HTML2), response(HTML1)]))
        records = [archive.fetch(self.config(), refresh=True) for _ in range(3)]
        self.assertEqual([r["source_version"] for r in records], [1, 2, 3])
        self.assertEqual(archive.fetch(self.config())["archive_version_id"], records[2]["archive_version_id"])
        self.assertEqual(records[2]["supersedes_archive_version_id"], records[1]["archive_version_id"])
        self.assertEqual(archive.read("issuer-report", source_version=2), HTML2)
        self.assertEqual(archive.read("issuer-report", source_version=3), HTML1)
        self.assertEqual(len(list((self.root / "objects").iterdir())), 2)

    def test_l1_does_not_require_a_company_and_ledger_symlinks_are_rejected(self):
        archive = self.archive(QueueTransport([response()]))
        record = archive.fetch(self.config(company_id=None))
        self.assertIsNone(record["config"]["company_id"])
        self.assert_code("missing_enrollment_subject", lambda: archive.enroll("issuer-report", source_version=1))
        other_root = self.root / "linked"
        other_root.mkdir()
        (other_root / "source_acquisitions.sqlite3").symlink_to(archive.db_path)
        self.assert_code("unsafe_ledger_path", lambda: SourceArchive(other_root))

    def test_concurrent_first_initialization_is_atomic(self):
        gate = threading.Barrier(2)
        errors = []
        def initialize():
            try:
                gate.wait(2)
                self.archive(QueueTransport([]))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=initialize) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(12)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.archive(QueueTransport([])).history("issuer-report")["versions"], [])

    def test_metadata_change_is_a_new_version_but_identical_bytes_share_one_object(self):
        transport = QueueTransport([response(), response()])
        archive = self.archive(transport)
        first = archive.fetch(self.config())
        changed = archive.fetch(self.config(title="Corrected configured title"))
        self.assertEqual(changed["source_version"], 2)
        self.assertNotEqual(first["config_sha256"], changed["config_sha256"])
        objects = [item for item in (self.root / "objects").iterdir() if not item.name.startswith(".")]
        self.assertEqual(len(objects), 1)

    def test_failures_are_recorded_and_do_not_hide_last_success_then_recover(self):
        transport = QueueTransport([response(), response(status=403), response(HTML2)])
        archive = self.archive(transport)
        first = archive.fetch(self.config())
        self.assert_code("http_error", lambda: archive.fetch(self.config(), refresh=True))
        local = archive.fetch(self.config())
        self.assertEqual(local["archive_version_id"], first["archive_version_id"])
        recovered = archive.fetch(self.config(), refresh=True)
        self.assertEqual(recovered["source_version"], 2)
        history = archive.history("issuer-report")
        self.assertEqual([item["outcome"] for item in history["attempts"]],
                         ["success", "failure", "success"])
        self.assertEqual(history["attempts"][1]["error_code"], "http_error")
        self.assertEqual(history["attempts"][1]["http_status"], 403)
        self.assertEqual(history["attempts"][1]["final_url"], URL)

    def test_content_length_mismatches_record_failure_without_archiving_bytes(self):
        cases = (("short-body", len(HTML1) + 7), ("overlong-body", len(HTML1) - 1),
                 ("invalid-length", "not-a-number"), ("huge-length", "9" * 5000))
        archive = self.archive(QueueTransport([
            response(HTML1, **{"content-length": str(declared)}) for _, declared in cases
        ]))
        for source_id, _ in cases:
            with self.subTest(source_id=source_id):
                self.assert_code("incomplete_response", lambda: archive.fetch(
                    self.config(source_id=source_id), refresh=True))
                history = archive.history(source_id)
                self.assertEqual(history["versions"], [])
                self.assertEqual(len(history["attempts"]), 1)
                attempt = history["attempts"][0]
                self.assertEqual(attempt["outcome"], "failure")
                self.assertEqual(attempt["error_code"], "incomplete_response")
                self.assertEqual(attempt["http_status"], 200)
                self.assertEqual(attempt["final_url"], URL)
        self.assertEqual(list((self.root / "objects").iterdir()), [])

    def test_import_unknown_collection_time_is_not_fabricated_and_invalid_time_rejected(self):
        path = Path(self.temporary.name) / "old.html"
        path.write_bytes(HTML1)
        archive = self.archive(QueueTransport([]))
        kwargs = {"expected_sha256": hashlib.sha256(HTML1).hexdigest(),
                  "expected_size": len(HTML1), "media_type": "text/html"}
        for bad in ("2026-10-01", "2026-10-01T01:00:00", "not-a-time", 42):
            self.assert_code("invalid_manifest", lambda: archive.import_local(
                self.config(), path, fetched_at=bad, **kwargs))
        record = archive.import_local(self.config(), path, **kwargs)
        self.assertIsNone(record["fetched_at"])
        self.assertIsNone(archive.fetch(self.config())["fetched_at"])
        enrolled = archive.enroll("issuer-report", source_version=1)
        self.assertIsNone(enrolled["collected_at"])
        again = archive.enroll("issuer-report", source_version=1)
        self.assertEqual(enrolled["document_version_id"], again["document_version_id"])
        self.assertTrue(archive.history("issuer-report")["attempts"][0]["attempted_at"])

    def test_expected_transport_exceptions_are_sanitized_and_recorded(self):
        secret = "token=do-not-record"
        failures = (http.client.HTTPException(secret), OSError(secret))
        archive = self.archive(QueueTransport(failures))
        for index in range(len(failures)):
            source_id = f"transport-failure-{index}"
            with self.subTest(source_id=source_id):
                with self.assertRaises(SourceArchiveError) as caught:
                    archive.fetch(self.config(source_id=source_id), refresh=True)
                self.assertEqual(caught.exception.code, "missing_retrieval")
                self.assertEqual(caught.exception.message, "source request failed")
                self.assertNotIn(secret, "".join(traceback.format_exception(caught.exception)))
                history = archive.history(source_id)
                self.assertEqual(history["versions"], [])
                self.assertEqual(len(history["attempts"]), 1)
                attempt = history["attempts"][0]
                self.assertEqual(attempt["outcome"], "failure")
                self.assertEqual(attempt["error_code"], "missing_retrieval")
                self.assertEqual(attempt["error_message"], "source request failed")
                self.assertNotIn(secret, json.dumps(attempt))

    def test_size_redirect_public_ip_and_symlink_boundaries(self):
        oversized = self.archive(QueueTransport([response(b"x" * 11)]))
        self.assert_code("response_too_large", lambda: oversized.fetch(self.config(max_bytes=10)))

        other = "https://example.com/other"
        redirected = self.archive(QueueTransport([response(b"", status=302, location=other)]))
        self.assert_code("redirect_not_allowed", lambda: redirected.fetch(self.config(), refresh=True))

        local_target = SourceArchive(self.root / "local", transport=QueueTransport([]),
                                     resolver=lambda host: ["127.0.0.1"])
        self.assert_code("unsafe_network_target", lambda: local_target.fetch(self.config()))
        self.assertEqual(local_target.history("issuer-report")["attempts"][0]["outcome"], "failure")

        path = Path(self.temporary.name) / "source.html"
        path.write_bytes(HTML1)
        link = Path(self.temporary.name) / "link.html"
        link.symlink_to(path)
        digest = hashlib.sha256(HTML1).hexdigest()
        self.assert_code("unsafe_source_path", lambda: oversized.import_local(
            self.config(), link, expected_sha256=digest, expected_size=len(HTML1), media_type="text/html"))

    def test_l1_archives_pdf_and_json_but_only_supported_representation_enrolls(self):
        pdf = b"%PDF-1.4\n%%EOF"
        archive = self.archive(QueueTransport([
            response(pdf, content_type="application/pdf"),
            response(b'{\"value\":1}', content_type="application/json"),
        ]))
        pdf_record = archive.fetch(self.config(source_id="pdf-report", url=URL + ".pdf",
                                                allowed_urls=[URL + ".pdf"]))
        self.assertEqual(pdf_record["media_type"], "application/pdf")
        enrolled = archive.enroll("pdf-report", source_version=1)
        self.assertEqual(enrolled["source_sha256"], hashlib.sha256(pdf).hexdigest())

        json_record = archive.fetch(self.config(source_id="json-metadata", media_types=["application/json"]))
        self.assertEqual(json_record["media_type"], "application/json")
        self.assert_code("l2_unsupported_media_type",
                         lambda: archive.enroll("json-metadata", source_version=1))

    def test_tampered_object_is_rejected_for_local_reuse_and_version_read(self):
        archive = self.archive(QueueTransport([response()]))
        record = archive.fetch(self.config())
        (self.root / "objects" / record["source_sha256"]).write_bytes(b"tampered")
        self.assert_code("object_tampered", lambda: archive.fetch(self.config()))
        self.assert_code("object_tampered", lambda: archive.read("issuer-report", source_version=1))

    def test_multi_instance_concurrent_refresh_deduplicates_version_and_records_attempts(self):
        transport = QueueTransport([response(), response()])
        first = self.archive(transport)
        second = self.archive(transport)
        gate = threading.Barrier(2)
        original_get = transport.get

        def synchronized_get(url, **kwargs):
            gate.wait(2)
            return original_get(url, **kwargs)

        transport.get = synchronized_get
        records, errors = [], []

        def run(service):
            try:
                records.append(service.fetch(self.config(), refresh=True))
            except Exception as exc:  # asserted below; preserve thread diagnostics
                errors.append(exc)

        threads = [threading.Thread(target=run, args=(service,)) for service in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertEqual(errors, [])
        self.assertEqual({item["archive_version_id"] for item in records}, {records[0]["archive_version_id"]})
        history = first.history("issuer-report")
        self.assertEqual(len(history["versions"]), 1)
        self.assertEqual(len(history["attempts"]), 2)

    def test_cli_import_local_enroll_and_document_read_are_idempotent(self):
        source = Path(self.temporary.name) / "source.html"
        source.write_bytes(HTML1)
        config_path = Path(self.temporary.name) / "sources.json"
        config_path.write_text(json.dumps({"sources": [self.config()]}), encoding="utf-8")
        digest = hashlib.sha256(HTML1).hexdigest()
        common = ["--root", str(self.root), "--config", str(config_path)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = source_main([*common, "import-local", "issuer-report", str(source),
                                "--expected-sha256", digest, "--expected-size", str(len(HTML1)),
                                "--media-type", "text/html", "--fetched-at", "2026-02-03T04:05:06Z"])
        self.assertEqual(code, 0)
        archived = json.loads(output.getvalue())
        self.assertEqual(archived["source_version"], 1)

        enrolled_outputs = []
        for _ in range(2):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(source_main([*common, "enroll", "issuer-report", "--source-version", "1"]), 0)
            enrolled_outputs.append(json.loads(output.getvalue()))
        self.assertEqual(enrolled_outputs[0]["document_version_id"], enrolled_outputs[1]["document_version_id"])
        self.assertEqual(enrolled_outputs[0]["collected_at"], "2026-02-03T04:05:06Z")
        raw = DocumentService(self.root).read_raw_version(
            company_id="ACME", document_version_id=enrolled_outputs[0]["document_version_id"])
        self.assertEqual(raw, HTML1)

    def test_link_candidates_are_versioned_safe_deduplicated_and_bounded(self):
        archive = self.archive(QueueTransport([]), resolver=lambda host: (_ for _ in ()).throw(
            AssertionError("DNS used")))
        long_label = "L" * 600
        html = f"""<!doctype html><html><head>
        <base href="https://files.example.net/disclosures/">
        <base href="https://ignored.example.net/">
        </head><body>
        <a href="report.pdf#page=1"> {long_label} </a>
        <a href="./report.pdf#page=2">duplicate</a>
        <a href="#table">local fragment</a>
        <a href="http://bad.example/x">http</a>
        <a href="javascript:alert(1)">script</a>
        <a href="//user:secret@bad.example/x">credentials</a>
        <a href="https://bad.example:444/x">port</a>
        <a href="file:///tmp/x">file</a>
        <a href="data:text/plain,x">data</a>
        <link rel="alternate" href="appendix.pdf" title="Appendix PDF">
        <link rel="stylesheet" href="asset.css">
        <script src="hidden.js"></script><img src="image.png">
        <a href="third.pdf">third</a>
        </body></html>""".encode()
        first = self.import_bytes(archive, html)
        second_html = b'<a href="v2-only.pdf">Version two</a>'
        second = self.import_bytes(archive, second_html)
        xhtml = b'<html xmlns="http://www.w3.org/1999/xhtml"><a href="xhtml.pdf">XHTML</a></html>'
        self.import_bytes(
            archive, xhtml, media_type="application/xhtml+xml",
            config=self.config(media_types=["text/html", "application/pdf", "application/xhtml+xml"]),
        )

        result = archive.list_link_candidates("issuer-report", source_version=1, limit=2)
        self.assertEqual(result["source_version"], 1)
        self.assertEqual(result["archive_version_id"], first["archive_version_id"])
        self.assertEqual(result["source_sha256"], hashlib.sha256(html).hexdigest())
        self.assertEqual(result["byte_length"], len(html))
        self.assertEqual(result["final_url"], URL)
        self.assertEqual(result["config_sha256"], first["config_sha256"])
        self.assertEqual(result["candidate_status"], "discovered_not_fetched_or_approved")
        self.assertEqual(result["base_status"], "accepted")
        self.assertEqual(result["rejected_count"], 7)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["candidates"]), 2)
        report, appendix = result["candidates"]
        self.assertEqual(report["resolved_url"], "https://files.example.net/disclosures/report.pdf")
        self.assertEqual(report["raw_href"], "report.pdf#page=1")
        self.assertEqual(report["occurrence_count"], 2)
        self.assertEqual(len(report["label"]), 500)
        self.assertEqual((report["tag"], report["rel"]), ("a", ""))
        self.assertEqual(appendix["resolved_url"], "https://files.example.net/disclosures/appendix.pdf")
        self.assertEqual((appendix["tag"], appendix["label"], appendix["rel"]),
                         ("link", "Appendix PDF", "alternate"))
        self.assertEqual(
            archive.list_link_candidates("issuer-report", source_version=2)["candidates"][0]["resolved_url"],
            "https://example.com/v2-only.pdf")
        self.assertEqual(second["source_version"], 2)
        self.assertEqual(
            archive.list_link_candidates("issuer-report", source_version=3)["candidates"][0]["resolved_url"],
            "https://example.com/xhtml.pdf")
        for invalid in (0, -1, True, None):
            self.assert_code("invalid_arguments", lambda value=invalid: archive.list_link_candidates(
                "issuer-report", source_version=value))
        for invalid in (0, 201, True):
            self.assert_code("invalid_arguments", lambda value=invalid: archive.list_link_candidates(
                "issuer-report", source_version=1, limit=value))

    def test_link_candidates_bound_intermediate_state_and_isolate_bad_urls(self):
        from workbench.link_candidates import _LinkHTMLParser, extract_link_candidates, MAX_LINK_EVENTS
        from workbench.source_archive import _safe_url_syntax
        malformed = b'<a href="https://[bad">bad</a><a href="/ok">ok</a>'
        result = extract_link_candidates(malformed, document_url="https://example.org/report", limit=5,
                                         validate_url=_safe_url_syntax)
        self.assertEqual(result['rejected_count'], 1)
        self.assertEqual(result['candidates'][0]['resolved_url'], 'https://example.org/ok')
        nested = ('<a href="/nested">' * (MAX_LINK_EVENTS + 2) + 'x' * 20000).encode()
        parser = _LinkHTMLParser(); parser.feed(nested.decode())
        self.assertEqual(len(parser.links), MAX_LINK_EVENTS)
        self.assertLessEqual(len(parser._anchors), 1)
        self.assertTrue(all(len(str(item['label'])) <= 500 for item in parser.links))
        result = extract_link_candidates(nested, document_url='https://example.org/report', limit=5,
                                         validate_url=_safe_url_syntax)
        self.assertTrue(result['truncated'])
        self.assertTrue(result['scan_truncated'])
        self.assertEqual(len(result['candidates']), 1)

    def test_link_candidates_reject_bad_first_base_and_do_not_use_later_base(self):
        archive = self.archive(QueueTransport([]))
        html = b'''<base href="http://unsafe.example/docs/"><base href="https://ignored.example/">
                   <a href="attachment.pdf#part">Attachment</a>'''
        self.import_bytes(archive, html)
        result = archive.list_link_candidates("issuer-report", source_version=1)
        self.assertEqual(result["base_status"], "rejected")
        self.assertEqual(result["rejected_count"], 2)
        self.assertEqual(result["candidates"][0]["resolved_url"],
                         "https://example.com/attachment.pdf")

    def test_link_candidates_reject_media_and_tampered_object(self):
        archive = self.archive(QueueTransport([]))
        pdf_config = self.config(media_types=["application/pdf"])
        self.import_bytes(archive, b"%PDF-1.4\n", media_type="application/pdf", config=pdf_config)
        self.assert_code("unsupported_media_type", lambda: archive.list_link_candidates(
            "issuer-report", source_version=1))

        html_archive = self.archive(QueueTransport([]))
        record = self.import_bytes(html_archive, b'<a href="attachment.pdf">Attachment</a>')
        (self.root / "objects" / record["source_sha256"]).write_bytes(b"tampered")
        self.assert_code("object_tampered", lambda: html_archive.list_link_candidates(
            "issuer-report", source_version=2))

    def test_cli_list_links_is_offline_and_does_not_mutate_archive_or_catalog(self):
        archive = self.archive(QueueTransport([]))
        raw = b'<a href="https://unapproved.example/attachment.pdf">Attachment</a>'
        self.import_bytes(archive, raw)
        config_path = Path(self.temporary.name) / "sources-links.json"
        config_path.write_text(json.dumps({"sources": [self.config()]}), encoding="utf-8")
        before = archive.history("issuer-report")
        catalog = self.root / "catalog.sqlite3"
        self.assertFalse(catalog.exists())
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("workbench.source_archive.system_resolver", side_effect=AssertionError("DNS used")), \
                mock.patch("workbench.source_archive.PinnedHTTPSHTTPTransport.get",
                           side_effect=AssertionError("HTTP used")), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = source_main([
                "--root", str(self.root), "--config", str(config_path), "list-links",
                "issuer-report", "--source-version", "1", "--limit", "1",
            ])
        self.assertEqual((code, stderr.getvalue()), (0, ""))
        output = json.loads(stdout.getvalue())
        self.assertEqual(output["candidates"][0]["resolved_url"],
                         "https://unapproved.example/attachment.pdf")
        self.assertEqual(output["candidate_status"], "discovered_not_fetched_or_approved")
        self.assertEqual(archive.history("issuer-report"), before)
        self.assertFalse(catalog.exists())

    def test_cli_export_is_exact_offline_versioned_and_never_overwrites(self):
        json_bytes = b'{"currency":"\xe2\x82\xac","items":[1,2]}\n'
        config_path = Path(self.temporary.name) / "sources.json"
        config_path.write_text(json.dumps({"sources": [self.config(
            media_types=["text/html", "application/json"]
        )]}), encoding="utf-8")
        common = ["--root", str(self.root), "--config", str(config_path)]

        for source, raw, media_type in (
            (Path(self.temporary.name) / "version-1.html", HTML1, "text/html"),
            (Path(self.temporary.name) / "version-2.json", json_bytes, "application/json"),
        ):
            source.write_bytes(raw)
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(source_main([
                    *common, "import-local", "issuer-report", str(source),
                    "--expected-sha256", hashlib.sha256(raw).hexdigest(),
                    "--expected-size", str(len(raw)), "--media-type", media_type,
                ]), 0)

        def export(version, output_path):
            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch("workbench.source_archive.PinnedHTTPSHTTPTransport.get",
                            side_effect=AssertionError("network used")), \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = source_main([
                    *common, "export", "issuer-report", "--source-version", str(version),
                    "--output", str(output_path),
                ])
            return code, stdout.getvalue(), stderr.getvalue()

        latest = Path(self.temporary.name) / "exports" / "latest.json"
        latest.parent.mkdir()
        code, stdout, stderr = export(2, latest)
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(latest.read_bytes(), json_bytes)
        self.assertEqual(json.loads(stdout), {
            "source_id": "issuer-report", "source_version": 2,
            "source_sha256": hashlib.sha256(json_bytes).hexdigest(),
            "byte_length": len(json_bytes), "output_path": str(latest),
        })

        old = latest.parent / "old.html"
        self.assertEqual(export(1, old)[0], 0)
        self.assertEqual(old.read_bytes(), HTML1)

        unknown = latest.parent / "unknown.bin"
        code, stdout, stderr = export(999, unknown)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error"], "unknown_version")
        self.assertFalse(unknown.exists())

        existing = latest.parent / "existing.bin"
        existing.write_bytes(b"keep-existing")
        code, stdout, stderr = export(2, existing)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error"], "output_exists")
        self.assertEqual(existing.read_bytes(), b"keep-existing")

        referent = latest.parent / "referent.bin"
        referent.write_bytes(b"keep-referent")
        link = latest.parent / "linked-output.bin"
        link.symlink_to(referent)
        code, stdout, stderr = export(2, link)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error"], "output_exists")
        self.assertTrue(link.is_symlink())
        self.assertEqual(referent.read_bytes(), b"keep-referent")

        record = SourceArchive(self.root).history("issuer-report")["versions"][1]
        (self.root / "objects" / record["source_sha256"]).write_bytes(b"tampered")
        tampered = latest.parent / "tampered.bin"
        code, stdout, stderr = export(2, tampered)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error"], "object_tampered")
        self.assertFalse(tampered.exists())
        self.assertEqual(list(latest.parent.glob(".*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
