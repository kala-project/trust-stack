"""Clean-session verifier application and narrow tool adapter.

Only runner handlers write durable receipts/findings/revisions. Model text is never an
approval and the author transcript is never part of the verifier packet.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import threading
import uuid

from .publication_contracts import CanonicalPublication
from .verification_contracts import (
    SemanticReview, VerificationMetadata, VerifiedCitation, CheckCoverage, PreparedPublication, VerificationFinding,
    CHECK_KINDS, FINDING_OUTCOMES, FINAL_OUTCOMES,
    check_kind as contract_check_kind, choice, completed_check_nodes,
    text as contract_text, verification_checks as derive_verification_checks,
    verification_metadata_schema, decode_limitations, validate_final_limits,
)
from .candidate_contracts import AuthorInputs
from .research_records import thaw_object
from .evidence_checks import (
    EvidenceCheckError, EvidenceRecordRead, ReceiptBackedEvent, ReferenceDescriptor,
    TrustedEvidenceAssembler, VerifiedRecordSource, calculation_records,
    projection_indexes, read_evidence_record, resolve_event_reference,
)
from .publication_revision import patch_publication, replace_publication, revision_diff
from .report_publications import ReportPublications, reference_dependencies, validate_bindings
from .tool_errors import ToolError
from .verification_store import (
    ArtifactRef, RevisedCandidate, VerificationBudgetExceeded,
    effective_findings, required_verifier_projection,
    VerificationCheckpoint, VerificationIntegrityError,
    VerificationStore, VerificationToolReceipt,
)

READ_TOOLS = frozenset({
    "discover_research_catalog", "search_evidence", "get_claim_evidence",
    "get_candidate_reference", "get_company_facts", "search_documents", "read_passage", "get_document_coverage",
    "read_document_table", "extract_document_table_fact", "analyze_period_coverage",
    "calculate_facts", "calculate_yoy", "calculate_regression", "calculate_forecast",
    "read_research_inputs", "list_native_sources", "describe_native_source", "execute_native_sql",
})


@dataclass(frozen=True)
class VerificationExecutionConfig:
    # Calls count every model-exposed operation, including rejected findings and finalization.
    max_tool_calls: int = 100
    max_provider_events: int = 2000
    max_output_chars: int = 32_000
    max_event_payload_bytes: int = 8_000_000
    max_tool_result_bytes: int = 500_000
    timeout_seconds: float = 600.0
    max_revision_rounds: int = 2

    def __post_init__(self):
        for name in ("max_tool_calls", "max_provider_events", "max_output_chars",
                     "max_event_payload_bytes", "max_tool_result_bytes",
                     "max_revision_rounds"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_tool_result_bytes > self.max_event_payload_bytes:
            raise ValueError("max_tool_result_bytes cannot exceed max_event_payload_bytes")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")


class VerificationToolAdapter:
    """Expose allowlisted data operations and persist exact authoritative results."""

    def __init__(self, store, claim, packet, definitions, *, max_result_bytes,
                 cancelled, usage_limits, result_adapter=None):
        self.store, self.claim, self.packet = store, claim, packet
        self.max_result_bytes = max_result_bytes
        self.cancelled, self.usage_limits = cancelled, usage_limits
        self.result_adapter = result_adapter
        self.references: dict[str, ArtifactRef] = {}
        self.tools = []
        for definition in definitions:
            if definition.get("name") not in READ_TOOLS:
                continue
            original, name = definition["handler"], definition["name"]

            def handler(_original=original, _name=name, **arguments):
                self.reserve_invocation()
                call_id = f"verify_tool_{uuid.uuid4().hex}"
                try:
                    result = _original(**arguments)
                    if self.result_adapter is not None:
                        result = self.result_adapter(_name, result)
                    if not isinstance(result, dict):
                        result = {"value": result}
                    raw = json.dumps(result, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")
                    # Reserve the exact result before it can be returned to the provider.
                    self.store.consume_usage(
                        self.claim, payload_bytes=len(raw), limits=self.usage_limits,
                        stage="tool_result")
                    if len(raw) > self.max_result_bytes:
                        self.store.append_tool_receipt(self.claim, VerificationToolReceipt(
                            self.packet["candidate_id"], self.packet["version_id"], call_id,
                            _name, deepcopy(arguments),
                            {"error": "tool_result_too_large", "stage": "tool_result",
                             "requested_size": min(len(raw), 1_000_000_000),
                             "completed": False}))
                        raise ToolError(
                            "tool_result_too_large",
                            "Tool result exceeds the verifier per-result limit; narrow the query.")
                except Exception as exc:
                    if not (isinstance(exc, ToolError)
                            and getattr(exc, "code", None) == "tool_result_too_large"):
                        self.store.append_tool_receipt(self.claim, VerificationToolReceipt(
                            self.packet["candidate_id"], self.packet["version_id"], call_id,
                            _name, deepcopy(arguments),
                            {"error": type(exc).__name__, "completed": False}))
                    raise
                reference = self.store.append_tool_receipt(self.claim, VerificationToolReceipt(
                    self.packet["candidate_id"], self.packet["version_id"], call_id,
                    _name, deepcopy(arguments), deepcopy(result)))
                self.references[reference.identifier] = reference
                return {**result, "_verification_receipt": {
                    "kind": reference.kind, "identifier": reference.identifier,
                    "sha256": reference.sha256,
                }}

            self.tools.append({**definition, "handler": handler})

    def reserve_invocation(self):
        if self.cancelled.is_set():
            raise RuntimeError("verification execution stopped")
        # The store checks cumulative wall time in the same transaction. The durable
        # store, not an attempt-local counter, owns the inclusive operation limit.
        self.store.consume_usage(
            self.claim, tool_calls=1, limits=self.usage_limits, stage="tool_invocation")

    @staticmethod
    def _control_error_code(exc):
        if isinstance(exc, ToolError):
            code = getattr(exc, "code", None)
            if isinstance(code, str) and code in {
                    "stale_version", "stale_preview", "missing_terminal_findings",
                    "receipt_not_current", "invalid_semantic_review", "invalid_verification"}:
                return code
        if isinstance(exc, VerificationBudgetExceeded):
            return "budget_exceeded"
        if isinstance(exc, VerificationIntegrityError):
            return "integrity_rejected"
        if isinstance(exc, (TypeError, ValueError)):
            return "invalid_arguments"
        return "control_rejected"

    def wrap_control(self, operation, handler):
        """Charge once and retain only bounded non-evidence rejection diagnostics."""
        def charged(**arguments):
            self.reserve_invocation()
            try:
                return handler(**arguments)
            except Exception as exc:
                try:
                    self.store.append_control_diagnostic(
                        self.claim, candidate_id=self.packet["candidate_id"],
                        operation=operation, error_code=self._control_error_code(exc))
                except Exception:
                    # A lost claim cannot safely write a diagnostic and must not replace
                    # the original control failure with logging behavior.
                    pass
                raise
        return charged


class VerificationApplication:
    """Own one verify claim; the worker sees no ChatService proxy."""

    def __init__(self, store: VerificationStore, provider, research_factory,
                 native_definitions_factory=None, *, document_fact_factory=None,
                 research_result_adapter=None, native_query_replay=None, config=None):
        self.store, self.provider, self.research_factory = store, provider, research_factory
        self.native_definitions_factory = native_definitions_factory
        self.document_fact_factory = document_fact_factory
        self.research_result_adapter = research_result_adapter
        self.native_query_replay = native_query_replay
        self.config = config or VerificationExecutionConfig()

    @staticmethod
    def _node_references(publication, node_id):
        return reference_dependencies(publication, node_id)

    @staticmethod
    def _reference(value) -> ArtifactRef:
        if not isinstance(value, dict):
            raise ValueError("tool result reference must be an object")
        return ArtifactRef(
            value.get("kind"), value.get("identifier", value.get("id")), value.get("sha256"))

    def execute(self, claim, cancel_event: threading.Event):
        usage_limits = {"tool_calls": self.config.max_tool_calls,
                        "provider_events": self.config.max_provider_events,
                        "output_chars": self.config.max_output_chars,
                        "payload_bytes": self.config.max_event_payload_bytes,
                        "elapsed_seconds": self.config.timeout_seconds}
        usage = self.store.begin_usage(claim)
        remaining = self.config.timeout_seconds - usage["elapsed_seconds"]
        if (remaining <= 0 or usage["tool_calls"] >= self.config.max_tool_calls
                or usage["provider_events"] >= self.config.max_provider_events
                or usage["output_chars"] >= self.config.max_output_chars
                or usage["payload_bytes"] >= self.config.max_event_payload_bytes
                or usage["exhaustion_code"] is not None):
            self.store.end_usage(claim)
            raise VerificationBudgetExceeded(
                usage["exhaustion_stage"] or "execution",
                usage["exhaustion_code"] or ("elapsed_seconds" if remaining <= 0
                                              else "cumulative_limit"),
                usage["exhaustion_requested_size"] or 0)
        packet = self.store.read_claim_input(claim)
        verification_checks = derive_verification_checks(packet["publication"])
        author_catalog = AuthorInputs.decode(packet["author_inputs"])
        native_scope = deepcopy(packet["input_snapshot"].get("native_sources"))

        def replay_native_query(query_id):
            if not callable(self.native_query_replay):
                raise ToolError(
                    "native_source_unavailable", "Native query replay capability is unavailable")
            return self.native_query_replay(
                query_id, owner=packet["conversation_id"],
                run_id=packet["author_run_id"], scope=native_scope)

        research = self.research_factory(packet)
        definitions = list(research.definitions())
        if self.native_definitions_factory is not None:
            definitions.extend(self.native_definitions_factory(packet, cancel_event))
        reusable = set()
        for checkpoint in packet["checkpoints"]:
            if checkpoint["version_id"] == packet["version_id"]:
                reusable.update(checkpoint["covered_node_ids"])
        state = {"packet": packet, "revision_rounds": packet["version_number"] - 1,
                 "preview": None, "pending_publication": None, "final": None,
                 "events": usage["provider_events"], "output_chars": usage["output_chars"],
                 "payload_bytes": usage["payload_bytes"],
                 "covered": reusable, "checkpoint_seq": 0}

        class _Replay:
            def replay_arithmetic(self, check_input, inputs, item):
                if not hasattr(research, "replay_arithmetic"):
                    raise EvidenceCheckError("deterministic arithmetic replay is unavailable")
                return research.replay_arithmetic(item)
            def replay_model(self, model, check_input, inputs, parameters):
                method = getattr(research, "replay_model", None)
                if not callable(method):
                    raise EvidenceCheckError("deterministic model replay is unavailable")
                result = method(model, check_input, inputs, parameters)
                return result.get("calculations", result) if isinstance(result, dict) else result

        def trusted_context():
            events = self.store.read_evidence_events(
                claim, candidate_id=packet["candidate_id"], version_id=packet["version_id"])
            snapshot = packet["input_snapshot"]
            company_id = snapshot.get("evidence", {}).get("company_id")
            company_ids = packet["scope"].get("company_ids") or packet["scope"].get("companies")
            if not isinstance(company_ids, list) or not company_ids:
                company_ids = [company_id]
            receipts = []
            for index, event in enumerate(events):
                raw = json.dumps(event, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False).encode("utf-8")
                receipts.append(ReceiptBackedEvent(
                    f"{packet['candidate_id']}:{packet['version_id']}:{index + 1}",
                    hashlib.sha256(raw).hexdigest(), raw))
            document_facts = ()
            if self.document_fact_factory is not None:
                document_facts = tuple(self.document_fact_factory(packet, tuple(receipts)))
            source = VerifiedRecordSource(
                run_id=packet["author_run_id"], company_id=company_id,
                company_ids=company_ids,
                snapshot_bytes=json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                          separators=(",", ":")).encode("utf-8"),
                event_receipts=receipts, document_facts=document_facts)
            check_input = TrustedEvidenceAssembler.assemble(
                source, run_id=packet["author_run_id"], company_id=company_id)
            indexes = projection_indexes(check_input)
            # The clean verifier starts with baseline facts only. Dynamic author facts
            # become calculation inputs only after the storage replay and sealed-scope
            # checks above, never by trusting values copied from a candidate descriptor.
            for verified in check_input.document_facts:
                fact = verified.fact
                identifier = fact["fact_id"]
                previous = research._facts.get(identifier)
                if previous is not None and previous != fact:
                    raise VerificationIntegrityError("source-verified fact conflicts with verifier input")
                research._facts[identifier] = deepcopy(fact)
            checked = calculation_records(check_input, indexes, _Replay())
            indexes["calculations"] = {item["calculation_id"]: item for item in checked.records
                                       if item.get("status") == "recorded"}
            return check_input, indexes, events, checked

        def preparation_catalogs():
            _check_input, indexes, events, checked = trusted_context()
            refs = author_catalog.to_json()
            # Only receipt-returned frozen facts are available for a new binding.
            allowed_fact_ids = {entry.identifier for entry in (author_catalog.fact or ())}
            def visit(value):
                if isinstance(value, dict):
                    fact_id = value.get("fact_id")
                    frozen = indexes["facts"].get(fact_id) if isinstance(fact_id, str) else None
                    if (isinstance(frozen, dict)
                            and {key: item for key, item in frozen.items() if key != "source_id"}
                            == {key: item for key, item in value.items() if key != "source_id"}):
                        allowed_fact_ids.add(fact_id)
                    for child in value.values(): visit(child)
                elif isinstance(value, list):
                    for child in value: visit(child)
            for event in events: visit(event.get("result"))
            facts = {key: value for key, value in indexes["facts"].items()
                     if key in allowed_fact_ids}
            # Add exact current receipt records, including corrected calculations.
            for kind in refs:
                identifiers = set(refs[kind])
                def collect(value):
                    if isinstance(value, dict):
                        key = "calculation_id" if kind == "calculation" else f"{kind}_id"
                        if isinstance(value.get(key), str): identifiers.add(value[key])
                        for child in value.values(): collect(child)
                    elif isinstance(value, list):
                        for child in value: collect(child)
                for event in events: collect(event.get("result"))
                for identifier in identifiers:
                    _event, original = resolve_event_reference(events, kind, identifier)
                    if original is not None:
                        refs[kind][identifier] = (
                            {"query_id": identifier, "record": original}
                            if kind == "query" else original)
            return refs, facts, indexes["calculations"]

        def get_candidate_reference(kind, identifier):
            """Return one bounded complete record allowed by a sealed descriptor."""
            submitted = packet["evidence_refs"] + packet["calculation_refs"]
            reference = next((item for item in submitted
                              if item["kind"] == kind and item["id"] == identifier), None)
            frozen_record = author_catalog.lookup(kind, identifier)
            if reference is None or frozen_record is None:
                raise VerificationIntegrityError(
                    "candidate reference is not present in the sealed input catalog")
            try:
                descriptor = ReferenceDescriptor.validate(
                    kind, identifier, thaw_object(frozen_record), reference["sha256"])
                check_input, _indexes, _events, checked = trusted_context()
                record = read_evidence_record(
                    check_input, kind, identifier,
                    calculations=checked.records,
                    calculation_events=checked.event_by_id,
                    max_record_bytes=self.config.max_tool_result_bytes,
                )
            except EvidenceCheckError as exc:
                raise VerificationIntegrityError(str(exc)) from exc
            response = record.as_dict()
            if response["state"] == "missing":
                response.update(
                    state="incomplete",
                    reason="sealed descriptor has no persisted complete evidence record")
            return {
                **response,
                "candidate_reference_sha256": descriptor.descriptor_sha256,
            }

        definitions.append({
            "name": "get_candidate_reference",
            "description": "Read one bounded complete passage, structured fact, validated document fact, SQL query, claim, or deterministically replayed calculation allowed by a submitted hash-bound descriptor. Missing or incomplete records return an explicit state and are never reconstructed from author text. Descriptor and complete-record hashes are distinct; neither is semantic approval.",
            "parameters": {"type": "object", "additionalProperties": False,
                           "required": ["kind", "identifier"],
                           "properties": {
                               "kind": {"type": "string", "enum": ["claim", "evidence", "passage", "query", "calculation"]},
                               "identifier": {"type": "string", "minLength": 1,
                                              "maxLength": 200}}},
            "handler": get_candidate_reference,
        })
        result_adapter = (None if self.research_result_adapter is None else
                          lambda name, result: self.research_result_adapter(research, name, result))
        adapter = VerificationToolAdapter(
            self.store, claim, packet, definitions,
            max_result_bytes=self.config.max_tool_result_bytes,
            cancelled=cancel_event, usage_limits=usage_limits,
            result_adapter=result_adapter)

        def require_current_version(version_id):
            if version_id != packet["version_id"]:
                raise ToolError(
                    "stale_version",
                    "The candidate version changed; use the current version_id from the latest revision result.")

        def validate_revision(publication):
            check_input, indexes, _events, calculation_check = trusted_context()
            max_record_bytes = self.config.max_tool_result_bytes

            class _Reviewer:
                def __init__(self):
                    self.indexes = indexes
                    self.run_id = packet["author_run_id"]

                def replay_native_query(self, query_id):
                    return replay_native_query(query_id)

                def read_reference_record(self, kind: str, identifier: str) -> EvidenceRecordRead:
                    return read_evidence_record(
                        check_input, kind, identifier,
                        calculations=calculation_check.records,
                        calculation_events=calculation_check.event_by_id,
                        max_record_bytes=max_record_bytes,
                    )

            validate_bindings(publication, _Reviewer())
            used_calculations = set()

            def collect(value):
                if isinstance(value, dict):
                    if value.get("kind") == "calculation" and isinstance(value.get("id"), str):
                        used_calculations.add(value["id"])
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)

            collect(publication)
            if used_calculations - set(indexes["calculations"]):
                raise VerificationIntegrityError(
                    "revision contains calculations that did not pass exact-input replay")

        def preview_revision(version_id, mode, revision_summary, title=None,
                             replacements=None, blocks=None, datasets=None,
                             value_style=None, scope_coverage=None):
            require_current_version(version_id)
            if (not isinstance(revision_summary, str) or not revision_summary.strip()
                    or len(revision_summary) > 4000):
                raise ValueError("revision_summary must contain 1..4000 characters")
            refs, facts, calculations = preparation_catalogs()
            if mode == "patch":
                if blocks is not None or datasets is not None or value_style is not None or scope_coverage is not None:
                    raise ValueError("patch mode accepts only optional title and replacements")
                publication = patch_publication(
                    publication=packet["publication"], title=title,
                    replacements=[] if replacements is None else replacements,
                    refs=refs, facts=facts, calculations=calculations, max_chars=100_000)
            elif mode == "replace":
                if replacements is not None or title is None or blocks is None:
                    raise ValueError("replace mode requires title and blocks and does not accept replacements")
                publication = replace_publication(
                    title=title, blocks=blocks, datasets=datasets,
                    value_style=value_style or "recorded", refs=refs, facts=facts,
                    calculations=calculations, max_chars=100_000)
            else:
                raise ValueError("mode must be patch or replace")
            if publication == packet["publication"]:
                raise VerificationIntegrityError("revision preview does not change the publication")
            validate_revision(publication)
            diff = revision_diff(packet["publication"], publication)
            if mode == "replace" and diff["removed_node_ids"]:
                if not isinstance(scope_coverage, str) or not scope_coverage.strip() or len(scope_coverage) > 4000:
                    raise ToolError(
                        "invalid_semantic_review",
                        "A full replacement that removes nodes must explain original-question scope coverage.")
            preview_id = f"revision_preview_{uuid.uuid4().hex}"
            state["preview"] = {
                "preview_id": preview_id, "candidate_id": packet["candidate_id"],
                "base_version_id": packet["version_id"], "input_hash": packet["input_hash"],
                "publication_sha256": packet["publication_sha256"],
                "publication": deepcopy(publication), "revision_summary": revision_summary,
                "scope_coverage": scope_coverage,
            }
            return deepcopy({
                "accepted": True, "preview_id": preview_id,
                "base_version_id": packet["version_id"], "mode": mode, "diff": diff,
                "original_question": packet["question"],
                "requirement": "Assess whether the revised publication still covers the original question; no mechanical completeness rule is applied.",
                **({"scope_coverage": scope_coverage} if scope_coverage is not None else {}),
            })

        def revise_candidate(version_id, preview_id):
            require_current_version(version_id)
            preview = state["preview"]
            if (not isinstance(preview, dict) or preview_id != preview.get("preview_id")
                    or version_id != preview.get("base_version_id")
                    or packet["candidate_id"] != preview.get("candidate_id")
                    or packet["input_hash"] != preview.get("input_hash")
                    or packet["publication_sha256"] != preview.get("publication_sha256")):
                raise ToolError(
                    "stale_preview",
                    "The preview handle is missing, replaced, or no longer bound to the current candidate version.")
            if state["revision_rounds"] >= self.config.max_revision_rounds:
                raise ValueError("revision round budget exhausted")
            validate_revision(preview["publication"])
            result = self.store.append_revision(claim, RevisedCandidate(
                packet["candidate_id"], packet["version_id"], CanonicalPublication.decode(preview["publication"]),
                (), preview["revision_summary"]))
            state["preview"] = None
            state["revision_rounds"] += 1
            packet.update(self.store.read_version(
                packet["conversation_id"], packet["candidate_id"], result["version_id"]))
            verification_checks.clear()
            verification_checks.update(derive_verification_checks(packet["publication"]))
            adapter.packet = packet
            state["covered"] = set()
            return deepcopy({
                "accepted": True, **result,
                "publication": packet["publication"],
                "content_nodes": list(packet["nodes"]),
                "verification_checks": verification_checks,
                "evidence_refs": packet["evidence_refs"],
                "calculation_refs": packet["calculation_refs"],
                "instruction": "Use this current publication and content_nodes. Terminal findings require current-version reads for every node, including unchanged nodes; previous-version receipts do not qualify. Read each support or binding.reference identity, not binding.id fact identifiers.",
            })

        def record_finding(version_id, node_id, check_kind, outcome,
                           explanation, receipt_ids=None):
            outcome = choice(outcome, FINDING_OUTCOMES)
            check_kind = contract_check_kind(check_kind)
            explanation = contract_text(explanation, "explanation", 20000)
            require_current_version(version_id)
            identity = packet["nodes"].get(node_id)
            if identity is None:
                raise VerificationIntegrityError("finding refers to an unknown current content node")
            if check_kind not in verification_checks[node_id]["required"]:
                raise VerificationIntegrityError(
                    f"{check_kind} is not applicable to current content node {node_id}")
            refs = ()
            explicit_receipts = receipt_ids is not None
            if explicit_receipts:
                try:
                    refs = self.store.resolve_receipt_ids(
                        claim, tuple(receipt_ids), candidate_id=packet["candidate_id"],
                        version_id=version_id)
                except VerificationIntegrityError as exc:
                    raise ToolError(
                        "receipt_not_current",
                        "A receipt ID is not available for this claim, candidate, and current version.") from exc
            if outcome not in {"unchecked", "unresolved"}:
                successful_refs = self.store.resolve_successful_receipts(
                    claim, candidate_id=packet["candidate_id"], version_id=version_id)
                successful = {reference.identifier for reference in successful_refs}
                if explicit_receipts and any(reference.identifier not in successful for reference in refs):
                    raise VerificationIntegrityError(
                        "terminal findings require successful complete verifier data reads")
                available_refs = refs if explicit_receipts else successful_refs
                if not available_refs:
                    raise VerificationIntegrityError(
                        "terminal findings require successful current-version persisted data reads")
                receipts = self.store.read_receipts(
                    claim, available_refs, candidate_id=packet["candidate_id"], version_id=version_id)
                dependencies = self._node_references(packet["publication"], node_id)
                selected = []
                missing = []
                for kind, identifier in dependencies:
                    match = None
                    for reference, receipt in zip(available_refs, receipts):
                        event, resolved = resolve_event_reference([receipt], kind, identifier)
                        if event is not None and resolved is not None:
                            match = reference
                            break
                    if match is None:
                        missing.append({"kind": kind, "identifier": identifier})
                    elif match not in selected:
                        selected.append(match)
                if missing:
                    raise VerificationIntegrityError(
                        "Missing successful current-version verifier reads for node " + node_id
                        + ": " + json.dumps(missing[:50], separators=(",", ":"))
                        + (f" (+{len(missing) - 50} more)" if len(missing) > 50 else "")
                        + ". Read the exact support/binding.reference identities above; "
                        "binding.id fact identifiers and previous-version receipts do not qualify.")
                if not explicit_receipts:
                    refs = tuple(selected) if dependencies else available_refs
            result = self.store.append_finding(claim, VerificationFinding(
                packet["candidate_id"], version_id, node_id, identity["node_hash"],
                identity["dependency_hash"], check_kind, outcome, explanation, refs))
            current_findings = self.store.read_findings(
                claim, candidate_id=packet["candidate_id"], version_id=version_id)
            state["covered"] = completed_check_nodes(current_findings, verification_checks)
            state["checkpoint_seq"] += 1
            self.store.append_checkpoint(claim, VerificationCheckpoint(
                packet["candidate_id"], packet["version_id"],
                f"finding-{state['checkpoint_seq']}", tuple(sorted(state["covered"])),
                tuple(sorted(set(packet["nodes"]) - state["covered"])), refs,
                "Sanitized hash-bound verifier progress."))
            return {"accepted": True, **result}

        def finalize_verification(version_id, outcome, limitations,
                                  semantic_review, verification):
            require_current_version(version_id)
            try:
                review = SemanticReview.decode(semantic_review)
            except ValueError as exc:
                raise ToolError("invalid_semantic_review", str(exc)) from exc
            try:
                metadata = VerificationMetadata.decode_model(verification)
                final_outcome = choice(outcome, FINAL_OUTCOMES)
                final_limitations = decode_limitations(limitations)
                validate_final_limits(final_outcome, final_limitations)
            except ValueError as exc:
                raise ToolError("invalid_verification", str(exc)) from exc
            required = tuple(packet["nodes"])
            findings = self.store.read_findings(
                claim, candidate_id=packet["candidate_id"], version_id=version_id)
            checked_set = completed_check_nodes(findings, verification_checks)
            terminal_findings = [item for item in effective_findings(findings)
                                 if item["node_id"] in checked_set]
            checked = tuple(node_id for node_id in required if node_id in checked_set)
            unresolved = tuple(node_id for node_id in required if node_id not in checked_set)
            if outcome in {"verified", "verified_with_limitations"} and unresolved:
                shown = list(unresolved[:50])
                suffix = "" if len(unresolved) <= 50 else f" (+{len(unresolved) - 50} more)"
                raise ToolError(
                    "missing_terminal_findings",
                    "Missing applicable current terminal findings for node IDs: "
                    + json.dumps(shown, ensure_ascii=False, separators=(",", ":")) + suffix)
            if outcome == "verified_with_limitations" and not any(
                    block.get("classification") == "evidence_gap" and block.get("limitations")
                    for block in packet["publication"].get("blocks", [])):
                raise VerificationIntegrityError(
                    "limitations publication must disclose the unanswered area in the report body")

            publication_refs = {reference for node_id in packet["nodes"]
                                for reference in self._node_references(packet["publication"], node_id)}
            citation_inputs = []
            seen_citations = set()
            for finding in terminal_findings:
                for raw_reference in finding["tool_result_refs"]:
                    reference = self._reference(raw_reference)
                    receipt = self.store.read_receipts(
                        claim, (reference,), candidate_id=packet["candidate_id"],
                        version_id=version_id)
                    for kind, identifier in publication_refs:
                        event, original = resolve_event_reference(receipt, kind, identifier)
                        key = (kind, identifier)
                        if event is not None and original is not None and key not in seen_citations:
                            seen_citations.add(key)
                            citation_inputs.append({
                                "kind": kind, "id": identifier,
                                "verification_receipt": {
                                    "kind": reference.kind,
                                    "identifier": reference.identifier,
                                    "sha256": reference.sha256,
                                },
                            })
            verified_citations = []
            for citation in citation_inputs:
                raw_reference = citation.get("verification_receipt") if isinstance(citation, dict) else None
                reference = self._reference(raw_reference)
                if not self.store.receipt_exists(
                        claim, reference, candidate_id=packet["candidate_id"],
                        version_id=packet["version_id"]):
                    raise VerificationIntegrityError("citation lacks a current persisted verifier receipt")
                receipt = self.store.read_receipts(
                    claim, (reference,), candidate_id=packet["candidate_id"],
                    version_id=packet["version_id"])
                event, original = resolve_event_reference(
                    receipt, citation.get("kind"), citation.get("id"))
                if event is None or original is None:
                    raise VerificationIntegrityError("citation receipt is irrelevant to its declared reference")
                # Identity and source closure come only from the exact persisted receipt.
                # Model-supplied company/version/source fields are intentionally discarded.
                source_metadata = original
                receipt_result = event.get("result") if isinstance(event, dict) else None
                if isinstance(receipt_result, dict) and isinstance(receipt_result.get("source_provenance"), dict):
                    source_metadata = receipt_result["source_provenance"]
                    if (citation.get("kind") == "evidence"
                            and isinstance(source_metadata.get("source"), dict)):
                        source_metadata = source_metadata["source"]
                    source_metadata = {**source_metadata,
                                       "company_id": original.get("company_id")}
                if (citation.get("kind") == "evidence"
                        and isinstance(event.get("result"), dict)
                        and event["result"].get("fact") == original):
                    provenance = event["result"].get("historical_provenance")
                    if isinstance(provenance, dict) and isinstance(provenance.get("source"), dict):
                        source_metadata = {**provenance["source"],
                                           "company_id": original.get("company_id")}
                derived = {}
                for key in ("company_id", "document_version_id", "source_sha256",
                            "parser_version", "title", "source_url", "published_at",
                            "collected_at", "rights_status"):
                    value = source_metadata.get(key) if isinstance(source_metadata, dict) else None
                    if value is not None:
                        derived[key] = deepcopy(value)
                verified_citations.append(VerifiedCitation.decode({
                    "kind": citation.get("kind"), "id": citation.get("id"),
                    "verification_receipt": deepcopy(raw_reference), **derived, "valid": True,
                }))
            publication_refs = {reference for node_id in packet["nodes"]
                                for reference in self._node_references(packet["publication"], node_id)}
            citation_refs = {(item.kind, item.id) for item in verified_citations}
            if outcome in {"verified", "verified_with_limitations"} and not publication_refs <= citation_refs:
                raise VerificationIntegrityError("final citations must cover every publication evidence reference")
            if outcome in {"verified", "verified_with_limitations"}:
                check_input, indexes, _events, calculation_check = trusted_context()
                max_record_bytes = self.config.max_tool_result_bytes
                class _Reviewer:
                    def __init__(self):
                        self.indexes = indexes
                        self.run_id = packet["author_run_id"]
                    def replay_native_query(self, query_id):
                        return replay_native_query(query_id)
                    def read_reference_record(self, kind: str, identifier: str) -> EvidenceRecordRead:
                        return read_evidence_record(
                            check_input, kind, identifier,
                            calculations=calculation_check.records,
                            calculation_events=calculation_check.event_by_id,
                            max_record_bytes=max_record_bytes,
                        )
                try:
                    validate_bindings(packet["publication"], _Reviewer())
                except Exception as exc:
                    raise VerificationIntegrityError(
                        f"publication bindings failed deterministic verification: {exc}") from exc
                used_calculations = set()
                def collect_calculations(value):
                    if isinstance(value, dict):
                        if value.get("kind") == "calculation" and isinstance(value.get("id"), str):
                            used_calculations.add(value["id"])
                        for child in value.values(): collect_calculations(child)
                    elif isinstance(value, list):
                        for child in value: collect_calculations(child)
                collect_calculations(packet["publication"])
                missing = used_calculations - set(indexes["calculations"])
                if missing:
                    raise VerificationIntegrityError(
                        "publication contains calculations that did not pass exact-input replay")
                if calculation_check.limitations and used_calculations:
                    # Invalid calculations cannot be hidden behind a model-supplied
                    # verified status. Used IDs above must all be in the valid index;
                    # retain exclusions in the auditable verification payload.
                    metadata = metadata.with_calculation_limitations(calculation_check.limitations)
            prepared = PreparedPublication(
                packet["candidate_id"], packet["version_id"], packet["input_hash"], final_outcome,
                CanonicalPublication.decode(packet["publication"]), review, tuple(verified_citations), metadata,
                CheckCoverage(required, checked, unresolved), final_limitations,
                required_verifier_projection(terminal_findings))
            if state["pending_publication"] is not None:
                raise VerificationIntegrityError("final publication is already staged")
            state["pending_publication"] = prepared
            return {"accepted": True, "staged": True,
                    "candidate_id": prepared.candidate_id, "version_id": prepared.version_id,
                    "outcome": prepared.outcome}

        natural_schema = deepcopy(ReportPublications.definition(lambda **_kwargs: None)["parameters"])
        block_schema = natural_schema["properties"]["blocks"]["items"]
        preview_schema = {"type": "object", "additionalProperties": False,
            "required": ["version_id", "mode", "revision_summary"],
            "properties": {
                "version_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "mode": {"type": "string", "enum": ["patch", "replace"]},
                "revision_summary": {"type": "string", "minLength": 1, "maxLength": 4000,
                    "description": "Durable concise account of corrected claims and why the report changed."},
                "title": natural_schema["properties"]["title"],
                "replacements": {"type": "array", "maxItems": 80, "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["block_id", "block"], "properties": {
                        "block_id": {"type": "string", "minLength": 1, "maxLength": 200},
                        "block": block_schema}}},
                "blocks": natural_schema["properties"]["blocks"],
                "datasets": natural_schema["properties"]["datasets"],
                "value_style": natural_schema["properties"]["value_style"],
                "scope_coverage": {"type": "string", "minLength": 1, "maxLength": 4000,
                    "description": "Required when explicit replacement removes nodes: explain coverage of the original question without claiming a mechanical completeness proof."},
            }}
        commit_schema = {"type": "object", "additionalProperties": False,
            "required": ["version_id", "preview_id"], "properties": {
                "version_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "preview_id": {"type": "string", "minLength": 1, "maxLength": 200}}}
        string_ids = {"type": "array", "maxItems": 10000,
                      "items": {"type": "string", "minLength": 1, "maxLength": 200},
                      "uniqueItems": True}
        finding_schema = {"type": "object", "additionalProperties": False,
            "required": ["version_id", "node_id", "check_kind", "outcome",
                         "explanation"],
            "properties": {
                "version_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "node_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "check_kind": {"type": "string", "enum": list(CHECK_KINDS)},
                "outcome": {"type": "string", "enum": ["supported", "corrected_and_rechecked",
                    "conflict_disclosed", "insufficient_material_disclosed", "unchecked", "unresolved"]},
                "explanation": {"type": "string", "minLength": 1, "maxLength": 20000},
                "receipt_ids": {**string_ids,
                    "description": "Optional narrowing only. Normally omit this field: the runner binds exact already-persisted successful verifier reads to this node's declared dependencies. If supplied, every ID remains strictly validated; aliases, guessed IDs, and wildcards are not accepted."}}}
        final_schema = {"type": "object", "additionalProperties": False,
            "required": ["version_id", "outcome", "limitations",
                         "semantic_review", "verification"],
            "properties": {
                "version_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "outcome": {"type": "string", "enum": ["verified", "verified_with_limitations", "unresolved"]},
                "limitations": {"type": "array", "maxItems": 100,
                                "items": {"type": "string", "minLength": 1, "maxLength": 4000}},
                "semantic_review": {"type": "object", "additionalProperties": False,
                    "required": ["original_question_coverage"],
                    "properties": {"original_question_coverage": {
                        "type": "string", "minLength": 1, "maxLength": 4000}}},
                "verification": verification_metadata_schema()}}
        tools = adapter.tools + [
            {"name": "preview_verification_revision",
             "description": "Preflight a targeted block patch (omitted blocks and all datasets are preserved) or an explicit complete replacement. This validates canonical bindings and deterministic calculations without appending a version. A newer preview replaces the prior opaque handle.",
             "parameters": preview_schema,
             "handler": adapter.wrap_control("preview_verification_revision", preview_revision)},
            {"name": "revise_verification_candidate",
             "description": "Commit the exact server-held publication from the current opaque preview. Do not resubmit report content or hashes. On acceptance, use the returned version_id and content node IDs.",
             "parameters": commit_schema,
             "handler": adapter.wrap_control("revise_verification_candidate", revise_candidate)},
            {"name": "record_verification_finding",
             "description": "Record one semantic check for a current-version node after performing the needed data reads. Normally omit receipt_ids: the runner binds exact already-persisted successful verifier reads to declared node dependencies. Supply receipt_ids only to deliberately narrow those reads; supplied IDs remain strict. The runner resolves hashes and never approves semantics or finalizes.",
             "parameters": finding_schema,
             "handler": adapter.wrap_control("record_verification_finding", record_finding)},
            {"name": "finalize_verification",
             "description": "Request finalization for the current version. The runner derives terminal coverage and citations, reports missing node IDs, replays bindings and calculations, and never formally publishes an unresolved outcome.",
             "parameters": final_schema,
             "handler": adapter.wrap_control("finalize_verification", finalize_verification),
             "stop_after_success": True},
        ]
        prompt = json.dumps({
            "question": packet["question"], "scope": packet["scope"],
            "as_of_snapshot": packet["input_snapshot"], "base_message_id": packet["base_message_id"],
            "candidate_id": packet["candidate_id"], "version_id": packet["version_id"],
            "publication_sha256": packet["publication_sha256"], "publication": packet["publication"],
            "content_nodes": list(packet["nodes"]), "verification_checks": verification_checks,
            "evidence_refs": packet["evidence_refs"],
            "calculation_refs": packet["calculation_refs"], "saved_checkpoints": packet["checkpoints"],
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        try:
            stream = getattr(self.provider, "stream_verification", None)
            if not callable(stream):
                raise RuntimeError("required verification provider does not support clean sessions")
            iterator = iter(stream([{"role": "user", "content": prompt}], tools, cancel_event))
            while True:
                if cancel_event.is_set():
                    raise RuntimeError("verification execution stopped")
                # Reserve before advancing provider code, so an abrupt process death
                # cannot make a consumed event (or elapsed time) disappear on retry.
                updated = self.store.consume_usage(
                    claim, provider_events=1, limits=usage_limits, stage="provider_event")
                state["events"] = updated["provider_events"]
                try:
                    event = next(iterator)
                except StopIteration:
                    break
                encoded = json.dumps(
                    event, ensure_ascii=False, separators=(",", ":"), default=str
                ).encode("utf-8")
                updated = self.store.consume_usage(
                    claim, payload_bytes=len(encoded), limits=usage_limits,
                    stage="provider_event")
                state["payload_bytes"] = updated["payload_bytes"]
                # Copilot normalizes all generated text into delta events. Complete
                # assistant/final events repeat that text and must not be charged again.
                text = event.get("text") if isinstance(event, dict) and event.get("type") == "delta" else None
                if isinstance(text, str) and text:
                    updated = self.store.consume_usage(
                        claim, output_chars=len(text), limits=usage_limits,
                        stage="model_output")
                    state["output_chars"] = updated["output_chars"]
            if state["pending_publication"] is None:
                raise RuntimeError("verifier ended without strict finalization")
            # Publication is the final side effect: the provider must first terminate and
            # every queued terminal event must fit the durable cumulative budgets.
            state["final"] = self.store.finalize(claim, state["pending_publication"])
            return state["final"]
        finally:
            self.store.end_usage(claim)
