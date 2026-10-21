from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import csv
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from workbench.build import build_database
from workbench.chat import ChatError, ChatService
from workbench.research_results import ResearchResultsService
from tests.support_chat_provider import ExplicitMSFTIntentAdapter


class EvidenceProvider(ExplicitMSFTIntentAdapter):
    def __init__(self):
        self.stream_calls = 0

    def status(self):
        return {"available": True, "provider": "test", "reason": ""}

    def stream(self, messages, tools, cancel_event):
        self.stream_calls += 1
        result = next(item for item in tools if item["name"] == "get_company_facts")["handler"](
            metric="revenue")
        fact = next(item for item in result["facts"] if item.get("source_id"))
        yield {"type": "delta", "text": f'Answer [evidence:{fact["evidence_id"]}]'}


class FailingArtifactService:
    def get(self, conversation_id, message_id):
        raise RuntimeError("internal projection detail")


class UnsafeArtifactService:
    def get(self, conversation_id, message_id):
        return {
            "artifact_id": "=artifact", "answer": "<script>alert(1)</script><a href=bad>",
            "datasets": [{"id": "+dataset", "chart": {"type": "bar"},
                          "rows": [{"id": "001.20", "value": "001.20"}]}],
            "traces": [{"id": "@trace", "steps": [{"details": "<img src=bad>"}]}],
        }


def wait(service, run_id):
    for _ in range(200):
        run = service.get_run(run_id)
        if run["status"] not in {"queued", "running"}:
            return run
        time.sleep(.01)
    raise AssertionError("run did not finish")


class ResearchResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        self.evidence_db = root / "evidence.sqlite3"
        build_database(db_path=self.evidence_db)
        self.provider = EvidenceProvider()
        self.chat = ChatService(self.evidence_db, root / "chat.sqlite3", self.provider)
        self.results = ResearchResultsService(self.chat)

    def tearDown(self):
        self.chat.close()
        self.temp.cleanup()

    def answer(self, company="MSFT"):
        conversation = self.chat.create_conversation(company)
        run = self.chat.submit(conversation["conversation_id"], "What was revenue?")
        self.assertEqual(wait(self.chat, run["run_id"])["status"], "completed")
        message = self.chat.get_conversation(conversation["conversation_id"])["messages"][-1]
        return conversation["conversation_id"], message

    def test_create_revise_and_frozen_roundtrip_exports(self):
        conversation_id, message = self.answer()
        first = self.results.create(conversation_id, message["message_id"], "Draft", "=2+2", ["<b>limit</b>"])
        self.assertEqual(first["question"], "What was revenue?")
        self.assertEqual(first["human_review_status"], "unreviewed")
        self.assertEqual(first["result_type"], "human_draft")
        self.assertEqual(first["publication_status"], "draft")
        self.assertEqual(first["machine_verification"]["status"], "references_checked")
        self.assertEqual(first["artifact"]["answer"], message["content"])
        self.assertTrue(first["artifact"]["datasets"])
        self.assertTrue(first["artifact"]["traces"])
        self.assertIsNone(first["artifact_gap"])
        self.assertEqual(first["artifact_context"], {
            "applies_to": "original_answer", "edits_revalidated": False, "status": "frozen",
        })
        calls_after_answer = self.provider.stream_calls
        second = self.results.revise(
            first["result_id"], conversation_id, "MSFT", 1, "Revised", "new body", [])
        self.assertEqual(second["version"], 2)
        self.assertEqual(second["citations"], first["citations"])
        self.assertEqual(second["source_snapshot"], first["source_snapshot"])
        self.assertEqual(second["artifact"], first["artifact"])
        self.assertFalse(second["artifact_context"]["edits_revalidated"])
        self.assertIn("not semantically reviewed or revalidated", second["warning"])
        self.assertEqual(self.provider.stream_calls, calls_after_answer, "save/export must not call the model")
        self.assertEqual(self.results.get(first["result_id"], conversation_id, "MSFT", 1)["body"], "=2+2")
        self.assertEqual(self.results.list(conversation_id)["results"][0]["version"], 2)

        json_body, _, _ = self.results.export(first["result_id"], conversation_id, "MSFT", 1, "json")
        exported = json.loads(json_body)
        self.assertEqual(exported["body"], "=2+2")
        self.assertEqual(exported["artifact"], first["artifact"])
        html, _, _ = self.results.export(first["result_id"], conversation_id, "MSFT", 1, "html")
        self.assertNotIn("<b>limit</b>", html)
        self.assertIn("&lt;b&gt;limit&lt;/b&gt;", html)
        self.assertIn('<html lang="en">', html)
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', html)
        for text in ("Company", "Version", "Created", "Source question", "Content", "Limitations",
                     "Unreviewed", "Wiki not published", "does not equal semantic support",
                     "Editing and publication warning",
                     "Original answer citations (do not automatically support edited content)"):
            self.assertIn(text, html)
        self.assertIn("white-space:pre-wrap", html)
        self.assertIn("<details><summary>Technical metadata (collapsed by default)</summary>", html)
        self.assertNotIn("<details open", html)
        visible_html, technical_html = html.split("<details>", 1)
        citation = first["citations"][0]
        sources = {item["source_id"]: item for item in first["source_snapshot"]["evidence"]["sources"]}
        evidence = {item["evidence_id"]: item for item in first["source_snapshot"]["evidence"]["evidence"]}
        source_title = sources[evidence[citation["id"]]["source_id"]]["title"]
        self.assertIn(source_title, visible_html)
        self.assertIn(">evidence</span>", visible_html)
        self.assertNotIn(first["result_id"], visible_html)
        self.assertNotIn(citation["id"], visible_html)
        self.assertNotIn('"source_snapshot"', visible_html)
        self.assertIn(first["result_id"], technical_html)
        self.assertIn(first["warning"], technical_html)
        lower_html = html.lower()
        for unsafe in ("<script", "<iframe", " href=", " src="):
            self.assertNotIn(unsafe, lower_html)

        csv_body, _, _ = self.results.export(first["result_id"], conversation_id, "MSFT", 1, "csv")
        self.assertIn("'=2+2", csv_body)
        self.assertIn("原回答数据与图表", html)
        self.assertIn("原回答证据追溯", html)
        rows = list(csv.DictReader(io.StringIO(csv_body)))
        result_metadata = {row["label"]: row["value"] for row in rows if row["record_type"] == "result"}
        self.assertEqual(result_metadata["result_id"], first["result_id"])
        self.assertEqual(result_metadata["version"], "1")
        self.assertEqual(result_metadata["status"], "unreviewed")
        self.assertEqual(result_metadata["publication_status"], "draft")
        dataset_rows = [json.loads(row["value"]) for row in rows
                        if row["record_type"] == "artifact_dataset"]
        self.assertEqual(dataset_rows, [row for dataset in first["artifact"]["datasets"]
                                        for row in (dataset.get("rows") or dataset.get("raw_rows") or [])])
        self.assertTrue(any(row["record_type"] == "artifact_trace" for row in rows))
        self.assertNotIn("new body", html + csv_body + json_body)

    def test_database_rejects_update_and_delete_but_allows_new_versions(self):
        conversation_id, message = self.answer()
        created = self.results.create(conversation_id, message["message_id"], "Draft", "body", [])
        revised = self.results.revise(
            created["result_id"], conversation_id, "MSFT", 1, "Revised", "new body", [])
        self.assertEqual(revised["version"], 2)

        statements = (
            ("UPDATE research_result_versions SET body=? WHERE result_id=? AND version=?",
             ("tampered", created["result_id"], 1)),
            ("DELETE FROM research_result_versions WHERE result_id=? AND version=?",
             (created["result_id"], 1)),
        )
        for sql, parameters in statements:
            with self.subTest(sql=sql), closing(sqlite3.connect(self.chat.chat_db)) as connection:
                with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                    connection.execute(sql, parameters)
                connection.rollback()
        self.assertEqual(
            self.results.get(created["result_id"], conversation_id, "MSFT", 1)["body"], "body")
        self.assertEqual(
            self.results.get(created["result_id"], conversation_id, "MSFT", 2)["body"], "new body")

    def test_cas_allows_only_one_concurrent_revision(self):
        conversation_id, message = self.answer()
        created = self.results.create(conversation_id, message["message_id"], "Draft", "body", [])

        def revise(number):
            other = ResearchResultsService(self.chat)
            try:
                return other.revise(
                    created["result_id"], conversation_id, "MSFT", 1, f"v{number}", "body", [])
            except ChatError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(revise, (1, 2)))
        self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
        conflict = next(item for item in outcomes if isinstance(item, ChatError))
        self.assertEqual((conflict.code, conflict.status_code), ("result_version_conflict", 409))

    def test_get_revise_and_export_require_conjunctive_scope(self):
        conversation_id, message = self.answer()
        created = self.results.create(conversation_id, message["message_id"], "Draft", "body", [])
        other_conversation = self.chat.create_conversation("MSFT")["conversation_id"]
        operations = (
            lambda conversation, company: self.results.get(
                created["result_id"], conversation, company),
            lambda conversation, company: self.results.revise(
                created["result_id"], conversation, company, 1, "bad", "bad", []),
            lambda conversation, company: self.results.export(
                created["result_id"], conversation, company, 1, "json"),
        )
        for operation in operations:
            for conversation, company in ((other_conversation, "MSFT"), (conversation_id, "AAPL")):
                with self.subTest(operation=operation, conversation=conversation, company=company):
                    with self.assertRaises(ChatError) as caught:
                        operation(conversation, company)
                    self.assertEqual((caught.exception.code, caught.exception.status_code),
                                     ("research_result_not_found", 404))
        stored = self.results.get(created["result_id"], conversation_id, "MSFT")
        self.assertEqual((stored["version"], stored["body"]), (1, "body"))

    def test_foreign_or_nonassistant_message_is_rejected_without_leaking_company(self):
        first_id, first_message = self.answer("MSFT")
        second = self.chat.create_conversation("AAPL")
        with self.assertRaises(ChatError) as caught:
            self.results.create(second["conversation_id"], first_message["message_id"], "x", "x", [])
        self.assertEqual(caught.exception.status_code, 404)
        self.assertNotIn("MSFT", caught.exception.message)
        user_message = self.chat.get_conversation(first_id)["messages"][0]
        with self.assertRaises(ChatError) as caught:
            self.results.create(first_id, user_message["message_id"], "x", "x", [])
        self.assertEqual(caught.exception.status_code, 409)

    def test_artifact_failure_saves_text_with_explicit_gap_and_no_approval(self):
        conversation_id, message = self.answer()
        service = ResearchResultsService(self.chat, FailingArtifactService())

        saved = service.create(conversation_id, message["message_id"], "Draft", "Keep this", [])

        self.assertEqual(saved["body"], "Keep this")
        self.assertIsNone(saved["artifact"])
        self.assertEqual(saved["artifact_gap"]["reason"], "projection_failed")
        self.assertEqual(saved["artifact_gap"]["error_code"], "RuntimeError")
        self.assertNotIn("internal projection detail", json.dumps(saved))
        self.assertEqual(saved["human_review_status"], "unreviewed")
        self.assertFalse(saved["artifact_context"]["edits_revalidated"])

    def test_artifact_export_escapes_html_and_preserves_formula_safe_decimal_ids(self):
        conversation_id, message = self.answer()
        service = ResearchResultsService(self.chat, UnsafeArtifactService())
        saved = service.create(conversation_id, message["message_id"], "Draft", "body", [])

        html, _, _ = service.export(saved["result_id"], conversation_id, "MSFT", 1, "html")
        lower_html = html.lower()
        for unsafe in ("<script", "<img", " href=", " src="):
            self.assertNotIn(unsafe, lower_html)
        self.assertIn("&lt;script&gt;", html)

        csv_body, _, _ = service.export(saved["result_id"], conversation_id, "MSFT", 1, "csv")
        rows = list(csv.DictReader(io.StringIO(csv_body)))
        artifact_row = next(row for row in rows if row["record_type"] == "artifact")
        dataset_row = next(row for row in rows if row["record_type"] == "artifact_dataset")
        trace_row = next(row for row in rows if row["record_type"] == "artifact_trace")
        self.assertEqual(artifact_row["value"], "'=artifact")
        self.assertEqual(dataset_row["label"], "'+dataset")
        self.assertEqual(trace_row["label"], "'@trace")
        self.assertEqual(json.loads(dataset_row["value"]), {"id": "001.20", "value": "001.20"})

    def test_additive_migration_marks_old_null_artifact_as_gap_without_backfill(self):
        provider = EvidenceProvider()
        chat = ChatService(self.evidence_db, self.root / "legacy-chat.sqlite3", provider)
        try:
            conversation = chat.create_conversation("MSFT")
            submitted = chat.submit(conversation["conversation_id"], "What was revenue?")
            self.assertEqual(wait(chat, submitted["run_id"])["status"], "completed")
            message = chat.get_conversation(conversation["conversation_id"])["messages"][-1]
            answer = chat.answer_context(conversation["conversation_id"], message["message_id"])
            with closing(sqlite3.connect(chat.chat_db)) as connection, connection:
                connection.executescript("""
                    CREATE TABLE research_result_versions (
                     result_id TEXT NOT NULL, version INTEGER NOT NULL,
                     conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
                     message_id TEXT NOT NULL REFERENCES messages(message_id), company_id TEXT NOT NULL,
                     title TEXT NOT NULL, body TEXT NOT NULL, limitations_json TEXT NOT NULL,
                     question TEXT NOT NULL, citations_json TEXT NOT NULL,
                     source_snapshot_json TEXT NOT NULL, created_at TEXT NOT NULL,
                     PRIMARY KEY(result_id,version));
                """)
                connection.execute(
                    "INSERT INTO research_result_versions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("legacy", 1, answer["conversation_id"], answer["message_id"], "MSFT",
                     "Old", "old body", "[]", answer["question"], json.dumps(answer["citations"]),
                     json.dumps(answer["source_snapshot"]), "2025-01-01T00:00:00Z"),
                )

            migrated = ResearchResultsService(chat)
            old = migrated.get("legacy", conversation["conversation_id"], "MSFT", 1)
            self.assertIsNone(old["artifact"])
            self.assertEqual(old["artifact_gap"]["reason"], "not_captured")
            fresh = migrated.create(
                conversation["conversation_id"], message["message_id"], "New", "new body", [])
            self.assertIsNotNone(fresh["artifact"])
            self.assertIsNone(migrated.get(
                "legacy", conversation["conversation_id"], "MSFT", 1)["artifact"])
        finally:
            chat.close()

    def test_new_run_snapshot_freezes_activity_transformation_details(self):
        conversation_id, message = self.answer()
        run = self.chat.get_run(message["run_id"])
        snapshot = next(event for event in run["events"] if event["type"] == "data_snapshot")

        self.assertNotIn("truncated", snapshot)
        self.assertTrue(snapshot["evidence"]["activities"])
        activity = snapshot["evidence"]["activities"][0]
        self.assertIn("method", activity)
        self.assertIn("parameters", activity)

    def test_old_evidence_uses_digest_checked_archive_after_new_build(self):
        conversation_id, message = self.answer()
        citation = message['citations'][0]
        original = self.chat.read_citation(conversation_id, message['message_id'], citation['kind'], citation['id'])
        with patch('workbench.build.BUILD_VERSION', 'deterministic-financial-evidence/test-next'):
            build_database(db_path=self.evidence_db)
        with closing(sqlite3.connect(self.evidence_db)) as connection, connection:
            connection.execute("UPDATE evidence SET quote='different newer span' WHERE evidence_id=?", (citation['id'],))
        restored = self.chat.read_citation(conversation_id, message['message_id'], citation['kind'], citation['id'])
        self.assertEqual(restored['quote'], original['quote'])
        self.assertEqual(restored['source_sha256'], original['source_sha256'])
        archives = list((self.evidence_db.parent / 'archives').glob('*.sqlite3'))
        self.assertEqual(len(archives), 1)
        archives[0].chmod(0o600)
        with closing(sqlite3.connect(archives[0])) as connection, connection:
            connection.execute("UPDATE metadata SET value='tampered' WHERE key='producer'")
        with self.assertRaises(ChatError):
            self.chat.read_citation(conversation_id, message['message_id'], citation['kind'], citation['id'])

    def test_authoritative_citation_rejects_forgery_swap_and_source_version_change(self):
        conversation_id, message = self.answer()
        citation = message["citations"][0]
        result = self.chat.read_citation(conversation_id, message["message_id"], citation["kind"], citation["id"])
        self.assertEqual(result["company_id"], "MSFT")
        self.assertEqual(len(result["source_sha256"]), 64)
        with self.assertRaises(ChatError) as caught:
            self.chat.read_citation(conversation_id, message["message_id"], "evidence", "forged")
        self.assertEqual(caught.exception.status_code, 404)

        with closing(sqlite3.connect(self.evidence_db)) as connection, connection:
            connection.execute("UPDATE metadata SET value='changed' WHERE key='generated_at'")
        preserved = self.chat.read_citation(conversation_id, message["message_id"], citation["kind"], citation["id"])
        self.assertEqual(preserved["quote"], result["quote"])
        self.assertEqual(preserved["source_sha256"], result["source_sha256"])
        with closing(sqlite3.connect(self.evidence_db)) as connection, connection:
            connection.execute("UPDATE evidence SET quote='tampered' WHERE evidence_id=?", (citation["id"],))
        with self.assertRaises(ChatError) as caught:
            self.chat.read_citation(conversation_id, message["message_id"], citation["kind"], citation["id"])
        self.assertEqual((caught.exception.code, caught.exception.status_code), ("citation_source_mismatch", 409))

        with closing(sqlite3.connect(self.chat.chat_db)) as connection, connection:
            connection.execute("UPDATE messages SET citations_json=? WHERE message_id=?", (
                json.dumps([{"kind": "evidence", "id": "forged", "valid": True}]), message["message_id"]))
        with self.assertRaises(ChatError) as caught:
            self.chat.read_citation(conversation_id, message["message_id"], "evidence", "forged")
        self.assertEqual(caught.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
