from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from workbench.build import build_database
from workbench.chat import ChatService, _now
from workbench.research_tools import ToolError


class CorrectionProvider:
    def status(self):
        return {"available": True, "provider": "explicit-correction-protocol"}

    def stream(self, messages, tools, cancel_event):
        self.descriptions = {tool["name"]: tool["description"] for tool in tools}
        handlers = {tool["name"]: tool["handler"] for tool in tools}
        source = handlers["search_evidence"](query="revenue")["results"][0]
        if "read_report_version" not in handlers:
            yield {"type": "delta", "text": f"协议测试旧结论：收入为10。[evidence:{source['evidence_id']}]"}
            return
        original = handlers["read_report_version"](version=1)
        handlers["record_report_correction"](
            version=1, original_text="收入为10。",
            explanation="旧版测试数值缺少证据支持，不应作为收入结论。",
            impact="撤回该数值；现有证据尚不足以形成可靠的收入结论。",
            evidence_refs=[{"kind": "evidence", "id": source["evidence_id"]}])
        assert "收入为10。" in original["content"]
        yield {"type": "delta", "text": f"撤回旧版测试数值。仅凭本次检索资料尚不能形成可靠收入结论。[evidence:{source['evidence_id']}]"}


class ReportVersionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.evidence = Path(cls.fixture.name) / "evidence.sqlite3"
        build_database(db_path=cls.evidence)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "chat.sqlite3"
        self.chat = ChatService(self.evidence, self.path, CorrectionProvider())
        self.conversation = self.chat.create_conversation("MSFT")["conversation_id"]
        old, _ = self.chat._persist_submission(self.conversation, None, "研究微软")
        self.publish(old["run_id"], "old-message", "旧结论：收入为10。")
        self.run, _ = self.chat._persist_submission(self.conversation, None, "核查旧结论")
        self.refs = {"evidence": {"current-evidence": {}}}

    def tearDown(self):
        self.chat.close()
        self.temp.cleanup()

    def publish(self, run_id, message_id, text):
        with closing(self.chat._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO messages(message_id,conversation_id,role,content,created_at,run_id,status) "
                "VALUES(?,?,?,?,?,?,?)",
                (message_id, self.conversation, "assistant", text, _now(), run_id, "completed"))
        self.chat._finish(run_id, "completed", None)

    def record(self, **overrides):
        arguments = {
            "version": 1, "original_text": "收入为10。",
            "explanation": "旧值没有依据。", "impact": "不能据此判断收入。",
            "evidence_refs": [{"kind": "evidence", "id": "current-evidence"}],
            **overrides,
        }
        return self.chat.report_versions.record(self.run["run_id"], self.refs, **arguments)

    def test_links_publish_only_with_completed_new_report_and_preserve_old_text(self):
        self.record()
        versions = self.chat.get_conversation(self.conversation)["report_versions"]
        self.assertEqual(versions[0]["corrected_by"], [])
        self.publish(self.run["run_id"], "new-message", "更正后的研究报告")
        conversation = self.chat.get_conversation(self.conversation)
        old, new = conversation["report_versions"]
        self.assertEqual(old["corrected_by"][0]["new_message_id"], "new-message")
        self.assertEqual(new["corrections"][0]["old_message_id"], "old-message")
        self.assertEqual(new["corrections"][0]["old_version"], 1)
        self.assertEqual(new["corrections"][0]["new_version"], 2)
        self.assertFalse(new["corrections"][0]["semantic_verified"])
        self.assertEqual(self.chat.report_versions.read(self.conversation, 1)["content"], "旧结论：收入为10。")

    def test_failed_run_cannot_publish_a_correction_link(self):
        self.record()
        self.chat._finish(self.run["run_id"], "failed", {"code": "test_failure"})
        self.assertEqual(self.chat.get_conversation(self.conversation)["report_versions"][0]["corrected_by"], [])
        with self.assertRaises(ToolError):
            self.record()

    def test_idempotence_and_conflicting_rewrite(self):
        first = self.record()
        self.assertEqual(self.record()["correction_id"], first["correction_id"])
        with self.assertRaises(ToolError) as caught:
            self.record(explanation="替换之前的解释")
        self.assertEqual(caught.exception.code, "correction_conflict")

    def test_invalid_versions_spans_and_unobserved_sources_are_rejected(self):
        for version in (0, True, "1", 2):
            with self.assertRaises(ToolError):
                self.record(version=version)
        for quote in ("不在原文中", ""):
            with self.assertRaises(ToolError):
                self.record(original_text=quote)
        for references in ([], [{"kind": "evidence", "id": "another-run"}], [{"id": "current-evidence"}]):
            with self.assertRaises(ToolError):
                self.record(evidence_refs=references)
        other = self.chat.create_conversation("AAPL")["conversation_id"]
        with self.assertRaises(ToolError):
            self.chat.report_versions.read(other, 1)

    def test_ambiguous_quote_requires_a_more_precise_original_span(self):
        with closing(self.chat._connect()) as connection, connection:
            connection.execute("UPDATE messages SET content=? WHERE message_id='old-message'",
                               ("收入为10。后面又说收入为10。",))
        with self.assertRaises(ToolError) as caught:
            self.record()
        self.assertEqual(caught.exception.code, "ambiguous_correction_span")

    def test_correction_records_are_immutable_and_survive_reopening(self):
        self.record()
        with closing(self.chat._connect()) as connection:
            for sql in ("UPDATE report_corrections SET impact='overwrite'", "DELETE FROM report_corrections"):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(sql)
        self.chat.close()
        self.chat = ChatService(self.evidence, self.path, CorrectionProvider())
        self.publish(self.run["run_id"], "new-message", "新版报告")
        self.assertEqual(len(self.chat.get_conversation(self.conversation)["report_versions"][1]["corrections"]), 1)

    def test_real_worker_wires_history_and_correction_tools(self):
        self.chat._launch(self.run["run_id"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            run = self.chat.get_run(self.run["run_id"])
            if run["status"] not in {"queued", "running"}:
                break
            time.sleep(.02)
        self.assertEqual(run["status"], "completed", run.get("error"))
        versions = self.chat.get_conversation(self.conversation)["report_versions"]
        self.assertEqual(len(versions[1]["corrections"]), 1)
        self.assertTrue(any(event["type"] == "tool_result" and event.get("authoritative")
                            and event.get("name") == "record_report_correction" for event in run["events"]))
        context = next(event for event in run["events"] if event["type"] == "research_context")
        self.assertTrue(context["authoritative"])
        self.assertIn(context["execution_date_utc"], self.chat.provider.descriptions["discover_research_catalog"])
        self.assertIn("not a financial data cutoff", self.chat.provider.descriptions["discover_research_catalog"])


if __name__ == "__main__":
    unittest.main()
