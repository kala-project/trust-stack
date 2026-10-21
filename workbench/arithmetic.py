"""Composable L3 arithmetic, with exact values and a bounded replayable dependency graph."""
from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
import json

from .research_tools import ToolError, _text, rounded_rational


VERSION = "research-arithmetic/1"
MAX_INPUTS = 50
MAX_STEPS = 32
MAX_SOURCE_FACTS = 120
MAX_RECORD_BYTES = 512_000
CONTEXT_FIELDS = ("company_id", "unit", "basis", "period_kind", "scope", "segment", "geography")


def is_current(record):
    return (isinstance(record, dict) and isinstance(record.get("parameters"), dict)
            and record["parameters"].get("execution_version") == VERSION)


def _display(value, operation, places):
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator == 1 and operation not in {"percent_change", "percent_ratio", "percentage_point_difference"}:
        text = rounded_rational(value, max(twos, fives))
        return text.rstrip("0").rstrip(".") if "." in text else text
    return rounded_rational(value, places)


def _fraction_record(value):
    return {"numerator": str(value.numerator), "denominator": str(value.denominator)}


def _operand(research, identifier):
    if identifier in research._facts:
        fact = research._facts[identifier]
        if fact.get("company_id") not in getattr(research, "company_ids", (research.company_id,)):
            raise ToolError("not_found_or_out_of_scope", "all inputs must belong to the authorized research scope")
        research._require_same((fact,), ("metric", "unit", "basis", "period_start", "period_end", "period_kind"))
        value = Fraction(research._decimal_values((fact,))[0])
        context = {key: fact.get(key) for key in CONTEXT_FIELDS}
        context.update(metric=fact["metric"], period_start=fact["period_start"], period_end=fact["period_end"],
                       periods=[[fact["period_start"], fact["period_end"]]])
        return value, context, [fact], [], {
            "reference": identifier, "kind": "fact", "metric": fact["metric"],
            "value": fact["value"], "unit": fact["unit"],
            **({"company_id": fact["company_id"]} if len(getattr(research, "company_ids", ())) > 1 else {}),
        }
    record = research._calculations.get(identifier)
    if record is None:
        raise ToolError("not_found_or_out_of_scope", "input is not a frozen fact or a calculation from this research")
    exact = record["arithmetic"]["exact_value"]
    value = Fraction(int(exact["numerator"]), int(exact["denominator"]))
    parameters = record["parameters"]
    steps = parameters["dependencies"] + [{
        "calculation_id": identifier, "operation": record["operation"],
        "fact_ids": parameters["input_refs"], "comparison_mode": record["comparison_mode"],
        "decimal_places": parameters["decimal_places"],
    }]
    return value, record["arithmetic"]["output_context"], record["inputs"], steps, {
        "reference": identifier, "kind": "calculation", "metric": None,
        "value": record["value"], "unit": record["unit"],
    }


def calculate(research, *, operation, fact_ids, comparison_mode="comparable", decimal_places=None):
    operation = _text(operation, "operation", maximum=30)
    comparison_mode = _text(comparison_mode, "comparison_mode", maximum=40)
    if operation not in {"sum", "difference", "ratio", "percent_change", "percent_ratio", "percentage_point_difference"}:
        raise ToolError("invalid_arguments", "operation is not supported")
    if comparison_mode not in {"comparable", "presentation_difference", "cross_subject"}:
        raise ToolError("invalid_arguments", "comparison_mode is not supported")
    if (not isinstance(fact_ids, list) or not 2 <= len(fact_ids) <= MAX_INPUTS
            or (operation != "sum" and len(fact_ids) != 2)):
        raise ToolError("invalid_arguments", "sum needs 2..50 input references; other operations need two")
    places = (2 if operation in {"percent_change", "percent_ratio", "percentage_point_difference"} else 12) if decimal_places is None else decimal_places
    if type(places) is not int or not 0 <= places <= 24:
        raise ToolError("invalid_arguments", "decimal_places must be an integer from 0 to 24")
    identifiers = [_text(item, "input reference", maximum=200) for item in fact_ids]
    operands = [_operand(research, item) for item in identifiers]
    values = [item[0] for item in operands]
    contexts = tuple(item[1] for item in operands)
    first = contexts[0]
    same_period = all(item["periods"] == first["periods"] for item in contexts[1:])
    if operation == "percent_ratio" and not same_period:
        raise ToolError("incompatible_facts", "percentage ratio requires the same input periods")
    if operation == "percentage_point_difference" and any(item["unit"] != "%" for item in contexts):
        raise ToolError("incompatible_facts", "percentage point difference requires two percentage (%) inputs")
    limitations = ["The arithmetic passed mechanical checks; its economic interpretation remains unverified.",
                   "Inputs are explicitly selected by the researcher; arithmetic does not establish additivity or rule out double counting."]
    for key in CONTEXT_FIELDS:
        if key == "basis" and comparison_mode == "presentation_difference":
            continue
        if key == "company_id":
            if comparison_mode == "cross_subject":
                continue
            identities = [tuple(item.get("company_ids") or [item["company_id"]]) for item in contexts]
            differs = any(item != identities[0] for item in identities[1:])
        else:
            differs = any(item[key] != first[key] for item in contexts[1:])
        if differs:
            raise ToolError("incompatible_facts", f"inputs have different {key}")
    if comparison_mode == "cross_subject":
        subjects = sorted({identifier for item in contexts
                           for identifier in (item.get("company_ids") or [item["company_id"]])})
        if len(subjects) < 2:
            raise ToolError("incompatible_facts", "cross_subject requires explicitly selected inputs from distinct subjects")
        limitations.append("This is an explicit cross-subject comparison, not temporal growth. "
                           "Selected periods are preserved; economic and fiscal-period comparability remain unverified.")
    elif comparison_mode == "presentation_difference":
        if operation != "difference":
            raise ToolError("incompatible_facts", "presentation_difference supports difference only")
        research._require_same(contexts, ("metric", "period_start", "period_end", "period_kind"))
        if contexts[0]["basis"] == contexts[1]["basis"]:
            raise ToolError("incompatible_facts", "presentation_difference requires different bases")
        limitations.append("This is a presentation difference, NOT growth; it does not establish a reconciliation cause.")
    elif operation == "percentage_point_difference":
        limitations.append("This is an absolute percentage-point difference, not relative percentage growth. "
                           "All selected input periods are preserved; period and economic comparability remain unverified.")
    elif operation in {"sum", "ratio", "percent_ratio"} and not same_period:
        raise ToolError("incompatible_facts", "inputs have different period_start or period_end")
    elif operation == "percent_change" or not same_period:
        research._require_same(contexts, ("metric",))
        alignment = research._require_corresponding_periods(*contexts)
        if alignment == "week_based_fiscal_year":
            limitations.append(
                "Compared actual 52/53-week fiscal-year date ranges without calendar normalization. "
                "Different week counts can affect comparability; this is reported-total growth, not a per-week adjustment.")
    if operation in {"ratio", "percent_change", "percent_ratio"} and not values[1]:
        raise ToolError("division_by_zero", "denominator input value is zero")
    if operation == "sum":
        exact = sum(values, Fraction(0))
        formula = " + ".join(f"operand[{index}]" for index in range(len(values)))
        limitations.append("This is a calculated sum, not an issuer-reported total.")
    elif operation in {"difference", "percentage_point_difference"}:
        exact, formula = values[0] - values[1], "operand[0] - operand[1]"
    elif operation == "ratio":
        exact, formula = values[0] / values[1], "operand[0] / operand[1]"
    elif operation == "percent_ratio":
        exact, formula = values[0] / values[1] * 100, "operand[0] / operand[1] * 100"
        limitations.append("This is a percentage ratio of same-period inputs, not a percentage change over time.")
    else:
        exact, formula = (values[0] - values[1]) / abs(values[1]) * 100, "(operand[0] - operand[1]) / abs(operand[1]) * 100"
        limitations.append("Percent change uses the absolute value of the baseline.")
    if max(abs(exact.numerator).bit_length(), exact.denominator.bit_length()) > 12000:
        raise ToolError("calculation_size_limit", "Exact rational exceeds the bounded calculation size")
    source_facts, dependencies = {}, {}
    for _, _, facts, steps, _ in operands:
        for fact in facts:
            if fact["fact_id"] in source_facts and source_facts[fact["fact_id"]] != fact:
                raise ToolError("input_conflict", "Dependency uses conflicting source versions")
            source_facts[fact["fact_id"]] = fact
        for step in steps:
            if step["calculation_id"] in dependencies and dependencies[step["calculation_id"]] != step:
                raise ToolError("input_conflict", "Dependency has conflicting definitions")
            dependencies[step["calculation_id"]] = step
    if len(dependencies) >= MAX_STEPS or len(source_facts) > MAX_SOURCE_FACTS:
        raise ToolError("calculation_size_limit", "Calculation dependency graph exceeds its bounded size")
    value = _display(exact, operation, places)
    unit = ("percentage points" if operation == "percentage_point_difference" else
            "x" if operation == "ratio" else "%" if operation in {"percent_change", "percent_ratio"} else first["unit"])
    output_context = {**first, "unit": unit}
    if comparison_mode == "cross_subject":
        output_context.update(company_id=None, company_ids=subjects)
    if operation in {"ratio", "percent_change", "percent_ratio", "percentage_point_difference"} or any(item["metric"] != first["metric"] for item in contexts[1:]):
        output_context["metric"] = None
    if comparison_mode == "presentation_difference":
        output_context["basis"] = "presentation_difference:" + " | ".join(item["basis"] for item in contexts)
    if not same_period:
        output_context.update(period_start=None, period_end=None,
                              periods=list(dict.fromkeys(tuple(period) for item in contexts for period in item["periods"])))
        output_context["periods"] = [list(period) for period in output_context["periods"]]
    parameters = {
        "execution_version": VERSION, "input_refs": identifiers, "decimal_places": places,
        "dependencies": list(dependencies.values()),
    }
    record = research._calculation_record(
        operation=operation, facts=tuple(source_facts.values()), comparison_mode=comparison_mode,
        formula=formula, value=Decimal(value), unit=unit, rule=VERSION, parameters=parameters,
        limitations=limitations,
    )
    record["arithmetic"] = {
        "exact_value": _fraction_record(exact), "output_context": output_context,
        "operands": [item[4] for item in operands],
        "rounding": {"mode": "half_away_from_zero", "decimal_places": places,
                     "applied": Fraction(Decimal(value)) != exact,
                     "policy": ("percentage-point display uses decimal_places; exact fraction retained" if operation == "percentage_point_difference" else
                                "percentage ratio display uses decimal_places; exact fraction retained" if operation == "percent_ratio" else
                                "finite results are exact except percent_change; recurring results use decimal_places")},
        "steps": [
            {"calculation_id": identifier, "formula": research._calculations[identifier]["formula"],
             "operands": deepcopy(research._calculations[identifier]["arithmetic"]["operands"]),
             "value": research._calculations[identifier]["value"], "unit": research._calculations[identifier]["unit"],
             "exact_value": deepcopy(research._calculations[identifier]["arithmetic"]["exact_value"])}
            for identifier in dependencies
        ],
    }
    if len(json.dumps(record, ensure_ascii=False, allow_nan=False).encode()) > MAX_RECORD_BYTES:
        raise ToolError("calculation_size_limit", "Calculation provenance exceeds the recording limit")
    research._calculations[record["calculation_id"]] = deepcopy(record)
    return record


def replay(research, record):
    parameters = record.get("parameters")
    if (not isinstance(parameters, dict) or parameters.get("execution_version") != VERSION
            or set(parameters) != {"execution_version", "input_refs", "decimal_places", "dependencies"}
            or not isinstance(parameters["dependencies"], list) or len(parameters["dependencies"]) >= MAX_STEPS):
        raise ToolError("invalid_calculation", "Recorded arithmetic contract is invalid")
    # Replay must not inherit ambient computed values or mutate the caller's cache.
    from copy import copy
    scratch = copy(research)
    scratch._calculations = {}
    for step in parameters["dependencies"]:
        if not isinstance(step, dict) or set(step) != {
                "calculation_id", "operation", "fact_ids", "comparison_mode", "decimal_places"}:
            raise ToolError("invalid_calculation", "Recorded dependency is invalid")
        computed = calculate(scratch, **{key: step[key] for key in step if key != "calculation_id"})
        if computed["calculation_id"] != step["calculation_id"]:
            raise ToolError("invalid_calculation", "Recorded dependency differs from deterministic replay")
    return calculate(scratch, operation=record.get("operation"), fact_ids=parameters["input_refs"],
                     comparison_mode=record.get("comparison_mode"), decimal_places=parameters["decimal_places"])
