from contextlib import closing
import gc
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import warnings
from unittest import mock

from workbench.build import build_database
from workbench.wiki import WikiError, WikiService


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/source-catalog.json"


class WikiServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.evidence_db = cls.root / "evidence.sqlite3"
        build_database(CATALOG, cls.evidence_db, ROOT)
        with closing(sqlite3.connect(cls.evidence_db)) as connection:
            cls.msft_claim = connection.execute(
                "SELECT claim_id,text FROM claims WHERE company_id='MSFT' AND text LIKE '%收入%' LIMIT 1"
            ).fetchone()
            if cls.msft_claim is None:
                cls.msft_claim = connection.execute(
                    "SELECT claim_id,text FROM claims WHERE company_id='MSFT' ORDER BY claim_id LIMIT 1"
                ).fetchone()
            cls.apple_claim_id = connection.execute(
                "SELECT claim_id FROM claims WHERE company_id='AAPL' ORDER BY claim_id LIMIT 1"
            ).fetchone()[0]

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.wiki_db = self.root / f"{self.id().rsplit('.', 1)[-1]}.sqlite3"
        self.service = WikiService(self.evidence_db, self.wiki_db)

    @property
    def statements(self):
        return [
            {"kind": "evidence_summary", "text": self.msft_claim[1], "claim_ids": [self.msft_claim[0]]},
            {"kind": "hypothesis", "text": "ROI may improve, but this has not been verified.", "claim_ids": []},
            {"kind": "open_question", "text": "What costs are required to test the ROI hypothesis?", "claim_ids": []},
        ]

    def change(self, base=0, title="Microsoft research notes"):
        return self.service.create_change("MSFT", base, title, self.statements)

    def approve(self, change):
        return self.service.review(change["change_id"], change["content_hash"], "approve")

    def test_database_aliases_are_rejected_without_modifying_evidence(self):
        before = self.evidence_db.read_bytes()
        with closing(sqlite3.connect(self.evidence_db)) as connection:
            tables_before = connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
            ).fetchall()
        symlink = self.root / f"{self.id().rsplit('.', 1)[-1]}-symlink.sqlite3"
        hardlink = self.root / f"{self.id().rsplit('.', 1)[-1]}-hardlink.sqlite3"
        symlink.symlink_to(self.evidence_db)
        os.link(self.evidence_db, hardlink)
        for alias in (self.evidence_db, symlink, hardlink):
            with self.subTest(alias=alias), self.assertRaises(WikiError) as caught:
                WikiService(self.evidence_db, alias)
            self.assertEqual(caught.exception.code, "database_path_collision")
            self.assertEqual(self.evidence_db.read_bytes(), before)
        with closing(sqlite3.connect(self.evidence_db)) as connection:
            tables_after = connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
            ).fetchall()
        self.assertEqual(tables_after, tables_before)

    def test_real_build_revenue_and_roi_hypothesis_round_trip(self):
        draft = self.service.get_page("MSFT")
        self.assertEqual(draft["version"], 0)
        self.assertEqual(draft["statements"], [])
        change = self.change()
        self.assertEqual(change["status"], "awaiting_approval")
        self.assertEqual(change["statements"][1]["kind"], "hypothesis")
        self.assertEqual(self.approve(change)["status"], "approved")
        page = self.service.publish(change["change_id"], change["content_hash"])
        self.assertEqual(page["version"], 1)
        self.assertEqual(page["semantic_status"], "unreviewed")
        self.assertTrue(any("not financial semantic verification" in item for item in page["limitations"]))
        self.assertEqual(self.service.get_change(change["change_id"])["status"], "applied")

    def test_hash_mismatch_and_unreviewed_publish_are_conflicts(self):
        change = self.change()
        with self.assertRaises(WikiError) as caught:
            self.service.review(change["change_id"], "0" * 64, "approve")
        self.assertEqual((caught.exception.code, caught.exception.status_code), ("hash_mismatch", 409))
        with self.assertRaises(WikiError) as caught:
            self.service.publish(change["change_id"], change["content_hash"])
        self.assertEqual((caught.exception.code, caught.exception.status_code), ("approval_required", 409))

    def test_two_approved_changes_on_same_baseline_make_loser_stale(self):
        first = self.change(title="First")
        second = self.change(title="Second")
        self.approve(first)
        self.approve(second)
        self.service.publish(first["change_id"], first["content_hash"])
        with self.assertRaises(WikiError) as caught:
            self.service.publish(second["change_id"], second["content_hash"])
        self.assertEqual((caught.exception.code, caught.exception.status_code), ("stale_change", 409))
        stale = self.service.get_change(second["change_id"])
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["events"][-1]["detail"]["reason"], "base_version_changed")

    def test_restart_preserves_complete_version_history(self):
        first = self.change(title="Version one")
        self.approve(first)
        self.service.publish(first["change_id"], first["content_hash"])
        restarted = WikiService(self.evidence_db, self.wiki_db)
        second = restarted.create_change("MSFT", 1, "Version two", self.statements)
        restarted.review(second["change_id"], second["content_hash"], "approve")
        restarted.publish(second["change_id"], second["content_hash"])
        self.assertEqual(restarted.get_page("MSFT")["versions"], [1, 2])
        self.assertEqual(restarted.get_page("MSFT", 1)["title"], "Version one")
        self.assertEqual(restarted.get_page("MSFT", 2)["title"], "Version two")
        self.assertEqual(len(restarted.list_changes("MSFT")), 2)

    def test_foreign_missing_and_empty_evidence_claims_are_rejected(self):
        invalid_sets = [
            [{"kind": "evidence_summary", "text": "foreign", "claim_ids": [self.apple_claim_id]}],
            [{"kind": "evidence_summary", "text": "missing", "claim_ids": ["claim-does-not-exist"]}],
            [{"kind": "evidence_summary", "text": "empty", "claim_ids": []}],
        ]
        expected = ["foreign_claim_reference", "invalid_claim_reference", "invalid_arguments"]
        for statements, code in zip(invalid_sets, expected):
            with self.subTest(code=code), self.assertRaises(WikiError) as caught:
                self.service.create_change("MSFT", 0, "Bad", statements)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.status_code, 400)

    def test_unknown_statement_fields_and_unbounded_content_are_rejected(self):
        bad = {"kind": "hypothesis", "text": "Maybe", "claim_ids": [], "author": "admin"}
        with self.assertRaises(WikiError):
            self.service.create_change("MSFT", 0, "Bad", [bad])
        with self.assertRaises(WikiError):
            self.service.create_change("MSFT", 0, "x" * 201, [])
        self.assertNotIn("author", self.change())

    def test_source_version_change_invalidates_approval_and_marks_page_review(self):
        change = self.change()
        self.approve(change)
        with closing(sqlite3.connect(self.evidence_db)) as connection:
            old = connection.execute(
                "SELECT source_version_id FROM sources ORDER BY source_id LIMIT 1"
            ).fetchone()[0]
            connection.execute(
                "UPDATE sources SET source_version_id=? WHERE source_version_id=?", (old + "-revised", old)
            )
            connection.commit()
        try:
            with self.assertRaises(WikiError) as caught:
                self.service.publish(change["change_id"], change["content_hash"])
            self.assertEqual(caught.exception.code, "stale_change")
            stale = self.service.get_change(change["change_id"])
            self.assertEqual(stale["events"][-1]["detail"]["reason"], "evidence_version_changed")
        finally:
            with closing(sqlite3.connect(self.evidence_db)) as connection:
                connection.execute(
                    "UPDATE sources SET source_version_id=? WHERE source_version_id=?", (old, old + "-revised")
                )
                connection.commit()

        fresh = self.change(title="Frozen page")
        self.approve(fresh)
        self.service.publish(fresh["change_id"], fresh["content_hash"])
        with closing(sqlite3.connect(self.evidence_db)) as connection:
            connection.execute("UPDATE metadata SET value=value || '-next' WHERE key='generated_at'")
            connection.commit()
        try:
            historical = self.service.get_page("MSFT", 1)
            self.assertEqual(historical["freshness_status"], "needs_review")
            self.assertEqual(historical["title"], "Frozen page")
        finally:
            with closing(sqlite3.connect(self.evidence_db)) as connection:
                connection.execute("UPDATE metadata SET value=substr(value,1,length(value)-5) WHERE key='generated_at'")
                connection.commit()

    def test_evidence_content_mutations_invalidate_approval(self):
        mutations = [
            ("claims", "claim_id", "text"),
            ("facts", "fact_id", "value"),
            ("evidence", "evidence_id", "quote"),
        ]
        for table, key, column in mutations:
            with self.subTest(table=table):
                change = self.change(title=f"Mutation check: {table}")
                self.approve(change)
                with closing(sqlite3.connect(self.evidence_db)) as connection:
                    row_id, original = connection.execute(
                        f"SELECT {key},{column} FROM {table} ORDER BY {key} LIMIT 1"
                    ).fetchone()
                    connection.execute(
                        f"UPDATE {table} SET {column}=? WHERE {key}=?", (original + "-mutated", row_id)
                    )
                    connection.commit()
                try:
                    with self.assertRaises(WikiError) as caught:
                        self.service.publish(change["change_id"], change["content_hash"])
                    self.assertEqual((caught.exception.code, caught.exception.status_code), ("stale_change", 409))
                    stale = self.service.get_change(change["change_id"])
                    self.assertEqual(stale["events"][-1]["detail"]["reason"], "evidence_version_changed")
                finally:
                    with closing(sqlite3.connect(self.evidence_db)) as connection:
                        connection.execute(
                            f"UPDATE {table} SET {column}=? WHERE {key}=?", (original, row_id)
                        )
                        connection.commit()

    def test_published_page_needs_review_after_content_mutation(self):
        change = self.change(title="Content-bound page")
        self.approve(change)
        self.service.publish(change["change_id"], change["content_hash"])
        with closing(sqlite3.connect(self.evidence_db)) as connection:
            fact_id, original = connection.execute(
                "SELECT fact_id,value FROM facts ORDER BY fact_id LIMIT 1"
            ).fetchone()
            connection.execute("UPDATE facts SET value=? WHERE fact_id=?", (original + "-mutated", fact_id))
            connection.commit()
        try:
            self.assertEqual(self.service.get_page("MSFT", 1)["freshness_status"], "needs_review")
        finally:
            with closing(sqlite3.connect(self.evidence_db)) as connection:
                connection.execute("UPDATE facts SET value=? WHERE fact_id=?", (original, fact_id))
                connection.commit()

    def test_reject_is_manual_ack_but_never_publishable(self):
        change = self.change()
        rejected = self.service.review(change["change_id"], change["content_hash"], "reject")
        self.assertEqual(rejected["status"], "rejected")
        with self.assertRaises(WikiError) as caught:
            self.service.publish(change["change_id"], change["content_hash"])
        self.assertEqual(caught.exception.code, "approval_required")

    def test_all_sql_connections_are_closed_without_resource_warning(self):
        opened = []
        real_connect = sqlite3.connect

        class TrackingConnection(sqlite3.Connection):
            closed = False

            def close(self):
                self.closed = True
                super().close()

        def connect(*args, **kwargs):
            kwargs["factory"] = TrackingConnection
            connection = real_connect(*args, **kwargs)
            opened.append(connection)
            return connection

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with mock.patch("workbench.wiki.sqlite3.connect", side_effect=connect):
                page = self.service.get_page("MSFT")
                change = self.service.create_change("MSFT", page["version"], "Tracked", self.statements)
                self.service.get_change(change["change_id"])
                self.service.list_changes("MSFT")
            gc.collect()
        self.assertTrue(opened)
        self.assertTrue(all(connection.closed for connection in opened))
        self.assertFalse([item for item in caught if issubclass(item.category, ResourceWarning)])

    def test_error_shape(self):
        error = WikiError("bad", "No", 409)
        self.assertEqual(error.as_dict(), {"error": "bad", "message": "No"})
        self.assertEqual(error.status_code, 409)


if __name__ == "__main__":
    unittest.main()
