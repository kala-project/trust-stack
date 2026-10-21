"""Deterministic span extraction for Answer Artifact atomic provenance.

This module only identifies exact ranges and citation locality.  It deliberately does
not decide whether a source entails a claim; answer_artifacts applies frozen-source
checks to these candidates.
"""
from __future__ import annotations

import re
from typing import Any


ANCHOR_SCHEMA_VERSION = "atomic-provenance/1.0"
OFFSET_UNIT = "unicode_code_points"
_CITATION = re.compile(r"\[(claim|evidence|passage|calculation|query):([^\]\s]+)\]")
_NUMBER = re.compile(r"(?<![\d.,])[+\-−]?\d[\d,]*(?:\.\d+)?(?:\s*(?:%|％|percent\b|per\s+cent\b|个百分点|百分点|percentage\s+points?))?", re.I)
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
_DATE_AROUND = re.compile(r"(?:FY\s*$|Q\s*$|Q[1-4]\s*$|财年\s*$|年\s*$)", re.I)
_CLAUSE_BREAK = re.compile(r"(?:[。！？!?；;]|\.(?!\d))(?:[”’\"']?)(?=\s|$|\[(?:claim|evidence|passage|calculation|query):)|\n+")
_INLINE_CLAUSE = re.compile(r"[,，](?=\s*(?:而|但|但是|同时|其中|因此|所以|because\b|while\b|but\b|however\b))", re.I)
_CAUSAL = re.compile(r"由于|因为|导致|因此|所以|归因于|because|due\s+to|caused?|result(?:ed)?\s+in|driven\s+by", re.I)


def utf16_offset(text: str, codepoint_offset: int) -> int:
    """Convert a Python/code-point boundary to a JavaScript UTF-16 boundary."""
    if not isinstance(codepoint_offset, int) or codepoint_offset < 0 or codepoint_offset > len(text):
        raise ValueError("codepoint offset is outside text")
    return len(text[:codepoint_offset].encode("utf-16-le")) // 2


def validate_annotations(text: str, annotations: Any) -> tuple[list[dict], list[str]]:
    """Accept future model spans only when every boundary and exact text is verifiable."""
    if annotations is None:
        return [], []
    if not isinstance(annotations, list):
        return [], ["assertion_annotations_not_a_list"]
    accepted, warnings = [], []
    occupied: list[tuple[int, int]] = []
    for index, item in enumerate(annotations):
        if not isinstance(item, dict):
            warnings.append(f"assertion_annotation_{index}_invalid")
            continue
        start, end, exact = item.get("start"), item.get("end"), item.get("text")
        if (not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int)
                or isinstance(end, bool) or not isinstance(exact, str) or start < 0
                or end <= start or end > len(text) or text[start:end] != exact
                or _CITATION.search(exact)):
            warnings.append(f"assertion_annotation_{index}_invalid")
            continue
        if any(start < prior_end and prior_start < end for prior_start, prior_end in occupied):
            warnings.append(f"assertion_annotation_{index}_overlap")
            continue
        occupied.append((start, end))
        accepted.append({"start": start, "end": end, "text": exact,
                         "annotation_id": item.get("id") if isinstance(item.get("id"), str) else None})
    return accepted, warnings


def _finding_ranges(text: str, findings: list[dict]) -> list[tuple[int, int, str]]:
    """Locate findings in order; never bind repeated text by a global first match."""
    result, cursor = [], 0
    for finding in findings:
        value = finding.get("text")
        identifier = finding.get("id")
        if not isinstance(value, str) or not isinstance(identifier, str):
            continue
        start = text.find(value, cursor)
        if start >= 0:
            end = start + len(value)
        else:
            # Table grouping may normalize a blank-line separator.  Bound the exact
            # original region by its first/last non-empty lines without inventing text.
            lines = [line.strip() for line in value.splitlines() if line.strip()]
            start = text.find(lines[0], cursor) if lines else -1
            last = text.find(lines[-1], max(cursor, start)) if start >= 0 and lines else -1
            if start < 0 or last < 0:
                continue
            end = last + len(lines[-1])
        result.append((start, end, identifier))
        cursor = end
    return result


def _finding_id(start: int, end: int, ranges: list[tuple[int, int, str]]) -> str | None:
    matches = [identifier for left, right, identifier in ranges if left <= start and end <= right]
    return matches[0] if len(matches) == 1 else None


def _references(value: str) -> list[tuple[str, str]]:
    return list(dict.fromkeys(_CITATION.findall(value)))


def _visible_bounds(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and (text[start].isspace() or text[start] in "#>*_`-"):
        start += 1
    while end > start and (text[end - 1].isspace() or text[end - 1] in "*_`"):
        end -= 1
    return (start, end) if end > start else None


def _prose_scopes(text: str, excluded: list[tuple[int, int]]) -> list[dict]:
    scopes = []
    boundaries = [0]
    for match in _CLAUSE_BREAK.finditer(text):
        boundaries.append(match.end())
    boundaries.append(len(text))
    rough = []
    for left, right in zip(boundaries, boundaries[1:]):
        cursor = left
        for match in _INLINE_CLAUSE.finditer(text, left, right):
            rough.append((cursor, match.start()))
            cursor = match.end()
        rough.append((cursor, right))
    for left, right in rough:
        if any(left < table_end and table_start < right for table_start, table_end in excluded):
            continue
        leading = re.match(
            r"\s*(?:(?:\[(?:claim|evidence|passage|calculation|query):[^\]\s]+\])\s*)*",
            text[left:right],
        )
        statement_left = left + (leading.end() if leading else 0)
        # A citation after terminal punctuation belongs to the bounded statement
        # immediately before it, but never to a neighbouring sentence.
        following = re.match(r"\s*(?:(?:\[(?:claim|evidence|passage|calculation|query):[^\]\s]+\])\s*)+", text[right:])
        reference_end = right + (following.end() if following else 0)
        bounds = _visible_bounds(text, statement_left, right)
        if not bounds:
            continue
        visible_start, visible_end = bounds
        visible = _CITATION.sub("", text[visible_start:visible_end]).strip()
        if not visible or (len(visible) < 100 and visible.rstrip("：:").startswith("#")):
            continue
        scopes.append({"start": visible_start, "end": visible_end, "text": text[visible_start:visible_end],
                       "reference_text": text[statement_left:reference_end],
                       "context": text[statement_left:right], "table_context": None})
    return scopes


def _table_scopes(text: str) -> tuple[list[dict], list[tuple[int, int]]]:
    lines = list(re.finditer(r"^.*(?:\n|$)", text, re.M))
    scopes, excluded = [], []
    index = 0
    while index < len(lines):
        raw = lines[index].group().rstrip("\n")
        if not raw.lstrip().startswith("|"):
            index += 1
            continue
        group = []
        while index < len(lines):
            value = lines[index].group().rstrip("\n")
            if not value.lstrip().startswith("|"):
                break
            group.append((lines[index].start(), value))
            index += 1
        if len(group) < 2:
            continue
        excluded.append((group[0][0], group[-1][0] + len(group[-1][1])))
        header = [cell.strip() for cell in group[0][1].strip().strip("|").split("|")]
        separator = all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in group[1][1].strip().strip("|").split("|"))
        if not separator:
            continue
        footer_end = group[-1][0] + len(group[-1][1])
        footer_match = re.match(r"(?:\s*\n){1,2}([^\n]*(?:来源|数据|source|figure|number)[^\n]*\[(?:claim|evidence|passage|calculation|query):[^\]\s]+\][^\n]*)", text[footer_end:], re.I)
        footer = footer_match.group(1) if footer_match else ""
        footer_refs = _references(footer)
        for row_start, row in group[2:]:
            cells = list(re.finditer(r"(?<=\|)([^|]*)(?=\|)", row))
            values = [match.group(1).strip() for match in cells]
            if not cells:
                continue
            row_header = values[0] if values else ""
            for column, match in enumerate(cells):
                content = match.group(1)
                trim_left = len(content) - len(content.lstrip())
                trim_right = len(content.rstrip())
                start = row_start + match.start(1) + trim_left
                end = row_start + match.start(1) + trim_right
                if end <= start:
                    continue
                context = " | ".join(part for part in (header[column] if column < len(header) else "", row_header, text[start:end]) if part)
                refs = _references(text[start:end]) or footer_refs
                scopes.append({"start": start, "end": end, "text": text[start:end],
                               "reference_text": " ".join(f"[{kind}:{identifier}]" for kind, identifier in refs),
                               "context": context, "table_context": {"column_header": header[column] if column < len(header) else None,
                                                                      "row_header": row_header}})
    return scopes, excluded


def build_candidates(text: str, findings: list[dict], annotations: Any = None) -> tuple[list[dict], list[str]]:
    """Return exact, non-authoritative candidates with local citation scopes."""
    ranges = _finding_ranges(text, findings)
    table_scopes, excluded = _table_scopes(text)
    scopes = _prose_scopes(text, excluded) + table_scopes
    annotated, warnings = validate_annotations(text, annotations)
    if annotated:
        assertion_scopes = [{"start": item["start"], "end": item["end"], "text": item["text"],
                             "reference_text": item["text"], "context": item["text"],
                             "table_context": None, "annotation_id": item["annotation_id"]}
                            for item in annotated]
    else:
        assertion_scopes = [scope for scope in scopes if scope["table_context"] is None]
    candidates = []
    citation_ranges = [(m.start(), m.end()) for m in _CITATION.finditer(text)]
    date_ranges = [(m.start(), m.end()) for m in re.finditer(
        r"(?<!\d)(?:19|20)\d{2}[-/]\d{1,2}(?:[-/]\d{1,2})?(?!\d)", text)]
    for scope in scopes:
        for match in _NUMBER.finditer(text, scope["start"], scope["end"]):
            start, end = match.span()
            raw = match.group().strip()
            start += len(match.group()) - len(match.group().lstrip())
            end = start + len(raw)
            plain = re.sub(r"\s*(?:%|％|percent\b|per\s+cent\b|个百分点|百分点|percentage\s+points?)$", "", raw, flags=re.I).replace("−", "-").replace(",", "")
            if (any(left <= start < right for left, right in citation_ranges)
                    or any(left <= start and end <= right for left, right in date_ranges)
                    or _YEAR.fullmatch(plain.lstrip("+-"))):
                continue
            before = text[max(scope["start"], start - 8):start]
            after = text[end:min(scope["end"], end + 3)]
            if _DATE_AROUND.search(before) or re.match(r"\s*(?:年|月|日|财年|Q[1-4])", after, re.I):
                continue
            candidates.append({"kind": "number", "start": start, "end": end, "text": text[start:end],
                               "numeric_text": plain, "finding_id": _finding_id(start, end, ranges),
                               "local_references": _references(scope["reference_text"]),
                               "context": scope["context"], "table_context": scope["table_context"]})
    for scope in assertion_scopes:
        clean = _CITATION.sub("", scope["text"])
        if not re.search(r"[A-Za-z\u4e00-\u9fff]", clean):
            continue
        # Keep the exact original assertion span; citation markers are excluded by
        # trimming a citation-only tail rather than creating synthetic text.
        tail = re.search(r"\s*(?:\[(?:claim|evidence|passage|calculation|query):[^\]\s]+\]\s*)+$", scope["text"])
        end = scope["start"] + (tail.start() if tail else len(scope["text"]))
        bounds = _visible_bounds(text, scope["start"], end)
        if not bounds:
            continue
        start, end = bounds
        candidates.append({"kind": "assertion", "start": start, "end": end, "text": text[start:end],
                           "finding_id": _finding_id(start, end, ranges),
                           "local_references": _references(scope["reference_text"]),
                           "context": scope["context"], "table_context": scope["table_context"],
                           "causal": bool(_CAUSAL.search(text[start:end])),
                           "annotation_id": scope.get("annotation_id")})
    candidates.sort(key=lambda item: (item["start"], 0 if item["kind"] == "assertion" else 1, item["end"]))
    return candidates, warnings
