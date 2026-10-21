"""Strict deterministic evidence, calculation, and binding checks.

The core consumes explicit frozen snapshots and authoritative tool results.  It has no
dependency on ChatService, artifact presentation, HTTP, or report readers.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from typing import Any, Protocol, Sequence

CHECK_INPUT_SCHEMA_VERSION = "evidence-check-input/1"
SCHEMA_VERSION = "answer-artifact/1.0"
ALGORITHM_VERSION = "answer-artifact-projection/2.10.0"


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_id(prefix: str, *parts: Any) -> str:
    raw = _canonical([SCHEMA_VERSION, ALGORITHM_VERSION, *parts]).encode("utf-8")
    return f"{prefix}_{__import__('hashlib').sha256(raw).hexdigest()}"

_CITATION = re.compile(r"\[(claim|evidence|passage|calculation|query):([^\]\s]+)\]")
_PARAGRAPH = re.compile(r"\n\s*\n+")
_CALCULATION_LANGUAGE = re.compile(
    r"(?i)(同比|环比|增长(?:率)?|下降(?:了|率)?|增加(?:了)?|减少(?:了)?|差额|相差|合计|总计|"
    r"year[- ]over[- ]year|yoy|growth|grew|rose|fell|increas(?:e|ed)|decreas(?:e|ed)|declin(?:e|ed)|"
    r"difference|percent(?:age)? change|sum|total|斜率|截距|slope|intercept|R²|R-squared)"
)
_ABSENCE_ASSERTION = re.compile(
    r"(没有披露|未披露|并未披露|未提供(?:任何)?解释|没有提供(?:任何)?解释|无调节表|未提供调节表|"
    r"(?:未|没有)(?:给出|提供)(?:明确的?|任何)?(?:调节表|解释)|"
    r"not disclosed|did not disclose|no disclosure|provided no explanation|no reconciliation table)", re.I
)
_SCOPED_SEARCH = re.compile(
    r"(在|就|仅限于).{0,24}(已检索|所检索|检索到|本次检索|上述资料|这些资料)|"
    r"(?:现有|本次|这些|所检索)(?:材料|资料|检索)|"
    r"(within|in) (?:the )?(?:searched|retrieved|reviewed) (?:material|documents?|sources?)", re.I
)

# These aliases are intentionally disjoint. English "income" alone is never a
# metric: revenue and operating income are different rows.
_METRIC_PATTERNS = {
    "revenue": (
        re.compile(r"营业收入|营业额|营收|收入"),
        re.compile(r"(?<!operating )(?<!net )\brevenues?\b", re.I),
    ),
    "operating_income": (
        re.compile(r"营业利润|经营利润"),
        re.compile(r"\boperating\s+(?:income|profit)\b", re.I),
    ),
    "net_income": (re.compile(r"净利润|净收益"), re.compile(r"\bnet\s+income\b", re.I)),
    "eps_diluted": (re.compile(r"摊薄每股收益|稀释每股收益"), re.compile(r"\bdiluted\s+(?:EPS|earnings?\s+per\s+share)\b", re.I)),
    "capital_expenditures": (
        re.compile(r"资本(?:性)?支出|物业及设备购置|购置物业及设备"),
        re.compile(r"\bcap(?:ital\s+)?ex(?:penditures?)?\b|\badditions?\s+to\s+property\s+and\s+equipment\b", re.I),
    ),
}
_METRIC_LABELS = {
    "revenue": "营业收入", "operating_income": "营业利润",
    "net_income": "净利润", "eps_diluted": "摊薄每股收益",
    "capital_expenditures": "资本支出",
}


def _index(items: Any, key: str) -> dict[str, dict]:
    if not isinstance(items, list):
        return {}
    return {
        item[key]: item for item in items
        if isinstance(item, dict) and isinstance(item.get(key), str)
    }


def _decimal_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return value if parsed.is_finite() else None


def _period_label(fact: dict) -> str:
    start, end = fact.get("period_start"), fact.get("period_end")
    if isinstance(start, str) and isinstance(end, str):
        return f"{start} – {end}"
    if isinstance(end, str):
        return end
    if isinstance(start, str):
        return start
    return str(fact.get("period_kind") or "Unspecified period")


def _decimal_scale(value: Decimal, power: int) -> Decimal:
    """Shift a finite Decimal exponent without ambient-context rounding."""
    sign, digits, exponent = value.as_tuple()
    return Decimal((sign, digits, exponent + power))


def _currency_unit(unit: Any) -> tuple[str, int] | None:
    """Return a strict currency and base-10 scale for one explicit unit label."""
    if not isinstance(unit, str):
        return None
    normalized = re.sub(r"\s+", " ", unit.strip()).lower()
    units = {
        "usd": ("USD", 0), "美元": ("USD", 0),
        "usd million": ("USD", 6), "usd millions": ("USD", 6), "百万美元": ("USD", 6),
        "usd billion": ("USD", 9), "usd billions": ("USD", 9), "十亿美元": ("USD", 9),
        "亿美元": ("USD", 8), "万美元": ("USD", 4), "万亿美元": ("USD", 12),
        "cny": ("CNY", 0), "rmb": ("CNY", 0), "人民币": ("CNY", 0),
        "cny million": ("CNY", 6), "cny millions": ("CNY", 6),
        "rmb million": ("CNY", 6), "rmb millions": ("CNY", 6), "百万元人民币": ("CNY", 6),
        "cny billion": ("CNY", 9), "cny billions": ("CNY", 9),
        "rmb billion": ("CNY", 9), "rmb billions": ("CNY", 9), "十亿元人民币": ("CNY", 9),
        "亿元人民币": ("CNY", 8), "万元人民币": ("CNY", 4), "万亿元人民币": ("CNY", 12),
    }
    return units.get(normalized)


_DECLARED_CURRENCY_UNIT = re.compile(
    r"(?i)(?<![A-Za-z])(?:USD|CNY|RMB|EUR|JPY|GBP|HKD|CAD|AUD|NZD|CHF|SGD|KRW|INR|SEK|NOK|BRL|ZAR)(?:\s+(?:millions?|billions?))?(?![A-Za-z])|"
    r"(?:百万元|十亿元|亿元|万元|万亿元)?人民币|(?:百万|十亿|亿|万|万亿)?(?:美元|欧元|日元|英镑|港元)"
)


def _declared_currency_units(text: str) -> set[tuple[str, int]] | None:
    parsed = [_currency_unit(token) for token in _DECLARED_CURRENCY_UNIT.findall(text)]
    # An explicit unsupported currency is not an absent unit. Never fall back
    # to a different common currency merely because conversion is unsupported.
    if any(unit is None for unit in parsed):
        return None
    return set(parsed)


def _currency_amount(value: Any, unit: Any) -> tuple[str, Decimal] | None:
    parsed_unit = _currency_unit(unit)
    decimal_text = _decimal_text(value)
    if parsed_unit is None or decimal_text is None:
        return None
    currency, power = parsed_unit
    return currency, _decimal_scale(Decimal(decimal_text), power)


def _money_amounts(text: str) -> set[tuple[str, Decimal]]:
    """Return explicit currency amounts for a conservative lexical-only check."""
    result: set[tuple[str, Decimal]] = set()
    text = text.replace("−", "-")
    number = r"([+-]?\d[\d,]*(?:\.\d+)?)"
    text = re.sub(rf"(?i){number}\s*(USD|CNY|RMB)\s+(millions?|billions?|trillions?)\b", lambda m: f"{m[2]} {m[1]} {m[3].lower().rstrip('s')}", text)
    currency_first = re.compile(
        rf"(?i)(US\$|USD|RMB|CNY|\$|¥|￥)\s*{number}\s*(billions?|millions?|trillions?|bn|mn|万亿|十亿|百万|亿|万)?"
    )
    number_first = re.compile(
        rf"(?i){number}\s*(billions?|millions?|trillions?|bn|mn|万亿元|十亿元|百万元|亿元|万元|万亿|十亿|百万|亿|万)?\s*(美元|人民币|USD|RMB|CNY)"
    )
    multipliers = {
        None: Decimal(1), "": Decimal(1), "billion": Decimal(10) ** 9,
        "bn": Decimal(10) ** 9, "million": Decimal(10) ** 6,
        "mn": Decimal(10) ** 6, "trillion": Decimal(10) ** 12,
        "亿": Decimal(10) ** 8, "万": Decimal(10) ** 4,
        "百万": Decimal(10) ** 6, "十亿": Decimal(10) ** 9, "万亿": Decimal(10) ** 12,
        "亿元": Decimal(10) ** 8, "万元": Decimal(10) ** 4,
        "百万元": Decimal(10) ** 6, "十亿元": Decimal(10) ** 9, "万亿元": Decimal(10) ** 12,
    }

    def add(currency: str, raw: str, scale: str | None) -> None:
        currency = "USD" if currency.upper() in {"US$", "USD", "$"} or currency == "美元" else "CNY"
        try:
            multiplier = multipliers.get((scale or "").lower().rstrip("s"), Decimal(1))
            result.add((currency, _decimal_scale(Decimal(raw.replace(",", "")), multiplier.adjusted())))
        except InvalidOperation:
            pass

    for match in currency_first.finditer(text):
        add(match.group(1), match.group(2), match.group(3))
    for match in number_first.finditer(text):
        add(match.group(3), match.group(1), match.group(2))
    return result


def _readable_money(currency: str, value: Decimal) -> str:
    """Render a base-currency amount without exposing an unreadable raw integer."""
    labels = {"USD": "美元", "CNY": "人民币"}
    for divisor, suffix in ((Decimal(10) ** 8, "亿"), (Decimal(10) ** 4, "万")):
        scaled = value / divisor
        if scaled == scaled.quantize(Decimal("0.01")):
            return f"{format(scaled.normalize(), 'f')}{suffix}{labels.get(currency, currency)}"
    rendered = format(value, ",f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return f"{currency} {rendered}"


def _quote_contains_fact_value(quote: str, value: str) -> bool:
    """Check a typed fact's source row, allowing accounting parentheses/signs only."""
    try:
        expected = abs(Decimal(value))
    except InvalidOperation:
        return False
    for raw in re.findall(r"\(?\s*([+-]?\d[\d,]*(?:\.\d+)?)\s*\)?", quote.replace("−", "-")):
        try:
            if abs(Decimal(raw.replace(",", ""))) == expected:
                return True
        except InvalidOperation:
            pass
    return False


def _metric_mentions(text: str) -> set[str]:
    return {metric for metric, patterns in _METRIC_PATTERNS.items()
            if any(pattern.search(text) for pattern in patterns)}


def _normalized_metric_label(text: Any) -> str | None:
    """Return a conservative literal identity for an otherwise unknown table metric."""
    if not isinstance(text, str):
        return None
    clean = _CITATION.sub("", text)
    clean = _DECLARED_CURRENCY_UNIT.sub("", clean)
    clean = re.sub(r"(?i)\bFY\s*(?:19|20)\d{2}\b|(?<!\d)(?:19|20)\d{2}(?!\d)", "", clean)
    clean = re.sub(
        r"[（(][^()（）]*(?:unit|USD|CNY|RMB|美元|人民币|百万|十亿|亿)[^()（）]*[)）]",
        "", clean, flags=re.I,
    )
    clean = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", clean.lower()).strip()
    if not clean or clean in {"value", "amount", "数值", "金额", "财年", "year", "fiscal year"}:
        return None
    return clean


def _answer_metric_identities(candidate: dict) -> set[str]:
    """Resolve a metric from the answer cell itself, not its surrounding table values."""
    table = candidate.get("table_context")
    parts: list[str] = []
    if isinstance(table, dict):
        row = table.get("row_header")
        column = table.get("column_header")
        row_has_year = isinstance(row, str) and bool(re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", row))
        column_has_year = isinstance(column, str) and bool(re.search(r"(?<!\d)(?:19|20)\d{2}(?!\d)", column))
        parts = [value for value, is_period in ((column, column_has_year), (row, row_has_year))
                 if isinstance(value, str) and not is_period]
    if not parts:
        parts = [candidate.get("context") or candidate.get("text") or ""]
    known = set().union(*(_metric_mentions(part) for part in parts))
    if known:
        return {f"known:{metric}" for metric in known}
    # Literal identities are useful only for bounded answer table headers. Guessing
    # an unknown metric phrase from prose would recreate the coincidence bug.
    if isinstance(table, dict):
        return {f"literal:{label}" for part in parts
                if (label := _normalized_metric_label(part)) is not None}
    return set()


def _source_metric_identities(row_header: Any) -> set[str]:
    known = _metric_mentions(row_header) if isinstance(row_header, str) else set()
    if known:
        return {f"known:{metric}" for metric in known}
    literal = _normalized_metric_label(row_header)
    return {f"literal:{literal}"} if literal else set()


def _period_kinds(text: str) -> set[str]:
    kinds = set()
    quarter = bool(re.search(r"季度|三个月|六个月|九个月|Q[1-4]|quarter(?:ly| ended)?|three months|six months|nine months", text, re.I))
    if quarter:
        kinds.add("quarter")
    # FY in "FY2025 Q4" identifies the fiscal year, not an annual value.
    if (re.search(r"年度|全年|year ended|twelve months", text, re.I)
            or (not quarter and re.search(r"财年|fiscal year|FY\s*(?:19|20)\d{2}", text, re.I))):
        kinds.add("year")
    return kinds


def _plain_numbers(text: str) -> set[Decimal]:
    values = set()
    for raw in re.findall(r"(?<![\d.,])[+-]?\d[\d,]*(?:\.\d+)?(?![\d.,])", text.replace("−", "-")):
        try:
            value = abs(Decimal(raw.replace(",", "")))
            if value < 1900 or value > 2100:
                values.add(value)
        except InvalidOperation:
            pass
    return values


def _unitless_quote_amounts(quote: str, units: set[tuple[str, int]] | None) -> set[tuple[str, Decimal]]:
    """Interpret bare table cells only when the answer declares one unambiguous unit."""
    if not units or len(units) != 1:
        return set()
    currency, power = next(iter(units))
    return {(currency, _decimal_scale(value, power)) for value in _plain_numbers(quote)}


def _local_source_dimensions(quote: str, displayed_values: set[Decimal]) -> list[dict]:
    """Return conservative row/header context for exact displayed numeric literals."""
    contexts = []
    normalized = quote.replace("−", "-")
    for match in re.finditer(r"(?<![\d.,])[+-]?\d[\d,]*(?:\.\d+)?(?![\d.,])", normalized):
        try:
            value = abs(Decimal(match.group().replace(",", "")))
        except InvalidOperation:
            continue
        if value not in displayed_values:
            continue
        before = normalized[max(0, match.start() - 220):match.start()]
        after = normalized[match.end():match.end() + 60]
        metric_matches = []
        for metric, patterns in _METRIC_PATTERNS.items():
            for pattern in patterns:
                metric_matches.extend((found.start(), found.end(), metric) for found in pattern.finditer(before))
        nearest_metric = set()
        cluster_start = None
        for start, end, metric in sorted(set(metric_matches), reverse=True):
            if cluster_start is not None and re.search(r"\d", before[end:cluster_start]):
                break
            nearest_metric.add(metric)
            cluster_start = start
        window = before + after
        contexts.append({
            "value": value,
            "metrics": nearest_metric,
            "period_kinds": _period_kinds(window),
            "years": {int(year) for year in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", window)},
        })
    return contexts


def _calculation_input_amounts(calculation: dict, source: Any) -> set[tuple[str, Decimal]]:
    """Return only inputs proved by this exact cited calculation and its frozen sources."""
    if (not isinstance(source, dict)
            or source.get("calculation_id") != calculation.get("calculation_id")
            or source.get("formula") != calculation.get("formula")
            or source.get("value") != calculation.get("value")
            or source.get("unit") != calculation.get("unit")
            or source.get("inputs") != calculation.get("inputs")
            or source.get("company_id") != calculation_subject(calculation).get("company_id")
            or source.get("company_ids") != calculation_subject(calculation).get("company_ids")
            or source.get("data_version") != calculation.get("data_version")
            or not isinstance(calculation.get("data_version"), dict)
            or not calculation["data_version"]
            or not isinstance(calculation.get("company_id"), str)):
        return set()
    inputs, sources = calculation.get("inputs"), source.get("sources")
    if not isinstance(inputs, list) or not isinstance(sources, list):
        return set()
    amounts: set[tuple[str, Decimal]] = set()
    for fact in inputs:
        matches = [item for item in sources if isinstance(item, dict)
                   and item.get("fact_id") == fact.get("fact_id")
                   and item.get("evidence_id") == fact.get("evidence_id")]
        if len(matches) != 1:
            return set()
        proof = matches[0]
        if (not re.fullmatch(r"[0-9a-f]{64}", str(proof.get("source_sha256") or ""))
                or not isinstance(proof.get("quote"), str)
                or not _quote_contains_fact_value(proof["quote"], str(fact.get("value") or ""))):
            return set()
        amount = _currency_amount(fact.get("value"), fact.get("unit"))
        if amount is None:
            return set()
        amounts.add(amount)
    return amounts


def _join_table_context(paragraphs: list[str]) -> list[str]:
    """Join declared table units and citation-only footnotes, never arbitrary prose."""
    grouped: list[str] = []
    for paragraph in paragraphs:
        table = any(re.match(r"^\s*\|\s*:?-{3,}", line) for line in paragraph.splitlines())
        if table and grouped and re.search(r"单位(?:均)?(?:为|是|[：:])", grouped[-1]) and len(grouped[-1]) < 160 and not _CITATION.search(grouped[-1]):
            paragraph = grouped.pop() + "\n\n" + paragraph
        elif grouped and "\n|" in grouped[-1] and _CITATION.search(paragraph):
            remaining = _CITATION.sub("", paragraph)
            source_footer = (len(remaining) <= 320 and re.match(
                r"(?:数据来自|来源(?:为|是|：|:)|(?:data|figures?|numbers?)\s+(?:come|came|are|were)\s+from|sourced?\s+from)",
                remaining.lstrip("* "), re.I))
            remaining = re.sub(r"(?i)(FY)?(?:19|20)\d{2}|数据|来源|原页面|原始|页面|重述|当时|同比及增量|同比|增量|计算引用|计算|引用|证据", "", remaining)
            if source_footer or not re.search(r"[\w\u4e00-\u9fff]", remaining):
                grouped[-1] += "\n\n" + paragraph
                continue
        grouped.append(paragraph)
    return grouped


def _table_calculation_matches(text: str, calculation: dict) -> bool:
    calculated = _currency_amount(calculation.get("value"), calculation.get("unit"))
    if calculated is None:
        return False
    table_lines = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    if len(table_lines) < 3:
        return False
    header = table_lines[0].strip("|").split("|")
    columns = [i for i, cell in enumerate(header) if re.search(
        r"差额|增量|变化|变动|增加|减少|同比|difference|change|increase|decrease", cell, re.I)]
    preamble = text.split(table_lines[0], 1)[0]
    common_units = _declared_currency_units(preamble)
    citation = ("calculation", calculation["calculation_id"])
    footer_has_reference = citation in _CITATION.findall("\n".join(line for line in text.splitlines() if not line.strip().startswith("|")))
    for line in table_lines[2:]:
        cells = line.strip("|").split("|")
        for column in columns:
            if column >= len(cells):
                continue
            column_units = _declared_currency_units(header[column])
            if column_units is None:
                continue
            declared_units = column_units or common_units
            # A calculation column must have one unambiguous local or common
            # currency unit. Units declared only by another column never apply.
            if declared_units is None or len(declared_units) != 1:
                continue
            displayed_currency, displayed_power = next(iter(declared_units))
            calculated_currency, calculated_value = calculated
            if displayed_currency != calculated_currency:
                continue
            cell = cells[column]
            cell_refs = _CITATION.findall(cell)
            if citation not in cell_refs and not (not cell_refs and footer_has_reference):
                continue
            clean = _CITATION.sub("", cell).replace("*", "").replace("−", "-")
            for raw, percent in re.findall(r"([+-]?\d[\d,]*(?:\.\d+)?)\s*([%％]?)", clean):
                displayed_value = _decimal_scale(Decimal(raw.replace(",", "")), displayed_power)
                if not percent and displayed_value == calculated_value:
                    return True
    return False


def _dimensional_coefficient_ranges(text: str, calculation: dict) -> list[tuple[int, int]]:
    unit = calculation.get("unit")
    if calculation.get("operation") != "regression_slope" or not isinstance(unit, str) or " / " not in unit:
        return []
    value = _decimal_text(calculation.get("value"))
    if value is None:
        return []
    ranges = []
    for label in {unit, unit.replace("USD millions", "百万美元")}:
        for match in re.finditer(
                rf"(?<![\d.])([+-]?\d[\d,]*(?:\.\d+)?)\s*{re.escape(label)}(?!\w)",
                text.replace("−", "-"), re.I):
            if Decimal(match[1].replace(",", "")) == Decimal(value):
                ranges.append((match.start(), match.end()))
    return ranges


def _calculation_output_matches(text: str, calculation: dict) -> bool:
    """Compare a recorded output literally; never infer an operation from prose."""
    text = text.replace("−", "-")
    try:
        value = Decimal(calculation["value"])
    except (InvalidOperation, KeyError, TypeError):
        return False
    if not value.is_finite():
        return False
    unit = str(calculation.get("unit") or "").strip()
    if calculation.get("operation", "").startswith("forecast_"):
        period = calculation.get("forecast", {}).get("period", {})
        citation = f"[calculation:{calculation.get('calculation_id')}]"
        labels = {
            "forecast_operating_cash_flow": r"经营现金流|operating cash flow|\bCFO\b",
            "forecast_capital_expenditures": r"资本开支(?:支付金额)?|capital expenditures|\bCapEx\b",
            "forecast_free_cash_flow": r"自由现金流|free cash flow|\bFCF\b",
        }
        classification = r"桥接估计|bridge estimate" if period.get("classification") == "bridge_estimate" else r"情景|条件预测|conditional forecast|scenario"
        output = _currency_amount(calculation.get("value"), unit)
        def context_matches(context):
            return (period.get("period_start") and period.get("period_end")
                    and period["period_start"] in context and period["period_end"] in context
                    and re.search(classification, context, re.I))
        def cell_matches(cell):
            return (citation in cell and output is not None
                    and {identifier for kind, identifier in _CITATION.findall(cell) if kind == "calculation"}
                    == {calculation.get("calculation_id")}
                    and output in _money_amounts(re.sub(r"[*`]", "", cell)))
        table_lines = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
        if len(table_lines) >= 3 and re.fullmatch(r"[\s|:\-]+", table_lines[1]):
            header = table_lines[0].strip("|").split("|")
            if header and re.search(r"期间|period", header[0], re.I):
                for line in table_lines[2:]:
                    cells = line.strip("|").split("|")
                    if len(cells) != len(header) or not context_matches(cells[0]):
                        continue
                    for column in range(1, len(cells)):
                        if (re.search(labels.get(calculation["operation"], r"(?!)"), header[column], re.I)
                                and cell_matches(cells[column])):
                            return True
        for statement in re.split(r"\n|[。；;]", text):
            if (not statement.lstrip().startswith("|") and cell_matches(statement)
                    and context_matches(statement)
                    and re.search(labels.get(calculation["operation"], r"(?!)"), statement, re.I)):
                return True
        return False
    if calculation.get("operation") == "regression_slope" and unit == "x":
        clean = re.sub(r"[*`]", "", _CITATION.sub("", text))
        for match in re.finditer(r"(?:斜率|slope)\s*(?:为|是|=|:|：|is\b)?\s*([+-]?\d[\d,]*(?:\.\d+)?)",
                                 clean, re.I):
            suffix = clean[match.end():].lstrip()
            if ((not suffix or suffix[0] in "；;，,。.)）\n")
                    and Decimal(match[1].replace(",", "")) == value):
                return True
    if calculation.get("operation") == "regression_slope" and " / " in unit:
        return bool(_dimensional_coefficient_ranges(text, calculation))
    if unit in {"%", "percent", "percentage points"}:
        for raw in re.findall(r"([+-]?\d[\d,]*(?:\.\d+)?)\s*(?:%|％|percent\b)", text, re.I):
            try:
                if Decimal(raw.replace(",", "")) == value:
                    return True
            except InvalidOperation:
                pass
        return False
    upper = unit.upper()
    currency = "USD" if "USD" in upper or "$" in unit else "CNY" if any(x in upper for x in ("CNY", "RMB")) else None
    if currency:
        scale = Decimal(1)
        if "BILLION" in upper:
            scale = Decimal(10) ** 9
        elif "MILLION" in upper:
            scale = Decimal(10) ** 6
        elif "TRILLION" in upper:
            scale = Decimal(10) ** 12
        return (currency, _decimal_scale(value, scale.adjusted())) in _money_amounts(text) or _table_calculation_matches(text, calculation)
    if not unit:
        return False
    return any(Decimal(raw.replace(",", "")) == value for raw in re.findall(
        rf"(?<![\d.])([+-]?\d[\d,]*(?:\.\d+)?)\s*{re.escape(unit)}(?!\w)", text, re.I))



class EvidenceCheckError(ValueError):
    """An explicit evidence-check boundary was malformed or inconsistent."""


@dataclass(frozen=True)
class ReceiptBackedEvent:
    """Exact event payload supplied by a persistence/verified-record adapter."""
    receipt_id: str
    payload_sha256: str
    payload_bytes: bytes


@dataclass(frozen=True)
class VerifiedDocumentFact:
    """A document derivation validated by the trusted record adapter."""
    event_receipt_id: str
    event_payload_sha256: str
    result_payload_sha256: str
    fact: dict
    source: dict


class TrustedEvidenceSource(Protocol):
    """Narrow trust boundary implemented by persistence or verified records.

    Implementations must obtain identity, scope, payload bytes and document validation
    from their authoritative record system. A model flag alone does not satisfy this.
    """
    def verified_company_scope(self, run_id: str, company_id: str) -> Sequence[str]: ...
    def verified_snapshot_bytes(self, run_id: str, company_id: str) -> bytes: ...
    def verified_event_receipts(self, run_id: str) -> Sequence[ReceiptBackedEvent]: ...
    def verified_document_facts(self, run_id: str, company_id: str) -> Sequence[VerifiedDocumentFact]: ...


class DocumentFactRecord:
    """Detached immutable view of one receipt-bound validated document fact."""
    __slots__ = ("_bytes",)

    def __init__(self, *args, **kwargs):
        raise EvidenceCheckError("document facts are created only by TrustedEvidenceAssembler")

    @classmethod
    def _from_values(cls, fact: dict, source: dict, event: dict):
        value = object.__new__(cls)
        value._bytes = _canonical({"fact": fact, "source": source, "event": event}).encode("utf-8")
        return value

    def _value(self):
        return json.loads(self._bytes)

    @property
    def fact(self): return self._value()["fact"]
    @property
    def source(self): return self._value()["source"]
    @property
    def event(self): return self._value()["event"]


class EvidenceCheckInput:
    """Canonical evidence graph sealed behind digest-checked detached accessors.

    This resists accidental mutation; it is not security isolation from Python code
    capable of introspecting and rewriting private attributes.
    """
    __slots__ = ("_canonical_bytes", "_digest")

    def __init__(self, *args, **kwargs):
        raise EvidenceCheckError("use TrustedEvidenceAssembler.assemble with a verified source")

    @classmethod
    def create(cls, **kwargs):
        raise EvidenceCheckError("raw dictionaries cannot create trusted evidence; use TrustedEvidenceAssembler")

    @classmethod
    def _seal(cls, payload: dict):
        value = object.__new__(cls)
        value._canonical_bytes = _canonical(payload).encode("utf-8")
        value._digest = hashlib.sha256(value._canonical_bytes).hexdigest()
        return value

    def _verified_payload(self):
        if hashlib.sha256(self._canonical_bytes).hexdigest() != self._digest:
            raise EvidenceCheckError("sealed evidence input digest mismatch")
        try:
            payload = json.loads(self._canonical_bytes)
        except (TypeError, ValueError) as exc:
            raise EvidenceCheckError("sealed evidence input is not canonical JSON") from exc
        if payload.get("schema_version") != CHECK_INPUT_SCHEMA_VERSION:
            raise EvidenceCheckError("unsupported evidence check input schema")
        return payload

    @property
    def schema_version(self): return self._verified_payload()["schema_version"]
    @property
    def run_id(self): return self._verified_payload()["run_id"]
    @property
    def company_id(self): return self._verified_payload()["company_id"]
    @property
    def company_ids(self): return tuple(self._verified_payload()["company_ids"])
    @property
    def snapshot(self): return self._verified_payload()["snapshot"]
    @property
    def events(self): return tuple(self._verified_payload()["events"])
    @property
    def document_facts(self):
        return tuple(DocumentFactRecord._from_values(item["fact"], item["source"], item["event"])
                     for item in self._verified_payload()["document_facts"])


def document_fact_results(event: dict) -> list[dict]:
    """Return natural document-fact results from supported public event schemas."""
    result = event.get("result")
    if not isinstance(result, dict):
        return []
    if event.get("name") == "extract_document_table_fact":
        return [result] if isinstance(result.get("fact"), dict) else []
    if event.get("name") == "get_candidate_reference":
        record = result.get("record")
        return [record] if (result.get("state") == "available"
                            and result.get("record_type") == "dynamic_document_fact"
                            and isinstance(result.get("source_provenance"), dict)
                            and isinstance(record, dict)
                            and isinstance(record.get("fact"), dict)) else []
    if event.get("name") in {"calculate_regression", "calculate_forecast", "read_research_inputs"}:
        restored = result.get("restored_document_facts")
        if not isinstance(restored, list):
            return []
        return [item for item in restored if isinstance(item, dict)
                and isinstance(item.get("fact"), dict)
                and (event["name"] == "read_research_inputs" or isinstance(item.get("reused_from"), dict))]
    return []


# Internal compatibility name retained for the sealed-record implementation below.
_document_results = document_fact_results


class VerifiedRecordSource:
    """Concrete capability for caller-owned records verified before entering the core.

    Callers provide immutable snapshot bytes and receipt hashes, rather than model
    dictionaries plus trust flags. The caller remains responsible for how those records
    were verified (for example, persistence reads or an authenticated archive).
    """

    def __init__(self, *, run_id: str, company_id: str, company_ids: Sequence[str],
                 snapshot_bytes: bytes, event_receipts: Sequence[ReceiptBackedEvent],
                 document_facts: Sequence[VerifiedDocumentFact] = ()):
        self._run_id, self._company_id = run_id, company_id
        self._company_ids, self._snapshot_bytes = tuple(company_ids), bytes(snapshot_bytes)
        self._event_receipts, self._document_facts = tuple(event_receipts), tuple(document_facts)

    def _check(self, run_id, company_id):
        if run_id != self._run_id or company_id != self._company_id:
            raise EvidenceCheckError("verified record source identity mismatch")
    def verified_company_scope(self, run_id, company_id):
        self._check(run_id, company_id); return self._company_ids
    def verified_snapshot_bytes(self, run_id, company_id):
        self._check(run_id, company_id); return self._snapshot_bytes
    def verified_event_receipts(self, run_id):
        self._check(run_id, self._company_id); return self._event_receipts
    def verified_document_facts(self, run_id, company_id):
        self._check(run_id, company_id); return self._document_facts


class TrustedEvidenceAssembler:
    """Construct sealed core input from receipt-backed, externally verified records."""

    @staticmethod
    def assemble(source: TrustedEvidenceSource, *, run_id: str, company_id: str) -> EvidenceCheckInput:
        if not isinstance(run_id, str) or not run_id or not isinstance(company_id, str) or not company_id:
            raise EvidenceCheckError("run_id and company_id must be non-empty strings")
        company_ids = tuple(source.verified_company_scope(run_id, company_id))
        if (not company_ids or len(set(company_ids)) != len(company_ids)
                or not all(isinstance(value, str) and value for value in company_ids)
                or company_id not in company_ids):
            raise EvidenceCheckError("verified company scope is invalid")
        try:
            snapshot = json.loads(source.verified_snapshot_bytes(run_id, company_id))
        except (TypeError, ValueError) as exc:
            raise EvidenceCheckError("verified snapshot bytes are invalid") from exc
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("evidence"), dict)
                or snapshot["evidence"].get("company_id") != company_id
                or not isinstance(snapshot.get("data_version"), dict)):
            raise EvidenceCheckError("verified snapshot is missing or outside company scope")
        declared_scope = snapshot.get("execution_scope")
        if isinstance(declared_scope, dict) and declared_scope.get("company_ids") != list(company_ids):
            raise EvidenceCheckError("verified company scope differs from effective snapshot scope")
        if len(company_ids) > 1 and (not isinstance(snapshot.get("subjects"), dict)
                                     or set(snapshot["subjects"]) != set(company_ids)):
            raise EvidenceCheckError("effective snapshot subjects differ from verified company scope")

        events, by_receipt = [], {}
        for receipt in source.verified_event_receipts(run_id):
            if (not isinstance(receipt, ReceiptBackedEvent) or not isinstance(receipt.receipt_id, str)
                    or not receipt.receipt_id or receipt.receipt_id in by_receipt
                    or not isinstance(receipt.payload_bytes, bytes)
                    or hashlib.sha256(receipt.payload_bytes).hexdigest() != receipt.payload_sha256):
                raise EvidenceCheckError("event receipt identity or payload hash is invalid")
            try:
                event = json.loads(receipt.payload_bytes)
            except (TypeError, ValueError) as exc:
                raise EvidenceCheckError("event receipt payload is invalid") from exc
            if (not isinstance(event, dict) or event.get("type") != "tool_result"
                    or event.get("authoritative") is not True or event.get("truncated") is True
                    or not isinstance(event.get("result"), dict)):
                raise EvidenceCheckError("verified events must be intact tool results")
            events.append(event)
            by_receipt[receipt.receipt_id] = (receipt, event)

        records = []
        for record in source.verified_document_facts(run_id, company_id):
            linked = by_receipt.get(record.event_receipt_id) if isinstance(record, VerifiedDocumentFact) else None
            if linked is None or linked[0].payload_sha256 != record.event_payload_sha256:
                raise EvidenceCheckError("document fact is not bound to an accepted event receipt")
            event = linked[1]
            matches = [item for item in _document_results(event)
                       if hashlib.sha256(_canonical(item).encode("utf-8")).hexdigest()
                       == record.result_payload_sha256]
            if len(matches) != 1 or matches[0].get("fact") != record.fact:
                raise EvidenceCheckError("document fact differs from its exact receipt payload")
            fact = record.fact
            if (not isinstance(fact.get("fact_id"), str) or not isinstance(fact.get("evidence_id"), str)
                    or fact.get("company_id") not in company_ids):
                raise EvidenceCheckError("document fact identity or company scope is invalid")
            records.append({"fact": deepcopy(fact), "source": deepcopy(record.source), "event": event})
        return EvidenceCheckInput._seal({
            "schema_version": CHECK_INPUT_SCHEMA_VERSION, "run_id": run_id,
            "company_id": company_id, "company_ids": list(company_ids),
            "snapshot": snapshot, "events": events, "document_facts": records,
        })


class CalculationReplay(Protocol):
    def replay_arithmetic(self, check_input: EvidenceCheckInput, inputs: list[dict], item: dict) -> dict: ...
    def replay_model(self, model: str, check_input: EvidenceCheckInput, inputs: list[dict], parameters: Any) -> list[dict]: ...


@dataclass(frozen=True)
class CalculationCheckResult:
    records: tuple[dict, ...]
    event_by_id: dict[str, dict]
    limitations: tuple[str, ...]


def projection_indexes(check_input: EvidenceCheckInput) -> dict[str, Any]:
    check_input._verified_payload()
    if check_input.schema_version != CHECK_INPUT_SCHEMA_VERSION:
        raise EvidenceCheckError("unsupported evidence check input schema")
    frozen = check_input.snapshot["evidence"]
    indexes = {
        "company_ids": list(check_input.company_ids),
        "sources": _index(frozen.get("sources"), "source_id"),
        "evidence": _index(frozen.get("evidence"), "evidence_id"),
        "claims": _index(frozen.get("claims"), "claim_id"),
        "activities": _index(frozen.get("activities"), "activity_id"),
        "facts": _index(frozen.get("facts"), "fact_id"),
        "document_facts": {}, "document_sources": {}, "document_events": {},
    }
    for record in check_input.document_facts:
        fact, source = record.fact, record.source
        previous = indexes["document_facts"].get(fact["fact_id"])
        if previous is not None and previous != fact:
            raise EvidenceCheckError("conflicting document-cell derivations")
        indexes["document_facts"][fact["fact_id"]] = fact
        indexes["facts"][fact["fact_id"]] = fact
        indexes["document_sources"][fact["evidence_id"]] = {**source, "fact": fact, "tables": [source["table"]]}
        indexes["document_events"][fact["fact_id"]] = record.event
    return indexes


def eligible_fact(fact, company_id, indexes):
    if not isinstance(fact, dict) or fact.get("company_id") not in indexes.get("company_ids", [company_id]):
        return False
    if fact.get("fact_id") in indexes.get("document_facts", {}):
        return fact == indexes["document_facts"][fact["fact_id"]] and _decimal_text(fact.get("value")) is not None
    required = ("fact_id", "metric", "unit", "period_kind", "basis", "evidence_id", "activity_id")
    if any(not isinstance(fact.get(key), str) or not fact[key] for key in required) or _decimal_text(fact.get("value")) is None:
        return False
    evidence = indexes["evidence"].get(fact["evidence_id"])
    source = indexes["sources"].get(evidence.get("source_id")) if isinstance(evidence, dict) else None
    return bool(source and evidence.get("source_sha256") == source.get("sha256")
                and fact.get("source_id") in (None, source.get("source_id")))


_EVIDENCE_RECORD_KINDS = {"claim", "evidence", "passage", "query", "calculation"}
_EVIDENCE_READ_STATES = {"available", "missing", "incomplete", "unsupported"}


@dataclass(frozen=True)
class ReferenceDescriptor:
    """Hash-bound catalog identity; deliberately not an evidence record."""

    kind: str
    identifier: str
    descriptor_sha256: str

    @classmethod
    def validate(cls, kind: str, identifier: str, value: Any, expected_sha256: str):
        if (kind not in _EVIDENCE_RECORD_KINDS or not isinstance(identifier, str) or not identifier
                or not isinstance(expected_sha256, str)):
            raise EvidenceCheckError("reference descriptor identity is invalid")
        actual = hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()
        if actual != expected_sha256:
            raise EvidenceCheckError("reference descriptor digest differs from its seal")
        return cls(kind, identifier, expected_sha256)


@dataclass(frozen=True)
class EvidenceRecordRead:
    """Bounded immutable read result; detached dictionaries are returned on access."""

    state: str
    kind: str
    identifier: str
    record_type: str | None = None
    record_sha256: str | None = None
    reason: str | None = None
    _record_bytes: bytes | None = None
    _provenance_bytes: bytes | None = None
    _event_bytes: bytes | None = None

    @property
    def record(self):
        return None if self._record_bytes is None else json.loads(self._record_bytes)

    @property
    def source_provenance(self):
        return None if self._provenance_bytes is None else json.loads(self._provenance_bytes)

    @property
    def event(self):
        return None if self._event_bytes is None else json.loads(self._event_bytes)

    def as_dict(self) -> dict:
        value = {"state": self.state, "kind": self.kind, "id": self.identifier}
        if self.record_type is not None:
            value["record_type"] = self.record_type
        if self.record_sha256 is not None:
            value["record_sha256"] = self.record_sha256
        if self.reason is not None:
            value["reason"] = self.reason
        if self._record_bytes is not None:
            value["record"] = self.record
        if self._provenance_bytes is not None:
            value["source_provenance"] = self.source_provenance
        return value


def _reference_matches(value: Any, kind: str, identifier: str):
    key = f"{kind}_id"
    if isinstance(value, dict):
        if value.get(key) == identifier:
            yield value
        for child_key, child in value.items():
            # Provenance describes a source; it is not the referenced fact/claim.
            # Its quote and locator must not outrank the actual record and erase
            # company identity when a complete candidate-reference read is cited.
            if not child_key.startswith("_") and child_key not in {"source_provenance", "historical_provenance"}:
                yield from _reference_matches(child, kind, identifier)
    elif isinstance(value, list):
        for child in value:
            yield from _reference_matches(child, kind, identifier)


def _record_quality(kind: str, value: dict) -> int:
    """Return zero for identity-only descriptors and wrappers."""
    if kind == "calculation":
        required = ("formula", "value", "unit", "inputs")
        return 100 + len(value.get("inputs", [])) if all(value.get(key) is not None for key in required) else 0
    if kind == "passage":
        # The compatibility tuple accepts the historical natural passage minimum;
        # read_evidence_record additionally requires frozen version/hash scope.
        required = ("quote",)
        return 100 + sum(value.get(key) is not None for key in (
            "document_version_id", "source_sha256", "context", "parser_version", "title")) \
            if all(value.get(key) is not None for key in required) else 0
    if kind == "query":
        required = ("sql", "columns", "rows", "sources")
        return 100 if all(value.get(key) is not None for key in required) else 0
    if kind == "evidence":
        if all(value.get(key) is not None for key in ("quote", "locator", "source_id")):
            return 110
        if (isinstance(value.get("fact_id"), str)
                and all(value.get(key) is not None for key in ("company_id", "metric", "value", "unit"))):
            return 100
        return 0
    if kind == "claim":
        return 100 if isinstance(value.get("text"), str) and value["text"] else 0
    return 0


def resolve_event_reference(events: list[dict], kind: str, identifier: str) -> tuple[dict | None, dict | None]:
    """Compatibility tuple adapter over schema-aware complete-record selection."""
    best = None
    best_score = 0
    best_event = None
    for event in events:
        if not isinstance(event, dict) or event.get("truncated") is True:
            continue
        for found in _reference_matches(event.get("result"), kind, identifier):
            # A document fact copied into calculation inputs is not an extraction
            # receipt. Select its explicit, replayable source result instead.
            if (kind == "evidence" and found.get("origin") == "frozen_document_table_cell-v1"
                    and not any(item.get("fact") == found for item in document_fact_results(event))):
                continue
            score = _record_quality(kind, found)
            if score > best_score:
                best_event, best, best_score = event, found, score
    return best_event, best


def _snapshot_document(snapshot: dict, document_version_id: str):
    documents = snapshot.get("documents")
    coverage = documents.get("coverage") if isinstance(documents, dict) else None
    values = coverage.get("documents") if isinstance(coverage, dict) else None
    if not isinstance(values, list):
        return None
    return next((item for item in values if isinstance(item, dict)
                 and item.get("document_version_id") == document_version_id), None)


def read_evidence_record(check_input: EvidenceCheckInput, kind: str, identifier: str, *,
                         calculations: Sequence[dict] = (), calculation_events=None,
                         max_record_bytes: int = 1_000_000) -> EvidenceRecordRead:
    """Read one complete record from a sealed scope/version without semantic approval.

    Calculations must be the output of ``calculation_records``. Document-table facts are
    selected only from the storage-replayed records already sealed into ``check_input``.
    """
    check_input._verified_payload()
    if kind not in _EVIDENCE_RECORD_KINDS:
        return EvidenceRecordRead("unsupported", kind, identifier, reason="unsupported reference kind")
    if not isinstance(identifier, str) or not identifier:
        raise EvidenceCheckError("evidence record identifier is invalid")
    if type(max_record_bytes) is not int or max_record_bytes < 1:
        raise EvidenceCheckError("evidence record byte bound is invalid")

    record = provenance = event = record_type = None
    incomplete = False
    if kind == "calculation":
        matches = [item for item in calculations if isinstance(item, dict)
                   and item.get("status") == "recorded"
                   and item.get("calculation_id") == identifier]
        if len(matches) > 1 and any(item != matches[0] for item in matches[1:]):
            raise EvidenceCheckError("conflicting validated calculation records share an id")
        if matches:
            record, record_type = matches[0], "calculation"
            event = (calculation_events or {}).get(identifier)
        else:
            incomplete = any(True for item in check_input.events
                             for _ in _reference_matches(item.get("result"), kind, identifier))
    else:
        # A dynamic document fact is complete only through the existing storage-backed
        # replay sealed by TrustedEvidenceAssembler; retain its natural result payload.
        if kind == "evidence":
            for validated in check_input.document_facts:
                if validated.fact.get("evidence_id") == identifier:
                    result = validated.event.get("result")
                    candidates = _document_results(validated.event)
                    selected = next((item for item in candidates
                                     if item.get("fact") == validated.fact), None)
                    if selected is not None:
                        record, provenance, event = selected, validated.source, validated.event
                        record_type = "dynamic_document_fact"
                        break
        if record is None:
            event, record = resolve_event_reference(list(check_input.events), kind, identifier)
            incomplete = event is None and any(
                True for item in check_input.events
                for _ in _reference_matches(item.get("result"), kind, identifier))
            if record is not None:
                if kind == "passage":
                    descriptor = _snapshot_document(check_input.snapshot, record.get("document_version_id"))
                    if (record.get("company_id") not in check_input.company_ids or descriptor is None
                            or not isinstance(record.get("locator"), dict)
                            or descriptor.get("source_sha256") != record.get("source_sha256")):
                        record, event, incomplete = None, None, True
                    else:
                        provenance, record_type = descriptor, "passage"
                elif kind == "query":
                    record_type = "sql_query"
                elif kind == "claim":
                    record_type = "claim"
                elif kind == "evidence" and isinstance(record.get("fact_id"), str):
                    indexes = projection_indexes(check_input)
                    frozen = indexes["facts"].get(record["fact_id"])
                    comparable = {key: value for key, value in record.items() if key != "source_id"}
                    if (frozen != comparable
                            or not eligible_fact(record, check_input.company_id, indexes)):
                        record, event, incomplete = None, None, True
                    else:
                        evidence = indexes["evidence"].get(identifier)
                        source = indexes["sources"].get(evidence.get("source_id")) if isinstance(evidence, dict) else None
                        provenance = {"evidence": evidence, "source": source}
                        record_type = "structured_fact"
                elif kind == "evidence":
                    indexes = projection_indexes(check_input)
                    evidence = indexes["evidence"].get(identifier)
                    source = indexes["sources"].get(evidence.get("source_id")) if isinstance(evidence, dict) else None
                    if evidence != record or not isinstance(source, dict) or evidence.get("source_sha256") != source.get("sha256"):
                        record, event, incomplete = None, None, True
                    else:
                        provenance, record_type = source, "source_evidence"

    if record is None:
        state = "incomplete" if incomplete else "missing"
        return EvidenceRecordRead(state, kind, identifier,
                                  reason="matching record is incomplete" if incomplete else "record is unavailable")
    raw = _canonical(record).encode("utf-8")
    if len(raw) > max_record_bytes:
        return EvidenceRecordRead("unsupported", kind, identifier, record_type=record_type,
                                  reason="complete record exceeds the read bound")
    return EvidenceRecordRead(
        "available", kind, identifier, record_type,
        hashlib.sha256(raw).hexdigest(), None, raw,
        None if provenance is None else _canonical(provenance).encode("utf-8"),
        None if event is None else _canonical(event).encode("utf-8"),
    )

@staticmethod
def calculation_subject(calculation):
    arithmetic = calculation.get("arithmetic")
    if isinstance(arithmetic, dict):
        context = arithmetic.get("output_context")
        context = context if isinstance(context, dict) else {}
        subject = {"company_id": context.get("company_id")}
        if "company_ids" in context:
            subject["company_ids"] = deepcopy(context["company_ids"])
        return subject
    # Legacy single-company records lack a separate result context.
    return {"company_id": calculation.get("company_id")}


@staticmethod
def resolve_source(kind, identifier, original, indexes):
    limitations: list[str] = []
    if kind == "query" and isinstance(original, dict):
        return {"title": "Native SQL query", "native_query": {
            key: value for key, value in original.items() if not key.startswith("_")
        }}, [original.get("numeric_semantics", "SQL execution does not certify financial meaning.")]
    if kind == "calculation":
        calculation = indexes.get("calculations", {}).get(identifier)
        if not isinstance(calculation, dict):
            return None, ["The calculation reference has no validated authoritative calculation record."]
        sources = []
        for fact in calculation["inputs"]:
            document_source = indexes.get("document_sources", {}).get(fact.get("evidence_id"))
            if document_source is not None:
                sources.append(document_source)
                continue
            evidence = indexes["evidence"].get(fact.get("evidence_id"))
            source = indexes["sources"].get(evidence.get("source_id")) if isinstance(evidence, dict) else None
            if not isinstance(source, dict) or evidence.get("source_sha256") != source.get("sha256"):
                return None, ["A calculation input has no valid frozen evidence-to-source hash linkage."]
            sources.append({
                "source_id": source.get("source_id"), "source_sha256": source.get("sha256"),
                "source_url": source.get("url"), "title": source.get("title"),
                "fact_id": fact.get("fact_id"),
                "evidence_id": evidence.get("evidence_id"), "quote": evidence.get("quote"),
                "locator": evidence.get("locator"),
            })
        return {
            "calculation_id": identifier, "formula": calculation["formula"],
            "value": calculation["value"], "unit": calculation["unit"],
            **calculation_subject(calculation),
            "data_version": calculation["data_version"],
            "inputs": calculation["inputs"], "sources": sources,
        }, limitations
    if kind == "passage" and isinstance(original, dict):
        source = {
            "source_id": original.get("source_id") or original.get("source_key"),
            "document_version_id": original.get("document_version_id"),
            "source_sha256": original.get("source_sha256"),
            "source_url": original.get("source_url"),
            "title": original.get("title"),
            "quote": original.get("quote"),
            "locator": original.get("locator"),
            **({"table": deepcopy(original["table"])} if isinstance(original.get("table"), dict) else {}),
            **({"interpretation": deepcopy(original["interpretation"])} if original.get("interpretation") is not None else {}),
            **({"native_query": deepcopy(original["native_query"])} if original.get("native_query") is not None else {}),
        }
        required = ("document_version_id", "source_sha256", "quote", "locator")
        if not all(source.get(key) is not None for key in required):
            limitations.append("The authoritative passage event lacks complete frozen source fields.")
        return source, limitations

    if kind == "evidence" and identifier in indexes.get("document_sources", {}):
        return indexes["document_sources"][identifier], limitations
    evidence_item = None
    if kind == "evidence":
        evidence_item = indexes["evidence"].get(identifier)
    elif kind == "claim":
        claim = indexes["claims"].get(identifier)
        evidence_ids = claim.get("evidence_ids", []) if isinstance(claim, dict) else []
        candidates = [indexes["evidence"].get(item) for item in evidence_ids]
        candidates = [item for item in candidates if item is not None]
        if len(candidates) == 1:
            evidence_item = candidates[0]
        else:
            limitations.append("The claim does not resolve to exactly one frozen evidence passage; no single source is displayed.")
    if not isinstance(evidence_item, dict):
        limitations.append("The reference has no matching evidence record in the frozen snapshot.")
        return None, limitations
    source_item = indexes["sources"].get(evidence_item.get("source_id"))
    if not isinstance(source_item, dict) or evidence_item.get("source_sha256") != source_item.get("sha256"):
        limitations.append("Frozen evidence-to-source hash linkage is missing or inconsistent.")
        return None, limitations
    return {
        "source_id": source_item.get("source_id"), "document_version_id": None,
        "source_sha256": source_item.get("sha256"), "source_url": source_item.get("url"),
        "title": source_item.get("title"), "quote": evidence_item.get("quote"),
        "locator": evidence_item.get("locator"),
    }, limitations

def frozen_fact_amounts(check_input, indexes):
    """Index only actually returned facts identical to this run's frozen snapshot."""
    check_input._verified_payload()
    by_evidence: dict[str, set[tuple[str, Decimal]]] = {}
    for event in check_input.events:
        if (event.get("authoritative") is not True or event.get("truncated") is True
                or event.get("name") not in {"get_company_facts", "get_claim_evidence", "calculate_yoy", "calculate_facts", "calculate_regression", "calculate_forecast"}):
            continue
        result = event.get("result")
        if (not isinstance(result, dict) or result.get("company_id") not in indexes.get("company_ids", [check_input.company_id])
                or result.get("data_version") != check_input.snapshot.get("data_version")
                or result.get("truncated") is True):
            continue
        returned_facts = result.get("facts", []) if isinstance(result.get("facts"), list) else []
        if event.get("name") == "calculate_yoy":
            returned_facts = [result.get("current_fact"), result.get("prior_fact")]
        elif event.get("name") == "calculate_facts":
            returned_facts = result.get("inputs", []) if isinstance(result.get("inputs"), list) else []
        elif event.get("name") in {"calculate_regression", "calculate_forecast"} and isinstance(result.get("calculations"), list):
            returned_facts = [
                fact for calculation in result.get("calculations", [])
                if isinstance(calculation, dict) and calculation.get("company_id") in indexes.get("company_ids", [check_input.company_id])
                and calculation.get("data_version") == check_input.snapshot.get("data_version")
                and isinstance(calculation.get("inputs"), list)
                for fact in calculation["inputs"]]
        for returned in returned_facts:
            if not isinstance(returned, dict) or not isinstance(returned.get("fact_id"), str):
                continue
            frozen = indexes.get("facts", {}).get(returned["fact_id"])
            # get_company_facts adds source_id for transport; the canonical
            # frozen fact may omit it. No other event field may differ.
            comparable_returned = {key: value for key, value in returned.items() if key != "source_id"}
            comparable_frozen = ({key: value for key, value in frozen.items() if key != "source_id"}
                                 if isinstance(frozen, dict) else None)
            if comparable_returned != comparable_frozen:
                continue
            if not eligible_fact(frozen, check_input.company_id, indexes):
                continue
            document_source = indexes.get('document_sources', {}).get(frozen['evidence_id'])
            expected_source = document_source['source_id'] if document_source else indexes['evidence'][frozen['evidence_id']]['source_id']
            if returned.get('source_id') not in (None, expected_source):
                continue
            amount = _currency_amount(frozen.get("value"), frozen.get("unit"))
            if amount is not None:
                by_evidence.setdefault(frozen["evidence_id"], set()).add(amount)
    return by_evidence

def findings(text, citation_map, trace_by_reference, citation_sources,
              calculations=None, frozen_fact_amounts=None, question=""):
    calculations = [item for item in (calculations or []) if item.get("status") == "recorded"]
    frozen_fact_amounts = frozen_fact_amounts or {}
    paragraphs = []
    for block in _PARAGRAPH.split(text):
        current = []
        for line in block.splitlines():
            if re.match(r"^\s*(?:[-*+]\s+|\d+[.)、]\s*)", line) and current:
                paragraphs.append("\n".join(current).strip())
                current = []
            if line.strip():
                current.append(line)
        if current:
            paragraphs.append("\n".join(current).strip())
    # Titles are navigation, not unsupported financial claims. List items
    # stay separate so a citation on one bullet cannot validate its neighbours.
    paragraphs = [item for item in paragraphs if not (
        len(item) < 100 and "\n" not in item and not _CITATION.search(item)
        and not re.search(r"单位(?:均)?(?:为|是|[：:])", item)
        and not _plain_numbers(item)
        and (item.lstrip().startswith("#") or re.fullmatch(
            r"(?i)(?:注意|结论|说明|来源|风险|限制|更正说明|完整新版|note|conclusion|sources|limitations|risks)[：:]",
            item.strip("* ")))
    )]
    paragraphs = _join_table_context(paragraphs)
    findings = []
    for position, paragraph in enumerate(paragraphs, 1):
        keys = list(dict.fromkeys(_CITATION.findall(paragraph)))
        references = []
        for key in keys:
            citation = citation_map.get(key)
            references.append({
                "kind": key[0], "id": key[1],
                "valid": bool(citation and citation.get("valid") is True),
            })
        valid = [item for item in references if item["valid"]]
        invalid = [item for item in references if not item["valid"]]
        status = "reference_invalid" if invalid else "source_linked" if valid else "unreferenced"
        if invalid:
            explanation = "At least one citation does not correspond to an actual tool result from this run and must be rechecked."
        elif valid:
            explanation = "The citation is linked to materials from this run, but the source has not been reviewed to confirm that it supports this statement."
        else:
            explanation = "This section has no verifiable citation bound to it and cannot be treated as a supported conclusion."

        money_text = list(paragraph)
        for calculation in calculations:
            if any(ref["kind"] == "calculation" and ref["id"] == calculation.get("calculation_id") for ref in valid):
                for start, end in _dimensional_coefficient_ranges(paragraph, calculation):
                    money_text[start:end] = " " * (end - start)
        paragraph_money = _money_amounts("".join(money_text))
        paragraph_units = _declared_currency_units(paragraph)
        displayed_values = _plain_numbers(_CITATION.sub("", paragraph))
        table_values = _plain_numbers("\n".join(
            line for line in paragraph.splitlines() if line.strip().startswith("|")))
        quote_money: set[tuple[str, Decimal]] = set()
        source_contexts = []
        contextual_units = set()
        unrelated_references = []
        for item in valid:
            source = citation_sources.get((item["kind"], item["id"]))
            if isinstance(source, dict) and isinstance(source.get("quote"), str):
                quote = source["quote"]
                quote_money.update(_money_amounts(quote))
                quote_money.update(_unitless_quote_amounts(quote, paragraph_units))
                contexts = _local_source_dimensions(quote, displayed_values)
                source_contexts.extend(contexts)
                if contexts:
                    quote_units = _declared_currency_units(quote)
                    if quote_units:
                        contextual_units.update(quote_units)
                # Only a clearly non-numeric note can be called unrelated here.
                # A different numeric formatting/scale remains honestly unassessed.
                shared_basis_context = (re.search(r"重述|重列|调整后|口径|recast|restat|adjusted|basis", paragraph, re.I)
                                        and re.search(r"重述|重列|调整后|口径|recast|restat|adjusted|basis", quote, re.I))
                if (displayed_values and not contexts and not _plain_numbers(quote)
                        and not (_metric_mentions(paragraph) & _metric_mentions(quote))
                        and not shared_basis_context):
                    unrelated_references.append(item)
            # A unitless table quote may still have a typed amount, but only
            # when this same paragraph cites its evidence and the fact was
            # actually returned, version-matched, and identical to the
            # current run's frozen fact. This remains a numeric-only check.
            if item["kind"] == "evidence":
                quote_money.update(frozen_fact_amounts.get(item["id"], set()))
        for calculation in calculations:
            if any(ref["kind"] == "calculation" and ref["id"] == calculation.get("calculation_id") for ref in valid):
                scale = {"USD millions": "million", "USD billions": "billion", "USD": ""}.get(calculation.get("unit"))
                if scale is not None:
                    quote_money.update(_money_amounts(f"USD {calculation['value']} {scale}"))
                # A cited recorded sum also proves its exact inputs, but only
                # through its complete frozen fact/evidence/SHA chain.
                source = citation_sources.get(("calculation", calculation.get("calculation_id")))
                quote_money.update(_calculation_input_amounts(calculation, source))
        missing = paragraph_money - quote_money
        if valid and missing:
            status = "needs_review"
            rendered = "、".join(_readable_money(currency, value)
                                for currency, value in sorted(missing))
            explanation = (
                f"The literal amount check did not find {rendered}; check whether a nearby passage was cited or different bases were mixed."
                "This notice checks amounts only and does not replace semantic review."
            )

        review_flags = []
        answer_metrics = _metric_mentions(paragraph) or _metric_mentions(question)
        metric_values: dict[str, set[Decimal]] = {}
        for context in source_contexts:
            for metric in context["metrics"]:
                metric_values.setdefault(metric, set()).add(context["value"])
        max_coverage = max(map(len, metric_values.values()), default=0)
        dominant_metrics = {metric for metric, values in metric_values.items()
                            if len(values) == max_coverage}
        # A unique metric covering the most exact values is deterministic;
        # tied same-amount rows are left unassessed rather than guessed.
        if len(answer_metrics) == 1 and len(dominant_metrics) == 1 and answer_metrics.isdisjoint(dominant_metrics):
            expected = next(iter(answer_metrics))
            actual = next(iter(dominant_metrics))
            review_flags.append({
                "code": "metric_identity_mismatch", "severity": "high",
                "message": f"Metric mismatch: the answer states {_METRIC_LABELS.get(expected, expected)}, but the cited source value corresponds to {_METRIC_LABELS.get(actual, actual)}; these values cannot answer the current question.",
                "action": "Return to the corresponding metric row in the source and retrieve values with the period, unit, and basis required by the question; do not reuse them merely because the value matches or the citation is valid.",
            })
        answer_periods = _period_kinds(paragraph) or _period_kinds(question)
        contextual_periods = set().union(*(item["period_kinds"] for item in source_contexts)) if source_contexts else set()
        answer_years = {int(year) for year in re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", paragraph)}
        contextual_years = set().union(*(item["years"] for item in source_contexts)) if source_contexts else set()
        period_kind_wrong = len(answer_periods) == 1 and len(contextual_periods) == 1 and answer_periods != contextual_periods
        year_wrong = bool(table_values) and len(answer_years) == 1 and len(contextual_years) == 1 and answer_years != contextual_years
        if period_kind_wrong or year_wrong:
            expected = ("annual" if "year" in answer_periods else "quarterly") if answer_periods else "the stated year"
            actual = ("annual" if "year" in contextual_periods else "quarterly") if contextual_periods else "another year"
            review_flags.append({
                "code": "period_identity_mismatch", "severity": "high",
                "message": f"Period mismatch: the answer requires {expected} data, but the cited source values correspond to {actual} data or a different year; they are not interchangeable.",
                "action": "Check the period type and year in the source header, then cite the row and column that exactly match the question.",
            })
        if paragraph_units and len(paragraph_units) == 1 and contextual_units and paragraph_units.isdisjoint(contextual_units):
            review_flags.append({
                "code": "unit_identity_mismatch", "severity": "high",
                "message": "Unit mismatch: the unit in the answer differs from the currency or scale of these source values and cannot be used unchanged.",
                "action": "Check the currency and scale in the source header; retain an auditable calculation record for any conversion.",
            })
        if unrelated_references and table_values and (paragraph_money or paragraph_units):
            review_flags.append({
                "code": "citation_scope_unrelated", "severity": "medium",
                "message": "Irrelevant citation scope: at least one cited passage contains neither this section’s amount nor its metric; a valid citation does not mean it supports the section.",
                "action": "Retain only source citations that actually contain the corresponding metric and value; mark citations with an uncertain support relationship as not assessed.",
            })
        absence = _ABSENCE_ASSERTION.search(paragraph)
        scoped_absence = bool(absence and _SCOPED_SEARCH.search(paragraph))
        if absence:
            review_flags.append({
                "code": "source_coverage_required" if scoped_absence else "unsupported_non_disclosure",
                "severity": "medium" if scoped_absence else "high",
                "message": "This conclusion is limited to the retrieved materials and still does not establish complete disclosure coverage." if scoped_absence
                           else "The non-disclosure assertion does not clearly limit itself to the materials searched.",
                "action": "Use only after a person confirms source coverage, search methods, and relevant documents; the system will not rewrite the answer automatically.",
            })
        numeric_prose = _CITATION.sub("", paragraph)
        numeric_prose = re.sub(
            r"(?i)(?<!\d)(?:19|20)\d{2}\s*[—–~至到-]\s*(?:19|20)\d{2}\s*(?:财年|年|fiscal years?)"
            r"|第\s*\d+\s*版|版本\s*\d+|\breport\s+version\s+\d+\b", "", numeric_prose)
        numeric_prose = re.sub(r"(?i)(?<!\d)(?:FY(?:19|20)\d{2}(?!\d)|(?:19|20)\d{2}(?=\s*(?:年|财年)|[-/]\d))(?:[-/年]\d{1,2}(?:[-/月]\d{1,2})?)?|Q[1-4]", "", numeric_prose)
        calculation_language = any(
            _CALCULATION_LANGUAGE.search(statement) is not None
            and re.search(r"[+-]?\d[\d,]*(?:\.\d+)?", statement) is not None
            for statement in re.split(r"[。！？；]|(?<!\d)[.!?;](?!\d)", numeric_prose)
        )
        calculation_refs = [item for item in references if item["kind"] == "calculation" and item["valid"]]
        cited_calculations = {item["id"] for item in calculation_refs}
        matched_calculations = [item for item in calculations
                                if item.get("calculation_id") in cited_calculations
                                and _calculation_output_matches(paragraph, item)]
        forecast_references = [item for item in calculations
                               if item.get("calculation_id") in cited_calculations
                               and item.get("operation", "").startswith("forecast_")]
        if paragraph_money and any(item not in matched_calculations for item in forecast_references):
            review_flags.append({
                "code": "forecast_output_context_mismatch", "severity": "high",
                "message": "Scenario amounts must be locally associated with their actual calculation, metric, full period, and bridge-estimate or conditional-forecast classification.",
                "action": "Check the amount, unit, date, and scenario label in each cited sentence or table row; do not present estimates as actual results.",
            })
        # A matching number elsewhere in a run cannot justify this paragraph.
        # Conversely a percentage explicitly quoted by the issuer is a source
        # statement, not necessarily a new calculation performed by the agent.
        def percentages(value):
            return {Decimal(raw.replace(",", "")) for raw in re.findall(r"([+-]?\d[\d,]*(?:\.\d+)?)\s*(?:[%％]|percent\b|per\s+cent\b)", value.replace("−", "-"), re.I)}
        def percentage_points(value):
            normalized = value.replace('−', '-')
            values = re.findall(r'([+-]?\d[\d,]*(?:\.\d+)?)\s*(?:percentage\s+points?|个百分点|百分点)', normalized, re.I)
            if re.search(r'percentage|margin|growth rate|利润率|毛利率', normalized, re.I):
                values += re.findall(r'([+-]?\d[\d,]*(?:\.\d+)?)\s*points?\b', normalized, re.I)
            return {Decimal(raw.replace(',', '')) for raw in values}
        stated_percentages = percentages(_CITATION.sub("", paragraph))
        stated_points = percentage_points(_CITATION.sub('', paragraph))
        if not valid and not calculation_language and (paragraph_money or stated_percentages or stated_points):
            review_flags.append({'code': 'missing_numeric_reference', 'severity': 'high',
                                 'message': 'This section’s amount or percentage has no locally bound, verifiable source.',
                                 'action': 'Add a source actually read in this research run for this section’s numbers; do not borrow the next section’s citation or fabricate evidence from the prose.'})
        elif not valid and not calculation_language and not absence:
            review_flags.append({
                "code": "missing_local_reference", "severity": "high",
                "message": "This section has no locally bound, verifiable evidence, including necessary context such as period and unit.",
                "action": "Link a source actually read in this run that supports the section; do not borrow adjacent citations or fabricate evidence.",
            })
        sourced_percentages = set()
        sourced_points = set()
        for reference in valid:
            source = citation_sources.get((reference["kind"], reference["id"]))
            if isinstance(source, dict):
                sourced_percentages.update(percentages(str(source.get("quote") or "")))
                sourced_points.update(percentage_points(str(source.get('quote') or '')))
        context_values = {item["value"] for item in source_contexts}
        source_reported_output = bool(valid and (
            ((stated_percentages or stated_points) and stated_percentages <= sourced_percentages and stated_points <= sourced_points)
            or (paragraph_money and paragraph_money <= quote_money and not stated_percentages and not stated_points)
            or (table_values and table_values <= context_values and not stated_percentages and not stated_points)))
        if calculation_language and not matched_calculations and not source_reported_output:
            review_flags.append({
                "code": "missing_calculation_record", "severity": "high",
                "message": "This section’s derived value is not bound to a valid calculation citation with a matching literal output.",
                "action": "Check this section’s calculation citation, actual inputs, formula, and exact output; if no calculation record exists, execute it rather than fabricating one from the prose.",
            })
        unmatched = cited_calculations - {item['calculation_id'] for item in matched_calculations}
        if calculation_language and matched_calculations and unmatched:
            review_flags.append({"code": "calculation_output_mismatch", "severity": "high",
                                 "message": "Some calculation citations in this section have no corresponding exact output; one matching calculation does not validate them all.",
                                 "action": "Check each calculated value, unit, and citation location in the table or prose."})
        # Deliberately narrow label check, not general semantic validation.
        financing_sums = [item for item in matched_calculations
                          if item.get("operation") == "sum"
                          and len(item.get("inputs", [])) >= 2
                          and all(re.search(r"financ", str(fact.get("basis") or ""), re.I)
                                  for fact in item.get("inputs", []))]
        broad_financing_label = re.search(
            r"(?:全部|总(?:体|额|计)?|净|实际)融资(?:活动)?现金流|"
            r"(?:total|net|overall|actual)\s+financing\s+cash\s+(?:flow|outflow)",
            paragraph, re.I)
        scoped_financing_label = re.search(
            r"(?:仅|只是|只代表|仅代表|限于).{0,24}(?:两项|所列|上述|这些).{0,12}融资(?:活动)?现金流|"
            r"only.{0,32}(?:listed|selected|these|two).{0,20}financing.{0,12}(?:line items|cash flows?)",
            paragraph, re.I)
        if financing_sums and broad_financing_label and not scoped_financing_label:
            review_flags.append({
                "code": "calculation_scope_mislabel", "severity": "high",
                "message": "The cited calculation sums only its listed financing cash-flow inputs and cannot be described as total, net, or actual financing cash flow.",
                "action": "Explicitly limit the statement to the sum of the listed financing cash-flow items, or obtain and cite the complete metric separately; this limited notice does not replace general semantic review.",
            })
        # Narrow documented translation risk, not a general entailment check:
        # reversal OF a court's decision does not make that court the reverser.
        active_court = [match for match in re.finditer(r'欧洲普通法院(?:撤销|推翻)', paragraph)
                        if not re.search(r'(?:不是|并非|不要|不应)[^。；\n]{0,12}$', paragraph[:match.start()])]
        if active_court and any(
            re.search(r"reversal of the European General Court[’']s", str((citation_sources.get((ref['kind'], ref['id'])) or {}).get('quote') or ''), re.I)
            for ref in valid if isinstance(citation_sources.get((ref['kind'], ref['id'])), dict)
        ):
            review_flags.append({'code': 'possible_source_role_reversal', 'severity': 'high',
                                 'message': 'The source says the General Court’s decision was overturned; the current wording may incorrectly cast the court as the party that overturned it.',
                                 'action': 'Check the action direction and actor against the source; the original answer will not be rewritten automatically, and this notice does not replace full semantic review.'})
        if review_flags:
            status = "reference_invalid" if invalid else "needs_review"
            explanation = "；".join(flag["message"] for flag in review_flags)

        if absence:
            classification = "evidence_gap"
        elif matched_calculations:
            classification = "recorded_calculation"
        elif valid:
            classification = "source_statement"
        else:
            classification = "interpretation"
        trace_ids = [trace_by_reference[(item["kind"], item["id"])] for item in references
                     if (item["kind"], item["id"]) in trace_by_reference]
        for calculation in matched_calculations:
            trace_id = trace_by_reference.get(("calculation", calculation["calculation_id"]))
            if trace_id and trace_id not in trace_ids:
                trace_ids.append(trace_id)
        findings.append({
            "id": _stable_id("finding", position, paragraph), "text": paragraph,
            "classification": classification,
            "status": status, "label": {
                "source_linked": "Source linked", "unreferenced": "No citation",
                "reference_invalid": "Invalid citation", "needs_review": "Needs review",
            }[status],
            "explanation": explanation, "references": references,
            "review_flags": review_flags, "trace_ids": trace_ids,
        })
    return findings

def calculation_records(check_input, indexes, replay: CalculationReplay) -> CalculationCheckResult:
    check_input._verified_payload()
    records, by_id, limitations = [], {}, []
    for event in check_input.events:
        if event.get("name") not in {"calculate_facts", "calculate_yoy", "calculate_regression", "calculate_forecast"}:
            continue
        result = event["result"]
        if not isinstance(result, dict) or "error" in result:
            limitations.append("Failed calculation-tool responses remain in the run record and are not treated as completed calculations or legacy calculation records.")
            continue
        if isinstance(result.get("calculations"), list):
            candidates = result["calculations"]
        elif isinstance(result.get("calculation"), dict):
            candidates = [result["calculation"]]
        else:
            candidates = [result]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if "error" in candidate:
                limitations.append("Failed calculation-tool responses remain in the run record and are not treated as completed calculations or legacy calculation records.")
                continue
            calculation_id = candidate.get("calculation_id")
            if not isinstance(calculation_id, str) or not calculation_id:
                if event.get("name") == "calculate_yoy":
                    records.append({
                        "calculation_id": None, "schema_version": None,
                        "operation": candidate.get("operation") or candidate.get("calculation"),
                        "formula": None, "value": candidate.get("value"), "unit": candidate.get("unit"),
                        "inputs": [item for item in (candidate.get("current_fact"), candidate.get("prior_fact")) if isinstance(item, dict)],
                        "evidence_ids": candidate.get("evidence_ids", []),
                        "data_version": candidate.get("data_version"), "company_id": candidate.get("company_id"),
                        "comparison_mode": None, "limitations": list(candidate.get("limitations", [])),
                        "status": "legacy", "reference": None,
                    })
                    limitations.append("A legacy calculate_yoy result has no calculation_id; it remains a legacy record without generating a citation or fabricated identifier.")
                else:
                    limitations.append("A calculation-tool result without calculation_id was excluded and no record was fabricated.")
                continue
            valid, reason = validate_calculation(candidate, check_input, indexes, replay)
            if not valid:
                limitations.append(f"Calculation {calculation_id} excluded: {reason}")
                continue
            record = {key: candidate.get(key) for key in (
                "calculation_id", "schema_version", "operation", "formula", "value", "unit",
                "inputs", "evidence_ids", "data_version", "company_id", "comparison_mode", "limitations"
            )}
            record.update({"status": "recorded", "reference": {"kind": "calculation", "id": calculation_id}})
            if "arithmetic" in candidate:
                record.update({key: candidate[key] for key in ("parameters", "arithmetic")})
            if candidate.get("operation", "").startswith("regression_"):
                record.update({key: candidate[key] for key in ("parameters", "regression")})
            if candidate.get("operation", "").startswith("forecast_"):
                record.update({key: candidate[key] for key in ("parameters", "forecast")})
            previous = by_id.get(calculation_id)
            if previous is not None and previous != record:
                raise EvidenceCheckError("conflicting authoritative calculation records share an id")
            by_id[calculation_id] = record
            records.append(record)
    # Repeated identical results are represented once; event linkage remains the
    # first actual authoritative event in deterministic runner order.
    unique = []
    seen = set()
    for item in records:
        key = item.get("calculation_id") or _canonical(item)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    event_map = {}
    for event in check_input.events:
        if event.get("name") in {"calculate_facts", "calculate_yoy", "calculate_regression", "calculate_forecast"}:
            result = event.get("result", {})
            if not isinstance(result, dict) or "error" in result:
                continue
            if isinstance(result.get("calculations"), list):
                candidates = result["calculations"]
            elif isinstance(result.get("calculation"), dict):
                candidates = [result["calculation"]]
            else:
                candidates = [result]
            for candidate in candidates:
                if isinstance(candidate, dict) and "error" not in candidate and candidate.get("calculation_id") in by_id:
                    event_map.setdefault(candidate["calculation_id"], event)
    return CalculationCheckResult(tuple(unique), event_map, tuple(dict.fromkeys(limitations)))

def validate_calculation(item, check_input, indexes, replay: CalculationReplay):
    check_input._verified_payload()
    from . import arithmetic, forecast, regression
    from .research_tools import ToolError
    required_strings = ("calculation_id", "schema_version", "operation", "formula", "value", "unit", "company_id", "comparison_mode")
    if any(not isinstance(item.get(key), str) or not item[key] for key in required_strings):
        return False, "Required fields are incomplete"
    if item["operation"] not in {"difference", "percent_change", "sum", "ratio", "percent_ratio", "percentage_point_difference", "yoy", "year_over_year_percent_change"} | regression.OPERATIONS | forecast.OPERATIONS:
        return False, "operation is not supported"
    if item["operation"] in {"percent_ratio", "percentage_point_difference"} and not arithmetic.is_current(item):
        return False, "Percentage ratios or percentage-point differences require an exact, fully reproducible calculation record"
    if not isinstance(item.get("data_version"), dict) or item["company_id"] not in indexes.get("company_ids", [check_input.company_id]) or item["data_version"] != check_input.snapshot.get("data_version"):
        return False, "Company or data version does not match the frozen run"
    if _decimal_text(item["value"]) is None:
        return False, "Output is not a finite Decimal string"
    inputs = item.get("inputs")
    if not isinstance(inputs, list) or not inputs or not all(eligible_fact(fact, check_input.company_id, indexes) for fact in inputs):
        return False, "Input is not a fact with frozen source provenance"
    frozen_facts = indexes.get("facts", {})
    if any(frozen_facts.get(fact.get("fact_id")) != fact for fact in inputs):
        return False, "Calculation inputs do not exactly match frozen facts"
    evidence_ids = item.get("evidence_ids")
    expected_evidence = list(dict.fromkeys(fact["evidence_id"] for fact in inputs))
    if not isinstance(evidence_ids, list) or evidence_ids != expected_evidence:
        return False, "evidence_ids do not match input facts"
    if not isinstance(item.get("limitations"), list) or not all(isinstance(value, str) for value in item["limitations"]):
        return False, "limitations is not a list of strings"
    if "arithmetic" in item or arithmetic.is_current(item):
        try:
            expected = replay.replay_arithmetic(check_input, inputs, item)
        except ToolError as exc:
            return False, f"Calculation review failed: {exc.message}"
        recorded = {key: value for key, value in item.items() if not key.startswith("_")}
        return (True, "") if recorded == expected else (False, "Calculation record does not match recomputation from complete dependencies")
    if item["operation"] in regression.OPERATIONS | forecast.OPERATIONS:
        model = "regression" if item["operation"] in regression.OPERATIONS else "forecast"
        try:
            expected = replay.replay_model(model, check_input, inputs, item.get("parameters"))
        except ToolError as exc:
            return False, f"Model validation failed: {exc.message}"
        return (True, "") if item in expected else (False, "Model record does not match recomputation from frozen inputs")
    operation = item["operation"]
    first = inputs[0]
    mode = item["comparison_mode"]
    if mode not in {"comparable", "presentation_difference"}:
        return False, "Comparison mode is not supported"
    dimensions = ("company_id", "unit", "period_kind")
    if mode == "presentation_difference":
        if (operation != "difference" or len(inputs) != 2
                or inputs[0].get("basis") == inputs[1].get("basis")
                or any((fact.get("period_start"), fact.get("period_end")) !=
                       (first.get("period_start"), first.get("period_end")) for fact in inputs[1:])):
            return False, "Recast differences must compare different bases for the same period and must not represent growth"
    else:
        dimensions += ("basis",)
    if operation not in {"sum", "ratio"}:
        dimensions += ("metric",)
    if any(any(fact.get(key) != first.get(key) for key in dimensions) for fact in inputs[1:]):
        return False, "Input facts are not comparable by company, metric, unit, period type, or basis"
    if operation in {"sum", "ratio"} and any(
            (fact.get("period_start"), fact.get("period_end")) !=
            (first.get("period_start"), first.get("period_end")) for fact in inputs[1:]):
        return False, "Aggregate inputs do not belong to the same period"
    if operation in {"difference", "percent_change", "ratio", "yoy", "year_over_year_percent_change"} and len(inputs) != 2:
        return False, "This operation requires exactly two inputs"
    if operation in {"percent_change", "yoy", "year_over_year_percent_change"} and item["unit"] != "%":
        return False, "Percentage operation output unit must be %"
    if operation == "ratio" and (item["unit"] != "x" or mode != "comparable"):
        return False, "Ratio output unit or comparison mode is inconsistent"
    if operation in {"difference", "sum"} and item["unit"] != first["unit"]:
        return False, "Amount operation output unit does not match the inputs"
    return True, ""


class WorkspaceCalculationReplay:
    """Adapter from narrow evidence/decision readers to deterministic replay."""

    def __init__(self, evidence_db, decisions):
        self.evidence_db = evidence_db
        self.decisions = decisions

    def _research(self, check_input, inputs):
        from .scoped_tools import tools_for_scope
        research = tools_for_scope(
            self.evidence_db, check_input.company_id, list(check_input.company_ids),
            frozen_snapshot=check_input.snapshot,
        )
        research._facts.update({fact["fact_id"]: fact for fact in inputs})
        return research

    def replay_arithmetic(self, check_input, inputs, item):
        return self._research(check_input, inputs).replay_arithmetic(item)

    def replay_model(self, model, check_input, inputs, parameters):
        result = self.decisions.replay_model(
            model, check_input.run_id, self._research(check_input, inputs), parameters
        )
        return result["calculations"]
