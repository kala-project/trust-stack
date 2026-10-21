"""Controlled continuous-flow regression; not live research or visual approval."""
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from scripts.browser_smoke import BrowserSmoke
from tests.test_report_reader_long_matrix_browser import CHROME, WEB, FixtureHandler


def flow_payload():
    sources = [{"id": f"source-{i}", "title": f"来源 {i}：公开材料与期间说明 " * (1 + i % 3),
                "published_at": "2025-10-01", "media_type": "text/html"} for i in range(12)]
    items = [f"限制 {i}：这是用于验证连续排版的受控文字，不是研究结论。" * (1 + i % 3) for i in range(17)]
    return {
        "schema_version": "report-reader-payload/1",
        "report": {"id": "flow-fixture", "conversation_id": "fixture-only", "title": "连续流工程测试"},
        "question": {"original": "验证来源和限制是否自然接续正文。", "scope": {}},
        "sources": sources, "anchors": [],
        "chapters": [
            {"id": "question", "title": "问题与结论", "blocks": [
                {"id": "intro", "type": "question", "text": "验证来源和限制是否自然接续正文。"}]},
            {"id": "sources", "title": "证据与来源", "blocks": [
                {"id": "sources-list", "type": "sources", "title": "参考资料",
                 "text": "这是来源列表的一次性引言。", "source_ids": [s["id"] for s in sources]}]},
            {"id": "boundaries", "title": "边界与方法", "blocks": [
                {"id": "limits-list", "type": "limitations", "title": "研究限制", "items": items,
                 "text": "；".join(items)}]},
        ],
    }


@unittest.skipUnless(Path(CHROME).is_file(), "Controlled Chromium binary is unavailable")
class ReportReaderFlowBrowserTests(unittest.TestCase):
    def test_sources_and_limits_fill_paper_by_complete_rows_not_chapters(self):
        self.check_flow(flow_payload())

    def test_oversized_source_keeps_chapter_with_first_row_without_blank_page(self):
        payload = flow_payload()
        payload['sources'][0]['title'] = '单条超长来源仍必须完整保留。' * 150
        self.check_flow(payload)

    def test_required_verifier_labels_render_on_block_and_whole_node_evidence(self):
        payload = flow_payload()
        outcomes = [
            ("supported", "Machine verification: supported"),
            ("corrected_and_rechecked", "Machine verification: corrected and rechecked"),
            ("conflict_disclosed", "Machine verification: conflict disclosed"),
            ("insufficient_material_disclosed", "Machine verification: insufficient material disclosed"),
        ]
        findings = [{"node_id": "qualitative", "check_kind": f"check-{index}",
                     "outcome": outcome, "outcome_label": label,
                     "explanation": f"公开说明 {index}", "receipts": []}
                    for index, (outcome, label) in enumerate(outcomes)]
        anchor = {"id": "qualitative-whole", "finding_id": "qualitative",
                  "kind": "assertion", "text": "这是解释性方法建议。",
                  "support_status": "unresolved", "support_label": "待核查",
                  "evidence_id": "evidence-qualitative-whole", "reference_keys": [],
                  "semantic_review": None, "required_verifier_findings": findings}
        payload["anchors"] = [anchor]
        payload["chapters"][0]["blocks"].append({
            "id": "qualitative", "type": "finding", "title": "方法建议",
            "classification": "interpretation", "text": anchor["text"],
            "status": "not_assessed", "status_label": "尚未经语义核查",
            "anchor_ids": [anchor["id"]], "required_verifier_findings": findings,
        })

        class Handler(FixtureHandler):
            def do_GET(self):
                if self.path.startswith('/api/reports/evidence?'):
                    return self._json({"anchor": anchor, "evidence": {
                        "claim": anchor["text"], "verification": {
                            "method": "必要节点核验", "boundary": "仍待人工核准",
                            "semantic_verified": False,
                            "required_verifier_findings": findings,
                            "precise_binding_status": "未建立",
                        }, "references": [], "traces": [], "warnings": []},
                        "frozen_detail": {"traces": []}})
                return super().do_GET()
        Handler.payload = payload
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(WEB)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        browser = None
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
                root = Path(temporary)
                browser = BrowserSmoke(
                    f"http://127.0.0.1:{server.server_port}/report-reader.html?report_id=flow-fixture",
                    CHROME, root / "output", 30)
                browser.start(str(root / "profile"))
                browser.wait("document.querySelectorAll('.reader-page-title').length === 3", "controlled reader payload")
                block_ids = browser.evaluate("[...document.querySelectorAll('[data-block-id]')].map(node => node.dataset.blockId)")
                self.assertIn("qualitative", block_ids)
                block_html = browser.evaluate("document.querySelector('[data-block-id=qualitative]').outerHTML")
                self.assertIn("reader-machine-verification", block_html)
                block_text = browser.evaluate("document.querySelector('[data-block-id=qualitative]').textContent")
                for _outcome, label in outcomes:
                    self.assertIn(label, block_text)
                self.assertNotIn("尚未经语义核查", block_text)
                self.assertIn("human approval is still pending", block_text)
                browser.click("document.querySelector('[data-anchor-id=qualitative-whole]')")
                browser.evaluate("new Promise(resolve => setTimeout(resolve, 500))")
                panel_html = browser.evaluate("document.querySelector('#evidence-body').outerHTML")
                self.assertIn("reader-machine-verification", panel_html)
                panel_text = browser.evaluate("document.querySelector('#evidence-body').textContent")
                for _outcome, label in outcomes:
                    self.assertIn(label, panel_text)
                self.assertIn("Verification applies to the whole passage; evidence is not annotated sentence by sentence", panel_text)
                self.assertIn("Model verification does not replace human approval", panel_text)
                resolutions = browser.evaluate("""(()=>{
                  const source={source_id:'legacy-key',source_sha256:'a'.repeat(64),
                    source_url:'https://example.test/filing',title:'Source',quote:'Original excerpt'};
                  const registered={id:'version-1',document_version_id:'version-1',source_key:'legacy-key',
                    source_sha256:'a'.repeat(64),url:source.source_url};
                  function links(s, sources){
                    const rendered=ReportEvidenceBrowser.render({reportPayload:{report:{id:'fixture'},sources},
                      payload:{evidence:{claim:'Controlled identity test',verification:{}},
                        frozen_detail:{traces:[{id:'t',status:'available',source:s,steps:[]}]}}});
                    return [...rendered.querySelectorAll('a[href]')].filter(a=>a.getAttribute('href').startsWith('/api/reports/raw-source?')).length;
                  }
                  return {exact:links(source,[registered]),
                    badHash:links({...source,source_sha256:'b'.repeat(64)},[registered]),
                    badKey:links({...source,source_id:'other-key'},[registered]),
                    badUrl:links({...source,source_url:'https://example.test/other'},[registered]),
                    ambiguous:links(source,[registered,{...registered,id:'version-2',document_version_id:'version-2'}]),
                    directBadHash:links({...source,source_id:'version-1',source_sha256:'b'.repeat(64)},[registered])};
                })()""")
                self.assertEqual(resolutions, {"exact": 1, "badHash": 0, "badKey": 0,
                                               "badUrl": 0, "ambiguous": 0, "directBadHash": 0})
        finally:
            if browser:
                browser.stop()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def check_flow(self, payload):
        before = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        class Handler(FixtureHandler):
            def do_GET(self):
                if self.path.startswith('/api/reports/source?'):
                    source_id = parse_qs(urlsplit(self.path).query)['source_id'][0]
                    source = next(s for s in self.payload['sources'] if s['id'] == source_id)
                    return self._json({'source': source, 'raw_download_available': False, 'related_evidence': []})
                return super().do_GET()
        Handler.payload = payload
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(WEB)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        browser = None
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
                root = Path(temporary)
                browser = BrowserSmoke(f"http://127.0.0.1:{server.server_port}/report-reader.html?report_id=flow-fixture",
                                       CHROME, root / "output", 30)
                browser.start(str(root / "profile"))
                browser.wait("document.querySelectorAll('#reader-root [data-source-id]').length===12", "all source rows loaded")
                for width, mode in [(1440, "single"), (1600, "two"), (390, "single")]:
                    browser.cdp.call("Emulation.setDeviceMetricsOverride", {
                        "width": width, "height": 1000, "deviceScaleFactor": 1, "mobile": width < 500})
                    browser.click(f"document.querySelector('[data-reading-mode={mode}]')")
                    browser.evaluate("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
                    result = browser.evaluate(r"""(() => {
                      const root=document.querySelector('#reader-root');
                      const pages=[...root.querySelectorAll(':scope > .reader-page')];
                      const spare=[];
                      for(let i=0;i<pages.length-1;i++) {
                        const page=pages[i], next=pages[i+1];
                        const block=next.querySelector('.reader-block');
                        if(!block?.matches('.reader-block-sources,.reader-block-limitations'))continue;
                        const probe=page.cloneNode(true);probe.hidden=false;
                        probe.querySelector('.reader-page-number')?.remove();
                        // Hidden pages have no measured width. The first displayed paper
                        // has the same column width as every other paper in this layout.
                        const paper=pages.find(p=>!p.hidden);
                        Object.assign(probe.style,{position:'absolute',visibility:'hidden',width:paper.getBoundingClientRect().width+'px',height:'auto',minHeight:'0',left:'0',top:'0'});
                        const last=probe.querySelector('.reader-block:last-child');
                        const selector=block.matches('.reader-block-sources')?'tbody tr':'ul > li';
                        const first=block.querySelector(selector);
                        if(!first)continue;
                        if(last?.dataset.blockId===block.dataset.blockId) {
                          last.querySelector(selector==='tbody tr'?'tbody':'ul').append(first.cloneNode(true));
                        } else {
                          for(const node of next.children){
                            if(node===block)break;
                            probe.append(node.cloneNode(true));
                          }
                          const minimum=block.cloneNode(true);
                          [...minimum.querySelectorAll(selector)].slice(1).forEach(n=>n.remove());
                          probe.append(minimum);
                        }
                        root.append(probe);
                        const height=probe.getBoundingClientRect().height;
                        const target=Number(page.dataset.layoutTarget);
                        if(height<=target+1)spare.push({page:i+1,height,target,next:block.dataset.blockId});
                        probe.remove();
                      }
                      return {
                        emptyPages:pages.filter(p=>!p.querySelector('.reader-block')).length,
                        sourceChapterTogether:root.querySelector('#sources').closest('.reader-page')===root.querySelector('[data-source-id="source-0"]').closest('.reader-page'),
                        sources:[...root.querySelectorAll('[data-source-id]')].map(n=>n.dataset.sourceId),
                        limits:[...root.querySelectorAll('[data-block-id="limits-list"] ul > li')].map(n=>n.textContent),
                        sourceFragments:root.querySelectorAll('[data-block-id="sources-list"]').length,
                        sourceIntroductions:[...root.querySelectorAll('.reader-narrative')].filter(n=>n.textContent==='这是来源列表的一次性引言。').length,
                        sourceHeaders:[...root.querySelectorAll('[data-block-id="sources-list"]')].every(n=>n.querySelectorAll('thead').length===1),
                        titles:root.querySelectorAll('[data-block-id="sources-list"] h3').length,
                        mixedChapterPage:pages.some(p=>p.dataset.pageChapters.split(' ').length>1),
                        spare,
                      };
                    })()""")
                    with self.subTest(width=width, mode=mode):
                        self.assertEqual(result["sources"], [s["id"] for s in payload["sources"]])
                        self.assertEqual(result["limits"], payload["chapters"][2]["blocks"][0]["items"])
                        self.assertEqual(result['emptyPages'], 0, 'chapter metadata alone must never create a paper page')
                        self.assertTrue(result['sourceChapterTogether'], 'source chapter navigation must point to its first actual row')
                        self.assertGreater(result["sourceFragments"], 1)
                        self.assertEqual(result["sourceIntroductions"], 1)
                        self.assertEqual(result["titles"], 1)
                        self.assertTrue(result["sourceHeaders"])
                        self.assertTrue(result["mixedChapterPage"])
                        self.assertEqual(result["spare"], [], "paper must accept the next complete source row/list item when it fits")
                # The split source DOM is recreated on resize. Both closing a
                # source panel and direct focused reflow must retain its identity.
                browser.cdp.call('Emulation.setDeviceMetricsOverride', {
                    'width': 1600, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
                browser.evaluate("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
                browser.evaluate("""(()=>{const button=document.querySelector('[data-source-id="source-10"]');
                  const page=[...document.querySelectorAll('#reader-root>.reader-page')].indexOf(button.closest('.reader-page'))+1;
                  const input=document.querySelector('#reader-goto');input.value=page;input.dispatchEvent(new Event('change',{bubbles:true}));})()""")
                browser.click("document.querySelector('[data-source-id=source-10]')")
                browser.wait("!document.querySelector('#evidence-panel').hidden && document.querySelector('#evidence-body').textContent.includes('来源 10')", 'source detail loaded')
                browser.cdp.call('Emulation.setDeviceMetricsOverride', {
                    'width': 1440, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
                browser.evaluate("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
                browser.escape()
                browser.wait("document.activeElement?.dataset.sourceId==='source-10' && !document.activeElement.closest('.reader-page').hidden", 'source focus restored after panel reflow')
                browser.cdp.call('Emulation.setDeviceMetricsOverride', {
                    'width': 1024, 'height': 1000, 'deviceScaleFactor': 1, 'mobile': False})
                browser.evaluate("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
                self.assertTrue(browser.evaluate("document.activeElement?.dataset.sourceId==='source-10' && !document.activeElement.closest('.reader-page').hidden"))
                self.assertEqual(json.dumps(payload, ensure_ascii=False, sort_keys=True), before)
        finally:
            if browser:
                browser.stop()
            server.shutdown()
            server.server_close()
            thread.join(5)
