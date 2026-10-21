from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from workbench.execution_scope import (
    ExecutionScope, ExecutionScopes, decode_scope, describe, normalize,
)
from workbench.research_inputs import InputError, ResearchInputStore
from workbench.research_tools import ToolError


class ExecutionScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "scope.sqlite3"
        with closing(self.connect()) as connection:
            connection.executescript("""
                CREATE TABLE conversations(conversation_id TEXT PRIMARY KEY, company_id TEXT NOT NULL);
                CREATE TABLE runs(run_id TEXT PRIMARY KEY, conversation_id TEXT REFERENCES conversations,
                                  status TEXT);
                INSERT INTO conversations VALUES('multi','MSFT'),('other','MSFT'),('missing','MSFT');
                INSERT INTO runs VALUES('run-multi','multi','running'),('run-other','other','running');
            """)
        self.inputs = ResearchInputStore(self.connect)
        self.scopes = self.inputs.scopes
        with closing(self.connect()) as connection, connection:
            self.scopes.create(connection, "multi", ["MSFT", "AAPL"])
            self.scopes.create(connection, "other", ["MSFT"])
            self.inputs.begin_run(connection, "run-multi")
            self.inputs.begin_run(connection, "run-other")

    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def test_membership_is_order_independent_and_persistent(self):
        expected = describe(["MSFT", "AAPL"])
        self.assertEqual(expected, describe(["AAPL", "MSFT"]))
        self.assertEqual(self.scopes.read("multi"), expected)
        self.assertEqual(ExecutionScopes(self.connect).read("multi")["company_ids"], ["AAPL", "MSFT"])
        self.assertNotIn("legacy", self.scopes.read("multi"))
        with self.assertRaises(ToolError) as error:
            self.scopes.read("missing")
        self.assertEqual(error.exception.code, "execution_scope_missing")

    def test_scope_never_defaults_to_an_arbitrary_subject(self):
        for invalid in ([], ["MSFT", "MSFT"], [" MSFT"], [None], "MSFT", ["x"] * 9):
            with self.subTest(invalid=invalid), self.assertRaises(ToolError):
                normalize(invalid)
        with closing(self.connect()) as connection, connection, self.assertRaises(ToolError):
            self.scopes.create(connection, "missing", ["AAPL"])

    def test_complete_json_decoder_rejects_wrong_fields_version_and_hash(self):
        valid = describe(["MSFT", "AAPL"])
        self.assertEqual(decode_scope(valid).to_json(), valid)
        invalid = [
            {**valid, "legacy": False},
            {key: value for key, value in valid.items() if key != "scope_id"},
            {**valid, "version": "research-execution-scope/2"},
            {**valid, "payload_sha256": "0" * 64},
            {**valid, "scope_id": "scope-" + "0" * 64},
            {**valid, "company_ids": []},
            {**valid, "company_ids": ["MSFT", "AAPL"]},
            {**valid, "company_ids": ("AAPL", "MSFT")},
            {**valid, "payload_sha256": None},
            {**valid, "company_ids": ["MSFT", "MSFT"]},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ToolError):
                decode_scope(value)
        with self.assertRaises(ToolError):
            ExecutionScope(("MSFT",), "scope-" + "0" * 64, "0" * 64)

    def test_domain_constructor_rejects_mutability_and_wrong_scalar_types(self):
        valid = ExecutionScope.create(["MSFT"])
        for ids, scope_id, digest in (
            (["MSFT"], valid.scope_id, valid.payload_sha256),
            (valid.company_ids, valid.scope_id, None),
            (valid.company_ids, False, valid.payload_sha256),
        ):
            with self.subTest(ids=ids, scope_id=scope_id, digest=digest), self.assertRaises(ToolError):
                ExecutionScope(ids, scope_id, digest)

    def test_scope_is_append_only_and_missing_new_record_fails(self):
        with closing(self.connect()) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE conversation_scopes SET scope_id='changed' WHERE conversation_id='multi'")
            connection.rollback()
            connection.execute("DROP TRIGGER conversation_scopes_no_delete")
            connection.execute("DELETE FROM conversation_scopes WHERE conversation_id='multi'")
            connection.commit()
        with self.assertRaises(ToolError) as error:
            self.scopes.read("multi")
        self.assertEqual(error.exception.code, "execution_scope_missing")

    def test_wrong_stored_version_and_fields_are_rejected(self):
        with closing(self.connect()) as connection, connection:
            connection.execute("DROP TRIGGER conversation_scope_contracts_no_update")
            connection.execute("UPDATE conversation_scope_contracts SET version='wrong' WHERE conversation_id='multi'")
        with self.assertRaises(ToolError) as error:
            self.scopes.read("multi")
        self.assertEqual(error.exception.code, "execution_scope_corrupt")

        with closing(self.connect()) as connection, connection:
            connection.execute("DROP TRIGGER conversation_scopes_no_update")
            connection.execute("UPDATE conversation_scopes SET payload_json=? WHERE conversation_id='other'",
                               ('{"company_ids":["MSFT"],"extra":true,"version":"research-execution-scope/1"}',))
        with self.assertRaises(ToolError) as error:
            self.scopes.read("other")
        self.assertEqual(error.exception.code, "execution_scope_corrupt")

    def test_tampered_scope_is_not_used_as_authorization(self):
        with closing(self.connect()) as connection, connection:
            connection.execute("DROP TRIGGER conversation_scopes_no_update")
            connection.execute("UPDATE conversation_scopes SET payload_json=? WHERE conversation_id='multi'",
                               ('{"version":"research-execution-scope/1","company_ids":["MSFT","FOREIGN"]}',))
        with self.assertRaises(ToolError) as error:
            self.scopes.read("multi")
        self.assertEqual(error.exception.code, "execution_scope_corrupt")
        with closing(self.connect()) as connection, self.assertRaises(InputError):
            self.inputs._owner(connection, "run-multi")

    def test_scope_reads_are_bound_to_the_requested_conversation(self):
        self.assertNotEqual(self.scopes.read("multi")["scope_id"], self.scopes.read("other")["scope_id"])
        with self.assertRaises(ToolError) as error:
            self.scopes.read("run-multi")
        self.assertEqual(error.exception.code, "scope_owner_missing")

    def test_input_ownership_uses_complete_scope_not_display_company(self):
        with closing(self.connect()) as connection:
            owner = self.inputs._owner(connection, "run-multi")
            other = self.inputs._owner(connection, "run-other")
        self.assertEqual(owner["company_id"], "MSFT")
        self.assertEqual(owner["company_ids"], ["AAPL", "MSFT"])
        self.assertEqual(other["company_ids"], ["MSFT"])
        self.assertNotEqual(owner["conversation_id"], other["conversation_id"])
        second_subject = {"fact": {"fact_id": "aapl-cell", "company_id": "AAPL", "value": "7"},
                          "interpretation": "private input fixture"}
        with closing(self.connect()) as connection, connection:
            self.inputs.record(connection, "run-multi", second_subject)
        self.assertEqual(self.inputs.read("run-multi", "aapl-cell"), second_subject)
        with closing(self.connect()) as connection, connection, self.assertRaises(InputError):
            self.inputs.record(connection, "run-other", second_subject)
        with self.assertRaises(InputError):
            self.inputs.read("run-multi", "aapl-cell", consumer_run_id="run-other")
        foreign = {"fact": {"fact_id": "foreign", "company_id": "FOREIGN", "value": "7"}}
        with closing(self.connect()) as connection, connection, self.assertRaises(InputError):
            self.inputs.record(connection, "run-multi", foreign)


if __name__ == "__main__":
    unittest.main()
