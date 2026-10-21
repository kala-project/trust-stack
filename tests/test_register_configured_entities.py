import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.register_configured_entities import register


class RegisterConfiguredEntitiesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'evidence.sqlite3'
        self.config = Path(self.tmp.name) / 'entities.json'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE companies(company_id TEXT PRIMARY KEY,name TEXT,ticker TEXT,description TEXT)')
            db.execute("INSERT INTO companies VALUES('OLD','Old Co','OLD','keep description')")
            db.execute('CREATE TABLE facts(value TEXT)')
            db.execute("INSERT INTO facts VALUES('unchanged')")

    def write(self, value):
        self.config.write_text(json.dumps(value))

    def rows(self):
        with closing(sqlite3.connect(self.db)) as db, db:
            return db.execute('SELECT * FROM companies ORDER BY company_id').fetchall()

    def test_append_only_and_idempotent(self):
        self.write({'OLD': {'name': 'Old Co', 'aliases': ['Old']},
                    'NEW': {'name': 'New Co', 'aliases': ['新公司']}})
        before = self.rows()
        self.assertEqual(register(self.db, self.config), ['NEW'])
        self.assertEqual(register(self.db, self.config), [])
        self.assertIn(before[0], self.rows())
        with closing(sqlite3.connect(self.db)) as db, db:
            self.assertEqual(db.execute('SELECT * FROM facts').fetchall(), [('unchanged',)])

    def test_mismatch_rolls_back_entire_batch(self):
        self.write({'NEW': {'name': 'New Co', 'aliases': []},
                    'OLD': {'name': 'Wrong issuer', 'aliases': []}})
        before = self.rows()
        with self.assertRaisesRegex(ValueError, 'mismatch'):
            register(self.db, self.config)
        self.assertEqual(self.rows(), before)

    def test_invalid_config_never_writes(self):
        before = self.rows()
        for config in [{}, [], {'X': {'name': 'X', 'aliases': [None]}},
                       {'x': {'name': 'X', 'aliases': []}}]:
            self.write(config)
            with self.assertRaises(ValueError):
                register(self.db, self.config)
            self.assertEqual(self.rows(), before)

    def test_missing_store_is_not_created(self):
        self.write({'NEW': {'name': 'New Co', 'aliases': []}})
        path = self.db.with_name('missing.sqlite3')
        with self.assertRaises(sqlite3.OperationalError):
            register(path, self.config)
        self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
