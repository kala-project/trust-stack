"""Build the deterministic evidence slice with ``python3 -m workbench.build``.

The builder verifies every catalogued raw artifact before parsing, never executes
source HTML, and atomically replaces only databases bearing this producer's exact
schema marker. It does not alter or treat raw files as mutable storage. Before a
successful replacement, the prior database is retained in ``var/archives`` (or
the target database's sibling ``archives`` directory). No history-query API is
provided yet.
"""

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile

from .parser import (
    PARSER_VERSION, ParseError, apple_release, microsoft_quarter_gaap,
    microsoft_segments, microsoft_shareholder_returns, parse_html,
)
from .repository import (
    PRODUCER, SCHEMA_VERSION, initialize, inspect_existing, insert_activity,
    insert_claim, insert_evidence, insert_fact, insert_source, json_text,
)

BUILD_VERSION = "deterministic-financial-evidence/1.1.0"
REQUIRED_FINANCIAL_SOURCES = {
    "msft-fy2025-q4-press-release",
    "msft-fy2024-q4-press-release",
    "msft-fy2025-q4-segment-revenues",
    "msft-fy2024-q4-segment-revenues-original-page",
    "aapl-fy2025-q4-press-release",
}


class BuildError(RuntimeError):
    """Raised on integrity, schema, or fail-closed extraction errors."""


def _stable_id(prefix: str, *parts: object) -> str:
    body = "\x1f".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(body.encode()).hexdigest()[:20]}"


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _tables_json(document) -> str:
    return json_text([
        {"index": table.index, "rows": table.rows, "start": table.start, "end": table.end}
        for table in document.tables
    ])


def _verify_sources(catalog: dict, root: Path) -> dict[str, dict]:
    verified: dict[str, dict] = {}
    seen: set[str] = set()
    root = root.resolve()
    for item in catalog.get("sources", []):
        source_id = item.get("id")
        if not source_id or source_id in seen:
            raise BuildError("catalog source IDs must be present and unique")
        seen.add(source_id)
        artifact = item.get("artifact")
        if artifact is None:
            continue
        path = (root / artifact["path"]).resolve()
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise BuildError(f"artifact path escapes repository root: {source_id}") from exc
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise BuildError(f"cannot read raw artifact for {source_id}") from exc
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) != artifact.get("bytes") or digest != artifact.get("sha256"):
            raise BuildError(f"raw integrity check failed for {source_id}")
        verified[source_id] = {"catalog": item, "raw": raw, "sha256": digest}
    missing = REQUIRED_FINANCIAL_SOURCES - verified.keys()
    if missing:
        raise BuildError(f"required loaded source missing: {', '.join(sorted(missing))}")
    return verified


def _evidence(source_id: str, digest: str, document, span: tuple[int, int],
              purpose: str, table_index=None, row_index=None) -> dict:
    start, end = span
    if start is None or end is None or document.text[start:end] == "":
        raise BuildError(f"empty evidence span for {source_id}: {purpose}")
    locator = {"type": "text_span", "start": start, "end": end}
    if table_index is not None:
        locator["table_index"] = table_index
    if row_index is not None:
        locator["row_index"] = row_index
    return {
        "evidence_id": _stable_id("ev", source_id, digest, PARSER_VERSION, purpose),
        "source_id": source_id,
        "quote": document.text[start:end],
        "locator": locator,
        "source_sha256": digest,
        "parser_version": PARSER_VERSION,
    }


def _activity(activity_id: str, kind: str, method: str, inputs: list,
              outputs: list, parameters: dict, generated_at: str) -> dict:
    return {
        "activity_id": activity_id, "type": kind, "method": method,
        "inputs": inputs, "outputs": outputs, "parameters": parameters,
        "created_at": generated_at,
    }


def _build_into(connection: sqlite3.Connection, catalog: dict, verified: dict[str, dict],
                generated_at: str) -> None:
    input_hashes = {key: value["sha256"] for key, value in sorted(verified.items())}
    input_fingerprint = hashlib.sha256(json_text({
        "build_version": BUILD_VERSION,
        "parser_version": PARSER_VERSION,
        "input_hashes": input_hashes,
    }).encode()).hexdigest()

    def derived_id(prefix: str, *parts: object) -> str:
        return _stable_id(prefix, BUILD_VERSION, PARSER_VERSION, input_fingerprint, *parts)

    initialize(connection, {
        "schema_version": SCHEMA_VERSION, "producer": PRODUCER,
        "build_version": BUILD_VERSION, "generated_at": generated_at,
        "catalog_schema_version": str(catalog.get("schema_version")),
        "input_hashes": json_text(input_hashes),
    })
    connection.executemany(
        "INSERT INTO companies VALUES(?,?,?,?)",
        [
            ("MSFT", "Microsoft Corporation", "MSFT", "Official issuer-release evidence preview."),
            ("AAPL", "Apple Inc.", "AAPL", "Official newsroom-release evidence preview."),
            ("CRM", "Salesforce, Inc.", "CRM", "Official newsroom-release document coverage only; structured facts not extracted."),
            ("IBM", "International Business Machines Corporation", "IBM", "Official newsroom-release document coverage only; structured facts not extracted."),
            ("NVDA", "NVIDIA Corporation", "NVDA", "Official newsroom-release document coverage only; structured facts not extracted."),
        ],
    )

    documents = {}
    for source_id, bundle in sorted(verified.items()):
        try:
            document = parse_html(bundle["raw"])
        except ParseError as exc:
            raise BuildError(f"HTML parse failed for {source_id}: {exc}") from exc
        documents[source_id] = document
        item = bundle["catalog"]
        artifact = item["artifact"]
        insert_source(connection, {
            "source_id": source_id,
            "source_version_id": _stable_id("srcv", source_id, bundle["sha256"]),
            "title": item["title"], "url": item["url"],
            "published_at": item.get("published_at"),
            "published_at_basis": item.get("published_at_basis") or "No publication date established.",
            "collected_at": artifact["collected_at"], "sha256": bundle["sha256"],
            "rights_status": item.get("rights", {}).get("assessment", "review_required"),
            "bytes": artifact["bytes"], "parser_version": PARSER_VERSION,
            "text": document.text, "tables_json": _tables_json(document),
        })

    evidence_rows: dict[str, dict] = {}
    fact_rows: dict[str, dict] = {}
    claim_rows: list[dict] = []

    def add_ev(row: dict) -> str:
        if row["quote"] != documents[row["source_id"]].text[row["locator"]["start"]:row["locator"]["end"]]:
            raise BuildError("evidence quote is not an exact parsed-text range")
        evidence_rows[row["evidence_id"]] = row
        return row["evidence_id"]

    # Microsoft quarterly GAAP reconciliation rows: two periods from each allowed release.
    quarter_specs = [
        ("msft-fy2025-q4-press-release", 2025),
        ("msft-fy2024-q4-press-release", 2024),
    ]
    fy25_table_evidence = None
    fy25_growth = None
    for source_id, current_year in quarter_specs:
        document = documents[source_id]
        digest = verified[source_id]["sha256"]
        try:
            extracted = microsoft_quarter_gaap(document, current_year)
        except ParseError as exc:
            raise BuildError(f"financial extraction failed for {source_id}: {exc}") from exc
        table = extracted["table"]
        table_ev = add_ev(_evidence(
            source_id, digest, document, (table.start, table.end),
            "quarter-gaap-table", table.index,
        ))
        if current_year == 2025:
            fy25_table_evidence = table_ev
            fy25_growth = {
                "reported": extracted["reported_growth"],
                "constant_currency": extracted["constant_currency_growth"],
            }
        source_fact_ids = []
        for year, values in extracted["years"].items():
            row_ev = add_ev(_evidence(
                source_id, digest, document, values["span"], f"quarter-gaap-{year}",
                table.index, values["row_index"],
            ))
            extraction_id = derived_id("act", "extract-msft-quarter", source_id, year, PARSER_VERSION)
            for metric, unit in (("revenue", "USD millions"), ("operating_income", "USD millions"),
                                 ("eps_diluted", "USD/share")):
                fact_id = derived_id("fact", source_id, year, metric, "quarter", "GAAP")
                fact_rows[fact_id] = {
                    "fact_id": fact_id, "company_id": "MSFT", "metric": metric,
                    "value": _decimal_text(values[metric]), "unit": unit,
                    "period_start": f"{year}-04-01", "period_end": f"{year}-06-30",
                    "period_kind": "quarter", "basis": "GAAP issuer reconciliation table",
                    "evidence_id": row_ev, "activity_id": extraction_id,
                }
                source_fact_ids.append(fact_id)
            insert_activity(connection, _activity(
                extraction_id, "deterministic_extraction",
                "validated_table_header_and_gaap_row/1.0.0", [source_id, row_ev],
                source_fact_ids[-3:], {"year": year, "period_kind": "quarter", "parser_version": PARSER_VERSION},
                generated_at,
            ))

    # Microsoft shareholder returns: cash-flow outflows are not interchangeable
    # with the issuer-reported combined capital-return total in release prose.
    shareholder_fact_ids = []
    shareholder_evidence_ids = []
    shareholder_activity_ids = []
    for source_id, current_year in quarter_specs:
        document = documents[source_id]
        digest = verified[source_id]["sha256"]
        try:
            extracted = microsoft_shareholder_returns(document, current_year)
        except ParseError as exc:
            raise BuildError(f"shareholder-return extraction failed for {source_id}: {exc}") from exc
        table = extracted["table"]
        for metric, row in extracted["rows"].items():
            row_ev = add_ev(_evidence(
                source_id, digest, document, row["span"],
                f"shareholder-cash-flow-{metric}-{current_year}", table.index, row["row_index"],
            ))
            shareholder_evidence_ids.append(row_ev)
            outputs = []
            activity_id = derived_id(
                "act", "extract-shareholder-cash-flow", source_id, current_year, metric,
                "cash flow statement; financing cash outflow",
            )
            for period_kind, period_start, value_key in (
                ("quarter", f"{current_year}-04-01", "quarter"),
                ("year", f"{current_year - 1}-07-01", "annual"),
            ):
                basis = "cash flow statement; financing cash outflow"
                period_end = f"{current_year}-06-30"
                fact_id = derived_id(
                    "fact", source_id, period_start, period_end, metric, period_kind, basis,
                )
                fact_rows[fact_id] = {
                    "fact_id": fact_id, "company_id": "MSFT", "metric": metric,
                    "value": _decimal_text(row[value_key]), "unit": "USD millions",
                    "period_start": period_start, "period_end": period_end,
                    "period_kind": period_kind, "basis": basis,
                    "evidence_id": row_ev, "activity_id": activity_id,
                }
                outputs.append(fact_id)
                shareholder_fact_ids.append(fact_id)
            shareholder_activity_ids.append(activity_id)
            insert_activity(connection, _activity(
                activity_id, "deterministic_extraction",
                "validated_microsoft_cash_flow_row/1.0.0", [source_id, row_ev], outputs,
                {
                    "operation": "parse parenthesized cash outflow, then store its absolute payment amount",
                    "evidence_quote": document.text[row["span"][0]:row["span"][1]],
                    "raw_values": {
                        "quarter": _decimal_text(row["quarter_raw"]),
                        "year": _decimal_text(row["annual_raw"]),
                    },
                    "cell_locators": {
                        "quarter": {"table_index": table.index, "row_index": row["row_index"], "column_index": 1},
                        "year": {"table_index": table.index, "row_index": row["row_index"], "column_index": 5},
                    },
                    "periods": {
                        "quarter": [f"{current_year}-04-01", f"{current_year}-06-30"],
                        "year": [f"{current_year - 1}-07-01", f"{current_year}-06-30"],
                    },
                    "unit": "USD millions", "cashflow_basis": True,
                }, generated_at,
            ))

        total_ev = add_ev(_evidence(
            source_id, digest, document, extracted["reported_total_span"],
            f"issuer-reported-capital-return-total-{current_year}",
        ))
        shareholder_evidence_ids.append(total_ev)
        total_basis = "issuer-reported combined capital return; release sentence; not cash-flow basis"
        total_metric = "issuer_reported_capital_return_total"
        total_fact_id = derived_id(
            "fact", source_id, f"{current_year}-04-01", f"{current_year}-06-30",
            total_metric, "quarter", total_basis,
        )
        total_activity_id = derived_id(
            "act", "extract-reported-capital-return", source_id, current_year, total_metric, total_basis,
        )
        fact_rows[total_fact_id] = {
            "fact_id": total_fact_id, "company_id": "MSFT", "metric": total_metric,
            "value": _decimal_text(extracted["reported_total"]), "unit": "USD millions",
            "period_start": f"{current_year}-04-01", "period_end": f"{current_year}-06-30",
            "period_kind": "quarter", "basis": total_basis,
            "evidence_id": total_ev, "activity_id": total_activity_id,
        }
        shareholder_fact_ids.append(total_fact_id)
        shareholder_activity_ids.append(total_activity_id)
        insert_activity(connection, _activity(
            total_activity_id, "deterministic_extraction",
            "validated_microsoft_capital_return_sentence/1.0.0", [source_id, total_ev], [total_fact_id],
            {
                "operation": "regex capture of issuer-reported USD billions, multiplied by 1000",
                "evidence_quote": document.text[
                    extracted["reported_total_span"][0]:extracted["reported_total_span"][1]
                ],
                "output_unit": "USD millions", "cashflow_basis": False,
                "component_arithmetic_performed": False,
            }, generated_at,
        ))

    # Annual segment values preserve the old presentation and later recast comparative separately.
    segment_specs = [
        ("msft-fy2024-q4-segment-revenues-original-page", (2024, 2023, 2022), "FY2024-era presentation"),
        ("msft-fy2025-q4-segment-revenues", (2025, 2024, 2023), "FY2025-page recast presentation"),
    ]
    segment_2024: dict[str, list[str]] = {}
    recast_statement_ev = None
    for source_id, years, basis in segment_specs:
        document = documents[source_id]
        digest = verified[source_id]["sha256"]
        try:
            extracted = microsoft_segments(document, years)
        except ParseError as exc:
            raise BuildError(f"segment extraction failed for {source_id}: {exc}") from exc
        table = extracted["table"]
        for section, names in extracted["sections"].items():
            for name, row in names.items():
                row_ev = add_ev(_evidence(
                    source_id, digest, document, row["span"],
                    f"segment-{section}-{name}", table.index, row["row_index"],
                ))
                for year, value in row["values"].items():
                    activity_id = derived_id("act", "extract-segment", source_id, section, name, year)
                    metric_name = f"segment_{section}_{re.sub('[^a-z0-9]+', '_', name.lower()).strip('_')}"
                    fact_id = derived_id("fact", source_id, metric_name, year, basis)
                    fact_rows[fact_id] = {
                        "fact_id": fact_id, "company_id": "MSFT", "metric": metric_name,
                        "value": _decimal_text(value), "unit": "USD millions",
                        "period_start": f"{year - 1}-07-01", "period_end": f"{year}-06-30",
                        "period_kind": "year", "basis": basis,
                        "evidence_id": row_ev, "activity_id": activity_id,
                    }
                    insert_activity(connection, _activity(
                        activity_id, "deterministic_extraction", "validated_annual_segment_row/1.0.0",
                        [source_id, row_ev], [fact_id],
                        {"year": year, "period_kind": "year", "presentation_basis": basis}, generated_at,
                    ))
                    if year == 2024:
                        segment_2024.setdefault(basis, []).append(fact_id)
        if source_id == "msft-fy2025-q4-segment-revenues":
            match = re.search(
                r"We have recast certain prior period amounts to conform to the way we internally manage and monitor our business\.",
                document.text,
            )
            if match is None:
                raise BuildError("FY2025 segment recast statement missing")
            recast_statement_ev = add_ev(_evidence(
                source_id, digest, document, match.span(), "segment-recast-statement",
            ))

    # Apple newsroom figures are source-reported; no PDF-number verification is asserted.
    apple_id = "aapl-fy2025-q4-press-release"
    apple_doc = documents[apple_id]
    try:
        apple = apple_release(apple_doc)
    except ParseError as exc:
        raise BuildError(f"Apple extraction failed: {exc}") from exc
    apple_main_ev = add_ev(_evidence(
        apple_id, verified[apple_id]["sha256"], apple_doc, apple["span"], "reported-quarter-results",
    ))
    apple_footnote_ev = add_ev(_evidence(
        apple_id, verified[apple_id]["sha256"], apple_doc, apple["footnote_span"], "adjusted-basis-footnote",
    ))
    apple_fact_ids = []
    for metric, value, unit in (
        ("revenue", apple["revenue"], "USD billions"),
        ("eps_diluted", apple["eps_diluted"], "USD/share"),
    ):
        activity_id = derived_id("act", "extract-apple", metric, PARSER_VERSION)
        fact_id = derived_id("fact", apple_id, metric, apple["fiscal_year"], "quarter", "source_reported")
        fact_rows[fact_id] = {
            "fact_id": fact_id, "company_id": "AAPL", "metric": metric,
            "value": _decimal_text(value), "unit": unit,
            "period_start": None, "period_end": apple["period_end"],
            "period_kind": "quarter", "basis": "issuer newsroom source_reported; accompanying PDF not checked",
            "evidence_id": apple_main_ev, "activity_id": activity_id,
        }
        apple_fact_ids.append(fact_id)
        insert_activity(connection, _activity(
            activity_id, "deterministic_extraction", "qualified_newsroom_regex/1.0.0",
            [apple_id, apple_main_ev, apple_footnote_ev], [fact_id],
            {"period_kind": "quarter", "pdf_numbers_checked": False, "adjusted_eps_comparison": True}, generated_at,
        ))

    for row in evidence_rows.values():
        insert_evidence(connection, row)
    for row in fact_rows.values():
        insert_fact(connection, row)

    if fy25_growth is None:
        raise BuildError("FY2025 reported/constant-currency growth was not extracted")

    # Real SQL reads of stored facts precede Decimal calculations and are fully recorded.
    calculated_claim_ids = []
    for metric in ("revenue", "operating_income", "eps_diluted"):
        # FY2024 appears in both releases, so query only the two FY2025-release facts.
        wanted = [
            fact_rows[derived_id("fact", "msft-fy2025-q4-press-release", year, metric, "quarter", "GAAP")]
            for year in (2024, 2025)
        ]
        selected_ids = [item["fact_id"] for item in wanted]
        query = "SELECT fact_id,value,period_end FROM facts WHERE fact_id IN (?,?) ORDER BY period_end"
        params = selected_ids
        rows = connection.execute(query, params).fetchall()
        if len(rows) != 2 or {row[0] for row in rows} != set(selected_ids):
            raise BuildError(f"SQL did not return exactly the two selected facts for {metric}")
        all_results = [
            {"fact_id": row[0], "value": row[1], "period_end": row[2]}
            for row in rows
        ]
        old, new = (Decimal(rows[0][1]), Decimal(rows[1][1]))
        growth = ((new / old) - Decimal(1)) * Decimal(100)
        growth_text = format(growth.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")
        activity_id = derived_id("act", "sql-decimal-yoy", metric, "2024-06-30", "2025-06-30")
        insert_activity(connection, _activity(
            activity_id, "sql_calculation", "sqlite_read_then_decimal_yoy/1.0.0",
            selected_ids, [growth_text],
            {
                "query": query, "params": params, "selected_fact_ids": selected_ids,
                "all_results": all_results, "formula": "((current/prior)-1)*100",
                "rounding": "0.01 HALF_UP",
            },
            generated_at,
        ))
        claim_id = derived_id("claim", "MSFT", metric, "quarter-yoy-2025")
        claim_rows.append({
            "claim_id": claim_id, "company_id": "MSFT",
            "text": f"Microsoft FY2025 Q4 {metric} GAAP同比（按表内两期值计算）为 {growth_text}% 。",
            "kind": "calculation", "verification_status": "verified_calculation",
            "explanation": "从SQLite读取已存两期季度GAAP事实后使用Decimal公式计算；不是从叙述答案硬编码。",
            "evidence_ids": [wanted[0]["evidence_id"], wanted[1]["evidence_id"]],
            "fact_ids": [item["fact_id"] for item in wanted], "activity_ids": [activity_id],
            "limitations": ([
                "表内金额以百万美元列示，百分比按未缩放表值计算。"
            ] if metric != "eps_diluted" else [
                "使用披露中已舍入的每股收益计算；结果可能与发行方用更高精度数值计算的增长率不同。"
            ]),
        })
        calculated_claim_ids.append(claim_id)

    claim_rows.extend([
        {
            "claim_id": derived_id("claim", "MSFT", "shareholder-returns-2024-2025"),
            "company_id": "MSFT",
            "text": "微软FY2024和FY2025第四季度及全年现金流量表分别披露了普通股回购和已付现金股利；新闻稿另行报告第四季度股东资本回报总额。",
            "kind": "fact", "verification_status": "source_reported",
            "explanation": "金额由特定微软新闻稿的严格表格行和原文句子确定性提取。这验证来源与数据结构，不声称审计财务正确性；报告总额与现金流量表项目口径不同。",
            "evidence_ids": shareholder_evidence_ids, "fact_ids": shareholder_fact_ids,
            "activity_ids": shareholder_activity_ids,
            "limitations": [
                "股东资本回报总额未用股利与回购相加、相减或反推。",
                "现金流量表付款金额由括号负数规范化为正的付款额。",
                "来源权利状态保持review_required；结构提取不构成审计保证。",
            ],
        },
        {
            "claim_id": derived_id("claim", "MSFT", "reported-vs-constant-currency-2025-q4"),
            "company_id": "MSFT",
            "text": f"微软FY2025 Q4收入表列同比为{fy25_growth['reported']}，固定汇率口径为{fy25_growth['constant_currency']}。",
            "kind": "fact", "verification_status": "qualified",
            "explanation": f"两者均从同一官方对账表提取；{fy25_growth['constant_currency']}是固定汇率补充口径，不应替代或混同{fy25_growth['reported']}的报告口径。",
            "evidence_ids": [fy25_table_evidence], "fact_ids": [], "activity_ids": [],
            "limitations": ["固定汇率为发行方补充口径。"],
        },
        {
            "claim_id": derived_id("claim", "MSFT", "segment-2024-presentations"),
            "company_id": "MSFT",
            "text": "FY2024分部年度值在FY2024原展示与FY2025页面重列比较栏中并列保留，未跨口径计算同比。",
            "kind": "fact", "verification_status": "qualified",
            "explanation": "FY2025页面说明重列是为符合内部管理和监控业务方式；这是展示/可比性变化，本切片不称其为会计差错。",
            "evidence_ids": [recast_statement_ev],
            "fact_ids": segment_2024.get("FY2024-era presentation", []) + segment_2024.get("FY2025-page recast presentation", []),
            "activity_ids": [], "limitations": ["不同展示口径的分部值不得混算同比。"],
        },
        {
            "claim_id": derived_id("claim", "MSFT", "independent-ai-roi-insufficient"),
            "company_id": "MSFT",
            "text": "在本次已载入资料中，尚未建立可独立复算的AI投资回报率（ROI）结论。",
            "kind": "inference", "verification_status": "insufficient",
            "explanation": "已载入发行方材料中的管理层业务观点不等同于具备独立投入、收益和归因数据的ROI计算。此结论仅限本次资料集，不推断其他资料或全球披露情况。",
            "evidence_ids": [], "fact_ids": [], "activity_ids": [],
            "limitations": ["未检查所有可能资料。", "管理层观点与独立ROI证据须区分。"],
        },
        {
            "claim_id": derived_id("claim", "AAPL", f"reported-quarter-{apple['fiscal_year']}"),
            "company_id": "AAPL",
            "text": (
                f"苹果新闻稿报告FY{apple['fiscal_year']}第四季度收入{_decimal_text(apple['revenue'])}十亿美元"
                f"（同比{apple['revenue_growth']}%）、摊薄EPS {_decimal_text(apple['eps_diluted'])}美元；"
                f"{apple['eps_growth']}%的EPS同比比较为adjusted basis。"
            ),
            "kind": "fact", "verification_status": "source_reported",
            "explanation": "数字和adjusted限定来自新闻稿正文及脚注；本切片未从所链接PDF核对数字，因此不声称计算验证。",
            "evidence_ids": [apple_main_ev, apple_footnote_ev], "fact_ids": apple_fact_ids,
            "activity_ids": [fact_rows[fid]["activity_id"] for fid in apple_fact_ids],
            "limitations": [
                "未下载或核对随附PDF中的财务报表与对账表。",
                f"{apple['eps_growth']}%比较不得称为GAAP增长。",
                "季度起始日未由已载入材料证实，保持null。",
            ],
        },
    ])
    for claim in claim_rows:
        insert_claim(connection, claim)


def verify_archives(path) -> tuple[Path, ...]:
    """Verify content-addressed archive bytes without rebuilding or trusting modes.

    Mode 0444 is only an accidental-write barrier for the local user, not WORM: the
    owner can chmod the file. Digest verification is therefore the security boundary.
    """
    target = Path(path).resolve()
    if target.is_file():
        archives = (target,)
    elif target.is_dir():
        archives = tuple(sorted(target.glob("*.sqlite3")))
    else:
        raise BuildError("archive path does not exist")
    for archive in archives:
        expected = archive.stem
        if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise BuildError(f"invalid archive name: {archive.name}")
        try:
            actual = hashlib.sha256(archive.read_bytes()).hexdigest()
        except OSError as exc:
            raise BuildError(f"cannot read archive: {archive.name}") from exc
        if actual != expected:
            raise BuildError(f"archive digest mismatch: {archive.name}")
    return archives


def _archive_existing(db_path: Path) -> Path | None:
    """Retain exact prior database bytes at a checked content-addressed path."""
    if not db_path.exists():
        return None
    try:
        raw = db_path.read_bytes()
    except OSError as exc:
        raise BuildError("cannot read existing database for archival") from exc
    digest = hashlib.sha256(raw).hexdigest()
    archive_dir = db_path.parent / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"{digest}.sqlite3"
    try:
        descriptor = os.open(archive_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        verify_archives(archive_path)
        try:
            if archive_path.read_bytes() != raw:
                raise BuildError("archive hash collision; refusing to replace existing database")
        except OSError as exc:
            raise BuildError("cannot check existing archive; refusing to replace database") from exc
    except OSError as exc:
        archive_path.unlink(missing_ok=True)
        raise BuildError("cannot archive existing database; refusing to replace it") from exc
    try:
        os.chmod(archive_path, 0o444)
        verify_archives(archive_path)
    except OSError as exc:
        raise BuildError("cannot protect archive; refusing to replace database") from exc
    return archive_path


def build_database(catalog_path=None, db_path=None, root=None) -> Path:
    """Verify inputs and atomically build a SQLite derived artifact."""
    package_root = Path(__file__).resolve().parent.parent
    catalog_path = Path(catalog_path or package_root / "data/source-catalog.json").resolve()
    db_path = Path(db_path or package_root / "var/evidence.sqlite3").resolve()
    root = Path(root).resolve() if root else catalog_path.parent.parent.resolve()
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"cannot load source catalog: {exc}") from exc
    verified = _verify_sources(catalog, root)  # Verify all raw bytes before any database write.
    try:
        inspect_existing(db_path)
    except RuntimeError as exc:
        raise BuildError(str(exc)) from exc
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=db_path.name + ".", suffix=".tmp", dir=db_path.parent)
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        connection = sqlite3.connect(temp_path)
        try:
            with connection:
                _build_into(connection, catalog, verified, generated_at)
                result = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if result != "ok":
                    raise BuildError(f"SQLite integrity check failed: {result}")
        finally:
            connection.close()
        _archive_existing(db_path)
        os.replace(temp_path, db_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return db_path


def main() -> int:
    try:
        path = build_database()
    except (BuildError, ParseError) as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 1
    print(f"built {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
