"""Bounded local-only text/CSV/XLSX parsing, preserving original upload bytes."""
import csv
import io
from pathlib import PurePosixPath
import zipfile
from xml.etree import ElementTree as ET

from .documents import DocumentError, DocumentService
from .parser import ParsedDocument, ParsedTable, ParseError


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
UPLOAD_TYPES = {".html": "text/html", ".htm": "text/html", ".pdf": "application/pdf",
                ".txt": "text/plain", ".md": "text/plain", ".markdown": "text/plain",
                ".csv": "text/csv", ".xlsx": XLSX}
MAX_PARSED_CHARS = 2_000_000
MAX_ROWS = 10_000
MAX_COLUMNS = 256
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _utf8(raw):
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ParseError("Text and CSV uploads must be UTF-8") from exc
    if "\x00" in text or len(text) > MAX_PARSED_CHARS or not text.strip():
        raise ParseError("Text is empty, contains NUL, or exceeds the 2-million-character parse limit")
    return text


def _tables(sheets, version):
    parts, tables, length, total_rows = [], [], 0, 0
    for title, rows in sheets:
        heading = title + "\n" if title else ""
        parts.append(heading)
        length += len(heading)
        start, spans = length, []
        for row in rows:
            total_rows += 1
            if total_rows > MAX_ROWS or len(row) > MAX_COLUMNS:
                raise ParseError("Table exceeds 10,000 total rows or 256 columns")
            line = "\t".join(row)
            spans.append((length, length + len(line)))
            parts.append(line + "\n")
            length += len(line) + 1
            if length > MAX_PARSED_CHARS:
                raise ParseError("Parsed workbook exceeds 2 million characters")
        if rows:
            tables.append(ParsedTable(len(tables), rows, start, length - 1, spans))
    if not tables:
        raise ParseError("No tabular data was found")
    return ParsedDocument("".join(parts), tables, version)


def _xml(archive, name):
    try:
        raw = archive.read(name)
    except KeyError as exc:
        raise ParseError("Workbook references a missing XML part") from exc
    if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ParseError("XML document types and entities are not allowed")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ParseError("Malformed workbook XML") from exc


def _xlsx(raw):
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            names = [item.filename for item in entries]
            if (len(entries) > 2048 or len(set(names)) != len(names)
                    or sum(item.file_size for item in entries) > 20 * 1024 * 1024
                    or any(item.flag_bits & 1 for item in entries)):
                raise ParseError("Workbook is encrypted, ambiguous, or exceeds expanded-size limits")
            if any("vbaproject" in name.lower() for name in names):
                raise ParseError("Macro-enabled workbooks are not supported")
            strings = []
            if "xl/sharedStrings.xml" in names:
                strings = ["".join(node.itertext()) for node in _xml(archive, "xl/sharedStrings.xml").findall(_NS + "si")]
            relationships = _xml(archive, "xl/_rels/workbook.xml.rels")
            targets = {}
            for rel in relationships:
                if rel.get("TargetMode") == "External":
                    raise ParseError("External workbook relationships are not supported")
                target = rel.get("Target", "")
                target = target.lstrip("/") if target.startswith("/") else "xl/" + target
                if ".." in PurePosixPath(target).parts:
                    raise ParseError("Workbook part escapes its package scope")
                targets[rel.get("Id")] = target
            sheets = []
            for sheet in _xml(archive, "xl/workbook.xml").findall(_NS + "sheets/" + _NS + "sheet"):
                target = targets.get(sheet.get(_REL + "id"))
                if not target:
                    raise ParseError("Worksheet relationship is missing")
                rows = []
                for row in _xml(archive, target).findall(_NS + "sheetData/" + _NS + "row"):
                    row_number = int(row.get("r", len(rows) + 1))
                    if row_number <= len(rows) or row_number > MAX_ROWS:
                        raise ParseError("Worksheet rows are unordered or exceed the limit")
                    while len(rows) < row_number - 1:
                        rows.append([])
                    cells, previous = [], -1
                    for cell in row.findall(_NS + "c"):
                        reference = cell.get("r", "")
                        letters = "".join(char for char in reference if "A" <= char <= "Z")
                        column = 0
                        for letter in letters:
                            column = column * 26 + ord(letter) - 64
                        column -= 1
                        if column <= previous or not 0 <= column < MAX_COLUMNS:
                            raise ParseError("Worksheet cell coordinates are invalid or exceed 256 columns")
                        previous = column
                        cells.extend([""] * (column + 1 - len(cells)))
                        formula = cell.find(_NS + "f")
                        if formula is not None:
                            # Cached formula values are not recomputed or trusted.
                            value = "[Unevaluated formula] =" + (formula.text or "")
                        elif cell.get("t") == "inlineStr":
                            value = "".join(cell.find(_NS + "is").itertext())
                        else:
                            value = cell.findtext(_NS + "v", "")
                            if cell.get("t") == "s":
                                index = int(value)
                                if not 0 <= index < len(strings):
                                    raise ParseError("Invalid shared-string index")
                                value = strings[index]
                            elif cell.get("t") == "b":
                                value = "TRUE" if value == "1" else "FALSE"
                        cells[column] = value
                    rows.append(cells)
                sheets.append((sheet.get("name", "Worksheet"), rows))
            return _tables(sheets, "session-xlsx-cells/1")
    except (zipfile.BadZipFile, ValueError, AttributeError, RuntimeError) as exc:
        raise ParseError(f"Workbook cannot be parsed: {exc}") from exc


def _text_document(raw):
    return ParsedDocument(_utf8(raw), [], "session-utf8-text/1")


def _csv_document(raw):
    try:
        rows = list(csv.reader(io.StringIO(_utf8(raw)), strict=True))
    except csv.Error as exc:
        raise ParseError(f"CSV cannot be parsed: {exc}") from exc
    return _tables([("", rows)], "session-utf8-csv/1")


class MaterialDocumentService(DocumentService):
    """Separate private source registry and schema; public catalogs remain unchanged."""
    catalog_media_types = "'text/html','application/pdf','text/plain','text/csv','" + XLSX + "'"

    def __init__(self, root, *, object_store=None, source_registry=None):
        super().__init__(root, object_store=object_store, source_registry=source_registry,
                         source_scope="private")

    @staticmethod
    def _validate_media(raw, requested):
        if requested in {"text/plain", "text/csv"}:
            _utf8(raw)
            return requested
        if requested == XLSX:
            if not raw.startswith(b"PK\x03\x04"):
                raise DocumentError("media_type_mismatch", "XLSX must be an actual ZIP workbook")
            return requested
        return DocumentService._validate_media(raw, requested)

    @staticmethod
    def _parse_document(raw, media_type):
        if media_type == "text/plain":
            return _text_document(raw)
        if media_type == "text/csv":
            return _csv_document(raw)
        if media_type == XLSX:
            return _xlsx(raw)
        return DocumentService._parse_document(raw, media_type)

    @staticmethod
    def _parse_frozen_document(raw, media_type, parser_version):
        parser = {("text/plain", "session-utf8-text/1"): _text_document,
                  ("text/csv", "session-utf8-csv/1"): _csv_document,
                  (XLSX, "session-xlsx-cells/1"): _xlsx}.get((media_type, parser_version))
        if parser is None:
            return DocumentService._parse_frozen_document(raw, media_type, parser_version)
        try:
            parsed = parser(raw)
        except (ParseError, OSError) as exc:
            raise DocumentError("historical_parser_unavailable", "The persisted material parser is unavailable") from exc
        if parsed.parser_version != parser_version:
            raise DocumentError("historical_parser_unavailable", "Material parser identity differs from the persisted version")
        return parsed
