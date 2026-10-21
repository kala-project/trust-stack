import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from workbench.documents import DocumentService
from workbench.official_sources import (
    MSFT_INDEX, MSFT_SUBMISSIONS, NetworkResponse, OfficialSourceError, OfficialSources,
)


PUBLIC = ["93.184.216.34"]
REPORT_URL = "https://www.microsoft.com/investor/reports/ar24/index.html"
SEC_URL = "https://www.sec.gov/Archives/edgar/data/789019/000095017024087843/msft-20240630.htm"


class FakeTransport:
    def __init__(self, routes):
        self.routes = {key: list(value) if isinstance(value, list) else [value] for key, value in routes.items()}
        self.calls = []

    def get(self, url, *, timeout, max_bytes, cancel_event=None, deadline=None):
        self.calls.append((url, timeout, max_bytes))
        responses = self.routes.get(url)
        if not responses:
            raise AssertionError(f"unexpected network request: {url}")
        return responses.pop(0) if len(responses) == 1 else responses.pop(0)


def response(body=b"", content_type="text/html", status=200, **headers):
    return NetworkResponse(status, {"content-type": content_type, **headers}, body)


def sec_metadata(*, ticker="MSFT", year=2024):
    return json.dumps({
        "cik": "789019", "tickers": [ticker],
        "filings": {"recent": {
            "form": ["10-K"], "reportDate": [f"{year}-06-30"], "filingDate": [f"{year}-07-30"],
            "accessionNumber": ["0000950170-24-087843"], "primaryDocument": ["msft-20240630.htm"],
        }},
    }).encode()


class OfficialSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.documents = DocumentService(Path(self.temp.name) / "documents")

    def tearDown(self):
        self.temp.cleanup()

    def service(self, routes, **kwargs):
        transport = FakeTransport(routes)
        service = OfficialSources(self.documents, transport=transport, resolver=lambda host: PUBLIC, **kwargs)
        return service, transport

    def discovery_routes(self, microsoft=None, sec=None):
        return {
            MSFT_INDEX: microsoft or response(
                f'<html><a href="{REPORT_URL}">Microsoft 2024 Annual Report</a></html>'.encode()),
            MSFT_SUBMISSIONS: sec or response(sec_metadata(), "application/json"),
        }

    def test_candidates_come_from_fetched_indexes_with_provenance(self):
        service, transport = self.service(self.discovery_routes())
        result = service.discover_official_reports("MSFT", [2024])
        self.assertEqual({item["official_source"] for item in result["candidates"]},
                         {"microsoft_investor", "sec_edgar"})
        self.assertTrue(all(item["discovery_sha256"] for item in result["candidates"]))
        self.assertEqual([call[0] for call in transport.calls], [MSFT_INDEX, MSFT_SUBMISSIONS])
        self.assertIn("not evidence", result["coverage_note"])

    def test_acquisition_archives_exact_bytes_emits_snapshot_addition_and_deduplicates(self):
        events = []
        routes = self.discovery_routes()
        raw = b"<!doctype html><html><body>Capital expenditures</body></html>"
        routes[REPORT_URL] = response(raw)
        service, transport = self.service(routes, snapshot_addition_callback=lambda event: events.append(event) or "ok")
        candidate = next(item for item in service.discover_official_reports("MSFT", [2024])
                         ["candidates"] if item["official_source"] == "microsoft_investor")
        first = service.acquire_official_report(candidate["candidate_id"])
        second = service.acquire_official_report(candidate["candidate_id"])
        self.assertEqual(first["document"]["document_version_id"], second["document"]["document_version_id"])
        self.assertEqual(self.documents.read_raw_version(
            company_id="MSFT", document_version_id=first["document"]["document_version_id"]), raw)
        self.assertEqual(first["provenance"]["publication_date"], None)
        self.assertNotEqual(first["provenance"]["discovered_at"], first["provenance"]["acquired_at"])
        self.assertEqual(events[0]["type"], "data_snapshot_addition")
        self.assertEqual(events[0]["document_version_ids"], [first["document"]["document_version_id"]])
        self.assertEqual(sum(call[0] == REPORT_URL for call in transport.calls), 1)

    def test_no_callback_marks_new_snapshot_required(self):
        routes = self.discovery_routes()
        routes[SEC_URL] = response(b"<html><body>10-K</body></html>")
        service, _ = self.service(routes)
        candidate = next(item for item in service.discover_official_reports("MSFT", [2024])["candidates"]
                         if item["official_source"] == "sec_edgar")
        acquired = service.acquire_official_report(candidate["candidate_id"])
        self.assertTrue(acquired["requires_new_research_snapshot"])
        self.assertEqual(acquired["document"]["source_url"], SEC_URL)

    def test_redirect_target_is_validated_before_second_request(self):
        routes = self.discovery_routes(microsoft=NetworkResponse(302, {"location": "https://127.0.0.1/secret"}, b""),
                                       sec=response(b"{}", "application/json"))
        service, transport = self.service(routes)
        with self.assertRaises(OfficialSourceError) as raised:
            service.discover_official_reports("MSFT", [2024])
        self.assertEqual(raised.exception.code, "missing_retrieval")
        self.assertNotIn("https://127.0.0.1/secret", [call[0] for call in transport.calls])

    def test_private_dns_answer_blocks_request(self):
        transport = FakeTransport({})
        service = OfficialSources(self.documents, transport=transport, resolver=lambda host: ["127.0.0.1"])
        with self.assertRaises(OfficialSourceError) as raised:
            service.discover_official_reports("MSFT", [2024])
        self.assertEqual(raised.exception.code, "missing_retrieval")
        self.assertEqual(transport.calls, [])

    def test_document_size_and_mime_are_bounded(self):
        for payload, mime, code in ((b"x" * 33, "text/html", "response_too_large"),
                                    (b"plain", "text/plain", "unsupported_media_type")):
            with self.subTest(code=code):
                routes = self.discovery_routes()
                routes[REPORT_URL] = response(payload, mime)
                service, _ = self.service(routes, max_document_bytes=32)
                candidate = next(item for item in service.discover_official_reports("MSFT", [2024])["candidates"]
                                 if item["official_source"] == "microsoft_investor")
                with self.assertRaises(OfficialSourceError) as raised:
                    service.acquire_official_report(candidate["candidate_id"])
                self.assertEqual(raised.exception.code, code)

    def test_redirects_share_one_timeout_budget(self):
        class SlowTransport(FakeTransport):
            def get(inner, url, **kwargs):
                time.sleep(0.03)
                return super(SlowTransport, inner).get(url, **kwargs)

        transport = SlowTransport({
            MSFT_INDEX: NetworkResponse(302, {"location": REPORT_URL}, b""),
            REPORT_URL: response(b"<html></html>"),
        })
        service = OfficialSources(self.documents, transport=transport, resolver=lambda host: PUBLIC,
                                  timeout_seconds=0.05)
        started = time.monotonic()
        with self.assertRaises(OfficialSourceError) as raised:
            service.discover_official_reports("MSFT", [2024])
        self.assertEqual(raised.exception.code, "deadline_exceeded")
        self.assertLess(time.monotonic() - started, 0.12)
        self.assertLess(transport.calls[1][1], transport.calls[0][1])

    def test_cancelled_fetch_never_registers_document(self):
        cancel = threading.Event()
        started, release = threading.Event(), threading.Event()

        class BlockingTransport(FakeTransport):
            def get(inner, url, **kwargs):
                if url == REPORT_URL:
                    started.set()
                    release.wait(2)
                return super(BlockingTransport, inner).get(url, **kwargs)

        routes = self.discovery_routes()
        routes[REPORT_URL] = response(b"<html>report</html>")
        transport = BlockingTransport(routes)
        service = OfficialSources(self.documents, transport=transport, resolver=lambda host: PUBLIC,
                                  cancel_event=cancel)
        candidate = next(item for item in service.discover_official_reports("MSFT", [2024])["candidates"]
                         if item["official_source"] == "microsoft_investor")
        errors = []
        thread = threading.Thread(target=lambda: self._capture_acquire(service, candidate, errors))
        thread.start()
        self.assertTrue(started.wait(1))
        cancel.set()
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors[0].code, "cancelled")
        self.assertEqual(self.documents.get_document_coverage(company_id="MSFT")["enrolled"], 0)

    @staticmethod
    def _capture_acquire(service, candidate, errors, results=None):
        try:
            value = service.acquire_official_report(candidate["candidate_id"])
            if results is not None:
                results.append(value)
        except OfficialSourceError as exc:
            errors.append(exc)

    def test_concurrent_same_candidate_fetches_and_registers_once(self):
        started, release = threading.Event(), threading.Event()

        class BlockingTransport(FakeTransport):
            def get(inner, url, **kwargs):
                if url == REPORT_URL:
                    started.set()
                    release.wait(2)
                return super(BlockingTransport, inner).get(url, **kwargs)

        routes = self.discovery_routes()
        routes[REPORT_URL] = response(b"<html>report</html>")
        transport, events = BlockingTransport(routes), []
        service = OfficialSources(self.documents, transport=transport, resolver=lambda host: PUBLIC,
                                  snapshot_addition_callback=lambda event: events.append(event))
        candidate = next(item for item in service.discover_official_reports("MSFT", [2024])["candidates"]
                         if item["official_source"] == "microsoft_investor")
        errors, results = [], []
        threads = [threading.Thread(target=self._capture_acquire,
                                    args=(service, candidate, errors, results)) for _ in range(2)]
        for thread in threads:
            thread.start()
        self.assertTrue(started.wait(1))
        release.set()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertIs(results[0], results[1])
        self.assertEqual(sum(call[0] == REPORT_URL for call in transport.calls), 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(self.documents.get_document_coverage(company_id="MSFT")["enrolled"], 1)

    def test_blocking_dns_wait_is_cancel_bounded(self):
        cancel, entered, release = threading.Event(), threading.Event(), threading.Event()

        def resolver(host):
            entered.set()
            release.wait(2)
            return PUBLIC

        service = OfficialSources(self.documents, transport=FakeTransport({}), resolver=resolver,
                                  cancel_event=cancel, timeout_seconds=1)
        errors = []
        thread = threading.Thread(target=lambda: self._capture_discovery(service, errors))
        thread.start()
        self.assertTrue(entered.wait(1))
        cancel.set()
        thread.join(0.3)
        release.set()
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors[0].code, "cancelled")

    @staticmethod
    def _capture_discovery(service, errors):
        try:
            service.discover_official_reports("MSFT", [2024])
        except OfficialSourceError as exc:
            errors.append(exc)

    def test_future_year_and_unissued_candidate_are_rejected_without_network(self):
        service, transport = self.service({})
        with self.assertRaises(OfficialSourceError) as raised:
            service.discover_official_reports("MSFT", [9999])
        self.assertEqual(raised.exception.code, "invalid_arguments")
        with self.assertRaises(OfficialSourceError) as raised:
            service.acquire_official_report("official-" + "0" * 64)
        self.assertEqual(raised.exception.code, "unknown_candidate")
        self.assertEqual(transport.calls, [])

    def test_wrong_company_sec_metadata_never_becomes_candidate(self):
        routes = self.discovery_routes(sec=response(sec_metadata(ticker="AAPL"), "application/json"))
        service, _ = self.service(routes)
        result = service.discover_official_reports("MSFT", [2024])
        self.assertEqual([item["official_source"] for item in result["candidates"]], ["microsoft_investor"])
        self.assertEqual(result["source_failures"], [{"source": "sec_edgar", "error": "wrong_company",
                                                  "message": "SEC submissions metadata did not identify Microsoft"}])
        with self.assertRaises(OfficialSourceError) as raised:
            service.discover_official_reports("AAPL", [2024])
        self.assertEqual(raised.exception.code, "unsupported_company")

    def test_off_allowlist_link_is_not_a_candidate(self):
        microsoft = response(b'<html><a href="https://example.com/investor/reports/ar24/index.html">Annual report</a></html>')
        service, _ = self.service(self.discovery_routes(microsoft=microsoft))
        result = service.discover_official_reports("MSFT", [2024])
        self.assertEqual([item["official_source"] for item in result["candidates"]], ["sec_edgar"])


if __name__ == "__main__":
    unittest.main()
