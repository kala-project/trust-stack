"""Controlled Chromium contract for reader download delivery and auth recovery."""
from pathlib import Path
import os
import json
import secrets
import tempfile
import threading
import unittest

from scripts.browser_smoke import BrowserSmoke
from workbench.build import build_database
from workbench.owner_auth import OwnerAuth
from workbench.server import EvidenceHTTPServer
from workbench.report_reader import list_reader_reports
from tests.test_report_publications import ReportProvider
from tests.test_recorded_calculations import wait


CHROME = os.environ.get("CHROMIUM_BINARY") or "/snap/chromium/current/usr/lib/chromium-browser/chrome"


@unittest.skipUnless(Path(CHROME).is_file(), "Controlled Chromium binary is unavailable")
class ReportReaderDeliveryBrowserTests(unittest.TestCase):
    def test_real_login_and_expiry_restore_the_same_published_report(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            database = root / "evidence.sqlite3"
            build_database(db_path=database)
            clock = [1000]
            auth = OwnerAuth(root / "auth" / "owner.sqlite3", session_seconds=3600, now=lambda: clock[0])
            password = secrets.token_urlsafe(32)
            auth.initialize(auth.bootstrap_path.read_text().strip(), password)
            server = EvidenceHTTPServer(0, database, chat_db=root / "chat.sqlite3", auth=auth, provider=ReportProvider())
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            browser = None
            try:
                initial = server.chat.create_and_submit("MSFT", "微软收入相对以前的累计变化")
                self.assertEqual(wait(server.chat, initial["run_id"])["status"], "completed")
                report = list_reader_reports(server.chat, initial["conversation_id"])["reports"][0]["report_id"]
                url = f"http://127.0.0.1:{server.server_port}/report-reader.html?report_id={report}&page=1&reading=single"
                browser = BrowserSmoke(f"http://127.0.0.1:{server.server_port}/", CHROME, root / "browser-output", 60)
                browser.start(str(root / "profile"))
                # Attach on the stable login shell before testing the direct-link
                # redirect; BrowserSmoke.start intentionally expects its exact URL.
                browser.cdp.call("Page.navigate", {"url": url})
                browser.wait("location.hash.startsWith('#/report-reader?')", "unauthenticated direct report redirects with return identity")

                def login():
                    browser.wait("Boolean(document.querySelector('#login-form:not([hidden]) #password'))", "real owner login is visible")
                    browser.evaluate(f"document.querySelector('#password').value={json.dumps(password)}; void 0")
                    browser.click("document.querySelector('#login-form button[type=submit]')")
                    browser.wait("document.querySelectorAll('.reader-page-title').length === 4", "same published report loaded after real login")
                    self.assertEqual(browser.evaluate("new URLSearchParams(location.search).get('report_id')"), report)

                login()
                browser.evaluate("""(() => {
                  const page=document.querySelector('[data-source-id]').closest('.reader-page');
                  const control=document.querySelector('#reader-goto');
                  control.value=Array.from(document.querySelectorAll('.reader-page')).indexOf(page)+1;
                  control.dispatchEvent(new Event('change',{bubbles:true}));
                })()""")
                browser.evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                before = browser.evaluate("location.href")
                clock[0] += 3601
                browser.click("document.querySelector('[data-source-id]')")
                browser.wait("location.pathname === '/' && location.hash.startsWith('#/report-reader?')", "actual expired source request returns to login")
                login()
                self.assertEqual(browser.evaluate("location.href"), before)
                self.assertEqual(browser.evaluate("Number(document.querySelector('#reader-goto').value)"),
                                 int(before.split('page=')[1].split('&')[0]))
            finally:
                if browser:
                    browser.stop()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

    def test_download_errors_stay_in_reader_success_releases_blob_and_401_recovers(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            root = Path(temporary)
            database = root / "evidence.sqlite3"
            build_database(db_path=database)
            server = EvidenceHTTPServer(0, database, chat_db=root / "chat.sqlite3")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = (f"http://127.0.0.1:{server.server_port}/report-reader.html"
                   "?report_id=missing&page=4&reading=two")
            browser = BrowserSmoke(url, CHROME, root / "browser-output", 60)
            try:
                browser.start(str(root / "profile"))
                browser.wait("document.getElementById('reader-title').textContent === 'Unable to open report'", "reader scripts loaded")
                result = browser.evaluate(r"""
                  (async () => {
                    const panel = document.getElementById('evidence-panel');
                    const body = document.getElementById('evidence-body');
                    panel.hidden = false;
                    const originalClick = HTMLAnchorElement.prototype.click;
                    const deliveries = [];
                    const revoked = [];
                    HTMLAnchorElement.prototype.click = function () {
                      if (this.download) { deliveries.push(this.download); return; }
                      return originalClick.call(this);
                    };
                    URL.createObjectURL = blob => {
                      blob.arrayBuffer().then(value => { window.__deliveredBytes = Array.from(new Uint8Array(value)); });
                      return 'blob:reader-contract';
                    };
                    URL.revokeObjectURL = value => revoked.push(value);
                    const run = async status => {
                      body.replaceChildren();
                      const link = document.createElement('a');
                      link.href = '/api/reports/raw-source?report_id=missing&source_id=s-' + status;
                      link.textContent = 'download';
                      body.append(link);
                      window.fetch = async () => status === 200
                        ? new Response(new Uint8Array([0, 255, 65, 10]), {status: 200, headers: {'Content-Disposition': 'attachment; filename="original-test.bin"'}})
                        : new Response('{}', {status});
                      const before = location.href;
                      link.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                      await new Promise(resolve => setTimeout(resolve, 30));
                      return {sameLocation: location.href === before, error: body.textContent};
                    };
                    const missing = await run(404);
                    const unavailable = await run(503);
                    const success = await run(200);
                    await new Promise(resolve => setTimeout(resolve, 30));
                    HTMLAnchorElement.prototype.click = originalClick;
                    return {missing, unavailable, success, deliveries, revoked, bytes: window.__deliveredBytes};
                  })()
                """)
                self.assertTrue(result["missing"]["sameLocation"])
                self.assertIn("unavailable", result["missing"]["error"])
                self.assertTrue(result["unavailable"]["sameLocation"])
                self.assertIn("temporarily unavailable", result["unavailable"]["error"])
                self.assertTrue(result["success"]["sameLocation"])
                self.assertEqual(result["bytes"], [0, 255, 65, 10])
                self.assertEqual(result["deliveries"], ["original-test.bin"])
                self.assertEqual(result["revoked"], ["blob:reader-contract"])

                # Switch only this disposable server to owner-auth mode so the
                # mocked expiry lands on the real login page rather than the app router.
                server.auth = OwnerAuth(root / "auth" / "owner.sqlite3")
                browser.evaluate(r"""
                  (() => {
                    const panel = document.getElementById('evidence-panel');
                    const body = document.getElementById('evidence-body');
                    panel.hidden = false;
                    body.replaceChildren();
                    const link = document.createElement('a');
                    link.href = '/api/reports/raw-source?report_id=missing&source_id=expired';
                    body.append(link);
                    window.fetch = async () => new Response('{}', {status: 401});
                    link.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                  })()
                """)
                browser.wait(
                    "location.pathname === '/' && location.hash === '#/report-reader?report_id=missing&page=4&reading=two'",
                    "expired download redirects to allowlisted reader login return",
                )
            finally:
                browser.stop()
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
