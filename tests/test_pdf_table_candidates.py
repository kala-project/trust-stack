"""Synthetic XML tests only; these are not acceptance of any real source."""
import copy
import hashlib
import json
import unittest
from xml.sax.saxutils import escape

from workbench.pdf_table_candidates import (
    CandidateParseError,
    build_replay_manifest,
    parse_bbox_table_candidate,
)


SOURCE_SHA = hashlib.sha256(b"synthetic-not-a-source").hexdigest()


def word(text, x_min, x_max, y_min, y_max):
    return (f'<word xMin="{x_min}" yMin="{y_min}" xMax="{x_max}" yMax="{y_max}">'
            f'{escape(text)}</word>')


def line(words, x_min, x_max, y_min, y_max, extra=""):
    return (f'<line xMin="{x_min}" yMin="{y_min}" xMax="{x_max}" yMax="{y_max}" {extra}>'
            + "".join(words) + "</line>")


def synthetic_xml(*, repeated_year=False, ambiguous=False, out_of_bounds=False, second_page=False,
                  rotation=False, duplicate_unit=False, unit_outside=False):
    title = line([
        word("CONSOLIDATED", 40, 135, 40, 50),
        word("STATEMENTS", 140, 220, 40, 50),
    ], 40, 220, 40, 50)
    unit_y = (85, 91) if unit_outside else (52, 58)
    unit = line([
        word("(in", 40, 55, unit_y[0], unit_y[1]),
        word("millions)", 59, 105, unit_y[0], unit_y[1]),
    ], 40, 105, unit_y[0], unit_y[1])
    duplicate = ""
    if duplicate_unit:
        duplicate = line([
            word("(in", 40, 55, 59, 60), word("millions)", 59, 105, 59, 60),
        ], 40, 105, 59, 60)
    fiscal = line([
        word("Fiscal", 320, 350, 61, 67), word("Year", 354, 380, 61, 67),
        word("Ended", 384, 416, 61, 67),
    ], 320, 416, 61, 67)
    years = [
        word("2025", 320, 350, 70, 80),
        word("2024", 420, 450, 70, 80),
        word("2023", 520, 550, 70, 80),
    ]
    if repeated_year:
        years.insert(1, word("2025", 370, 400, 70, 80))
    header = line(years, 320, 550, 70, 80)
    revenue_words = [
        word("Total", 40, 72, 100, 110), word("revenues", 76, 130, 100, 110),
        word("37,895", 310, 355, 100, 110), word("34,857", 410, 455, 100, 110),
        word("31,352", 510, 555, 100, 110),
    ]
    if ambiguous:
        revenue_words.insert(3, word("99", 356, 370, 100, 110))
    revenue = line(revenue_words, 40, 555, 100, 110)
    operating = line([
        word("Operating", 40, 100, 130, 140), word("&", 104, 110, 130, 140),
        word("income", 114, 155, 130, 140),
        word("(7,205)", 305, 355, 130, 140), word("5,011", 415, 455, 130, 140),
        word("1,030", 515, 555, 130, 140),
    ], 40, 555, 130, 140)
    if out_of_bounds:
        revenue = line([
            word("Total", 40, 72, 100, 110), word("revenues", 76, 130, 100, 110),
            word("37,895", 590, 610, 100, 110),
        ], 40, 610, 100, 110)
    rotate = ' rotate="90"' if rotation else ""
    page = (f'<page width="600" height="800"{rotate}><flow><block>'
            f'{title}{unit}{duplicate}{fiscal}{header}{revenue}{operating}</block></flow></page>')
    if second_page:
        page += '<page width="600" height="800"><flow/></page>'
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><doc>' + page +
            '</doc></body></html>').encode()


def repeated_period_xml():
    lines = [
        line([word("CONSOLIDATED", 40, 135, 40, 50),
              word("STATEMENTS", 140, 220, 40, 50)], 40, 220, 40, 50),
        line([word("2026", 260, 290, 70, 80), word("2025", 340, 370, 70, 80),
              word("2026", 420, 450, 70, 80), word("2025", 500, 530, 70, 80)],
             260, 530, 70, 80),
        line([word("Total", 40, 72, 100, 110), word("revenues", 76, 130, 100, 110),
              word("11", 260, 290, 100, 110), word("10", 340, 370, 100, 110),
              word("101", 420, 450, 100, 110), word("90", 500, 530, 100, 110)],
             40, 530, 100, 110),
        line([word("Operating", 40, 100, 130, 140), word("&", 104, 110, 130, 140),
              word("income", 114, 155, 130, 140), word("(2)", 260, 290, 130, 140),
              word("1", 340, 370, 130, 140), word("(20)", 420, 450, 130, 140),
              word("10", 500, 530, 130, 140)], 40, 530, 130, 140),
    ]
    return ('<html><body><doc><page width="600" height="800"><flow><block>'
            + "".join(lines) + '</block></flow></page></doc></body></html>').encode()


def parse(xml, **overrides):
    arguments = dict(
        source_sha256=SOURCE_SHA,
        physical_page=69,
        title="CONSOLIDATED STATEMENTS",
        years=["2025", "2024"],
        row_labels=["Total revenues", "Operating & income"],
    )
    arguments.update(overrides)
    return parse_bbox_table_candidate(xml, **arguments)


class PdfTableCandidateTests(unittest.TestCase):
    def test_synthetic_normal_candidate_is_explicitly_untrusted(self):
        result = parse(synthetic_xml())
        self.assertEqual(result["candidate_status"], "candidate_not_trusted_fact")
        self.assertEqual(result["semantic_status"], "unapproved")
        self.assertEqual(result["physical_page"], 69)
        self.assertEqual(result["source_sha256"], SOURCE_SHA)
        self.assertEqual(
            [[cell["raw_text"] for cell in row["cells"]] for row in result["rows"]],
            [["37,895", "34,857"], ["(7,205)", "5,011"]],
        )

    def test_synthetic_ambiguous_numeric_assignment_is_rejected(self):
        with self.assertRaisesRegex(CandidateParseError, "exactly one numeric word"):
            parse(synthetic_xml(ambiguous=True))

    def test_separate_sign_and_malformed_accounting_numbers_are_not_dropped(self):
        original = word("37,895", 310, 355, 100, 110).encode()
        separated = word("-", 296, 303, 100, 110).encode() + original
        cases = [synthetic_xml().replace(original, separated),
                 synthetic_xml().replace(b"(7,205)", b"(7,205"),
                 synthetic_xml().replace(b"37,895", b"3,7,895")]
        for xml in cases:
            with self.subTest(xml=xml), self.assertRaisesRegex(CandidateParseError, "signs, annotations or malformed"):
                parse(xml)

    def test_rows_below_a_second_year_header_cannot_reuse_first_header(self):
        later_header = line([word("2025", 320, 350, 116, 124),
                             word("2024", 420, 450, 116, 124),
                             word("2023", 520, 550, 116, 124)], 320, 550, 116, 124).encode()
        marker = b'<line xMin="40" yMin="130"'
        xml = synthetic_xml().replace(marker, later_header + marker)
        with self.assertRaisesRegex(CandidateParseError, "row label"):
            parse(xml)

    def test_invalid_scope_types_and_nonfinite_coordinates_fail_closed(self):
        for arguments in ({"years": None}, {"years": [2025]}, {"row_labels": [{}]},
                          {"source_sha256": None}, {"physical_page": True}):
            with self.subTest(arguments=arguments), self.assertRaises(CandidateParseError):
                parse(synthetic_xml(), **arguments)
        with self.assertRaisesRegex(CandidateParseError, "finite"):
            parse(synthetic_xml().replace(b'xMin="310"', b'xMin="NaN"'))

    def test_synthetic_out_of_page_bbox_is_rejected(self):
        with self.assertRaisesRegex(CandidateParseError, "outside the physical page"):
            parse(synthetic_xml(out_of_bounds=True))

    def test_synthetic_repeated_year_is_rejected(self):
        with self.assertRaisesRegex(CandidateParseError, "repeats a year"):
            parse(synthetic_xml(repeated_year=True))

    def test_explicit_year_columns_select_only_the_requested_source_positions(self):
        xml = repeated_period_xml()
        with self.assertRaisesRegex(CandidateParseError, "repeats a year"):
            parse(xml, years=["2026", "2025"])

        result = parse(
            xml, years=["2026", "2025"], year_column_indices=[2, 3]
        )

        self.assertEqual(result["parser_version"], "pdf-table-candidates-bbox/4")
        self.assertEqual(result["year_column_indices"], [2, 3])
        self.assertEqual([column["source_column_index"] for column in result["columns"]],
                         [2, 3])
        self.assertEqual([word["text"] for word in result["year_header"]["original_words"]],
                         ["2026", "2025", "2026", "2025"])
        self.assertEqual(
            [[cell["raw_text"] for cell in row["cells"]] for row in result["rows"]],
            [["101", "90"], ["(20)", "10"]],
        )
        self.assertEqual(result["columns"][0]["original_header_word"]["bbox"]["x_min"],
                         420.0)
        self.assertEqual(result["rows"][0]["cells"][0]["bbox"]["x_min"], 420.0)

        first_group = parse(xml, years=["2026", "2025"], year_column_indices=[0, 1])
        self.assertEqual(
            [[cell["raw_text"] for cell in row["cells"]] for row in first_group["rows"]],
            [["11", "10"], ["(2)", "1"]],
        )
        self.assertEqual(first_group["rows"][0]["cells"][0]["bbox"]["x_min"], 260.0)
        self.assertNotEqual(first_group["structure_hash"], result["structure_hash"])

    def test_explicit_year_column_indices_fail_closed(self):
        cases = (
            ([3, 2], "monotonic"),
            ([2, 2], "unique"),
            ([2], "one unique"),
            ([2, True], "one unique"),
            ([1, 2], "does not contain requested year"),
            ([2, 4], "outside the year header"),
        )
        for indices, message in cases:
            with self.subTest(indices=indices), self.assertRaisesRegex(CandidateParseError, message):
                parse(repeated_period_xml(), years=["2026", "2025"],
                      year_column_indices=indices)

    def test_synthetic_original_words_and_bboxes_are_preserved(self):
        result = parse(synthetic_xml())
        row = result["rows"][1]
        self.assertEqual([item["text"] for item in row["original_label_words"]],
                         ["Operating", "&", "income"])
        self.assertEqual(row["cells"][0]["original_words"], [{
            "text": "(7,205)",
            "bbox": {"x_min": 305.0, "y_min": 130.0, "x_max": 355.0, "y_max": 140.0},
        }])
        self.assertEqual([item["text"] for item in result["year_header"]["original_words"]],
                         ["2025", "2024", "2023"])

    def test_synthetic_structure_hash_is_deterministic_and_source_bound(self):
        first = parse(synthetic_xml())
        second = parse(synthetic_xml())
        other = parse(synthetic_xml(), source_sha256=hashlib.sha256(b"other").hexdigest())
        self.assertEqual(first["structure_hash"], second["structure_hash"])
        self.assertRegex(first["structure_hash"], r"^sha256:[0-9a-f]{64}$")
        self.assertNotEqual(first["structure_hash"], other["structure_hash"])

    def test_synthetic_explicit_source_context_preserves_full_words_and_geometry(self):
        result = parse(
            synthetic_xml(),
            unit_line="(in millions)",
            fiscal_period_header="Fiscal Year Ended 2025 2024 2023",
        )
        self.assertEqual(result["parser_version"], "pdf-table-candidates-bbox/3")
        self.assertEqual(
            result["structure_hash"],
            "sha256:2844fea25060e336f711f8984531409b280c816e0e62a1fa9a13069f296fca8d",
        )
        self.assertNotIn("year_column_indices", result)
        self.assertTrue(all("source_column_index" not in column for column in result["columns"]))
        self.assertEqual(result["unit_line"]["raw_text"], "(in millions)")
        self.assertEqual(
            [word["text"] for word in result["unit_line"]["original_words"]],
            ["(in", "millions)"],
        )
        self.assertEqual(
            [line["raw_text"] for line in result["fiscal_period_header"]["original_lines"]],
            ["Fiscal Year Ended", "2025 2024 2023"],
        )
        self.assertEqual(
            result["fiscal_period_header"]["bbox"],
            {"x_min": 320.0, "y_min": 61.0, "x_max": 550.0, "y_max": 80.0},
        )
        self.assertEqual(result["page_bbox"]["x_max"], 600.0)
        self.assertEqual(result["physical_page"], 69)

    def test_synthetic_context_missing_duplicate_and_outside_region_fail_closed(self):
        with self.assertRaisesRegex(CandidateParseError, "unit_line must match exactly one"):
            parse(synthetic_xml(), unit_line="missing raw unit")
        with self.assertRaisesRegex(CandidateParseError, "unit_line must match exactly one"):
            parse(synthetic_xml(duplicate_unit=True), unit_line="(in millions)")
        with self.assertRaisesRegex(CandidateParseError, "outside the selected table"):
            parse(synthetic_xml(unit_outside=True), unit_line="(in millions)")
        with self.assertRaisesRegex(CandidateParseError, "fiscal_period_header must match exactly one"):
            parse(synthetic_xml(), fiscal_period_header="Missing fiscal header")
        with self.assertRaisesRegex(CandidateParseError, "outside the selected table"):
            parse(synthetic_xml(), fiscal_period_header="(in millions)")

    def test_synthetic_context_selector_bounds_are_strict(self):
        for arguments in (
            {"unit_line": 7},
            {"unit_line": " x"},
            {"unit_line": "x" * 501},
            {"fiscal_period_header": ""},
            {"fiscal_period_header": "Fiscal\x00Header"},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(CandidateParseError):
                parse(synthetic_xml(), **arguments)

    def test_replay_manifest_binds_selectors_profile_tools_and_has_no_amounts(self):
        xml = synthetic_xml()
        candidate = parse(
            xml,
            unit_line="(in millions)",
            fiscal_period_header="Fiscal Year Ended 2025 2024 2023",
        )
        extraction = {
            "source_sha256": SOURCE_SHA,
            "physical_page": 69,
            "executables": {
                "pdftotext": {"path": "/usr/bin/pdftotext", "sha256": "1" * 64},
                "bwrap": {"path": "/usr/bin/bwrap", "sha256": "2" * 64},
            },
            "replay_profile": {
                "profile_version": "synthetic-profile/1",
                "extractor_arguments": ["pdftotext", "-f", "69", "-l", "69", "-bbox-layout"],
                "environment": {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "other": "cleared"},
                "resource_bounds": {"network": "unshared", "memory_bytes": 123},
            },
        }
        selectors = {
            "title": "CONSOLIDATED STATEMENTS",
            "years": ["2025", "2024"],
            "row_labels": ["Total revenues", "Operating & income"],
            "unit_line": "(in millions)",
            "fiscal_period_header": "Fiscal Year Ended 2025 2024 2023",
        }
        manifest = build_replay_manifest(
            extraction=extraction,
            bbox_layout_sha256=hashlib.sha256(xml).hexdigest(),
            candidate=candidate,
            selectors=selectors,
        )
        same = build_replay_manifest(
            extraction=copy.deepcopy(extraction),
            bbox_layout_sha256=hashlib.sha256(xml).hexdigest(),
            candidate=candidate,
            selectors=copy.deepcopy(selectors),
        )
        changed_selector = copy.deepcopy(selectors)
        changed_selector["unit_line"] = None
        other_selector = build_replay_manifest(
            extraction=extraction,
            bbox_layout_sha256=hashlib.sha256(xml).hexdigest(),
            candidate=candidate,
            selectors=changed_selector,
        )
        changed_profile = copy.deepcopy(extraction)
        changed_profile["replay_profile"]["profile_version"] = "synthetic-profile/2"
        other_profile = build_replay_manifest(
            extraction=changed_profile,
            bbox_layout_sha256=hashlib.sha256(xml).hexdigest(),
            candidate=candidate,
            selectors=selectors,
        )
        self.assertEqual(manifest["manifest_identity"], same["manifest_identity"])
        self.assertEqual(
            manifest["manifest_identity"],
            "sha256:b746dda2dcbc843bbdf0b5a28a58dab6562bbafba3845e99b72aed05d250a161",
        )
        self.assertNotIn("year_column_indices", manifest["selectors"])
        self.assertNotEqual(manifest["manifest_identity"], other_selector["manifest_identity"])
        self.assertNotEqual(manifest["manifest_identity"], other_profile["manifest_identity"])
        self.assertRegex(manifest["executables"]["pdftotext"]["sha256"], r"^[0-9a-f]{64}$")
        serialized = json.dumps(manifest, sort_keys=True)
        self.assertNotIn("37,895", serialized)
        self.assertNotIn("7,205", serialized)

    def test_synthetic_missing_label_rotation_cross_page_and_dtd_are_rejected(self):
        with self.assertRaisesRegex(CandidateParseError, "row label"):
            parse(synthetic_xml(), row_labels=["Missing row"])
        with self.assertRaisesRegex(CandidateParseError, "rotated"):
            parse(synthetic_xml(rotation=True))
        with self.assertRaisesRegex(CandidateParseError, "exactly one physical page"):
            parse(synthetic_xml(second_page=True))
        unsafe = b'<!DOCTYPE x [<!ENTITY y "expanded">]><x>&y;</x>'
        with self.assertRaisesRegex(CandidateParseError, "DTD and entity"):
            parse(unsafe)
        for encoding in ("utf-16", "utf-16-le", "utf-32-be"):
            with self.subTest(encoding=encoding), self.assertRaises(CandidateParseError):
                parse(unsafe.decode().encode(encoding))


if __name__ == "__main__":
    unittest.main()
