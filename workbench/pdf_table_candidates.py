"""Strict, review-only table candidates from pdftotext ``-bbox-layout`` XML.

This module does not create parsed tables or approved facts.  Callers must name the
single statement title, years, and row labels they want inspected.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any
import xml.etree.ElementTree as ET


MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_XML_BYTES = 2_000_000
MAX_PAGES = 1
MAX_LINES = 5_000
MAX_WORDS = 20_000
MAX_ELEMENTS = 100_000
MAX_ATTRIBUTES = 200_000
PDF_TIMEOUT_SECONDS = 8
PDF_CPU_SECONDS = 4
PDF_MEMORY_BYTES = 512 * 1024 * 1024
PARSER_VERSION = "pdf-table-candidates-bbox/3"
YEAR_COLUMN_SELECTION_PARSER_VERSION = "pdf-table-candidates-bbox/4"
EXTRACTION_PROFILE_VERSION = "pdftotext-bbox-layout-sandbox/2"
REPLAY_MANIFEST_VERSION = "pdf-table-candidate-replay/1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_YEAR_RE = re.compile(r"(?:19|20)\d{2}\Z")
_NUMBER_ATOM = r"\$?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_NUMBER_RE = re.compile(rf"(?:-?{_NUMBER_ATOM}|\({_NUMBER_ATOM}\))\Z")


class CandidateParseError(ValueError):
    """A stable refusal to infer a coordinate-table candidate."""


class CandidateExtractionError(RuntimeError):
    """A bounded PDF extraction failure."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _finite_float(value: str | None, name: str) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise CandidateParseError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise CandidateParseError(f"{name} must be a finite number")
    return number


def _bbox(element: ET.Element, *, limit: tuple[float, float], kind: str) -> dict[str, float]:
    box = {
        "x_min": _finite_float(element.get("xMin"), f"{kind}.xMin"),
        "y_min": _finite_float(element.get("yMin"), f"{kind}.yMin"),
        "x_max": _finite_float(element.get("xMax"), f"{kind}.xMax"),
        "y_max": _finite_float(element.get("yMax"), f"{kind}.yMax"),
    }
    if not (0 <= box["x_min"] < box["x_max"] <= limit[0] and
            0 <= box["y_min"] < box["y_max"] <= limit[1]):
        raise CandidateParseError(f"{kind} bbox is outside the physical page or is empty")
    return box


def _word_record(element: ET.Element, page_limit: tuple[float, float]) -> dict[str, Any]:
    text = element.text if element.text is not None else ""
    if not text or "\x00" in text or len(text) > 500:
        raise CandidateParseError("word text must contain 1..500 safe characters")
    return {"text": text, "bbox": _bbox(element, limit=page_limit, kind="word")}


def _normalized(words: list[dict[str, Any]]) -> str:
    return " ".join(word["text"] for word in words).strip()


def _center(box: dict[str, float]) -> float:
    return (box["x_min"] + box["x_max"]) / 2


def _union(words: list[dict[str, Any]]) -> dict[str, float]:
    return {
        "x_min": min(word["bbox"]["x_min"] for word in words),
        "y_min": min(word["bbox"]["y_min"] for word in words),
        "x_max": max(word["bbox"]["x_max"] for word in words),
        "y_max": max(word["bbox"]["y_max"] for word in words),
    }


def _optional_context_selector(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise CandidateParseError(f"{name} must be null or contain 1..500 characters")
    if value != value.strip() or "\x00" in value:
        raise CandidateParseError(f"{name} must be exact trimmed safe source text")
    return value


def _context_record(
    selected_lines: list[dict[str, Any]],
    selected_word_groups: list[list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    groups = selected_word_groups or [line["words"] for line in selected_lines]
    words = [word for group in groups for word in group]
    return {
        "raw_text": " ".join(_normalized(group) for group in groups),
        "original_lines": [
            {"raw_text": _normalized(group), "original_words": group, "bbox": _union(group)}
            for group in groups
        ],
        "original_words": words,
        "bbox": _union(words),
    }


def _exact_word_spans(
    selected_lines: list[dict[str, Any]], selector: str,
) -> list[list[list[dict[str, Any]]]]:
    """Return exact contiguous source-word matches, grouped by their source lines."""
    located = [
        (word, line_index)
        for line_index, line in enumerate(selected_lines)
        for word in line["words"]
    ]
    matches: list[list[list[dict[str, Any]]]] = []
    # A 500-character selector cannot contain more than 251 non-empty words
    # separated by spaces. Keep adversarial many-word lines strictly bounded.
    for start in range(len(located)):
        for end in range(start + 1, min(len(located), start + 251) + 1):
            words = [item[0] for item in located[start:end]]
            text = _normalized(words)
            if len(text) > len(selector):
                break
            if text != selector:
                continue
            groups = [[] for _line in selected_lines]
            for word, line_index in located[start:end]:
                groups[line_index].append(word)
            if all(groups):
                matches.append(groups)
    return matches


def parse_bbox_table_candidate(
    xml: bytes,
    *,
    source_sha256: str,
    physical_page: int,
    title: str,
    years: list[str],
    row_labels: list[str],
    unit_line: str | None = None,
    fiscal_period_header: str | None = None,
    year_column_indices: list[int] | None = None,
) -> dict[str, Any]:
    """Parse one explicitly scoped statement candidate or refuse ambiguity.

    ``physical_page`` is supplied by the bounded ``-f/-l`` extraction call because
    pdftotext's XML does not retain the original physical page number.
    """
    if not isinstance(xml, bytes) or not xml or len(xml) > MAX_XML_BYTES:
        raise CandidateParseError("bbox XML must contain 1..2000000 bytes")
    try:
        xml.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CandidateParseError("bbox XML must use UTF-8") from exc
    if b"\x00" in xml:
        raise CandidateParseError("NUL-containing or wide-character XML is not accepted")
    # pdftotext emits this fixed external XHTML identifier. Remove it before
    # parsing so no external resource is consulted; reject every other DTD and
    # every entity declaration (including declarations hidden later in input).
    pdftotext_doctype = (
        b'<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" '
        b'"http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">'
    )
    upper_xml = xml.upper()
    if b"<!ENTITY" in upper_xml:
        raise CandidateParseError("DTD and entity declarations are not accepted")
    if b"<!DOCTYPE" in upper_xml:
        if not xml.startswith(pdftotext_doctype) or b"<!DOCTYPE" in upper_xml[len(pdftotext_doctype):]:
            raise CandidateParseError("DTD and entity declarations are not accepted")
        xml = xml[len(pdftotext_doctype):]
    if not isinstance(source_sha256, str) or _SHA256_RE.fullmatch(source_sha256) is None:
        raise CandidateParseError("source_sha256 must be a lowercase SHA-256 hex digest")
    if not isinstance(physical_page, int) or isinstance(physical_page, bool) or physical_page < 1:
        raise CandidateParseError("physical_page must be a positive integer")
    if not isinstance(title, str) or not title.strip() or len(title) > 300:
        raise CandidateParseError("title must contain 1..300 characters")
    if (not isinstance(years, list) or not years or len(years) > 8
            or any(not isinstance(year, str) or _YEAR_RE.fullmatch(year) is None for year in years)
            or len(set(years)) != len(years)):
        raise CandidateParseError("years must be 1..8 unique four-digit year strings")
    if (not isinstance(row_labels, list) or not row_labels or len(row_labels) > 30
            or any(not isinstance(label, str) or not label.strip() or len(label) > 300 for label in row_labels)
            or len(set(row_labels)) != len(row_labels)):
        raise CandidateParseError("row_labels must be 1..30 unique non-empty labels")
    if year_column_indices is not None:
        if (not isinstance(year_column_indices, list)
                or len(year_column_indices) != len(years)
                or any(type(index) is not int or index < 0 for index in year_column_indices)
                or len(set(year_column_indices)) != len(year_column_indices)):
            raise CandidateParseError(
                "year_column_indices must contain one unique non-negative integer per requested year"
            )
        if year_column_indices != sorted(year_column_indices):
            raise CandidateParseError("year_column_indices must be monotonic")
    unit_line = _optional_context_selector(unit_line, "unit_line")
    fiscal_period_header = _optional_context_selector(fiscal_period_header, "fiscal_period_header")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise CandidateParseError("bbox XML is not well formed") from exc
    elements = list(root.iter())
    if len(elements) > MAX_ELEMENTS:
        raise CandidateParseError("bbox XML exceeds the element limit")
    if sum(len(element.attrib) for element in elements) > MAX_ATTRIBUTES:
        raise CandidateParseError("bbox XML exceeds the attribute limit")
    pages = [element for element in elements if _local_name(element.tag) == "page"]
    if len(pages) != MAX_PAGES:
        raise CandidateParseError("bbox XML must contain exactly one physical page")
    page = pages[0]
    if any(key.casefold() in {"rotate", "rotation"} and value not in {"0", "0.0"}
           for element in elements for key, value in element.attrib.items()):
        raise CandidateParseError("rotated content is not accepted")
    width = _finite_float(page.get("width"), "page.width")
    height = _finite_float(page.get("height"), "page.height")
    if width <= 0 or height <= 0 or width > 20_000 or height > 20_000:
        raise CandidateParseError("physical page dimensions are outside limits")
    page_limit = (width, height)

    line_elements = [element for element in page.iter() if _local_name(element.tag) == "line"]
    word_count = sum(1 for element in page.iter() if _local_name(element.tag) == "word")
    if len(line_elements) > MAX_LINES or word_count > MAX_WORDS:
        raise CandidateParseError("bbox XML exceeds line or word limits")
    lines: list[dict[str, Any]] = []
    for element in line_elements:
        line_box = _bbox(element, limit=page_limit, kind="line")
        words = [_word_record(child, page_limit) for child in element.iter()
                 if child is not element and _local_name(child.tag) == "word"]
        if not words:
            continue
        for word in words:
            box = word["bbox"]
            if not (line_box["x_min"] <= box["x_min"] < box["x_max"] <= line_box["x_max"] and
                    line_box["y_min"] <= box["y_min"] < box["y_max"] <= line_box["y_max"]):
                raise CandidateParseError("word bbox crosses its line bbox")
        lines.append({"bbox": line_box, "words": words, "text": _normalized(words)})

    # bbox-layout commonly emits labels and each numeric column in separate XML
    # lines. Join only baselines whose vertical centers are within two points;
    # adjacent financial rows remain separate and every word keeps its raw box.
    visual_lines: list[dict[str, Any]] = []
    for raw_line in sorted(lines, key=lambda item: (
            (item["bbox"]["y_min"] + item["bbox"]["y_max"]) / 2,
            item["bbox"]["x_min"])):
        raw_center = (raw_line["bbox"]["y_min"] + raw_line["bbox"]["y_max"]) / 2
        if visual_lines and abs(raw_center - visual_lines[-1]["center_y"]) <= 2.0:
            group = visual_lines[-1]
            group["parts"].append(raw_line)
            group["center_y"] = sum(
                (part["bbox"]["y_min"] + part["bbox"]["y_max"]) / 2 for part in group["parts"]
            ) / len(group["parts"])
        else:
            visual_lines.append({"center_y": raw_center, "parts": [raw_line]})
    lines = []
    for group in visual_lines:
        words = sorted(
            [word for part in group["parts"] for word in part["words"]],
            key=lambda word: (word["bbox"]["x_min"], word["bbox"]["y_min"]),
        )
        lines.append({"bbox": _union(words), "words": words, "text": _normalized(words)})

    title_matches = [line for line in lines if line["text"] == title]
    if len(title_matches) != 1:
        raise CandidateParseError("requested title must match exactly one line on the page")
    title_line = title_matches[0]
    below_title = [line for line in lines if line["bbox"]["y_min"] >= title_line["bbox"]["y_max"]]
    header_matches: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for line in below_title:
        year_words = [word for word in line["words"] if _YEAR_RE.fullmatch(word["text"])]
        texts = [word["text"] for word in year_words]
        if all(year in texts for year in years):
            header_matches.append((line, year_words))
    if not header_matches:
        raise CandidateParseError("requested years must occur together on a header line below the title")
    # Bind the statement title to its nearest following year row. Later year
    # rows can belong to footnote subtables on the same physical page.
    header_line, year_words = min(header_matches, key=lambda item: item[0]["bbox"]["y_min"])
    year_texts = [word["text"] for word in year_words]
    ordered_headers = sorted(year_words, key=lambda word: word["bbox"]["x_min"])
    for left, right in zip(ordered_headers, ordered_headers[1:]):
        if left["bbox"]["x_max"] >= right["bbox"]["x_min"]:
            raise CandidateParseError("year headers overlap or are not monotonic")
    spatial_years = [word["text"] for word in ordered_headers]
    if year_column_indices is None:
        if len(set(year_texts)) != len(year_texts):
            raise CandidateParseError("year header repeats a year")
        requested_positions = [spatial_years.index(year) for year in years]
        if requested_positions != sorted(requested_positions):
            raise CandidateParseError("requested years are not monotonic on the header line")
    else:
        if any(index >= len(ordered_headers) for index in year_column_indices):
            raise CandidateParseError("year_column_indices contains an index outside the year header")
        requested_positions = list(year_column_indices)
        for year, index in zip(years, requested_positions):
            if spatial_years[index] != year:
                raise CandidateParseError(
                    f"year_column_indices position {index} does not contain requested year {year}"
                )

    centers = [_center(word["bbox"]) for word in ordered_headers]
    if len(centers) == 1:
        half_gap = max(ordered_headers[0]["bbox"]["x_max"] - ordered_headers[0]["bbox"]["x_min"], 20.0)
        boundaries = [max(0.0, centers[0] - half_gap), min(width, centers[0] + half_gap)]
    else:
        inner = [(left + right) / 2 for left, right in zip(centers, centers[1:])]
        boundaries = [max(0.0, centers[0] - (centers[1] - centers[0]) / 2), *inner,
                      min(width, centers[-1] + (centers[-1] - centers[-2]) / 2)]
    if any(left >= right for left, right in zip(boundaries, boundaries[1:])):
        raise CandidateParseError("derived year columns are not monotonic and non-overlapping")
    columns = [
        {"year": word["text"], "x_min": boundaries[index], "x_max": boundaries[index + 1],
         "header_word": word}
        for index, word in enumerate(ordered_headers)
    ]
    first_column_x = columns[0]["x_min"]
    next_headers = [line["bbox"]["y_min"] for line in below_title
                    if line["bbox"]["y_min"] >= header_line["bbox"]["y_max"]
                    and sum(bool(_YEAR_RE.fullmatch(word["text"])) for word in line["words"]) >= 2]
    table_bottom = min(next_headers, default=height)

    # Context selectors are exact source text, not semantic labels. Unit context
    # is one complete visual line. A fiscal header may occupy one or two complete
    # visual lines, but must include the selected year-header line. More complex
    # layouts remain explicitly unsupported rather than being inferred.
    selected_context: dict[str, Any] = {}
    if unit_line is not None:
        page_unit_matches = [line for line in lines if line["text"] == unit_line]
        unit_matches = [
            line for line in page_unit_matches
            if (line["bbox"]["y_min"] >= title_line["bbox"]["y_max"]
                and line["bbox"]["y_max"] <= header_line["bbox"]["y_min"])
        ]
        if not unit_matches and page_unit_matches:
            raise CandidateParseError("requested unit_line is outside the selected table title/header region")
        if len(unit_matches) != 1:
            raise CandidateParseError("requested unit_line must match exactly one complete line in the selected table region")
        selected_context["unit_line"] = _context_record(unit_matches)
    if fiscal_period_header is not None:
        page_fiscal_matches: list[tuple[list[dict[str, Any]], list[list[dict[str, Any]]]]] = []
        for index in range(len(lines)):
            for span_length in (1, 2):
                span = lines[index:index + span_length]
                if len(span) != span_length:
                    continue
                for word_groups in _exact_word_spans(span, fiscal_period_header):
                    page_fiscal_matches.append((span, word_groups))
        fiscal_matches = [
            (span, word_groups) for span, word_groups in page_fiscal_matches
            if (any(line is header_line for line in span)
                and span[0]["bbox"]["y_min"] >= title_line["bbox"]["y_max"]
                and span[-1]["bbox"]["y_max"] <= header_line["bbox"]["y_max"])
        ]
        if not fiscal_matches and page_fiscal_matches:
            raise CandidateParseError("requested fiscal_period_header is outside the selected table header region")
        if len(fiscal_matches) != 1:
            raise CandidateParseError(
                "requested fiscal_period_header must match exactly one complete one- or two-line header in the selected table region"
            )
        selected_span, selected_word_groups = fiscal_matches[0]
        selected_context["fiscal_period_header"] = _context_record(selected_span, selected_word_groups)

    output_rows = []
    for requested_label in row_labels:
        matches: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
        for line in lines:
            if (line["bbox"]["y_min"] < header_line["bbox"]["y_max"]
                    or line["bbox"]["y_max"] > table_bottom):
                continue
            label_words = [word for word in line["words"] if word["bbox"]["x_max"] <= first_column_x]
            if _normalized(label_words) == requested_label:
                matches.append((line, label_words))
        if len(matches) != 1:
            raise CandidateParseError(f"row label {requested_label!r} must match exactly one line below the header")
        row_line, label_words = matches[0]
        for word in row_line["words"]:
            if (word["bbox"]["x_max"] > first_column_x
                    and not _NUMBER_RE.fullmatch(word["text"]) and word["text"] != "$"):
                raise CandidateParseError("separate signs, annotations or malformed numeric tokens are not supported")
        numeric_words = [word for word in row_line["words"] if _NUMBER_RE.fullmatch(word["text"])]
        assignments: list[list[dict[str, Any]]] = [[] for _column in columns]
        for word in numeric_words:
            box = word["bbox"]
            owners = [index for index, column in enumerate(columns)
                      if column["x_min"] <= box["x_min"] and box["x_max"] <= column["x_max"]]
            if len(owners) != 1:
                raise CandidateParseError("numeric word has no unique in-column assignment")
            assignments[owners[0]].append(word)
        cells = []
        for requested_year, position in zip(years, requested_positions):
            words = assignments[position]
            if len(words) != 1:
                raise CandidateParseError(
                    f"row {requested_label!r} must have exactly one numeric word for year {requested_year}"
                )
            cells.append({
                "year": requested_year,
                "raw_text": words[0]["text"],
                "original_words": words,
                "bbox": _union(words),
            })
        output_rows.append({
            "label": requested_label,
            "original_label_words": label_words,
            "original_row_words": row_line["words"],
            "bbox": row_line["bbox"],
            "cells": cells,
        })

    selected_columns = []
    for year, position in zip(years, requested_positions):
        column = columns[position]
        selected_column = {
            "year": year,
            "x_min": column["x_min"],
            "x_max": column["x_max"],
            "original_header_word": column["header_word"],
        }
        if year_column_indices is not None:
            selected_column["source_column_index"] = position
        selected_columns.append(selected_column)
    candidate = {
        "candidate_status": "candidate_not_trusted_fact",
        "semantic_status": "unapproved",
        "parser_version": (
            YEAR_COLUMN_SELECTION_PARSER_VERSION
            if year_column_indices is not None else PARSER_VERSION
        ),
        "source_sha256": source_sha256,
        "physical_page": physical_page,
        "page_bbox": {"x_min": 0.0, "y_min": 0.0, "x_max": width, "y_max": height},
        "title": {"raw_text": title_line["text"], "original_words": title_line["words"],
                  "bbox": title_line["bbox"]},
        "year_header": {"original_words": year_words, "bbox": header_line["bbox"]},
        "columns": selected_columns,
        "rows": output_rows,
    }
    if year_column_indices is not None:
        candidate["year_column_indices"] = list(year_column_indices)
    candidate.update(selected_context)
    candidate["structure_hash"] = "sha256:" + hashlib.sha256(_canonical(candidate)).hexdigest()
    return candidate


def _tool_sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def build_replay_manifest(
    *,
    extraction: dict[str, Any],
    bbox_layout_sha256: str,
    candidate: dict[str, Any],
    selectors: dict[str, Any],
) -> dict[str, Any]:
    """Bind source, selectors, tools, extraction profile, XML, and candidate identity."""
    if (_SHA256_RE.fullmatch(bbox_layout_sha256) is None
            or not isinstance(extraction, dict)
            or not isinstance(candidate, dict)
            or not isinstance(selectors, dict)):
        raise ValueError("invalid replay manifest inputs")
    required_extraction = {"source_sha256", "physical_page", "executables", "replay_profile"}
    if not required_extraction.issubset(extraction):
        raise ValueError("extraction metadata is incomplete for replay")
    structure_hash = candidate.get("structure_hash")
    if not isinstance(structure_hash, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", structure_hash) is None:
        raise ValueError("candidate structure hash is invalid")
    manifest = {
        "manifest_version": REPLAY_MANIFEST_VERSION,
        "candidate_parser_version": candidate.get("parser_version"),
        "raw_pdf_sha256": extraction["source_sha256"],
        "physical_page": extraction["physical_page"],
        "bbox_xml_sha256": bbox_layout_sha256,
        "candidate_structure_hash": structure_hash,
        "selectors": selectors,
        "executables": extraction["executables"],
        "extraction_profile": extraction["replay_profile"],
    }
    manifest["manifest_identity"] = "sha256:" + hashlib.sha256(_canonical(manifest)).hexdigest()
    return manifest


def _limit_pdf_process() -> None:
    import resource  # Pure XML inspection remains usable on non-POSIX hosts.
    resource.setrlimit(resource.RLIMIT_CPU, (PDF_CPU_SECONDS, PDF_CPU_SECONDS + 1))
    resource.setrlimit(resource.RLIMIT_AS, (PDF_MEMORY_BYTES, PDF_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_XML_BYTES + 4096, MAX_XML_BYTES + 4096))


def extract_bbox_layout_page(raw: bytes, *, physical_page: int) -> dict[str, Any]:
    """Run approved system pdftotext for one page inside a no-network bwrap."""
    if not isinstance(raw, bytes) or not raw.startswith(b"%PDF-") or not raw or len(raw) > MAX_PDF_BYTES:
        raise CandidateExtractionError("input must be a PDF containing at most 20 MiB")
    if not isinstance(physical_page, int) or isinstance(physical_page, bool) or not 1 <= physical_page <= 100_000:
        raise CandidateExtractionError("physical_page must be an integer from 1 through 100000")
    pdftotext = shutil.which("pdftotext")
    bwrap = shutil.which("bwrap")
    if not pdftotext or not bwrap:
        raise CandidateExtractionError("PDF extraction requires approved system pdftotext and bubblewrap")
    if not pdftotext.startswith("/usr/bin/") or not bwrap.startswith("/usr/bin/"):
        raise CandidateExtractionError("PDF extractor paths must be approved system binaries")
    executables = {
        "pdftotext": {"path": pdftotext, "sha256": _tool_sha256(pdftotext)},
        "bwrap": {"path": bwrap, "sha256": _tool_sha256(bwrap)},
    }
    identity = (
        f"{EXTRACTION_PROFILE_VERSION};pdftotext={executables['pdftotext']['sha256']};"
        f"bwrap={executables['bwrap']['sha256']}"
    )
    extractor_arguments = [
        "pdftotext", "-f", str(physical_page), "-l", str(physical_page), "-enc", "UTF-8",
        "-bbox-layout", "/input/document.pdf", "/output/document.xhtml",
    ]
    font_mounts = [
        path for path in ("/etc/fonts", "/var/cache/fontconfig") if Path(path).exists()
    ]
    sandbox_arguments = [
        "bwrap", "--unshare-all", "--die-with-parent", "--clearenv",
        "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "LANG", "C.UTF-8",
        "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64", "--proc", "/proc", "--dev", "/dev",
        "--tmpfs", "/tmp",
        *[argument for path in font_mounts for argument in ("--ro-bind", path, path)],
        "--ro-bind", "[ephemeral input directory]", "/input",
        "--bind", "[ephemeral output directory]", "/output", "--",
    ]
    with tempfile.TemporaryDirectory(prefix="pdf-bbox-") as directory:
        root = Path(directory)
        input_dir = root / "input"
        output_dir = root / "output"
        input_dir.mkdir(mode=0o700)
        output_dir.mkdir(mode=0o700)
        (input_dir / "document.pdf").write_bytes(raw)
        command = [
            bwrap, "--unshare-all", "--die-with-parent", "--clearenv",
            "--setenv", "PATH", "/usr/bin:/bin", "--setenv", "LANG", "C.UTF-8",
            "--ro-bind", "/usr", "/usr",
            "--symlink", "usr/lib", "/lib",
            "--symlink", "usr/lib64", "/lib64",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
        ]
        for path in font_mounts:
            command.extend(["--ro-bind", path, path])
        command.extend([
            "--ro-bind", str(input_dir), "/input", "--bind", str(output_dir), "/output", "--",
            pdftotext, *extractor_arguments[1:],
        ])
        diagnostics = root / "diagnostics.log"
        try:
            # RLIMIT_FSIZE also bounds diagnostics; an adversarial PDF must not
            # make the parent accumulate an unbounded stderr pipe in memory.
            with diagnostics.open("wb") as log:
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                        timeout=PDF_TIMEOUT_SECONDS, check=False, preexec_fn=_limit_pdf_process)
        except subprocess.TimeoutExpired as exc:
            raise CandidateExtractionError("PDF bbox extraction exceeded the time limit") from exc
        if result.returncode != 0:
            with diagnostics.open("rb") as log:
                detail = log.read(4096).decode("utf-8", "replace").strip() or "pdftotext failed"
            raise CandidateExtractionError("PDF bbox extraction failed: " + detail.splitlines()[0][:300])
        output = output_dir / "document.xhtml"
        try:
            if output.stat().st_size > MAX_XML_BYTES:
                raise CandidateExtractionError("PDF bbox output exceeds the 2-million-byte limit")
            xml = output.read_bytes()
        except OSError as exc:
            raise CandidateExtractionError("PDF bbox extraction did not produce readable output") from exc
    if not xml or b"\x00" in xml:
        raise CandidateExtractionError("PDF bbox extraction produced empty or NUL-containing output")
    return {
        "xml": xml,
        "tool_identity": identity,
        "executables": executables,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "physical_page": physical_page,
        "limits": {
            "input_bytes": MAX_PDF_BYTES,
            "output_bytes": MAX_XML_BYTES,
            "timeout_seconds": PDF_TIMEOUT_SECONDS,
            "cpu_seconds": PDF_CPU_SECONDS,
            "memory_bytes": PDF_MEMORY_BYTES,
            "network": "bubblewrap --unshare-all",
            "environment": "cleared; fixed PATH and LANG only",
            "diagnostic_bytes": MAX_XML_BYTES + 4096,
        },
        "replay_profile": {
            "profile_version": EXTRACTION_PROFILE_VERSION,
            "extractor_arguments": extractor_arguments,
            "sandbox_arguments": sandbox_arguments,
            "environment": {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "other": "cleared"},
            "resource_bounds": {
                "input_bytes": MAX_PDF_BYTES,
                "output_bytes": MAX_XML_BYTES,
                "diagnostic_bytes": MAX_XML_BYTES + 4096,
                "timeout_seconds": PDF_TIMEOUT_SECONDS,
                "cpu_seconds": PDF_CPU_SECONDS,
                "address_space_bytes": PDF_MEMORY_BYTES,
                "network": "unshared",
                "pages": MAX_PAGES,
                "xml_elements": MAX_ELEMENTS,
                "xml_attributes": MAX_ATTRIBUTES,
                "xml_lines": MAX_LINES,
                "xml_words": MAX_WORDS,
            },
        },
    }
