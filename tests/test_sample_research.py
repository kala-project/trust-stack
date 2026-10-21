from contextlib import closing
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.parse import quote

from workbench.build import build_database
from workbench.sample_research import SampleResearchError, SampleResearchService
from workbench.server import EvidenceHTTPServer


SCOPE = {
    "company_ids": ["MSFT", "AAPL"],
    "metric": "revenue",
    "period_kind": "quarter",
    "start_date": "2025-04-01",
    "end_date": "2025-09-27",
}


class NoModelProvider:
    def status(self):
        return {"available": False, "provider": "sample-test", "reason": "model disabled"}

    def stream(self, *_args):
        raise AssertionError("sample research must not call a model")


class SampleResearchServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.evidence = root / "evidence.sqlite3"
        self.results = root / "sample.sqlite3"
        build_database(db_path=self.evidence)
        self.service = SampleResearchService(self.evidence, self.results)

    def tearDown(self):
        self.temp.cleanup()

    def update_fact(self, company_id, **values):
        assignments = ",".join(f"{key}=?" for key in values)
        with closing(sqlite3.connect(self.evidence)) as connection:
            connection.execute(
                f"UPDATE facts SET {assignments} WHERE company_id=? AND metric='revenue' "
                "AND period_end BETWEEN '2025-04-01' AND '2025-09-27'",
                [*values.values(), company_id],
            )
            connection.commit()

    def make_comparable(self, msft_value="1", aapl_value="2", unit="USD millions"):
        common = {"unit": unit, "period_start": "2025-04-01", "period_end": "2025-06-30",
                  "basis": "GAAP comparable basis"}
        self.update_fact("MSFT", value=msft_value, **common)
        self.update_fact("AAPL", value=aapl_value, **common)

    def test_current_frozen_data_is_complete_comparison_but_not_aggregated(self):
        result = self.service.create(SCOPE)
        self.assertEqual(result["claim_boundary"], "User-defined sample; not representative of an industry or the industry as a whole")
        self.assertEqual(result["coverage"], {
            "requested_members": 2, "members_with_unique_input": 2, "aggregation_denominator": 2})
        self.assertEqual(result["aggregate"]["status"], "disabled")
        self.assertIsNone(result["aggregate"]["value"])
        self.assertTrue(any("unit" in item for item in result["aggregate"]["blockers"]))
        self.assertEqual([item["company"]["company_id"] for item in result["members"]], ["MSFT", "AAPL"])
        for member in result["members"]:
            self.assertTrue(member["inputs"])
            evidence = member["inputs"][0]["evidence"]
            self.assertTrue(evidence["quote"])
            self.assertEqual(len(evidence["source"]["sha256"]), 64)

    def test_operating_income_missing_is_not_replaced_with_revenue_or_zero(self):
        result = self.service.create({**SCOPE, "metric": "operating_income"})
        apple = next(item for item in result["members"] if item["company"]["company_id"] == "AAPL")
        self.assertEqual((apple["status"], apple["selected"], apple["inputs"]), ("missing", None, []))
        self.assertEqual(result["coverage"]["aggregation_denominator"], 2)
        self.assertEqual(result["aggregate"]["status"], "disabled")
        self.assertEqual(result["aggregate"]["inputs"], [])

    def test_idempotent_snapshot_and_exact_version_read(self):
        first = self.service.create(SCOPE)
        second = self.service.create(SCOPE)
        self.assertEqual((first["result_id"], first["version"], first["content_hash"]),
                         (second["result_id"], second["version"], second["content_hash"]))
        self.assertEqual(self.service.get(first["result_id"], 1), first)
        with closing(sqlite3.connect(self.results)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM result_versions").fetchone()[0], 1)
        with self.assertRaises(SampleResearchError):
            self.service.create({**SCOPE, "company_ids": ["MSFT"]})
        with self.assertRaises(SampleResearchError):
            self.service.create({**SCOPE, "start_date": "2025-02-30"})

    def test_enum_fields_reject_unhashable_and_non_string_types(self):
        for field, value in (("metric", []), ("metric", {}),
                             ("period_kind", []), ("period_kind", {})):
            with self.subTest(field=field, value=type(value).__name__):
                with self.assertRaises(SampleResearchError) as caught:
                    self.service.create({**SCOPE, field: value})
                self.assertEqual((caught.exception.code, caught.exception.status_code),
                                 ("invalid_scope", 400))

    def test_exact_high_precision_sum_and_invalid_numeric_values(self):
        left = "1234567890123456789012345678.12345678901234567890"
        right = "0.00000000000000000001"
        self.make_comparable(left, right)
        result = self.service.create(SCOPE)
        self.assertEqual(result["aggregate"]["status"], "computed")
        self.assertEqual(result["aggregate"]["value"],
                         "1234567890123456789012345678.12345678901234567891")

        for invalid in ("NaN", "Infinity", "1e1001", "9" * 1001):
            with self.subTest(invalid=invalid[:20]):
                self.update_fact("MSFT", value=invalid)
                with self.assertRaises(RuntimeError):
                    self.service.create(SCOPE)

    def test_empty_or_mismatched_comparability_fields_and_unknown_unit_block_sum(self):
        cases = (
            ({"basis": ""}, {"basis": ""}, "basis"),
            ({"period_start": ""}, {"period_start": ""}, "period_start"),
            ({"basis": "basis-a"}, {"basis": "basis-b"}, "basis"),
            ({"unit": "USD millions"}, {"unit": "USD billions"}, "unit"),
            ({"unit": "widgets"}, {"unit": "widgets"}, "currency unit"),
        )
        for msft_change, aapl_change, blocker in cases:
            with self.subTest(blocker=blocker):
                self.make_comparable()
                self.update_fact("MSFT", **msft_change)
                self.update_fact("AAPL", **aapl_change)
                result = self.service.create(SCOPE)
                self.assertEqual(result["aggregate"]["status"], "disabled")
                self.assertTrue(any(blocker in item for item in result["aggregate"]["blockers"]))

    def test_corrupt_evidence_chain_sha_and_quote_are_not_reported_as_missing(self):
        with closing(sqlite3.connect(self.evidence)) as connection:
            apple_fact = connection.execute(
                "SELECT fact_id,evidence_id FROM facts WHERE company_id='AAPL' AND metric='revenue'"
            ).fetchone()
            msft_evidence = connection.execute(
                "SELECT evidence_id FROM facts WHERE company_id='MSFT' AND metric='revenue' "
                "AND period_end='2025-06-30'"
            ).fetchone()[0]
            connection.execute("UPDATE facts SET evidence_id=? WHERE fact_id=?",
                               (msft_evidence, apple_fact[0]))
            connection.commit()
        with self.assertRaisesRegex(RuntimeError, "cross-company"):
            self.service.create(SCOPE)

        build_database(db_path=self.evidence)
        with closing(sqlite3.connect(self.evidence)) as connection:
            connection.execute("UPDATE evidence SET source_sha256=? WHERE evidence_id=?",
                               ("0" * 64, apple_fact[1]))
            connection.commit()
        with self.assertRaisesRegex(RuntimeError, "sha256"):
            self.service.create(SCOPE)

        build_database(db_path=self.evidence)
        with closing(sqlite3.connect(self.evidence)) as connection:
            connection.execute("UPDATE evidence SET quote='wrong quote' WHERE evidence_id=?",
                               (apple_fact[1],))
            connection.commit()
        with self.assertRaisesRegex(RuntimeError, "quote locator"):
            self.service.create(SCOPE)

        build_database(db_path=self.evidence)
        with closing(sqlite3.connect(self.evidence)) as connection:
            connection.execute("DELETE FROM evidence WHERE evidence_id=?", (apple_fact[1],))
            connection.commit()
        with self.assertRaisesRegex(RuntimeError, "orphan"):
            self.service.create(SCOPE)

    def test_saved_record_tampering_breaks_get_and_idempotent_create(self):
        result = self.service.create(SCOPE)
        with closing(sqlite3.connect(self.results)) as connection:
            saved = json.loads(connection.execute(
                "SELECT result_json FROM result_versions").fetchone()[0])
            saved["claim_boundary"] = "tampered"
            connection.execute("UPDATE result_versions SET result_json=?", (json.dumps(saved),))
            connection.commit()
        with self.assertRaisesRegex(RuntimeError, "content hash"):
            self.service.get(result["result_id"])
        with self.assertRaisesRegex(RuntimeError, "content hash"):
            self.service.create(SCOPE)

    def test_concurrent_create_is_one_idempotent_version(self):
        barrier = threading.Barrier(6)
        results, errors = [], []

        def create():
            try:
                barrier.wait()
                results.append(self.service.create(SCOPE))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=create) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(errors)
        self.assertEqual(len(results), 6)
        self.assertEqual({(item["result_id"], item["version"], item["content_hash"])
                          for item in results},
                         {(results[0]["result_id"], 1, results[0]["content_hash"])})
        with closing(sqlite3.connect(self.results)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM result_versions").fetchone()[0], 1)

    def test_concurrent_initialization_creates_complete_schema(self):
        result_db = Path(self.temp.name) / "concurrent.sqlite3"
        barrier = threading.Barrier(4)
        services, errors = [], []

        def initialize():
            try:
                barrier.wait()
                services.append(SampleResearchService(self.evidence, result_db))
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=initialize) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(errors)
        self.assertEqual(len(services), 4)
        with closing(sqlite3.connect(result_db)) as connection:
            self.assertEqual(dict(connection.execute("SELECT key,value FROM metadata")), {
                "producer": "workbench.sample_research", "schema_version": "sample-research/1.0"})
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' "
                "AND name IN ('metadata','result_versions')").fetchone()[0], 2)


class SampleResearchHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cls.evidence, cls.sample = root / "evidence.sqlite3", root / "sample.sqlite3"
        build_database(db_path=cls.evidence)
        cls.server = EvidenceHTTPServer(0, cls.evidence, sample_db=cls.sample, provider=NoModelProvider())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port
        cls.origin = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(3); cls.temp.cleanup()

    def request(self, path, data=None, origin=None):
        body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        headers = {"Origin": origin or self.origin, "Content-Type": "application/json"} if data is not None else {}
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        try:
            connection.request("POST" if data is not None else "GET", path, body=body, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_options_create_and_owned_version_read(self):
        code, options = self.request("/api/sample-research/options")
        self.assertEqual(code, 200)
        self.assertEqual({item["id"] for item in options["companies"]}, {"MSFT", "AAPL", "NVDA", "CRM", "IBM"})
        self.assertEqual(options["mode"], "custom_sample_not_industry")

        code, result = self.request("/api/sample-research/results", SCOPE)
        self.assertEqual((code, result["aggregate"]["status"]), (201, "disabled"))
        path = "/api/sample-research/result?id=" + quote(result["result_id"]) + "&version=1"
        self.assertEqual(self.request(path), (200, result))
        self.assertEqual(self.request("/api/sample-research/result?id=sample-bad")[0], 400)

    def test_write_requires_same_origin_and_strict_registered_scope(self):
        self.assertEqual(self.request("/api/sample-research/results", SCOPE, "http://evil.test")[0], 403)
        code, error = self.request("/api/sample-research/results", {**SCOPE, "company_ids": ["MSFT", "GOOG"]})
        self.assertEqual((code, error["error"]), (400, "unknown_member"))
        code, error = self.request("/api/sample-research/results", {**SCOPE, "extra": True})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
