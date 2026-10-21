import base64
from contextlib import closing
import hashlib
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import zipfile

from workbench.api_contracts import validate_response
from workbench.build import build_database
from workbench.chat import ChatError, ChatService
from workbench.documents import DocumentError, DocumentService
from workbench.material_parser import MaterialDocumentService, XLSX
from workbench.parser import ParsedDocument
from workbench.research_tools import ResearchTools
from tests.test_chat import PassageProvider, FakeProvider, TERMINAL
from tests.test_documents import pdf_with_text


def workbook(cell='<c r="A1" t="inlineStr"><is><t>ArbitraryChatToken</t></is></c>'):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Inputs" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1">' + cell + '</row></sheetData></worksheet>')
    return output.getvalue()


class SessionMaterialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.evidence = self.root / "evidence.sqlite3"
        build_database(db_path=self.evidence)
        self.public = DocumentService(self.root / "public")
        self.chat = ChatService(self.evidence, self.root / "chat.sqlite3",
                                provider=PassageProvider(), document_service=self.public)
        self.cid = self.chat.create_conversation("MSFT")["conversation_id"]
        self.other = self.chat.create_conversation("MSFT")["conversation_id"]

    def tearDown(self):
        for thread in list(self.chat._threads.values()):
            thread.join(10)
        self.temp.cleanup()

    def upload(self, raw=b"ArbitraryChatToken user supplied information", filename="notes.txt", cid=None):
        return self.chat.materials.upload(cid or self.cid, filename, base64.b64encode(raw).decode(), True)

    def wait(self, run):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.chat.get_run(run["run_id"])
            if result["status"] in TERMINAL:
                thread = self.chat._threads.get(run["run_id"])
                if thread:
                    thread.join(5)
                return result
            time.sleep(.01)
        self.fail("research did not finish")

    def test_private_original_versions_and_public_unchanged(self):
        before = self.public.db_path.read_bytes()
        first = self.upload()
        self.assertEqual(first["parse_status"], "parsed")
        self.assertEqual(self.upload()["document_version_id"], first["document_version_id"])
        second = self.upload(b"ArbitraryChatToken revised")
        self.assertEqual(second["source_version"], 2)
        listed = self.chat.materials.list(self.cid)["materials"]
        self.assertEqual([item["document_version_id"] for item in listed], [second["document_version_id"]])
        self.assertEqual(self.chat.materials.raw(self.cid, first["document_version_id"])[0],
                         b"ArbitraryChatToken user supplied information")
        self.assertEqual(self.chat.materials.list(self.other)["materials"], [])
        with self.assertRaises(ChatError):
            self.chat.materials.raw(self.other, first["document_version_id"])
        self.assertEqual(before, self.public.db_path.read_bytes())
        self.assertEqual(self.public.get_document_coverage(company_id="MSFT")["enrolled"], 0)
        other_record = self.upload(cid=self.other)
        self.assertNotEqual(first["document_version_id"], other_record["document_version_id"])
        with self.assertRaises(DocumentError):
            self.chat.documents_for({"conversation_id": self.other}).read_frozen_version(
                company_id="MSFT", document_version_id=first["document_version_id"])

    def test_replay_scope_preserves_private_ownership_and_expires(self):
        first = self.upload()
        self.upload(cid=self.other)
        documents = self.chat.documents_for({"conversation_id": self.cid})
        other = self.chat.documents_for({"conversation_id": self.other})
        with documents.frozen_replay_scope():
            self.assertIsNotNone(documents.private._frozen_replay_cache.get())
            self.assertIsNotNone(documents.public._frozen_replay_cache.get())
            self.assertIsNone(other.private._frozen_replay_cache.get())
            saved = documents.read_frozen_version(company_id="MSFT", document_version_id=first["document_version_id"])
            with self.assertRaises(DocumentError):
                other.read_frozen_version(company_id="MSFT", document_version_id=first["document_version_id"])
            self.assertEqual(saved, documents.read_frozen_version(company_id="MSFT", document_version_id=first["document_version_id"]))
        self.assertIsNone(documents.private._frozen_replay_cache.get())
        self.assertIsNone(documents.public._frozen_replay_cache.get())

    def test_material_is_shared_only_with_authorized_subjects_in_its_conversation(self):
        conversation = self.chat.create_conversation("MSFT", company_ids=["MSFT", "AAPL"])
        identifier = conversation["conversation_id"]
        version = self.upload(b"ArbitraryChatToken multi-subject private material", cid=identifier)
        documents = self.chat.documents_for(conversation)
        for company in ("MSFT", "AAPL"):
            coverage = documents.get_document_coverage(company_id=company)
            self.assertEqual([item["document_version_id"] for item in coverage["documents"]],
                             [version["document_version_id"]])
            self.assertEqual(documents.read_raw_version(
                company_id=company, document_version_id=version["document_version_id"]),
                b"ArbitraryChatToken multi-subject private material")
        research = self.chat.research_for(conversation)
        self.assertEqual(research.document_coverage["enrolled"], 1)
        self.assertEqual(len(research.document_version_ids), 1)
        with self.assertRaises(DocumentError):
            documents.read_raw_version(company_id="OUTSIDE", document_version_id=version["document_version_id"])
        with self.assertRaises(DocumentError):
            self.chat.documents_for({"conversation_id": self.other}).read_raw_version(
                company_id="MSFT", document_version_id=version["document_version_id"])
        self.assertEqual(self.public.get_document_coverage(company_id="MSFT")["enrolled"], 0)
        self.assertEqual(self.public.get_document_coverage(company_id="AAPL")["enrolled"], 0)

    def test_worker_snapshot_citation_artifact_and_old_original(self):
        first = self.upload()
        run = self.wait(self.chat.submit(self.cid, "Explain the supplied document"))
        self.assertEqual(run["status"], "completed", run.get("error"))
        answer = [item for item in self.chat.get_conversation(self.cid)["messages"]
                  if item["role"] == "assistant"][-1]
        citation = answer["citations"][0]
        detail = self.chat.read_citation(self.cid, answer["message_id"], "passage", citation["id"])
        validate_response("GET", "/api/chat/citation", 200, detail)
        self.assertEqual(detail["source_scope"], "session_material")
        self.assertEqual(detail["source_visibility"], "private")
        self.assertEqual(detail["document_version_id"], first["document_version_id"])
        with self.assertRaises(ChatError):
            self.chat.read_citation(self.other, answer["message_id"], "passage", citation["id"])
        from workbench.answer_artifacts import AnswerArtifactService
        artifact = AnswerArtifactService(self.chat).get(self.cid, answer["message_id"])
        self.assertTrue(artifact["artifact_id"])
        self.upload(b"ArbitraryChatToken later material")
        old = self.chat.read_citation(self.cid, answer["message_id"], "passage", citation["id"])
        self.assertEqual(old, detail)
        raw = self.chat.read_raw_source(self.cid, answer["message_id"], first["source_sha256"])
        self.assertEqual(raw, b"ArbitraryChatToken user supplied information")
        followup = self.chat.submit(self.cid, "Continue this research")
        self.assertEqual(followup["status"], "blocked")
        decision = self.chat.decisions.for_run(followup["run_id"])
        self.assertEqual(decision["kind"], "time")
        with self.assertRaises(ChatError) as blocked:
            self.upload()
        self.assertEqual(blocked.exception.code, "confirmation_pending")

    def test_material_frozen_replay_ignores_new_default_and_rejects_identity_change(self):
        cases = [("notes.md", b"Archived note", "text/plain"),
                 ("data.csv", b"metric,2025\nrevenue,123.45\n", "text/csv"),
                 ("data.xlsx", workbook(), XLSX)]
        for filename, raw, media_type in cases:
            with self.subTest(filename=filename):
                record = self.upload(raw, filename)
                documents = self.chat.documents_for({"conversation_id": self.cid})
                expected = documents.read_frozen_version(company_id="MSFT", document_version_id=record["document_version_id"])
                with mock.patch.object(MaterialDocumentService, "_parse_document", side_effect=AssertionError("today's dispatcher")):
                    self.assertEqual(documents.read_frozen_version(company_id="MSFT", document_version_id=record["document_version_id"]), expected)
                with self.assertRaises(DocumentError) as caught:
                    MaterialDocumentService._parse_frozen_document(raw, media_type, "unrecognized/2")
                self.assertEqual(caught.exception.code, "historical_parser_unsupported")
        with mock.patch("workbench.material_parser._csv_document", return_value=ParsedDocument("changed", [], "session-utf8-csv/2")):
            with self.assertRaises(DocumentError) as caught:
                MaterialDocumentService._parse_frozen_document(b"a,b\n", "text/csv", "session-utf8-csv/1")
        self.assertEqual(caught.exception.code, "historical_parser_unavailable")

    def test_csv_xlsx_tables_are_reparsed_from_original(self):
        for filename, raw in [
                ("data.csv", b"metric,2025\nrevenue,123.45\n"),
                ("data.xlsx", workbook('<c r="B1"><v>123.45</v></c><c r="C1"><f>B1*2</f><v>246.9</v></c>'))]:
            with self.subTest(filename=filename):
                record = self.upload(raw, filename)
                self.assertEqual(record["parse_status"], "parsed", record.get("parse_error"))
                documents = self.chat.documents_for({"conversation_id": self.cid})
                frozen = documents.read_frozen_version(company_id="MSFT", document_version_id=record["document_version_id"])
                self.assertIn("123.45", frozen["text"])
                self.assertEqual(len(frozen["tables"]), 1)
                self.assertEqual(documents.read_raw_version(company_id="MSFT", document_version_id=record["document_version_id"]), raw)
                if filename.endswith("xlsx"):
                    self.assertIn("[未计算公式]", frozen["text"])
                    self.assertNotIn("246.9", frozen["text"])

    def test_limits_consent_failures_and_pdf_status(self):
        for filename, payload, consent in [("../notes.txt", "YQ==", True),
                                           ("bad\r\nheader.txt", "YQ==", True),
                                           ("notes.txt", "!!!", True),
                                           ("notes.txt", "YQ==", False),
                                           ("notes.exe", "YQ==", True)]:
            with self.subTest(filename=filename, payload=payload, consent=consent), self.assertRaises(ChatError):
                self.chat.materials.upload(self.cid, filename, payload, consent)
        pdf = self.upload(pdf_with_text(["SessionPdfAnchor"]), "filing.pdf")
        self.assertEqual(pdf["parse_status"], "parsed")
        self.assertIn("Material saved", pdf["message"])
        documents = self.chat.documents_for({"conversation_id": self.cid})
        self.assertTrue(documents.search_documents(company_id="MSFT", query="SessionPdfAnchor")["results"])
        malformed = self.upload(b"PK\x03\x04not-a-workbook", "bad.xlsx")
        self.assertEqual(malformed["parse_status"], "parse_failed")
        self.assertEqual(self.chat.materials.raw(self.cid, malformed["document_version_id"])[0], b"PK\x03\x04not-a-workbook")

    def test_markdown_material_is_utf8_text_and_searchable(self):
        record = self.upload(b"# Research note\n\nMarkdownAnchor says retention is 42 units.\n", "note.md")
        self.assertEqual(record["parse_status"], "parsed", record.get("parse_error"))
        self.assertEqual(record["media_type"], "text/plain")
        documents = self.chat.documents_for({"conversation_id": self.cid})
        result = documents.search_documents(company_id="MSFT", query="MarkdownAnchor")["results"][0]
        self.assertEqual(result["media_type"], "text/plain")
        self.assertIn("MarkdownAnchor", result["quote"])
        frozen = documents.read_frozen_version(company_id="MSFT", document_version_id=record["document_version_id"])
        self.assertIn("# Research note", frozen["text"])
        self.assertEqual(frozen["tables"], [])

    def test_missing_catalog_and_cross_owner_copy_fail_closed(self):
        self.upload()
        root = self.chat.materials.root / hashlib.sha256(self.cid.encode()).hexdigest()
        (root / "catalog.sqlite3").rename(root / "removed.sqlite3")
        with self.assertRaises(ChatError) as missing:
            self.chat.materials.list(self.cid)
        self.assertEqual(missing.exception.code, "material_storage_invalid")


if __name__ == "__main__":
    unittest.main()
