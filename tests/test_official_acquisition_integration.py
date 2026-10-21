from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest

from workbench.answer_artifacts import AnswerArtifactService
from workbench.build import build_database
from workbench.chat import ChatError, ChatService
from workbench.documents import DocumentService
from workbench.official_sources import MSFT_INDEX, MSFT_SUBMISSIONS, NetworkResponse, OfficialSources
from tests.support_chat_provider import ExplicitMSFTIntentAdapter


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/source-catalog.json"
PUBLIC = ["93.184.216.34"]
REPORT_URL = "https://www.microsoft.com/investor/reports/ar24/index.html"
RAW = (b"<!doctype html><html><body><table>"
       b"<tr><th>USD millions</th><th>2024</th></tr>"
       b"<tr><td>Capital expenditures</td><td>$120</td></tr>"
       b"<tr><td>Revenue</td><td>$240</td></tr>"
       b"</table></body></html>")


class Transport:
    def __init__(self):
        metadata = json.dumps({
            "cik": "789019", "tickers": ["MSFT"],
            "filings": {"recent": {"form": [], "reportDate": [], "filingDate": [],
                                      "accessionNumber": [], "primaryDocument": []}},
        }).encode()
        self.routes = {
            MSFT_INDEX: NetworkResponse(200, {"content-type": "text/html"},
                f'<a href="{REPORT_URL}">FY2024 annual report</a>'.encode()),
            MSFT_SUBMISSIONS: NetworkResponse(200, {"content-type": "application/json"}, metadata),
            REPORT_URL: NetworkResponse(200, {"content-type": "text/html"}, RAW),
        }
        self.calls = []

    def get(self, url, *, timeout, max_bytes, cancel_event=None, deadline=None):
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"unexpected request: {url}")
        return self.routes[url]


def wait(service, run_id, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = service.get_run(run_id)
        if run["status"] not in {"queued", "running"}:
            return run
        time.sleep(0.01)
    raise AssertionError("run did not finish")


class BlockingTransport(Transport):
    def __init__(self):
        super().__init__()
        self.report_started = threading.Event()
        self.release_report = threading.Event()

    def get(self, url, *, timeout, max_bytes, cancel_event=None, deadline=None):
        if url == REPORT_URL:
            self.report_started.set()
            self.release_report.wait(3)
        return super().get(url, timeout=timeout, max_bytes=max_bytes,
                           cancel_event=cancel_event, deadline=deadline)


class AcquisitionProvider(ExplicitMSFTIntentAdapter):
    supports_official_acquisition = True

    def __init__(self):
        self.version_id = self.source_sha256 = self.evidence_id = self.calculation_id = None

    def status(self):
        return {"available": True, "reason": "", "provider": "explicit-official-test"}

    def stream(self, messages, tools, cancel_event):
        handlers = {item["name"]: item["handler"] for item in tools}
        candidate = handlers["discover_official_reports"](
            company_id="MSFT", fiscal_years=[2024])["candidates"][0]
        acquired = handlers["acquire_official_report"](candidate_id=candidate["candidate_id"])
        self.version_id = acquired["document"]["document_version_id"]
        self.source_sha256 = acquired["document"]["source_sha256"]
        catalog = handlers["discover_research_catalog"]()
        assert [item["document_version_id"] for item in catalog["documents"]] == [self.version_id]
        common = dict(document_version_id=self.version_id, table_index=0,
                      header_row_indices=[0], row_label_column_index=0,
                      unit="USD millions", period_start="2023-07-01", period_end="2024-06-30",
                      period_kind="year", basis="issuer table presentation",
                      metadata_mode="explicit_interpretation")
        capex = handlers["extract_document_table_fact"](
            **common, row_index=1, column_index=1, expected_value="120",
            metric="capital_expenditures")["fact"]
        revenue = handlers["extract_document_table_fact"](
            **common, row_index=2, column_index=1, expected_value="240",
            metric="revenue_from_document")["fact"]
        self.evidence_id = capex["evidence_id"]
        calculation = handlers["calculate_facts"](
            operation="ratio", fact_ids=[capex["fact_id"], revenue["fact_id"]])
        self.calculation_id = calculation["calculation_id"]
        hit = handlers["search_documents"](
            query="Capital expenditures", document_version_ids=[self.version_id])["results"][0]
        passage = handlers["read_passage"](passage_id=hit["passage_id"])
        yield {"type": "delta", "text": (
            f'Archived annual report [passage:{passage["passage_id"]}] shows capex '
            f'[evidence:{self.evidence_id}]; ratio {calculation["value"]} '
            f'[calculation:{self.calculation_id}]')}


class NoCapabilityProvider(ExplicitMSFTIntentAdapter):
    def status(self):
        return {"available": True, "reason": "", "provider": "no-model-test"}

    def stream(self, messages, tools, cancel_event):
        names = {item["name"] for item in tools}
        assert "discover_official_reports" not in names
        assert "acquire_official_report" not in names
        yield {"type": "delta", "text": "No network tools were enabled."}


class OfficialAcquisitionIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.evidence_db = Path(cls.fixture.name) / "evidence.sqlite3"
        build_database(CATALOG, cls.evidence_db, ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def make_service(self, root, provider, transport=None):
        documents = DocumentService(root / "documents")
        def factory(document_service, **kwargs):
            if transport is None:
                raise AssertionError("official network factory must not be constructed")
            return OfficialSources(document_service, transport=transport,
                                   resolver=lambda host: PUBLIC, **kwargs)
        return ChatService(self.evidence_db, root / "chat.sqlite3", provider,
                           document_service=documents, official_sources_factory=factory), documents

    def test_worker_acquires_adds_extracts_calculates_cites_and_restart_reads_exact_raw(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider, transport = AcquisitionProvider(), Transport()
            service, documents = self.make_service(root, provider, transport)
            conversation = service.create_conversation("MSFT")
            run_id = service.submit(conversation["conversation_id"], "Use the official FY2024 report")["run_id"]
            self.assertEqual(wait(service, run_id)["status"], "completed")
            message = service.get_conversation(conversation["conversation_id"])["messages"][-1]
            context = service.answer_context(conversation["conversation_id"], message["message_id"])
            snapshot = context["source_snapshot"]
            self.assertIn(provider.version_id, snapshot["documents"]["document_version_ids"])
            self.assertEqual([item["type"] for item in snapshot["derivations"]],
                             ["data_snapshot", "data_snapshot_addition"])
            self.assertEqual(service.read_raw_source(conversation["conversation_id"], message["message_id"],
                                                     provider.source_sha256), RAW)
            self.assertEqual(service.read_citation(conversation["conversation_id"], message["message_id"],
                                                   "calculation", provider.calculation_id)["value"], "0.5")
            artifact = AnswerArtifactService(service).get(
                conversation["conversation_id"], message["message_id"])
            self.assertTrue(any(item.get("document_version_id") == provider.version_id
                                for item in artifact["audit"]["frozen_raw_sources"]))
            service.close()
            restarted = ChatService(self.evidence_db, root / "chat.sqlite3", NoCapabilityProvider(),
                                    document_service=documents)
            try:
                self.assertEqual(restarted.read_raw_source(conversation["conversation_id"], message["message_id"],
                                                           provider.source_sha256), RAW)
            finally:
                restarted.close()

    def test_tampered_or_foreign_addition_is_rejected_and_initial_event_stays_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider, transport = AcquisitionProvider(), Transport()
            service, documents = self.make_service(root, provider, transport)
            conversation = service.create_conversation("MSFT")
            run_id = service.submit(conversation["conversation_id"], "Acquire official report")["run_id"]
            self.assertEqual(wait(service, run_id)["status"], "completed")
            message = service.get_conversation(conversation["conversation_id"])["messages"][-1]
            with closing(sqlite3.connect(service.chat_db)) as connection:
                initial_before = connection.execute(
                    "SELECT payload_json FROM events WHERE run_id=? AND type='data_snapshot'", (run_id,)).fetchone()[0]
                row = connection.execute(
                    "SELECT seq,payload_json FROM events WHERE run_id=? AND type='data_snapshot_addition'", (run_id,)).fetchone()
            original = json.loads(row[1])

            for mutation in (lambda value: value.update(company_id="AAPL"),
                             lambda value: value.update(document_version_ids=[])):
                payload = json.loads(row[1]); mutation(payload)
                with closing(sqlite3.connect(service.chat_db)) as connection, connection:
                    connection.execute("UPDATE events SET payload_json=? WHERE run_id=? AND seq=?",
                                       (json.dumps(payload), run_id, row[0]))
                with self.assertRaisesRegex(ChatError, "identity"):
                    service.answer_context(conversation["conversation_id"], message["message_id"])

            with closing(sqlite3.connect(service.chat_db)) as connection, connection:
                connection.execute("UPDATE events SET payload_json=? WHERE run_id=? AND seq=?",
                                   (json.dumps(original), run_id, row[0]))
            (documents.objects_path / provider.source_sha256).write_bytes(b"tampered")
            with self.assertRaisesRegex(ChatError, "archive"):
                service.answer_context(conversation["conversation_id"], message["message_id"])
            with closing(sqlite3.connect(service.chat_db)) as connection:
                initial_after = connection.execute(
                    "SELECT payload_json FROM events WHERE run_id=? AND type='data_snapshot'", (run_id,)).fetchone()[0]
            self.assertEqual(initial_before, initial_after)
            service.close()

    def test_cancellation_after_archive_fetch_denies_snapshot_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider, transport = AcquisitionProvider(), BlockingTransport()
            service, documents = self.make_service(root, provider, transport)
            conversation = service.create_conversation("MSFT")
            run_id = service.submit(conversation["conversation_id"], "Acquire, unless cancelled")["run_id"]
            self.assertTrue(transport.report_started.wait(2))
            service.cancel(run_id)
            transport.release_report.set()
            self.assertEqual(wait(service, run_id)["status"], "cancelled")
            # Cancellation wins before the atomic registration/grant boundary.
            self.assertEqual(documents.get_document_coverage(company_id="MSFT")["enrolled"], 0)
            with closing(sqlite3.connect(service.chat_db)) as connection:
                additions = connection.execute(
                    "SELECT count(*) FROM events WHERE run_id=? AND type='data_snapshot_addition'", (run_id,)).fetchone()[0]
            self.assertEqual(additions, 0)
            service.close()

    def test_historical_run_does_not_borrow_a_future_acquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, documents = self.make_service(root, NoCapabilityProvider())
            conversation = first.create_conversation("MSFT")
            old_run = first.submit(conversation["conversation_id"], "Historical answer")["run_id"]
            self.assertEqual(wait(first, old_run)["status"], "completed")
            old_message = first.get_conversation(conversation["conversation_id"])["messages"][-1]
            first.close()

            provider, transport = AcquisitionProvider(), Transport()
            second, _ = self.make_service(root, provider, transport)
            new_conversation = second.create_conversation("MSFT")
            new_run = second.submit(new_conversation["conversation_id"], "Acquire official report")["run_id"]
            self.assertEqual(wait(second, new_run)["status"], "completed")
            old_snapshot = second.answer_context(
                conversation["conversation_id"], old_message["message_id"])["source_snapshot"]
            self.assertNotIn(provider.version_id, old_snapshot["documents"]["document_version_ids"])
            with self.assertRaisesRegex(ChatError, "not frozen"):
                second.read_raw_source(conversation["conversation_id"], old_message["message_id"],
                                       provider.source_sha256)
            second.close()

    def test_no_capability_provider_never_constructs_official_network_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service, _ = self.make_service(root, NoCapabilityProvider())
            conversation = service.create_conversation("MSFT")
            run_id = service.submit(conversation["conversation_id"], "No internet")["run_id"]
            self.assertEqual(wait(service, run_id)["status"], "completed")
            service.close()


if __name__ == "__main__":
    unittest.main()
