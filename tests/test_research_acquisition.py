from contextlib import closing
from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from tests.support_chat_provider import ExplicitMSFTIntentAdapter
from tests.test_report_publications import block
from workbench.build import build_database
from workbench.chat import ChatService
from workbench.documents import DocumentService
from workbench.research_tools import ResearchTools, ToolError
from workbench.scoped_tools import tools_for_scope
from workbench.source_acquisition import AcquisitionError


def wait(service, run_id, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = service.get_run(run_id)
        if run["status"] not in {"queued", "running"}:
            return run
        time.sleep(0.01)
    raise AssertionError("run did not finish")


class ControlledAcquisition:
    """A deterministic acquisition boundary which archives only when invoked."""

    def __init__(self, documents, root, *, record_company="MSFT", failure=None, block=False):
        self.documents = documents
        self.root = Path(root)
        self.record_company = record_company
        self.failure = failure
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        if not block:
            self.release.set()
        self.record = None

    def ensure_annual(self, *, company_id, fiscal_year):
        self.calls.append((company_id, fiscal_year))
        self.entered.set()
        self.release.wait(5)
        if self.failure is not None:
            raise AcquisitionError(self.failure, "controlled acquisition failure")
        if self.record is None:
            source = self.root / f"{self.record_company}-{fiscal_year}.html"
            source.write_text(
                "<!doctype html><html><body>"
                f"<h1>{self.record_company} annual report {fiscal_year}</h1>"
                "<p>ControlledAcquisitionAnchor revenue was 245 million.</p>"
                "</body></html>",
                encoding="utf-8",
            )
            self.record = self.documents.register_file(
                source,
                company_id=self.record_company,
                source_key=f"controlled-annual-{fiscal_year}",
                title=f"Controlled annual report {fiscal_year}",
                url=f"https://example.test/{self.record_company}/{fiscal_year}",
                published_at=f"{fiscal_year}-07-01",
                rights_status="public",
            )
        return {
            "status": "available",
            "document": deepcopy(self.record),
            "acquisition": {"method": "controlled_provider", "receipt_id": "receipt-1"},
        }


class AcquisitionReportProvider(ExplicitMSFTIntentAdapter):
    requires_structured_reports = True

    def __init__(self):
        self.tool_names = []
        self.available = None
        self.passage = None

    def status(self):
        return {"available": True, "provider": "controlled-acquisition"}

    def stream(self, messages, tools, cancel_event):
        handlers = {item["name"]: item["handler"] for item in tools}
        self.tool_names.append(set(handlers))
        self.available = handlers["ensure_public_annual_report"](fiscal_year=2025)
        found = handlers["search_documents"](
            query="ControlledAcquisitionAnchor", document_version_ids=[
                self.available["document"]["document_version_id"]
            ]
        )
        self.passage = handlers["read_passage"](
            passage_id=found["results"][0]["passage_id"]
        )
        reference = {"kind": "passage", "id": self.passage["passage_id"]}
        handlers["publish_research_report"](
            title="Controlled acquired annual report",
            blocks=[block(
                [{"kind": "text", "text": "The controlled annual report is available."}],
                support=[reference],
            )],
        )
        yield {"type": "delta", "text": "Structured report prepared."}


class ToolListProvider(ExplicitMSFTIntentAdapter):
    def __init__(self):
        self.tool_names = []

    def status(self):
        return {"available": True, "provider": "tool-list"}

    def stream(self, messages, tools, cancel_event):
        self.tool_names.append({item["name"] for item in tools})
        yield {"type": "delta", "text": "controlled answer"}


class EnsureOnlyProvider(ExplicitMSFTIntentAdapter):
    def status(self):
        return {"available": True, "provider": "ensure-only"}

    def stream(self, messages, tools, cancel_event):
        handlers = {item["name"]: item["handler"] for item in tools}
        handlers["ensure_public_annual_report"](fiscal_year=2025)
        yield {"type": "delta", "text": "acquired"}


class ResearchAcquisitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.evidence_db = Path(cls.fixture.name) / "evidence.sqlite3"
        build_database(db_path=cls.evidence_db)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.documents = DocumentService(self.root / "documents")

    @staticmethod
    def definition(tools, name):
        return next(item for item in tools.definitions() if item["name"] == name)

    def test_default_hidden_and_successful_run_adds_searchable_authoritative_snapshot(self):
        plain = ResearchTools(self.evidence_db, "MSFT", self.documents)
        self.assertNotIn("ensure_public_annual_report", {item["name"] for item in plain.definitions()})

        acquisition = ControlledAcquisition(self.documents, self.root)
        provider = AcquisitionReportProvider()
        chat = ChatService(
            self.evidence_db, self.root / "chat.sqlite3", provider,
            document_service=self.documents, source_acquisition=acquisition,
        )
        self.addCleanup(chat.close)
        submitted = chat.create_and_submit("MSFT", "Acquire and inspect Microsoft's 2025 annual report")
        run = wait(chat, submitted["run_id"])
        self.assertEqual(run["status"], "completed", run.get("error"))
        version_id = provider.available["document"]["document_version_id"]
        self.assertEqual(acquisition.calls, [("MSFT", 2025)])
        self.assertIn("ensure_public_annual_report", provider.tool_names[0])

        snapshots = [event for event in run["events"] if event["type"] == "data_snapshot"]
        self.assertEqual(len(snapshots), 1)
        self.assertNotIn(version_id, snapshots[0]["documents"]["document_version_ids"])
        effective = chat.effective_snapshot(run["run_id"], "MSFT")
        self.assertIn(version_id, effective["documents"]["document_version_ids"])
        additions = [event for event in run["events"]
                     if event["type"] == "data_snapshot_addition"]
        self.assertEqual(len(additions), 1)
        addition = additions[0]
        self.assertIs(addition["authoritative"], True)
        self.assertEqual((addition["company_id"], addition["source"]["fiscal_year"]), ("MSFT", 2025))
        self.assertEqual(addition["source"]["source_sha256"], acquisition.record["source_sha256"])
        self.assertEqual(addition["document"]["source_sha256"], acquisition.record["source_sha256"])

        message = chat.get_conversation(submitted["conversation_id"])["messages"][-1]
        answer = chat.answer_context(submitted["conversation_id"], message["message_id"])
        self.assertEqual(answer["publication_schema"], "structured-research-report/1")
        self.assertEqual(answer["citations"][0]["kind"], "passage")
        citation = chat.read_citation(
            submitted["conversation_id"], message["message_id"], "passage",
            provider.passage["passage_id"],
        )
        self.assertEqual(citation["document_version_id"], version_id)
        self.assertIn("ControlledAcquisitionAnchor", citation["quote"])

        frozen = deepcopy(answer)
        chat.close()
        reopened = ChatService(
            self.evidence_db, self.root / "chat.sqlite3", ToolListProvider(),
            document_service=self.documents,
        )
        self.addCleanup(reopened.close)
        self.assertEqual(
            reopened.answer_context(submitted["conversation_id"], message["message_id"]), frozen)
        self.assertEqual(
            reopened.read_citation(
                submitted["conversation_id"], message["message_id"], "passage",
                provider.passage["passage_id"],
            )["document_version_id"],
            version_id,
        )

    def test_scoped_dispatch_grants_only_selected_child_and_aggregate(self):
        scoped = tools_for_scope(
            self.evidence_db, "MSFT", ("MSFT", "AAPL"), self.documents)
        acquisition = ControlledAcquisition(
            self.documents, self.root, record_company="AAPL")
        events = []

        def grant(event):
            events.append(deepcopy(event))
            child = scoped.for_company(event["company_id"])
            scoped.add_document_for(event["company_id"], child.prepare_document_addition(event["document"]))

        self.assertTrue(scoped.enable_public_acquisition(acquisition, grant))
        definition = self.definition(scoped, "ensure_public_annual_report")
        self.assertIn("company_id", definition["parameters"]["required"])
        self.assertEqual(set(definition["parameters"]["properties"]["company_id"]["enum"]), {"MSFT", "AAPL"})
        result = definition["handler"](company_id="AAPL", fiscal_year=2025)
        version_id = result["document"]["document_version_id"]
        self.assertEqual(
            definition["handler"](company_id="AAPL", fiscal_year=2025), result)
        self.assertEqual(acquisition.calls, [("AAPL", 2025)])
        self.assertEqual(events[0]["company_id"], "AAPL")
        self.assertIn(version_id, scoped.for_company("AAPL").document_version_ids)
        self.assertNotIn(version_id, scoped.for_company("MSFT").document_version_ids)
        self.assertIn(version_id, scoped.document_version_ids)
        with self.assertRaises(ToolError) as caught:
            definition["handler"](company_id="GOOG", fiscal_year=2025)
        self.assertEqual(caught.exception.code, "subject_out_of_scope")
        self.assertEqual(len(acquisition.calls), 1)

    def test_invalid_years_foreign_record_ungranted_callback_and_failure_are_cached(self):
        acquisition = ControlledAcquisition(self.documents, self.root)
        tools = ResearchTools(self.evidence_db, "MSFT", self.documents)
        self.assertTrue(tools.enable_public_acquisition(acquisition, lambda event: None))
        for invalid in (True, 1989, 2101, "2025"):
            with self.assertRaises(ToolError) as caught:
                tools.ensure_public_annual_report(fiscal_year=invalid)
            self.assertEqual(caught.exception.code, "invalid_fiscal_year")
        self.assertEqual(acquisition.calls, [])
        with self.assertRaises(ToolError) as caught:
            tools.ensure_public_annual_report(fiscal_year=2025)
        self.assertEqual(caught.exception.code, "snapshot_grant_failed")
        with self.assertRaises(ToolError):
            tools.ensure_public_annual_report(fiscal_year=2025)
        self.assertEqual(acquisition.calls, [("MSFT", 2025)])

        foreign = ControlledAcquisition(self.documents, self.root / "foreign", record_company="AAPL")
        (self.root / "foreign").mkdir()
        foreign_tools = ResearchTools(self.evidence_db, "MSFT", self.documents)
        foreign_tools.enable_public_acquisition(foreign, lambda event: self.fail("foreign record was granted"))
        with self.assertRaises(ToolError) as caught:
            foreign_tools.ensure_public_annual_report(fiscal_year=2024)
        self.assertEqual(caught.exception.code, "invalid_snapshot_addition")

        failed = ControlledAcquisition(
            self.documents, self.root, failure="controlled_failure")
        failed_tools = ResearchTools(self.evidence_db, "MSFT", self.documents)
        failed_tools.enable_public_acquisition(failed, lambda event: self.fail("failure was granted"))
        for _ in range(2):
            with self.assertRaises(ToolError) as caught:
                failed_tools.ensure_public_annual_report(fiscal_year=2023)
            self.assertEqual(caught.exception.code, "controlled_failure")
        self.assertEqual(failed.calls, [("MSFT", 2023)])

    def test_cancel_before_grant_and_during_acquisition_never_adds_snapshot_event(self):
        acquisition = ControlledAcquisition(self.documents, self.root, block=True)
        chat = ChatService(
            self.evidence_db, self.root / "cancel.sqlite3", EnsureOnlyProvider(),
            document_service=self.documents, source_acquisition=acquisition,
        )
        self.addCleanup(chat.close)
        submitted = chat.create_and_submit("MSFT", "Acquire the controlled annual report")
        self.assertTrue(acquisition.entered.wait(2), "acquisition did not start")
        chat.cancel(submitted["run_id"])
        acquisition.release.set()
        run = wait(chat, submitted["run_id"])
        self.assertEqual(run["status"], "cancelled", run.get("error"))
        self.assertFalse(any(event["type"] == "data_snapshot_addition" for event in run["events"]))

    def test_execute_run_enables_new_and_refresh_but_not_retain(self):
        acquisition = ControlledAcquisition(self.documents, self.root)
        provider = ToolListProvider()
        chat = ChatService(
            self.evidence_db, self.root / "time.sqlite3", provider,
            document_service=self.documents, source_acquisition=acquisition,
        )
        self.addCleanup(chat.close)
        initial = chat.create_and_submit("MSFT", "Research Microsoft")
        self.assertEqual(wait(chat, initial["run_id"])["status"], "completed")
        self.assertIn("ensure_public_annual_report", provider.tool_names[-1])
        conversation_id = initial["conversation_id"]

        def age_answers():
            with closing(chat._connect()) as connection, connection:
                connection.execute(
                    "UPDATE messages SET created_at='2020-01-01T00:00:00Z' "
                    "WHERE conversation_id=? AND role='assistant'", (conversation_id,))

        intake = {
            "status": "ready",
            "scope": {"subjects": [{"kind": "company", "id": "MSFT"}],
                      "relationship_to_context": "same_company_followup"},
        }
        age_answers()
        with patch("workbench.research_tasks.ResearchTaskService.intake", return_value=intake):
            pending = chat.submit(conversation_id, "Continue with retained data")
        decision = chat.get_run(pending["run_id"])["decision"]
        retained = chat.decisions.answer(
            decision["decision_id"], ["retain"], "", "accepted", "retain-acquisition-tools")
        self.assertEqual(wait(chat, retained["run_id"])["status"], "completed")
        self.assertNotIn("ensure_public_annual_report", provider.tool_names[-1])

        age_answers()
        with patch("workbench.research_tasks.ResearchTaskService.intake", return_value=intake):
            pending = chat.submit(conversation_id, "Refresh the data")
        decision = chat.get_run(pending["run_id"])["decision"]
        refreshed = chat.decisions.answer(
            decision["decision_id"], ["refresh"], "", "accepted", "refresh-acquisition-tools")
        self.assertEqual(wait(chat, refreshed["run_id"])["status"], "completed")
        self.assertIn("ensure_public_annual_report", provider.tool_names[-1])

    def test_grant_rejects_wrong_subject_and_cancelled_run(self):
        provider = ToolListProvider()
        chat = ChatService(
            self.evidence_db, self.root / "grant.sqlite3", provider,
            document_service=self.documents,
        )
        self.addCleanup(chat.close)
        submitted = chat.create_and_submit("MSFT", "Research Microsoft")
        self.assertEqual(wait(chat, submitted["run_id"])["status"], "completed")
        research = chat.research_for({"company_id": "MSFT", "conversation_id": submitted["conversation_id"]})
        retrieval = type("Retrieval", (), {"add_document_version": lambda self, value: None})()
        event = {"company_id": "AAPL", "document": {}, "source": {}, "document_version_ids": []}
        with self.assertRaises(ToolError) as caught:
            chat._grant_snapshot_addition(
                event, research=research, run_id=submitted["run_id"],
                cancel_event=threading.Event(), retrieval=retrieval,
                authorized_company_ids={"MSFT"},
            )
        self.assertEqual(caught.exception.code, "invalid_snapshot_addition")
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(ToolError) as caught:
            chat._grant_snapshot_addition(
                {}, research=research, run_id=submitted["run_id"],
                cancel_event=cancelled, retrieval=retrieval,
                authorized_company_ids={"MSFT"},
            )
        self.assertEqual(caught.exception.code, "cancelled")


if __name__ == "__main__":
    unittest.main()
