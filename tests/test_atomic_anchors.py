from decimal import Decimal
import unittest

from workbench.answer_artifacts import AnswerArtifactService
from workbench.atomic_anchors import ANCHOR_SCHEMA_VERSION, build_candidates, utf16_offset


class AtomicAnchorTests(unittest.TestCase):
    def setUp(self):
        self.service = AnswerArtifactService(None)

    def project(self, text, sources=None, valid=None, calculations=None, question=""):
        sources = sources or {}
        valid = set(valid if valid is not None else sources)
        citation_map = {key: {"valid": key in valid} for key in set(sources) | valid}
        findings = self.service._findings(text, citation_map, {}, sources,
                                         calculations or [], {}, question)
        traces = {key: "trace-" + key[1] for key in citation_map}
        anchors, warnings = self.service._atomic_anchors(
            text, findings, citation_map, traces, sources, calculations or [])
        for anchor in anchors:
            self.assertEqual(text[anchor["start"]:anchor["end"]], anchor["text"])
        return anchors, findings, warnings

    def numbers(self, anchors):
        return [item for item in anchors if item["kind"] == "number"]

    def test_exact_local_binding_does_not_share_repeated_values_or_citations(self):
        text = "2023年营业收入为10。[passage:a] 2024年营业收入为10。[passage:b]"
        sources = {
            ("passage", "a"): {"quote": "FY2023 Revenue 10"},
            ("passage", "b"): {"quote": "FY2024 Revenue 10"},
        }
        anchors, _, _ = self.project(text, sources, question="营业收入")
        numbers = self.numbers(anchors)
        self.assertEqual(len(numbers), 2)
        self.assertEqual([[ref["id"] for ref in item["references"]] for item in numbers], [["a"], ["b"]])
        self.assertTrue(all(item["support_status"] == "supported" for item in numbers))
        self.assertNotEqual(numbers[0]["id"], numbers[1]["id"])

    def test_duplicate_value_without_dimensions_is_ambiguous(self):
        text = "该值为10。[passage:p]"
        source = {("passage", "p"): {"quote": "2023 Revenue 10; 2024 Operating income 10"}}
        anchors, _, _ = self.project(text, source)
        self.assertEqual(self.numbers(anchors)[0]["support_status"], "ambiguous")

    def test_multiple_propositions_are_independent_and_causal_claim_is_unresolved(self):
        text = "Revenue was 10. [passage:r] Profit improved because demand strengthened. [passage:d]"
        sources = {
            ("passage", "r"): {"quote": "Revenue was 10."},
            ("passage", "d"): {"quote": "Demand strengthened."},
        }
        anchors, _, _ = self.project(text, sources)
        assertions = [item for item in anchors if item["kind"] == "assertion"]
        self.assertEqual(len(assertions), 2)
        self.assertEqual([ref["id"] for ref in assertions[0]["references"]], ["r"])
        self.assertEqual(assertions[0]["classification"], "direct_statement")
        self.assertEqual(assertions[1]["classification"], "inference")
        self.assertEqual(assertions[1]["support_status"], "ambiguous")

    def test_quoted_number_is_clickable_but_year_date_and_citation_id_are_not(self):
        text = 'In FY2024 the reported value was "94" on 2024-06-30. [passage:p107]'
        anchors, _, _ = self.project(text, {("passage", "p107"): {"quote": 'reported value was "94"'}})
        self.assertEqual([item["text"] for item in self.numbers(anchors)], ["94"])

    def test_markdown_table_cells_keep_header_row_context_and_local_references(self):
        text = ("| 财年 | 营业收入（USD） | 营业利润（USD） |\n"
                "|---|---:|---:|\n"
                "| 2023 | 10 [passage:r23] | 10 [passage:o23] |\n"
                "| 2024 | 10 [passage:r24] | 12 [passage:o24] |")
        sources = {
            ("passage", "r23"): {"quote": "FY2023 Revenue USD 10"},
            ("passage", "o23"): {"quote": "FY2023 Operating income USD 10"},
            ("passage", "r24"): {"quote": "FY2024 Revenue USD 10"},
            ("passage", "o24"): {"quote": "FY2024 Operating income USD 12"},
        }
        anchors, _, _ = self.project(text, sources)
        numbers = self.numbers(anchors)
        self.assertEqual([item["text"] for item in numbers], ["10", "10", "10", "12"])
        self.assertEqual([[ref["id"] for ref in item["references"]] for item in numbers],
                         [["r23"], ["o23"], ["r24"], ["o24"]])
        self.assertTrue(all(item["evidence"][0]["table"]["column_header"] for item in numbers))

    def test_wrong_metric_flag_propagates_and_prevents_supported_status(self):
        text = "单位为百万美元。2022财年营业收入为7,321。[passage:wrong]"
        sources = {("passage", "wrong"): {
            "quote": "Year ended June 30 2022 Revenue 9,876 Operating income 7,321"}}
        anchors, findings, _ = self.project(text, sources, question="2022财年营业收入")
        self.assertEqual(findings[0]["review_flags"][0]["code"], "metric_identity_mismatch")
        number = self.numbers(anchors)[0]
        self.assertEqual(number["support_status"], "unresolved")
        self.assertEqual(number["warnings"][0]["code"], "metric_identity_mismatch")

    def test_transposed_financial_table_uses_column_metric_and_row_year(self):
        rows = [
            ["", "Twelve Months Ended June 30,", ""],
            ["($ in millions, except per share amounts)", "Revenue", "Operating Income", "Net Income", "Diluted Earnings per Share"],
            ["2024 As Reported (GAAP)", "$245,122", "$109,433", "$88,136", "$11.80"],
            ["2025 As Reported (GAAP)", "$281,724", "$128,528", "$101,832", "$13.64"],
        ]
        quote = " ".join(" ".join(row) for row in rows)
        source = {("passage", "annual"): {
            "quote": quote, "context": quote, "context_start": 0,
            "tables": [{"table_index": 1, "rows": rows, "locator": {"start": 0, "end": len(quote)}}]}}
        text = "2025财年营业收入为281,724百万美元。[passage:annual]"
        anchors, findings, _ = self.project(text, source)
        self.assertNotIn("metric_identity_mismatch", [flag["code"] for flag in findings[0]["review_flags"]])
        amount = self.numbers(anchors)[0]
        self.assertEqual(amount["support_status"], "supported")
        self.assertEqual(amount["evidence"][0]["source_cell"]["column_index"], 1)
        self.assertEqual(amount["evidence"][0]["source_cell"]["row_index"], 3)
        self.assertEqual(amount["evidence"][0]["source_cell"]["unit_header"],
                         "($ in millions, except per share amounts)")
        wrong, _, _ = self.project("2025财年营业收入为128,528百万美元。[passage:annual]", source)
        self.assertEqual(self.numbers(wrong)[0]["support_status"], "unresolved")

    def test_flattened_column_headers_do_not_assign_every_value_to_last_metric(self):
        text = "2025财年营业收入为281,724百万美元。[passage:annual]"
        source = {("passage", "annual"): {
            "quote": "Twelve Months Ended June 30, ($ in millions) Revenue Operating Income Net Income Diluted Earnings per Share 2025 As Reported (GAAP) $281,724 $128,528 $101,832 $13.64"}}
        anchors, findings, _ = self.project(text, source)
        self.assertNotIn("metric_identity_mismatch", [flag["code"] for flag in findings[0]["review_flags"]])
        self.assertNotEqual(self.numbers(anchors)[0]["support_status"], "supported")

    def test_unbacked_number_and_missing_old_reference_remain_clickable(self):
        unbacked, _, _ = self.project("Margin rose to 12% without a local source.")
        self.assertEqual(self.numbers(unbacked)[0]["support_status"], "unresolved")
        missing, _, _ = self.project("Margin was 12%. [passage:gone]", {}, valid=set())
        # Include a known-invalid citation map just as an old history would.
        citation_map = {("passage", "gone"): {"valid": False}}
        findings = self.service._findings("Margin was 12%. [passage:gone]", citation_map, {}, {})
        anchors, _ = self.service._atomic_anchors("Margin was 12%. [passage:gone]", findings,
                                                 citation_map, {}, {}, [])
        number = self.numbers(anchors)[0]
        self.assertEqual(number["support_status"], "unavailable")
        self.assertEqual(number["trace_ids"], [])

    def test_actual_calculation_only_and_raw_display_conversion(self):
        calculation = {"status": "recorded", "calculation_id": "c", "value": "10715",
                       "unit": "USD millions", "operation": "sum"}
        text = "输入94亿美元与13.15亿美元，两项合计107.15亿美元。[calculation:c]"
        anchors, _, _ = self.project(text, {}, valid={("calculation", "c")}, calculations=[calculation])
        numbers = self.numbers(anchors)
        self.assertEqual([item["support_status"] for item in numbers],
                         ["unresolved", "unresolved", "calculated"])
        number = numbers[-1]
        self.assertEqual(number["calculation_id"], "c")
        self.assertEqual(number["evidence"][0]["match"], "recorded_exact_output")

    def test_unicode_offsets_and_annotation_validation_contract(self):
        text = "😀收入为12%。"
        findings = [{"id": "f", "text": text}]
        candidates, warnings = build_candidates(text, findings, [{"start": 1, "end": 6, "text": "收入为12", "id": "a"}])
        self.assertEqual(warnings, [])
        number = next(item for item in candidates if item["kind"] == "number")
        self.assertEqual(utf16_offset(text, number["start"]), number["start"] + 1)
        _, rejected = build_candidates(text, findings, [{"start": 1, "end": 6, "text": "错误"}])
        self.assertEqual(rejected, ["assertion_annotation_0_invalid"])
        self.assertEqual(ANCHOR_SCHEMA_VERSION, "atomic-provenance/1.0")

    @staticmethod
    def structured_source(years, rows, table_index=7):
        table_start = 80
        table = {
            "table_index": table_index,
            "locator": {"start": table_start, "end": 500},
            "rows": [
                ["", "Three Months Ended June 30,", "", "Twelve Months Ended June 30,"],
                ["", str(years[0]), "", str(years[1]), "", str(years[0]), "", str(years[1])],
                ["Operations", "", "", "", "", "", "", ""],
                ["Net income", "$10", "", "$9", "", "$40", "", "$35"],
                *rows,
            ],
        }
        context = "CASH FLOWS STATEMENTS (In millions) (Unaudited) ".ljust(table_start) + "table"
        return {
            "quote": " ".join(" ".join(row) for row in rows),
            "context": context,
            "context_start": 0,
            "tables": [table],
        }

    def test_unknown_metric_cannot_be_supported_by_numeric_collision(self):
        text = ("单位为百万美元。\n\n| 财年 | Mystery metric（百万美元） |\n"
                "|---|---:|\n| FY2024 | 7,321 [passage:p] |")
        source = self.structured_source(
            (2024, 2023), [["Operating income", "$1", "", "$2", "", "$7,321", "", "$6,000"]])
        anchors, _, _ = self.project(text, {("passage", "p"): source})
        self.assertEqual(self.numbers(anchors)[0]["support_status"], "unresolved")

    def test_wrong_year_in_multi_year_table_does_not_use_quote_wide_intersection(self):
        text = ("单位为百万美元。\n\n| 财年 | 营业收入（百万美元） |\n"
                "|---|---:|\n| FY2024 | 7,321 [passage:p] |")
        source = self.structured_source(
            (2024, 2023), [["Revenue", "$1", "", "$2", "", "$9,999", "", "$7,321"]])
        anchors, _, _ = self.project(text, {("passage", "p"): source})
        self.assertEqual(self.numbers(anchors)[0]["support_status"], "unresolved")

    def test_same_amount_different_metrics_uses_exact_source_row(self):
        text = ("单位为百万美元。\n\n| 财年 | 营业收入（百万美元） |\n"
                "|---|---:|\n| FY2024 | 7,321 [passage:p] |")
        source = self.structured_source((2024, 2023), [
            ["Revenue", "$1", "", "$2", "", "$7,321", "", "$6,000"],
            ["Operating income", "$1", "", "$2", "", "$7,321", "", "$5,000"],
        ])
        number = self.numbers(self.project(text, {("passage", "p"): source})[0])[0]
        self.assertEqual(number["support_status"], "supported")
        self.assertEqual(number["evidence"][0]["source_cell"]["row_header"], "Revenue")

    def test_real_capex_cells_require_annual_headers_row_and_table_unit(self):
        text = ("单位为亿美元。\n\n| 财年 | CapEx（亿美元） |\n|---|---:|\n"
                "| FY2023 | 281.07 [passage:p24] |\n"
                "| FY2024 | 444.77 [passage:p24] |\n"
                "| FY2025 | 645.51 [passage:p25] |")
        source24 = self.structured_source((2024, 2023), [[
            "Additions to property and equipment", "(13,873)", "", "(8,943)", "", "(44,477)", "", "(28,107)"
        ]], table_index=8)
        source25 = self.structured_source((2025, 2024), [[
            "Additions to property and equipment", "(17,079)", "", "(13,873)", "", "(64,551)", "", "(44,477)"
        ]], table_index=7)
        numbers = self.numbers(self.project(text, {
            ("passage", "p24"): source24, ("passage", "p25"): source25,
        }, question="FY2023至FY2025资本支出")[0])
        self.assertEqual([item["support_status"] for item in numbers], ["supported"] * 3)
        proofs = [item["evidence"][0]["source_cell"] for item in numbers]
        self.assertEqual([item["value"] for item in proofs], ["(28,107)", "(44,477)", "(64,551)"])
        self.assertTrue(all(item["row_header"] == "Additions to property and equipment" for item in proofs))
        self.assertTrue(all("Twelve Months Ended June 30," in item["column_headers"] for item in proofs))
        self.assertTrue(all(item["unit_header"].lower() == "(in millions)" for item in proofs))

    def test_capex_does_not_match_revenue_or_operating_income_in_prose(self):
        text = "FY2024 CapEx was 7,321. [passage:p]"
        source = {("passage", "p"): {
            "quote": "FY2024 Revenue 9,999; FY2023 Operating income 7,321"}}
        number = self.numbers(self.project(text, source)[0])[0]
        self.assertEqual(number["support_status"], "unresolved")

    def test_decimal_parser_does_not_round_large_values(self):
        candidate = {"numeric_text": "12345678901234567890.123456789", "text": "12345678901234567890.123456789",
                     "context": "USD 12345678901234567890.123456789", "table_context": None}
        matched, _, details = self.service._numeric_anchor_match(
            candidate, {"quote": "USD 12345678901234567890.123456789"})
        self.assertTrue(matched)
        self.assertEqual(Decimal(details["decimal"]), Decimal(candidate["numeric_text"]))


if __name__ == "__main__":
    unittest.main()
