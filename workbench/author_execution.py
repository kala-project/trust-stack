"""Author-side durable candidate boundary; no verifier or publication decisions."""
from __future__ import annotations

from .candidate_contracts import AuthorInputs, CandidateSubmission
from .execution_scope import ExecutionScope
from .publication_contracts import CanonicalPublication
from .research_records import FrozenObject


def candidate_submission(*, question: str, scope: ExecutionScope, input_snapshot: FrozenObject,
                         base_message_id: str | None, publication: CanonicalPublication,
                         refs: AuthorInputs, request_identity: str,
                         max_attempts: int = 3, deadline_at: float | None = None) -> CandidateSubmission:
    """Accept only decoded domain values; wrong JSON cannot reach durable candidate storage."""
    if type(refs) is not AuthorInputs:
        raise TypeError("refs must be AuthorInputs")
    return CandidateSubmission(
        question=question, scope=scope, input_snapshot=input_snapshot,
        base_message_id=base_message_id, publication=publication,
        evidence_refs=refs.references(calculations=False),
        calculation_refs=refs.references(calculations=True),
        request_identity=request_identity, max_attempts=max_attempts,
        deadline_at=deadline_at, author_inputs=refs,
    )
