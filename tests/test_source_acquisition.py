from datetime import datetime, timedelta, timezone
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from workbench.documents import DocumentService
from workbench.source_acquisition import (
    AcquisitionError, PublicSourceAcquisition, SystemCurlTransport,
)


COMPANIES = {
    "AAPL": {"official_prefixes": ["https://s2.q4cdn.com/470004039/"]},
    "MSFT": {"official_prefixes": ["https://www.microsoft.com/investor/reports/"]},
    "GOOG": {
        "feed_url": "https://abc.xyz/feed/FinancialReport.svc/GetFinancialReportList?LanguageId=1&pageSize=-1&pageNumber=0&year=-1&reportTypes=",
        "official_prefixes": ["https://s206.q4cdn.com/479360582/"],
    },
    "AMZN": {"official_prefixes": ["https://s2.q4cdn.com/299287126/"]},
    "META": {"official_prefixes": ["https://s21.q4cdn.com/399680738/"]},
    "NVDA": {"official_prefixes": ["https://s201.q4cdn.com/141608511/"]},
    "TSLA": {"official_prefixes": ["https://ir.tesla.com/_flysystem/s3/sec/"]},
    "NFLX": {"official_prefixes": ["https://s22.q4cdn.com/959853165/"]},
}
URL = "https://www.microsoft.com/investor/reports/ar24/index.html"


def annual_html(company="Microsoft", year=2024, *, proxy=False):
    heading = "Proxy Statement DEF 14A" if proxy else f"Annual Report {year}"
    text = (
        f"{heading}. {company} Corporation. For the fiscal year ended June 30, {year}. "
        "Consolidated Balance Sheets. Statements of Consolidated Income. "
        "Statements of Cash Flows. Management discussion and audited financial statements. "
    )
    return ("<!doctype html><html><body>" + text * 8 + "</body></html>").encode()


class QueueTransport:
    def __init__(self, responses, *, delay=0):
        self.responses = list(responses)
        self.calls = []
        self.delay = delay
        self.lock = threading.Lock()

    def get(self, url, *, max_bytes):
        with self.lock:
            self.calls.append((url, max_bytes))
            if not self.responses:
                raise AssertionError("unexpected network request")
            response = self.responses.pop(0)
        if self.delay:
            time.sleep(self.delay)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response(url)
        return response


def response(body, url=URL, content_type="text/html", status=200):
    return {
        "body": body,
        "curl_exit": 0 if status == 200 else 22,
        "http_status": status,
        "tls_verify_result": 0,
        "final_url": url,
        "content_type": content_type,
        "size_download": len(body),
    }


class ProcessTransport:
    def __init__(self, log_path, gate):
        self.log_path = str(log_path)
        self.gate = gate

    def _log(self, value):
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(value + "\n")

    def get(self, url, *, max_bytes):
        self.gate.wait(5)
        self._log(f"start {os.getpid()}")
        time.sleep(0.1)
        self._log(f"end {os.getpid()}")
        return response(annual_html(), url=url)


class ProcessSleep:
    def __init__(self, log_path):
        self.log_path = str(log_path)

    def __call__(self, seconds):
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(f"sleep {os.getpid()} {seconds}\n")


def run_process_network(root, config_path, log_path, gate):
    service = PublicSourceAcquisition(
        root, config_path=config_path,
        transport=ProcessTransport(log_path, gate), sleep=ProcessSleep(log_path),
    )
    service._network(URL, max_bytes=1_000_000, purpose="process_test")


class SourceAcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "documents"

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, *, company="MSFT", year=2024, url=URL, title=None, source_class="issuer_hosted"):
        return {
            "company_id": company,
            "fiscal_year": year,
            "title": title or f"{company} {year} Annual Report",
            "url": url,
            "source_id": f"bigtech-{company.casefold()}-fy{year}-annual",
            "media_types": ["application/pdf", "text/html"],
            "rights_status": "review_required",
            "published_at": None,
            "source_class": source_class,
        }

    def config(self, sources, *, companies=None, cooldown=900, feed_ttl=3600,
               host_interval=3):
        catalog = self.base / "catalog.json"
        catalog.write_text(json.dumps({"sources": sources}), encoding="utf-8")
        config = self.base / "acquisition.json"
        config.write_text(json.dumps({
            "version": 1,
            "catalog_path": catalog.name,
            "min_text_chars": 100,
            "feed_cache_seconds": feed_ttl,
            "cooldown_seconds": cooldown,
            "host_interval_seconds": host_interval,
            "companies": companies or COMPANIES,
        }), encoding="utf-8")
        return config

    def service(self, sources, responses=(), **kwargs):
        transport = kwargs.pop("transport", QueueTransport(responses))
        clock = kwargs.pop("clock", None)
        sleep = kwargs.pop("sleep", None)
        return PublicSourceAcquisition(
            self.root, config_path=self.config(sources, **kwargs), transport=transport,
            clock=clock, sleep=sleep,
        ), transport

    def assert_code(self, code, callback):
        with self.assertRaises(AcquisitionError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code, caught.exception.message)

    def test_download_then_local_reuse_never_uses_network(self):
        source = self.source()
        first, transport = self.service([source], [response(annual_html())])
        downloaded = first.ensure_annual(company_id="MSFT", fiscal_year=2024)
        self.assertEqual(downloaded["status"], "available")
        self.assertEqual(downloaded["acquisition"]["discovery_method"], "exact_static_config")
        self.assertEqual(len(transport.calls), 1)

        second = PublicSourceAcquisition(
            self.root, config_path=first.config_path, transport=QueueTransport([])
        )
        reused = second.ensure_annual(company_id="MSFT", fiscal_year=2024)
        self.assertEqual(reused["acquisition"]["method"], "local_archive")
        self.assertEqual(reused["document"]["document_version_id"], downloaded["document"]["document_version_id"])
        self.assertEqual(reused["document"]["company_id"], "MSFT")

    def test_feed_candidate_must_match_exact_official_prefix(self):
        companies = json.loads(json.dumps(COMPANIES))
        feed_url = companies["GOOG"]["feed_url"]
        feed = json.dumps({"reports": [{
            "DocumentTitle": "2026 Annual Report",
            "DocumentUrl": "https://evil.example/alphabet-2026.pdf",
        }]}).encode()
        service, transport = self.service(
            [], [response(feed, url=feed_url, content_type="application/json")], companies=companies
        )
        self.assert_code("period_not_found", lambda: service.ensure_annual(company_id="GOOG", fiscal_year=2026))
        self.assertEqual([call[0] for call in transport.calls], [feed_url])
        self.assertEqual(DocumentService(self.root).get_document_coverage(company_id="GOOG")["enrolled"], 0)

    def test_q4_feed_uses_attachment_year_and_prefers_standalone_10k(self):
        companies = json.loads(json.dumps(COMPANIES))
        feed_url = companies["GOOG"]["feed_url"]
        annual_url = "https://s206.q4cdn.com/479360582/files/2026-wrapper.pdf"
        ten_k_url = "https://s206.q4cdn.com/479360582/files/2026-10-k.html"
        feed = json.dumps({"GetFinancialReportListResult": [{
            "ReportTitle": "2027 Annual Report Group", "ReportYear": 2027,
            "Documents": [
                {"DocumentTitle": "2026 Annual Report", "DocumentPath": annual_url},
                {"DocumentTitle": "2026 Form 10-K", "DocumentPath": ten_k_url},
            ],
        }]}).encode()
        transport = QueueTransport([
            response(feed, url=feed_url, content_type="application/json"),
            response(annual_html(company="Alphabet", year=2026), url=ten_k_url),
        ])
        service, _ = self.service([], transport=transport, companies=companies)
        result = service.ensure_annual(company_id="GOOG", fiscal_year=2026)
        self.assertEqual([call[0] for call in transport.calls], [feed_url, ten_k_url])
        self.assertEqual(result["acquisition"]["discovery_method"], "official_feed")
        self.assertEqual(result["document"]["source_url"], ten_k_url)

    def test_q4_pdf_label_uses_official_tenk_category(self):
        feed_url = COMPANIES["GOOG"]["feed_url"]
        report_url = "https://s206.q4cdn.com/479360582/files/2026-10-k.html"
        feed = json.dumps({"GetFinancialReportListResult": [{
            "ReportTitle": "2026", "ReportYear": 2026,
            "Documents": [{"DocumentTitle": "PDF", "DocumentCategory": "tenk",
                           "DocumentPath": report_url}],
        }]}).encode()
        service, transport = self.service([], [
            response(feed, url=feed_url, content_type="application/json"),
            response(annual_html(company="Alphabet", year=2026), url=report_url),
        ])
        result = service.ensure_annual(company_id="GOOG", fiscal_year=2026)
        self.assertEqual(result["document"]["source_url"], report_url)
        self.assertEqual([call[0] for call in transport.calls], [feed_url, report_url])

    def test_wrong_company_year_and_proxy_are_rejected_before_archive(self):
        cases = [
            (annual_html(company="Apple"), "content_validation_failed"),
            (annual_html(year=2023), "content_validation_failed"),
            (annual_html(proxy=True), "proxy_rejected"),
        ]
        for index, (body, code) in enumerate(cases):
            with self.subTest(code=code, index=index):
                root = self.base / f"case-{index}"
                old_root, self.root = self.root, root
                try:
                    service, _ = self.service([self.source()], [response(body)])
                    self.assert_code(code, lambda: service.ensure_annual(company_id="MSFT", fiscal_year=2024))
                    self.assertEqual(service.archive.history(self.source()["source_id"])["versions"], [])
                finally:
                    self.root = old_root

    def test_429_stops_host_and_persists_cooldown(self):
        service, transport = self.service([self.source()], [response(b"rate limited", status=429)])
        self.assert_code("rate_limited", lambda: service.ensure_annual(company_id="MSFT", fiscal_year=2024))
        self.assert_code("host_cooldown", lambda: service.ensure_annual(company_id="MSFT", fiscal_year=2024))
        self.assertEqual(len(transport.calls), 1)
        receipts = list((self.root / "public-source-acquisition" / "receipts").glob("*.json"))
        self.assertTrue(any(json.loads(path.read_text())["error_code"] == "rate_limited" for path in receipts))

    def test_concurrent_same_year_downloads_and_enrolls_once(self):
        transport = QueueTransport([response(annual_html())], delay=0.1)
        service, _ = self.service([self.source()], transport=transport)
        results, errors = [], []

        def run():
            try:
                results.append(service.ensure_annual(company_id="MSFT", fiscal_year=2024))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(errors, [])
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual({item["document"]["document_version_id"] for item in results},
                         {results[0]["document"]["document_version_id"]})
        self.assertEqual(len(service.archive.history(self.source()["source_id"])["versions"]), 1)

    def test_tampered_local_original_is_rejected_without_redownload(self):
        source = self.source()
        service, _ = self.service([source], [response(annual_html())])
        available = service.ensure_annual(company_id="MSFT", fiscal_year=2024)
        digest = available["document"]["source_sha256"]
        (self.root / "objects" / digest).write_bytes(b"tampered")
        replacement = QueueTransport([response(annual_html())])
        again = PublicSourceAcquisition(self.root, config_path=service.config_path, transport=replacement)
        self.assert_code("object_tampered", lambda: again.ensure_annual(company_id="MSFT", fiscal_year=2024))
        self.assertEqual(replacement.calls, [])

    def test_missing_period_and_existing_document_identity_reuse(self):
        missing, transport = self.service([])
        self.assert_code("period_not_configured", lambda: missing.ensure_annual(company_id="MSFT", fiscal_year=2026))
        self.assertEqual(transport.calls, [])

        source = self.source()
        config_path = self.config([source])
        documents = DocumentService(self.root)
        original = documents.register_bytes(
            annual_html(), company_id="MSFT", source_key=source["source_id"],
            title=source["title"], url=source["url"], media_type="text/html",
            rights_status="review_required",
        )
        service = PublicSourceAcquisition(self.root, config_path=config_path, transport=QueueTransport([]))
        reused = service.ensure_annual(company_id="MSFT", fiscal_year=2024)
        self.assertEqual(reused["acquisition"]["method"], "local_document")
        self.assertEqual(reused["document"]["document_version_id"], original["document_version_id"])

    def test_proxy_reference_is_allowed_but_comparison_year_is_not(self):
        body = annual_html().replace(
            b"Management discussion",
            b"The proxy statement is incorporated by reference. Management discussion",
        )
        service, _ = self.service([self.source()], [response(body)])
        self.assertEqual(service.ensure_annual(company_id="MSFT", fiscal_year=2024)["status"], "available")

        proxy_cover = annual_html().replace(
            b"Annual Report 2024.",
            b"SCHEDULE 14A DEF 14A Proxy Statement. Reference to the 2024 Annual Report.",
        )
        old_root, self.root = self.root, self.base / "proxy-cover-with-reference"
        try:
            proxy_service, _ = self.service([self.source()], [response(proxy_cover)])
            self.assert_code(
                "proxy_rejected",
                lambda: proxy_service.ensure_annual(company_id="MSFT", fiscal_year=2024),
            )
        finally:
            self.root = old_root

        wrong = annual_html(year=2025).replace(
            b"For the fiscal year ended June 30, 2025.",
            b"Year Ended June 30, 2025 2024 2023.",
        )
        old_root, self.root = self.root, self.base / "comparison-year"
        try:
            rejected, _ = self.service([self.source()], [response(wrong)])
            self.assert_code(
                "content_validation_failed",
                lambda: rejected.ensure_annual(company_id="MSFT", fiscal_year=2024),
            )
        finally:
            self.root = old_root

    def test_cover_identity_rejects_competitor_mentions_and_accepts_historical_names(self):
        competitor = annual_html(company="Apple Inc.").replace(
            b"Management discussion", b"Microsoft is a competitor. Management discussion")
        service, _ = self.service([self.source()], [response(competitor)])
        self.assert_code(
            "content_validation_failed",
            lambda: service.ensure_annual(company_id="MSFT", fiscal_year=2024),
        )

        historical = {
            "AAPL": "Apple Computer, Inc.", "MSFT": "Microsoft Corporation",
            "GOOG": "Google Inc.", "AMZN": "Amazon.com, Inc.",
            "META": "Facebook, Inc.", "NVDA": "NVIDIA Corporation",
            "TSLA": "Tesla Motors, Inc.", "NFLX": "Netflix, Inc.",
        }
        validator, _ = self.service([])
        for company, name in historical.items():
            with self.subTest(company=company):
                result = validator._validate_content(
                    annual_html(company=name), "text/html", company, 2024,
                    f"{company} 2024 Annual Report",
                )
                self.assertTrue(result["checks"]["company_identity"])

    def test_discovered_year_persists_in_archive_config_across_instances(self):
        companies = json.loads(json.dumps(COMPANIES))
        feed_url = companies["GOOG"]["feed_url"]
        report_url = "https://s206.q4cdn.com/479360582/files/2026-10-k.html"
        feed = json.dumps({"reports": [{
            "DocumentTitle": "2026 Form 10-K", "DocumentUrl": report_url,
        }]}).encode()
        first_transport = QueueTransport([
            response(feed, url=feed_url, content_type="application/json"),
            response(annual_html(company="Alphabet", year=2026), url=report_url),
        ])
        first, _ = self.service([], transport=first_transport, companies=companies)
        downloaded = first.ensure_annual(company_id="GOOG", fiscal_year=2026)
        version = first.archive.history("bigtech-goog-fy2026-annual")["versions"][-1]
        self.assertEqual(version["config"]["fiscal_year"], 2026)
        self.assertEqual(version["config"]["source_class"], "issuer_hosted")
        self.assertEqual(version["config"]["discovered_from"], feed_url)

        blocked = QueueTransport([])
        second = PublicSourceAcquisition(
            self.root, config_path=first.config_path, transport=blocked,
        )
        reused = second.ensure_annual(company_id="GOOG", fiscal_year=2026)
        self.assertEqual(blocked.calls, [])
        self.assertEqual(reused["document"]["document_version_id"],
                         downloaded["document"]["document_version_id"])

    def test_network_same_company_sha_reuses_other_source_identity(self):
        source = self.source()
        body = annual_html()
        original = DocumentService(self.root).register_bytes(
            body, company_id="MSFT", source_key="older-msft-identity",
            title="Microsoft 2024 Annual Report", url="https://example.invalid/local",
            media_type="text/html", rights_status="review_required",
        )
        service, _ = self.service([source], [response(body)])
        result = service.ensure_annual(company_id="MSFT", fiscal_year=2024)
        coverage = DocumentService(self.root).get_document_coverage(company_id="MSFT")
        self.assertEqual(coverage["enrolled"], 1)
        self.assertEqual(result["document"]["document_version_id"], original["document_version_id"])
        self.assertTrue(result["acquisition"]["identity_reused"])

    def test_unsafe_official_urls_are_rejected_before_transport(self):
        for index, suffix in enumerate(("../escape.html", "%2fescape.html", "%5cescape.html")):
            with self.subTest(suffix=suffix):
                old_root, self.root = self.root, self.base / f"unsafe-{index}"
                try:
                    url = "https://www.microsoft.com/investor/reports/" + suffix
                    transport = QueueTransport([])
                    service, _ = self.service([self.source(url=url)], transport=transport)
                    self.assert_code(
                        "unapproved_source",
                        lambda: service.ensure_annual(company_id="MSFT", fiscal_year=2024),
                    )
                    self.assertEqual(transport.calls, [])
                finally:
                    self.root = old_root

    def test_system_curl_disables_curlrc_before_all_options(self):
        completed = mock.Mock(returncode=7, stdout="{}")
        with mock.patch("workbench.source_acquisition.subprocess.run", return_value=completed) as run:
            SystemCurlTransport().get(URL, max_bytes=100)
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["/usr/bin/curl", "--disable"])

    def test_host_interval_and_independent_cooldowns_use_injected_clock(self):
        class FakeTime:
            def __init__(self):
                self.value = datetime(2026, 1, 1, tzinfo=timezone.utc)
                self.sleeps = []
            def clock(self):
                return self.value
            def sleep(self, seconds):
                self.sleeps.append(seconds)
                self.value += timedelta(seconds=seconds)

        fake = FakeTime()
        transport = QueueTransport([response(annual_html()), response(annual_html())])
        service, _ = self.service([self.source()], transport=transport,
                                  clock=fake.clock, sleep=fake.sleep)
        service._network(URL, max_bytes=1_000_000, purpose="test")
        service._network(URL, max_bytes=1_000_000, purpose="test")
        self.assertEqual(fake.sleeps, [3])
        service._set_cooldown("one.example")
        service._set_cooldown("two.example")
        self.assertTrue(service._host_state("cooldowns", "one.example").is_file())
        self.assertTrue(service._host_state("cooldowns", "two.example").is_file())

    def test_same_host_is_serial_and_spaced_across_processes(self):
        context = multiprocessing.get_context("fork")
        config_path = self.config([self.source()])
        # Initialize shared catalogs before child processes race only on host state.
        PublicSourceAcquisition(
            self.root, config_path=config_path, transport=QueueTransport([]))
        log_path = self.base / "process-network.log"
        gate = context.Event()
        processes = [context.Process(
            target=run_process_network,
            args=(self.root, config_path, log_path, gate),
        ) for _ in range(2)]
        for process in processes:
            process.start()
        gate.set()
        for process in processes:
            process.join(5)
            self.assertEqual(process.exitcode, 0)
        lines = log_path.read_text(encoding="utf-8").splitlines()
        transport_events = [line.split()[0] for line in lines
                            if line.startswith(("start ", "end "))]
        self.assertEqual(transport_events, ["start", "end", "start", "end"])
        sleeps = [float(line.split()[2]) for line in lines if line.startswith("sleep ")]
        self.assertEqual(len(sleeps), 1)
        self.assertGreaterEqual(sleeps[0], 2.8)
        self.assertLessEqual(sleeps[0], 3.0)

    def test_feed_cache_is_serial_across_instances(self):
        companies = json.loads(json.dumps(COMPANIES))
        feed_url = companies["GOOG"]["feed_url"]
        feed = json.dumps({"reports": []}).encode()
        transport = QueueTransport([
            response(feed, url=feed_url, content_type="application/json")
        ], delay=0.05)
        config_path = self.config([], companies=companies)
        services = [PublicSourceAcquisition(
            self.root, config_path=config_path, transport=transport) for _ in range(2)]
        bodies, errors = [], []
        threads = [threading.Thread(
            target=lambda service=service: (
                bodies.append(service._feed_body("GOOG", feed_url))))
            for service in services]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(bodies, [feed, feed])
        self.assertEqual(len(transport.calls), 1)

    def test_time_configuration_is_positive_and_bounded(self):
        for field, value in (("cooldown", 0), ("cooldown", 86_401),
                             ("feed_ttl", -1), ("feed_ttl", 86_401),
                             ("host_interval", 2), ("host_interval", 61)):
            with self.subTest(field=field, value=value):
                kwargs = {field: value}
                self.assert_code(
                    "invalid_config",
                    lambda kwargs=kwargs: self.service([], **kwargs),
                )

    def test_validation_failure_is_hash_verifiable_in_quarantine_and_receipted(self):
        bad = annual_html(company="Apple")
        service, _ = self.service([self.source()], [response(bad)])
        self.assert_code(
            "content_validation_failed",
            lambda: service.ensure_annual(company_id="MSFT", fiscal_year=2024),
        )
        digest = hashlib.sha256(bad).hexdigest()
        quarantined = self.root / "public-source-acquisition" / "quarantine" / f"{digest}.raw"
        self.assertEqual(quarantined.read_bytes(), bad)
        receipts = [json.loads(path.read_text()) for path in
                    (self.root / "public-source-acquisition" / "receipts").glob("*.json")]
        self.assertTrue(any(item.get("quarantine_sha256") == digest for item in receipts))
        self.assertTrue(any(item.get("purpose") == "ensure_annual" and
                            item.get("status") == "failed" for item in receipts))

    def test_parse_failure_never_returns_available(self):
        service, _ = self.service([self.source()], [response(annual_html())])
        failed_record = {"parse_status": "parse_failed", "parse_error": "synthetic"}
        with mock.patch.object(service.archive, "enroll", return_value=failed_record):
            self.assert_code(
                "parse_failed",
                lambda: service.ensure_annual(company_id="MSFT", fiscal_year=2024),
            )
        receipts = [json.loads(path.read_text()) for path in
                    (self.root / "public-source-acquisition" / "receipts").glob("*.json")]
        self.assertTrue(any(item.get("error_code") == "parse_failed" for item in receipts))

    def test_real_2025_originals_validate_read_only(self):
        project = Path(__file__).resolve().parents[1]
        fixtures = [
            ("AAPL", "application/pdf", "downloads-r1/bigtech-aapl-fy2025-annual/original"),
            ("MSFT", "text/html", "downloads-r1/bigtech-msft-fy2025-annual/original"),
            ("NFLX", "application/pdf", "downloads-r1/bigtech-nflx-fy2025-annual/original"),
            ("TSLA", "application/pdf", "downloads-r3/bigtech-tsla-fy2025-official-supplement/original"),
        ]
        base = project / "var" / "source-acquisition-test"
        if not all((base / relative).is_file() for _, _, relative in fixtures):
            self.skipTest("review download originals are not present")
        service, _ = self.service([])
        for company, media, relative in fixtures:
            with self.subTest(company=company):
                raw = (base / relative).read_bytes()
                result = service._validate_content(
                    raw, media, company, 2025, f"{company} FY2025 Annual Report / Form 10-K")
                self.assertEqual(result["main_fiscal_year"], 2025)
        microsoft = (base / fixtures[1][2]).read_bytes()
        self.assert_code(
            "content_validation_failed",
            lambda: service._validate_content(
                microsoft, "text/html", "MSFT", 2024, "Microsoft 2024 Annual Report"),
        )

    def test_public_input_is_only_supported_subject_and_integer_year(self):
        service, _ = self.service([])
        self.assert_code("unsupported_company", lambda: service.ensure_annual(company_id="IBM", fiscal_year=2024))
        self.assert_code("invalid_fiscal_year", lambda: service.ensure_annual(company_id="MSFT", fiscal_year=True))
        self.assert_code("invalid_fiscal_year", lambda: service.ensure_annual(company_id="MSFT", fiscal_year="2024"))


if __name__ == "__main__":
    unittest.main()
