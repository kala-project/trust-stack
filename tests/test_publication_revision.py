"""Pure canonical publication revision tests; no model or durable store is used."""
from copy import deepcopy
import unittest

from workbench.publication_revision import patch_publication
from workbench.report_publications import prepare
from workbench.tool_errors import ToolError


class PublicationRevisionTests(unittest.TestCase):
    def setUp(self):
        self.refs = {"evidence": {"ev-1": {"evidence_id": "ev-1"}},
                     "claim": {}, "passage": {}, "query": {}, "calculation": {}}
        self.facts = {"fact-1": {
            "fact_id": "fact-1", "value": "12", "unit": "count",
            "period_start": "2025-01-01", "period_end": "2025-12-31",
            "period_kind": "year", "basis": "recorded", "evidence_id": "ev-1",
        }}
        self.blocks = [self.block("Alpha ", fact=True), self.block("Bravo"), self.block("Charlie")]
        self.datasets = [{
            "title": "Values", "description": "Recorded values", "display": "table",
            "points": [{"label": "A", "kind": "fact", "id": "fact-1"}],
        }]
        self.publication = prepare(
            title="Original", blocks=self.blocks, datasets=self.datasets, refs=self.refs,
            facts=self.facts, calculations={}, max_chars=10000)

    @staticmethod
    def block(text, *, fact=False):
        parts = [{"kind": "text", "text": text}]
        if fact:
            parts.append({"kind": "fact", "id": "fact-1"})
        return {"classification": "source_statement", "parts": parts,
                "support": [], "reasoning": "", "limitations": []}

    def patch(self, replacements, **kwargs):
        return patch_publication(
            publication=self.publication, replacements=replacements, refs=self.refs,
            facts=self.facts, calculations={}, max_chars=10000, **kwargs)

    def assert_spans_match(self, publication):
        for block in publication["blocks"]:
            self.assertEqual(publication["answer"][block["start"]:block["end"]], block["text"])
            for span in block["spans"]:
                self.assertEqual(publication["answer"][span["start"]:span["end"]], span["text"])

    def test_longer_shorter_and_title_patch_translate_all_spans(self):
        for text in ("A considerably longer corrected paragraph", "X"):
            with self.subTest(text=text):
                revised = self.patch([{"block_id": "report-block-2", "block": self.block(text)}],
                                     title="A changed title")
                self.assertEqual(revised["title"], "A changed title")
                self.assertEqual(revised["blocks"][1]["text"], text)
                self.assert_spans_match(revised)

    def test_multiblock_patch_preserves_omitted_text_bindings_and_datasets_exactly(self):
        before = deepcopy(self.publication)
        revised = self.patch([
            {"block_id": "report-block-1", "block": self.block("Corrected first ", fact=True)},
            {"block_id": "report-block-3", "block": self.block("Corrected third")},
        ])
        self.assertEqual(revised["blocks"][1]["text"], before["blocks"][1]["text"])
        self.assertEqual(revised["blocks"][1]["support"], before["blocks"][1]["support"])
        self.assertEqual(revised["blocks"][1]["spans"][0]["binding"],
                         before["blocks"][1]["spans"][0]["binding"])
        self.assertEqual(revised["datasets"], before["datasets"])
        self.assertEqual(revised["blocks"][-1]["text"], before["blocks"][-1]["text"])
        self.assertEqual(revised["blocks"][-1]["support"], before["blocks"][-1]["support"])
        self.assertEqual([span["binding"] for span in revised["blocks"][-1]["spans"]],
                         [span["binding"] for span in before["blocks"][-1]["spans"]])
        self.assert_spans_match(revised)
        self.assertEqual(self.publication, before)

    def test_unknown_duplicate_and_dataset_generated_block_are_rejected(self):
        with self.assertRaisesRegex(ToolError, "Unknown replacement"):
            self.patch([{"block_id": "missing", "block": self.block("x")}])
        with self.assertRaisesRegex(ToolError, "Duplicate replacement"):
            self.patch([{"block_id": "report-block-1", "block": self.block("x")},
                        {"block_id": "report-block-1", "block": self.block("y")}])
        with self.assertRaisesRegex(ToolError, "use explicit replace mode"):
            self.patch([{"block_id": "report-block-4", "block": self.block("x")}])

    def test_nested_user_data_is_not_mutated(self):
        replacement = {"block_id": "report-block-2", "block": self.block("Nested")}
        replacement["block"]["limitations"] = ["keep me"]
        original_replacement = deepcopy(replacement)
        original_refs = deepcopy(self.refs)
        original_facts = deepcopy(self.facts)
        self.patch([replacement])
        self.assertEqual(replacement, original_replacement)
        self.assertEqual(self.refs, original_refs)
        self.assertEqual(self.facts, original_facts)


if __name__ == "__main__":
    unittest.main()
