"""Identity-rich evidence details must not change legacy frozen reader projection."""
from copy import deepcopy
import unittest
from workbench.report_reader import _source_summary


class EvidenceIdentityTests(unittest.TestCase):
    def test_details_preserve_fact_identity_without_changing_legacy_projection(self):
        source = {
            'inputs': [{'fact_id': 'f1', 'evidence_id': 'e1', 'metric': 'Revenue', 'value': '10'}],
            'sources': [{'fact': {'fact_id': 'f1', 'evidence_id': 'e1'},
                         'source_id': 'doc', 'quote': '10'}],
        }
        before = deepcopy(source)
        legacy = _source_summary(source)
        self.assertNotIn('fact_id', legacy['inputs'][0])
        self.assertNotIn('fact_id', legacy['sources'][0])
        detail = _source_summary(source, include_identity=True)
        self.assertEqual(detail['sources'][0]['fact_id'], detail['inputs'][0]['fact_id'])
        self.assertEqual(detail['sources'][0]['evidence_id'], detail['inputs'][0]['evidence_id'])
        self.assertEqual(source, before)
        self.assertEqual(_source_summary(source), legacy)

    def test_missing_identity_is_not_invented_from_array_position(self):
        source = {'inputs': [{'fact_id': 'f1'}], 'sources': [{'quote': 'unbound'}]}
        detail = _source_summary(source, include_identity=True)
        self.assertNotIn('fact_id', detail['sources'][0])
