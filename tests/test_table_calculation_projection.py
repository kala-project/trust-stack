import unittest
from workbench.answer_artifacts import AnswerArtifactService


class TableCalculationProjectionTests(unittest.TestCase):
    def setUp(self):
        self.service=AnswerArtifactService(None)
        self.calcs=[{'calculation_id':'calc-a','value':'29092','unit':'USD millions','status':'recorded'}, {'calculation_id':'calc-b','value':'-17898','unit':'USD millions','status':'recorded'}]
        self.refs={('calculation',c['calculation_id']):{'valid':True} for c in self.calcs}

    def findings(self,text):
        return self.service._findings(text,self.refs,{}, {},self.calcs)

    def test_declared_units_and_inline_table_calculations_are_not_false_gaps(self):
        text='单位：**百万美元**；差额＝FY2025页面重述后 − FY2024当时页面。\n\n| 分部 | 差额 |\n|---|---:|\n| A | **+29,092** [calculation:calc-a] |\n| B | **−17,898** [calculation:calc-b] |'
        findings=self.findings(text)
        self.assertEqual(len(findings),1)
        self.assertEqual(findings[0]['classification'],'recorded_calculation')
        self.assertFalse(findings[0]['review_flags'])
        variant=text.replace('单位：**百万美元**；差额＝FY2025页面重述后 − FY2024当时页面。','差额按FY2025页面 − FY2024当时页面计算，单位均为**百万美元**：')
        self.assertFalse(self.findings(variant)[0]['review_flags'])
        wrong=self.findings(text.replace('−17,898','−17,899'))[0]
        self.assertEqual(wrong['review_flags'][0]['code'],'calculation_output_mismatch')

    def test_only_citation_footer_can_join_table_not_neighbouring_claim(self):
        table='| 分部 | 差额（百万美元） |\n|---|---:|\n| A | 29,092 |'
        findings=self.findings(table+'\n\n计算引用：[calculation:calc-a]\n\n这说明没有披露其他原因。')
        self.assertEqual(len(findings),2)
        self.assertEqual(findings[0]['classification'],'recorded_calculation')
        self.assertEqual(findings[1]['references'],[])
        self.assertEqual(findings[1]['review_flags'][0]['code'],'unsupported_non_disclosure')
        unrelated=self.findings(table+'\n\n另一个业务差额为29092百万美元。[calculation:calc-a]')
        self.assertEqual(len(unrelated),2)
        self.assertEqual(unrelated[0]['review_flags'][0]['code'],'missing_calculation_record')

    def test_years_and_citation_identifiers_are_not_derived_numeric_outputs(self):
        for text in ['2025 财年的增量和增速均高于 2024 财年。','不足以证明AI投入导致FY2025第四季度利润增长。','不是同比增长。[passage:123abc456]', '单位：百万美元；差额＝FY2025页面 − FY2024页面。']:
            findings=self.findings(text)
            self.assertFalse(any(flag['code'] in {'missing_calculation_record', 'missing_numeric_reference'}
                                 for finding in findings for flag in finding['review_flags']), text)
        self.assertEqual(self.findings('收入增加2025万美元。')[0]['review_flags'][0]['code'],'missing_calculation_record')

    def test_unicode_negative_percentage_cannot_match_positive_growth(self):
        from workbench.answer_artifacts import _calculation_output_matches
        self.assertFalse(_calculation_output_matches('同比变化为−25%。',{'value':'25','unit':'%'}))
        self.assertTrue(_calculation_output_matches('同比变化为−25%。',{'value':'-25','unit':'%'}))

    def test_table_converts_explicit_column_unit_with_exact_decimal_scaling(self):
        calculation = {'calculation_id': 'calc-convert', 'value': '8538',
                       'unit': 'USD millions', 'status': 'recorded'}
        refs = {('calculation', 'calc-convert'): {'valid': True}}
        def finding(value, unit='亿美元'):
            text = f'| 财年 | 同比增加（{unit}） |\n|---|---:|\n| 2024 | {value} [calculation:calc-convert] |'
            return self.service._findings(text, refs, {}, {}, [calculation])[0]

        self.assertFalse(finding('85.38')['review_flags'])
        common = ('单位均为亿美元。\n\n| 财年 | 同比增加 |\n|---|---:|\n'
                  '| 2024 | 85.38 [calculation:calc-convert] |')
        self.assertFalse(self.service._findings(common, refs, {}, {}, [calculation])[0]['review_flags'])
        self.assertEqual(finding('85.39')['review_flags'][0]['code'], 'missing_calculation_record')
        self.assertFalse(finding('8.538', 'USD billions')['review_flags'])
        self.assertEqual(finding('85.38', 'USD billions')['review_flags'][0]['code'],
                         'missing_calculation_record')
        self.assertEqual(finding('85.38', '亿元人民币')['review_flags'][0]['code'],
                         'missing_calculation_record')
        self.assertEqual(finding('-85.38')['review_flags'][0]['code'], 'missing_calculation_record')

        negative = {**calculation, 'value': '-8538'}
        text = '| 财年 | 同比变化（亿美元） |\n|---|---:|\n| 2024 | −85.38 [calculation:calc-convert] |'
        self.assertFalse(self.service._findings(text, refs, {}, {}, [negative])[0]['review_flags'])

    def test_table_cannot_borrow_units_from_another_column(self):
        table='| 收入（百万美元） | 差额（亿美元） |\n|---|---:|\n| 106820 | 29092 [calculation:calc-a] |'
        self.assertEqual(self.findings(table)[0]['review_flags'][0]['code'],'missing_calculation_record')
        missing='| 收入（百万美元） | 差额 |\n|---|---:|\n| 106820 | 29092 [calculation:calc-a] |'
        self.assertEqual(self.findings(missing)[0]['review_flags'][0]['code'],'missing_calculation_record')

    def test_explicit_unsupported_currency_cannot_inherit_common_dollars(self):
        from workbench.answer_artifacts import _table_calculation_matches
        record = {'calculation_id': 'calc-unit', 'value': '1', 'unit': 'USD millions'}
        for label in ['差额（EUR）', '差额EUR', '差额（欧元）', '差额（JPY billions）']:
            table = f'单位：百万美元\n\n| {label} |\n|---|\n| 1 [calculation:calc-unit] |'
            self.assertFalse(_table_calculation_matches(table, record), label)
        dollars = '| 差额（USD） |\n|---|\n| 1000000 [calculation:calc-unit] |'
        self.assertTrue(_table_calculation_matches(dollars, record))

    def test_explicit_source_coverage_still_needs_review(self):
        finding=self.findings('现有材料没有给出明确调节表。')[0]
        self.assertEqual(finding['classification'],'evidence_gap')
        self.assertEqual(finding['review_flags'][0]['code'],'source_coverage_required')
