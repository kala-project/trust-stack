from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sqlite3
import tempfile
import unittest
from unittest import mock

import workbench.build as build_module
from workbench.build import BuildError, build_database, verify_archives
from workbench.parser import ParseError, microsoft_quarter_gaap, parse_decimal, parse_html
from workbench.service import EvidenceService

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/source-catalog.json"


def changed_input_fixture(parent: Path) -> tuple[Path, Path]:
    """Copy loaded inputs, change disclosed values, and update copied catalog integrity."""
    root = parent / "fixture"
    shutil.copytree(ROOT / "data", root / "data")
    changes = {
        "data/raw/microsoft-fy2025-q4-press-release.html": (
            (b">18%</span>", b">19%</span>"),
            (b">17%</span>", b">16%</span>"),
        ),
        "data/raw/apple-fy2025-q4-press-release.html": ((
            b"quarterly revenue of $102.5 billion, up 8 percent year over year. "
            b"Diluted earnings per share was $1.85, up 13 percent",
            b"quarterly revenue of $103.7 billion, up 9 percent year over year. "
            b"Diluted earnings per share was $1.91, up 14 percent",
        ),),
    }
    catalog_path = root / "data/source-catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    by_path = {item["artifact"]["path"]: item for item in catalog["sources"] if item.get("artifact")}
    for relative, replacements in changes.items():
        path = root / relative
        raw = path.read_bytes()
        for old, new in replacements:
            if raw.count(old) < 1:
                raise AssertionError(f"fixture token missing: {relative}")
            raw = raw.replace(old, new, 1)
        path.write_bytes(raw)
        by_path[relative]["artifact"]["bytes"] = len(raw)
        by_path[relative]["artifact"]["sha256"] = hashlib.sha256(raw).hexdigest()
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    return root, catalog_path


class ParserTests(unittest.TestCase):
    def test_scripts_styles_and_noscript_are_excluded(self):
        doc = parse_html(b"<p>safe</p><script>evil()</script><style>x</style><noscript>hidden</noscript><p>end</p>")
        self.assertEqual(doc.text, "safe end")
        self.assertNotIn("evil", doc.text)

    def test_table_rows_and_exact_spans(self):
        doc = parse_html(b"<h1>A</h1><table><tr><th>Name</th><th>Value</th></tr><tr><td>X</td><td>2</td></tr></table>")
        self.assertEqual(doc.tables[0].rows, [["Name", "Value"], ["X", "2"]])
        start, end = doc.tables[0].row_spans[1]
        self.assertEqual(doc.text[start:end], "X 2")
        self.assertEqual(doc.text[doc.tables[0].start:doc.tables[0].end], "Name Value X 2")

    def test_decimal_negative_and_units_syntax(self):
        self.assertEqual(str(parse_decimal("$(1,234.50)")), "-1234.50")
        self.assertEqual(str(parse_decimal("$ 76,441")), "76441")
        with self.assertRaises(ParseError):
            parse_decimal("$4 million")

    def test_microsoft_missing_table_fails_closed(self):
        with self.assertRaises(ParseError):
            microsoft_quarter_gaap(parse_html(b"<p>Revenue $1</p>"), 2025)

    def test_quarter_table_not_twelve_month_table(self):
        raw = (ROOT / "data/raw/microsoft-fy2025-q4-press-release.html").read_bytes()
        result = microsoft_quarter_gaap(parse_html(raw), 2025)
        self.assertIn("Three Months Ended", " ".join(result["table"].rows[0]))
        self.assertNotIn("Twelve Months Ended", " ".join(result["table"].rows[0]))
        self.assertEqual(str(result["years"][2025]["revenue"]), "76441")


class BuiltEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.temp.name) / "evidence.sqlite3"
        build_database(CATALOG, cls.db, ROOT)
        cls.service = EvidenceService(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_actual_microsoft_facts_and_units(self):
        facts = self.service.company("MSFT")["facts"]
        current = {(f["metric"], f["value"], f["unit"]) for f in facts if f["period_end"] == "2025-06-30" and f["period_kind"] == "quarter"}
        self.assertIn(("revenue", "76441", "USD millions"), current)
        self.assertIn(("operating_income", "34323", "USD millions"), current)
        self.assertIn(("eps_diluted", "3.65", "USD/share"), current)

    def test_decimal_yoy_calculations_are_actual(self):
        claims = self.service.company("MSFT")["claims"]
        texts = " ".join(c["text"] for c in claims if c["verification_status"] == "verified_calculation")
        self.assertIn("18.10%", texts)
        self.assertIn("22.91%", texts)
        self.assertIn("23.73%", texts)

    def test_sql_activity_records_query_params_inputs_outputs(self):
        claim = next(c for c in self.service.company("MSFT")["claims"] if "18.10%" in c["text"])
        payload = self.service.evidence(claim["claim_id"])
        activity = payload["activities"][0]
        self.assertEqual(activity["type"], "sql_calculation")
        self.assertIn("SELECT fact_id,value", activity["parameters"]["query"])
        self.assertIn("WHERE fact_id IN (?,?)", activity["parameters"]["query"])
        self.assertEqual(activity["parameters"]["params"], activity["parameters"]["selected_fact_ids"])
        self.assertEqual(len(activity["parameters"]["all_results"]), 2)
        self.assertEqual(
            {row["fact_id"] for row in activity["parameters"]["all_results"]},
            set(activity["parameters"]["selected_fact_ids"]),
        )
        self.assertEqual(activity["outputs"], ["18.10"])
        self.assertEqual(len(activity["inputs"]), 2)

    def test_reported_and_constant_currency_are_qualified(self):
        claim = next(c for c in self.service.company("MSFT")["claims"] if "固定汇率" in c["text"])
        self.assertEqual(claim["verification_status"], "qualified")
        self.assertIn("18%", claim["text"])
        self.assertIn("17%", claim["text"])
        self.assertIn("补充口径", claim["explanation"])

    def test_segment_2024_presentations_are_separate(self):
        facts = self.service.company("MSFT")["facts"]
        metric = "segment_revenue_productivity_and_business_processes"
        values = {(f["basis"], f["value"]) for f in facts if f["metric"] == metric and f["period_end"] == "2024-06-30"}
        self.assertEqual(values, {("FY2024-era presentation", "77728"), ("FY2025-page recast presentation", "106820")})
        claim = next(c for c in self.service.company("MSFT")["claims"] if "并列保留" in c["text"])
        self.assertIn("不称其为会计差错", claim["explanation"])
        self.assertNotIn("会计差错。", claim["text"])

    def test_apple_source_reported_adjusted_basis(self):
        result = self.service.company("AAPL")
        claim = result["claims"][0]
        self.assertEqual(claim["verification_status"], "source_reported")
        self.assertIn("adjusted basis", claim["text"])
        self.assertIn("未从所链接PDF核对", claim["explanation"])
        facts = {(f["metric"], f["value"], f["basis"]) for f in result["facts"]}
        self.assertTrue(any(metric == "revenue" and value == "102.5" and "source_reported" in basis for metric, value, basis in facts))
        payload = self.service.evidence(claim["claim_id"])
        self.assertTrue(any("Non-GAAP measure excluding" in item["quote"] for item in payload["evidence"]))
        self.assertTrue(all(f["period_start"] is None for f in result["facts"]))

    def test_recursive_evidence_includes_fact_evidence_and_extractions(self):
        recast = next(c for c in self.service.company("MSFT")["claims"] if "并列保留" in c["text"])
        payload = self.service.evidence(recast["claim_id"])
        self.assertEqual(len({source["source_id"] for source in payload["sources"]}), 2)
        self.assertTrue(any("77,728" in item["quote"] for item in payload["evidence"]))
        self.assertTrue(any("106,820" in item["quote"] for item in payload["evidence"]))
        self.assertTrue(all(activity["type"] == "deterministic_extraction" for activity in payload["activities"]))

        yoy = next(c for c in self.service.company("MSFT")["claims"] if "18.10%" in c["text"])
        types = [item["type"] for item in self.service.evidence(yoy["claim_id"])["activities"]]
        self.assertIn("sql_calculation", types)
        self.assertIn("deterministic_extraction", types)

    def test_every_quote_matches_derived_source_text_and_hash(self):
        with closing(sqlite3.connect(self.db)) as connection:
            rows = connection.execute("SELECT source_id,quote,start,end,source_sha256 FROM evidence").fetchall()
        for source_id, quote, start, end, digest in rows:
            source = self.service.source(source_id)
            self.assertEqual(quote, source["text"][start:end])
            self.assertEqual(digest, source["source"]["sha256"])

    def test_source_text_is_derived_and_null_date_warns(self):
        source = self.service.source("msft-fy2025-q4-segment-revenues")
        self.assertIsNone(source["source"]["published_at"])
        self.assertIn("remains null", source["warning"])
        self.assertNotIn("<script", source["text"].lower())
        self.assertGreater(len(source["tables"]), 0)

    def test_roi_claim_is_scoped_insufficient(self):
        claim = next(c for c in self.service.company("MSFT")["claims"] if c["verification_status"] == "insufficient")
        self.assertIn("本次已载入资料", claim["text"])
        self.assertIn("管理层业务观点", claim["explanation"])
        self.assertIn("不推断其他资料或全球", claim["explanation"])

    def test_wiki_is_non_llm_draft_not_approval(self):
        wiki = self.service.company("MSFT")["wiki"]
        self.assertEqual(wiki["status"], "draft")
        self.assertEqual(wiki["mode"], "rules_based_draft")
        self.assertIn("not LLM-generated", wiki["sections"][0]["body"])

    def test_report_remains_on_one_snapshot_during_atomic_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "live.sqlite3"
            replacement = root / "replacement.sqlite3"
            changed_root, changed_catalog = changed_input_fixture(root)
            build_database(CATALOG, target, ROOT)
            build_database(changed_catalog, replacement, changed_root)
            service = EvidenceService(target)
            expected = service.report("MSFT")
            replacement_hashes = EvidenceService(replacement).report("MSFT")["input_hashes"]
            original_helper = service._evidence_with_connection
            replaced = False

            def replace_mid_report(connection, claim_id):
                nonlocal replaced
                if not replaced:
                    os.replace(replacement, target)
                    replaced = True
                return original_helper(connection, claim_id)

            with mock.patch.object(
                service, "_evidence_with_connection", side_effect=replace_mid_report
            ):
                actual = service.report("MSFT")
            self.assertTrue(replaced)
            self.assertEqual(actual, expected)
            self.assertNotEqual(actual["input_hashes"], replacement_hashes)
            self.assertEqual(EvidenceService(target).report("MSFT")["input_hashes"], replacement_hashes)

    def test_validate_store_accepts_consistent_local_store_without_truth_claim(self):
        result = self.service.validate_store()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["scope"], "local_structure_and_internal_consistency")
        self.assertFalse(result["source_truthfulness_verified"])

    def test_validate_store_rejects_metadata_sha_and_quote_tampering(self):
        mutations = {
            "metadata": "UPDATE metadata SET value='other' WHERE key='producer'",
            "sha": "UPDATE sources SET sha256=? WHERE source_id=(SELECT source_id FROM sources LIMIT 1)",
            "quote": "UPDATE evidence SET quote=quote || 'x' WHERE evidence_id=(SELECT evidence_id FROM evidence LIMIT 1)",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, statement in mutations.items():
                with self.subTest(name=name):
                    db = root / f"{name}.sqlite3"
                    shutil.copy2(self.db, db)
                    with closing(sqlite3.connect(db)) as connection:
                        with connection:
                            if name == "sha":
                                connection.execute(statement, ("0" * 64,))
                            else:
                                connection.execute(statement)
                    with self.assertRaisesRegex(RuntimeError, "producer/schema|source identity|quote/range"):
                        EvidenceService(db).validate_store()

    def test_report_has_actual_manifest_and_redistribution_limit(self):
        report = self.service.report("MSFT")
        self.assertEqual(report["status"], "unreviewed")
        self.assertTrue(report["generated_at"].endswith("Z"))
        self.assertEqual(report["manifest"]["public_redistribution"], False)
        self.assertEqual(report["manifest"]["source_text_included"], False)
        self.assertEqual(report["manifest"]["snapshot_is_approval"], False)
        catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
        expected_hashes = sum(1 for item in catalog["sources"] if item.get("artifact"))
        self.assertEqual(len(report["input_hashes"]), expected_hashes)
        self.assertTrue(any("redistribution" in item for item in report["limitations"]))
        self.assertTrue(any("historical query API" in item for item in report["limitations"]))
        self.assertGreater(len(report["sources"]), 0)
        self.assertGreater(len(report["evidence"]), 0)
        self.assertGreater(len(report["activities"]), 0)
        self.assertEqual(len(report["evidence_chains"]), len(report["claims"]))
        self.assertTrue(any(chain["source_ids"] for chain in report["evidence_chains"]))

    def test_eps_calculation_discloses_rounded_input_limit(self):
        claim = next(c for c in self.service.company("MSFT")["claims"] if "23.73%" in c["text"])
        self.assertTrue(any("已舍入的每股收益" in item for item in claim["limitations"]))
        self.assertFalse(any("金额以百万美元" in item for item in claim["limitations"]))

    def test_service_closes_every_read_connection(self):
        opened = []

        class TrackingConnection(sqlite3.Connection):
            closed = False

            def close(self):
                self.closed = True
                super().close()

        def connect():
            connection = sqlite3.connect(
                f"file:{self.db}?mode=ro", uri=True, factory=TrackingConnection
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            opened.append(connection)
            return connection

        apple_claim = self.service.company("AAPL")["claims"][0]
        with mock.patch.object(self.service, "_connect", side_effect=connect):
            self.service.overview()
            self.service.company("AAPL")
            self.service.source("aapl-fy2025-q4-press-release")
            self.service.evidence(apple_claim["claim_id"])
            self.service.report("AAPL")
        self.assertEqual(len(opened), 5)
        self.assertTrue(all(connection.closed for connection in opened))

    def test_unknown_ids_raise_keyerror(self):
        for call in (
            lambda: self.service.company("NOPE"), lambda: self.service.evidence("NOPE"),
            lambda: self.service.source("NOPE"), lambda: self.service.report("NOPE"),
        ):
            with self.assertRaises(KeyError):
                call()

    def test_build_is_idempotent_with_stable_ids(self):
        with closing(sqlite3.connect(self.db)) as connection:
            before = [row[0] for row in connection.execute("SELECT fact_id FROM facts ORDER BY fact_id")]
        build_database(CATALOG, self.db, ROOT)
        with closing(sqlite3.connect(self.db)) as connection:
            after = [row[0] for row in connection.execute("SELECT fact_id FROM facts ORDER BY fact_id")]
        self.assertEqual(before, after)
        self.assertEqual(len(after), len(set(after)))


class BuildSafetyTests(unittest.TestCase):
    @staticmethod
    def _derived_ids(db: Path) -> dict[str, set[str]]:
        with closing(sqlite3.connect(db)) as connection:
            columns = {"facts": "fact_id", "activities": "activity_id", "claims": "claim_id"}
            return {
                table: {row[0] for row in connection.execute(f"SELECT {column} FROM {table}")}
                for table, column in columns.items()
            }

    def test_changed_fixture_updates_claims_catalog_hashes_and_ids(self):
        original_msft = hashlib.sha256((ROOT / "data/raw/microsoft-fy2025-q4-press-release.html").read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            changed_root, changed_catalog = changed_input_fixture(parent)
            original_db = parent / "original.sqlite3"
            changed_db = parent / "changed.sqlite3"
            build_database(CATALOG, original_db, ROOT)
            build_database(changed_catalog, changed_db, changed_root)

            changed_service = EvidenceService(changed_db)
            msft = next(c for c in changed_service.company("MSFT")["claims"] if "固定汇率" in c["text"])
            self.assertIn("19%", msft["text"])
            self.assertIn("16%", msft["text"])
            self.assertNotIn("18%", msft["text"])
            apple = changed_service.company("AAPL")["claims"][0]
            for value in ("103.7", "1.91", "9%", "14%"):
                self.assertIn(value, apple["text"])
            self.assertEqual(
                {fact["value"] for fact in changed_service.company("AAPL")["facts"]},
                {"103.7", "1.91"},
            )
            original_ids = self._derived_ids(original_db)
            changed_ids = self._derived_ids(changed_db)
            for table in original_ids:
                self.assertTrue(original_ids[table].isdisjoint(changed_ids[table]))
        self.assertEqual(
            hashlib.sha256((ROOT / "data/raw/microsoft-fy2025-q4-press-release.html").read_bytes()).hexdigest(),
            original_msft,
        )

    def test_build_and_parser_versions_change_all_derived_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            baseline = root / "baseline.sqlite3"
            build_changed = root / "build.sqlite3"
            parser_changed = root / "parser.sqlite3"
            build_database(CATALOG, baseline, ROOT)
            with mock.patch.object(build_module, "BUILD_VERSION", "test-build/next"):
                build_database(CATALOG, build_changed, ROOT)
            with mock.patch.object(build_module, "PARSER_VERSION", "test-parser/next"):
                build_database(CATALOG, parser_changed, ROOT)
            baseline_ids = self._derived_ids(baseline)
            for candidate in (build_changed, parser_changed):
                changed_ids = self._derived_ids(candidate)
                for table in baseline_ids:
                    self.assertTrue(baseline_ids[table].isdisjoint(changed_ids[table]))

    def test_rebuild_archives_exact_prior_database_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "evidence.sqlite3"
            build_database(CATALOG, db, ROOT)
            prior = db.read_bytes()
            digest = hashlib.sha256(prior).hexdigest()
            build_database(CATALOG, db, ROOT)
            archive = db.parent / "archives" / f"{digest}.sqlite3"
            self.assertTrue(archive.exists())
            self.assertEqual(archive.read_bytes(), prior)
            self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o444)
            self.assertEqual(verify_archives(archive.parent), (archive,))
            archived_stat = archive.stat()
            build_database(CATALOG, db, ROOT)
            self.assertEqual(archive.read_bytes(), prior)
            self.assertEqual(archive.stat().st_mtime_ns, archived_stat.st_mtime_ns)

    def test_verify_archives_detects_owner_tampering_without_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "evidence.sqlite3"
            build_database(CATALOG, db, ROOT)
            build_database(CATALOG, db, ROOT)
            archive = next((db.parent / "archives").glob("*.sqlite3"))
            os.chmod(archive, 0o644)  # 0444 is not WORM; the owner can undo it.
            archive.write_bytes(archive.read_bytes() + b"tampered")
            with self.assertRaisesRegex(BuildError, "archive digest mismatch"):
                verify_archives(archive.parent)

    def test_hash_tampering_stops_before_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / "data", root / "data")
            target = root / "data/raw/apple-fy2025-q4-press-release.html"
            target.write_bytes(target.read_bytes() + b"tampered")
            db = root / "evidence.sqlite3"
            with self.assertRaisesRegex(BuildError, "integrity check failed"):
                build_database(root / "data/source-catalog.json", db, root)
            self.assertFalse(db.exists())

    def test_missing_required_source_fails(self):
        catalog = json.loads(CATALOG.read_text())
        catalog["sources"] = [s for s in catalog["sources"] if s["id"] != "aapl-fy2025-q4-press-release"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "catalog.json"
            path.write_text(json.dumps(catalog))
            with self.assertRaisesRegex(BuildError, "required loaded source missing"):
                build_database(path, Path(tmp) / "out.sqlite3", ROOT)

    def test_unknown_existing_schema_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "evidence.sqlite3"
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("CREATE TABLE user_data(value TEXT)")
                connection.execute("INSERT INTO user_data VALUES('keep')")
            original = hashlib.sha256(db.read_bytes()).hexdigest()
            with self.assertRaisesRegex(BuildError, "unknown"):
                build_database(CATALOG, db, ROOT)
            self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), original)


if __name__ == "__main__":
    unittest.main()
