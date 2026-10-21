"""Deterministic, single-company OLS over explicitly selected frozen L2 facts."""
from copy import deepcopy
from datetime import date
from fractions import Fraction
import re

from .research_tools import ToolError, rounded_rational as _rounded


OPERATIONS = frozenset({"regression_slope", "regression_intercept", "regression_r_squared"})
PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["method", "x_fact_ids", "y_fact_ids", "y_prior_fact_ids", "y_transform", "lag_years"],
    "properties": {
        "method": {"type": "string", "enum": ["ols_with_intercept"]},
        "x_fact_ids": {"type": "array", "minItems": 3, "maxItems": 40,
                       "uniqueItems": True, "items": {"type": "string", "maxLength": 200}},
        "y_fact_ids": {"type": "array", "minItems": 3, "maxItems": 40,
                       "uniqueItems": True, "items": {"type": "string", "maxLength": 200}},
        "y_prior_fact_ids": {"type": "array", "maxItems": 40,
                             "uniqueItems": True, "items": {"type": "string", "maxLength": 200}},
        "y_transform": {"type": "string", "enum": ["level", "percent_change"]},
        "lag_years": {"type": "integer", "minimum": 0, "maximum": 5},
    },
}
LIMITATIONS = [
    "普通最小二乘法包含截距，只描述所选样本的线性关联，不证明因果关系。",
    "没有检验显著性、残差自相关、异方差、遗漏变量或样本外预测能力。",
    "样本由确认方案逐项指定；缺失期间不补零，不插值，也不自动替换或删除样本。",
]


def validate_plan(plan):
    if not isinstance(plan, dict) or set(plan) != set(PLAN_SCHEMA["required"]):
        raise ToolError("invalid_regression_plan", "Regression requires an explicit model, sample, transform and time alignment")
    if (plan["method"] != "ols_with_intercept" or not isinstance(plan["y_transform"], str)
            or plan["y_transform"] not in {"level", "percent_change"}
            or type(plan["lag_years"]) is not int or not 0 <= plan["lag_years"] <= 5):
        raise ToolError("invalid_regression_plan", "Unsupported regression model, transform or lag")
    for key in ("x_fact_ids", "y_fact_ids", "y_prior_fact_ids"):
        ids = plan[key]
        if (not isinstance(ids, list) or len(ids) > 40
                or any(not isinstance(item, str) or not 0 < len(item) <= 200 for item in ids)
                or len(set(ids)) != len(ids)):
            raise ToolError("invalid_regression_plan", "Sample fact IDs must be distinct bounded lists")
    count = len(plan["x_fact_ids"])
    prior_count = count if plan["y_transform"] == "percent_change" else 0
    if not 3 <= count <= 40 or len(plan["y_fact_ids"]) != count or len(plan["y_prior_fact_ids"]) != prior_count:
        raise ToolError("invalid_regression_plan", "Provide 3..40 complete paired observations, including prior-year Y facts for growth")
    return deepcopy(plan)


def _period(fact):
    try:
        start, end = date.fromisoformat(fact["period_start"]), date.fromisoformat(fact["period_end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ToolError("invalid_regression_sample", "Sample periods must be complete calendar dates") from exc
    if end <= start or fact.get("period_kind") not in {"year", "quarter"}:
        raise ToolError("invalid_regression_sample", "Regression accepts explicitly dated annual or quarterly flow periods")
    return start, end


def _aligned(earlier, later, years):
    return all(b.year - a.year == years and (a.month, a.day) == (b.month, b.day)
               for a, b in zip(_period(earlier), _period(later)))


def resolve_sample(research, plan):
    plan = validate_plan(plan)
    series = []
    for key in ("x_fact_ids", "y_fact_ids", "y_prior_fact_ids"):
        facts = []
        for identifier in plan[key]:
            fact = research._facts.get(identifier)
            if fact is None or fact.get("company_id") != research.company_id:
                raise ToolError("regression_fact_unavailable", "Read the exact proposed frozen facts before calculating; do not substitute another sample")
            if not isinstance(fact.get("value"), str) or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", fact["value"]) or len(fact["value"]) > 100:
                raise ToolError("invalid_regression_sample", "Regression values must be finite decimal strings of at most 100 characters")
            _period(fact)
            facts.append(fact)
        if facts:
            research._require_same(tuple(facts), ("metric", "unit", "basis", "period_kind"))
            research._require_same_optional_scope(tuple(facts))
            if any(_period(a)[1] >= _period(b)[0] for a, b in zip(facts, facts[1:])):
                raise ToolError("invalid_regression_sample", "Sample periods must be ordered, non-overlapping and not duplicated")
        series.append(facts)
    xs, ys, priors = series
    research._require_same(tuple(xs + ys + priors), ("period_kind",))
    research._require_same_optional_scope(tuple(xs + ys + priors))
    if priors:
        research._require_same(tuple(ys + priors), ("metric", "unit", "basis", "period_kind"))
    for index, (x, y) in enumerate(zip(xs, ys)):
        if not _aligned(x, y, plan["lag_years"]):
            raise ToolError("invalid_regression_sample", "X and Y periods do not match the confirmed whole-year lag")
        if priors and (not _aligned(priors[index], y, 1) or Fraction(priors[index]["value"]) == 0):
            raise ToolError("invalid_regression_sample", "Growth requires a nonzero, corresponding prior-year Y baseline")
    return plan, xs, ys, priors


def describe_plan(research, plan):
    plan, xs, ys, priors = resolve_sample(research, plan)
    def display(fact):
        return {key: fact.get(key) for key in (
            "metric", "value", "unit", "basis", "period_start", "period_end", "period_kind",
            "scope", "segment", "geography")}
    return {
        "method": "含截距的一元普通最小二乘回归",
        "equation": "Y = 截距 + 斜率 × X",
        "y_transform": plan["y_transform"], "lag_years": plan["lag_years"],
        "sample_count": len(xs),
        "samples": [{"x": display(x), "y": display(y),
                     "y_prior": display(priors[index]) if priors else None}
                    for index, (x, y) in enumerate(zip(xs, ys))],
        "limitations": LIMITATIONS + (["样本少于10组，结果尤其容易受单个观测影响。"] if len(xs) < 10 else []),
    }


def input_fact_ids(plan):
    return list(dict.fromkeys(plan["x_fact_ids"] + plan["y_fact_ids"] + plan["y_prior_fact_ids"]))


def model_result(result):
    """Project recorded outputs for the model without copying every source three times."""
    if not isinstance(result, dict) or "error" in result or not result.get("calculations"):
        return result
    records = result["calculations"]
    model = records[0]["regression"]
    projection = {
        "schema_version": "regression-model-view/1",
        "all_calculation_outputs_and_ids_included": True,
        "full_sample_and_source_records": "persisted by the runner; open each calculation citation in the complete derivation reader",
        "report_values": result["report_values"],
        "calculations": [{key: item[key] for key in (
            "calculation_id", "operation", "formula", "value", "unit")} for item in records],
        "model": {key: model[key] for key in (
            "method", "equation", "sample_count", "y_transform", "lag_years", "limitations")},
        "undefined_statistics": result.get("undefined_statistics", []),
        "interpretation": result["interpretation"],
    }
    if "_tool_budget" in result:
        projection["_tool_budget"] = result["_tool_budget"]
    if "_retrieval_policy" in result:
        projection["_retrieval_policy"] = {key: value for key, value in result["_retrieval_policy"].items()
                                           if key != "known_refs"}
        projection["retrieval_candidates_repeated_in_policy"] = "omitted; existing retrieval results remain available"
    return projection


def calculate(research, plan, authorization):
    from decimal import Decimal
    plan, xs, ys, priors = resolve_sample(research, plan)
    if not isinstance(authorization, dict) or set(authorization) != {"decision_id", "option_id", "source_run_id"}:
        raise ToolError("regression_confirmation_required", "An accepted structured regression plan is required")
    if any(not isinstance(value, str) or not value for value in authorization.values()):
        raise ToolError("regression_confirmation_required", "Regression authorization is incomplete")
    x_values = [Fraction(fact["value"]) for fact in xs]
    y_values = [Fraction(fact["value"]) for fact in ys]
    if priors:
        y_values = [(current - Fraction(prior["value"])) / abs(Fraction(prior["value"])) * 100
                    for current, prior in zip(y_values, priors)]
    n = len(xs)
    mean_x, mean_y = sum(x_values) / n, sum(y_values) / n
    sxx = sum((value - mean_x) ** 2 for value in x_values)
    syy = sum((value - mean_y) ** 2 for value in y_values)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(x_values, y_values))
    if not sxx:
        raise ToolError("regression_constant_x", "X has no variation; a unique regression slope cannot be estimated")
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    r_squared = sxy * sxy / (sxx * syy) if syy else None
    y_unit = "%" if priors else ys[0]["unit"]
    slope_unit = "x" if not priors and xs[0]["unit"] == y_unit else f"{'百分点' if priors else y_unit} / {xs[0]['unit']}"
    inputs = list({fact["fact_id"]: fact for fact in xs + ys + priors}.values())
    description = describe_plan(research, plan)
    undefined = []
    if r_squared is None:
        reason = "Y在所选样本中没有变化，R²的分母为零，因此未定义；不得将其填为0或100%。"
        description["limitations"].append(reason)
        undefined.append({"operation": "regression_r_squared", "status": "undefined", "reason": reason})
    model = {
        **description,
        "arithmetic": "Exact rational arithmetic; outputs rounded HALF_UP to 6 decimals. Displayed intermediates use 12 decimals and are not reused for calculation.",
        "growth_formula": "(本期Y原值 - 上年同期Y原值) / abs(上年同期Y原值) × 100" if priors else None,
        "observations": [{"x": x["value"], "y": _rounded(y, 12)}
                         for x, y in zip(xs, y_values)],
        "statistics": {key: _rounded(value, 12) for key, value in (
            ("mean_x", mean_x), ("mean_y", mean_y), ("sxx", sxx), ("syy", syy), ("sxy", sxy))},
    }
    outputs = [
        ("regression_slope", slope, slope_unit, "斜率 = Σ((X - X均值) × (Y - Y均值)) / Σ((X - X均值)²)"),
        ("regression_intercept", intercept, y_unit, "截距 = Y均值 - 斜率 × X均值"),
    ]
    if r_squared is not None:
        outputs.append(("regression_r_squared", r_squared * 100, "%",
                        "R² = Σ((X - X均值) × (Y - Y均值))² / (Σ((X - X均值)²) × Σ((Y - Y均值)²)) × 100%"))
    records = []
    for operation, value, unit, formula in outputs:
        record = research._calculation_record(
            operation=operation, facts=tuple(inputs), comparison_mode="confirmed_regression",
            formula=formula, value=Decimal(_rounded(value)), unit=unit,
            rule="exact-rational-ols-with-intercept-v1",
            limitations=description["limitations"] + [model["arithmetic"]],
            parameters={"plan": plan, "authorization": authorization},
        )
        record["regression"] = deepcopy(model)
        records.append(record)
    labels = {"regression_slope": "斜率", "regression_intercept": "截距", "regression_r_squared": "样本内R²"}
    return {
        "company_id": research.company_id, "data_version": research.data_version,
        "report_values": [{"label": labels[item["operation"]],
                           "value_text": f"{item['value']} {item['unit']}",
                           "citation": f"[calculation:{item['calculation_id']}]"}
                          for item in records],
        "calculations": records, "interpretation": "unverified",
        "undefined_statistics": undefined,
        "instruction": (
            "Publish each report_values entry with its complete value_text and its OWN citation in the same "
            "sentence or table cell. Do not cite slope to prove intercept or R². Do not convert the recorded "
            "percentage R² to a newly rounded unitless number. The reader already exposes all sample facts, "
            "formulas and intermediate statistics from these records: do not reconstruct or round them in prose. "
            "Use ordinary Markdown/text, not LaTeX delimiters (unsupported by the reader). "
            "Cite the actual records when explaining their recorded method and limitations. "
            "Explicitly disclose undefined_statistics; never invent a number or citation for an undefined statistic. "
            "Association is not causation, significance or a forecast."
        ),
    }
