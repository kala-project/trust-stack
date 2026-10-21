"""Confirmed annual cash-flow scenarios; model estimates never become L2 facts."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction
import re

from .research_tools import ToolError, rounded_rational


OPERATIONS = frozenset({"forecast_operating_cash_flow", "forecast_capital_expenditures", "forecast_free_cash_flow"})
_ANNUAL_PERIOD_DAYS = frozenset({364, 365, 366, 371})
_PERIOD_PROPERTIES = {
    "period_start": {"type": "string", "format": "date"},
    "period_end": {"type": "string", "format": "date"},
    "cfo_growth_percent": {"type": "string", "maxLength": 20},
    "capex_growth_percent": {"type": "string", "maxLength": 20},
}
PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["method", "operating_cash_flow_fact_id", "capital_expenditures_fact_id", "capex_sign", "periods"],
    "properties": {
        "method": {"type": "string", "enum": ["annual_cash_flow_growth"]},
        "operating_cash_flow_fact_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "capital_expenditures_fact_id": {"type": "string", "minLength": 1, "maxLength": 200},
        "capex_sign": {"type": "string", "enum": ["negative_cash_outflow", "positive_payment_amount"]},
        "periods": {"type": "array", "minItems": 1, "maxItems": 10,
                    "items": {"type": "object", "additionalProperties": False,
                              "required": list(_PERIOD_PROPERTIES), "properties": _PERIOD_PROPERTIES}},
    },
}
LIMITATIONS = [
    "这是经确认假设下的条件情景，不是未来事实、管理层指引或已验证预测。",
    "经营现金流与资本开支分别按确认的逐期增长率递推；不自动预测收入、利润、营运资本或融资。",
    "自由现金流仅定义为经营现金流减资本开支支付金额；不包含未列明的租赁、收购或其他调整。",
    "桥接估计表示期间已结束但本模型未采用实绩；不得将估计改称已披露数据。",
]


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ToolError("invalid_forecast_plan", "Use complete ISO calendar dates")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ToolError("invalid_forecast_plan", "Invalid calendar date") from exc


def _number(value, maximum=100):
    if not isinstance(value, str) or len(value) > maximum or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
        raise ToolError("invalid_forecast_plan", "Values and assumptions must be bounded finite decimal strings")
    return Fraction(value)


def validate_plan(plan):
    if (not isinstance(plan, dict) or set(plan) != set(PLAN_SCHEMA["required"])
            or plan["method"] != "annual_cash_flow_growth"
            or not isinstance(plan["capex_sign"], str)
            or plan["capex_sign"] not in {"negative_cash_outflow", "positive_payment_amount"}):
        raise ToolError("invalid_forecast_plan", "Specify the supported model and the native CapEx sign convention")
    for key in ("operating_cash_flow_fact_id", "capital_expenditures_fact_id"):
        if not isinstance(plan[key], str) or not 0 < len(plan[key]) <= 200:
            raise ToolError("invalid_forecast_plan", "Both actual baseline fact IDs are required")
    if not isinstance(plan["periods"], list) or not 1 <= len(plan["periods"]) <= 10:
        raise ToolError("invalid_forecast_plan", "Specify 1..10 consecutive annual scenario periods")
    for period in plan["periods"]:
        if not isinstance(period, dict) or set(period) != set(_PERIOD_PROPERTIES):
            raise ToolError("invalid_forecast_plan", "Each period needs dates and both explicit growth assumptions")
        start, end = _date(period["period_start"]), _date(period["period_end"])
        if (end - start).days + 1 not in _ANNUAL_PERIOD_DAYS:
            raise ToolError("invalid_forecast_plan", "This model accepts full annual periods only")
        for key in ("cfo_growth_percent", "capex_growth_percent"):
            if _number(period[key], 20) < -100:
                raise ToolError("invalid_forecast_plan", "Growth below -100% is not supported by this model")
    return deepcopy(plan)


def input_fact_ids(plan):
    return [plan["operating_cash_flow_fact_id"], plan["capital_expenditures_fact_id"]]


def _baseline(research, plan, as_of_date):
    plan = validate_plan(plan)
    reference = _date(as_of_date)
    facts = [research._facts.get(identifier) for identifier in input_fact_ids(plan)]
    if any(not isinstance(fact, dict) or fact.get("company_id") != research.company_id for fact in facts):
        raise ToolError("forecast_fact_unavailable", "Read both frozen baseline facts before proposing the model")
    cfo, capex = facts
    if (cfo.get("metric") != "operating_cash_flow" or capex.get("metric") != "capital_expenditures"
            or cfo.get("period_kind") != "year" or capex.get("period_kind") != "year"):
        raise ToolError("invalid_forecast_baseline", "Use explicitly interpreted annual operating_cash_flow and capital_expenditures facts")
    research._require_same(tuple(facts), ("unit", "basis", "period_start", "period_end", "period_kind"))
    research._require_same_optional_scope(tuple(facts))
    if (not isinstance(cfo.get("unit"), str)
            or not re.fullmatch(r"(?:USD|CNY)(?: (?:millions|billions))?", cfo["unit"])):
        raise ToolError("invalid_forecast_baseline", "This model currently supports USD/CNY amounts, millions or billions without currency conversion")
    start, end = _date(cfo["period_start"]), _date(cfo["period_end"])
    if (end - start).days + 1 not in _ANNUAL_PERIOD_DAYS or end >= reference:
        raise ToolError("invalid_forecast_baseline", "Use a completed actual annual baseline before the scenario reference date")
    _number(cfo.get("value"))
    amount = _number(capex.get("value"))
    if ((plan["capex_sign"] == "negative_cash_outflow" and amount > 0)
            or (plan["capex_sign"] == "positive_payment_amount" and amount < 0)):
        raise ToolError("invalid_forecast_baseline", "The confirmed CapEx convention conflicts with the native signed value")
    for period in plan["periods"]:
        following = _date(period["period_start"])
        if following - end != timedelta(days=1):
            raise ToolError("invalid_forecast_plan", "Periods must begin immediately after the baseline, without missing bridge years")
        end = _date(period["period_end"])
        for fact in research._facts.values():
            if (fact.get("metric") in {"operating_cash_flow", "capital_expenditures"}
                    and fact.get("company_id") == research.company_id
                    and all(fact.get(key) == cfo.get(key) for key in ("unit", "basis", "scope", "segment", "geography"))
                    and fact.get("period_kind") == "year"
                    and fact.get("period_start") == period["period_start"] and fact.get("period_end") == period["period_end"]):
                raise ToolError("forecast_actual_available", "An actual for a modeled period is already loaded; propose an updated baseline instead of silently replacing it with an estimate")
    return plan, cfo, capex, reference


def describe_plan(research, plan, *, as_of_date):
    plan, cfo, capex, reference = _baseline(research, plan, as_of_date)
    keys = ("fact_id", "metric", "value", "unit", "basis", "period_start", "period_end", "scope", "segment", "geography")
    return {
        "method": "年度经营现金流与资本开支独立增长情景",
        "as_of_date": as_of_date,
        "equation": "自由现金流 = 经营现金流 - 资本开支支付金额",
        "baseline": {name: {key: fact.get(key) for key in keys}
                     for name, fact in (("operating_cash_flow", cfo), ("capital_expenditures", capex))},
        "capex_sign": plan["capex_sign"],
        "capex_conversion": "资本开支支付金额 = -原表现金流出" if plan["capex_sign"] == "negative_cash_outflow"
                            else "资本开支支付金额 = 原表正数支付金额",
        "periods": [{**period, "classification": "bridge_estimate" if _date(period["period_end"]) < reference else "conditional_forecast"}
                    for period in plan["periods"]],
        "limitations": list(LIMITATIONS),
    }


def calculate(research, plan, authorization, *, as_of_date):
    plan, cfo, capex, _ = _baseline(research, plan, as_of_date)
    if (not isinstance(authorization, dict) or set(authorization) != {"decision_id", "option_id", "source_run_id"}
            or any(not isinstance(value, str) or not value for value in authorization.values())):
        raise ToolError("forecast_confirmation_required", "An accepted structured forecast plan is required")
    description = describe_plan(research, plan, as_of_date=as_of_date)
    cash = _number(cfo["value"])
    investment = _number(capex["value"]) * (-1 if plan["capex_sign"] == "negative_cash_outflow" else 1)
    records, steps, previous_ids = [], [], {}
    labels = {"forecast_operating_cash_flow": "情景经营现金流",
              "forecast_capital_expenditures": "情景资本开支支付金额", "forecast_free_cash_flow": "情景自由现金流"}
    for index, period in enumerate(description["periods"]):
        previous_cash, previous_investment = cash, investment
        cash *= 1 + _number(period["cfo_growth_percent"]) / 100
        investment *= 1 + _number(period["capex_growth_percent"]) / 100
        step = {**period, "previous_cfo": rounded_rational(previous_cash, 12),
                "previous_capex_payment": rounded_rational(previous_investment, 12),
                "cfo": rounded_rational(cash), "capex_payment": rounded_rational(investment),
                "free_cash_flow": rounded_rational(cash - investment)}
        ids = {}
        for operation, value, formula, deps in (
            ("forecast_operating_cash_flow", cash, "本期经营现金流 = 上期经营现金流 × (1 + 本期假设增长率 / 100)",
             [previous_ids["cfo"]] if previous_ids else []),
            ("forecast_capital_expenditures", investment, "本期资本开支支付金额 = 上期资本开支支付金额 × (1 + 本期假设增长率 / 100)",
             [previous_ids["capex"]] if previous_ids else []),
            ("forecast_free_cash_flow", cash - investment, description["equation"], None),
        ):
            record = research._calculation_record(
                operation=operation, facts=(cfo, capex), comparison_mode="confirmed_forecast",
                formula=formula, value=Decimal(rounded_rational(value)), unit=cfo["unit"],
                rule="exact-rational-annual-cash-flow-growth-v1", limitations=LIMITATIONS,
                parameters={"plan": plan, "authorization": authorization, "as_of_date": as_of_date, "period_index": index})
            key = {"forecast_operating_cash_flow": "cfo", "forecast_capital_expenditures": "capex",
                   "forecast_free_cash_flow": "fcf"}[operation]
            ids[key] = record["calculation_id"]
            record["forecast"] = {
                "as_of_date": as_of_date, "period": dict(period), "step": dict(step),
                "depends_on_calculation_ids": deps if deps is not None else [ids["cfo"], ids["capex"]],
                "capex_conversion": description["capex_conversion"],
                "arithmetic": "Exact rational recursion without intermediate rounding; displayed outputs HALF_UP to 6 decimals, prior-period intermediates to 12 decimals.",
            }
            records.append(record)
        steps.append({**step, "calculation_ids": ids})
        previous_ids = ids
    return {
        "company_id": research.company_id, "data_version": research.data_version,
        "model": description, "steps": steps, "calculations": records, "interpretation": "conditional_scenario",
        "report_values": [{"label": labels[item["operation"]], "period": item["forecast"]["period"],
                           "value_text": f"{item['value']} {item['unit']}",
                           "citation": f"[calculation:{item['calculation_id']}]"} for item in records],
        "instruction": "Publish conditional estimates, not facts. Label every bridge period. Each output needs its metric, full ISO start/end dates, bridge_estimate (桥接估计) or conditional_forecast (条件预测), exact amount/unit and its OWN citation. A comparison table may put dates/classification in its first column and metrics in column headers; each numeric cell must carry its own amount/unit and citation. Do not round or reconstruct results. The reader exposes all assumptions, recursive steps and native baselines.",
    }


def model_result(result):
    if not isinstance(result, dict) or "error" in result or not result.get("calculations"):
        return result
    projected = {key: result[key] for key in ("company_id", "report_values", "interpretation", "instruction")}
    projected["schema_version"] = "forecast-model-view/1"
    projected["model"] = {key: value for key, value in result["model"].items() if key != "baseline"}
    projected["baseline_sources"] = [
        {**{key: fact.get(key) for key in ("metric", "value", "unit", "basis", "period_start", "period_end",
                                          "scope", "segment", "geography", "raw_value", "row_label")},
         "citation": f"[evidence:{fact['evidence_id']}]"}
        for fact in result["calculations"][0]["inputs"]]
    projected["baseline_citation_instruction"] = "Use the exact evidence citations above for baseline amounts and context. fact_id is an internal lookup key, not a supported report citation."
    projected["full_derivation"] = "All recursive steps, output records and native inputs are preserved by the runner."
    if "_tool_budget" in result:
        projected["_tool_budget"] = result["_tool_budget"]
    return projected
