from contextlib import closing
from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest

from workbench.research_inputs import InputError, ResearchInputStore


class ResearchInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "inputs.sqlite3"
        with closing(self.connect()) as connection, connection:
            connection.executescript("""
                CREATE TABLE conversations(conversation_id TEXT PRIMARY KEY,company_id TEXT);
                CREATE TABLE runs(run_id TEXT PRIMARY KEY,conversation_id TEXT,status TEXT);
                INSERT INTO conversations VALUES('a','MSFT'),('b','MSFT');
                INSERT INTO runs VALUES
                    ('origin','a','running'),('next','a','running'),('last','a','running'),
                    ('foreign','b','running'),('stopped','a','cancelled'),('legacy','a','completed');
            """)
        self.store = ResearchInputStore(self.connect)
        with closing(self.connect()) as connection, connection:
            for run_id in ("origin", "next", "last", "foreign", "stopped"):
                self.store.begin_run(connection, run_id)
        self.result = {"fact": {"fact_id": "input-1", "company_id": "MSFT", "value": "123"},
                       "interpretation": "explicit fixture"}

    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def record(self, run_id, result=None):
        with closing(self.connect()) as connection, connection:
            self.store.record(connection, run_id, result if result is not None else self.result)

    def test_restart_deduplication_and_transient_fields(self):
        self.record("origin")
        self.record("origin", {**self.result, "_tool_budget": {"remaining": 2}})
        reopened = ResearchInputStore(self.connect)
        self.assertEqual(reopened.read("origin", "input-1"), self.result)
        with closing(self.connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM research_inputs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM research_input_uses").fetchone()[0], 1)

    def test_origin_is_stable_across_multiple_consumers(self):
        self.record("origin")
        first = self.store.read("origin", "input-1", consumer_run_id="next")
        self.record("next", first)
        second = self.store.read("next", "input-1", consumer_run_id="last")
        self.assertEqual(first, second)
        self.record("last", second)
        self.assertEqual(self.store.read("last", "input-1")["reused_from"]["run_id"], "origin")
        source, result = self.store.lookup("last", "input-1")
        self.assertEqual(source, "origin")
        self.assertEqual(result["reused_from"]["run_id"], "origin")

    def test_catalog_pagination_and_lookup_never_cross_conversations(self):
        for index in range(3):
            result = deepcopy(self.result)
            result["fact"]["fact_id"] = f"input-{index}"
            self.record("origin", result)
        first = self.store.catalog("next", limit=2)
        second = self.store.catalog("next", after=first["next_cursor"], limit=2)
        self.assertEqual([item["input_id"] for item in first["inputs"] + second["inputs"]],
                         ["input-0", "input-1", "input-2"])
        self.assertIsNone(second["next_cursor"])
        self.assertEqual(self.store.catalog("foreign")["inputs"], [])
        with self.assertRaises(InputError):
            self.store.lookup("foreign", "input-0")

    def test_wrong_owner_missing_use_and_stopped_writes_fail(self):
        self.record("origin")
        for callback, code in (
            (lambda: self.store.read("origin", "input-1", consumer_run_id="foreign"), "input_out_of_scope"),
            (lambda: self.store.read("next", "input-1"), "research_input_missing"),
            (lambda: self.record("stopped"), "inactive_research"),
            (lambda: self.record("foreign", {**self.result, "reused_from": {
                "run_id": "origin", "fact_id": "input-1"}}), "input_out_of_scope"),
        ):
            with self.subTest(code=code), self.assertRaises(InputError) as caught:
                callback()
            self.assertEqual(caught.exception.code, code)

    def test_same_id_cannot_change_interpretation_or_create_partial_use(self):
        self.record("origin")
        changed = deepcopy(self.result)
        changed["interpretation"] = "changed"
        with self.assertRaises(InputError) as caught:
            self.record("next", changed)
        self.assertEqual(caught.exception.code, "input_conflict")
        self.assertEqual(self.store.read("origin", "input-1"), self.result)
        with self.assertRaises(InputError):
            self.store.read("next", "input-1")

    def test_registry_is_append_only_and_tampering_is_detected(self):
        self.record("origin")
        for table in ("research_inputs", "research_input_uses", "research_input_contracts"):
            with self.subTest(table=table), closing(self.connect()) as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(f"DELETE FROM {table}")
        with closing(self.connect()) as connection, connection:
            connection.execute("DROP TRIGGER research_inputs_no_update")
            connection.execute("UPDATE research_inputs SET payload_json='{}'")
        with self.assertRaises(InputError) as caught:
            self.store.read("origin", "input-1")
        self.assertEqual(caught.exception.code, "input_integrity_mismatch")

    def test_transaction_failure_leaves_neither_input_nor_use(self):
        with self.assertRaisesRegex(RuntimeError, "event failed"):
            with closing(self.connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                self.store.record(connection, "origin", self.result)
                raise RuntimeError("event failed")
        with closing(self.connect()) as connection:
            for table in ("research_inputs", "research_input_uses"):
                self.assertEqual(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_legacy_runs_are_not_relabelled_and_unknown_contract_is_rejected(self):
        self.record("legacy")
        self.assertFalse(self.store.has_contract("legacy"))
        with self.assertRaises(InputError):
            self.store.read("legacy", "input-1")
        with closing(self.connect()) as connection, connection:
            connection.execute("DROP TRIGGER research_input_contracts_no_update")
            connection.execute("UPDATE research_input_contracts SET version='unknown' WHERE run_id='origin'")
        with self.assertRaises(InputError) as caught:
            self.store.has_contract("origin")
        self.assertEqual(caught.exception.code, "input_contract_unsupported")
