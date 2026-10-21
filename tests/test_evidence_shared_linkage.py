from copy import deepcopy
import unittest

from workbench.chat import ChatService


class SharedEvidenceLinkageTests(unittest.TestCase):
    def setUp(self):
        self.evidence_id = "ev-shared"
        self.company_id = "MSFT"
        self.item = {
            "evidence_id": self.evidence_id,
            "source_id": "source-1",
            "source_sha256": "a" * 64,
            "quote": "Revenue was 100.",
            "locator": {"start": 12, "end": 28, "table_index": 1, "row_index": 2},
            "claim_id": "claim-current-selection",
        }
        self.frozen_item = {
            **self.item,
            "claim_id": "claim-frozen-selection",
        }
        self.source = {
            "source_id": "source-1",
            "sha256": "a" * 64,
            "title": "Frozen filing",
            "url": "https://example.test/filing",
        }
        claims = [
            {
                "claim_id": "claim-current-selection",
                "company_id": self.company_id,
                "evidence_ids": [self.evidence_id],
            },
            {
                "claim_id": "claim-frozen-selection",
                "company_id": self.company_id,
                "evidence_ids": [self.evidence_id],
            },
        ]
        chains = [
            {"claim_id": claim["claim_id"], "evidence_ids": [self.evidence_id]}
            for claim in claims
        ]
        self.report = {"claims": claims, "evidence_chains": chains}
        self.frozen = deepcopy(self.report)
        self.citation = {
            "kind": "evidence",
            "id": self.evidence_id,
            "valid": True,
            "claim_id": "claim-current-selection",
            "source_id": "source-1",
        }
        self.original = {
            **self.item,
            "claim_id": "claim-frozen-selection",
            "company_id": self.company_id,
        }

    def matches(self, **changes):
        values = {
            "item": deepcopy(self.item),
            "frozen_item": deepcopy(self.frozen_item),
            "source": deepcopy(self.source),
            "frozen_source": deepcopy(self.source),
            "report": deepcopy(self.report),
            "frozen": deepcopy(self.frozen),
            "company_id": self.company_id,
            "citation": deepcopy(self.citation),
            "original": deepcopy(self.original),
        }
        values.update(changes)
        return ChatService._evidence_linkage_matches(**values)

    def test_shared_evidence_accepts_different_selected_claims_from_same_frozen_set(self):
        self.assertTrue(self.matches())
        self.assertEqual(
            ChatService._evidence_claim_membership(self.frozen, self.evidence_id),
            {"claim-current-selection", "claim-frozen-selection"},
        )

    def test_tampered_membership_is_rejected(self):
        report = deepcopy(self.report)
        report["claims"][1]["evidence_ids"] = []
        report["evidence_chains"][1]["evidence_ids"] = []
        self.assertFalse(self.matches(report=report))

        citation = deepcopy(self.citation)
        citation["claim_id"] = "claim-not-an-owner"
        self.assertFalse(self.matches(citation=citation))

    def test_tampered_locator_hash_quote_company_or_current_source_is_rejected(self):
        mutations = {}
        for field, value in (
            ("locator", {"start": 13, "end": 28, "table_index": 1, "row_index": 2}),
            ("source_sha256", "b" * 64),
            ("quote", "Revenue was 101."),
        ):
            item = deepcopy(self.item)
            item[field] = value
            mutations[field] = {"item": item}

        frozen = deepcopy(self.frozen)
        frozen["claims"][0]["company_id"] = "AAPL"
        mutations["company"] = {"frozen": frozen}

        current_source = deepcopy(self.source)
        current_source["title"] = "A newer source"
        mutations["current_source"] = {"source": current_source}

        for name, change in mutations.items():
            with self.subTest(name=name):
                self.assertFalse(self.matches(**change))

    def test_evidence_absent_from_snapshot_is_rejected(self):
        self.assertFalse(self.matches(frozen_item=None))
        frozen = {"claims": [], "evidence_chains": []}
        self.assertFalse(self.matches(frozen=frozen))


if __name__ == "__main__":
    unittest.main()
