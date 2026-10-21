#!/usr/bin/env python3
"""Independently replay frozen report arithmetic; not source or human verification.

No production calculation imports, formula evaluation, company rules, or expected
answers. Unsupported operations fail explicitly. Input is a saved artifact JSON.
"""
import argparse
from decimal import Decimal, ROUND_HALF_UP, localcontext
from fractions import Fraction
import json
from pathlib import Path


def verify(artifact):
    calculations = {c['calculation_id']: c for c in artifact.get('calculations', [])
                    if c.get('status') == 'recorded'}
    if len(calculations) != sum(c.get('status') == 'recorded' for c in artifact.get('calculations', [])):
        raise ValueError('Duplicate calculation identity')
    facts = {}
    for calculation in calculations.values():
        for fact in calculation.get('inputs', []):
            key = fact['fact_id']
            value = Fraction(Decimal(fact['value']))
            if key in facts and facts[key] != value:
                raise ValueError(f'Conflicting values for fact {key}')
            facts[key] = value
    results, visiting, checks = {}, set(), []

    def replay(key):
        if key in results:
            return results[key]
        if key in visiting:
            raise ValueError('Calculation dependency cycle')
        visiting.add(key)
        calculation = calculations[key]
        arithmetic = calculation.get('arithmetic')
        operation = calculation['operation']
        legacy = arithmetic is None
        if legacy:
            if (calculation.get('schema_version') != 'recorded-calculation-v1'
                    or operation not in {'sum', 'difference', 'ratio', 'percent_change'}
                    or len(calculation.get('inputs', [])) != 2):
                raise ValueError('Unsupported calculation without exact arithmetic trace')
            # The legacy contract records ordered facts, not an exact-value trace.
            # Recompute independently; never manufacture a historical trace.
            operands = [{'kind': 'fact', 'reference': f['fact_id'], 'value': f['value']}
                        for f in calculation['inputs']]
        else:
            operands = arithmetic['operands']
        values = []
        for operand in operands:
            if operand['kind'] == 'fact':
                value = facts[operand['reference']]
                if value != Fraction(Decimal(operand['value'])):
                    raise ValueError('Operand differs from frozen fact')
            elif operand['kind'] == 'calculation':
                # Never use the rounded display value as a dependency.
                value = replay(operand['reference'])
                if Decimal(operand['value']) != Decimal(calculations[operand['reference']]['value']):
                    raise ValueError('Dependency display differs from referenced calculation')
            else:
                raise ValueError(f"Unsupported operand kind: {operand['kind']}")
            values.append(value)
        if operation == 'sum' and len(values) >= 2:
            exact = sum(values, Fraction(0))
        elif len(values) == 2:
            left, right = values
            if operation in ('difference', 'percentage_point_difference'):
                exact = left - right
            elif operation == 'percent_change':
                exact = (left - right) / abs(right) * 100
            elif operation in ('ratio', 'percent_ratio'):
                exact = left / right * (100 if operation == 'percent_ratio' else 1)
            else:
                raise ValueError(f'Unsupported operation: {operation}')
        else:
            raise ValueError('Unsupported operation or operand count')
        if not legacy:
            recorded = arithmetic['exact_value']
            if exact != Fraction(int(recorded['numerator']), int(recorded['denominator'])):
                raise ValueError(f'Exact arithmetic mismatch: {key}')
        rounding = ({'applied': operation == 'percent_change', 'decimal_places': 2}
                    if legacy else arithmetic['rounding'])
        display = Fraction(Decimal(calculation['value']))
        if rounding.get('applied'):
            places = rounding['decimal_places']
            if not isinstance(places, int) or not 0 <= places <= 18:
                raise ValueError('Unsupported display precision')
            with localcontext() as context:
                context.prec = max(100, len(str(abs(exact.numerator))) + len(str(exact.denominator)) + places + 10)
                expected = (Decimal(exact.numerator) / Decimal(exact.denominator)).quantize(
                    Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
            if display != Fraction(expected):
                raise ValueError(f'Display rounding mismatch: {key}')
        elif display != exact:
            raise ValueError(f'Unrounded display mismatch: {key}')
        visiting.remove(key)
        results[key] = exact
        checks.append({'calculation_id': key, 'operation': operation,
                       'exact': [str(exact.numerator), str(exact.denominator)],
                       'display': calculation['value'],
                       'recorded_exact_trace_checked': not legacy})
        return exact

    for key in calculations:
        replay(key)
    return {'classification': 'independent-arithmetic-from-frozen-inputs',
            'source_values_independently_verified': False,
            'economic_interpretation_verified': False,
            'independent_user_acceptance': False,
            'status': 'passed' if checks else 'no_recorded_calculations',
            'fact_count': len(facts), 'calculation_count': len(checks), 'checks': checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifact', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = verify(json.loads(args.artifact.read_text()))
    except Exception as error:
        result = {'status': 'failed', 'error': f'{type(error).__name__}: {error}'}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
