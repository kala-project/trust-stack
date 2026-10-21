"""Deterministic, non-executing HTML-to-evidence parsing (standard library only)."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import re
from typing import Optional

PARSER_VERSION = "visible-html-tables/1.0.0"
_SKIP_TAGS = {"script", "style", "noscript"}


class ParseError(ValueError):
    """Raised when an allowed source does not match its expected structure."""


@dataclass(frozen=True)
class ParsedTable:
    index: int
    rows: list[list[str]]
    start: int
    end: int
    row_spans: list[tuple[int, int]]


@dataclass(frozen=True)
class ParsedDocument:
    text: str
    tables: list[ParsedTable]
    parser_version: str = PARSER_VERSION


class _VisibleHTMLParser(HTMLParser):
    """Collect text and tables without executing or retaining source markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.length = 0
        self.skip_depth = 0
        self.tables: list[dict] = []
        self.table: Optional[dict] = None
        self.row: Optional[dict] = None
        self.cell: Optional[list[str]] = None

    def _append(self, value: str) -> Optional[tuple[int, int]]:
        value = " ".join(value.split())
        if not value:
            return None
        if self.parts:
            self.parts.append(" ")
            self.length += 1
        start = self.length
        self.parts.append(value)
        self.length += len(value)
        return start, self.length

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag == "table":
            if self.table is not None:
                raise ParseError("nested tables are not supported")
            self.table = {"rows": [], "spans": [], "start": None, "end": None}
        elif tag == "tr" and self.table is not None:
            self.row = {"cells": [], "start": None, "end": None}
        elif tag in ("th", "td") and self.row is not None:
            self.cell = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        span = self._append(data)
        if span is None:
            return
        if self.cell is not None:
            self.cell.append(" ".join(data.split()))
        if self.row is not None:
            if self.row["start"] is None:
                self.row["start"] = span[0]
            self.row["end"] = span[1]
        if self.table is not None:
            if self.table["start"] is None:
                self.table["start"] = span[0]
            self.table["end"] = span[1]

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            if self.skip_depth:
                self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag in ("th", "td") and self.cell is not None and self.row is not None:
            self.row["cells"].append(" ".join(x for x in self.cell if x).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None and self.table is not None:
            if any(self.row["cells"]):
                self.table["rows"].append(self.row["cells"])
                self.table["spans"].append((self.row["start"], self.row["end"]))
            self.row = None
            self.cell = None
        elif tag == "table" and self.table is not None:
            if self.table["rows"]:
                self.tables.append(self.table)
            self.table = None
            self.row = None
            self.cell = None


def parse_html(raw: bytes) -> ParsedDocument:
    """Decode UTF-8 HTML and return normalized visible text plus positioned tables."""
    try:
        html = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError("source is not UTF-8 HTML") from exc
    parser = _VisibleHTMLParser()
    try:
        parser.feed(html)
        parser.close()
    except (AssertionError, ParseError) as exc:
        raise ParseError(f"invalid or unsupported HTML: {exc}") from exc
    text = "".join(parser.parts)
    tables = [
        ParsedTable(i, item["rows"], item["start"], item["end"], item["spans"])
        for i, item in enumerate(parser.tables)
    ]
    return ParsedDocument(text=text, tables=tables)


def parse_decimal(value: str) -> Decimal:
    """Parse issuer table numbers, including parenthesized negatives."""
    cleaned = value.strip().replace("$", "").replace(",", "").replace(" ", "")
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    if negative:
        cleaned = cleaned[1:-1]
    if cleaned in {"", "-", "—"}:
        raise ParseError(f"not a numeric value: {value!r}")
    try:
        number = Decimal(cleaned)
    except InvalidOperation as exc:
        raise ParseError(f"not a numeric value: {value!r}") from exc
    return -number if negative else number


def _find_unique_table(document: ParsedDocument, predicate, description: str) -> ParsedTable:
    matches = [table for table in document.tables if predicate(table)]
    if len(matches) != 1:
        raise ParseError(f"expected exactly one {description} table, found {len(matches)}")
    return matches[0]


def microsoft_quarter_gaap(document: ParsedDocument, current_year: int) -> dict:
    """Extract the two GAAP rows from a validated Microsoft quarter table."""
    prior_year = current_year - 1

    def matches(table: ParsedTable) -> bool:
        flat = " | ".join(" | ".join(row) for row in table.rows[:3])
        return (
            "Three Months Ended June 30," in flat
            and "Twelve Months Ended" not in flat
            and "Revenue" in flat
            and "Operating Income" in flat
            and "Diluted Earnings per Share" in flat
            and any(row and row[0] == f"{prior_year} As Reported (GAAP)" for row in table.rows)
            and any(row and row[0] == f"{current_year} As Reported (GAAP)" for row in table.rows)
        )

    table = _find_unique_table(document, matches, "Microsoft quarterly GAAP reconciliation")
    header = next((r for r in table.rows if "Revenue" in r and "Operating Income" in r), None)
    if header is None or header[:5] != [
        "($ in millions, except per share amounts)", "Revenue", "Operating Income",
        "Net Income", "Diluted Earnings per Share",
    ]:
        raise ParseError("unexpected Microsoft reconciliation header")
    output = {"table": table, "years": {}}
    for year in (prior_year, current_year):
        label = f"{year} As Reported (GAAP)"
        indexes = [i for i, row in enumerate(table.rows) if row and row[0] == label]
        if len(indexes) != 1:
            raise ParseError(f"missing or duplicate GAAP row for {year}")
        index = indexes[0]
        row = table.rows[index]
        if len(row) != 5:
            raise ParseError(f"unexpected GAAP row width for {year}")
        output["years"][year] = {
            "revenue": parse_decimal(row[1]),
            "operating_income": parse_decimal(row[2]),
            "eps_diluted": parse_decimal(row[4]),
            "row_index": index,
            "span": table.row_spans[index],
        }
    gaap = next((r for r in table.rows if r and r[0] == "Percentage Change Y/Y (GAAP)"), None)
    constant = next((r for r in table.rows if r and r[0] == "Percentage Change Y/Y Constant Currency"), None)
    percentage = re.compile(r"-?\d+(?:\.\d+)?%")
    if gaap is None or constant is None or len(gaap) != 5 or len(constant) != 5:
        raise ParseError("missing or malformed reported/constant-currency growth rows")
    if any(percentage.fullmatch(value) is None for value in gaap[1:] + constant[1:]):
        raise ParseError("non-numeric reported/constant-currency growth value")
    output["reported_growth"] = gaap[1] if gaap else None
    output["constant_currency_growth"] = constant[1] if constant else None
    return output


_MICROSOFT_CAPITAL_RETURN_RE = re.compile(
    r"Microsoft returned \$(?P<amount>\d+(?:\.\d+)?) billion to shareholders in the form of "
    r"(?:dividends and share repurchases|share repurchases and dividends) in the fourth quarter "
    r"of fiscal year (?P<year>\d{4})\."
)


def microsoft_shareholder_returns(document: ParsedDocument, current_year: int) -> dict:
    """Extract Microsoft's current-year Q4/year cash outflows and reported Q4 total.

    This parser is deliberately tied to the structure and wording of the loaded
    Microsoft FY2024/FY2025 Q4 releases. Cash-flow components and the issuer's
    combined capital-return sentence remain separate measurements.
    """
    prior_year = current_year - 1

    def matches(table: ParsedTable) -> bool:
        return (
            len(table.rows) == 41
            and table.rows[0] == ["", "Three Months Ended June 30,", "", "Twelve Months Ended June 30,"]
            and table.rows[1] == ["", str(current_year), "", str(prior_year), "", str(current_year), "", str(prior_year)]
            and any(row and row[0] == "Financing" for row in table.rows)
            and any(row and row[0] == "Common stock repurchased" for row in table.rows)
            and any(row and row[0] == "Common stock cash dividends paid" for row in table.rows)
        )

    table = _find_unique_table(document, matches, "Microsoft statements of cash flows")
    rows = {}
    for metric, label in (
        ("payments_for_repurchase_of_common_stock", "Common stock repurchased"),
        ("payments_of_dividends", "Common stock cash dividends paid"),
    ):
        indexes = [index for index, row in enumerate(table.rows) if row and row[0] == label]
        if len(indexes) != 1:
            raise ParseError(f"missing or duplicate Microsoft cash-flow row: {label}")
        row_index = indexes[0]
        row = table.rows[row_index]
        if len(row) != 8 or any(row[index] != "" for index in (2, 4, 6)):
            raise ParseError(f"unexpected Microsoft cash-flow row shape: {label}")
        quarter_raw = parse_decimal(row[1])
        annual_raw = parse_decimal(row[5])
        if quarter_raw >= 0 or annual_raw >= 0:
            raise ParseError(f"expected parenthesized cash outflows for: {label}")
        rows[metric] = {
            "label": label,
            "row_index": row_index,
            "span": table.row_spans[row_index],
            "quarter_raw": quarter_raw,
            "quarter": abs(quarter_raw),
            "annual_raw": annual_raw,
            "annual": abs(annual_raw),
        }

    reported_matches = list(_MICROSOFT_CAPITAL_RETURN_RE.finditer(document.text))
    if len(reported_matches) != 1:
        raise ParseError(
            f"expected exactly one Microsoft capital-return sentence, found {len(reported_matches)}"
        )
    reported = reported_matches[0]
    if int(reported.group("year")) != current_year:
        raise ParseError("Microsoft capital-return sentence has unexpected fiscal year")
    return {
        "table": table,
        "rows": rows,
        "reported_total": parse_decimal(reported.group("amount")) * Decimal(1000),
        "reported_total_span": reported.span(),
    }


def microsoft_segments(document: ParsedDocument, years: tuple[int, int, int]) -> dict:
    """Extract one strictly-shaped Microsoft annual segment table."""
    expected_names = ["Productivity and Business Processes", "Intelligent Cloud", "More Personal Computing"]

    def matches(table: ParsedTable) -> bool:
        return bool(table.rows) and "Year Ended June 30," in " ".join(table.rows[0])

    table = _find_unique_table(document, matches, "Microsoft annual segment")
    if len(table.rows) != 12 or table.rows[1][:3] != [str(y) for y in years]:
        raise ParseError("unexpected Microsoft segment years or shape")
    sections: dict[str, dict] = {}
    for section, start in (("revenue", 3), ("operating_income", 8)):
        rows = table.rows[start:start + 3]
        if [row[1] for row in rows] != expected_names:
            raise ParseError(f"unexpected {section} segment rows")
        sections[section] = {}
        for row_index, row in zip(range(start, start + 3), rows):
            if len(row) != 5:
                raise ParseError("unexpected Microsoft segment row width")
            sections[section][row[1]] = {
                "values": {year: parse_decimal(row[pos + 2]) for pos, year in enumerate(years)},
                "row_index": row_index,
                "span": table.row_spans[row_index],
            }
    return {"table": table, "sections": sections}


_APPLE_RESULT_RE = re.compile(
    r"Apple today announced financial results for its fiscal (?P<year>\d{4}) fourth quarter ended "
    r"(?P<end>[A-Z][a-z]+ \d{1,2}, \d{4})\. The Company posted quarterly revenue of "
    r"\$(?P<revenue>\d+(?:\.\d+)?) billion, up (?P<revenue_growth>\d+) percent year over year\. "
    r"Diluted earnings per share was \$(?P<eps>\d+(?:\.\d+)?), up (?P<eps_growth>\d+) percent "
    r"year over year on an adjusted basis\. 1"
)
_APPLE_FOOTNOTE_RE = re.compile(
    r"Non-GAAP measure excluding the one-time income tax charge recognized during the fourth quarter "
    r"of 2024 related to the impact of the reversal of the European General Court’s State Aid decision\. "
    r"See the section titled .Reconciliation of 2024 Non-GAAP to GAAP Results of Operations. at the end "
    r"of the accompanying financial statements\."
)


def apple_release(document: ParsedDocument) -> dict:
    """Extract Apple newsroom figures and the qualifying adjusted-comparison footnote."""
    match = _APPLE_RESULT_RE.search(document.text)
    footnote = _APPLE_FOOTNOTE_RE.search(document.text)
    if match is None or footnote is None:
        raise ParseError("unexpected Apple release result or adjusted-basis footnote")
    try:
        period_end = datetime.strptime(match.group("end"), "%B %d, %Y").date().isoformat()
    except ValueError as exc:
        raise ParseError("invalid Apple quarter end date") from exc
    return {
        "fiscal_year": int(match.group("year")),
        "period_end": period_end,
        "revenue": parse_decimal(match.group("revenue")),
        "eps_diluted": parse_decimal(match.group("eps")),
        "revenue_growth": match.group("revenue_growth"),
        "eps_growth": match.group("eps_growth"),
        "span": match.span(),
        "footnote_span": footnote.span(),
    }
