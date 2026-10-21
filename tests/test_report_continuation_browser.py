"""Controlled Chromium contracts for continuing research inside the formal reader."""
from pathlib import Path
import json
import os
import tempfile
import threading
import unittest

from scripts.browser_smoke import BrowserSmoke
from tests.test_recorded_calculations import wait
from tests.test_report_publications import ReportProvider
from workbench.build import build_database
from workbench.report_reader import list_reader_reports
from workbench.server import EvidenceHTTPServer


CHROME = os.environ.get("CHROMIUM_BINARY") or "/snap/chromium/current/usr/lib/chromium-browser/chrome"


class ControlledDecisionReportProvider(ReportProvider):
    """Protocol substitute: one explicit decision, then a structured report."""
    def __init__(self):
        super().__init__()
        self.stream_calls = 0

    def status(self):
        return {"available": True, "reason": "", "provider": "controlled-decision-report-provider"}

    def stream(self, messages, tools, cancel_event):
        self.stream_calls += 1
        if self.stream_calls == 1:
            request = next(item["handler"] for item in tools if item["name"] == "request_confirmation")
            request(
                kind="basis",
                question="跨页面遗留的原始确认问题",
                explanation="这是受控 provider 提出的确认，不是真实模型研究。",
                options=[
                    {"id": "original", "label": "沿用原口径", "benefit": "保持可比性", "limitation": "不扩展范围"},
                    {"id": "expanded", "label": "扩展核查口径", "benefit": "覆盖更多边界", "limitation": "需要更多处理"},
                ],
                multiple=False,
            )
            return
        yield from super().stream(messages, tools, cancel_event)


@unittest.skipUnless(Path(CHROME).is_file(), "Controlled Chromium binary is unavailable")
class ReportContinuationBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.root = Path(cls.temporary.name)
        database = cls.root / "evidence.sqlite3"
        build_database(db_path=database)
        cls.server = EvidenceHTTPServer(0, database, chat_db=cls.root / "chat.sqlite3", provider=ReportProvider())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        initial = cls.server.chat.create_and_submit("MSFT", "受控 reader continuation 浏览器协议")
        assert wait(cls.server.chat, initial["run_id"])["status"] == "completed"
        cls.conversation_id = initial["conversation_id"]
        cls.report = list_reader_reports(cls.server.chat, cls.conversation_id)["reports"][0]
        cls.url = f"http://127.0.0.1:{cls.server.server_port}{cls.report['reader_href']}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=5)
        cls.temporary.cleanup()

    def setUp(self):
        self.browser = BrowserSmoke(self.url, CHROME, self.root / self._testMethodName, 60)
        self.browser.start(str(self.root / f"profile-{self._testMethodName}"))
        self.browser.wait("Boolean(document.getElementById('reader-continuation-button') && !document.getElementById('reader-continuation-button').hidden)", "continuation action is available after immutable payload loads")

    def tearDown(self):
        self.browser.stop()

    def install_api(self, script):
        self.browser.evaluate("sessionStorage.clear(); void 0")
        self.browser.evaluate(script)

    def test_draft_lost_response_poll_retry_focus_and_cancel(self):
        self.install_api(r"""
          (() => {
            const original = window.fetch.bind(window);
            const current = {report_id: 'CURRENT', version: 1, title: '受控报告'};
            window.__api = {messages: [], runGets: 0, cancels: 0, statusGets: 0, lose: true, runMode: 'fail', cancelled: false};
            window.fetch = async (input, options={}) => {
              const url = new URL(typeof input === 'string' ? input : input.url, location.href);
              if (url.pathname === '/api/chat/status') {
                __api.statusGets += 1;
                if (__api.statusGets === 1) return new Promise(resolve => { __api.resolveStatus=()=>resolve(new Response(JSON.stringify({capabilities:{report_continuation:true}}), {status:200})); });
                return new Response(JSON.stringify({capabilities:{report_continuation:true}}), {status:200});
              }
              if (url.pathname === '/api/reports') return new Response(JSON.stringify({reports:[current]}), {status:200});
              if (url.pathname === '/api/chat/messages') {
                __api.messages.push(JSON.parse(options.body));
                if (__api.lose) { __api.lose = false; throw new TypeError('controlled lost response'); }
                return new Response(JSON.stringify({run_id:'run-recovery', conversation_id:__api.messages[0].conversation_id, status:'queued'}), {status:202});
              }
              if (url.pathname === '/api/chat/run') {
                __api.runGets += 1;
                if (__api.runMode === 'fail') throw new TypeError('controlled poll failure');
                const status = __api.cancelled ? 'cancelled' : 'running';
                return new Response(JSON.stringify({run_id:'run-recovery', conversation_id:__api.messages[0].conversation_id, status, events:[], next_cursor:0}), {status:200});
              }
              if (url.pathname === '/api/chat/cancel') {
                __api.cancels += 1; __api.cancelled = true;
                return new Response(JSON.stringify({run_id:'run-recovery', status:'cancelling'}), {status:200});
              }
              return original(input, options);
            };
          })()
        """)
        self.browser.click("document.getElementById('reader-continuation-button')")
        self.browser.wait("Boolean(document.querySelector('.continuation-form textarea'))", "continuation form opens in reader dialog")
        self.assertEqual(self.browser.evaluate("document.activeElement.id"), "evidence-panel")
        self.assertTrue(self.browser.evaluate("document.querySelector('.reader-shell').inert"))
        self.browser.evaluate(r"""(() => {
          const body=document.getElementById('evidence-body');
          const submit=document.querySelector('.continuation-form button[type=submit]');
          const textarea=document.querySelector('.continuation-form textarea');
          (submit.disabled ? textarea : submit).id='continuation-last-interactive';
          const disabled=document.createElement('button'); disabled.id='trap-disabled'; disabled.disabled=true;
          const hidden=document.createElement('button'); hidden.id='trap-hidden'; hidden.hidden=true;
          const inert=document.createElement('span'); inert.inert=true;
          const inertButton=document.createElement('button'); inertButton.id='trap-inert'; inert.append(inertButton);
          body.append(disabled,hidden,inert);
          document.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',shiftKey:true,bubbles:true}));
        })()""")
        self.assertEqual(self.browser.evaluate("document.activeElement.id"), "continuation-last-interactive")
        self.browser.evaluate("__api.resolveStatus(); void 0")
        self.browser.wait("!document.querySelector('.continuation-form button[type=submit]').disabled", "readiness rerenders after focused control is detached")
        self.assertTrue(self.browser.evaluate("document.getElementById('evidence-panel').contains(document.activeElement)"))
        self.browser.evaluate("(() => { document.querySelector('.continuation-form button[type=submit]').focus(); document.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',bubbles:true})); })()")
        self.assertEqual(self.browser.evaluate("document.activeElement.id"), "evidence-close")
        self.browser.evaluate("(() => { const x=document.querySelector('.continuation-form textarea'); x.value='请核查反例'; x.dispatchEvent(new Event('input',{bubbles:true})); })()")
        self.browser.evaluate("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})); void 0")
        self.browser.wait("document.getElementById('evidence-panel').hidden", "Escape closes continuation dialog")
        self.assertEqual(self.browser.evaluate("document.activeElement.id"), "reader-continuation-button")
        self.browser.click("document.getElementById('reader-continuation-button')")
        self.browser.wait("document.querySelector('.continuation-form textarea').value === '请核查反例'", "tab-scoped draft survives close and reopen")
        self.browser.wait("!document.querySelector('.continuation-form button[type=submit]').disabled", "submit waits for capability and version checks")
        self.browser.click("document.querySelector('.continuation-form button[type=submit]')")
        self.browser.wait("document.getElementById('evidence-body').textContent.includes('No submission response was received')", "lost 202 response offers same-request recovery")
        self.browser.click("document.querySelector('.continuation-form button[type=submit]')")
        self.browser.wait("document.getElementById('evidence-body').textContent.includes('the question will not be submitted again')", "poll failure does not resubmit model work")
        calls = self.browser.evaluate("__api.messages")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0]["base_message_id"], self.report["message_id"])
        self.browser.evaluate("__api.runMode='running'; void 0")
        self.browser.click("Array.from(document.querySelectorAll('button')).find(x=>x.textContent==='Reload status')")
        self.browser.wait("document.getElementById('evidence-body').textContent.includes('Research in progress')", "status retry performs GET only")
        self.assertEqual(self.browser.evaluate("__api.messages.length"), 2)
        self.browser.click("Array.from(document.querySelectorAll('button')).find(x=>x.textContent.includes('Cancel this continued research'))")
        self.browser.wait("document.getElementById('evidence-body').textContent.includes('Research cancelled')", "cancel reaches persisted run status")
        self.assertEqual(self.browser.evaluate("__api.cancels"), 1)

    def test_panel_focus_and_inert_cleanup_survive_dispose_and_remount(self):
        self.browser.evaluate("fetch('/api/reports/reader?'+new URLSearchParams({report_id:new URL(location.href).searchParams.get('report_id')})).then(response=>response.json()).then(payload=>{window.__remountPayload=payload})")
        self.browser.click("document.getElementById('reader-continuation-button')")
        self.browser.wait("!document.getElementById('evidence-panel').hidden", "continuation dialog opens before disposal")
        self.assertTrue(self.browser.evaluate("document.querySelector('.reader-shell').inert"))
        self.assertEqual(self.browser.evaluate("document.getElementById('reader-continuation-button').getAttribute('aria-expanded')"), "true")

        self.browser.evaluate("window.ReportReaderRenderer.dispose()")
        self.assertTrue(self.browser.evaluate("document.getElementById('evidence-panel').hidden"))
        self.assertFalse(self.browser.evaluate("document.querySelector('.reader-shell').inert"))
        self.assertIsNone(self.browser.evaluate("document.getElementById('reader-continuation-button').getAttribute('aria-expanded')"))
        self.assertFalse(self.browser.evaluate("document.body.classList.contains('reader-modal-open')"))

        self.browser.evaluate("window.ReportReaderRenderer.mount({payload:window.__remountPayload})")
        self.browser.wait("!document.getElementById('reader-continuation-button').hidden", "continuation action returns after remount")
        self.browser.click("document.getElementById('reader-continuation-button')")
        self.browser.wait("Boolean(document.querySelector('.continuation-form textarea'))", "remounted continuation dialog renders")
        self.assertEqual(self.browser.evaluate("document.activeElement.id"), "evidence-panel")
        self.browser.evaluate("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',shiftKey:true,bubbles:true})); void 0")
        self.assertTrue(self.browser.evaluate("document.getElementById('evidence-panel').contains(document.activeElement)"))
        self.browser.evaluate("document.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true})); void 0")
        self.assertEqual(self.browser.evaluate("document.activeElement.id"), "reader-continuation-button")
        self.assertFalse(self.browser.evaluate("document.querySelector('.reader-shell').inert"))

    def test_decision_decline_accept_and_exact_new_report_link(self):
        current_id = self.report["report_id"]
        self.install_api(f"""
          (() => {{
            const original = window.fetch.bind(window);
            const current = {{report_id:{json.dumps(current_id)}, version:1, title:'受控报告'}};
            window.__api = {{messages:[], answers:[], accepted:false}};
            const decision = number => ({{decision_id:'decision-'+number, run_id:'run-'+number, status:'pending', kind:'scope',
              question:'请选择核查方向', explanation:'受控确认，不是真实研究。', groups:[{{id:'g',label:'方向',option_ids:['a','b'],minimum:1,maximum:2}}],
              options:[{{id:'a',label:'反例',benefit:'核查边界',limitation:'增加时间'}},{{id:'b',label:'来源',benefit:'补充来源',limitation:'不保证结论改变'}}]}});
            window.fetch = async (input, options={{}}) => {{
              const url = new URL(typeof input === 'string' ? input : input.url, location.href);
              if (url.pathname === '/api/chat/status') return new Response(JSON.stringify({{capabilities:{{report_continuation:true}}}}), {{status:200}});
              if (url.pathname === '/api/reports') {{
                const reports = __api.accepted ? [current, {{report_id:'NEW',version:2,title:'新报告',run_id:'run-final',message_id:'message-final'}}] : [current];
                return new Response(JSON.stringify({{reports}}), {{status:200}});
              }}
              if (url.pathname === '/api/chat/messages') {{
                __api.messages.push(JSON.parse(options.body)); const n=__api.messages.length;
                return new Response(JSON.stringify({{run_id:'run-'+n,conversation_id:__api.messages[0].conversation_id,status:'queued'}}), {{status:202}});
              }}
              if (url.pathname === '/api/chat/run') {{
                const id=url.searchParams.get('id');
                if (id==='run-final') return new Response(JSON.stringify({{run_id:id,conversation_id:__api.messages[0].conversation_id,status:'completed',events:[{{type:'message',message_id:'message-final'}}],next_cursor:1}}), {{status:200}});
                const n=Number(id.split('-')[1]);
                if (__api.accepted) return new Response(JSON.stringify({{run_id:id,conversation_id:__api.messages[0].conversation_id,status:'blocked',events:[],next_cursor:4,decision:{{...decision(n),status:'accepted',continuation_run_id:'run-final'}}}}), {{status:200}});
                return new Response(JSON.stringify({{run_id:id,conversation_id:__api.messages[0].conversation_id,status:'waiting_confirmation',events:[],next_cursor:0,decision:decision(n)}}), {{status:200}});
              }}
              if (url.pathname === '/api/research/decisions/answer') {{
                const body=JSON.parse(options.body); __api.answers.push(body);
                if (body.action==='declined') return new Response(JSON.stringify({{run_id:null,conversation_id:__api.messages[0].conversation_id,status:'declined'}}), {{status:200}});
                __api.accepted=true;
                throw new TypeError('controlled lost decision response');
              }}
              return original(input, options);
            }};
          }})()
        """)
        original_text = self.browser.evaluate("document.getElementById('reader-root').textContent")
        self.browser.click("document.getElementById('reader-continuation-button')")
        self.browser.wait("Boolean(document.querySelector('.continuation-form textarea'))", "decision test form is ready")
        self.browser.evaluate("(() => { const x=document.querySelector('.continuation-form textarea'); x.value='继续核查'; x.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('.continuation-form').requestSubmit(); })()")
        self.browser.wait("document.querySelectorAll('.continuation-decision-option input').length === 2", "required decision renders in reader")
        self.assertEqual(self.browser.evaluate("document.querySelectorAll('.continuation-decision-option input:checked').length"), 0)
        self.browser.click("Array.from(document.querySelectorAll('button')).find(x=>x.textContent==='Decline and keep original question')")
        self.browser.wait("Boolean(document.querySelector('.continuation-form'))", "declining returns to a new editable question")
        declined = self.browser.evaluate("__api.answers[0]")
        self.assertEqual(declined["action"], "declined")
        self.assertEqual(declined["selected_ids"], [])
        self.browser.evaluate("(() => { const x=document.querySelector('.continuation-form textarea'); x.value='改为核查来源'; x.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('.continuation-form').requestSubmit(); })()")
        self.browser.wait("document.querySelectorAll('.continuation-decision-option input').length === 2", "second decision has no inherited selection")
        self.assertEqual(self.browser.evaluate("document.querySelectorAll('.continuation-decision-option input:checked').length"), 0)
        self.browser.click("Array.from(document.querySelectorAll('button')).find(x=>x.textContent==='Confirm and continue research')")
        self.browser.wait("document.getElementById('evidence-body').textContent.includes('Select an option')", "accept never auto-selects a decision option")
        self.assertEqual(self.browser.evaluate("__api.answers.length"), 1)
        self.browser.click("document.querySelector('.continuation-decision-option input[value=a]')")
        self.browser.click("Array.from(document.querySelectorAll('button')).find(x=>x.textContent==='Confirm and continue research')")
        self.browser.wait("document.getElementById('evidence-body').textContent.includes('Could not confirm whether the selection was saved')", "lost decision response is not assumed to mean rejection")
        self.browser.click("[...document.querySelectorAll('button')].find(x=>x.textContent==='Reload confirmation status')")
        self.browser.wait("Boolean(Array.from(document.querySelectorAll('a')).find(x=>x.textContent==='Open new report'))", "GET recovers the already accepted continuation without another decision POST")
        self.assertEqual(self.browser.evaluate('__api.answers.length'), 2)
        accepted = self.browser.evaluate("__api.answers[1]")
        self.assertEqual(accepted["action"], "accepted")
        self.assertEqual(accepted["selected_ids"], ["a"])
        self.assertEqual(self.browser.evaluate("Array.from(document.querySelectorAll('a')).find(x=>x.textContent==='Open new report').getAttribute('href')"), "/report-reader.html?report_id=NEW")
        self.assertEqual(self.browser.evaluate("document.getElementById('reader-root').textContent"), original_text)

    def test_lost_response_replays_after_newer_report_and_blocks_inflight_edit(self):
        current_id = json.dumps(self.report['report_id'])
        conversation_id = json.dumps(self.conversation_id)
        base_message_id = json.dumps(self.report['message_id'])
        self.install_api(r"""
          (() => {
            const original=window.fetch.bind(window);
            const current={report_id:CURRENT_ID,version:1,title:'当前报告'};
            window.__api={ready:false,posts:[],finished:false};
            window.fetch=async (input, options={}) => {
              const url=new URL(typeof input==='string'?input:input.url,location.href);
              if(url.pathname==='/api/chat/status') {
                if(!__api.ready) throw new TypeError('offline');
                return new Response(JSON.stringify({capabilities:{report_continuation:true}}));
              }
              if(url.pathname==='/api/reports') return new Response(JSON.stringify({reports:__api.finished?[current,{report_id:'RECOVERED',version:2,run_id:'run-recovered'}]:[current]}));
              if(url.pathname==='/api/chat/conversation' && __api.finished) return new Response(JSON.stringify({
                conversation_id:CONVERSATION_ID,
                decision:{decision_id:'decision-other-tab',run_id:'run-other-tab',base_message_id:BASE_MESSAGE_ID,status:'pending',kind:'basis',question:'不得覆盖稳定请求',explanation:'受控并发确认',options:[{id:'x',label:'其他选择',benefit:'测试',limitation:'测试'}],groups:[],multiple:false}
              }));
              if(url.pathname==='/api/chat/messages') {
                __api.posts.push(JSON.parse(options.body));
                if(__api.posts.length===1) return new Promise((resolve,reject)=>{__api.release=()=>{__api.finished=true;reject(new TypeError('lost success'));};});
                return new Response(JSON.stringify({run_id:'run-recovered',conversation_id:__api.posts[0].conversation_id,status:'completed'}),{status:202});
              }
              if(url.pathname==='/api/chat/run') return new Response(JSON.stringify({run_id:'run-recovered',conversation_id:__api.posts[0].conversation_id,status:'completed',events:[],next_cursor:0}));
              return original(input,options);
            };
          })()
        """.replace('CURRENT_ID', current_id).replace('CONVERSATION_ID', conversation_id).replace('BASE_MESSAGE_ID', base_message_id))
        self.browser.click("document.getElementById('reader-continuation-button')")
        self.browser.wait("document.getElementById('evidence-body').textContent.includes('Recheck connection and version')", 'readiness failure has an actual retry action')
        self.browser.evaluate('__api.ready=true')
        self.browser.click("[...document.querySelectorAll('button')].find(x=>x.textContent==='Recheck connection and version')")
        self.browser.wait("!document.querySelector('.continuation-form button[type=submit]').disabled", 'readiness can recover without closing the reader')
        self.browser.evaluate("(()=>{const x=document.querySelector('.continuation-form textarea');x.value='核查边界';x.dispatchEvent(new Event('input',{bubbles:true}));})()")
        self.browser.click("document.querySelector('.continuation-form button[type=submit]')")
        self.browser.wait("__api.posts.length===1 && typeof __api.release==='function'", 'POST remains in flight')
        self.assertTrue(self.browser.evaluate("document.querySelector('.continuation-form button[type=submit]').disabled && document.querySelector('.continuation-form textarea').readOnly"))
        self.browser.evaluate("document.querySelector('.continuation-form').requestSubmit(); void 0")
        self.assertEqual(self.browser.evaluate('__api.posts.length'), 1)
        self.browser.evaluate('__api.release(); void 0')
        self.browser.wait("document.getElementById('evidence-body').textContent.includes('No submission response was received')", 'lost completion remains recoverable')
        self.browser.click("document.querySelector('.continuation-form button[type=submit]')")
        self.browser.wait("!![...document.querySelectorAll('a')].find(x=>x.textContent==='Open new report')", 'existing newer report does not block reclaiming the lost submission')
        posts=self.browser.evaluate('__api.posts')
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0], posts[1])
        self.assertEqual(self.browser.evaluate("[...document.querySelectorAll('a')].find(x=>x.textContent==='Open new report').getAttribute('href')"), '/report-reader.html?report_id=RECOVERED')

    def test_real_backend_cross_page_pending_decision_without_session_storage(self):
        # Real temporary HTTP/backend state. ControlledDecisionReportProvider is
        # an explicit protocol substitute, not a live-model or user acceptance run.
        initial = self.server.chat.create_and_submit("MSFT", "受控跨页面确认的初始报告")
        self.assertEqual(wait(self.server.chat, initial["run_id"])["status"], "completed")
        conversation_id = initial["conversation_id"]
        report = list_reader_reports(self.server.chat, conversation_id)["reports"][0]
        base = f"http://127.0.0.1:{self.server.server_port}"
        previous_provider = self.server.chat.provider
        controlled_provider = ControlledDecisionReportProvider()
        self.server.chat.provider = controlled_provider
        try:
            self.browser.cdp.call("Page.navigate", {"url": base + "/"})
            self.browser.wait("document.readyState === 'complete'", "separate application page is ready")
            submitted = self.browser.evaluate(f"""
              (async () => {{
                const response = await fetch('/api/chat/messages', {{
                  method: 'POST', headers: {{'Content-Type':'application/json','Accept':'application/json'}},
                  body: JSON.stringify({{
                    conversation_id: {json.dumps(conversation_id)},
                    message: '从其他页面提交并等待确认',
                    request_id: 'controlled-cross-page-request',
                    base_message_id: {json.dumps(report['message_id'])}
                  }})
                }});
                return {{status: response.status, body: await response.json()}};
              }})()
            """)
            self.assertEqual(submitted["status"], 202)
            self.assertEqual(wait(self.server.chat, submitted["body"]["run_id"])["status"], "blocked")
            projected = self.server.chat.get_conversation(conversation_id)["decision"]
            self.assertEqual(projected["run_id"], submitted["body"]["run_id"])
            self.assertEqual(projected["base_message_id"], report["message_id"])
            self.assertEqual(projected["status"], "pending")

            self.browser.evaluate("sessionStorage.clear(); void 0")
            self.browser.cdp.call("Page.navigate", {"url": base + report["reader_href"]})
            self.browser.wait("!!document.querySelector('#reader-continuation-button:not([hidden])')", "fresh reader page is loaded")
            self.assertEqual(self.browser.evaluate("sessionStorage.length"), 0)
            self.browser.click("document.getElementById('reader-continuation-button')")
            self.browser.wait("document.querySelector('[data-decision-id] h3')?.textContent === '跨页面遗留的原始确认问题'", "reader discovers the original pending decision through conversation GET")
            self.assertEqual(self.browser.evaluate("document.querySelectorAll('.continuation-decision-option input').length"), 2)
            self.assertEqual(self.browser.evaluate("document.querySelectorAll('.continuation-decision-option input:checked').length"), 0)
            self.assertIn("其他页面留下", self.browser.evaluate("document.getElementById('evidence-body').textContent"))
            self.assertIn("从其他页面提交并等待确认", self.browser.evaluate("document.querySelector('.continuation-original-question').textContent"))
            previous_document = self.browser.evaluate('performance.timeOrigin')
            self.browser.cdp.call('Page.reload')
            self.browser.wait(f"performance.timeOrigin !== {previous_document} && document.readyState === 'complete'", 'reader reload commits a new document')
            self.browser.wait("!!document.querySelector('#reader-continuation-button:not([hidden])')", 'reader reload restores its saved run')
            self.browser.click("document.getElementById('reader-continuation-button')")
            self.browser.wait("document.querySelector('.continuation-original-question')?.textContent.includes('从其他页面提交并等待确认') === true", 'run polling retains the original question after refresh')
            self.browser.click("document.querySelector('.continuation-decision-option input[value=original]')")
            self.browser.click("[...document.querySelectorAll('button')].find(x=>x.textContent==='Confirm and continue research')")
            self.browser.wait("!![...document.querySelectorAll('a')].find(x=>x.textContent==='Open new report')", "reader answers the discovered decision and follows the real continuation publication")

            reports = list_reader_reports(self.server.chat, conversation_id)["reports"]
            self.assertEqual(len(reports), 2)
            self.assertTrue(all(item["conversation_id"] == conversation_id for item in reports))
            self.assertEqual(controlled_provider.stream_calls, 2)
            answered = self.server.chat.get_conversation(conversation_id)["decision"]
            self.assertEqual(answered["status"], "accepted")
            self.assertEqual(answered["selected_ids"], ["original"])
            self.assertTrue(answered["continuation_run_id"])

            # The accepted decision remains readable on the conversation, but its
            # old base must never be claimed from the newly published report.
            latest = max(reports, key=lambda item: item["version"])
            self.browser.evaluate("sessionStorage.clear(); void 0")
            self.browser.cdp.call("Page.navigate", {"url": base + latest["reader_href"]})
            self.browser.wait("!!document.querySelector('#reader-continuation-button:not([hidden])')", "new report reader is loaded")
            self.browser.click("document.getElementById('reader-continuation-button')")
            self.browser.wait("!!document.querySelector('.continuation-form button[type=submit]')", "old-base accepted decision is not claimed by the new report")
            self.assertFalse(self.browser.evaluate("Boolean(document.querySelector('[data-decision-id]'))"))
        finally:
            self.server.chat.provider = previous_provider

    def test_real_backend_continuation_keeps_conversation_and_old_payload(self):
        # Real application HTTP and temporary DB; only the model is explicitly
        # controlled. No frozen production report or real model is touched.
        from unittest.mock import patch
        from urllib.request import urlopen
        from tests.test_research_tasks import CountingProvider
        initial=self.server.chat.create_and_submit('MSFT','受控集成初始报告')
        self.assertEqual(wait(self.server.chat,initial['run_id'])['status'],'completed')
        cid=initial['conversation_id']
        report=list_reader_reports(self.server.chat,cid)['reports'][0]
        base=f'http://127.0.0.1:{self.server.server_port}'
        def payload():
            with urlopen(base+'/api/reports/reader?report_id='+report['report_id']) as response:return json.load(response)
        before=payload()
        self.browser.cdp.call('Page.navigate',{'url':base+report['reader_href']})
        self.browser.wait("!!document.querySelector('#reader-continuation-button:not([hidden])')", 'fresh isolated report is loaded')
        with patch.object(self.server.chat.provider,'resolve_research_intent',CountingProvider().resolve_research_intent,create=True):
            self.browser.click("document.getElementById('reader-continuation-button')")
            self.browser.wait("!!document.querySelector('.continuation-form button[type=submit]') && !document.querySelector('.continuation-form button[type=submit]').disabled", 'real capability and version APIs permit continuation')
            self.browser.evaluate("(()=>{const x=document.querySelector('.continuation-form textarea');x.value='请进一步分析';x.dispatchEvent(new Event('input',{bubbles:true}));})()")
            self.browser.click("document.querySelector('.continuation-form button[type=submit]')")
            self.browser.wait("!![...document.querySelectorAll('a')].find(x=>x.textContent==='Open new report')", 'real submit, poll and publication APIs connect end to end')
        reports=list_reader_reports(self.server.chat,cid)['reports']
        self.assertEqual(len(reports),2)
        self.assertTrue(all(item['conversation_id']==cid for item in reports))
        self.assertEqual(payload(),before)
        link=self.browser.evaluate("[...document.querySelectorAll('a')].find(x=>x.textContent==='Open new report').getAttribute('href')")
        latest=max(reports,key=lambda item:item['version'])
        self.assertEqual(link,latest['reader_href'])

    def test_old_report_offers_latest_without_posting(self):
        current_id = self.report["report_id"]
        self.install_api(f"""
          (() => {{
            const original=window.fetch.bind(window); window.__posts=0;
            window.fetch=async (input, options={{}}) => {{
              const url=new URL(typeof input==='string'?input:input.url,location.href);
              if(url.pathname==='/api/chat/status') return new Response(JSON.stringify({{capabilities:{{report_continuation:true}}}}),{{status:200}});
              if(url.pathname==='/api/reports') return new Response(JSON.stringify({{reports:[{{report_id:{json.dumps(current_id)},version:1,title:'旧版'}},{{report_id:'LATEST',version:3,title:'最新版'}}]}}),{{status:200}});
              if(url.pathname==='/api/chat/messages') {{ window.__posts+=1; throw new Error('must not post'); }}
              return original(input,options);
            }};
          }})()
        """)
        self.browser.click("document.getElementById('reader-continuation-button')")
        self.browser.wait("document.getElementById('evidence-body').textContent.includes('version 3 is available')", "old reader explicitly offers the max-version report")
        self.assertEqual(self.browser.evaluate("document.querySelector('a.continuation-button').getAttribute('href')"), "/report-reader.html?report_id=LATEST")
        self.assertFalse(self.browser.evaluate("Boolean(document.querySelector('.continuation-form'))"))
        self.assertEqual(self.browser.evaluate("window.__posts"), 0)
        for width in (320, 390):
            self.browser.cdp.call("Emulation.setDeviceMetricsOverride", {"width": width, "height": 780, "deviceScaleFactor": 1, "mobile": True})
            self.browser.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            geometry = self.browser.evaluate("(() => { const p=document.getElementById('evidence-panel').getBoundingClientRect(); const a=document.querySelector('a.continuation-button').getBoundingClientRect(); return {panelLeft:p.left,panelRight:p.right,actionLeft:a.left,actionRight:a.right,viewport:innerWidth,overflow:document.getElementById('evidence-body').scrollWidth-document.getElementById('evidence-body').clientWidth}; })()")
            self.assertGreaterEqual(geometry["panelLeft"], -0.5)
            self.assertLessEqual(geometry["panelRight"], geometry["viewport"] + 0.5)
            self.assertGreaterEqual(geometry["actionLeft"], geometry["panelLeft"] - 0.5)
            self.assertLessEqual(geometry["actionRight"], geometry["panelRight"] + 0.5)
            self.assertLessEqual(geometry["overflow"], 1)


if __name__ == "__main__":
    unittest.main()
