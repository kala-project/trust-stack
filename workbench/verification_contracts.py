"""Strict verifier-to-publication contracts, independent of SQL and model execution."""
from __future__ import annotations

from dataclasses import dataclass, replace
import re
from typing import Literal, TypeVar

from .candidate_contracts import ArtifactRef
from .publication_contracts import CanonicalPublication, Reference, ReferenceKind
from .research_records import JsonObject, freeze_object, thaw_object

FindingOutcome = Literal["supported", "corrected_and_rechecked", "conflict_disclosed", "insufficient_material_disclosed", "unchecked", "unresolved"]
TerminalOutcome = Literal["supported", "corrected_and_rechecked", "conflict_disclosed", "insufficient_material_disclosed"]
FinalOutcome = Literal["verified", "verified_with_limitations", "unresolved"]
CheckKind = Literal["source_identity", "temporal_alignment", "numeric_reproducibility", "semantic_support", "limitation_disclosure"]
FINDING_OUTCOMES: tuple[FindingOutcome, ...] = ("supported", "corrected_and_rechecked", "conflict_disclosed", "insufficient_material_disclosed", "unchecked", "unresolved")
TERMINAL_OUTCOMES: tuple[TerminalOutcome, ...] = ("supported", "corrected_and_rechecked", "conflict_disclosed", "insufficient_material_disclosed")
FINAL_OUTCOMES: tuple[FinalOutcome, ...] = ("verified", "verified_with_limitations", "unresolved")
CHECK_KINDS: tuple[CheckKind, ...] = ("source_identity", "temporal_alignment", "numeric_reproducibility", "semantic_support", "limitation_disclosure")
T = TypeVar("T", bound=str)


class VerificationContractError(ValueError):
    pass


def text(value: object, name: str, maximum: int = 200) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise VerificationContractError(f"{name} must be nonempty text of at most {maximum} characters")
    return value


def digest(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise VerificationContractError("digest must be a lowercase SHA-256")
    return value


def choice(value: object, allowed: tuple[T, ...]) -> T:
    for item in allowed:
        if type(value) is str and value == item:
            return item
    raise VerificationContractError("unsupported verification outcome")


def check_kind(value: object) -> CheckKind:
    for item in CHECK_KINDS:
        if type(value) is str and value == item:
            return item
    raise VerificationContractError("unsupported verification check kind")


def fields(value: object, required: set[str], optional: set[str] | None = None) -> JsonObject:
    data = thaw_object(freeze_object(value, "verification contract"))
    if not required <= data.keys() or data.keys() - required - (optional or set()):
        raise VerificationContractError("verification contract has missing or unknown fields")
    return data


def strings(value: tuple[str, ...], name: str, maximum: int = 10000, length: int = 200) -> None:
    if type(value) is not tuple or len(value) > maximum:
        raise VerificationContractError(f"{name} must be a bounded immutable tuple")
    for item in value:
        text(item, name, length)
    if len(value) != len(set(value)):
        raise VerificationContractError(f"{name} contains duplicate values")


def verification_checks(publication: JsonObject) -> dict[str, dict[str, list[CheckKind]]]:
    """Derive applicable and inapplicable checks from canonical node structure."""
    checks: dict[str, dict[str, list[CheckKind]]] = {}

    def references(node: object) -> bool:
        if not isinstance(node, dict):
            return False
        if node.get("support"):
            return True
        for item in [*node.get("spans", []), *node.get("points", [])]:
            if isinstance(item, dict) and isinstance(item.get("binding"), dict):
                if item["binding"].get("reference"):
                    return True
        return False

    def numeric_binding(value: object) -> bool:
        if isinstance(value, dict):
            if value.get("kind") in {"fact", "calculation", "query_cell"}:
                return True
            return any(numeric_binding(child) for child in value.values())
        if isinstance(value, list):
            return any(numeric_binding(child) for child in value)
        return False

    def add(node_id: str, node: object) -> None:
        required = {"semantic_support"}
        if references(node):
            required.update({"source_identity", "temporal_alignment"})
        if numeric_binding(node):
            required.add("numeric_reproducibility")
        if (isinstance(node, dict)
                and (node.get("classification") == "evidence_gap" or node.get("limitations"))):
            required.add("limitation_disclosure")
        checks[node_id] = {
            "required": [kind for kind in CHECK_KINDS if kind in required],
            "not_applicable": [kind for kind in CHECK_KINDS if kind not in required],
        }

    add("report-title", publication.get("title", ""))
    blocks_value = publication.get("blocks")
    blocks = blocks_value if isinstance(blocks_value, list) else []
    for index, block in enumerate(blocks, 1):
        if not isinstance(block, dict):
            continue
        block_id = block.get("block_id")
        add(block_id if isinstance(block_id, str) and block_id else f"report-block-{index}", block)
    datasets_value = publication.get("datasets")
    datasets = datasets_value if isinstance(datasets_value, list) else []
    for index, dataset in enumerate(datasets, 1):
        add(f"report-dataset-{index}", dataset)
    return checks


def completed_check_nodes(findings: list[JsonObject], checks: dict[str, dict[str, list[CheckKind]]]) -> set[str]:
    """Return nodes whose latest finding is terminal for every applicable check."""
    latest = {(item["node_id"], item["check_kind"]): item for item in findings}
    terminal = {key for key, item in latest.items() if item["outcome"] in TERMINAL_OUTCOMES}
    return {node_id for node_id, projection in checks.items()
            if all((node_id, kind) in terminal for kind in projection["required"])}


def decode_limitations(value: object) -> tuple[str, ...]:
    data = fields({"limitations": value}, {"limitations"})
    items = data["limitations"]
    if type(items) is not list:
        raise VerificationContractError("limitations must be a JSON array")
    result = tuple(text(item, "limitation", 4000) for item in items)
    strings(result, "limitations", maximum=100, length=4000)
    return result


def validate_final_limits(outcome: FinalOutcome, limitations: tuple[str, ...]) -> None:
    choice(outcome, FINAL_OUTCOMES)
    strings(limitations, "limitations", maximum=100, length=4000)
    if outcome == "verified" and limitations:
        raise VerificationContractError("verified-without-limitations cannot carry limitations")
    if outcome == "verified_with_limitations" and not limitations:
        raise VerificationContractError("verified-with-limitations needs explicit limitations")


@dataclass(frozen=True, slots=True)
class SemanticReview:
    original_question_coverage: str

    def __post_init__(self) -> None:
        text(self.original_question_coverage, "original_question_coverage", 4000)

    @classmethod
    def decode(cls, value: object) -> SemanticReview:
        data = fields(value, {"original_question_coverage"})
        return cls(text(data["original_question_coverage"], "original_question_coverage", 4000))

    def to_json(self) -> JsonObject:
        return {"original_question_coverage": self.original_question_coverage}


@dataclass(frozen=True, slots=True)
class VerificationMetadata:
    method: str | None = None
    semantic_truth_guaranteed: Literal[False] | None = None
    deterministic_calculation_limitations: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.method is not None:
            text(self.method, "verification method", 4000)
        if self.semantic_truth_guaranteed is not None and self.semantic_truth_guaranteed is not False:
            raise VerificationContractError("verification cannot guarantee semantic truth")
        if self.deterministic_calculation_limitations is not None:
            strings(self.deterministic_calculation_limitations, "calculation limitations", length=20000)

    @classmethod
    def decode_model(cls, value: object) -> VerificationMetadata:
        # Computation limitations are runner-owned, never supplied by the model.
        data = fields(value, set(), {"method", "semantic_truth_guaranteed"})
        method = text(data["method"], "verification method", 4000) if "method" in data else None
        guaranteed: Literal[False] | None = None
        if "semantic_truth_guaranteed" in data:
            if data["semantic_truth_guaranteed"] is not False:
                raise VerificationContractError("semantic_truth_guaranteed must be false")
            guaranteed = False
        return cls(method, guaranteed)

    def with_calculation_limitations(self, values: tuple[str, ...]) -> VerificationMetadata:
        return replace(self, deterministic_calculation_limitations=values)

    def to_json(self) -> JsonObject:
        result: JsonObject = {}
        if self.method is not None:
            result["method"] = self.method
        if self.semantic_truth_guaranteed is not None:
            result["semantic_truth_guaranteed"] = self.semantic_truth_guaranteed
        if self.deterministic_calculation_limitations is not None:
            result["deterministic_calculation_limitations"] = list(self.deterministic_calculation_limitations)
        return result


def verification_metadata_schema() -> JsonObject:
    return {"type": "object", "additionalProperties": False, "properties": {
        "method": {"type": "string", "minLength": 1, "maxLength": 4000},
        "semantic_truth_guaranteed": {"type": "boolean", "const": False},
    }}


@dataclass(frozen=True, slots=True)
class ReceiptIdentity:
    receipt_id: str
    sha256: str

    def __post_init__(self) -> None:
        text(self.receipt_id, "receipt_id")
        digest(self.sha256)

    @classmethod
    def decode(cls, value: object) -> ReceiptIdentity:
        data = fields(value, {"receipt_id", "sha256"})
        return cls(text(data["receipt_id"], "receipt_id"), digest(data["sha256"]))

    def to_json(self) -> JsonObject:
        return {"receipt_id": self.receipt_id, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class RequiredFinding:
    node_id: str
    check_kind: CheckKind
    outcome: TerminalOutcome
    explanation: str
    receipts: tuple[ReceiptIdentity, ...]

    def __post_init__(self) -> None:
        text(self.node_id, "node_id")
        check_kind(self.check_kind)
        choice(self.outcome, TERMINAL_OUTCOMES)
        text(self.explanation, "explanation", 20000)
        if type(self.receipts) is not tuple or any(type(item) is not ReceiptIdentity for item in self.receipts):
            raise VerificationContractError("receipts must be immutable ReceiptIdentity values")
        keys = [(item.receipt_id, item.sha256) for item in self.receipts]
        if keys != sorted(set(keys)):
            raise VerificationContractError("receipt identities must be unique and ordered")

    @classmethod
    def decode(cls, value: object) -> RequiredFinding:
        data = fields(value, {"node_id", "check_kind", "outcome", "explanation", "receipts"})
        receipts = data["receipts"]
        if type(receipts) is not list:
            raise VerificationContractError("receipts must be a JSON array")
        return cls(text(data["node_id"], "node_id"), check_kind(data["check_kind"]),
                   choice(data["outcome"], TERMINAL_OUTCOMES), text(data["explanation"], "explanation", 20000),
                   tuple(ReceiptIdentity.decode(item) for item in receipts))

    def to_json(self) -> JsonObject:
        return {"node_id": self.node_id, "check_kind": self.check_kind, "outcome": self.outcome,
                "explanation": self.explanation, "receipts": [item.to_json() for item in self.receipts]}


@dataclass(frozen=True, slots=True)
class VerifiedCitation:
    kind: ReferenceKind
    id: str
    verification_receipt: ArtifactRef
    company_id: str | None = None
    document_version_id: str | None = None
    source_sha256: str | None = None
    parser_version: str | None = None
    title: str | None = None
    source_url: str | None = None
    published_at: str | None = None
    collected_at: str | None = None
    rights_status: str | None = None

    def __post_init__(self) -> None:
        Reference(self.kind, self.id)
        text(self.id, "citation id")
        if (type(self.verification_receipt) is not ArtifactRef
                or self.verification_receipt.kind != "verification_tool_result"):
            raise VerificationContractError("citation requires a verifier tool receipt")
        for key, value in self.metadata():
            if value is not None:
                text(value, key, 20000)
        if self.source_sha256 is not None:
            digest(self.source_sha256)

    def metadata(self) -> tuple[tuple[str, str | None], ...]:
        return (("company_id", self.company_id), ("document_version_id", self.document_version_id),
                ("source_sha256", self.source_sha256), ("parser_version", self.parser_version),
                ("title", self.title), ("source_url", self.source_url), ("published_at", self.published_at),
                ("collected_at", self.collected_at), ("rights_status", self.rights_status))

    @classmethod
    def decode(cls, value: object) -> VerifiedCitation:
        names = ("company_id", "document_version_id", "source_sha256", "parser_version", "title",
                 "source_url", "published_at", "collected_at", "rights_status")
        data = fields(value, {"kind", "id", "verification_receipt", "valid"}, set(names))
        if data["valid"] is not True:
            raise VerificationContractError("published citation must be verified")
        reference = Reference.decode({"kind": data["kind"], "id": data["id"]})
        receipt = fields(data["verification_receipt"], {"kind", "identifier", "sha256"})
        values = [text(data[name], name, 20000) if name in data else None for name in names]
        return cls(reference.kind, reference.id, ArtifactRef(
            text(receipt["kind"], "receipt kind"), text(receipt["identifier"], "receipt identifier"),
            digest(receipt["sha256"])), *values)

    def to_json(self) -> JsonObject:
        ref = self.verification_receipt
        result: JsonObject = {"kind": self.kind, "id": self.id, "valid": True,
                              "verification_receipt": {"kind": ref.kind, "identifier": ref.identifier,
                                                       "sha256": ref.sha256}}
        result.update({key: value for key, value in self.metadata() if value is not None})
        return result


@dataclass(frozen=True, slots=True)
class CheckCoverage:
    required: tuple[str, ...]
    checked: tuple[str, ...]
    unresolved: tuple[str, ...]

    def __post_init__(self) -> None:
        for name, values in (("required", self.required), ("checked", self.checked), ("unresolved", self.unresolved)):
            strings(values, name)
        if not self.required or set(self.checked) & set(self.unresolved):
            raise VerificationContractError("check coverage must have required nodes and disjoint outcomes")
        if set(self.required) != set(self.checked) | set(self.unresolved):
            raise VerificationContractError("checked and unresolved must partition the required nodes")


@dataclass(frozen=True, slots=True)
class VerificationFinding:
    candidate_id: str
    version_id: str
    node_id: str
    node_hash: str
    dependency_hash: str
    check_kind: CheckKind
    outcome: FindingOutcome
    explanation: str
    tool_result_refs: tuple[ArtifactRef, ...] = ()

    def __post_init__(self) -> None:
        for name, value in (("candidate_id", self.candidate_id), ("version_id", self.version_id),
                            ("node_id", self.node_id)):
            text(value, name)
        check_kind(self.check_kind)
        digest(self.node_hash)
        digest(self.dependency_hash)
        choice(self.outcome, FINDING_OUTCOMES)
        text(self.explanation, "explanation", 20000)
        if type(self.tool_result_refs) is not tuple or any(type(ref) is not ArtifactRef for ref in self.tool_result_refs):
            raise VerificationContractError("finding references must be immutable ArtifactRef values")
        keys = [(ref.kind, ref.identifier) for ref in self.tool_result_refs]
        if len(set(keys)) != len(keys):
            raise VerificationContractError("finding references cannot repeat")


@dataclass(frozen=True, slots=True)
class PreparedPublication:
    candidate_id: str
    version_id: str
    input_hash: str
    outcome: FinalOutcome
    publication: CanonicalPublication
    semantic_review: SemanticReview
    citations: tuple[VerifiedCitation, ...]
    verification: VerificationMetadata
    coverage: CheckCoverage
    limitations: tuple[str, ...] = ()
    required_verifier_findings: tuple[RequiredFinding, ...] = ()

    def __post_init__(self) -> None:
        text(self.candidate_id, "candidate_id")
        text(self.version_id, "version_id")
        digest(self.input_hash)
        choice(self.outcome, FINAL_OUTCOMES)
        for value, expected in ((self.publication, CanonicalPublication), (self.semantic_review, SemanticReview),
                                (self.verification, VerificationMetadata), (self.coverage, CheckCoverage)):
            if type(value) is not expected:
                raise VerificationContractError("publication input requires explicit domain types")
        if type(self.citations) is not tuple or len(self.citations) > 10000 or any(type(c) is not VerifiedCitation for c in self.citations):
            raise VerificationContractError("citations must be an immutable VerifiedCitation tuple")
        keys = [(c.kind, c.id) for c in self.citations]
        if len(keys) != len(set(keys)):
            raise VerificationContractError("citations cannot repeat")
        validate_final_limits(self.outcome, self.limitations)
        if type(self.required_verifier_findings) is not tuple or any(type(f) is not RequiredFinding for f in self.required_verifier_findings):
            raise VerificationContractError("required findings must be immutable RequiredFinding values")
        finding_keys = [(f.node_id, f.check_kind) for f in self.required_verifier_findings]
        if finding_keys != sorted(set(finding_keys)):
            raise VerificationContractError("required findings must have unique ordered identities")
        if any(f.node_id not in self.coverage.checked for f in self.required_verifier_findings):
            raise VerificationContractError("required findings refer to unchecked nodes")
        if self.outcome == "unresolved":
            if not self.coverage.unresolved:
                raise VerificationContractError("unresolved publication needs unresolved nodes")
        elif self.coverage.unresolved:
            raise VerificationContractError("unresolved checks cannot be labeled verified")
