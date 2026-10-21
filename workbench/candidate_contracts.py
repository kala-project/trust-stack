"""Typed author-to-verifier boundary. JSON is decoded before a candidate exists."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import re

from .execution_scope import ExecutionScope
from .publication_contracts import CanonicalPublication
from .research_records import FrozenObject, JsonObject, canonical_json, freeze_object, thaw_object


class CandidateContractError(ValueError):
    pass


def _text(value: object, field: str, maximum: int = 200) -> str:
    if type(value) is not str or not value.strip() or len(value) > maximum:
        raise CandidateContractError(f"{field} must be bounded nonempty text")
    return value


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    kind: str
    identifier: str
    sha256: str

    def __post_init__(self) -> None:
        _text(self.kind, "reference.kind")
        _text(self.identifier, "reference.identifier")
        if type(self.sha256) is not str or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None:
            raise CandidateContractError("reference.sha256 must be a lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    identifier: str
    # Tool-specific descriptor/result bytes; scope, kind and identity belong to the catalog.
    record: FrozenObject

    def __post_init__(self) -> None:
        _text(self.identifier, "catalog identifier")
        if type(self.record) is not FrozenObject:
            raise CandidateContractError("catalog record must be an immutable JSON object")

    def reference(self, kind: str) -> ArtifactRef:
        raw = canonical_json(thaw_object(self.record), "catalog record")
        return ArtifactRef(kind, self.identifier, hashlib.sha256(raw.encode()).hexdigest())


def _entries(value: object, field: str) -> tuple[CatalogEntry, ...]:
    frozen = freeze_object(value, field)
    entries = []
    for identifier, record in frozen.items:
        if type(record) is not FrozenObject:
            raise CandidateContractError(f"{field} values must be objects")
        entries.append(CatalogEntry(identifier, record))
    return tuple(entries)


@dataclass(frozen=True, slots=True)
class AuthorInputs:
    claim: tuple[CatalogEntry, ...]
    evidence: tuple[CatalogEntry, ...]
    passage: tuple[CatalogEntry, ...]
    calculation: tuple[CatalogEntry, ...]
    query: tuple[CatalogEntry, ...]
    fact: tuple[CatalogEntry, ...] | None = None

    def __post_init__(self) -> None:
        for kind, entries in self.catalogs():
            if type(entries) is not tuple or any(type(entry) is not CatalogEntry for entry in entries):
                raise CandidateContractError(f"{kind} must contain immutable CatalogEntry values")
            identifiers = [entry.identifier for entry in entries]
            if identifiers != sorted(set(identifiers)):
                raise CandidateContractError(f"{kind} identifiers must be unique and ordered")
        if sum(len(entries) for _, entries in self.catalogs()) > 10_000:
            raise CandidateContractError("author input catalog is too large")

    def catalogs(self) -> tuple[tuple[str, tuple[CatalogEntry, ...]], ...]:
        catalogs = (("claim", self.claim), ("evidence", self.evidence),
                    ("passage", self.passage), ("calculation", self.calculation), ("query", self.query))
        return catalogs + (("fact", self.fact),) if self.fact is not None else catalogs

    @classmethod
    def decode(cls, value: object) -> AuthorInputs:
        mapping = thaw_object(freeze_object(value, "author_inputs"))
        required = {"claim", "evidence", "passage", "calculation", "query"}
        if not required <= mapping.keys() or mapping.keys() - required - {"fact"}:
            raise CandidateContractError("author_inputs requires the five explicit reference catalogs")
        return cls(_entries(mapping["claim"], "claim"), _entries(mapping["evidence"], "evidence"),
                   _entries(mapping["passage"], "passage"), _entries(mapping["calculation"], "calculation"),
                   _entries(mapping["query"], "query"),
                   _entries(mapping["fact"], "fact") if "fact" in mapping else None)

    def to_json(self) -> JsonObject:
        return {kind: {entry.identifier: thaw_object(entry.record) for entry in entries}
                for kind, entries in self.catalogs()}

    def lookup(self, kind: str, identifier: str) -> FrozenObject | None:
        for catalog_kind, entries in self.catalogs():
            if catalog_kind == kind:
                return next((entry.record for entry in entries if entry.identifier == identifier), None)
        raise CandidateContractError("unknown author input kind")

    def references(self, *, calculations: bool) -> tuple[ArtifactRef, ...]:
        return tuple(entry.reference(kind) for kind, entries in self.catalogs()
                     if (kind == "calculation" if calculations else kind in {"claim", "evidence", "passage", "query"})
                     for entry in entries)


@dataclass(frozen=True, slots=True)
class CandidateSubmission:
    question: str
    scope: ExecutionScope
    input_snapshot: FrozenObject
    base_message_id: str | None
    publication: CanonicalPublication
    evidence_refs: tuple[ArtifactRef, ...]
    calculation_refs: tuple[ArtifactRef, ...]
    request_identity: str
    author_inputs: AuthorInputs
    max_attempts: int = 3
    deadline_at: float | None = None

    def __post_init__(self) -> None:
        _text(self.question, "question", 20_000)
        _text(self.request_identity, "request_identity")
        if self.base_message_id is not None:
            _text(self.base_message_id, "base_message_id")
        if type(self.scope) is not ExecutionScope:
            raise CandidateContractError("scope must be ExecutionScope")
        if type(self.input_snapshot) is not FrozenObject:
            raise CandidateContractError("input_snapshot must be an immutable object")
        if type(self.publication) is not CanonicalPublication:
            raise CandidateContractError("publication must be CanonicalPublication")
        if type(self.author_inputs) is not AuthorInputs:
            raise CandidateContractError("author_inputs must be AuthorInputs")
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 20:
            raise CandidateContractError("max_attempts must be an integer in 1..20")
        if self.deadline_at is not None and (type(self.deadline_at) not in (int, float)
                                           or not math.isfinite(self.deadline_at)):
            raise CandidateContractError("deadline_at must be finite or null")
        for name, refs, kinds in (
            ("evidence_refs", self.evidence_refs, {"claim", "evidence", "passage", "query"}),
            ("calculation_refs", self.calculation_refs, {"calculation"}),
        ):
            if type(refs) is not tuple or any(type(ref) is not ArtifactRef for ref in refs):
                raise CandidateContractError(f"{name} must be immutable ArtifactRef values")
            keys = [(ref.kind, ref.identifier) for ref in refs]
            if len(keys) != len(set(keys)):
                raise CandidateContractError(f"{name} cannot contain duplicates")
            for ref in refs:
                if ref.kind not in kinds:
                    raise CandidateContractError(f"{name} contains the wrong reference kind")
                record = self.author_inputs.lookup(ref.kind, ref.identifier)
                if record is None or CatalogEntry(ref.identifier, record).reference(ref.kind) != ref:
                    raise CandidateContractError(f"{name} differs from its author input")
