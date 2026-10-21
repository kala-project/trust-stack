"""Real HTTP + isolated DB; scripted provider is test-only, never selectable by API."""
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from workbench.build import build_database
from workbench.server import EvidenceHTTPServer


class ToolUsingTestProvider:
    def __init__(self):
        self.histories = []
        self.slow = False

    def status(self):
        return {"available": True, "provider": "test-only-scripted-provider", "reason": "Not a real model"}

    def resolve_research_intent(self, prompt, timeout_seconds):
        payload = json.loads(prompt.split("\n", 1)[1])
        company_id = payload.get("current_company_id") or "MSFT"
        value = {
            "kind": "company_research",
            "subjects": [{"kind": "company", "name": company_id, "candidate_id": company_id}],
            "directions": ["test follow-up"], "metrics": [], "period_text": None,
            "basis": [], "unit": None, "assumptions": [], "uncertainties": [],
            "excluded_entities": [], "is_continuation": True,
            "identity_ambiguity": False, "summary": payload["question"],
        }
        return {"intent": value, "provenance": {
            "prompt": prompt, "prompt_version": "research-intent/1.0",
            "provider": "explicit-test-adapter", "model": "http-test-intent-v1", "result": value,
        }}

    def stream(self, messages, tools, cancel_event):
        self.histories.append(messages)
        if self.slow:
            cancel_event.wait(2)
            return
        tool = next(t for t in tools if t['name'] == 'search_evidence')
        result = tool['handler'](query='revenue', limit=3)
        yield {"type": "delta", "text": "测试协议回答，不是真实模型。"}
        # Exercise returned refs without inventing an ID.
        text = json.dumps(result)
        import re
        refs = re.findall(r'"evidence_id":\s*"([^"]+)"', text)
        if refs:
            yield {"type": "delta", "text": f" [evidence:{refs[0]}]"}


class ChatHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / 'evidence.sqlite3'
        build_database(db_path=cls.db)
        cls.provider = ToolUsingTestProvider()
        cls.server = EvidenceHTTPServer(0, cls.db, provider=cls.provider)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port
        cls.origin = f'http://127.0.0.1:{cls.port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(3); cls.tmp.cleanup()

    def request(self, path, data=None, *, raw=None, headers=None, method=None):
        method = method or ('POST' if data is not None or raw is not None else 'GET')
        h = {'Origin': self.origin, 'Content-Type':'application/json'} if method=='POST' else {}
        h.update(headers or {})
        body = json.dumps(data, ensure_ascii=False).encode() if data is not None else raw
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=6)
        try:
            c.request(method, path, body=body, headers=h)
            r=c.getresponse(); return r.status, json.loads(r.read())
        finally:
            c.close()

    def conversation(self, company='MSFT'):
        code, data=self.request('/api/chat/conversations', {'company_id':company})
        self.assertEqual(code,201); return data['conversation_id']

    def wait_run(self, rid):
        for _ in range(150):
            code, run=self.request('/api/chat/run?id='+rid)
            self.assertEqual(code,200)
            if run['status'] not in ('queued','running'): return run
            time.sleep(.01)
        self.fail('run did not terminate')

    def test_server_preflights_all_database_pairs_before_chat_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence.sqlite3"
            build_database(db_path=evidence)
            evidence_before = evidence.read_bytes()

            with self.assertRaisesRegex(RuntimeError, "evidence and chat"):
                EvidenceHTTPServer(0, evidence, chat_db=evidence, wiki_db=root / "wiki.sqlite3")
            self.assertEqual(evidence.read_bytes(), evidence_before)

            chat = root / "chat.sqlite3"
            wiki_symlink = root / "wiki-symlink.sqlite3"
            wiki_symlink.symlink_to(evidence)
            with self.assertRaisesRegex(RuntimeError, "evidence and wiki"):
                EvidenceHTTPServer(0, evidence, chat_db=chat, wiki_db=wiki_symlink)
            self.assertFalse(chat.exists())
            self.assertEqual(evidence.read_bytes(), evidence_before)

            chat_alias = root / "chat-alias.sqlite3"
            wiki_alias = root / "wiki-alias.sqlite3"
            chat_alias.write_bytes(b"unchanged")
            os.link(chat_alias, wiki_alias)
            with self.assertRaisesRegex(RuntimeError, "chat and wiki"):
                EvidenceHTTPServer(0, evidence, chat_db=chat_alias, wiki_db=wiki_alias)
            self.assertEqual(chat_alias.read_bytes(), b"unchanged")

    def test_two_turns_actual_tools_and_persisted_history(self):
        cid=self.conversation()
        for question in ('收入是多少？','继续说明来源限制'):
            code, start=self.request('/api/chat/messages', {'conversation_id':cid,'message':question})
            self.assertEqual(code,202)
            run=self.wait_run(start['run_id']); self.assertEqual(run['status'],'completed',run)
            self.assertTrue(any(e['type']=='tool_result' and e.get('authoritative') for e in run['events']))
            code, drained=self.request('/api/chat/run?id='+start['run_id']+'&after='+str(run['next_cursor']))
            self.assertEqual(code,200); self.assertEqual(drained['events'],[])
        code, convo=self.request('/api/chat/conversation?id='+cid)
        self.assertEqual(code,200); self.assertEqual(len(convo['messages']),4)
        self.assertEqual(convo['messages'][-1]['verification']['status'],'references_checked')
        self.assertTrue(convo['messages'][-1]['citations'][0]['valid'])
        self.assertEqual(len(self.provider.histories[-1]),3)
        code, listing=self.request('/api/chat/conversations?company=MSFT')
        self.assertTrue(any(c['conversation_id']==cid for c in listing['conversations']))

    def test_default_real_provider_is_blocked_without_fake_answer(self):
        cid=self.conversation('AAPL')
        with patch.object(self.provider,'status',return_value={'available':False,'reason':'login missing','provider':'test'}):
            self.assertFalse(self.request('/api/chat/status')[1]['available'])
            code, body=self.request('/api/chat/messages', {'conversation_id':cid,'message':'你好'})
            self.assertEqual(code,503); self.assertEqual(body['error'],'provider_unavailable')
        self.assertEqual(self.request('/api/chat/conversation?id='+cid)[1]['messages'],[])

    def test_strict_post_boundary(self):
        path='/api/chat/conversations'
        for headers, code in [({'Origin':'null'},403),({'Origin':'http://evil.test'},403),({'Content-Type':'text/plain'},415),({'Content-Length':'70000'},413),({'Sec-Fetch-Site':'cross-site'},403)]:
            self.assertEqual(self.request(path,{'company_id':'MSFT'},headers=headers)[0],code)
        for raw in (b'{"company_id":"MSFT","company_id":"AAPL"}',b'[]',b'{"company_id":"MSFT","provider":"fake"}', b'{"company_id":NaN}',b'bad',b'\xff'):
            self.assertEqual(self.request(path,raw=raw)[0],400)
        for method in ('PUT','PATCH','DELETE','OPTIONS'):
            self.assertEqual(self.request(path,raw=b'{}',method=method)[0],405)
        self.assertEqual(self.request('/api/chat/messages?provider=fake',raw=b'{}')[0],405)

    def test_chat_status_advertises_safe_continuation_and_accepts_optional_fields(self):
        code, status = self.request('/api/chat/status')
        self.assertEqual(code, 200)
        self.assertTrue(status['capabilities']['report_continuation'])
        self.assertTrue(status['capabilities']['chat_submission_idempotency'])
        conversation_id = self.conversation()
        code, body = self.request('/api/chat/messages', {
            'conversation_id': conversation_id, 'message': 'follow up',
            'request_id': 'http-reader-request', 'base_message_id': 'missing-report',
        })
        self.assertEqual(code, 409)
        self.assertEqual(body['error'], 'report_context_conflict')

    def test_missing_origin_and_ambiguous_framing(self):
        c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        try:
            c.request('POST','/api/chat/conversations',body=b'{"company_id":"MSFT"}',headers={'Content-Type':'application/json'})
            self.assertEqual(c.getresponse().status,403)
        finally:c.close()
        for extra in (('Content-Length','2'),('Transfer-Encoding','chunked')):
            c=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
            try:
                c.putrequest('POST','/api/chat/conversations')
                for key,val in [('Origin',self.origin),('Content-Type','application/json'),('Content-Length','2'),extra]:c.putheader(key,val)
                c.endheaders(b'{}');self.assertEqual(c.getresponse().status,400)
            finally:c.close()

    def test_invalid_gets_and_evidence_integrity_gate(self):
        for path in ('/api/chat/run?id=x&after=-1','/api/chat/status?provider=fake','/api/chat/conversation?id=x&id=y'):
            self.assertEqual(self.request(path)[0],400)
        self.assertEqual(self.request('/api/chat/conversation?id=missing')[0],404)
        cid=self.conversation()
        with patch.object(self.server.service,'validate_store',side_effect=RuntimeError('private diagnostic')):
            code, body=self.request('/api/chat/messages',{'conversation_id':cid,'message':'hi'})
            self.assertEqual(code,503);self.assertNotIn('private diagnostic',json.dumps(body))

    def test_task_lost_ack_retry_after_completion_reuses_the_run(self):
        # Controlled protocol test: discard successful responses as a client
        # could on disconnect; observer queries are confined to this temp DB.
        import sqlite3
        from contextlib import closing
        request = {'question': '微软收入是多少', 'context_conversation_id': None,
                   'request_id': 'lost-task-ack-after-completion'}
        before = len(self.provider.histories)
        self.assertEqual(self.request('/api/research/tasks', request)[0], 201)
        with closing(sqlite3.connect(self.server.chat.chat_db)) as db:
            task_id, version = db.execute(
                'SELECT task_id,current_scope_version FROM research_tasks WHERE request_id=?',
                (request['request_id'],)).fetchone()
        start = {'task_id': task_id, 'expected_scope_version': version}
        self.assertEqual(self.request('/api/research/tasks/start', start)[0], 202)
        with closing(sqlite3.connect(self.server.chat.chat_db)) as db:
            first_run = db.execute('SELECT run_id FROM research_task_runs WHERE task_id=?', (task_id,)).fetchone()[0]
        self.assertEqual(self.wait_run(first_run)['status'], 'completed')
        code, replay_task = self.request('/api/research/tasks', request)
        self.assertEqual((code, replay_task['task_id']), (201, task_id))
        code, replay_run = self.request('/api/research/tasks/start', start)
        self.assertEqual((code, replay_run['run_id']), (202, first_run))
        self.assertEqual(len(self.provider.histories) - before, 1)
        with closing(sqlite3.connect(self.server.chat.chat_db)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM research_task_runs WHERE task_id=?', (task_id,)).fetchone()[0], 1)

    def test_cancel_and_duplicate_submit(self):
        cid=self.conversation();self.provider.slow=True
        try:
            code, start=self.request('/api/chat/messages',{'conversation_id':cid,'message':'wait'})
            self.assertEqual(code,202)
            self.assertEqual(self.request('/api/chat/messages',{'conversation_id':cid,'message':'duplicate'})[0],409)
            self.assertEqual(self.request('/api/chat/cancel',{'run_id':start['run_id']})[0],200)
            self.assertEqual(self.wait_run(start['run_id'])['status'],'cancelled')
        finally:self.provider.slow=False
