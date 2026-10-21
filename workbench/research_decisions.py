"""Persist research decisions separately from report text and raw data."""
from __future__ import annotations

from contextlib import closing
from copy import copy
import json
import uuid

from .chat import ChatError, _json, _now
from .research_tools import ToolError
from .research_inputs import InputError
from . import forecast, regression


_MODELS = {"regression": regression, "forecast": forecast}


_SCHEMA = """
CREATE TABLE IF NOT EXISTS research_decisions (
 decision_id TEXT PRIMARY KEY,
 run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
 question TEXT NOT NULL,
 kind TEXT NOT NULL,
 explanation TEXT NOT NULL,
 options_json TEXT NOT NULL,
 multiple INTEGER NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_decision_answers (
 decision_id TEXT PRIMARY KEY REFERENCES research_decisions(decision_id),
 request_id TEXT NOT NULL UNIQUE,
 selected_json TEXT NOT NULL,
 notes TEXT NOT NULL,
 action TEXT NOT NULL,
 continuation_run_id TEXT REFERENCES runs(run_id),
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_decision_groups (
 decision_id TEXT PRIMARY KEY REFERENCES research_decisions(decision_id),
 groups_json TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS research_decision_groups_no_update
BEFORE UPDATE ON research_decision_groups BEGIN SELECT RAISE(ABORT,'decision groups are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_decision_groups_no_delete
BEFORE DELETE ON research_decision_groups BEGIN SELECT RAISE(ABORT,'decision groups are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_decisions_no_update
BEFORE UPDATE ON research_decisions BEGIN SELECT RAISE(ABORT,'decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_decisions_no_delete
BEFORE DELETE ON research_decisions BEGIN SELECT RAISE(ABORT,'decisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_decision_answers_no_update
BEFORE UPDATE ON research_decision_answers BEGIN SELECT RAISE(ABORT,'decision answers are immutable'); END;
CREATE TRIGGER IF NOT EXISTS research_decision_answers_no_delete
BEFORE DELETE ON research_decision_answers BEGIN SELECT RAISE(ABORT,'decision answers are immutable'); END;
"""


def _text(value, name, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ToolError("invalid_confirmation", f"{name} must contain 1..{maximum} characters")
    return value.strip()


def _validate_groups(groups, options, multiple):
    if groups is None:
        return []
    if not isinstance(groups, list) or len(groups) > 8:
        raise ToolError("invalid_confirmation", "groups must contain at most eight choice groups")
    checked, assigned = [], []
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"id", "label", "option_ids", "minimum", "maximum"}:
            raise ToolError("invalid_confirmation", "Each group needs id, label, option_ids, minimum and maximum")
        ids, minimum, maximum = group["option_ids"], group["minimum"], group["maximum"]
        if (not isinstance(ids, list) or not ids or any(not isinstance(item, str) for item in ids)
                or len(set(ids)) != len(ids) or type(minimum) is not int or type(maximum) is not int
                or not 1 <= minimum <= maximum <= len(ids)):
            raise ToolError("invalid_confirmation", "Group limits must select 1..maximum distinct options")
        checked.append({
            "id": _text(group["id"], "group id", 80),
            "label": _text(group["label"], "group label", 200),
            "option_ids": ids, "minimum": minimum, "maximum": maximum,
        })
        assigned.extend(ids)
    if checked and (len({item["id"] for item in checked}) != len(checked)
                    or len(set(assigned)) != len(assigned)
                    or set(assigned) != {item["id"] for item in options}
                    or (not multiple and sum(item["maximum"] for item in checked) > 1)):
        raise ToolError("invalid_confirmation", "Groups must uniquely partition all options and respect multiple")
    return checked


def validate_request(kind, question, explanation, options, multiple, groups=None):
    if kind not in {"basis", "model", "coverage", "time", "other"}:
        raise ToolError("invalid_confirmation", "Unknown research decision kind")
    if type(multiple) is not bool or not isinstance(options, list) or not 1 <= len(options) <= 8:
        raise ToolError("invalid_confirmation", "Provide 1..8 explained options and a boolean multiple")
    checked = []
    for option in options:
        required = {"id", "label", "benefit", "limitation"}
        if not isinstance(option, dict) or set(option) not in (required, required | {"regression"}, required | {"forecast"}):
            raise ToolError("invalid_confirmation", "Each option needs id, label, benefit and limitation")
        item = {key: _text(option[key], key, 80 if key == "id" else 2000) for key in required}
        for model, module in _MODELS.items():
            if model in option:
                if kind != "model":
                    raise ToolError("invalid_confirmation", "A structured calculation plan is a model decision")
                item[model] = module.validate_plan(option[model])
        checked.append(item)
    if len({item["id"] for item in checked}) != len(checked):
        raise ToolError("invalid_confirmation", "Option ids must be unique")
    return {
        "kind": kind, "question": _text(question, "question", 1000),
        "explanation": _text(explanation, "explanation", 4000),
        "options": checked, "multiple": multiple,
        "groups": _validate_groups(groups, checked, multiple),
    }


class ResearchDecisions:
    def __init__(self, chat):
        self.chat = chat
        with closing(chat._connect()) as connection:
            connection.executescript(_SCHEMA)

    def for_run(self, run_id):
        with closing(self.chat._connect()) as connection:
            row = connection.execute(
                "SELECT d.*,a.selected_json,a.notes,a.action,a.continuation_run_id,g.groups_json "
                "FROM research_decisions d LEFT JOIN research_decision_answers a USING(decision_id) "
                "LEFT JOIN research_decision_groups g USING(decision_id) "
                "WHERE d.run_id=?", (run_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "decision_id": row["decision_id"], "run_id": row["run_id"],
            "kind": row["kind"], "question": row["question"], "explanation": row["explanation"],
            "options": json.loads(row["options_json"]), "multiple": bool(row["multiple"]),
            "groups": json.loads(row["groups_json"] or "[]"),
            "created_at": row["created_at"],
            "status": row["action"] or "pending",
            "selected_ids": json.loads(row["selected_json"]) if row["selected_json"] else [],
            "notes": row["notes"], "continuation_run_id": row["continuation_run_id"],
        }

    def request(self, run_id, *, kind, question, explanation, options, multiple=False, groups=None, research=None):
        request = validate_request(kind, question, explanation, options, multiple, groups)
        for option in request["options"]:
            for model, module in _MODELS.items():
                if model in option:
                    if research is None:
                        raise ToolError("invalid_confirmation", "Model proposals require the active frozen data service")
                    kwargs = {"as_of_date": _now()[:10]} if model == "forecast" else {}
                    inputs = (research.for_facts(module.input_fact_ids(option[model]))
                              if hasattr(research, "for_facts") else research)
                    option[f"{model}_details"] = module.describe_plan(inputs, option[model], **kwargs)
        decision_id = f"decision_{uuid.uuid4().hex}"
        with closing(self.chat._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["status"] not in {"queued", "running"}:
                raise ToolError("inactive_research", "Research is no longer active")
            connection.execute(
                "INSERT INTO research_decisions VALUES(?,?,?,?,?,?,?,?)",
                (decision_id, run_id, request["question"], kind, request["explanation"],
                 _json(request["options"]), int(multiple), _now()),
            )
            if request["groups"]:
                connection.execute("INSERT INTO research_decision_groups VALUES(?,?)",
                                   (decision_id, _json(request["groups"])))
            error = {"code": "confirmation_required", "message": request["question"]}
            connection.execute(
                "UPDATE runs SET status='blocked',error_json=?,finished_at=? WHERE run_id=?",
                (_json(error), _now(), run_id),
            )
            self.chat._insert_event(
                connection, run_id, "confirmation_required",
                {"decision_id": decision_id, **request, "authoritative": True}, claim=None)
            connection.commit()
        return self.for_run(run_id)

    def definition(self, run_id, cancel_event, research=None):
        def request_confirmation(**arguments):
            result = self.request(run_id, research=research, **arguments)
            cancel_event.set()
            return {"status": "awaiting_user", "decision_id": result["decision_id"],
                    "instruction": "Research is paused. Do not calculate or produce a report until the user responds."}

        return {
            "name": "request_confirmation",
            "description": (
                "Pause for a consequential research decision. Explain meaning and tradeoffs, allow multiple "
                "bases when appropriate. Mandatory before selecting a forecast model/assumptions or reducing "
                "the requested scope because data is missing. This ends the current run without a report."
                " For independent categories (basis, model, scenario), supply groups that partition all options "
                "and set multiple=true. Each group declares minimum/maximum selections; allow parallel bases "
                "where meaningful. Without groups, each option must be a coherent complete alternative, "
                "not one component of a plan. Model selection alone never confirms numeric assumptions."
                " For executable OLS, use kind=model and include each option's regression object with exact "
                "chronologically ordered frozen X/Y fact IDs, level or prior-year percent_change Y, and whole-year "
                "lag. The UI shows the actual sample and transform before approval. Explain any coverage reduction. "
                "No regression is computed until acceptance. Use calculate_regression with the accepted decision "
                "and option IDs; free-text model choices do not authorize it."
                " For annual cash-flow scenarios, attach forecast: actual annual operating_cash_flow and "
                "capital_expenditures fact IDs, explicit native CapEx sign, and every consecutive annual period "
                "with separate decimal-string CFO/CapEx growth percentages, including bridge years. "
                "The UI shows actual baselines and all assumptions; calculate_forecast executes only the accepted option."
            ),
            "parameters": {
                "type": "object", "additionalProperties": False,
                "required": ["kind", "question", "explanation", "options", "multiple"],
                "properties": {
                    "kind": {"type": "string", "enum": ["basis", "model", "coverage", "time", "other"]},
                    "question": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "explanation": {"type": "string", "minLength": 1, "maxLength": 4000},
                    "multiple": {"type": "boolean"},
                    "groups": {
                        "type": "array", "maxItems": 8,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ["id", "label", "option_ids", "minimum", "maximum"],
                            "properties": {
                                "id": {"type": "string", "minLength": 1, "maxLength": 80},
                                "label": {"type": "string", "minLength": 1, "maxLength": 200},
                                "option_ids": {"type": "array", "minItems": 1, "maxItems": 8,
                                               "uniqueItems": True, "items": {"type": "string"}},
                                "minimum": {"type": "integer", "minimum": 1, "maximum": 8},
                                "maximum": {"type": "integer", "minimum": 1, "maximum": 8},
                            },
                        },
                    },
                    "options": {
                        "type": "array", "minItems": 1, "maxItems": 8,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "required": ["id", "label", "benefit", "limitation"],
                            "properties": {**{key: {"type": "string", "minLength": 1,
                                                  "maxLength": 80 if key == "id" else 2000}
                                           for key in ("id", "label", "benefit", "limitation")},
                                           "regression": regression.PLAN_SCHEMA,
                                           "forecast": forecast.PLAN_SCHEMA},
                        },
                    },
                },
            },
            "handler": request_confirmation,
        }

    def regression_authorization(self, run_id, decision_id, option_id):
        return self.model_authorization("regression", run_id, decision_id, option_id)

    def model_authorization(self, model, run_id, decision_id, option_id):
        """Resolve only an accepted choice on this run's actual continuation chain."""
        decision_id = _text(decision_id, "decision_id", 200)
        option_id = _text(option_id, "option_id", 200)
        visited = set()
        with closing(self.chat._connect()) as connection:
            while run_id and run_id not in visited:
                visited.add(run_id)
                row = connection.execute(
                    "SELECT d.*,a.selected_json,a.notes FROM research_decision_answers a "
                    "JOIN research_decisions d USING(decision_id) "
                    "WHERE a.continuation_run_id=? AND a.action='accepted'", (run_id,),
                ).fetchone()
                if row is None:
                    break
                selected = json.loads(row["selected_json"])
                if row["decision_id"] == decision_id:
                    option = next((item for item in json.loads(row["options_json"])
                                   if item["id"] == option_id and option_id in selected), None)
                    if option is None or model not in option:
                        break
                    if row["notes"].strip():
                        raise ToolError(f"{model}_reconfirmation_required",
                                        "The user added requirements. Incorporate them in a revised structured plan and obtain confirmation before computing.")
                    return option, {"decision_id": decision_id, "option_id": option_id,
                                    "source_run_id": row["run_id"]}
                if selected == ["refresh"] and connection.execute(
                        "SELECT 1 FROM events WHERE run_id=? AND type='time_reference'",
                        (row["run_id"],)).fetchone():
                    break
                run_id = row["run_id"]
        raise ToolError(f"{model}_confirmation_required",
                        "This run has no accepted structured model choice with these IDs; do not infer authorization from labels or another conversation.")

    def replay_model(self, model, run_id, research, parameters):
        if not isinstance(parameters, dict) or not isinstance(parameters.get("authorization"), dict):
            raise ToolError(f"{model}_confirmation_required", "The recorded model authorization is missing")
        authorization = parameters["authorization"]
        option, approved = self.model_authorization(
            model, run_id, authorization.get("decision_id"), authorization.get("option_id"))
        if approved != authorization or option[model] != parameters.get("plan"):
            raise ToolError(f"{model}_sample_changed", "The recorded plan differs from the accepted option")
        kwargs = {"as_of_date": option["forecast_details"]["as_of_date"]} if model == "forecast" else {}
        if model == "forecast" and parameters.get("as_of_date") != kwargs["as_of_date"]:
            raise ToolError("forecast_sample_changed", "The scenario reference date differs from the confirmed proposal")
        if hasattr(research, "for_facts"):
            research = research.for_facts(_MODELS[model].input_fact_ids(option[model]))
        if _MODELS[model].describe_plan(research, option[model], **kwargs) != option[f"{model}_details"]:
            raise ToolError(f"{model}_sample_changed", "The actual inputs differ from the confirmed proposal")
        return _MODELS[model].calculate(research, option[model], approved, **kwargs)

    def regression_definition(self, run_id, research):
        return self.model_definition("regression", run_id, research)

    def model_definition(self, model, run_id, research):
        module = _MODELS[model]
        restored_cache = {}
        def calculate_model(decision_id, option_id):
            option, authorization = self.model_authorization(model, run_id, decision_id, option_id)
            plan = option[model]
            restored = []
            identifiers = module.input_fact_ids(plan)
            missing = [identifier for identifier in identifiers if identifier not in research._facts]
            answer = {"run_id": run_id, "company_id": research.company_id,
                      "source_snapshot": self.chat.effective_snapshot(run_id, research.company_id)}
            if missing:
                source_run_id = authorization["source_run_id"]
                registered = self.chat.inputs.has_contract(source_run_id)
                events = [] if registered else self.chat.get_run(source_run_id)["events"]
                for identifier in missing:
                    try:
                        original = (self.chat.inputs.read(source_run_id, identifier, consumer_run_id=run_id)
                                    if registered else self.chat._document_fact_derivation(events, fact_id=identifier))
                    except InputError as exc:
                        raise ToolError(exc.code, exc.message) from exc
                    if original is None:
                        raise ToolError(f"{model}_fact_unavailable", "The confirmed input has no unique recorded document-cell origin")
                    result = {key: value for key, value in original.items() if not key.startswith("_")}
                    result["reused_from"] = original.get("reused_from") or {
                        "run_id": authorization["source_run_id"], "fact_id": identifier}
                    restored.append(result)
            proofs_by_id = {**restored_cache, **{item["fact"]["fact_id"]: item for item in restored}}
            proofs = [proofs_by_id[identifier] for identifier in identifiers if identifier in proofs_by_id]
            for proof in proofs:
                try:
                    self.chat._validated_document_fact(
                        answer, proof, expected_fact=research._facts.get(proof["fact"]["fact_id"]),
                        source_run_id=authorization["source_run_id"])
                except ChatError as exc:
                    raise ToolError(exc.code, exc.message) from exc
            inputs = copy(research)
            inputs._facts = {**research._facts, **{item["fact"]["fact_id"]: item["fact"] for item in restored}}
            parameters = {"plan": plan, "authorization": authorization}
            if model == "forecast":
                parameters["as_of_date"] = option["forecast_details"]["as_of_date"]
            calculated = self.replay_model(model, run_id, inputs, parameters)
            restored_cache.update({item["fact"]["fact_id"]: item for item in restored})
            research._facts.update({item["fact"]["fact_id"]: item["fact"] for item in restored})
            if proofs:
                calculated["restored_document_facts"] = proofs
            return calculated
        return {
            "name": f"calculate_{model}",
            "description": (
                f"Execute the exact accepted structured {model} option on this continuation chain. "
                "One call returns individually citable outputs, full assumptions, formulas and frozen source facts. "
                "Regression returns slope/intercept/R-squared; forecast returns each period's CFO/CapEx/FCF "
                "and recursive dependencies, with bridge periods explicitly classified. "
                "No floats, no arbitrary code, no silent input substitution. "
                "Confirmed document-cell inputs are restored from their recorded L2 derivations only after "
                "checking the current frozen snapshot and original bytes; there is no need to repeat extraction. "
                "Model or sample changes and user-added requirements require a new confirmed plan."
            ),
            "parameters": {
                "type": "object", "additionalProperties": False,
                "required": ["decision_id", "option_id"],
                "properties": {key: {"type": "string", "minLength": 1, "maxLength": 200}
                               for key in ("decision_id", "option_id")},
            },
            "handler": calculate_model,
        }

    def answer(self, decision_id, selected_ids, notes, action, request_id):
        decision_id = self.chat._valid_id(decision_id, "decision_id")
        request_id = self.chat._valid_id(request_id, "request_id")
        if (action not in {"accepted", "declined"} or not isinstance(selected_ids, list)
                or any(not isinstance(item, str) for item in selected_ids)
                or len(selected_ids) != len(set(selected_ids))
                or not isinstance(notes, str) or len(notes) > 4000):
            raise ChatError("invalid_decision", "Invalid confirmation content.")
        with closing(self.chat._connect()) as connection:
            row = connection.execute(
                "SELECT d.*,r.conversation_id,g.groups_json FROM research_decisions d "
                "JOIN runs r USING(run_id) LEFT JOIN research_decision_groups g USING(decision_id) "
                "WHERE decision_id=?", (decision_id,),
            ).fetchone()
            if row is None:
                raise ChatError("decision_not_found", "Pending confirmation not found.", 404)
            existing = connection.execute(
                "SELECT * FROM research_decision_answers WHERE decision_id=? OR request_id=?",
                (decision_id, request_id),
            ).fetchone()
        chosen = _json(sorted(selected_ids))

        def replay(previous):
            if (previous["decision_id"] != decision_id or previous["request_id"] != request_id
                    or previous["selected_json"] != chosen or previous["notes"] != notes
                    or previous["action"] != action):
                raise ChatError("decision_conflict", "This item has already been confirmed, or the request identifier is bound to another operation. Refresh and try again.", 409)
            return {"run_id": previous["continuation_run_id"],
                    "conversation_id": row["conversation_id"], "status": action}

        if existing is not None:
            return replay(existing)
        options = json.loads(row["options_json"])
        groups = json.loads(row["groups_json"] or "[]")
        if (not set(selected_ids) <= {item["id"] for item in options}
                or (action == "accepted" and (not selected_ids or (not row["multiple"] and len(selected_ids) != 1)))
                or (action == "declined" and selected_ids)):
            raise ChatError("invalid_decision", "Select a valid option; a single-choice item cannot accept multiple selections.")
        if action == "accepted":
            for group in groups:
                count = len(set(selected_ids) & set(group["option_ids"]))
                if not group["minimum"] <= count <= group["maximum"]:
                    raise ChatError("invalid_decision",
                                    f"“{group['label']}” requires {group['minimum']} to {group['maximum']} selections.")

        def guard(connection):
            previous = connection.execute(
                "SELECT * FROM research_decision_answers WHERE decision_id=? OR request_id=?",
                (decision_id, request_id),
            ).fetchone()
            return replay(previous) if previous else None

        def record(connection, run=None):
            connection.execute("INSERT INTO research_decision_answers VALUES(?,?,?,?,?,?,?)",
                               (decision_id, request_id, chosen, notes, action,
                                run["run_id"] if run else None, _now()))
            if run:
                self.chat._insert_event(connection, run["run_id"], "confirmed_decision", {
                    "authoritative": True, "decision_id": decision_id,
                    "kind": row["kind"], "source_run_id": row["run_id"],
                    "selected_ids": sorted(selected_ids),
                    "context": context,
                }, claim=None)

        if action == "declined":
            with closing(self.chat._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                result = guard(connection)
                if result is not None:
                    return result
                record(connection)
                connection.commit()
            return {"run_id": None, "conversation_id": row["conversation_id"], "status": "declined"}
        context = {
            "decision": row["question"], "explanation": row["explanation"], "kind": row["kind"],
            "selected": [item for item in options if item["id"] in selected_ids], "user_notes": notes,
            "groups": groups,
        }
        message = "Continue research. Confirmed: " + "; ".join(item["label"] for item in context["selected"]) + "."
        if notes.strip():
            message += "\nAdditional requirements: " + notes.strip()
        result, _started = self.chat._persist_submission(
            row["conversation_id"], None, message, transaction_guard=guard,
            transaction_precondition=self.chat._require_provider, transaction_hook=record,
            confirmed_decision_id=decision_id)
        return result
