"""Boundary tests: rejected values never need a database or model call."""
from dataclasses import replace
import unittest

from workbench.author_execution import candidate_submission
from workbench.candidate_contracts import ArtifactRef, AuthorInputs, CandidateContractError, CatalogEntry
from workbench.execution_scope import ExecutionScope
from workbench.publication_contracts import CanonicalPublication
from workbench.report_publications import prepare
from workbench.research_records import freeze_object
from workbench.verification_store import RevisedCandidate


def inputs():
    return {"claim": {}, "evidence": {"e-1": {"evidence_id": "e-1"}},
            "passage": {}, "calculation": {}, "query": {}}


def submission():
    publication = prepare(title="Boundary test", blocks=[{
        "classification": "evidence_gap", "parts": [{"kind": "text", "text": "Limited evidence."}],
        "support": [], "reasoning": "", "limitations": ["Controlled test only."],
    }], refs={}, facts={}, calculations={}, max_chars=10000)
    return candidate_submission(
        question="A research question", scope=ExecutionScope.create(["MSFT"]),
        input_snapshot=freeze_object({"evidence": {}}, "snapshot"), base_message_id=None,
        publication=CanonicalPublication.decode(publication), refs=AuthorInputs.decode(inputs()),
        request_identity="candidate-1")


class CandidateContractTests(unittest.TestCase):
    def test_real_producer_builds_typed_candidate_and_exact_reference_hashes(self):
        candidate = submission()
        self.assertEqual(candidate.author_inputs.to_json(), inputs())
        entry = candidate.author_inputs.evidence[0]
        self.assertEqual(candidate.evidence_refs, (entry.reference("evidence"),))
        self.assertEqual(candidate.publication.blocks[0].classification, "evidence_gap")
        self.assertEqual(candidate.scope.company_ids, ("MSFT",))

    def test_candidate_rejects_untyped_values_at_construction(self):
        candidate = submission()
        for name, invalid in (
            ("scope", {"company_ids": ["MSFT"]}), ("input_snapshot", {}),
            ("publication", candidate.publication.to_json()), ("author_inputs", inputs()),
            ("evidence_refs", list(candidate.evidence_refs)), ("max_attempts", True),
            ("deadline_at", float("nan")), ("question", None),
        ):
            with self.subTest(field=name), self.assertRaises(CandidateContractError):
                replace(candidate, **{name: invalid})

    def test_reference_kind_duplicate_and_hash_mismatch_fail_before_storage(self):
        candidate = submission()
        reference = candidate.evidence_refs[0]
        for refs in ((reference, reference), (ArtifactRef("calculation", "e-1", reference.sha256),),
                     (ArtifactRef("evidence", "e-1", "0" * 64),)):
            with self.subTest(refs=refs), self.assertRaises(CandidateContractError):
                replace(candidate, evidence_refs=refs)
        with self.assertRaises(CandidateContractError):
            ArtifactRef("evidence", "e-1", "G" * 64)

    def test_author_catalog_missing_unknown_scalar_and_mutability_are_rejected(self):
        malformed = [inputs(), inputs(), inputs()]
        del malformed[0]["query"]
        malformed[1]["legacy"] = {}
        malformed[2]["evidence"] = {"e-1": 1}
        for value in malformed:
            with self.subTest(value=value), self.assertRaises(ValueError):
                AuthorInputs.decode(value)
        with self.assertRaises(CandidateContractError):
            AuthorInputs([], (), (), (), ())
        with self.assertRaises(CandidateContractError):
            CatalogEntry("e-1", {})

    def test_decoded_candidate_cannot_change_through_original_or_json_projection(self):
        raw = inputs()
        catalog = AuthorInputs.decode(raw)
        raw["evidence"]["e-1"]["evidence_id"] = "changed"
        projected = catalog.to_json()
        projected["evidence"]["e-1"]["evidence_id"] = "also changed"
        self.assertEqual(catalog.to_json(), inputs())

    def test_revision_uses_same_typed_publication(self):
        candidate = submission()
        revision = RevisedCandidate("candidate", "version", candidate.publication, (), "Checked wording.")
        self.assertIs(revision.publication, candidate.publication)
        with self.assertRaises(TypeError):
            RevisedCandidate("candidate", "version", candidate.publication.to_json(), (), "Changed.")


if __name__ == "__main__":
    unittest.main()
