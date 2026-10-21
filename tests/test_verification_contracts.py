"""Construction failures need neither a model nor a database."""
from dataclasses import replace
import unittest

from tests.test_candidate_contracts import submission
from workbench.candidate_contracts import ArtifactRef
from workbench.verification_contracts import (
    CheckCoverage, PreparedPublication, ReceiptIdentity, RequiredFinding, SemanticReview,
    VerificationContractError, VerificationFinding, VerificationMetadata, VerifiedCitation,
    decode_limitations, validate_final_limits, verification_metadata_schema,
)


class VerificationContractTests(unittest.TestCase):
    def prepared(self):
        return PreparedPublication(
            "candidate", "version", "a" * 64, "verified", submission().publication,
            SemanticReview("The original question was assessed."), (), VerificationMetadata(),
            CheckCoverage(("n1",), ("n1",), ()), (),
            (RequiredFinding("n1", "semantic_support", "supported", "Checked text.", ()),))

    def test_prepared_requires_domain_values_not_old_dicts(self):
        prepared = self.prepared()
        for name, invalid in (
            ("semantic_review", {"original_question_coverage": "old dict"}),
            ("publication", prepared.publication.to_json()), ("verification", {}),
            ("citations", ({},)), ("citations", []), ("coverage", {}),
            ("required_verifier_findings", ({},)), ("limitations", []),
            ("outcome", "looks_verified"), ("input_hash", True),
        ):
            with self.subTest(field=name), self.assertRaises(ValueError):
                replace(prepared, **{name: invalid})

    def test_coverage_has_no_duplicate_missing_overlapping_or_foreign_nodes(self):
        for required, checked, unresolved in (
            (("n", "n"), ("n",), ()), (("n",), (), ()),
            (("n",), ("n",), ("n",)), (("n",), ("foreign",), ()),
            ([], (), ()), ((), (), ()), ((True,), (True,), ()),
        ):
            with self.subTest(required=required, checked=checked, unresolved=unresolved):
                with self.assertRaises(VerificationContractError):
                    CheckCoverage(required, checked, unresolved)
        self.assertEqual(CheckCoverage(("n1", "n2"), ("n1",), ("n2",)).unresolved, ("n2",))

    def test_outcome_limitations_and_coverage_cannot_contradict(self):
        prepared = self.prepared()
        for changes in (
            {"outcome": "unresolved"}, {"outcome": "verified_with_limitations"},
            {"limitations": ("Not fully checked.",)},
            {"coverage": CheckCoverage(("n1",), (), ("n1",)), "required_verifier_findings": ()},
        ):
            with self.subTest(changes=changes), self.assertRaises(VerificationContractError):
                replace(prepared, **changes)
        result = replace(prepared, outcome="unresolved", coverage=CheckCoverage(("n1",), (), ("n1",)),
                         required_verifier_findings=())
        self.assertEqual(result.outcome, "unresolved")

    def test_model_metadata_is_closed_and_cannot_claim_runner_owned_facts(self):
        for value in ({"status": "verified"}, {"publication_schema": "forged"},
                      {"verification_candidate_id": "other"},
                      {"deterministic_calculation_limitations": []},
                      {"semantic_truth_guaranteed": True}, {"semantic_truth_guaranteed": 0},
                      {"semantic_truth_guaranteed": None}, {"method": []}, {"method": " "}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                VerificationMetadata.decode_model(value)
        metadata = VerificationMetadata.decode_model({"method": "Exact replay", "semantic_truth_guaranteed": False})
        self.assertEqual(metadata.to_json(), {"method": "Exact replay", "semantic_truth_guaranteed": False})
        self.assertEqual(VerificationMetadata.decode_model({}).to_json(), {})
        self.assertNotIn("deterministic_calculation_limitations", verification_metadata_schema()["properties"])
        runner_metadata = metadata.with_calculation_limitations(("Excluded a bad calculation.",))
        self.assertEqual(runner_metadata.to_json()["deterministic_calculation_limitations"], ["Excluded a bad calculation."])
        self.assertNotIn("deterministic_calculation_limitations", metadata.to_json())

    def test_semantic_review_and_limitations_reject_unknown_fields_and_wrong_shapes(self):
        for value in ({}, {"status": "assessed"}, {"original_question_coverage": "ok", "scratchpad": "x"},
                      {"original_question_coverage": True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SemanticReview.decode(value)
        for value in ("words", ("tuple-not-json",), None, [False], ["same", "same"], [" "]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_limitations(value)
        self.assertEqual(decode_limitations(["Unanswered period."]), ("Unanswered period.",))
        with self.assertRaises(VerificationContractError):
            validate_final_limits("verified_with_limitations", ())

    def test_required_finding_is_immutable_terminal_and_canonical(self):
        receipt = ReceiptIdentity("r1", "a" * 64)
        finding = RequiredFinding("n1", "source_identity", "supported", "Compared originals.", (receipt,))
        raw = finding.to_json()
        self.assertEqual(RequiredFinding.decode(raw), finding)
        raw["receipts"][0]["sha256"] = "b" * 64
        self.assertEqual(finding.receipts[0].sha256, "a" * 64)
        for changes in ({"outcome": "unchecked"}, {"receipts": (receipt, receipt)},
                        {"receipts": [receipt]}, {"explanation": ""}, {"check_kind": False},
                        {"check_kind": "invented_check"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(finding, **changes)
        with self.assertRaises(ValueError):
            RequiredFinding.decode({**finding.to_json(), "legacy": True})

    def test_verified_citation_requires_precise_identity_and_receipt_kind(self):
        citation = VerifiedCitation("evidence", "e1", ArtifactRef("verification_tool_result", "r1", "a" * 64),
                                    company_id="MSFT", source_sha256="b" * 64)
        self.assertEqual(VerifiedCitation.decode(citation.to_json()), citation)
        for changes in ({"kind": "unknown"}, {"valid": False}, {"valid": 1}, {"company_id": None},
                        {"source_sha256": "bad"}, {"legacy": {}}, {"verification_receipt": {}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                VerifiedCitation.decode({**citation.to_json(), **changes})
        with self.assertRaises(VerificationContractError):
            replace(citation, verification_receipt=ArtifactRef("tool_result", "r1", "a" * 64))

    def test_finding_rejects_bad_outcome_hash_and_mutable_refs_at_construction(self):
        finding = VerificationFinding("c", "v", "n", "a" * 64, "b" * 64,
                                      "semantic_support", "supported", "Checked.")
        for changes in ({"outcome": "approved"}, {"node_hash": "bad"}, {"check_kind": "custom_check"},
                        {"tool_result_refs": []},
                        {"tool_result_refs": ({},)}, {"explanation": False}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(finding, **changes)


if __name__ == "__main__":
    unittest.main()
