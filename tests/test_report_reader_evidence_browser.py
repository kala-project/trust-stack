"""Real-DOM contract test for the standalone report evidence browser."""
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

from scripts.browser_smoke import BrowserSmoke

ROOT = Path(__file__).resolve().parents[1]
_CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
# This host's snap wrapper can exist while snap-confine lacks required capabilities.
CHROMIUM = os.environ.get("CHROMIUM_BINARY") or (None if _CHROMIUM and _CHROMIUM.startswith("/snap/") else _CHROMIUM)


@unittest.skipUnless(shutil.which("node"), "Node is required for the evidence browser contract")
class ReportEvidenceBrowserNodeContractTests(unittest.TestCase):
    def test_rendering_switching_safety_and_mobile_steps(self):
        result = subprocess.run(
            ["node", "tests/run_report_reader_evidence_contract.js"],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("49 checks", result.stdout)


@contextmanager
def static_server():
    handler = lambda *args, **kwargs: SimpleHTTPRequestHandler(*args, directory=ROOT, **kwargs)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/tests/report_reader_evidence_contract.html"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@unittest.skipUnless(CHROMIUM, "Chromium is required for the evidence browser DOM contract")
class ReportEvidenceBrowserContractTests(unittest.TestCase):
    def test_rendering_switching_safety_and_mobile_steps(self):
        # Chromium children may finish writing their disposable profile after the parent exits.
        # Ignore only profile-cleanup races, never browser or contract assertion failures.
        with static_server() as url, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
            browser = BrowserSmoke(url, CHROMIUM, ROOT / "var" / "browser", 90)
            try:
                browser.start(profile)
                result = browser.wait(
                    "window.__evidenceContractResult || false",
                    "evidence browser contract completion",
                )
            finally:
                browser.stop()
        self.assertTrue(result.get("ok"), result.get("error"))
        self.assertGreaterEqual(len(result.get("checks", [])), 20, json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
