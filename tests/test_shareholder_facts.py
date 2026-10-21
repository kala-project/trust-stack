from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from workbench.build import build_database
from workbench.parser import ParseError, microsoft_shareholder_returns, parse_html
from workbench.research_tools import ResearchTools


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/source-catalog.json"
FY25_RAW = ROOT / "data/raw/microsoft-fy2025-q4-press-release.html"


class MicrosoftShareholderParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = parse_html(FY25_RAW.read_bytes())

    def test_gold_values_and_reported_total_are_separate(self):
        result = microsoft_shareholder_returns(self.document, 2025)
        dividends = result["rows"]["payments_of_dividends"]
        repurchases = result["rows"]["payments_for_repurchase_of_common_stock"]
        self.assertEqual((dividends["quarter"], dividends["annual"]), (6169, 24082))
        self.assertEqual((repurchases["quarter"], repurchases["annual"]), (4546, 18420))
        self.assertEqual(result["reported_total"], 9400)
        self.assertEqual(self.document.text[slice(*result["reported_total_span"])], (
            "Microsoft returned $9.4 billion to shareholders in the form of dividends and "
            "share repurchases in the fourth quarter of fiscal year 2025."
        ))

    def test_reported_total_regex_captures_amount_instead_of_hardcoding_it(self):
        changed = replace(self.document, text=self.document.text.replace(
            "Microsoft returned $9.4 billion", "Microsoft returned $7.25 billion", 1
        ))
        self.assertEqual(microsoft_shareholder_returns(changed, 2025)["reported_total"], 7250)

    def test_missing_or_ambiguous_cash_flow_table_fails_closed(self):
        cash_flow = microsoft_shareholder_returns(self.document, 2025)["table"]
        without_dividends = replace(
            cash_flow,
            rows=[row for row in cash_flow.rows if not row or row[0] != "Common stock cash dividends paid"],
            row_spans=[
                span for row, span in zip(cash_flow.rows, cash_flow.row_spans)
                if not row or row[0] != "Common stock cash dividends paid"
            ],
        )
        missing = replace(
            self.document,
            tables=[without_dividends if table is cash_flow else table for table in self.document.tables],
        )
        with self.assertRaises(ParseError):
            microsoft_shareholder_returns(missing, 2025)

        ambiguous = replace(self.document, tables=[*self.document.tables, cash_flow])
        with self.assertRaisesRegex(ParseError, "expected exactly one"):
            microsoft_shareholder_returns(ambiguous, 2025)


class BuiltShareholderFactsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_hash = hashlib.sha256(FY25_RAW.read_bytes()).hexdigest()
        cls.temp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.temp.name) / "shareholder.sqlite3"
        build_database(CATALOG, cls.db, ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_fact_count_is_60_after_adding_10_shareholder_facts(self):
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM facts").fetchone()[0], 60)

    def test_gold_amounts_periods_units_and_bases(self):
        with closing(sqlite3.connect(self.db)) as connection:
            rows = connection.execute(
                """SELECT metric,value,period_start,period_end,period_kind,unit,basis
                   FROM facts
                   WHERE metric IN (?,?,?)
                   ORDER BY metric,period_end,period_kind""",
                (
                    "payments_of_dividends",
                    "payments_for_repurchase_of_common_stock",
                    "issuer_reported_capital_return_total",
                ),
            ).fetchall()
        self.assertEqual(len(rows), 10)
        by_key = {(row[0], row[3], row[4]): row[1:] for row in rows}
        expected = {
            ("payments_of_dividends", "2024-06-30", "quarter"): ("5574", "2024-04-01"),
            ("payments_of_dividends", "2024-06-30", "year"): ("21771", "2023-07-01"),
            ("payments_of_dividends", "2025-06-30", "quarter"): ("6169", "2025-04-01"),
            ("payments_of_dividends", "2025-06-30", "year"): ("24082", "2024-07-01"),
            ("payments_for_repurchase_of_common_stock", "2024-06-30", "quarter"): ("4210", "2024-04-01"),
            ("payments_for_repurchase_of_common_stock", "2024-06-30", "year"): ("17254", "2023-07-01"),
            ("payments_for_repurchase_of_common_stock", "2025-06-30", "quarter"): ("4546", "2025-04-01"),
            ("payments_for_repurchase_of_common_stock", "2025-06-30", "year"): ("18420", "2024-07-01"),
            ("issuer_reported_capital_return_total", "2024-06-30", "quarter"): ("8400", "2024-04-01"),
            ("issuer_reported_capital_return_total", "2025-06-30", "quarter"): ("9400", "2025-04-01"),
        }
        for key, (value, period_start) in expected.items():
            row = by_key[key]
            self.assertEqual((row[0], row[1]), (value, period_start))
            self.assertEqual(row[4], "USD millions")
            if key[0] == "issuer_reported_capital_return_total":
                self.assertIn("not cash-flow basis", row[5])
            else:
                self.assertIn("cash flow statement", row[5])

    def test_requested_fact_queries_now_return_two_period_kinds_and_years(self):
        tools = ResearchTools(self.db, "MSFT")
        for metric in ("payments_of_dividends", "payments_for_repurchase_of_common_stock"):
            facts = tools.get_company_facts(metric=metric)["facts"]
            self.assertEqual(len(facts), 4)
            self.assertEqual({fact["period_kind"] for fact in facts}, {"quarter", "year"})
            self.assertEqual({fact["period_end"] for fact in facts}, {"2024-06-30", "2025-06-30"})

    def test_shareholder_fact_graph_links_and_activity_details_are_valid(self):
        with closing(sqlite3.connect(self.db)) as connection:
            connection.row_factory = sqlite3.Row
            facts = connection.execute(
                "SELECT * FROM facts WHERE metric LIKE 'payments_%' OR metric='issuer_reported_capital_return_total'"
            ).fetchall()
            claim = connection.execute(
                "SELECT * FROM claims WHERE text LIKE '%股东资本回报总额%'"
            ).fetchone()
            activities = {
                row["activity_id"]: row for row in connection.execute(
                    "SELECT * FROM activities WHERE activity_id IN (%s)" % ",".join("?" for _ in facts),
                    [row["activity_id"] for row in facts],
                )
            }
            evidence_ids = {
                row[0] for row in connection.execute("SELECT evidence_id FROM evidence")
            }
        self.assertIsNotNone(claim)
        self.assertEqual(set(json.loads(claim["fact_ids_json"])), {row["fact_id"] for row in facts})
        for fact in facts:
            self.assertIn(fact["evidence_id"], evidence_ids)
            activity = activities[fact["activity_id"]]
            self.assertIn(fact["fact_id"], json.loads(activity["outputs_json"]))
            parameters = json.loads(activity["parameters_json"])
            self.assertIn("operation", parameters)
            self.assertIn("evidence_quote", parameters)
            if fact["metric"].startswith("payments_"):
                self.assertIn("cell_locators", parameters)
                self.assertTrue(parameters["cashflow_basis"])
            else:
                self.assertFalse(parameters["cashflow_basis"])
                self.assertFalse(parameters["component_arithmetic_performed"])

    def test_original_raw_file_is_unchanged(self):
        self.assertEqual(hashlib.sha256(FY25_RAW.read_bytes()).hexdigest(), self.original_hash)


if __name__ == "__main__":
    unittest.main()
