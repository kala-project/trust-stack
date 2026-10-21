"""Controlled verification-tool tests; not financial or user acceptance."""
from copy import deepcopy
from fractions import Fraction
import unittest
from scripts.check_report_arithmetic import verify


def calculation(key, operation, values, expected, display, rounded=False):
    facts = [{'fact_id': f'{key}-f{i}', 'value': str(value)} for i, value in enumerate(values)]
    return {'status': 'recorded', 'calculation_id': key, 'operation': operation,
            'value': display, 'inputs': facts, 'arithmetic': {
                'operands': [{'kind': 'fact', 'reference': f['fact_id'], 'value': f['value']} for f in facts],
                'exact_value': {'numerator': str(expected.numerator), 'denominator': str(expected.denominator)},
                'rounding': {'applied': rounded, 'decimal_places': 2}}}


class ReportArithmeticCheckTests(unittest.TestCase):
    def test_operations_and_negative_baseline(self):
        cases = [('sum', [2, 3, 4], 9), ('difference', [2, 3], -1),
                 ('ratio', [5, 2], Fraction(5, 2)), ('percent_ratio', [1, 4], 25),
                 ('percent_change', [-2, -4], 50), ('percentage_point_difference', [20, 15], 5)]
        for i, (operation, values, expected) in enumerate(cases):
            with self.subTest(operation=operation):
                display = '2.5' if operation == 'ratio' else str(expected)
                result = verify({'calculations': [calculation(str(i), operation, values, Fraction(expected), display)]})
                self.assertEqual(result['status'], 'passed')
                self.assertFalse(result['source_values_independently_verified'])

    def test_dependency_uses_exact_value_not_rounded_display(self):
        left = calculation('left', 'percent_ratio', [1, 3], Fraction(100, 3), '33.33', True)
        right = calculation('right', 'percent_ratio', [1, 6], Fraction(50, 3), '16.67', True)
        gap = calculation('gap', 'percentage_point_difference', [], Fraction(50, 3), '16.67', True)
        gap['arithmetic']['operands'] = [{'kind': 'calculation', 'reference': c['calculation_id'], 'value': c['value']} for c in [left, right]]
        self.assertEqual(verify({'calculations': [gap, left, right]})['calculation_count'], 3)
        gap['value'] = '16.66'
        with self.assertRaisesRegex(ValueError, 'rounding mismatch'):
            verify({'calculations': [gap, left, right]})

    def test_corruption_and_unsupported_fail(self):
        original = calculation('a', 'sum', [1, 2], Fraction(3), '3')
        for mutate in [lambda c: c.update(operation='eval'),
                       lambda c: c['arithmetic']['exact_value'].update(numerator='4'),
                       lambda c: c['arithmetic']['operands'][0].update(value='2')]:
            damaged = deepcopy(original); mutate(damaged)
            with self.assertRaises(ValueError): verify({'calculations': [damaged]})
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            verify({'calculations': [original, original]})
        self.assertEqual(verify({})['status'], 'no_recorded_calculations')

    def test_legacy_ordered_facts_contract_is_explicit_and_never_invents_trace(self):
        for operation, values, display in [('percent_change', [4, 3], '33.33'),
                                            ('percent_change', [-2, -4], '50.00'),
                                            ('ratio', [5, 2], '2.5'),
                                            ('difference', [2, 3], '-1'), ('sum', [2, 3], '5')]:
            item = {'schema_version': 'recorded-calculation-v1', 'status': 'recorded',
                    'calculation_id': 'legacy', 'operation': operation, 'value': display,
                    'inputs': [{'fact_id': str(i), 'value': str(v)} for i, v in enumerate(values)]}
            before = deepcopy(item)
            result = verify({'calculations': [item]})
            self.assertEqual(result['status'], 'passed')
            self.assertFalse(result['checks'][0]['recorded_exact_trace_checked'])
            self.assertEqual(item, before)
            item['value'] = '0'
            with self.assertRaisesRegex(ValueError, 'mismatch'):
                verify({'calculations': [item]})
        for changes in ({'schema_version': 'unknown'}, {'operation': 'eval'}, {'inputs': []}):
            bad = {**before, **changes}
            with self.assertRaisesRegex(ValueError, 'Unsupported'):
                verify({'calculations': [bad]})

    def test_cycle_fails(self):
        item = calculation('loop', 'sum', [], Fraction(2), '2')
        item['arithmetic']['operands'] = [{'kind': 'calculation', 'reference': 'loop', 'value': '2'}] * 2
        with self.assertRaisesRegex(ValueError, 'cycle'):
            verify({'calculations': [item]})
