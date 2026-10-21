import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from workbench.documents import DocumentError, DocumentService
from workbench.parser import ParsedDocument, ParseError


def pdf_with_text(pages):
    objects = []

    def add(value):
        objects.append(value.encode("latin-1"))

    kids = []
    for index, text in enumerate(pages):
        page_id = 4 + index * 2
        content_id = 5 + index * 2
        kids.append(f"{page_id} 0 R")
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET"
        add(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        )
        add(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
    fixed = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode("latin-1"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    all_objects = [None, *fixed, *objects]
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number in range(1, len(all_objects)):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(all_objects[number])
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(all_objects)}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(all_objects)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


class DocumentServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.root = self.base / "documents"
        self.service = DocumentService(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def source(self, name, body):
        path = self.base / name
        path.write_bytes(body)
        return path

    def html(self, text, extra=""):
        return f"<!doctype html><html><body>{text}{extra}</body></html>".encode()

    def register(self, name="source.html", text="Alpha disclosure", **overrides):
        path = self.source(name, self.html(text))
        values = {"company_id": "MSFT", "source_key": "filing", "title": "Filing"}
        values.update(overrides)
        return self.service.register_file(path, **values)

    def error_code(self, expected, callable_):
        with self.assertRaises(DocumentError) as caught:
            callable_()
        self.assertEqual(caught.exception.code, expected)

    def test_registration_duplicate_and_metadata_revision_are_append_only(self):
        path = self.source("annual.html", self.html("Revenue was stable."))
        options = dict(
            company_id="BRK.B:US_1-A", source_key="annual-report", title="Annual report",
            url="https://example.test/annual", published_at=None, collected_at=None,
        )
        first = self.service.register_file(path, **options)
        duplicate = self.service.register_file(path, **options)
        self.assertEqual(first, duplicate)
        self.assertEqual(first["source_version"], 1)
        self.assertIsNone(first["published_at"])
        self.assertIsNone(first["collected_at"])

        changed = self.service.register_file(path, **{**options, "published_at": "2025-08-01"})
        self.assertEqual(changed["source_version"], 2)
        self.assertEqual(changed["supersedes_document_version_id"], first["document_version_id"])
        self.assertNotEqual(changed["document_version_id"], first["document_version_id"])
        self.assertEqual(changed["source_sha256"], first["source_sha256"])

        connection = sqlite3.connect(self.root / "catalog.sqlite3")
        try:
            self.assertEqual(connection.execute("SELECT count(*) FROM document_versions").fetchone()[0], 2)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE document_versions SET title='lost' WHERE document_version_id=?",
                    (first["document_version_id"],),
                )
        finally:
            connection.close()

    def test_content_revision_and_current_default_with_explicit_old_version(self):
        old = self.register(text="LegacyOnlyToken appeared.")
        new_path = self.source("new.html", self.html("CurrentOnlyToken appeared."))
        new = self.service.register_file(
            new_path, company_id="MSFT", source_key="filing", title="Replacement"
        )
        self.assertEqual(new["source_version"], 2)
        current = self.service.search_documents(company_id="MSFT", query="LegacyOnlyToken")
        self.assertEqual(current["results"], [])
        self.assertEqual(current["version_scope"], "current_per_source")
        self.assertIsNone(current["as_of"])
        self.assertIn("does not mean", current["interpretation"])

        selected = self.service.search_documents(
            company_id="MSFT", query="LegacyOnlyToken",
            document_version_ids=[old["document_version_id"]],
        )
        self.assertEqual(len(selected["results"]), 1)
        self.assertEqual(selected["results"][0]["document_version_id"], old["document_version_id"])
        self.assertEqual(selected["version_scope"], "explicit")

    def test_object_tamper_is_detected_before_reuse_and_reads(self):
        record = self.register()
        object_path = self.root / "objects" / record["source_sha256"]
        object_path.write_bytes(b"tampered")
        self.error_code(
            "object_tampered",
            lambda: self.service.read_raw_version(
                company_id="MSFT", document_version_id=record["document_version_id"]
            ),
        )
        same = self.source("same.html", self.html("Alpha disclosure"))
        self.error_code(
            "object_tampered",
            lambda: self.service.register_file(
                same, company_id="MSFT", source_key="other", title="Other"
            ),
        )
        self.error_code(
            "object_tampered", lambda: self.service.get_document_coverage(company_id="MSFT")
        )

    def test_unknown_existing_schema_is_rejected_without_writing_it(self):
        alien_root = self.base / "alien"
        alien_root.mkdir()
        database = alien_root / "catalog.sqlite3"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE evidence(secret TEXT)")
        connection.execute("INSERT INTO evidence VALUES('keep')")
        connection.commit()
        connection.close()

        self.error_code("unknown_catalog_schema", lambda: DocumentService(alien_root))
        connection = sqlite3.connect(database)
        try:
            self.assertEqual(connection.execute("SELECT secret FROM evidence").fetchone()[0], "keep")
            names = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )}
            self.assertEqual(names, {"evidence"})
        finally:
            connection.close()

    def test_input_path_type_size_signature_and_symlink_boundaries(self):
        target = self.source("target.html", self.html("safe"))
        link = self.base / "link.html"
        link.symlink_to(target)
        self.error_code(
            "unsafe_source_path",
            lambda: self.service.register_file(
                link, company_id="MSFT", source_key="link", title="Link"
            ),
        )
        invalid = self.source("invalid.html", b"<html>\xff</html>")
        self.error_code(
            "invalid_source_type",
            lambda: self.service.register_file(
                invalid, company_id="MSFT", source_key="bad", title="Bad"
            ),
        )
        pdf = self.source("typed.pdf", b"%PDF-1.7\n%%EOF")
        self.error_code(
            "media_type_mismatch",
            lambda: self.service.register_file(
                pdf, company_id="MSFT", source_key="typed", title="Typed", media_type="text/html"
            ),
        )
        oversized = self.base / "large.pdf"
        with oversized.open("wb") as handle:
            handle.seek(20 * 1024 * 1024)
            handle.write(b"x")
        self.error_code(
            "source_too_large",
            lambda: self.service.register_file(
                oversized, company_id="MSFT", source_key="large", title="Large"
            ),
        )
        self.error_code(
            "invalid_company_id",
            lambda: self.service.register_file(
                target, company_id="msft", source_key="bad-company", title="Bad"
            ),
        )

        outside = self.base / "outside-objects"
        outside.mkdir()
        self.service.objects_path.rmdir()
        self.service.objects_path.symlink_to(outside, target_is_directory=True)
        self.error_code(
            "object_tampered",
            lambda: self.service.register_file(
                target, company_id="MSFT", source_key="escape", title="Escape"
            ),
        )
        self.assertEqual(list(outside.iterdir()), [])

    def test_pdf_text_is_parsed_with_page_locator_but_no_fake_tables(self):
        path = self.source("report.pdf", pdf_with_text([
            "PdfSearchAnchor first page disclosure",
            "SecondPageAnchor page two disclosure",
        ]))
        record = self.service.register_file(
            path, company_id="MSFT", source_key="pdf", title="PDF report"
        )
        self.assertEqual(record["parse_status"], "parsed", record.get("parse_error"))
        coverage = self.service.get_document_coverage(company_id="MSFT")
        self.assertEqual(
            {key: coverage[key] for key in ("enrolled", "parsed", "failed", "unsupported")},
            {"enrolled": 1, "parsed": 1, "failed": 0, "unsupported": 0},
        )
        result = self.service.search_documents(company_id="MSFT", query="SecondPageAnchor")["results"][0]
        self.assertEqual(result["locator"]["page"], 2)
        self.assertEqual(result["media_type"], "application/pdf")
        self.assertEqual(result["tables"], [])
        read = self.service.read_passage(company_id="MSFT", passage_id=result["passage_id"])
        self.assertEqual(read["locator"]["page"], 2)
        self.assertIn("SecondPageAnchor", read["context"])
        frozen = self.service.read_frozen_version(
            company_id="MSFT", document_version_id=record["document_version_id"]
        )
        self.assertIn("PdfSearchAnchor", frozen["text"])
        self.assertEqual(frozen["parser_version"], coverage["documents"][0]["parser_version"])
        self.assertEqual(frozen["tables"], [])

    def test_frozen_table_positions_are_json_arrays_without_hash_change(self):
        record = self.register(text="<table><tr><td>Metric</td><td>Value</td></tr>"
                                    "<tr><td>Measure</td><td>7</td></tr></table>")
        frozen = self.service.read_frozen_version(
            company_id="MSFT", document_version_id=record["document_version_id"])
        table = frozen["tables"][0]
        self.assertTrue(table["row_spans"])
        self.assertTrue(all(type(span) is list for span in table["row_spans"]))
        self.assertEqual(table, json.loads(json.dumps(table)))
        legacy = {key: value for key, value in table.items() if key != "table_sha256"}
        legacy["row_spans"] = [tuple(span) for span in table["row_spans"]]
        self.assertEqual(table["table_sha256"], hashlib.sha256(json.dumps(
            legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest())

    def test_frozen_replay_ignores_changed_enrollment_dispatcher(self):
        record = self.register(text="PersistedParserAnchor")
        expected = self.service.read_frozen_version(
            company_id="MSFT", document_version_id=record["document_version_id"]
        )
        future_default = ParsedDocument(
            text="today's different interpretation", tables=[], parser_version="future-default/2"
        )
        with mock.patch.object(DocumentService, "_parse_document", return_value=future_default):
            replayed = self.service.read_frozen_version(
                company_id="MSFT", document_version_id=record["document_version_id"]
            )
        self.assertEqual(replayed, expected)
        self.assertIn("PersistedParserAnchor", replayed["text"])

    def test_frozen_replay_rejects_unknown_or_changed_parser_identity(self):
        record = self.register(text="HistoricalParserAnchor")
        version_id = record["document_version_id"]
        with mock.patch(
            "workbench.documents.parse_html",
            return_value=ParsedDocument(
                text="changed interpretation", tables=[], parser_version="visible-html-tables/2.0.0"
            ),
        ):
            self.error_code(
                "historical_parser_unavailable",
                lambda: self.service.read_frozen_version(
                    company_id="MSFT", document_version_id=version_id
                ),
            )

        connection = sqlite3.connect(self.root / "catalog.sqlite3")
        try:
            connection.execute(
                "UPDATE parsed_documents SET parser_version=? WHERE document_version_id=?",
                ("unknown-historical-parser/1", version_id),
            )
            connection.commit()
        finally:
            connection.close()
        self.error_code(
            "historical_parser_unsupported",
            lambda: self.service.read_frozen_version(
                company_id="MSFT", document_version_id=version_id
            ),
        )

    def test_frozen_replay_scope_reuses_parse_but_rechecks_bytes_and_expires(self):
        record = self.register(text="RequestScopedReplayAnchor")
        arguments = {"company_id": "MSFT", "document_version_id": record["document_version_id"]}
        with (mock.patch.object(
                self.service, "_parse_frozen_document",
                wraps=self.service._parse_frozen_document,
              ) as parse,
              mock.patch.object(
                self.service, "_verify_object", wraps=self.service._verify_object,
              ) as verify):
            with self.service.frozen_replay_scope():
                first = self.service.read_frozen_version(**arguments)
                second = self.service.read_frozen_version(**arguments)
                second["tables"].append({"mutated": True})
                third = self.service.read_frozen_version(**arguments)
            outside = self.service.read_frozen_version(**arguments)

        self.assertEqual(parse.call_count, 2)
        self.assertEqual(verify.call_count, 4)
        self.assertEqual(first, third)
        self.assertEqual(first, outside)
        self.assertNotEqual(second, third)

    def test_frozen_replay_scope_never_hides_raw_object_tampering(self):
        record = self.register(text="RequestScopedTamperAnchor")
        arguments = {"company_id": "MSFT", "document_version_id": record["document_version_id"]}
        object_path = self.root / "objects" / record["source_sha256"]
        with self.service.frozen_replay_scope():
            self.service.read_frozen_version(**arguments)
            raw = object_path.read_bytes()
            object_path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
            self.error_code(
                "object_tampered",
                lambda: self.service.read_frozen_version(**arguments),
            )

    def test_existing_unsupported_pdf_gets_new_parsed_version_without_rewriting_history(self):
        raw = pdf_with_text(["LegacyPdfAnchor"])
        path = self.source("legacy.pdf", raw)
        with mock.patch.object(DocumentService, "_parse_document", side_effect=ParseError("old parser did not support PDF")):
            old = self.service.register_file(path, company_id="MSFT", source_key="pdf", title="PDF report")
        self.assertEqual(old["parse_status"], "parse_failed")
        new = self.service.register_file(path, company_id="MSFT", source_key="pdf", title="PDF report")
        self.assertEqual(new["parse_status"], "parsed")
        self.assertEqual(new["source_version"], old["source_version"] + 1)
        self.assertEqual(new["supersedes_document_version_id"], old["document_version_id"])

    def test_pdf_without_extractable_text_fails_explicitly(self):
        path = self.source("report.pdf", b"%PDF-1.7\n1 0 obj\n(fake text)\nendobj\n%%EOF")
        record = self.service.register_file(
            path, company_id="MSFT", source_key="pdf", title="PDF report"
        )
        self.assertEqual(record["parse_status"], "parse_failed")
        self.assertTrue(record["parse_error"])
        self.assertEqual(self.service.search_documents(company_id="MSFT", query="fake text")["results"], [])

    def test_nested_table_parse_failure_preserves_verified_raw(self):
        raw = self.html(
            "Before<table><tr><td>outer<table><tr><td>inner</td></tr></table></td></tr></table>"
        )
        path = self.source("nested.html", raw)
        record = self.service.register_file(
            path, company_id="MSFT", source_key="nested", title="Nested"
        )
        self.assertEqual(record["parse_status"], "parse_failed")
        self.assertIn("nested tables", record["parse_error"])
        self.assertEqual(
            self.service.read_raw_version(
                company_id="MSFT", document_version_id=record["document_version_id"]
            ), raw,
        )
        self.error_code(
            "historical_parser_missing",
            lambda: self.service.read_frozen_version(
                company_id="MSFT", document_version_id=record["document_version_id"]
            ),
        )
        coverage = self.service.get_document_coverage(company_id="MSFT")
        self.assertEqual((coverage["enrolled"], coverage["failed"]), (1, 1))

    def test_fts_escaping_alias_expansion_and_cross_company_isolation(self):
        microsoft = self.register(text="The company repurchased shares and paid dividends.")
        apple_path = self.source("apple.html", self.html("SecretCrossCompanyToken and a buyback."))
        apple = self.service.register_file(
            apple_path, company_id="AAPL", source_key="release", title="Apple release"
        )
        injected = self.service.search_documents(company_id="MSFT", query='" OR *')
        self.assertIsInstance(injected["results"], list)

        buyback = self.service.search_documents(company_id="MSFT", query="回购")
        self.assertEqual(buyback["results"][0]["document_version_id"], microsoft["document_version_id"])
        self.assertIn("repurchased", buyback["expanded_query"]["terms"])
        dividends = self.service.search_documents(company_id="MSFT", query="分红")
        self.assertEqual(len(dividends["results"]), 1)
        self.assertIn("dividends", dividends["expanded_query"]["terms"])

        self.assertEqual(
            self.service.search_documents(company_id="MSFT", query="SecretCrossCompanyToken")["results"],
            [],
        )
        passage = self.service.search_documents(company_id="AAPL", query="SecretCrossCompanyToken")[
            "results"
        ][0]
        self.assertEqual(passage["document_version_id"], apple["document_version_id"])
        self.error_code(
            "passage_not_found",
            lambda: self.service.read_passage(
                company_id="MSFT", passage_id=passage["passage_id"]
            ),
        )
        self.error_code(
            "document_not_found",
            lambda: self.service.search_documents(
                company_id="MSFT", query="buyback",
                document_version_ids=[apple["document_version_id"]],
            ),
        )

    def test_unicode_codepoint_offsets_context_chunks_and_table_metadata(self):
        prefix = "甲" * 1_150
        table = "<table><tr><th>项目</th><th>值</th></tr><tr><td>回购</td><td>十亿</td></tr></table>"
        suffix = "乙" * 1_350 + " 分红"
        path = self.source("unicode.html", self.html(prefix, table + suffix))
        record = self.service.register_file(
            path, company_id="MSFT", source_key="unicode", title="Unicode table"
        )
        result = self.service.search_documents(company_id="MSFT", query="回购")["results"][0]
        start, end = result["locator"]["start"], result["locator"]["end"]
        self.assertEqual(result["quote"], self._parsed_text(record)[start:end])
        self.assertTrue(result["tables"])
        self.assertEqual(result["tables"][0]["rows"][1], ["回购", "十亿"])

        read = self.service.read_passage(
            company_id="MSFT", passage_id=result["passage_id"], context_chars=17
        )
        self.assertEqual(read["context"][read["offset"]:read["offset"] + len(read["quote"])], read["quote"])
        self.assertLessEqual(len(read["context"]), len(read["quote"]) + 34)
        self.assertTrue(read["truncated"])

        connection = sqlite3.connect(self.root / "catalog.sqlite3")
        try:
            spans = connection.execute(
                "SELECT start_char,end_char,passage_id FROM passages WHERE document_version_id=? "
                "ORDER BY start_char", (record["document_version_id"],),
            ).fetchall()
        finally:
            connection.close()
        self.assertGreater(len(spans), 1)
        self.assertEqual(spans[0][0], 0)
        self.assertEqual(spans[-1][1], len(self._parsed_text(record)))
        self.assertTrue(all(right[0] <= left[1] for left, right in zip(spans, spans[1:])))
        for start, end, passage_id in spans:
            expected = hashlib.sha256(
                f"{record['document_version_id']}\0visible-html-tables/1.0.0\0{start}\0{end}".encode()
            ).hexdigest()
            self.assertEqual(passage_id, expected)

        english = self.service.search_documents(company_id="MSFT", query="dividends")
        self.assertTrue(english["results"])
        self.assertIn("分红", english["expanded_query"]["terms"])

    def test_passage_context_expands_to_ascii_word_boundaries_in_frozen_text(self):
        record = self.register(
            text=("prefixword " * 140) + "TargetRevenue operating income "
                 + ("suffixword " * 140)
        )
        result = self.service.search_documents(
            company_id="MSFT", query="TargetRevenue"
        )["results"][0]
        read = self.service.read_passage(
            company_id="MSFT", passage_id=result["passage_id"], context_chars=3
        )
        frozen = self._parsed_text(record)
        start, end = read["context_locator"]["start"], read["context_locator"]["end"]
        self.assertEqual(read["context"], frozen[start:end])
        self.assertEqual(
            read["context"][read["offset"]:read["offset"] + len(read["quote"])],
            read["quote"],
        )
        if start:
            self.assertFalse(frozen[start - 1].isascii() and frozen[start - 1].isalnum()
                             and frozen[start].isascii() and frozen[start].isalnum())
        if end < len(frozen):
            self.assertFalse(frozen[end - 1].isascii() and frozen[end - 1].isalnum()
                             and frozen[end].isascii() and frozen[end].isalnum())

    def test_context_amount_requires_its_own_exact_citation_candidate(self):
        exact = "returned $9.4 billion to shareholders"
        record = self.register(
            text="SearchAnchor " + "x" * 1_220 + exact + " through dividends and repurchases."
        )
        searched = self.service.search_documents(company_id="MSFT", query="SearchAnchor")
        requested = searched["results"][0]
        self.assertNotIn(exact, requested["quote"])

        read = self.service.read_passage(
            company_id="MSFT", passage_id=requested["passage_id"],
            context_chars=500, exact_quote=exact,
        )
        self.assertIn(exact, read["context"])
        self.assertIn("navigation only", read["citation_guidance"])
        selection = read["exact_citation_selection"]
        self.assertEqual(selection["status"], "matched")
        supporting = selection["occurrences"][0]["supporting_passages"]
        self.assertTrue(supporting)
        self.assertTrue(all(exact in item["quote"] for item in supporting))
        self.assertNotIn(requested["passage_id"], [item["passage_id"] for item in supporting])
        candidate = supporting[0]
        self.assertEqual(
            candidate["quote"],
            self._parsed_text(record)[candidate["locator"]["start"]:candidate["locator"]["end"]],
        )

        missing = self.service.read_passage(
            company_id="MSFT", passage_id=requested["passage_id"],
            context_chars=500, exact_quote="$8.4 billion",
        )
        self.assertEqual(missing["exact_citation_selection"]["status"], "not_found_in_context")
        self.assertEqual(missing["exact_citation_selection"]["occurrences"], [])

    def _parsed_text(self, record):
        connection = sqlite3.connect(self.root / "catalog.sqlite3")
        try:
            return connection.execute(
                "SELECT text FROM parsed_documents WHERE document_version_id=?",
                (record["document_version_id"],),
            ).fetchone()[0]
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
