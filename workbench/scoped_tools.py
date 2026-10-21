"""Explicit subject dispatch over frozen per-subject data, without changing source identities."""
from copy import copy, deepcopy

from .execution_scope import describe, normalize
from .research_tools import ResearchTools, ToolError


_RECORD_KEYS = {
    "sources": "source_id", "evidence": "evidence_id", "claims": "claim_id",
    "facts": "fact_id", "evidence_chains": "claim_id", "activities": "activity_id",
}


def snapshot_for(research):
    result = {
        "data_version": deepcopy(research.data_version),
        "evidence": {"company_id": research.company_id, **{
            key: deepcopy(research._company[key]) for key in _RECORD_KEYS}},
        "documents": {"coverage": deepcopy(research.document_coverage),
                      "document_version_ids": list(research.document_version_ids)},
        "authoritative": True,
    }
    if isinstance(research, ScopedResearchTools):
        result["execution_scope"] = describe(research.company_ids)
        result["subjects"] = {identifier: snapshot_for(child) for identifier, child in research.subjects.items()}
    return result


def _merge(records, key):
    merged = {}
    for record in records:
        identifier = record[key]
        if identifier in merged and merged[identifier] != record:
            raise ToolError("scope_record_conflict", "Selected subject snapshots disagree about a shared record")
        merged[identifier] = deepcopy(record)
    return [merged[identifier] for identifier in sorted(merged)]


def tools_for_scope(evidence_db, company_id, company_ids, document_service=None, *, frozen_snapshot=None,
                    evidence_reader=None):
    ids = normalize(company_ids)
    if company_id not in ids:
        raise ToolError("invalid_execution_scope", "Legacy anchor is outside the explicit research scope")
    if (frozen_snapshot is not None and frozen_snapshot.get("execution_scope") is not None
            and frozen_snapshot["execution_scope"] != describe(ids)):
        raise ToolError("invalid_frozen_snapshot", "Frozen scope differs from authorized membership")
    if len(ids) == 1:
        return ResearchTools(
            evidence_db, company_id, document_service, frozen_snapshot=frozen_snapshot,
            evidence_reader=evidence_reader)
    return ScopedResearchTools(
        evidence_db, company_id, ids, document_service, frozen_snapshot=frozen_snapshot,
        evidence_reader=evidence_reader)


class ScopedResearchTools(ResearchTools):
    """The inherited company_id is a legacy record anchor, never a default data selection."""

    def __init__(self, evidence_db, company_id, company_ids, document_service=None, *,
                 frozen_snapshot=None, evidence_reader=None):
        self.company_ids = normalize(company_ids)
        if company_id not in self.company_ids:
            raise ToolError("invalid_execution_scope", "Research anchor is outside its subject set")
        if frozen_snapshot is not None:
            if (frozen_snapshot.get("execution_scope") != describe(self.company_ids)
                    or not isinstance(frozen_snapshot.get("subjects"), dict)
                    or set(frozen_snapshot["subjects"]) != set(self.company_ids)):
                raise ToolError("invalid_frozen_snapshot", "Frozen subjects differ from the authorized execution scope")
        self.subjects = {
            identifier: ResearchTools(
                evidence_db, identifier, document_service,
                frozen_snapshot=frozen_snapshot["subjects"][identifier] if frozen_snapshot is not None else None,
                evidence_reader=evidence_reader)
            for identifier in self.company_ids
        }
        anchor = self.subjects[company_id]
        if any(record.get("company_id") != identifier
               for identifier, child in self.subjects.items()
               for key in ("facts", "claims") for record in child._company[key]):
            raise ToolError("invalid_frozen_snapshot", "A subject snapshot contains another subject's records")
        if any(child.data_version != anchor.data_version for child in self.subjects.values()):
            raise ToolError("scope_version_conflict", "Selected data changed between subject reads; no mixed snapshot was created")
        merged = snapshot_for(anchor)
        for key, identity in _RECORD_KEYS.items():
            merged["evidence"][key] = _merge(
                (record for child in self.subjects.values() for record in child._company[key]), identity)
        coverage = {
            "available": any(child.document_coverage.get("available") for child in self.subjects.values()),
            "documents": _merge(
                (record for child in self.subjects.values()
                 for record in child.document_coverage.get("documents", [])), "document_version_id"),
        }
        coverage.update(enrolled=len(coverage["documents"]), parsed=0, failed=0, unsupported=0)
        for record in coverage["documents"]:
            key = {"parsed": "parsed", "parse_failed": "failed"}.get(record.get("parse_status"), "unsupported")
            coverage[key] += 1
        merged["documents"] = {
            "coverage": coverage,
            "document_version_ids": [item["document_version_id"] for item in coverage["documents"]],
        }
        if frozen_snapshot is not None and any(
                frozen_snapshot.get(key) != merged[key] for key in ("data_version", "evidence", "documents")):
            raise ToolError("invalid_frozen_snapshot", "Aggregate snapshot differs from its frozen subject records")
        super().__init__(evidence_db, company_id, document_service, frozen_snapshot=merged,
                         evidence_reader=evidence_reader)

    def enable_document_derivation(self, snapshot_addition_callback) -> bool:
        """Enable derivation on every explicitly dispatched child and the aggregate."""
        if not all(child.enable_document_derivation(snapshot_addition_callback)
                   for child in self.subjects.values()):
            return False
        return super().enable_document_derivation(snapshot_addition_callback)

    def enable_public_acquisition(self, acquisition, snapshot_addition_callback, *, cancelled=None) -> bool:
        for child in self.subjects.values():
            child.enable_public_acquisition(acquisition, snapshot_addition_callback, cancelled=cancelled)
        return super().enable_public_acquisition(acquisition, snapshot_addition_callback, cancelled=cancelled)

    def add_document_for(self, company_id, descriptor) -> None:
        """Add one granted document to its subject and the deterministically merged view."""
        child = self.for_company(company_id)
        version_id = descriptor.get("document_version_id") if isinstance(descriptor, dict) else None
        child_has = version_id in child.document_version_ids
        aggregate_has = version_id in self.document_version_ids
        if child_has and not aggregate_has:
            raise ToolError("invalid_snapshot_addition", "Subject and aggregate document scopes disagree")
        if aggregate_has and next((item for item in self.document_coverage["documents"]
                                   if item["document_version_id"] == version_id), None) != descriptor:
            raise ToolError("invalid_snapshot_addition", "Shared document descriptors disagree")
        if child_has:
            return
        # A private document may be explicitly shared by several authorized subjects.
        # Grant the child independently; the aggregate remains a deduplicated union.
        child.add_document(descriptor)
        super().add_document(descriptor)
        with self._document_lock:
            documents = self.document_coverage["documents"]
            documents.sort(key=lambda item: item["document_version_id"])
            self.document_version_ids = tuple(item["document_version_id"] for item in documents)

    def for_company(self, company_id):
        if not isinstance(company_id, str) or company_id not in self.subjects:
            raise ToolError("subject_out_of_scope", "Tool calls must explicitly select a subject from this research")
        child = self.subjects[company_id]
        child._facts.update({identifier: fact for identifier, fact in self._facts.items()
                             if fact.get("company_id") == company_id})
        return child

    def for_facts(self, identifiers):
        records = [self._facts.get(identifier) for identifier in identifiers]
        if not records or any(record is None for record in records):
            raise ToolError("model_fact_unavailable", "Model inputs must be present in the authorized frozen data")
        subjects = {record.get("company_id") for record in records}
        if len(subjects) != 1 or not subjects.issubset(self.subjects):
            raise ToolError("model_scope_unsupported", "This model requires inputs from one explicit authorized subject")
        identifier = next(iter(subjects))
        child = copy(self.subjects[identifier])
        child._facts = {key: fact for key, fact in self._facts.items() if fact.get("company_id") == identifier}
        child._calculations = {}
        return child

    def definitions(self):
        result = []
        child_definitions = {
            identifier: {item["name"]: item for item in child.definitions()}
            for identifier, child in self.subjects.items()
        }

        def handler_for(tool_name):
            def dispatch(**arguments):
                identifier = arguments.pop("company_id", None)
                child = self.for_company(identifier)
                value = child_definitions[identifier][tool_name]["handler"](**arguments)
                self._facts.update(child._facts)
                return value
            return dispatch

        for definition in super().definitions():
            name = definition["name"]
            if name == "calculate_facts":
                definition = {**definition, "parameters": deepcopy(definition["parameters"])}
                definition["parameters"]["properties"]["comparison_mode"]["enum"].append("cross_subject")
                definition["description"] += (
                    " cross_subject explicitly compares different authorized subjects without asserting fiscal-period "
                    "or economic comparability; original periods are preserved. Unit and basis checks still apply.")
                result.append(definition)
                continue
            definition = {**definition, "parameters": deepcopy(definition["parameters"])}
            definition["parameters"]["properties"]["company_id"] = {
                "type": "string", "enum": list(self.company_ids)}
            definition["parameters"]["required"] = [
                *definition["parameters"].get("required", []), "company_id"]
            definition["description"] += " Explicit company_id is mandatory; no first-subject default is used."
            if name == "derive_pdf_tables":
                definition["description"] += (
                    " Select the subject that owns base_document_version_id; a base version from one subject "
                    "cannot be reused for another subject."
                )

            definition["handler"] = handler_for(name)
            result.append(definition)
        return result

    tools = definitions

    def replay_arithmetic(self, record):
        if record.get("company_id") != self.company_id:
            return self.for_company(record.get("company_id")).replay_arithmetic(record)
        return super().replay_arithmetic(record)
