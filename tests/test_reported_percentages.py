import unittest
from workbench.answer_artifacts import AnswerArtifactService


class ReportedPercentageTests(unittest.TestCase):
    def findings(self, text, quotes):
        refs = {('evidence', key): {'valid': True} for key in quotes}
        sources = {('evidence', key): {'quote': value} for key, value in quotes.items()}
        return AnswerArtifactService(None)._findings(text, refs, {}, sources, [])

    def test_english_reported_percentage_is_not_invented_arithmetic(self):
        for quote in ['up 13 percent on an adjusted basis', 'up 13 per cent', 'up 13 PERCENT']:
            finding = self.findings('发行方披露调整后同比增长13%。[evidence:value]', {'value': quote})[0]
            self.assertFalse(finding['review_flags'])
            self.assertEqual(finding['classification'], 'source_statement')

    def test_percentage_points_and_other_values_cannot_validate_percent(self):
        for quote in ['13 percentage points', '13 percentile', 'up 12 percent', 'down −13 percent']:
            finding = self.findings('同比增长13%。[evidence:value]', {'value': quote})[0]
            self.assertEqual(finding['review_flags'][0]['code'], 'missing_calculation_record')

    def test_basis_footnote_cannot_borrow_value_from_previous_paragraph(self):
        quotes = {'value': 'EPS $1.85, up 13 percent on an adjusted basis', 'basis': 'Non-GAAP measure excluding a one-time tax charge'}
        text = '同比增长13%。[evidence:value]\n\n13%属于调整后同比。[evidence:basis]'
        findings = self.findings(text, quotes)
        self.assertFalse(findings[0]['review_flags'])
        self.assertEqual(findings[1]['review_flags'][0]['code'], 'missing_calculation_record')
        fixed = self.findings(text + ' [evidence:value]', quotes)
        self.assertFalse(fixed[1]['review_flags'])

    def test_percentage_points_are_distinct_from_percent_and_require_source_context(self):
        quote = 'Microsoft Cloud gross margin percentage was 68%, down 2 points year-over-year.'
        good = self.findings('毛利率为68%，同比下降2个百分点。[evidence:value]', {'value': quote})[0]
        self.assertFalse(good['review_flags'])
        for text in ['毛利率同比下降2%。', '毛利率为68%，同比下降18个百分点。']:
            bad = self.findings(text+'[evidence:value]', {'value': quote})[0]
            self.assertEqual(bad['review_flags'][0]['code'], 'missing_calculation_record')
        for source in ['up 2%', 'received 2 points in a game', 'Investor Relations Microsoft AI Azure Windows']:
            bad = self.findings('毛利率同比下降2个百分点。[evidence:value]', {'value': source})[0]
            self.assertEqual(bad['review_flags'][0]['code'], 'missing_calculation_record')

    def test_uncited_headline_money_and_percentage_are_not_silently_accepted(self):
        for headline in ['稀释每股收益为1.85美元。', '披露的比例为13%。']:
            text = headline + '\n\n调整口径说明。[evidence:value]'
            quotes = {'value': 'Diluted earnings per share was $1.85, up 13 percent on an adjusted basis'}
            finding = self.findings(text, quotes)[0]
            self.assertEqual(finding['review_flags'][0]['code'], 'missing_numeric_reference')
            fixed = self.findings(headline + '[evidence:value]', quotes)[0]
            self.assertFalse(fixed['review_flags'])
        flags = self.findings('截至2025年9月27日。', {})[0]['review_flags']
        self.assertEqual([flag['code'] for flag in flags], ['missing_local_reference'])

    def test_known_passive_legal_event_must_not_reverse_the_actor(self):
        quotes = {'basis': 'impact of the reversal of the European General Court’s State Aid decision'}
        wrong = self.findings('因欧洲普通法院撤销国家援助裁决而确认税费。[evidence:basis]', quotes)[0]
        self.assertEqual(wrong['review_flags'][0]['code'], 'possible_source_role_reversal')
        for text in ['因欧洲普通法院的国家援助裁决被推翻而确认税费。', '并非欧洲普通法院撤销了该裁决。']:
            self.assertFalse(self.findings(text+'[evidence:basis]', quotes)[0]['review_flags'])
        unrelated = self.findings('欧洲普通法院撤销另一决定。[evidence:other]', {'other': 'A different legal event'})[0]
        self.assertFalse(unrelated['review_flags'])
