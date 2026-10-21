import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from workbench.native_sql import LEGACY_VERSION, NativeSqlError, VERSION, execute


class NativeSqlTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "provider.sqlite"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript("""
                CREATE TABLE measurements(
                    subject TEXT COLLATE NOCASE,
                    "unfamiliar field" INTEGER,
                    missing TEXT,
                    payload BLOB
                );
                INSERT INTO measurements VALUES
                    ('Alpha',7,NULL,X'0001'),
                    ('alpha',11,'known',X'02'),
                    ('Beta',13,NULL,X'03');
            """)
        self.sources = [{
            "alias": "provider", "path": str(self.path),
            "source_id": "synthetic-native-snapshot",
            "sha256": hashlib.sha256(self.path.read_bytes()).hexdigest(),
        }]

    def test_preserves_native_collation_types_and_parameterized_aggregation(self):
        result = execute(
            self.sources,
            'SELECT subject,SUM("unfamiliar field"),COUNT(*) FROM provider.measurements '
            'WHERE "unfamiliar field">? GROUP BY subject ORDER BY subject', [0])
        self.assertEqual(result["rows"], [["Alpha", 18, 2], ["Beta", 13, 1]])
        self.assertEqual([column["index"] for column in result["columns"]], [0, 1, 2])
        self.assertTrue(any(read["column"] == "unfamiliar field" for read in result["reads"]))
        self.assertNotIn("path", result["sources"][0])
        self.assertEqual(result, execute(self.sources, result["sql"], [0]))

    def test_null_blob_duplicate_labels_and_row_limit_are_explicit(self):
        result = execute(self.sources, 'SELECT missing AS x,payload AS x FROM provider.measurements',
                         row_limit=1)
        self.assertEqual([column["name"] for column in result["columns"]], ["x", "x"])
        self.assertEqual(result["rows"], [[None, {"type": "blob", "base64": "AAE="}]])
        self.assertTrue(result["truncated"])
        self.assertIn("NULL is not zero", result["numeric_semantics"])

    def test_sql_native_precision_and_zero_division_are_not_claimed_exact(self):
        result = execute(self.sources, "SELECT 0.1+0.2,1/0,7/2")
        self.assertEqual(result["rows"], [[0.30000000000000004, None, 3]])
        self.assertIn("not exact decimal", result["numeric_semantics"])
        with self.assertRaises(NativeSqlError) as raised:
            execute(self.sources, "SELECT 1e999")
        self.assertEqual(raised.exception.code, "invalid_sql_result")

    def test_large_integers_real_storage_classes_and_negative_zero_survive_json(self):
        sql = "SELECT 9007199254740993,9223372036854775807,-9223372036854775808,1.0,-0.0"
        result = execute(self.sources, sql)
        self.assertEqual(result["version"], VERSION)
        self.assertEqual(result["rows"][0][:3], [
            {"type": "integer", "decimal": "9007199254740993"},
            {"type": "integer", "decimal": "9223372036854775807"},
            {"type": "integer", "decimal": "-9223372036854775808"},
        ])
        self.assertEqual(result["display"]["rows"][0],
                         ["9007199254740993", "9223372036854775807", "-9223372036854775808", "1.0", "-0.0"])
        self.assertEqual(result["display"]["storage_classes"][0], ["integer"] * 3 + ["real"] * 2)
        with self.assertRaises(NativeSqlError):
            execute(self.sources, "SELECT ?", [9007199254740993])
        with self.assertRaises(NativeSqlError):
            execute(self.sources, "SELECT ?", ["\ud800"])
        bound = execute(self.sources, "SELECT CAST(? AS INTEGER)", ["9007199254740993"])
        self.assertEqual(bound["rows"][0][0], result["rows"][0][0])
        legacy = execute(self.sources, sql, output_version=LEGACY_VERSION)
        self.assertEqual(legacy["rows"][0][0], 9007199254740993)
        self.assertNotIn("display", legacy)
        self.assertEqual(legacy, execute(self.sources, sql, output_version=LEGACY_VERSION))

    def test_cancellation_releases_active_worker_and_executor_slot(self):
        start = time.monotonic()
        with self.assertRaises(NativeSqlError) as raised:
            execute(
                self.sources,
                "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<50000) "
                "SELECT SUM(length(lower(?||x))) FROM n", ["X" * 20000],
                cancelled=lambda: time.monotonic() - start > 0.05)
        self.assertEqual(raised.exception.code, "sql_cancelled")
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(execute(self.sources, "SELECT 1")["rows"], [[1]])

    def test_write_attach_metadata_functions_and_nondeterminism_are_rejected(self):
        before = self.path.read_bytes()
        for sql in (
            "DELETE FROM provider.measurements",
            "CREATE TABLE provider.extra(value)",
            "ATTACH DATABASE ':memory:' AS extra",
            "PRAGMA provider.table_info(measurements)",
            "SELECT load_extension('arbitrary')",
            "SELECT random()",
            "SELECT datetime('now')",
            "SELECT readfile('/etc/passwd')",
            "SELECT 1; SELECT 2",
            "WITH measurements AS (SELECT * FROM pragma_database_list) SELECT * FROM measurements",
        ):
            with self.subTest(sql=sql), self.assertRaises(NativeSqlError):
                execute(self.sources, sql)
        self.assertEqual(self.path.read_bytes(), before)

    def test_two_original_snapshots_can_be_joined_without_retyping(self):
        second = {**self.sources[0], "alias": "comparison"}
        result = execute([*self.sources, second],
                         'SELECT SUM(a."unfamiliar field"*b."unfamiliar field") '
                         'FROM provider.measurements a JOIN comparison.measurements b '
                         'ON a.rowid=b.rowid')
        self.assertEqual(result["rows"], [[339]])
        self.assertEqual({read["alias"] for read in result["reads"]}, {"provider", "comparison"})

    def test_tampering_limits_cancellation_and_invalid_inputs_fail_explicitly(self):
        bad_hash = [{**self.sources[0], "sha256": "0" * 64}]
        with self.assertRaises(NativeSqlError) as raised:
            execute(bad_hash, "SELECT 1")
        self.assertEqual(raised.exception.code, "sql_source_integrity")
        with self.assertRaises(NativeSqlError) as raised:
            execute(self.sources, "SELECT 1", cancelled=lambda: True)
        self.assertEqual(raised.exception.code, "sql_cancelled")
        with self.assertRaises(NativeSqlError) as raised:
            execute(self.sources, "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL "
                    "SELECT x+1 FROM n WHERE x<1000000000) SELECT SUM(x) FROM n")
        self.assertEqual(raised.exception.code, "sql_execution_limit")
        for kwargs in ({"row_limit": True}, {"parameters": [True]}, {"parameters": [float("nan")]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(NativeSqlError):
                execute(self.sources, "SELECT 1", **kwargs)
        self.assertEqual(execute(self.sources, "SELECT 1")["rows"], [[1]])


if __name__ == "__main__":
    unittest.main()
