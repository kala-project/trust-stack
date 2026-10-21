from copy import deepcopy
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tests.test_chat import FakeProvider, wait
from workbench.api_contracts import validate_response
from workbench.build import build_database
from workbench.chat import ChatService
from workbench.documents import DocumentService
from workbench.research_tools import ResearchTools
from workbench.research_scope import resolve_scope


class FactProvider(FakeProvider):
    def stream(self, messages, tools, cancel_event):
        result = next(t for t in tools if t['name'] == 'get_company_facts')['handler'](metric='revenue', period='2023-06-30')
        fact = result['facts'][0]
        assert fact['source_id']
        yield {'type': 'delta', 'text': f"收入 {fact['value']} 百万美元 [evidence:{fact['evidence_id']}]"}


class FactLineageTests(unittest.TestCase):
    def test_new_run_freezes_nonclaim_fact_and_opens_bound_citation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / 'evidence.sqlite3'
            build_database(db_path=db)
            service = ChatService(db, root / 'chat.sqlite3', FactProvider())
            try:
                conv = service.create_conversation('MSFT')
                run = service.submit(conv['conversation_id'], '微软2023第四季度收入是多少？')
                final = wait(service, run['run_id'])
                self.assertEqual(final['status'], 'completed')
                answer = service.get_conversation(conv['conversation_id'])['messages'][-1]
                evidence_id = answer['citations'][0]['id']
                frozen = next(e['evidence'] for e in final['events'] if e['type'] == 'data_snapshot')
                self.assertFalse(ChatService._evidence_claim_membership(frozen, evidence_id))
                self.assertTrue(any(f['evidence_id'] == evidence_id for f in frozen['facts']))
                self.assertTrue(any(e['evidence_id'] == evidence_id for e in frozen['evidence']))
                citation = service.read_citation(conv['conversation_id'], answer['message_id'], 'evidence', evidence_id)
                validate_response('GET', '/api/chat/citation', 200, citation)
                self.assertEqual(citation['detail_schema_version'], 'citation-detail/1')
                self.assertNotIn('source_visibility', citation)
                self.assertIn('56,189', citation['quote'])
                self.assertIsNone(citation['context'])
                self.assertEqual(citation['tables'], [])
                tools = ResearchTools(db, 'MSFT')
                item = next(e for e in frozen['evidence'] if e['evidence_id'] == evidence_id)
                source = next(s for s in frozen['sources'] if s['source_id'] == item['source_id'])
                arguments = dict(item=item, frozen_item=item, source=source, frozen_source=source, report=tools._company, frozen=frozen, company_id='MSFT', citation=answer['citations'][0], original={'evidence_id':evidence_id})
                self.assertTrue(ChatService._evidence_linkage_matches(**arguments))
                old = deepcopy(frozen); old.pop('facts')
                self.assertFalse(ChatService._evidence_linkage_matches(**{**arguments, 'frozen':old}))
                changed = deepcopy(frozen)
                for fact in changed['facts']:
                    if fact['evidence_id'] == evidence_id: fact['company_id'] = 'AAPL'
                self.assertFalse(ChatService._evidence_linkage_matches(**{**arguments, 'frozen':changed}))
                changed = deepcopy(item); changed['quote'] += 'changed'
                self.assertFalse(ChatService._evidence_linkage_matches(**{**arguments, 'frozen_item':changed}))
            finally:
                service.close()

    def test_same_metadata_malicious_current_body_never_supplies_frozen_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / 'evidence.sqlite3'
            build_database(db_path=db)
            documents = DocumentService(root / 'documents')
            source = Path(__file__).parents[1] / 'data/raw/microsoft-fy2024-q4-press-release.html'
            documents.register_file(
                source, company_id='MSFT', source_key='fy2024', title='FY2024')
            service = ChatService(
                db, root / 'chat.sqlite3', FactProvider(), document_service=documents)
            try:
                conv = service.create_conversation('MSFT')
                run = service.submit(conv['conversation_id'], '微软2023第四季度收入是多少？')
                self.assertEqual(wait(service, run['run_id'])['status'], 'completed')
                answer = service.get_conversation(conv['conversation_id'])['messages'][-1]
                evidence_id = answer['citations'][0]['id']
                with sqlite3.connect(db) as connection:
                    connection.execute(
                        "UPDATE sources SET text='MALICIOUS CURRENT BODY WITH SAME METADATA' "
                        "WHERE source_id='msft-fy2024-q4-press-release'")
                    connection.commit()
                citation = service.read_citation(
                    conv['conversation_id'], answer['message_id'], 'evidence', evidence_id)
                self.assertIn('56,189', citation['quote'])
                self.assertIn('56,189', citation['context'])
                self.assertNotIn('MALICIOUS CURRENT BODY', citation['context'])
                self.assertTrue(citation['document_version_id'])
            finally:
                service.close()

    def test_ai_investment_phrase_does_not_create_unknown_subject(self):
        question = ('现有微软资料能否量化AI投资的投资回报率，并证明AI投入导致了FY2025第四季度利润增长？'
                    '请只在证据足够时下结论。')
        self.assertEqual(resolve_scope(question)['code'], 'scope_ready')
        for question in ['微软与$AI的收入比较', '研究AI公司收入', '微软与NVDA收入比较']:
            self.assertNotEqual(resolve_scope(question)['code'], 'scope_ready')
