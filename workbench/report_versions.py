"""Append-only correction links between completed reports in one conversation."""
from __future__ import annotations

from contextlib import closing
import json
import uuid

from .chat import _json, _now
from .research_tools import ToolError


class ReportVersions:
    def __init__(self, chat):
        self.chat = chat
        with closing(chat._connect()) as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS report_corrections (
                    correction_id TEXT PRIMARY KEY,
                    old_message_id TEXT NOT NULL REFERENCES messages(message_id),
                    new_run_id TEXT NOT NULL REFERENCES runs(run_id),
                    start INTEGER NOT NULL,
                    end INTEGER NOT NULL,
                    original_text TEXT NOT NULL,
                    explanation TEXT NOT NULL,
                    impact TEXT NOT NULL,
                    evidence_refs_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(old_message_id,new_run_id,start,end)
                );
                CREATE TRIGGER IF NOT EXISTS report_corrections_no_update
                BEFORE UPDATE ON report_corrections
                BEGIN SELECT RAISE(ABORT,'report corrections are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS report_corrections_no_delete
                BEFORE DELETE ON report_corrections
                BEGIN SELECT RAISE(ABORT,'report corrections are immutable'); END;
            """)

    @staticmethod
    def _reports(connection, conversation_id):
        return connection.execute(
            "SELECT message_id,content,run_id,created_at FROM messages "
            "WHERE conversation_id=? AND role='assistant' AND status='completed' ORDER BY created_at,rowid",
            (conversation_id,),
        ).fetchall()

    def read(self, conversation_id, version):
        if type(version) is not int or version < 1:
            raise ToolError("invalid_report_version", "Report version must be a positive integer")
        with closing(self.chat._connect()) as connection:
            reports = self._reports(connection, conversation_id)
        if version > len(reports):
            raise ToolError("report_version_not_found", "Report version does not exist in this conversation")
        return {"version": version, **dict(reports[version - 1]),
                "limitation": "Prior report text is not source evidence. Re-read actual sources before reusing claims."}

    def record(self, run_id, refs, *, version, original_text, explanation, impact, evidence_refs, claim=None):
        for name, value in (("original_text", original_text), ("explanation", explanation), ("impact", impact)):
            if not isinstance(value, str) or not value.strip() or len(value) > 4000:
                raise ToolError("invalid_correction", f"{name} must contain 1..4000 characters")
        if not isinstance(evidence_refs, list) or not 1 <= len(evidence_refs) <= 8:
            raise ToolError("invalid_correction", "Provide 1..8 sources actually obtained in this run")
        checked = []
        for reference in evidence_refs:
            if (not isinstance(reference, dict) or set(reference) != {"kind", "id"}
                    or not isinstance(reference.get("kind"), str)
                    or not isinstance(reference.get("id"), str)
                    or reference["id"] not in refs.get(reference["kind"], {})):
                raise ToolError("invalid_correction_reference", "Correction references must belong to the current run")
            if reference not in checked:
                checked.append(reference)
        checked.sort(key=lambda item: (item["kind"], item["id"]))
        with closing(self.chat._connect()) as connection:
            run = connection.execute("SELECT conversation_id,status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None or run["status"] not in {"queued", "running"}:
            raise ToolError("inactive_research", "Corrections require an active research run")
        original = self.read(run["conversation_id"], version)
        start = original["content"].find(original_text)
        if start < 0 or original["content"].find(original_text, start + 1) >= 0:
            raise ToolError("ambiguous_correction_span", "Quote one unique exact span of the original report")
        end = start + len(original_text)
        with closing(self.chat._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if claim is not None:
                self.chat.jobs.assert_claim(connection, claim)
            state = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if state is None or state["status"] not in {"queued", "running"}:
                raise ToolError("inactive_research", "Research stopped before the correction could be recorded")
            prior = connection.execute(
                "SELECT * FROM report_corrections WHERE old_message_id=? AND new_run_id=? AND start=? AND end=?",
                (original["message_id"], run_id, start, end),
            ).fetchone()
            if prior:
                if (prior["explanation"], prior["impact"], prior["evidence_refs_json"]) != (
                        explanation, impact, _json(checked)):
                    raise ToolError("correction_conflict", "A recorded correction cannot be overwritten")
                return {"correction_id": prior["correction_id"], "status": "recorded"}
            correction_id = f"correction_{uuid.uuid4().hex}"
            connection.execute(
                "INSERT INTO report_corrections VALUES(?,?,?,?,?,?,?,?,?,?)",
                (correction_id, original["message_id"], run_id, start, end, original_text,
                 explanation, impact, _json(checked), _now()),
            )
            connection.commit()
        return {"correction_id": correction_id, "status": "recorded",
                "publication": "Visible only when this run publishes a completed report; not independent verification."}

    def project(self, connection, conversation_id, reports):
        versions = [{"version": number, "message_id": item["message_id"], "run_id": item.get("run_id"),
                     "created_at": item["created_at"], "corrected_by": [], "corrections": []}
                    for number, item in enumerate(reports, 1)]
        by_message = {item["message_id"]: item for item in versions}
        corrections = connection.execute(
            "SELECT c.*,m.message_id AS new_message_id FROM report_corrections c "
            "JOIN messages old ON old.message_id=c.old_message_id "
            "JOIN messages m ON m.run_id=c.new_run_id AND m.role='assistant' AND m.status='completed' "
            "JOIN runs r ON r.run_id=c.new_run_id AND r.status='completed' "
            "WHERE old.conversation_id=? AND m.conversation_id=? ORDER BY c.created_at,c.rowid",
            (conversation_id, conversation_id),
        ).fetchall()
        for row in corrections:
            old, new = by_message[row["old_message_id"]], by_message[row["new_message_id"]]
            correction = {key: row[key] for key in (
                "correction_id", "old_message_id", "new_message_id", "start", "end",
                "original_text", "explanation", "impact", "created_at")}
            correction.update(old_version=old["version"], new_version=new["version"],
                              evidence_refs=json.loads(row["evidence_refs_json"]),
                              semantic_verified=False)
            old["corrected_by"].append(correction)
            new["corrections"].append(correction)
        return versions

    def definitions(self, run_id, conversation_id, refs, *, claim=None):
        with closing(self.chat._connect()) as connection:
            count = len(self._reports(connection, conversation_id))
        if not count:
            return []
        return [{
            "name": "read_report_version",
            "description": f"Read an original report version (1..{count}) in this research. It is history, not source evidence.",
            "parameters": {"type": "object", "additionalProperties": False, "required": ["version"],
                           "properties": {"version": {"type": "integer", "minimum": 1, "maximum": count}}},
            "handler": lambda version: self.read(conversation_id, version),
        }, {
            "name": "record_report_correction",
            "description": (
                "Record a correction to a unique exact span of a prior report after obtaining current-run evidence. "
                "Explain the error and its impact, then include the correction in the new integrated report. "
                "Never use this just because newer data changed; original reports remain immutable. "
                "Use readable report version numbers in the report, never internal correction or message IDs."
            ),
            "parameters": {
                "type": "object", "additionalProperties": False,
                "required": ["version", "original_text", "explanation", "impact", "evidence_refs"],
                "properties": {
                    "version": {"type": "integer", "minimum": 1, "maximum": count},
                    **{name: {"type": "string", "minLength": 1, "maxLength": 4000}
                       for name in ("original_text", "explanation", "impact")},
                    "evidence_refs": {"type": "array", "minItems": 1, "maxItems": 8, "items": {
                        "type": "object", "additionalProperties": False, "required": ["kind", "id"],
                        "properties": {"kind": {"type": "string", "enum": ["claim", "evidence", "passage", "calculation", "query"]},
                                       "id": {"type": "string", "minLength": 1}},
                    }},
                },
            },
            "handler": lambda **arguments: self.record(run_id, refs, claim=claim, **arguments),
        }]
