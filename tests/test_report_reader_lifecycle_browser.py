"""Controlled browser contract for the single reader renderer lifecycle.

DOM and interaction evidence only; this is not screenshot review or user acceptance.
"""
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from scripts.browser_smoke import BrowserSmoke
from tests.test_report_reader_long_matrix_browser import CHROME, WEB, FixtureHandler, fixture_payload


@unittest.skipUnless(Path(CHROME).is_file(), "Controlled Chromium binary is unavailable")
class ReportReaderLifecycleBrowserTests(unittest.TestCase):
    def test_mount_update_dispose_rejects_stale_load_and_deduplicates_navigation(self):
        FixtureHandler.payload = fixture_payload()
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(FixtureHandler, directory=str(WEB)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        browser = None
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
                root = Path(temporary)
                browser = BrowserSmoke(
                    f"http://127.0.0.1:{server.server_port}/report-reader.html?report_id=controlled-long-matrix",
                    CHROME, root / "output", 45,
                )
                browser.start(str(root / "profile"))
                browser.wait("window.ReportReaderRenderer && document.querySelectorAll('#reader-root>.reader-page').length>2",
                             "renderer lifecycle and paginated report ready")

                # Hold a reader response until after disposal. Its successful body must
                # not render or notify the detached owner.
                browser.evaluate(r"""(() => {
                  window.__lifecyclePayload = null;
                  window.__lifecycleReady = 0;
                  window.__realFetch = window.fetch;
                  window.fetch = (url, init) => {
                    if (String(url).startsWith('/api/reports/reader?')) {
                      return new Promise(resolve => { window.__resolveStaleReader = resolve; });
                    }
                    return window.__realFetch(url, init);
                  };
                  window.ReportReaderRenderer.dispose();
                  window.ReportReaderRenderer.mount({reportId:'stale-report', callbacks:{onReady(){window.__lifecycleReady++;}}});
                  window.ReportReaderRenderer.dispose();
                })()""")
                browser.evaluate(r"""window.__resolveStaleReader(new Response(JSON.stringify({
                  schema_version:'report-reader-payload/1', report:{id:'stale',title:'迟到报告'},
                  question:{original:'迟到问题'}, chapters:[{id:'c',title:'迟到章节',blocks:[]}], sources:[], anchors:[]
                }),{status:200,headers:{'Content-Type':'application/json'}}))""")
                browser.evaluate("new Promise(r=>setTimeout(r,50))")
                self.assertEqual(browser.evaluate("window.__lifecycleReady"), 0)
                self.assertEqual(browser.evaluate("document.querySelector('#reader-root').children.length"), 0)

                # Mount twice around the same owned host. A real click advances one
                # page only, proving old listeners were removed. Theme is shell-owned
                # and must survive both lifecycle transitions.
                browser.evaluate(r"""(() => {
                  window.fetch = window.__realFetch;
                  document.documentElement.dataset.theme='dark';
                  const payload = JSON.parse(JSON.stringify(%s));
                  window.ReportReaderRenderer.mount({payload});
                  window.ReportReaderRenderer.dispose();
                  window.ReportReaderRenderer.mount({payload});
                })()""" % __import__("json").dumps(FixtureHandler.payload, ensure_ascii=False))
                browser.wait("document.querySelectorAll('#reader-root>.reader-page').length>2", "repeated mount rendered once")
                browser.click("document.querySelector('#reader-next')")
                self.assertEqual(browser.evaluate("new URL(location.href).searchParams.get('page')"), "2")
                self.assertEqual(browser.evaluate("document.documentElement.dataset.theme"), "dark")
                self.assertEqual(browser.evaluate("document.querySelectorAll('[data-anchor-id=matrix-description-anchor]').length"), 1)

                # A candidate uses this same renderer. A later formal publication is
                # only prompted: it must not swap candidate bytes, navigate, steal
                # focus, reset the selected page, or alter the shell theme.
                browser.evaluate(r"""(() => {
                  const formal = {report_id:'formal-new',message_id:'formal-message',reader_href:'/report-reader.html?report_id=formal-new'};
                  const realFetch = window.fetch;
                  window.fetch = (url, init) => {
                    if (String(url).startsWith('/api/verification/candidate?')) return new Promise(resolve => { window.__resolveCandidateStatus = () => resolve(new Response(JSON.stringify({candidate_id:'candidate-fixed',conversation_id:'conversation-fixed',status:'verified',formal_message_id:'formal-message',versions:[{version_id:'candidate-version',version_number:1}]}),{status:200,headers:{'Content-Type':'application/json'}})); });
                    if (String(url).startsWith('/api/reports?')) return Promise.resolve(new Response(JSON.stringify({reports:[formal]}),{status:200,headers:{'Content-Type':'application/json'}}));
                    return realFetch(url, init);
                  };
                  const payload = JSON.parse(JSON.stringify(%s));
                  Object.assign(payload.report,{id:'candidate-fixed',version_id:'candidate-version',title:'固定候选稿',kind:'verification_candidate',conversation_id:'conversation-fixed',candidate_id:'candidate-fixed',candidate_version_id:'candidate-version',message_id:null,formal_message_id:null});
                  window.ReportReaderRenderer.update({payload});
                  document.querySelector('#reader-info-button').focus();
                })()""" % __import__("json").dumps(FixtureHandler.payload, ensure_ascii=False))
                browser.evaluate("window.__resolveCandidateStatus()")
                browser.wait("document.querySelector('#reader-versions').textContent.includes('open new formal version')", "new formal version prompt")
                self.assertEqual(browser.evaluate("document.querySelector('#reader-title').textContent"), "固定候选稿")
                self.assertEqual(browser.evaluate("document.activeElement.id"), "reader-info-button")
                self.assertEqual(browser.evaluate("new URL(location.href).searchParams.get('page')"), "2")
                self.assertEqual(browser.evaluate("document.documentElement.dataset.theme"), "dark")

                # Unsupported and incomplete payloads fail at the public boundary,
                # before any report content can be projected.
                self.assertTrue(browser.evaluate(r"""(() => {
                  try { window.ReportReaderRenderer.validatePayload({schema_version:'report-reader-payload/2'}); }
                  catch (error) { return error.message.includes('Unsupported'); }
                  return false;
                })()"""))
                browser.evaluate("window.ReportReaderRenderer.dispose()")
                self.assertEqual(browser.evaluate("document.querySelector('#reader-root').children.length"), 0)
        finally:
            if browser:
                browser.stop()
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == "__main__":
    unittest.main()
