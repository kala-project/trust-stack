import unittest

from workbench.research_scope import resolve_scope, selected_company


class ScopeFollowupTests(unittest.TestCase):
    def test_financial_followups_need_real_context_not_a_new_default(self):
        for question in ('收入证据是什么？', '继续说明来源限制', '这个季度收入是多少？', '它的利润率呢？'):
            with self.subTest(question=question):
                result = resolve_scope(question, context_company_id='AAPL')
                self.assertEqual(result['status'], 'ready')
                self.assertEqual(selected_company(result), 'AAPL')
                self.assertEqual(resolve_scope(question)['status'], 'needs_clarification')

    def test_financial_period_tokens_are_not_companies(self):
        for question in ('MSFT FY2025 Q4 dividends and buybacks', 'Microsoft Q4 GAAP EPS'):
            with self.subTest(question=question):
                result = resolve_scope(question)
                self.assertEqual(result['status'], 'ready')
                self.assertEqual(selected_company(result), 'MSFT')

    def test_lowercase_comparison_subject_is_not_an_uncommon_metric(self):
        for question in ('Microsoft vs google revenue', 'MSFT and google earnings', '微软与google比较收入'):
            with self.subTest(question=question):
                result = resolve_scope(question, context_company_id='MSFT')
                self.assertEqual(result['status'], 'needs_clarification')
                self.assertIsNone(selected_company(result))
        self.assertEqual(resolve_scope('Microsoft comparison to earlier quarter')['status'], 'ready')

    def test_unknown_subjects_still_block_context_inheritance(self):
        for question in ('英伟达2025年收入如何？', '宁德时代利润率', '分析谷歌业务',
                         '谷歌的业务与微软有什么不同', '不要研究微软，请分析 Google 的收入趋势'):
            with self.subTest(question=question):
                result = resolve_scope(question, context_company_id='AAPL')
                self.assertEqual(result['status'], 'needs_clarification')
                self.assertIsNone(selected_company(result))
