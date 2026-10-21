import hashlib
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.support_chat_provider import ExplicitMSFTIntentAdapter
from workbench.build import build_database
from workbench.chat import ChatService
from workbench.document_facts import validate_document_fact, with_provenance
from workbench.documents import (
    DocumentError,
    DocumentService,
    PDF_PARSER_PREFIX,
    PDF_TABLE_PROFILE_VERSION,
)
from workbench.parser import ParsedDocument
from workbench.research_tools import ResearchTools, ToolError
from workbench.retrieval_budget import RetrievalBudget
from workbench.scoped_tools import snapshot_for, tools_for_scope
from workbench.session_materials import ConversationDocuments


def _word(text, x1, x2, y1, y2):
    return f'<word xMin="{x1}" yMin="{y1}" xMax="{x2}" yMax="{y2}">{text}</word>'


def _line(words, x1, x2, y1, y2):
    return f'<line xMin="{x1}" yMin="{y1}" xMax="{x2}" yMax="{y2}">{"".join(words)}</line>'


def bbox_xml(first_amount="100"):
    lines = [
        _line([_word("STATEMENT", 40, 130, 10, 20)], 40, 130, 10, 20),
        _line([_word("(in", 40, 60, 30, 40), _word("millions)", 65, 120, 30, 40)],
              40, 120, 30, 40),
        _line([_word("Fiscal", 250, 285, 45, 55), _word("Year", 290, 315, 45, 55),
               _word("Ended", 320, 355, 45, 55)], 250, 355, 45, 55),
        _line([_word("2025", 320, 350, 60, 70), _word("2024", 420, 450, 60, 70)],
              320, 450, 60, 70),
        _line([_word("Revenue", 40, 100, 100, 110), _word(first_amount, 320, 350, 100, 110),
               _word("90", 420, 445, 100, 110)], 40, 445, 100, 110),
        _line([_word("Operating", 40, 100, 120, 130), _word("income", 105, 150, 120, 130),
               _word("(20)", 320, 350, 120, 130), _word("10", 420, 445, 120, 130)],
              40, 445, 120, 130),
    ]
    return (f'<html><body><doc><page width="600" height="800"><flow><block>'
            f'{"".join(lines)}</block></flow></page></doc></body></html>').encode()


def repeated_period_bbox_xml():
    lines = [
        _line([_word("STATEMENT", 40, 130, 10, 20)], 40, 130, 10, 20),
        _line([_word("(in", 40, 60, 30, 40), _word("millions)", 65, 120, 30, 40)],
              40, 120, 30, 40),
        _line([_word("Quarter", 240, 285, 45, 55), _word("And", 290, 315, 45, 55),
               _word("Year", 320, 350, 45, 55), _word("Ended", 355, 395, 45, 55)],
              240, 395, 45, 55),
        _line([_word("2026", 260, 290, 60, 70), _word("2025", 340, 370, 60, 70),
               _word("2026", 420, 450, 60, 70), _word("2025", 500, 530, 60, 70)],
              260, 530, 60, 70),
        _line([_word("Revenue", 40, 100, 100, 110), _word("11", 260, 290, 100, 110),
               _word("10", 340, 370, 100, 110), _word("100", 420, 450, 100, 110),
               _word("90", 500, 530, 100, 110)], 40, 530, 100, 110),
        _line([_word("Operating", 40, 100, 120, 130), _word("income", 105, 150, 120, 130),
               _word("(2)", 260, 290, 120, 130), _word("1", 340, 370, 120, 130),
               _word("(20)", 420, 450, 120, 130), _word("10", 500, 530, 120, 130)],
              40, 530, 120, 130),
    ]
    return (f'<html><body><doc><page width="600" height="800"><flow><block>'
            f'{"".join(lines)}</block></flow></page></doc></body></html>').encode()


def wait_for_run(service, run_id, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = service.get_run(run_id)
        if run["status"] not in {"queued", "running"}:
            return run
        time.sleep(0.01)
    raise AssertionError("run did not finish")


class PdfDerivationProvider(ExplicitMSFTIntentAdapter):
    def status(self):
        return {"available": True, "reason": "", "provider": "pdf-derivation-fixture"}

    def stream(self, messages, tools, cancel_event):
        handlers = {item["name"]: item["handler"] for item in tools}
        catalog = handlers["discover_research_catalog"]()
        base = catalog["documents"][0]["document_version_id"]
        derived = handlers["derive_pdf_tables"](
            base_document_version_id=base, tables=profile()["tables"])
        self.version_id = derived["document_version_id"]
        refreshed = handlers["discover_research_catalog"]()
        assert self.version_id in [item["document_version_id"] for item in refreshed["documents"]]
        common = dict(
            document_version_id=self.version_id, table_index=0, header_row_indices=[0],
            row_label_column_index=0, unit="USD millions", period_start="2024-01-01",
            period_end="2024-12-31", period_kind="year",
            basis="issuer table presentation", metadata_mode="explicit_interpretation",
        )
        revenue = handlers["extract_document_table_fact"](
            **common, row_index=1, column_index=1, expected_value="100", metric="revenue")["fact"]
        income = handlers["extract_document_table_fact"](
            **common, row_index=2, column_index=1, expected_value="-20",
            metric="operating_income")["fact"]
        self.evidence_id = revenue["evidence_id"]
        calculation = handlers["calculate_facts"](
            operation="ratio", fact_ids=[income["fact_id"], revenue["fact_id"]])
        self.calculation_id = calculation["calculation_id"]
        yield {"type": "delta", "text": (
            f"Derived source value [evidence:{self.evidence_id}] and ratio "
            f"[calculation:{self.calculation_id}]"
        )}


def profile(years=None):
    return {
        "profile_version": PDF_TABLE_PROFILE_VERSION,
        "tables": [{
            "physical_page": 2,
            "title": "STATEMENT",
            "years": years or ["2025", "2024"],
            "row_labels": ["Revenue", "Operating income"],
            "unit_line": "(in millions)",
            "fiscal_period_header": "Fiscal Year Ended 2025 2024",
        }],
    }


class PdfDocumentTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.evidence_fixture = tempfile.TemporaryDirectory()
        cls.evidence_db = Path(cls.evidence_fixture.name) / "evidence.sqlite3"
        project_root = Path(__file__).resolve().parents[1]
        build_database(project_root / "data/source-catalog.json", cls.evidence_db, project_root)

    @classmethod
    def tearDownClass(cls):
        cls.evidence_fixture.cleanup()

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.service = DocumentService(self.root / "documents")
        self.raw = b"%PDF-1.7\nformal-table-fixture\n%%EOF"
        self.state = {"xml": bbox_xml(), "tool": "a"}

    def tearDown(self):
        self.temporary.cleanup()

    def text_parse(self, raw):
        return ParsedDocument(
            text="First page passage\fSecond page STATEMENT Revenue Operating income\f",
            tables=[],
            parser_version=f"{PDF_PARSER_PREFIX};pdftotext=pdftotext:1111111111111111;"
                           "bwrap=bwrap:2222222222222222",
        )

    def bbox_extract(self, raw, *, physical_page):
        tool = self.state["tool"]
        digit = "1" if tool == "a" else "3"
        return {
            "xml": self.state["xml"],
            "tool_identity": f"bbox-tool/{tool}",
            "executables": {
                "pdftotext": {"path": "/usr/bin/pdftotext", "sha256": digit * 64},
                "bwrap": {"path": "/usr/bin/bwrap", "sha256": "2" * 64},
            },
            "source_sha256": hashlib.sha256(raw).hexdigest(),
            "physical_page": physical_page,
            "replay_profile": {
                "profile_version": f"bbox-profile/{tool}",
                "extractor_arguments": ["pdftotext", "-f", str(physical_page), "-l",
                                        str(physical_page), "-bbox-layout"],
                "environment": {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "other": "cleared"},
                "resource_bounds": {"network": "unshared", "pages": 1},
            },
        }

    def register(self, raw=None, selected_profile=None):
        return self.service.register_bytes(
            self.raw if raw is None else raw,
            company_id="CRM", source_key="annual-report", title="Annual report",
            media_type="application/pdf", rights_status="isolated_candidate_only",
            pdf_table_profile=profile() if selected_profile is None else selected_profile,
        )

    def patches(self):
        return (
            mock.patch("workbench.documents._parse_pdf_text", side_effect=self.text_parse),
            mock.patch("workbench.documents.extract_bbox_layout_page", side_effect=self.bbox_extract),
        )

    def test_same_source_adds_formal_parse_version_and_preserves_full_text_and_coordinates(self):
        text_patch, bbox_patch = self.patches()
        with text_patch, bbox_patch:
            old = self.service.register_bytes(
                self.raw, company_id="CRM", source_key="annual-report", title="Annual report",
                media_type="application/pdf", rights_status="isolated_candidate_only",
            )
            formal = self.register()
            duplicate = self.register()
            frozen = self.service.read_frozen_version(
                company_id="CRM", document_version_id=formal["document_version_id"]
            )

        self.assertEqual(formal["source_version"], old["source_version"] + 1)
        self.assertEqual(formal["supersedes_document_version_id"], old["document_version_id"])
        self.assertEqual(formal, duplicate)
        self.assertEqual(old["source_sha256"], formal["source_sha256"])
        self.assertEqual(frozen["text"], self.text_parse(self.raw).text)
        self.assertEqual(frozen["tables"][0]["rows"], [
            ["", "2025", "2024"], ["Revenue", "100", "90"],
            ["Operating income", "(20)", "10"],
        ])
        provenance = frozen["tables"][0]["provenance"]
        self.assertEqual(provenance["semantic_status"], "unapproved")
        self.assertEqual(provenance["authorization_status"], "unchanged")
        self.assertEqual(provenance["coordinate_table"]["physical_page"], 2)
        self.assertEqual(frozen["parse_identity"]["profile"], profile())
        self.assertNotIn("year_column_indices", provenance["manifest"]["selectors"])
        self.assertNotIn("year_column_indices", provenance["coordinate_table"])
        self.assertEqual(provenance["coordinate_table"]["unit_line"]["raw_text"], "(in millions)")
        self.assertEqual(
            provenance["coordinate_table"]["rows"][0]["cells"][0]["original_words"][0]["text"],
            "100",
        )
        old_connection = sqlite3.connect(self.root / "documents" / "catalog.sqlite3")
        try:
            self.assertEqual(old_connection.execute(
                "SELECT count(*) FROM parsed_tables WHERE document_version_id=?",
                (old["document_version_id"],),
            ).fetchone()[0], 0)
        finally:
            old_connection.close()

    def test_derivation_copies_metadata_preserves_tables_and_is_idempotent(self):
        text_patch, bbox_patch = self.patches()
        with text_patch, bbox_patch:
            original = self.service.register_bytes(
                self.raw, company_id="CRM", source_key="annual-report", title="Annual report",
                url="https://example.test/report.pdf", published_at="2025-03-01",
                media_type="application/pdf", rights_status="isolated_candidate_only",
                collected_at="2025-03-02T00:00:00Z",
            )
            formal = self.service.derive_pdf_tables(
                company_id="CRM", base_document_version_id=original["document_version_id"],
                table_selectors=profile()["tables"],
            )
            subset = {**profile()["tables"][0], "row_labels": ["Revenue"]}
            combined = self.service.derive_pdf_tables(
                company_id="CRM", base_document_version_id=formal["document_version_id"],
                table_selectors=[subset, subset],
            )
            duplicate = self.service.derive_pdf_tables(
                company_id="CRM", base_document_version_id=combined["document_version_id"],
                table_selectors=[subset],
            )
            old_frozen = self.service.read_frozen_version(
                company_id="CRM", document_version_id=formal["document_version_id"])
            combined_frozen = self.service.read_frozen_version(
                company_id="CRM", document_version_id=combined["document_version_id"])

        self.assertEqual(duplicate, combined)
        self.assertEqual(len(old_frozen["tables"]), 1)
        self.assertEqual(len(combined_frozen["tables"]), 2)
        self.assertEqual(combined_frozen["tables"][0]["rows"], old_frozen["tables"][0]["rows"])
        self.assertEqual(combined_frozen["tables"][1]["rows"], [
            ["", "2025", "2024"], ["Revenue", "100", "90"],
        ])
        for key in ("source_sha256", "title", "source_url", "published_at", "media_type",
                    "rights_status", "collected_at"):
            self.assertEqual(combined_frozen[key], old_frozen[key])
        self.assertEqual(
            self.service.read_raw_version(
                company_id="CRM", document_version_id=original["document_version_id"]),
            self.raw,
        )

    def test_repeated_year_columns_derive_fact_and_frozen_replay_from_selected_group(self):
        self.state["xml"] = repeated_period_bbox_xml()
        selector = {
            "physical_page": 2,
            "title": "STATEMENT",
            "years": ["2026", "2025"],
            "year_column_indices": [2, 3],
            "row_labels": ["Revenue", "Operating income"],
            "unit_line": "(in millions)",
            "fiscal_period_header": "Quarter And Year Ended 2026 2025 2026 2025",
        }
        text_patch, bbox_patch = self.patches()
        with text_patch, bbox_patch:
            base = self.service.register_bytes(
                self.raw, company_id="CRM", source_key="annual-report", title="Annual report",
                media_type="application/pdf", rights_status="isolated_candidate_only",
            )
            derived = self.service.derive_pdf_tables(
                company_id="CRM", base_document_version_id=base["document_version_id"],
                table_selectors=[selector],
            )
            frozen = self.service.read_frozen_version(
                company_id="CRM", document_version_id=derived["document_version_id"])
            table = self.service.read_table(
                company_id="CRM", document_version_id=derived["document_version_id"],
                table_index=0,
            )
            tools = ResearchTools(self.evidence_db, "CRM", self.service)
            result = tools.extract_document_table_fact(
                document_version_id=derived["document_version_id"], table_index=0,
                row_index=1, column_index=1, header_row_indices=[0],
                row_label_column_index=0, expected_value="100", metric="revenue",
                unit="USD millions", period_start="2025-02-01", period_end="2026-01-31",
                period_kind="year", basis="issuer table presentation",
                metadata_mode="explicit_interpretation",
            )
            descriptor = next(item for item in tools.document_coverage["documents"]
                              if item["document_version_id"] == derived["document_version_id"])
            recorded = with_provenance(result, descriptor)
            snapshot = {
                "authoritative": True, "truncated": False,
                "documents": {"coverage": tools.document_coverage,
                              "document_version_ids": list(tools.document_version_ids)},
            }
            replayed, summary = validate_document_fact(
                snapshot=snapshot, company_id="CRM", documents=self.service,
                result=recorded, expected_fact=result["fact"],
            )

        self.assertEqual(frozen["tables"][0]["rows"], [
            ["", "2026", "2025"], ["Revenue", "100", "90"],
            ["Operating income", "(20)", "10"],
        ])
        provenance = table["provenance"]
        coordinate = provenance["coordinate_table"]
        self.assertEqual(coordinate["parser_version"], "pdf-table-candidates-bbox/4")
        self.assertEqual(coordinate["year_column_indices"], [2, 3])
        self.assertEqual([item["text"] for item in coordinate["year_header"]["original_words"]],
                         ["2026", "2025", "2026", "2025"])
        self.assertEqual(provenance["manifest"]["selectors"]["year_column_indices"], [2, 3])
        self.assertEqual(frozen["parse_identity"]["profile"]["tables"][0], selector)
        target = result["fact"]["locator"]["pdf_provenance"]["target"]
        self.assertEqual(target["header"]["source_column_index"], 2)
        self.assertEqual(target["cell"]["bbox"]["x_min"], 420.0)
        self.assertEqual(replayed, result["fact"])
        self.assertEqual(summary["table"]["provenance"]["target"]["header"]
                         ["source_column_index"], 2)

    def test_derivation_rejects_foreign_non_pdf_and_failed_selectors_without_append(self):
        text_patch, bbox_patch = self.patches()
        with text_patch, bbox_patch:
            base = self.service.register_bytes(
                self.raw, company_id="CRM", source_key="annual-report", title="Annual report",
                media_type="application/pdf",
            )
            html = self.service.register_bytes(
                b"<html><body>not pdf</body></html>", company_id="CRM",
                source_key="web", title="Web", media_type="text/html",
            )
            with self.assertRaisesRegex(DocumentError, "does not exist"):
                self.service.derive_pdf_tables(
                    company_id="MSFT", base_document_version_id=base["document_version_id"],
                    table_selectors=profile()["tables"],
                )
            with self.assertRaisesRegex(DocumentError, "only from a PDF"):
                self.service.derive_pdf_tables(
                    company_id="CRM", base_document_version_id=html["document_version_id"],
                    table_selectors=profile()["tables"],
                )
            with closing(sqlite3.connect(self.root / "documents" / "catalog.sqlite3")) as connection:
                before = connection.execute(
                    "SELECT count(*) FROM document_versions"
                ).fetchone()[0]
            invalid = [{**profile()["tables"][0], "title": "ABSENT TABLE"}]
            with self.assertRaisesRegex(DocumentError, "no document version was appended") as failed:
                self.service.derive_pdf_tables(
                    company_id="CRM", base_document_version_id=base["document_version_id"],
                    table_selectors=invalid,
                )
            self.assertIn("requested title must match exactly one line", failed.exception.message)
            self.assertNotIn(str(self.root), failed.exception.message)
            with closing(sqlite3.connect(self.root / "documents" / "catalog.sqlite3")) as connection:
                after = connection.execute(
                    "SELECT count(*) FROM document_versions"
                ).fetchone()[0]
        self.assertEqual(after, before)

    def test_partial_fiscal_header_has_actionable_diagnostic_without_relaxing_match(self):
        text_patch, bbox_patch = self.patches()
        with text_patch, bbox_patch:
            base = self.service.register_bytes(
                self.raw, company_id="CRM", source_key="annual-report", title="Annual report",
                media_type="application/pdf",
            )
            selected = {**profile()["tables"][0], "fiscal_period_header": "Fiscal Year Ended"}
            with self.assertRaises(DocumentError) as failed:
                self.service.derive_pdf_tables(
                    company_id="CRM", base_document_version_id=base["document_version_id"],
                    table_selectors=[selected],
                )
            self.assertEqual(failed.exception.code, "pdf_table_parse_failed")
            self.assertIn("fiscal_period_header", failed.exception.message)
            self.assertIn("selected table header region", failed.exception.message)
            complete = self.service.derive_pdf_tables(
                company_id="CRM", base_document_version_id=base["document_version_id"],
                table_selectors=profile()["tables"],
            )
            self.assertEqual(complete["parse_status"], "parsed")

    def test_derived_version_requires_grant_then_supports_fact_calculation_and_frozen_replay(self):
        text_patch, bbox_patch = self.patches()
        with text_patch, bbox_patch:
            base = self.service.register_bytes(
                self.raw, company_id="MSFT", source_key="annual-report", title="Annual report",
                media_type="application/pdf", rights_status="isolated_candidate_only",
            )
            tools = ResearchTools(self.evidence_db, "MSFT", self.service)
            foreign_tools = ResearchTools(self.evidence_db, "CRM", self.service)
            granted_events = []

            def grant(event):
                descriptor = tools.prepare_document_addition(event["document"])
                tools.add_document(descriptor)
                granted_events.append(event)
                return {"status": "granted"}

            self.assertTrue(tools.enable_document_derivation(grant))
            derive_definition = next(item for item in tools.definitions()
                                     if item["name"] == "derive_pdf_tables")
            index_schema = derive_definition["parameters"]["properties"]["tables"]["items"] \
                ["properties"]["year_column_indices"]
            self.assertEqual(index_schema["items"], {"type": "integer", "minimum": 0})
            self.assertTrue(index_schema["uniqueItems"])
            self.assertIn("strictly increasing 0-based positions", index_schema["description"])
            with self.assertRaisesRegex(ToolError, "outside this run"):
                tools.derive_pdf_tables(
                    base_document_version_id="0" * 64, tables=profile()["tables"])
            self.assertTrue(foreign_tools.enable_document_derivation(lambda event: None))
            with self.assertRaisesRegex(ToolError, "outside this run"):
                foreign_tools.derive_pdf_tables(
                    base_document_version_id=base["document_version_id"],
                    tables=profile()["tables"],
                )

            derived = tools.derive_pdf_tables(
                base_document_version_id=base["document_version_id"],
                tables=profile()["tables"],
            )
            version_id = derived["document_version_id"]
            self.assertEqual(len(granted_events), 1)
            read = tools.read_document_table(document_version_id=version_id, table_index=0)
            common = dict(
                document_version_id=version_id, table_index=0, header_row_indices=[0],
                row_label_column_index=0, unit="USD millions", period_start="2024-01-01",
                period_end="2024-12-31", period_kind="year",
                basis="issuer table presentation", metadata_mode="explicit_interpretation",
            )
            revenue = tools.extract_document_table_fact(
                **common, row_index=1, column_index=1, expected_value="100", metric="revenue")
            income = tools.extract_document_table_fact(
                **common, row_index=2, column_index=1, expected_value="-20",
                metric="operating_income")
            calculation = tools.calculate_facts(
                operation="ratio", fact_ids=[income["fact"]["fact_id"], revenue["fact"]["fact_id"]])
            descriptor = next(item for item in tools.document_coverage["documents"]
                              if item["document_version_id"] == version_id)
            recorded = with_provenance(revenue, descriptor)
            snapshot = {
                "authoritative": True, "truncated": False,
                "documents": {"coverage": tools.document_coverage,
                              "document_version_ids": list(tools.document_version_ids)},
            }
            replayed, _summary = validate_document_fact(
                snapshot=snapshot, company_id="MSFT", documents=self.service,
                result=recorded, expected_fact=revenue["fact"],
            )

        self.assertEqual(read["rows"][1]["cells"][1]["numeric_candidate"]["value"], "100")
        self.assertEqual(calculation["value"], "-0.2")
        self.assertEqual(replayed, revenue["fact"])
        self.assertIn(version_id, tools.document_version_ids)

    def test_chat_run_grants_derived_version_and_freezes_fact_and_calculation_refs(self):
        text_patch, bbox_patch = self.patches()
        with text_patch, bbox_patch:
            base = self.service.register_bytes(
                self.raw, company_id="MSFT", source_key="annual-report", title="Annual report",
                media_type="application/pdf", rights_status="isolated_candidate_only",
            )
            provider = PdfDerivationProvider()
            chat = ChatService(
                self.evidence_db, self.root / "chat.sqlite3", provider,
                document_service=self.service,
            )
            try:
                conversation = chat.create_conversation("MSFT")
                run_id = chat.submit(
                    conversation["conversation_id"], "Use the loaded PDF source"
                )["run_id"]
                run = wait_for_run(chat, run_id)
                self.assertEqual(run["status"], "completed", run.get("error"))
                message = chat.get_conversation(conversation["conversation_id"])["messages"][-1]
                answer = chat.answer_context(conversation["conversation_id"], message["message_id"])
                fact = chat.read_citation(
                    conversation["conversation_id"], message["message_id"],
                    "evidence", provider.evidence_id,
                )
                calculation = chat.read_citation(
                    conversation["conversation_id"], message["message_id"],
                    "calculation", provider.calculation_id,
                )
            finally:
                chat.close()

        self.assertNotEqual(provider.version_id, base["document_version_id"])
        self.assertIn(provider.version_id, answer["source_snapshot"]["documents"]["document_version_ids"])
        self.assertEqual([item["type"] for item in answer["source_snapshot"]["derivations"]],
                         ["data_snapshot", "data_snapshot_addition"])
        self.assertEqual(fact["document_version_id"], provider.version_id)
        self.assertEqual(calculation["value"], "-0.2")

    def test_callback_failure_does_not_grant_derived_scope(self):
        text_patch, bbox_patch = self.patches()
        with text_patch, bbox_patch:
            base = self.service.register_bytes(
                self.raw, company_id="MSFT", source_key="annual-report", title="Annual report",
                media_type="application/pdf",
            )
            tools = ResearchTools(self.evidence_db, "MSFT", self.service)
            before = tools.document_version_ids

            def cancelled(_event):
                raise ToolError("cancelled", "run cancellation requested")

            tools.enable_document_derivation(cancelled)
            with self.assertRaisesRegex(ToolError, "cancellation"):
                tools.derive_pdf_tables(
                    base_document_version_id=base["document_version_id"],
                    tables=profile()["tables"],
                )
        self.assertEqual(tools.document_version_ids, before)

    def _controller_for(self, research, documents):
        initial = snapshot_for(research)
        chat_db = self.root / "isolated-controller.sqlite3"
        with closing(sqlite3.connect(chat_db)) as connection:
            connection.executescript("""
                CREATE TABLE conversations(
                  conversation_id TEXT PRIMARY KEY, company_id TEXT NOT NULL, title TEXT NOT NULL,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE runs(
                  run_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, status TEXT NOT NULL,
                  error_json TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
                  owner_pid INTEGER, cancel_requested INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE events(
                  run_id TEXT NOT NULL, seq INTEGER NOT NULL, type TEXT NOT NULL,
                  payload_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(run_id,seq));
            """)
            connection.execute(
                "INSERT INTO conversations VALUES(?,?,?,?,?)",
                ("conv", "MSFT", "fixture", "now", "now"))
            connection.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?)",
                ("run", "conv", "running", None, "now", "now", None, None, 0))
            connection.execute(
                "INSERT INTO events VALUES(?,?,?,?,?)",
                ("run", 1, "data_snapshot", json.dumps(initial), "now"))
            connection.commit()

        # Deliberately bypass the production constructor: this fixture owns only the
        # isolated event database and dependencies exercised by grant/replay.
        chat = ChatService.__new__(ChatService)
        chat.chat_db = chat_db
        chat.document_service = documents
        chat._lock = threading.RLock()
        chat.execution_scope_for = lambda _context: {"company_ids": ["AAPL", "MSFT"]}
        chat.documents_for = lambda _context: documents
        cancelled = threading.Event()
        retrieval = RetrievalBudget(
            company_id="MSFT", data_version=research.data_version,
            document_version_ids=research.document_version_ids)

        def grant(event):
            return chat._grant_snapshot_addition(
                event, research=research, run_id="run", cancel_event=cancelled,
                retrieval=retrieval, authorized_company_ids=frozenset(("AAPL", "MSFT")))

        return chat, cancelled, retrieval, grant

    def test_multi_subject_controller_derives_explicit_subjects_and_replays_scope(self):
        text_patch, bbox_patch = self.patches()
        with text_patch, bbox_patch:
            bases = {
                company_id: self.service.register_bytes(
                    self.raw + company_id.encode(), company_id=company_id,
                    source_key="annual-report", title=f"{company_id} annual report",
                    media_type="application/pdf", rights_status="isolated_candidate_only",
                )
                for company_id in ("MSFT", "AAPL")
            }
            research = tools_for_scope(
                self.evidence_db, "MSFT", ["MSFT", "AAPL"], self.service)
            initial = snapshot_for(research)
            untouched_initial = deepcopy(initial)

            chat, cancelled, retrieval, grant = self._controller_for(research, self.service)
            chat_db = chat.chat_db

            self.assertTrue(research.enable_document_derivation(grant))
            definitions = {item["name"]: item for item in research.definitions()}
            derive = definitions["derive_pdf_tables"]
            self.assertIn("cannot be reused for another subject", derive["description"])
            with self.assertRaisesRegex(ToolError, "explicitly select"):
                derive["handler"](
                    company_id="OUTSIDE", base_document_version_id=bases["MSFT"]["document_version_id"],
                    tables=profile()["tables"])
            with self.assertRaisesRegex(ToolError, "outside this run"):
                derive["handler"](
                    company_id="AAPL", base_document_version_id=bases["MSFT"]["document_version_id"],
                    tables=profile()["tables"])

            msft_before = snapshot_for(research)["subjects"]["MSFT"]
            aapl = derive["handler"](
                company_id="AAPL", base_document_version_id=bases["AAPL"]["document_version_id"],
                tables=profile()["tables"])
            self.assertEqual(snapshot_for(research)["subjects"]["MSFT"], msft_before)
            with closing(sqlite3.connect(chat_db)) as connection:
                recorded_event = json.loads(connection.execute(
                    "SELECT payload_json FROM events WHERE type='data_snapshot_addition'"
                ).fetchone()[0])
            outside = deepcopy(recorded_event)
            outside["company_id"] = "OUTSIDE"
            outside["source"]["company_id"] = "OUTSIDE"
            with self.assertRaisesRegex(ToolError, "outside the run scope"):
                grant(outside)
            wrong_subject = deepcopy(recorded_event)
            wrong_subject["company_id"] = "MSFT"
            wrong_subject["source"]["company_id"] = "MSFT"
            with self.assertRaisesRegex(ToolError, "foreign or invalid"):
                grant(wrong_subject)
            wrong_base = deepcopy(recorded_event)
            wrong_base["source"]["base_document_version_id"] = bases["MSFT"]["document_version_id"]
            with self.assertRaisesRegex(ToolError, "base version is outside"):
                grant(wrong_base)
            self.assertEqual(snapshot_for(research)["subjects"]["MSFT"], msft_before)
            msft = derive["handler"](
                company_id="MSFT", base_document_version_id=bases["MSFT"]["document_version_id"],
                tables=profile()["tables"])

            read = definitions["read_document_table"]["handler"](
                company_id="AAPL", document_version_id=aapl["document_version_id"], table_index=0)
            common = dict(
                company_id="AAPL", document_version_id=aapl["document_version_id"], table_index=0,
                header_row_indices=[0], row_label_column_index=0, unit="USD millions",
                period_start="2024-01-01", period_end="2024-12-31", period_kind="year",
                basis="issuer table presentation", metadata_mode="explicit_interpretation")
            revenue = definitions["extract_document_table_fact"]["handler"](
                **common, row_index=1, column_index=1, expected_value="100", metric="revenue")
            income = definitions["extract_document_table_fact"]["handler"](
                **common, row_index=2, column_index=1, expected_value="-20",
                metric="operating_income")
            calculated = research.calculate_facts(
                operation="ratio", fact_ids=[income["fact"]["fact_id"], revenue["fact"]["fact_id"]])
            self.assertEqual(read["rows"][1]["cells"][1]["numeric_candidate"]["value"], "100")
            self.assertEqual(calculated["value"], "-0.2")

            repeated = derive["handler"](
                company_id="AAPL", base_document_version_id=bases["AAPL"]["document_version_id"],
                tables=profile()["tables"])
            self.assertEqual(repeated["status"], "already_in_scope")
            with closing(sqlite3.connect(chat_db)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM events WHERE type='data_snapshot_addition'").fetchone()[0], 2)

            before_cancel = snapshot_for(research)
            cancelled.set()
            narrower = [{**profile()["tables"][0], "row_labels": ["Revenue"]}]
            with self.assertRaisesRegex(ToolError, "cancellation"):
                derive["handler"](
                    company_id="AAPL", base_document_version_id=aapl["document_version_id"],
                    tables=narrower)
            self.assertEqual(snapshot_for(research), before_cancel)
            cancelled.clear()

            effective = chat.effective_snapshot("run", "MSFT")
            self.assertEqual(initial, untouched_initial)
            self.assertIn(aapl["document_version_id"],
                          effective["subjects"]["AAPL"]["documents"]["document_version_ids"])
            self.assertIn(msft["document_version_id"],
                          effective["subjects"]["MSFT"]["documents"]["document_version_ids"])
            aggregate_ids = effective["documents"]["document_version_ids"]
            self.assertEqual(aggregate_ids, sorted(aggregate_ids))
            rebuilt = tools_for_scope(
                self.evidence_db, "MSFT", ["MSFT", "AAPL"], self.service,
                frozen_snapshot=effective)
            self.assertEqual(snapshot_for(rebuilt), {
                key: value for key, value in effective.items() if key != "derivations"})

    def test_multi_subject_private_material_derivation_binds_selected_logical_subject(self):
        text_patch, bbox_patch = self.patches()
        with text_patch, bbox_patch:
            base = self.service.register_bytes(
                self.raw, company_id="MSFT", source_key="session:conv:report.pdf",
                title="Private report.pdf", media_type="application/pdf",
                rights_status="session_only:conv")
            documents = ConversationDocuments(
                None, self.service, "conv", "MSFT", company_ids=("AAPL", "MSFT"))
            research = tools_for_scope(
                self.evidence_db, "MSFT", ["MSFT", "AAPL"], documents)
            chat, cancelled, retrieval, controller_grant = self._controller_for(research, documents)
            events = []

            def grant(event):
                result = controller_grant(event)
                events.append(event)
                return result

            research.enable_document_derivation(grant)
            derive = next(item["handler"] for item in research.definitions()
                          if item["name"] == "derive_pdf_tables")
            result = derive(
                company_id="AAPL", base_document_version_id=base["document_version_id"],
                tables=profile()["tables"])

            self.assertEqual(events[0]["company_id"], "AAPL")
            self.assertEqual(events[0]["document"]["company_id"], "AAPL")
            self.assertNotIn(result["document_version_id"],
                             research.for_company("MSFT").document_version_ids)
            second = derive(
                company_id="MSFT", base_document_version_id=base["document_version_id"],
                tables=profile()["tables"])
            self.assertEqual(second["document_version_id"], result["document_version_id"])
            self.assertEqual(len(events), 2)
            self.assertEqual(research.document_version_ids.count(result["document_version_id"]), 1)
            facts = {}
            for subject in ("AAPL", "MSFT"):
                child = research.for_company(subject)
                self.assertIn(result["document_version_id"], child.document_version_ids)
                extracted = child.extract_document_table_fact(
                    document_version_id=result["document_version_id"], table_index=0,
                    row_index=1, column_index=1, header_row_indices=[0], row_label_column_index=0,
                    expected_value="100", metric="revenue", unit="USD millions",
                    period_start="2025-01-01", period_end="2025-12-31", period_kind="year",
                    basis="issuer table presentation", metadata_mode="explicit_interpretation")
                descriptor = next(item for item in child.document_coverage["documents"]
                                  if item["document_version_id"] == result["document_version_id"])
                extracted = with_provenance(extracted, descriptor)
                facts[subject] = extracted["fact"]["fact_id"]
                _, detail = validate_document_fact(
                    documents=documents, company_id=subject, snapshot=snapshot_for(child), result=extracted)
                self.assertEqual(detail["source_scope"], "session_material")
                self.assertEqual(detail["conversation_id"], "conv")
            self.assertNotEqual(facts["AAPL"], facts["MSFT"])
            effective = chat.effective_snapshot("run", "MSFT")
            rebuilt = tools_for_scope(
                self.evidence_db, "MSFT", ["MSFT", "AAPL"], documents, frozen_snapshot=effective)
            self.assertEqual(snapshot_for(rebuilt), {
                key: value for key, value in effective.items() if key != "derivations"})

    def test_research_fact_keeps_compact_pdf_coordinates_states_rights_and_replays_exactly(self):
        text_patch, bbox_patch = self.patches()
        with text_patch, bbox_patch:
            record = self.service.register_bytes(
                self.raw, company_id="MSFT", source_key="annual-report", title="Annual report",
                media_type="application/pdf", rights_status="isolated_candidate_only",
                pdf_table_profile=profile(),
            )
            tools = ResearchTools(self.evidence_db, "MSFT", self.service)
            read = tools.read_document_table(
                document_version_id=record["document_version_id"], table_index=0,
                row_offset=1, row_limit=1,
            )
            provenance = read["pdf_provenance"]
            self.assertEqual(read["document_version"]["rights_status"], "isolated_candidate_only")
            self.assertEqual(provenance["physical_page"], 2)
            self.assertEqual(provenance["page_bbox"]["x_max"], 600.0)
            self.assertEqual(provenance["semantic_status"], "unapproved")
            self.assertEqual(provenance["authorization_status"], "unchanged")
            self.assertEqual(provenance["visible_rows"][0]["cells"][0]["bbox"]["x_min"], 320.0)
            self.assertIn("manifest_identity", provenance["manifest"])
            self.assertLess(len(json.dumps(read)), 18_000)

            result = tools.extract_document_table_fact(
                document_version_id=record["document_version_id"], table_index=0,
                row_index=1, column_index=1, header_row_indices=[0],
                row_label_column_index=0, expected_value="100", metric="revenue",
                unit="(in millions)", period_start="2024-01-01", period_end="2024-12-31",
                period_kind="year", basis="Revenue", metadata_mode="table_proven",
                unit_evidence_text="(in millions)",
                period_evidence_text="Fiscal Year Ended 2025 2024",
                basis_evidence_text="Revenue",
            )
            fact = result["fact"]
            target = fact["locator"]["pdf_provenance"]["target"]
            self.assertEqual(target["cell"]["raw_text"], "100")
            self.assertEqual(target["cell"]["bbox"]["x_min"], 320.0)
            self.assertEqual(target["header"]["original_header_word"]["text"], "2025")
            self.assertEqual(target["row"]["original_label_words"][0]["text"], "Revenue")
            self.assertEqual(fact["source_rights_status"], "isolated_candidate_only")
            self.assertEqual(fact["locator"]["pdf_provenance"]["semantic_status"], "unapproved")
            self.assertLess(len(json.dumps(fact)), 12_000)

            descriptor = next(item for item in tools.document_coverage["documents"]
                              if item["document_version_id"] == record["document_version_id"])
            recorded = with_provenance(result, descriptor)
            self.assertLess(len(json.dumps(recorded)), 18_000)
            snapshot = {
                "authoritative": True, "truncated": False,
                "documents": {
                    "coverage": tools.document_coverage,
                    "document_version_ids": list(tools.document_version_ids),
                },
            }
            replayed_fact, summary = validate_document_fact(
                snapshot=snapshot, company_id="MSFT", documents=self.service,
                result=recorded, expected_fact=fact,
            )
            self.assertEqual(replayed_fact, fact)
            self.assertEqual(summary["table"]["provenance"]["target"]["cell"]["bbox"],
                             target["cell"]["bbox"])
            self.assertFalse(summary["interpretation"]["semantic_verified"])

            for field, mutate in (
                ("bbox", lambda item: item["target"]["cell"]["bbox"].__setitem__("x_min", 1.0)),
                ("semantic_status", lambda item: item.__setitem__("semantic_status", "approved")),
            ):
                with self.subTest(forged=field):
                    forged = json.loads(json.dumps(recorded))
                    mutate(forged["fact"]["locator"]["pdf_provenance"])
                    with self.assertRaises(DocumentError) as mismatch:
                        validate_document_fact(
                            snapshot=snapshot, company_id="MSFT", documents=self.service,
                            result=forged,
                        )
                    self.assertEqual(mismatch.exception.code, "citation_source_mismatch")

    def test_profile_tool_xml_and_source_bytes_each_change_version_identity(self):
        text_patch, bbox_patch = self.patches()
        with text_patch, bbox_patch:
            first = self.register()
            self.assertEqual(self.register(), first)
            changed_profile = self.register(selected_profile=profile(["2025"]))
            self.state["tool"] = "b"
            changed_tool = self.register()
            self.state["xml"] = bbox_xml("101")
            changed_xml = self.register()
            changed_bytes = self.register(raw=self.raw + b"\nrevision")

        versions = [first, changed_profile, changed_tool, changed_xml, changed_bytes]
        self.assertEqual([item["source_version"] for item in versions], [1, 2, 3, 4, 5])
        self.assertEqual(len({item["document_version_id"] for item in versions}), 5)
        self.assertEqual(first["source_sha256"], changed_profile["source_sha256"])
        self.assertNotEqual(changed_xml["document_version_id"], changed_tool["document_version_id"])
        self.assertNotEqual(changed_bytes["source_sha256"], changed_xml["source_sha256"])

    def test_frozen_replay_rejects_identity_or_xml_tampering(self):
        text_patch, bbox_patch = self.patches()
        with text_patch, bbox_patch:
            record = self.register()
            arguments = {"company_id": "CRM", "document_version_id": record["document_version_id"]}
            self.service.read_frozen_version(**arguments)
            self.state["xml"] = bbox_xml("101")
            with self.assertRaises(DocumentError) as changed_xml:
                self.service.read_frozen_version(**arguments)
            self.assertEqual(changed_xml.exception.code, "historical_parser_unavailable")

        connection = sqlite3.connect(self.root / "documents" / "catalog.sqlite3")
        try:
            saved = connection.execute(
                "SELECT identity_json FROM parsed_document_identities WHERE document_version_id=?",
                (record["document_version_id"],),
            ).fetchone()[0]
            identity = json.loads(saved)
            identity["profile"]["tables"][0]["title"] = "TAMPERED"
            connection.execute(
                "UPDATE parsed_document_identities SET identity_json=? WHERE document_version_id=?",
                (json.dumps(identity), record["document_version_id"]),
            )
            connection.commit()
        finally:
            connection.close()
        self.state["xml"] = bbox_xml()
        text_patch, bbox_patch = self.patches()
        with text_patch, bbox_patch, self.assertRaises(DocumentError) as tampered:
            self.service.read_frozen_version(**arguments)
        self.assertEqual(tampered.exception.code, "historical_parser_unavailable")

    def test_cached_replay_rejects_changed_version_metadata(self):
        text_patch, bbox_patch = self.patches()
        with text_patch, bbox_patch:
            record = self.register()
            arguments = {"company_id": "CRM", "document_version_id": record["document_version_id"]}
            with self.service.frozen_replay_scope():
                self.service.read_frozen_version(**arguments)
                with closing(sqlite3.connect(self.service.db_path)) as conn, conn:
                    conn.execute("DROP TRIGGER document_versions_no_update")  # Simulate catalog tampering, not a supported write.
                    conn.execute("UPDATE document_versions SET title='changed' WHERE document_version_id=?",
                                 (record["document_version_id"],))
                with self.assertRaises(DocumentError):
                    self.service.read_frozen_version(**arguments)

    def test_legacy_migration_rolls_back_failure_and_preserves_frozen_versions(self):
        from workbench import documents
        old = self.service.register_bytes(
            b"<!doctype html><html><body><p>Unchanged historical source.</p></body></html>", company_id="CRM", source_key="legacy",
            title="Legacy", media_type="text/html", rights_status="review_required")
        with closing(sqlite3.connect(self.service.db_path)) as conn, conn:
            conn.execute("DROP TABLE parsed_table_provenance")
            conn.execute("DROP TABLE parsed_document_identities")
            fingerprint = DocumentService._schema_fingerprint(conn)
            self.assertEqual(fingerprint, DocumentService._expected_schema_fingerprint(documents._SCHEMA_V1))
            conn.execute("UPDATE catalog_meta SET value=? WHERE key='schema_version'",
                         (documents.LEGACY_SCHEMA_VERSION,))
            conn.execute("UPDATE catalog_meta SET value=? WHERE key='schema_fingerprint'", (fingerprint,))
            conn.execute("PRAGMA user_version=1")
        with mock.patch.object(documents, "_SCHEMA_ADDITIONS_V2",
                               documents._SCHEMA_ADDITIONS_V2 + "\nSELECT nonexistent_migration_function();"):
            with self.assertRaises(DocumentError):
                DocumentService(self.service.root)
        with closing(sqlite3.connect(self.service.db_path)) as conn, conn:
            self.assertEqual(DocumentService._schema_fingerprint(conn), fingerprint)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
        migrated = DocumentService(self.service.root)
        frozen = migrated.read_frozen_version(company_id="CRM", document_version_id=old["document_version_id"])
        self.assertEqual(frozen["text"], "Unchanged historical source.")
        with closing(sqlite3.connect(self.service.db_path)) as conn, conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM document_versions").fetchone()[0], 1)

    def test_failed_profile_reparse_cannot_supersede_a_successful_version(self):
        text_patch, bbox_patch = self.patches()
        with text_patch, bbox_patch:
            first = self.register()
            self.state["xml"] = b"invalid XML"
            for _ in range(2):
                with self.assertRaises(DocumentError) as failed:
                    self.register()
                self.assertEqual(failed.exception.code, "pdf_table_parse_failed")
        with closing(sqlite3.connect(self.service.db_path)) as conn:
            rows = conn.execute("SELECT document_version_id FROM document_versions").fetchall()
            self.assertEqual(rows, [(first["document_version_id"],)])

    def test_profile_cannot_follow_xml_order_against_spatial_year_order(self):
        text_patch, bbox_patch = self.patches()
        self.state["xml"] = bbox_xml().replace(
            b'<word xMin="320" yMin="60" xMax="350" yMax="70">2025</word>',
            b'<word xMin="420" yMin="60" xMax="450" yMax="70">2025</word>').replace(
            b'<word xMin="420" yMin="60" xMax="450" yMax="70">2024</word>',
            b'<word xMin="320" yMin="60" xMax="350" yMax="70">2024</word>')
        with text_patch, bbox_patch, self.assertRaises(DocumentError):
            self.register()

    def test_profile_does_not_change_rights_or_claim_semantic_approval(self):
        text_patch, bbox_patch = self.patches()
        with text_patch, bbox_patch:
            record = self.register()
        table = self.service.read_table(
            company_id="CRM", document_version_id=record["document_version_id"], table_index=0
        )
        self.assertEqual(table["rights_status"], "isolated_candidate_only")
        self.assertEqual(table["provenance"]["semantic_status"], "unapproved")
        serialized = json.dumps(table, sort_keys=True)
        self.assertNotIn('"semantic_verified": true', serialized)
        self.assertNotIn('"approved": true', serialized)


if __name__ == "__main__":
    unittest.main()
