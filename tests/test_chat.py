from contextlib import closing
import json
from concurrent.futures import ThreadPoolExecutor
import os
import re
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from workbench.api_contracts import validate_response
from workbench.build import build_database
from workbench.chat import ChatError, ChatService
from workbench.answer_review import PersistedEvidenceSource
from workbench.evidence_checks import EvidenceCheckError
from workbench.documents import DocumentService
from workbench.worker import DurableWorker
from tests.test_documents import pdf_with_text


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/source-catalog.json"
TERMINAL = {"completed", "failed", "cancelled", "blocked"}


def model_intent(company_id="MSFT", **overrides):
    value = {
        "kind": "company_research",
        "subjects": [{"kind": "company", "name": company_id, "candidate_id": company_id}],
        "directions": ["answer the follow-up"], "metrics": [], "period_text": None,
        "basis": [], "unit": None, "assumptions": [], "uncertainties": [],
        "excluded_entities": [], "is_continuation": True,
        "identity_ambiguity": False, "summary": "preserved test question",
    }
    value.update(overrides)
    return value


class FakeProvider:
    def __init__(self, answer="answer", *, use_tool=False, fail=False, intents=None, intent_failure=None):
        self.answer = answer
        self.use_tool = use_tool
        self.fail = fail
        self.histories = []
        self.intents = list(intents or [])
        self.intent_failure = intent_failure
        self.intent_calls = []

    def status(self):
        return {"available": True, "reason": "", "provider": "fake"}

    def resolve_research_intent(self, prompt, timeout_seconds):
        self.intent_calls.append((prompt, timeout_seconds))
        if self.intent_failure is not None:
            raise self.intent_failure
        current = json.loads(prompt.split("\n", 1)[1]).get("current_company_id") or "MSFT"
        value = self.intents.pop(0) if self.intents else model_intent(current)
        return {"intent": value, "provenance": {
            "prompt": prompt, "prompt_version": "research-intent/1.0",
            "provider": "explicit-fake-model", "model": "fake-intent-v1",
            "result": value, "usage": {"model": "fake-intent-v1", "input_tokens": 7},
        }}

    def stream(self, messages, tools, cancel_event):
        self.histories.append(messages)
        if self.fail:
            raise RuntimeError("secret provider failure")
        answer = self.answer
        if self.use_tool:
            result = next(tool for tool in tools if tool["name"] == "get_company_facts")["handler"](metric="revenue", period="2025-06-30")
            answer = f'Revenue is supported [evidence:{result["facts"][0]["evidence_id"]}]'
        yield {"type": "delta", "text": answer}
        yield {"type": "usage", "tokens": 4}


class StructuredTestProvider:
    """Explicit structured author and independent verifier around a test author."""
    requires_structured_reports = True

    def __init__(self, author=None, *, preserve_references=True):
        self.author = author or FakeProvider()
        self.preserve_references = preserve_references

    def status(self):
        return self.author.status()

    def resolve_research_intent(self, prompt, timeout_seconds):
        return self.author.resolve_research_intent(prompt, timeout_seconds)

    def stream(self, messages, tools, cancel_event):
        text = ""
        for event in self.author.stream(messages, tools, cancel_event):
            if event.get("type") == "delta":
                text += event.get("text", "")
            elif event.get("type") == "final_answer":
                text = event.get("text", "")
            yield event
        if cancel_event.is_set() or not text:
            return
        matches = re.findall(r"\[(evidence|claim|passage):([^\]]+)\]", text)
        self.supports = ([{"kind": kind, "id": identifier} for kind, identifier in matches]
                         if self.preserve_references else [])
        clean_text = re.sub(r"\s*\[(?:evidence|claim|passage):[^\]]+\]", "", text).strip()
        publish = next(tool for tool in tools if tool["name"] == "publish_research_report")
        publish["handler"](title="Controlled candidate", blocks=[{
            "classification": "source_statement" if self.supports else "evidence_gap",
            "parts": [{"kind": "text", "text": clean_text}],
            "support": self.supports, "reasoning": "",
            "limitations": ([] if self.supports else
                            ["This controlled candidate requires independent verification."]),
        }])

    def stream_verification(self, messages, tools, cancel_event):
        packet = json.loads(messages[0]["content"])
        handlers = {tool["name"]: tool["handler"] for tool in tools}
        searched = handlers["search_evidence"](query="controlled candidate verification")
        receipts = [searched["_verification_receipt"]["identifier"]]
        for reference in getattr(self, "supports", []):
            result = handlers["get_candidate_reference"](
                kind=reference["kind"], identifier=reference["id"])
            receipts.append(result["_verification_receipt"]["identifier"])
        supported = bool(getattr(self, "supports", []))
        for node_id in packet["content_nodes"]:
            for check_kind in packet["verification_checks"][node_id]["required"]:
                handlers["record_verification_finding"](
                    version_id=packet["version_id"], node_id=node_id,
                    check_kind=check_kind,
                    outcome="supported" if supported else "insufficient_material_disclosed",
                    explanation="Controlled independent verifier checked the frozen scope.",
                    receipt_ids=receipts)
        handlers["finalize_verification"](
            version_id=packet["version_id"],
            outcome="verified" if supported else "verified_with_limitations",
            limitations=[] if supported else ["The candidate explicitly discloses limited support."],
            semantic_review={"original_question_coverage": "Controlled test assessment."},
            verification={"method": "controlled-independent-test", "semantic_truth_guaranteed": False})
        yield {"type": "usage", "tokens": 1}


class ClaimProvider(FakeProvider):
    def __init__(self, claim_id):
        super().__init__()
        self.claim_id = claim_id

    def stream(self, messages, tools, cancel_event):
        result = next(tool for tool in tools if tool["name"] == "get_claim_evidence")["handler"](
            claim_id=self.claim_id)
        assert result["claim"]["claim_id"] == self.claim_id
        yield {"type": "delta", "text": f"Existing statement [claim:{self.claim_id}]"}


class PassageProvider(FakeProvider):
    def __init__(self, spoof_id=None):
        super().__init__()
        self.spoof_id = spoof_id

    def stream(self, messages, tools, cancel_event):
        if self.spoof_id:
            yield {"type": "tool_result", "name": "search_documents", "result": {"passage_id": self.spoof_id}}
            yield {"type": "delta", "text": f"Spoofed [passage:{self.spoof_id}]"}
            return
        result = next(tool for tool in tools if tool["name"] == "search_documents")["handler"](query="ArbitraryChatToken")
        passage = result["results"][0]
        yield {"type": "delta", "text": f'Document support [passage:{passage["passage_id"]}]'}


class PdfPassageProvider(FakeProvider):
    def stream(self, messages, tools, cancel_event):
        result = next(tool for tool in tools if tool["name"] == "search_documents")["handler"](
            query="PdfChatAnchor")
        passage = result["results"][0]
        self.passage = passage
        yield {"type": "delta", "text": f'PDF page support [passage:{passage["passage_id"]}]'}


class ContextCitationProvider(FakeProvider):
    def stream(self, messages, tools, cancel_event):
        by_name = {item["name"]: item["handler"] for item in tools}
        searched = by_name["search_documents"](query="ContextSearchAnchor", limit=1)
        requested = searched["results"][0]
        read = by_name["read_passage"](
            passage_id=requested["passage_id"], context_chars=500,
            exact_quote="returned $9.4 billion to shareholders",
        )
        assert "$9.4 billion" not in read["quote"]
        candidate = read["exact_citation_selection"]["occurrences"][0]["supporting_passages"][0]
        by_name["read_passage"](passage_id=candidate["passage_id"], context_chars=500)
        yield {"type": "delta", "text": f'Exact support [passage:{candidate["passage_id"]}]'}


class FinalAnswerProvider(FakeProvider):
    def stream(self, messages, tools, cancel_event):
        self.histories.append(messages)
        yield {"type": "delta", "text": "Searching. "}
        yield {"type": "assistant_message", "text": "Searching. ",
               "message_id": "comment-1", "phase": "commentary"}
        yield {"type": "delta", "text": "Checking. "}
        yield {"type": "assistant_message", "text": "Checking. ",
               "message_id": "comment-2", "phase": "commentary"}
        yield {"type": "assistant_message", "text": self.answer,
               "message_id": "final-1", "phase": "final"}
        yield {"type": "final_answer", "text": self.answer}


class BlockingProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()

    def stream(self, messages, tools, cancel_event):
        self.started.set()
        while not cancel_event.wait(0.01):
            pass
        return
        yield  # pragma: no cover


class IgnoringCancellationProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.started = threading.Event()

    def stream(self, messages, tools, cancel_event):
        self.started.set()
        time.sleep(0.15)
        yield {"type": "delta", "text": "late partial"}


class EmptyProvider(FakeProvider):
    def stream(self, messages, tools, cancel_event):
        return
        yield  # pragma: no cover


def start_worker(service, run_id):
    worker = DurableWorker(service, worker_id=f"test-background-{run_id}", heartbeat_seconds=.05)
    thread = threading.Thread(target=worker.run_once, daemon=True)
    thread.start()
    return thread


def wait(service, run_id, timeout=3):
    worker = DurableWorker(service, worker_id=f"test-pump-{run_id}", heartbeat_seconds=.05)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        worker.run_once()
        result = service.get_run(run_id)
        if result["status"] in TERMINAL:
            with closing(service._connect()) as connection:
                pending = connection.execute(
                    "SELECT count(*) FROM jobs WHERE status IN ('queued','retry_wait','running')"
                ).fetchone()[0]
            if not pending:
                return result
        time.sleep(0.01)
    raise AssertionError("run did not finish")


class ChatServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.evidence_db = Path(cls.fixture.name) / "evidence.sqlite3"
        build_database(CATALOG, cls.evidence_db, ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.chat_db = Path(self.temp.name) / "chat.sqlite3"
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.close()
        self.temp.cleanup()

    def service(self, provider=None, document_service=None):
        service = ChatService(
            self.evidence_db, self.chat_db, provider, document_service=document_service,
            job_retry_delays=(0.0, 0.0))
        self.services.append(service)
        return service

    def published_base(self, service, *, created_at=None):
        conversation = service.create_conversation("MSFT")
        conversation_id = conversation["conversation_id"]
        created_at = created_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        run_id, user_id, message_id = "run_base", "msg_base_user", "msg_base_report"
        with closing(service._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO runs(run_id,conversation_id,status,created_at,finished_at) VALUES(?,?,?,?,?)",
                (run_id, conversation_id, "completed", created_at, created_at),
            )
            connection.execute(
                "INSERT INTO messages(message_id,conversation_id,role,content,created_at,run_id,status) "
                "VALUES(?,?,?,?,?,?,?)",
                (user_id, conversation_id, "user", "original", created_at, run_id, "completed"),
            )
            connection.execute(
                "INSERT INTO messages(message_id,conversation_id,role,content,created_at,run_id,status) "
                "VALUES(?,?,?,?,?,?,?)",
                (message_id, conversation_id, "assistant", "frozen report", created_at, run_id, "completed"),
            )
            connection.execute(
                "INSERT INTO report_publications VALUES(?,?,?,?,?)",
                (message_id, run_id, "report-publication/1", "{}", "test-sha"),
            )
        return conversation_id, message_id

    def test_report_continuation_replays_terminal_run_and_conflicts_on_changed_payload(self):
        provider = FakeProvider()
        service = self.service(provider)
        conversation_id, base_message_id = self.published_base(service)
        first = service.submit(
            conversation_id, "follow up", request_id="reader-request-1",
            base_message_id=base_message_id)
        service.cancel(first["run_id"])

        replay = service.submit(
            conversation_id, "follow up", request_id="reader-request-1",
            base_message_id=base_message_id)
        self.assertEqual(replay, {"run_id": first["run_id"], "conversation_id": conversation_id,
                                  "status": "cancelled"})
        self.assertEqual(len(provider.intent_calls), 1)
        with self.assertRaises(ChatError) as caught:
            service.submit(
                conversation_id, "changed", request_id="reader-request-1",
                base_message_id=base_message_id)
        self.assertEqual(caught.exception.code, "idempotency_conflict")

        completed = service.submit(
            conversation_id, "second follow up", request_id="reader-request-2",
            base_message_id=base_message_id)
        with closing(service._connect()) as connection, connection:
            connection.execute(
                "UPDATE runs SET status='completed',finished_at=? WHERE run_id=?",
                (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), completed["run_id"]),
            )
        completed_replay = service.submit(
            conversation_id, "second follow up", request_id="reader-request-2",
            base_message_id=base_message_id)
        self.assertEqual(completed_replay["status"], "completed")
        self.assertEqual(completed_replay["run_id"], completed["run_id"])
        self.assertEqual(len(provider.intent_calls), 2)
        with closing(service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM chat_submission_requests").fetchone()[0], 2)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE role='user'").fetchone()[0], 3)
            frozen = connection.execute(
                "SELECT content FROM messages WHERE message_id=?", (base_message_id,)).fetchone()[0]
            self.assertEqual(frozen, "frozen report")

    def test_report_continuation_stale_base_precedes_intake_and_transaction_rechecks(self):
        provider = FakeProvider()
        service = self.service(provider)
        conversation_id, base_message_id = self.published_base(service)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        with closing(service._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO runs(run_id,conversation_id,status,created_at,finished_at) VALUES(?,?,?,?,?)",
                ("run_newer", conversation_id, "completed", now, now),
            )
            connection.execute(
                "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                ("msg_newer", conversation_id, "assistant", "newer", now, "run_newer",
                 "completed", None, None),
            )
        with self.assertRaises(ChatError) as caught:
            service.submit(conversation_id, "follow up", request_id="stale-1",
                           base_message_id=base_message_id)
        self.assertEqual(caught.exception.code, "report_context_conflict")
        self.assertEqual(provider.intent_calls, [])

        # Remove the competing answer, then add it after the optimistic check but
        # before _persist_submission obtains its writer transaction.
        with closing(service._connect()) as connection, connection:
            connection.execute("DELETE FROM messages WHERE message_id='msg_newer'")
            connection.execute("DELETE FROM runs WHERE run_id='run_newer'")
        resolution = model_intent("MSFT")
        resolution.update({"status": "ready", "scope": {
            **resolution, "subjects": [{"kind": "company", "id": "MSFT", "name": "MSFT"}],
            "relationship_to_context": "same_scope",
        }})

        def race_intake(*_):
            with closing(service._connect()) as connection, connection:
                connection.execute(
                    "INSERT INTO runs(run_id,conversation_id,status,created_at,finished_at) VALUES(?,?,?,?,?)",
                    ("run_race", conversation_id, "completed", now, now),
                )
                connection.execute(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",
                    ("msg_race", conversation_id, "assistant", "racing", now, "run_race",
                     "completed", None, None),
                )
            return resolution

        with mock.patch("workbench.research_tasks.ResearchTaskService.intake", side_effect=race_intake):
            with self.assertRaises(ChatError) as caught:
                service.submit(conversation_id, "follow up", request_id="cas-1",
                               base_message_id=base_message_id)
        self.assertEqual(caught.exception.code, "report_context_conflict")
        with closing(service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM chat_submission_requests WHERE request_id='cas-1'").fetchone()[0], 0)

    def test_report_continuation_cross_instance_has_one_run_and_user_message(self):
        first_provider, second_provider = FakeProvider(), FakeProvider()
        first_service = self.service(first_provider)
        conversation_id, base_message_id = self.published_base(first_service)
        second_service = self.service(second_provider)

        def submit(service):
            return service.submit(conversation_id, "parallel", request_id="shared-reader-key",
                                  base_message_id=base_message_id)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, (first_service, second_service)))
        self.assertEqual(results[0]["run_id"], results[1]["run_id"])
        with closing(first_service._connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone()[0], 2)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE role='user'").fetchone()[0], 2)

    def test_blocked_report_continuation_replay_does_not_duplicate_decision(self):
        provider = FakeProvider()
        service = self.service(provider)
        conversation_id, base_message_id = self.published_base(
            service, created_at="2020-01-01T00:00:00Z")
        first = service.submit(conversation_id, "follow up", request_id="blocked-reader-key",
                               base_message_id=base_message_id)
        self.assertEqual(first["status"], "blocked")
        replay = service.submit(conversation_id, "follow up", request_id="blocked-reader-key",
                                base_message_id=base_message_id)
        self.assertEqual(replay, first)
        self.assertEqual(len(provider.intent_calls), 1)
        with closing(service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM research_decisions WHERE run_id=?", (first["run_id"],)).fetchone()[0], 1)

    def test_conversation_listing_without_task_tables_and_malformed_linkage_errors(self):
        service = self.service()
        conversation = service.create_conversation("MSFT")
        self.assertEqual(
            service.list_conversations()["conversations"][0]["conversation_id"],
            conversation["conversation_id"],
        )
        with closing(service._connect()) as connection, connection:
            connection.execute("CREATE TABLE research_tasks(task_id TEXT PRIMARY KEY)")
            connection.execute("CREATE TABLE research_task_runs(task_id TEXT,conversation_id TEXT)")
        with self.assertRaises(sqlite3.OperationalError):
            service.list_conversations()
        self.assertEqual(
            service.list_conversations(include_internal=True)["conversations"][0]["conversation_id"],
            conversation["conversation_id"],
        )

    def test_pdf_passage_citation_round_trips_page_and_parser_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = DocumentService(root / "documents")
            path = root / "filing.pdf"
            path.write_bytes(pdf_with_text(["Intro page", "PdfChatAnchor source text"]))
            record = documents.register_file(
                path, company_id="MSFT", source_key="pdf", title="PDF filing")
            provider = PdfPassageProvider()
            service = self.service(StructuredTestProvider(provider), documents)
            conversation = service.create_conversation("MSFT")
            run = service.submit(conversation["conversation_id"], "cite the PDF")
            self.assertEqual(wait(service, run["run_id"])["status"], "completed")
            message = service.get_conversation(conversation["conversation_id"])["messages"][-1]
            answer = service.answer_context(conversation["conversation_id"], message["message_id"])
            self.assertEqual(answer["citations"][0]["kind"], "passage")
            self.assertEqual(answer["citations"][0]["document_version_id"], record["document_version_id"])
            detail = service.read_citation(
                conversation["conversation_id"], message["message_id"],
                "passage", answer["citations"][0]["id"])
            validate_response("GET", "/api/chat/citation", 200, detail)
            self.assertEqual(detail["detail_schema_version"], "citation-detail/1")
            self.assertEqual(detail["source_visibility"], "public")
            self.assertEqual(detail["media_type"], "application/pdf")
            self.assertEqual(detail["locator"]["page"], 2)
            self.assertEqual(detail["tables"], [])
            self.assertIn("PdfChatAnchor", detail["context"])

    def test_concurrent_tool_and_stream_events_have_atomic_sequences(self):
        provider = BlockingProvider()
        service = self.service(provider)
        conversation = service.create_conversation("MSFT")
        run = service.submit(conversation["conversation_id"], "concurrent event regression")
        worker_thread = start_worker(service, run["run_id"])
        self.assertTrue(provider.started.wait(2))
        barrier = threading.Barrier(12)

        def emit(worker):
            barrier.wait(timeout=3)
            for index in range(25):
                service._event(
                    run["run_id"], "usage", {"worker": worker, "index": index}, claim=None)

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(emit, range(12)))
        events = service.get_run(run["run_id"])["events"]
        self.assertEqual(sum(event["type"] == "usage" for event in events), 300)
        self.assertEqual([event["seq"] for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(service.cancel(run["run_id"])["status"], "cancelled")
        worker_thread.join(2)

    def test_research_writer_commits_while_a_source_reader_keeps_its_snapshot(self):
        provider = BlockingProvider()
        service = self.service(provider)
        conversation = service.create_conversation("MSFT")
        run = service.submit(conversation["conversation_id"], "reader writer isolation")
        worker_thread = start_worker(service, run["run_id"])
        self.assertTrue(provider.started.wait(2))
        with closing(service._connect()) as reader, ThreadPoolExecutor(max_workers=1) as pool:
            self.assertEqual(reader.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            reader.execute("BEGIN")
            count = reader.execute("SELECT count(*) FROM events WHERE run_id=?", (run["run_id"],)).fetchone()[0]
            pending = pool.submit(
                service._event, run["run_id"], "usage", {"writer_progress": True}, claim=None)
            try:
                pending.result(timeout=2)
                self.assertEqual(reader.execute("SELECT count(*) FROM events WHERE run_id=?",
                                                (run["run_id"],)).fetchone()[0], count)
                self.assertTrue(service.get_run(run["run_id"])["events"][-1]["writer_progress"])
            finally:
                reader.rollback()
        self.assertEqual(service.cancel(run["run_id"])["status"], "cancelled")
        worker_thread.join(2)

    def test_polling_can_read_committed_events_during_an_uncommitted_writer(self):
        provider = BlockingProvider()
        service = self.service(provider)
        conversation = service.create_conversation("MSFT")
        run = service.submit(conversation["conversation_id"], "poll while writing")
        worker_thread = start_worker(service, run["run_id"])
        self.assertTrue(provider.started.wait(2))
        with closing(service._connect()) as writer, ThreadPoolExecutor(max_workers=1) as pool:
            writer.execute("BEGIN IMMEDIATE")
            service._insert_event(
                writer, run["run_id"], "usage", {"not_committed": True}, claim=None)
            pending = pool.submit(service.get_run, run["run_id"])
            try:
                snapshot = pending.result(timeout=2)
                self.assertFalse(any(event.get("not_committed") for event in snapshot["events"]))
            finally:
                writer.rollback()
        self.assertEqual(service.cancel(run["run_id"])["status"], "cancelled")
        worker_thread.join(2)

    def test_existing_delete_journal_database_is_preserved_when_enabling_wal(self):
        service = self.service()
        original = service.create_conversation("MSFT")
        service.close()
        with closing(sqlite3.connect(self.chat_db)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0], "delete")
        reopened = self.service()
        self.assertEqual(reopened.get_conversation(original["conversation_id"])["conversation_id"],
                         original["conversation_id"])
        with closing(reopened._connect()) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_database_aliases_are_rejected_without_modifying_evidence(self):
        before = self.evidence_db.read_bytes()
        with closing(sqlite3.connect(self.evidence_db)) as connection:
            tables_before = connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
            ).fetchall()
        symlink = Path(self.temp.name) / "evidence-symlink.sqlite3"
        hardlink = Path(self.temp.name) / "evidence-hardlink.sqlite3"
        symlink.symlink_to(self.evidence_db)
        os.link(self.evidence_db, hardlink)
        for alias in (self.evidence_db, symlink, hardlink):
            with self.subTest(alias=alias), self.assertRaises(ChatError) as caught:
                ChatService(self.evidence_db, alias)
            self.assertEqual(caught.exception.code, "database_path_collision")
            self.assertEqual(self.evidence_db.read_bytes(), before)
        with closing(sqlite3.connect(self.evidence_db)) as connection:
            tables_after = connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
            ).fetchall()
        self.assertEqual(tables_after, tables_before)

    def test_unavailable_intent_model_fails_but_saves_user_draft_and_snapshot(self):
        service = self.service()
        conversation = service.create_conversation("MSFT")
        with self.assertRaises(ChatError) as caught:
            service.submit(conversation["conversation_id"], "question")
        self.assertEqual((caught.exception.code, caught.exception.status_code),
                         ("intent_model_unavailable", 503))
        saved = service.get_conversation(conversation["conversation_id"])["messages"]
        self.assertEqual([(item["role"], item["content"]) for item in saved], [("user", "question")])
        with closing(service._connect()) as connection:
            run = connection.execute("SELECT run_id,status,error_json FROM runs").fetchone()
        self.assertEqual(run["status"], "failed")
        result = service.get_run(run["run_id"])
        snapshots = [event for event in result["events"] if event["type"] == "intent_snapshot"]
        self.assertEqual(len(snapshots), 1)
        self.assertNotIn("prompt", snapshots[0]["resolution"]["scope"]["intent_metadata"])
        self.assertEqual(service.status()["blocked"]["cause"], "provider_unavailable")

    def test_model_intake_allows_same_company_and_records_resolved_intent_once(self):
        provider = FakeProvider(intents=[model_intent("MSFT", metrics=["收入"])])
        service = self.service(provider)
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "那收入呢？")
        run = wait(service, submitted["run_id"])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"]["code"], "structured_candidate_required")
        self.assertEqual(len(provider.intent_calls), 1)
        snapshot = next(event for event in run["events"] if event["type"] == "intent_snapshot")
        metadata = snapshot["resolution"]["scope"]["intent_metadata"]
        self.assertEqual(metadata["result"]["metrics"], ["收入"])
        self.assertEqual(metadata["model"], "fake-intent-v1")
        self.assertNotIn("prompt", metadata)

    def test_model_intake_rejects_different_company_and_ambiguous_identity_without_run(self):
        ambiguous = model_intent(
            subjects=[
                {"kind": "company", "name": "Microsoft", "candidate_id": "MSFT"},
                {"kind": "company", "name": "Apple", "candidate_id": "AAPL"},
            ],
            kind="ambiguous_identity", identity_ambiguity=True,
            uncertainties=["它可能指两家公司"],
        )
        cases = (
            (model_intent("AAPL"), "scope_mismatch", "创建新研究任务"),
            (ambiguous, "scope_clarification", "身份有歧义"),
        )
        for index, (candidate, code, message) in enumerate(cases):
            service = ChatService(
                self.evidence_db, Path(self.temp.name) / f"branch-{index}.sqlite3",
                FakeProvider(intents=[candidate]),
            )
            self.services.append(service)
            conversation = service.create_conversation("MSFT")
            with self.assertRaises(ChatError) as caught:
                service.submit(conversation["conversation_id"], "follow-up")
            self.assertEqual((caught.exception.code, caught.exception.status_code), (code, 409))
            self.assertIn(message, caught.exception.message)
            self.assertEqual(service.get_conversation(conversation["conversation_id"])["messages"], [])
            with closing(service._connect()) as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    def test_tool_backed_citation_is_checked_and_persisted(self):
        service = self.service(StructuredTestProvider(FakeProvider(use_tool=True)))
        conversation = service.create_conversation("MSFT")
        run = service.submit(conversation["conversation_id"], "What was revenue?")
        self.assertEqual(wait(service, run["run_id"])["status"], "completed")
        assistant = service.get_conversation(conversation["conversation_id"])["messages"][-1]
        self.assertTrue(assistant["citations"][0]["valid"])
        self.assertEqual(assistant["verification"]["verification_outcome"], "verified")
        self.assertEqual(assistant["verification"]["method"], "controlled-independent-test")

    def test_formal_receipts_and_snapshot_do_not_depend_on_display_events(self):
        service = self.service(StructuredTestProvider(FakeProvider(use_tool=True)))
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "What was revenue?")
        run_id = submitted["run_id"]
        self.assertEqual(wait(service, run_id)["status"], "completed")

        before = PersistedEvidenceSource(service, run_id, "MSFT")
        expected_snapshot = service.effective_snapshot(run_id, "MSFT")
        self.assertTrue(before.receipts)
        with closing(service._connect()) as connection, connection:
            connection.execute("DELETE FROM events WHERE run_id=?", (run_id,))
        after = PersistedEvidenceSource(service, run_id, "MSFT")
        self.assertEqual(service.effective_snapshot(run_id, "MSFT"), expected_snapshot)
        self.assertEqual(
            [item.payload_bytes for item in after.receipts],
            [item.payload_bytes for item in before.receipts],
        )

        with closing(service._connect()) as connection, connection:
            connection.execute("DROP TRIGGER author_tool_receipts_no_update")
            connection.execute(
                "UPDATE author_tool_receipts SET result_json='{}' WHERE run_id=?", (run_id,))
        with self.assertRaises(EvidenceCheckError):
            PersistedEvidenceSource(service, run_id, "MSFT")

    def test_runtime_contamination_is_not_a_completed_answer(self):
        service = self.service(FakeProvider('我会先研究。 [tool] You have 8148 weighted tokens left'))
        conversation = service.create_conversation('MSFT')
        result = service.submit(conversation['conversation_id'], 'msft 最近10年CapEX的变化')
        run = wait(service, result['run_id'])
        self.assertEqual(run['status'], 'failed')
        self.assertEqual(run['error']['code'], 'structured_candidate_required')
        messages = service.get_conversation(conversation['conversation_id'])['messages']
        self.assertFalse(any(message['role'] == 'assistant' for message in messages))
        self.assertEqual(messages[-1]['content'], 'msft 最近10年CapEX的变化')

    def test_unstructured_forged_or_bare_text_never_publishes_formal_answer(self):
        for answer in ("Made up [evidence:not-real]", "Revenue increased."):
            db = Path(self.temp.name) / (str(len(answer)) + ".sqlite3")
            service = ChatService(self.evidence_db, db, FakeProvider(answer))
            self.services.append(service)
            conversation = service.create_conversation("MSFT")
            result = wait(service, service.submit(
                conversation["conversation_id"], "question")["run_id"])
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "structured_candidate_required")
            with closing(service._connect()) as connection:
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0], 0)

    def test_claim_citation_actual_producer_matches_closed_contract(self):
        claim_id = self.service().evidence_reader.company("MSFT")["claims"][0]["claim_id"]
        service = self.service(StructuredTestProvider(ClaimProvider(claim_id)))
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "Read an existing research statement")
        self.assertEqual(wait(service, submitted["run_id"])["status"], "completed")
        answer = service.get_conversation(conversation["conversation_id"])["messages"][-1]
        detail = service.read_citation(
            conversation["conversation_id"], answer["message_id"], "claim", claim_id)
        validate_response("GET", "/api/chat/citation", 200, detail)
        self.assertEqual(detail["detail_schema_version"], "citation-detail/1")
        self.assertEqual(detail["source_type"], "existing_research_statement")
        self.assertNotIn("source_visibility", detail)

    def test_passage_citation_is_authoritative_persisted_and_snapshotted(self):
        documents = DocumentService(Path(self.temp.name) / "documents")
        source = Path(self.temp.name) / "chat-source.html"
        source.write_text("<!doctype html><html><body><div>ArbitraryChatToken with supporting context.</div></body></html>")
        registered = documents.register_file(source, company_id="MSFT", source_key="chat", title="Chat source")
        service = self.service(StructuredTestProvider(PassageProvider()), documents)
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "Search the document")
        run = wait(service, submitted["run_id"])
        assistant = service.get_conversation(conversation["conversation_id"])["messages"][-1]
        citation = assistant["citations"][0]
        self.assertTrue(citation["valid"])
        self.assertEqual(citation["company_id"], "MSFT")
        self.assertEqual(citation["document_version_id"], registered["document_version_id"])
        reader = service.read_citation(
            conversation["conversation_id"], assistant["message_id"], "passage", citation["id"])
        self.assertEqual(reader["document_version_id"], registered["document_version_id"])
        self.assertEqual(reader["source_sha256"], registered["source_sha256"])
        self.assertIn("ArbitraryChatToken", reader["context"])
        snapshot = next(event for event in run["events"] if event["type"] == "data_snapshot")
        self.assertEqual(snapshot["documents"]["document_version_ids"], [registered["document_version_id"]])

    def test_exact_context_candidate_is_registered_and_readable_as_citation(self):
        documents = DocumentService(Path(self.temp.name) / "context-documents")
        source = Path(self.temp.name) / "context-source.html"
        source.write_text(
            "<html><body>ContextSearchAnchor " + "x" * 1_220
            + "returned $9.4 billion to shareholders through dividends and repurchases.</body></html>"
        )
        documents.register_file(
            source, company_id="MSFT", source_key="context", title="Context source"
        )
        service = self.service(StructuredTestProvider(ContextCitationProvider()), documents)
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "Find the exact amount")
        self.assertEqual(wait(service, submitted["run_id"])["status"], "completed")
        assistant = service.get_conversation(conversation["conversation_id"])["messages"][-1]
        citation = assistant["citations"][0]
        self.assertTrue(citation["valid"])
        reader = service.read_citation(
            conversation["conversation_id"], assistant["message_id"], "passage", citation["id"]
        )
        self.assertIn("returned $9.4 billion to shareholders", reader["quote"])

    def test_context_passage_locator_projection_preserves_strict_snapshot_checks(self):
        documents = DocumentService(Path(self.temp.name) / "projected-context-documents")
        source = Path(self.temp.name) / "projected-context-source.html"
        source.write_text(
            "<html><body>ContextSearchAnchor " + "x" * 1_220
            + "returned $9.4 billion to shareholders through dividends and repurchases.</body></html>"
        )
        registered = documents.register_file(
            source, company_id="MSFT", source_key="projected-context", title="Projected context source"
        )
        service = self.service(StructuredTestProvider(ContextCitationProvider()), documents)
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "Find the exact amount")
        self.assertEqual(wait(service, submitted["run_id"])["status"], "completed")
        assistant = service.get_conversation(conversation["conversation_id"])["messages"][-1]
        citation = assistant["citations"][0]
        read_passage = documents.read_passage

        def projected(**kwargs):
            item = read_passage(**kwargs)
            item["locator"] = {
                **item["locator"], "page": 69, "page_start": 2_000,
                "page_end": 3_200, "page_basis": "verified PDF page boundary",
            }
            return item

        with mock.patch.object(documents, "read_passage", side_effect=projected):
            reader = service.read_citation(
                conversation["conversation_id"], assistant["message_id"], "passage", citation["id"]
            )
        self.assertEqual(reader["locator"]["page"], 69)

        def changed_offset(**kwargs):
            item = projected(**kwargs)
            item["locator"]["start"] += 1
            return item

        with mock.patch.object(documents, "read_passage", side_effect=changed_offset):
            with self.assertRaises(ChatError) as offset_error:
                service.read_citation(
                    conversation["conversation_id"], assistant["message_id"], "passage", citation["id"]
                )
        self.assertEqual(offset_error.exception.code, "citation_source_mismatch")

        def changed_version(**kwargs):
            item = projected(**kwargs)
            item["document_version_id"] = "0" * 64
            return item

        with mock.patch.object(documents, "read_passage", side_effect=changed_version):
            with self.assertRaises(ChatError) as version_error:
                service.read_citation(
                    conversation["conversation_id"], assistant["message_id"], "passage", citation["id"]
                )
        self.assertEqual(version_error.exception.code, "citation_source_mismatch")
        self.assertNotEqual(registered["document_version_id"], "0" * 64)

    def test_saved_history_survives_current_document_corruption(self):
        documents = DocumentService(Path(self.temp.name) / "documents")
        source = Path(self.temp.name) / "history-source.html"
        source.write_text("<html>ArbitraryChatToken original disclosure</html>")
        registered = documents.register_file(source, company_id="MSFT", source_key="history", title="History")
        service = self.service(StructuredTestProvider(PassageProvider()), documents)
        conversation = service.create_conversation("MSFT")
        run = service.submit(conversation["conversation_id"], "Preserve this answer")
        self.assertEqual(wait(service, run["run_id"])["status"], "completed")
        before = service.get_conversation(conversation["conversation_id"])
        (documents.objects_path / registered["source_sha256"]).write_bytes(b"damaged")
        after = service.get_conversation(conversation["conversation_id"])
        self.assertEqual(before["messages"], after["messages"])
        self.assertEqual(before["research_scope"]["latest_snapshot"], after["research_scope"]["latest_snapshot"])
        self.assertFalse(after["research_scope"]["newest_available"]["available"])
        answer = after["messages"][-1]
        with self.assertRaises(ChatError) as caught:
            service.read_citation(conversation["conversation_id"], answer["message_id"], "passage", answer["citations"][0]["id"])
        self.assertEqual(caught.exception.status_code, 409)

    def test_citation_rejects_changed_derived_quote_even_when_raw_hash_matches(self):
        documents = DocumentService(Path(self.temp.name) / "documents")
        source = Path(self.temp.name) / "quote-source.html"
        source.write_text("<html>ArbitraryChatToken true amount</html>")
        documents.register_file(source, company_id="MSFT", source_key="quote", title="Quote")
        service = self.service(StructuredTestProvider(PassageProvider()), documents)
        conversation = service.create_conversation("MSFT")
        run = service.submit(conversation["conversation_id"], "Read the amount")
        wait(service, run["run_id"])
        answer = service.get_conversation(conversation["conversation_id"])["messages"][-1]
        identifier = answer["citations"][0]["id"]
        with closing(sqlite3.connect(documents.db_path)) as connection, connection:
            connection.execute("UPDATE passages SET quote=? WHERE passage_id=?", ("Wrong replacement amount", identifier))
        with self.assertRaises(ChatError) as caught:
            service.read_citation(conversation["conversation_id"], answer["message_id"], "passage", identifier)
        self.assertEqual(caught.exception.status_code, 409)

    def test_provider_tool_result_cannot_spoof_passage_authority(self):
        fake_id = "a" * 64
        service = self.service(StructuredTestProvider(PassageProvider(spoof_id=fake_id)))
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "accept spoof")
        run = wait(service, submitted["run_id"])
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["error"]["code"], "invalid_report_reference")
        self.assertFalse(any(message["role"] == "assistant" for message in
                             service.get_conversation(conversation["conversation_id"])["messages"]))
        self.assertTrue(any(event["type"] == "provider_tool_result_ignored" for event in run["events"]))

    def test_multiturn_history_only_contains_completed_assistant_answers(self):
        provider = FakeProvider("first")
        service = self.service(StructuredTestProvider(provider))
        conversation = service.create_conversation("MSFT")
        first = service.submit(conversation["conversation_id"], "one")
        wait(service, first["run_id"])
        second = service.submit(conversation["conversation_id"], "two")
        wait(service, second["run_id"])
        history = [item["content"] for item in provider.histories[-1]]
        self.assertEqual((history[0], history[-1]), ("one", "two"))
        self.assertEqual(len(history), 3)
        self.assertIn("first", history[1])

    def test_final_answer_replaces_streamed_commentary_and_is_recorded(self):
        service = self.service(StructuredTestProvider(FinalAnswerProvider("Concise final")))
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "question")
        result = wait(service, submitted["run_id"])
        self.assertEqual(result["status"], "completed")
        assistant = service.get_conversation(conversation["conversation_id"])["messages"][-1]
        self.assertIn("Concise final", assistant["content"])
        self.assertEqual(assistant["verification"]["verification_outcome"],
                         "verified_with_limitations")
        final_events = [event["text"] for event in result["events"]
                        if event["type"] == "final_answer"]
        self.assertEqual(final_events[0], "Concise final")
        self.assertIn("Concise final", final_events[-1])
        self.assertEqual(len(final_events), 2)
        self.assertEqual(
            len([event for event in result["events"] if event["type"] == "assistant_message"]),
            3,
        )

    def test_final_answer_is_subject_to_output_budget(self):
        service = self.service(FinalAnswerProvider("012345678901234567890123456789"))
        service.max_output_chars = 25
        conversation = service.create_conversation("MSFT")
        run = service.submit(conversation["conversation_id"], "question")
        result = wait(service, run["run_id"])
        self.assertEqual(result["error"]["code"], "output_limit")
        self.assertEqual(len(service.get_conversation(conversation["conversation_id"])["messages"]), 1)

    def test_duplicate_submit_across_instances_and_cancellation(self):
        provider = BlockingProvider()
        first = self.service(provider)
        conversation = first.create_conversation("MSFT")
        run = first.submit(conversation["conversation_id"], "one")
        worker_thread = start_worker(first, run["run_id"])
        self.assertTrue(provider.started.wait(1))
        second = self.service(FakeProvider())
        with self.assertRaisesRegex(ChatError, "active run") as caught:
            second.submit(conversation["conversation_id"], "two")
        self.assertEqual(caught.exception.status_code, 409)
        first.cancel(run["run_id"])
        self.assertEqual(wait(first, run["run_id"])["status"], "cancelled")
        worker_thread.join(2)

    def test_provider_exception_is_safe_and_not_an_assistant_answer(self):
        service = self.service(FakeProvider(fail=True))
        conversation = service.create_conversation("MSFT")
        run = service.submit(conversation["conversation_id"], "question")
        result = wait(service, run["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "provider_error")
        self.assertNotIn("secret", result["error"]["message"])
        self.assertEqual([m["role"] for m in service.get_conversation(conversation["conversation_id"])["messages"]], ["user"])

    def test_output_budget_fails_without_persisting_partial_answer(self):
        service = self.service(FakeProvider("0123456789"))
        service.max_output_chars = 5
        conversation = service.create_conversation("MSFT")
        run = service.submit(conversation["conversation_id"], "question")
        result = wait(service, run["run_id"])
        self.assertEqual(result["error"]["code"], "output_limit")
        self.assertEqual(len(service.get_conversation(conversation["conversation_id"])["messages"]), 1)

    def test_empty_response_fails_without_persisting_assistant_message(self):
        service = self.service(EmptyProvider())
        conversation = service.create_conversation("MSFT")
        result = wait(service, service.submit(conversation["conversation_id"], "question")["run_id"])
        self.assertEqual(result["error"]["code"], "structured_candidate_required")
        roles = [item["role"] for item in service.get_conversation(conversation["conversation_id"])["messages"]]
        self.assertEqual(roles, ["user"])

    def test_cancel_is_immediately_terminal_when_provider_ignores_event(self):
        provider = IgnoringCancellationProvider()
        service = self.service(provider)
        conversation = service.create_conversation("MSFT")
        run = service.submit(conversation["conversation_id"], "question")
        worker_thread = start_worker(service, run["run_id"])
        self.assertTrue(provider.started.wait(1))
        self.assertEqual(service.cancel(run["run_id"])["status"], "cancelled")
        self.assertEqual(service.get_run(run["run_id"])["status"], "cancelled")
        worker_thread.join(2)
        roles = [item["role"] for item in service.get_conversation(conversation["conversation_id"])["messages"]]
        self.assertEqual(roles, ["user"])

    def test_worker_reclaims_dead_attempt_and_reaches_safe_terminal_state(self):
        service = self.service(FakeProvider())
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "recover queued work")
        now = [10_000_000_000.0]
        service.jobs._clock = lambda: now[0]
        stale = service.jobs.claim("dead-worker", worker_pid=99999999)
        self.assertEqual(stale.run_id, submitted["run_id"])
        now[0] += service.jobs.lease_seconds + 1
        worker = DurableWorker(service, worker_id="replacement-worker", heartbeat_seconds=.05)
        self.assertTrue(worker.run_once())
        result = service.get_run(submitted["run_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "structured_candidate_required")
        with closing(service._connect()) as connection:
            attempts = connection.execute(
                "SELECT status FROM job_attempts WHERE job_id=? ORDER BY attempt_number",
                (stale.job_id,)).fetchall()
        self.assertEqual([row["status"] for row in attempts], ["stale", "failed"])

    def test_new_service_does_not_start_or_recover_a_live_same_process_run(self):
        service = self.service(FakeProvider())
        conversation = service.create_conversation("MSFT")
        with closing(sqlite3.connect(self.chat_db)) as connection, connection:
            connection.execute(
                "INSERT INTO runs(run_id,conversation_id,status,created_at,owner_pid) VALUES(?,?,?,?,?)",
                ("run_live", conversation["conversation_id"], "running", "2020-01-01T00:00:00Z", os.getpid()),
            )
        restarted = self.service(FakeProvider())
        self.assertEqual(restarted.get_run("run_live")["status"], "running")


if __name__ == "__main__":
    unittest.main()
