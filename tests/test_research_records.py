from __future__ import annotations

from dataclasses import replace
import sqlite3

import unittest
import tempfile
from pathlib import Path
from contextlib import closing

from workbench.chat import ChatError, ChatService
from workbench.research_records import (
    AuthorToolReceipt,
    FrozenArray,
    FrozenObject,
    ResearchRecordError,
    ResearchRecordStore,
    ResearchSnapshot,
)


NOW = "2026-10-03T11:27:11Z"
_BASE_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE conversations(
 conversation_id TEXT PRIMARY KEY
);
CREATE TABLE runs(
 run_id TEXT PRIMARY KEY,
 conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
 status TEXT NOT NULL
);
CREATE TABLE events(
 run_id TEXT NOT NULL REFERENCES runs(run_id),
 seq INTEGER NOT NULL,
 type TEXT NOT NULL,
 payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 PRIMARY KEY(run_id, seq)
);
"""


def _connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


class ResearchRecordTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = str(Path(self.temp.name) / "records.sqlite3")
        with closing(_connect(path)) as connection, connection:
            connection.executescript(_BASE_SCHEMA)
            connection.execute("INSERT INTO conversations VALUES('conversation-1')")
            connection.execute("INSERT INTO conversations VALUES('conversation-2')")
            connection.execute("INSERT INTO runs VALUES('run-1','conversation-1','running')")
        store = ResearchRecordStore(lambda: _connect(path))
        with closing(_connect(path)) as connection, connection:
            store.begin_run(connection, "run-1", "conversation-1", ("company-b", "company-a"))
        self.records = path, store

    def test_records_persist_as_typed_immutable_values(self):
        path, store = self.records
        with closing(_connect(path)) as connection, connection:
            receipt = store.append_receipt(
                connection,
                run_id="run-1",
                call_id="call-1",
                tool_name="lookup",
                arguments={"query": "cash", "filters": [1, True]},
                result={"facts": [{"value": 7.5}]},
                created_at=NOW,
            )
            snapshot = store.append_snapshot(
                connection,
                run_id="run-1",
                snapshot={"companies": ["company-a", "company-b"], "complete": True},
                created_at=NOW,
            )

        assert receipt.arguments_object() == {"filters": [1, True], "query": "cash"}
        assert snapshot.snapshot_object()["complete"] is True
        assert store.read_receipts("run-1") == (receipt,)
        assert store.read_snapshot("run-1") == snapshot
        store.assert_scope("run-1", "conversation-1", ("company-a", "company-b"))

        with closing(_connect(path)) as connection, connection, self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE author_tool_receipts SET tool_name='changed' WHERE run_id='run-1'")


    def test_frozen_containers_reject_pseudo_json_and_duplicate_or_unsorted_keys(self):
        with self.assertRaisesRegex(ResearchRecordError, "non-JSON"):
            FrozenArray((("tuple", "is-not-json"),))
        with self.assertRaisesRegex(ResearchRecordError, "unique and canonically ordered"):
            FrozenObject((("same", 1), ("same", 2)))
        with self.assertRaisesRegex(ResearchRecordError, "unique and canonically ordered"):
            FrozenObject((("z", 1), ("a", 2)))
        with self.assertRaisesRegex(ResearchRecordError, "must be a tuple"):
            FrozenArray([1])


    def test_record_dataclasses_reject_wrong_types_hash_version_revision_and_time(self):
        path, store = self.records
        with closing(_connect(path)) as connection, connection:
            receipt = store.append_receipt(
                connection, run_id="run-1", call_id="call-1", tool_name="lookup",
                arguments={}, result={"ok": True}, created_at=NOW,
            )
            snapshot = store.append_snapshot(
                connection, run_id="run-1", snapshot={"ok": True}, created_at=NOW)

        invalid = [
            lambda: replace(receipt, revision=True),
            lambda: replace(receipt, arguments=((),)),
            lambda: replace(receipt, receipt_sha256="0" * 64),
            lambda: replace(receipt, record_version="author-tool-receipt/999"),
            lambda: replace(receipt, created_at="2026-10-03 11:27:11"),
            lambda: replace(snapshot, snapshot_sha256="f" * 64),
            lambda: replace(snapshot, snapshot=((),)),
            lambda: replace(snapshot, record_version="research-snapshot/999"),
        ]
        for construct in invalid:
            with self.assertRaises(ResearchRecordError):
                construct()

        with self.assertRaises(ResearchRecordError):
            AuthorToolReceipt(
                "run-1", 1, "call-2", "lookup", FrozenObject(()), FrozenObject(()),
                "not-a-hash", NOW,
            )
        with self.assertRaises(ResearchRecordError):
            ResearchSnapshot("run-1", 0, FrozenObject(()), "0" * 64, NOW)


    def test_begin_run_requires_tuple_and_rejects_duplicate_scope(self):
        path, store = self.records
        with closing(_connect(path)) as connection, connection:
            connection.execute("INSERT INTO runs VALUES('run-2','conversation-1','queued')")
            with self.assertRaisesRegex(ResearchRecordError, "tuple"):
                store.begin_run(connection, "run-2", "conversation-1", "company-a")
            with self.assertRaisesRegex(ResearchRecordError, "distinct"):
                store.begin_run(
                    connection, "run-2", "conversation-1", ("company-a", "company-a"))

        with self.assertRaisesRegex(ResearchRecordError, "tuple"):
            store.assert_scope("run-1", "conversation-1", "company-a")
        with self.assertRaisesRegex(ResearchRecordError, "differs"):
            store.assert_scope("run-1", "conversation-1", ("company-a",))


    def test_snapshot_append_checks_run_owner_and_active_state(self):
        path, store = self.records
        with closing(_connect(path)) as connection, connection:
            connection.execute(
                "UPDATE runs SET conversation_id='conversation-2' WHERE run_id='run-1'")
            with self.assertRaises(ResearchRecordError) as owner:
                store.append_snapshot(
                    connection, run_id="run-1", snapshot={}, created_at=NOW)
            assert owner.exception.code == "invalid_record_scope"
            connection.execute(
                "UPDATE runs SET conversation_id='conversation-1', status='completed' WHERE run_id='run-1'")
            with self.assertRaises(ResearchRecordError) as inactive:
                store.append_snapshot(
                    connection, run_id="run-1", snapshot={}, created_at=NOW)
            assert inactive.exception.code == "inactive_research"


    def test_transaction_rollback_leaves_no_partial_receipt(self):
        path, store = self.records
        with self.assertRaisesRegex(RuntimeError, "display write failed"):
            with closing(_connect(path)) as connection, connection:
                store.append_receipt(
                    connection, run_id="run-1", call_id="call-rollback", tool_name="lookup",
                    arguments={}, result={"ok": True}, created_at=NOW,
                )
                raise RuntimeError("display write failed")

        assert store.read_receipts("run-1") == ()


    def test_reads_reject_tampered_hashes_and_noncontiguous_snapshot_revisions(self):
        path, store = self.records
        with closing(_connect(path)) as connection, connection:
            store.append_receipt(
                connection, run_id="run-1", call_id="call-1", tool_name="lookup",
                arguments={}, result={"ok": True}, created_at=NOW,
            )
            store.append_snapshot(
                connection, run_id="run-1", snapshot={"revision": 1}, created_at=NOW)
            store.append_snapshot(
                connection, run_id="run-1", snapshot={"revision": 2}, created_at=NOW)
            connection.execute("DROP TRIGGER author_tool_receipts_no_update")
            connection.execute("DROP TRIGGER research_snapshots_no_update")
            connection.execute(
                "UPDATE author_tool_receipts SET result_json='{}' WHERE run_id='run-1'")
            connection.execute(
                "UPDATE research_snapshots SET revision=3 WHERE run_id='run-1' AND revision=2")

        with self.assertRaises(ResearchRecordError) as receipt_error:
            store.read_receipts("run-1")
        assert receipt_error.exception.code == "record_integrity_mismatch"
        with self.assertRaisesRegex(ResearchRecordError, "not contiguous"):
            store.read_snapshot("run-1")


    def test_generic_event_cannot_create_authoritative_tool_result(self):
        service = ChatService.__new__(ChatService)
        with self.assertRaises(ChatError) as rejected:
            service._event(
                "run-1", "tool_result",
                {"name": "lookup", "result": {}, "authoritative": True},
                claim=None,
            )
        assert rejected.exception.code == "formal_tool_receipt_required"


    def test_oversized_combined_tool_completion_is_rejected_before_any_store_access(self):
        service = ChatService.__new__(ChatService)
        with self.assertRaises(ChatError) as rejected:
            service._record_tool_completion(
                "run-1",
                call_id="call-large",
                tool_name="lookup",
                arguments={"value": "a" * 600_000},
                result={"value": "b" * 600_000},
                claim=None,
            )
        assert rejected.exception.code == "event_too_large"
