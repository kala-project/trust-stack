"""Real worker -> frozen calculator citation -> artifact -> saved exports, no LLM."""
import json
from pathlib import Path
import tempfile
import unittest

from tests.test_recorded_calculations import CalculationProvider, wait
from workbench.answer_artifacts import AnswerArtifactService
from workbench.build import build_database
from workbench.chat import ChatService
from workbench.research_results import ResearchResultsService


class PresentationProvider(CalculationProvider):
    def stream(self, messages, tools, cancel_event):
        handlers = {tool['name']: tool['handler'] for tool in tools}
        facts = handlers['get_company_facts'](metric='segment_revenue_productivity_and_business_processes')['facts']
        facts = [fact for fact in facts if fact['period_end'] == '2024-06-30' and fact['period_kind'] == 'year']
        old = next(fact for fact in facts if fact['basis'] == 'FY2024-era presentation')
        new = next(fact for fact in facts if fact['basis'] == 'FY2025-page recast presentation')
        calc = handlers['calculate_facts'](operation='difference', fact_ids=[new['fact_id'], old['fact_id']], comparison_mode='presentation_difference')
        yield {'type':'delta', 'text':f"重列差额为 {calc['value']} 百万美元；这不是业务增长。[calculation:{calc['calculation_id']}]"}


class CalculationArtifactIntegrationTests(unittest.TestCase):
    def test_actual_recorded_tools_project_and_export_without_current_data_backfill(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); db=root/'evidence.sqlite3'; build_database(db_path=db)
            for number, provider in enumerate([CalculationProvider(), PresentationProvider()]):
                with self.subTest(provider=type(provider).__name__):
                    chat=ChatService(db,root/f'chat-{number}.sqlite3',provider)
                    try:
                        conv=chat.create_conversation('MSFT'); cid=conv['conversation_id']
                        run=chat.submit(cid,'微软的收入差额是多少？请给出完整计算审计。')
                        self.assertEqual(wait(chat,run['run_id'])['status'],'completed')
                        answer=chat.get_conversation(cid)['messages'][-1]
                        service=AnswerArtifactService(chat)
                        artifact=service.get(cid,answer['message_id'])
                        self.assertEqual(len(artifact['calculations']),1,artifact['limitations'])
                        calc=artifact['calculations'][0]
                        self.assertEqual(calc['value'],'20252' if number==0 else '29092')
                        self.assertIsInstance(calc['data_version'],dict)
                        self.assertEqual(artifact['findings'][0]['classification'],'recorded_calculation')
                        self.assertFalse(artifact['findings'][0]['review_flags'])
                        self.assertFalse(artifact['verification']['semantic_verified'])
                        trace=next(t for t in artifact['traces'] if t['reference']==calc['reference'])
                        self.assertEqual([s['layer'] for s in trace['steps']],['L4','L3','L2','L1'])
                        self.assertEqual(len(trace['source']['sources']),2)
                        self.assertEqual({s['evidence_id'] for s in trace['source']['sources']},set(calc['evidence_ids']))
                        self.assertTrue(all(s['quote'] and s['source_sha256'] for s in trace['source']['sources']))
                        operations=artifact['audit']['authoritative_tool_operations']
                        self.assertEqual(len(operations),2)
                        self.assertEqual(len({op['call_id'] for op in operations}),2)
                        self.assertTrue(all(op['call_id'] and op['status']=='completed' for op in operations))
                        self.assertEqual(operations[0]['input'],{'metric':'revenue' if number==0 else 'segment_revenue_productivity_and_business_processes'})
                        self.assertEqual(operations[1]['input']['operation'],'difference')
                        self.assertEqual([s['fact_id'] for s in trace['source']['sources']],[f['fact_id'] for f in calc['inputs']])
                        saved=ResearchResultsService(chat,service).create(cid,answer['message_id'],'计算验收',answer['content'],[])
                        self.assertEqual(saved['artifact']['calculations'],artifact['calculations'])
                        content,mime,filename=ResearchResultsService(chat,service).export(saved['result_id'],cid,'MSFT',1,'json')
                        exported=json.loads(content)
                        self.assertEqual(exported['artifact']['artifact_id'],artifact['artifact_id'])
                        self.assertEqual(exported['artifact']['audit'],artifact['audit'])
                    finally:chat.close()

    def test_audit_preserves_failed_missing_truncated_and_unpaired_legacy_events(self):
        service=AnswerArtifactService(None)
        def event(seq, kind, call_id=None, **extra):
            return dict(seq=seq,type=kind,call_id=call_id,name='example',authoritative=True,**extra)
        events=[event(1,'tool_call','a',arguments={'which':'a'}),
                event(2,'tool_call','b',arguments={'which':'b'}),
                event(3,'tool_result','b',result={'error':'invalid_scope'}),
                event(4,'tool_call','c',arguments={'which':'c'}),
                event(5,'tool_result','c',truncated=True,original_chars=2000000),
                event(6,'tool_call',arguments={'legacy':'do not pair'}),
                event(7,'tool_result',result={'legacy':'do not pair'}),
                dict(seq=8,type='tool_result',call_id='a',name='example',authoritative=False,result={'fake':True})]
        audit=service._audit_summary({'run_id':'r','question':'q','company_id':'MSFT'},{'evidence':{},'documents':{}},[],[],events)
        operations=audit['authoritative_tool_operations']
        self.assertEqual([op['status'] for op in operations],['missing_output','error','truncated','missing_output','unpaired_output'])
        self.assertEqual(operations[1]['input'],{'which':'b'})
        self.assertEqual(operations[1]['output'],{'error':'invalid_scope'})
        self.assertIsNone(operations[0]['output'])
        self.assertIsNone(operations[-1]['input'])
        self.assertEqual(operations[2]['result_record']['original_chars'],2000000)

    def test_common_currency_units_keep_negative_sign_and_exact_scale(self):
        from decimal import Decimal
        from workbench.answer_artifacts import _money_amounts
        for text in ['−20,252 百万美元','-20252 USD millions','USD -20,252 million','-20.252 十亿美元']:
            self.assertEqual(_money_amounts(text),{('USD',Decimal('-20252000000'))},text)
        self.assertEqual(_money_amounts('USD 12345678901234567890.123456789 million'),
                         {('USD',Decimal('12345678901234567890123456.789'))})

    def test_chinese_period_and_metric_recommendation_is_not_first_dataset(self):
        service=AnswerArtifactService(None)
        rows=lambda kind:[{'period':{'kind':kind},'value':'1'}]
        data=[{'id':'year-profit','title':'operating_income','rows':rows('year')}, {'id':'quarter-revenue','title':'revenue','rows':rows('quarter')}, {'id':'quarter-profit','title':'operating_income','rows':rows('quarter')}]
        p=service._presentation('微软第四季度营业利润同比如何变化？',[],data)
        self.assertEqual(p['recommended_dataset_id'],'quarter-profit')
        self.assertEqual(p['audit_accessible_dataset_ids'],[d['id'] for d in data])

    def test_company_annual_revenue_never_recommends_segment_or_quarter_profit(self):
        service = AnswerArtifactService(None)
        def row(kind, year, basis='GAAP'):
            return {'period': {'kind': kind, 'end': f'{year}-06-30'},
                    'label': f'{year}-06-30', 'value': '1', 'basis': basis}
        datasets = [
            {'id': 'cloud', 'title': 'revenue', 'segment': 'intelligent_cloud',
             'basis': 'GAAP', 'rows': [row('year', 2025)]},
            {'id': 'quarter-profit', 'title': 'operating_income', 'basis': 'GAAP',
             'rows': [row('quarter', 2023), row('quarter', 2024), row('quarter', 2025)]},
        ]
        result = service._presentation('该公司2023至2025财年营业收入如何变化？', [], datasets)
        self.assertIsNone(result['recommended_dataset_id'])
        self.assertEqual(result['audit_accessible_dataset_ids'], ['cloud', 'quarter-profit'])
        self.assertIn('不推荐用于当前问题', result['recommended_dataset_reason'])

        datasets.append({'id': 'company-revenue', 'title': 'revenue', 'basis': 'GAAP',
                         'rows': [row('year', year) for year in (2023, 2024, 2025)]})
        matched = service._presentation('该公司2023至2025财年营业收入如何变化？', [], datasets)
        self.assertEqual(matched['recommended_dataset_id'], 'company-revenue')
