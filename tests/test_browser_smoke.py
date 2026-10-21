import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.browser_smoke import BrowserSmoke


class BrowserTargetTests(unittest.TestCase):
    def test_root_url_has_path(self):
        browser = BrowserSmoke('http://127.0.0.1:8766', 'unused', Path('.'), 1)
        self.assertEqual(browser.base_url, 'http://127.0.0.1:8766/')

    def test_reader_query_is_not_modified(self):
        url = 'http://127.0.0.1:8766/report-reader.html?report_id=report_123#analysis-findings'
        browser = BrowserSmoke(url, 'unused', Path('.'), 1)
        self.assertEqual(browser.base_url, url)

    def test_hash_route_is_not_modified(self):
        url = 'http://127.0.0.1:8766/#/research?tab=history'
        browser = BrowserSmoke(url, 'unused', Path('.'), 1)
        self.assertEqual(browser.base_url, url)

    def test_start_waits_for_runtime_location_not_speculative_target_url(self):
        url = 'http://127.0.0.1:8766/report-reader.html?report_id=report_123'
        # Runtime.evaluate returns the boolean result of the location equality:
        # false while the main world is still about:blank, then true on commit.
        evaluations = iter([False, True])

        class FakeProcess:
            returncode = None

            def poll(self):
                return None

        class FakeCDP:
            instance = None

            def __init__(self, websocket_url):
                self.websocket_url = websocket_url
                self.calls = []
                FakeCDP.instance = self

            def call(self, method, params=None, timeout=10):
                self.calls.append((method, params))
                if method == 'Runtime.evaluate':
                    return {'result': {'value': next(evaluations)}}
                return {}

        target = [{
            'type': 'page',
            'url': url,
            'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/page/target',
        }]

        class TargetResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.close()

        with tempfile.TemporaryDirectory() as profile:
            (Path(profile) / 'DevToolsActivePort').write_text('9222\n/devtools/browser/id\n')
            browser = BrowserSmoke(url, 'unused', Path('.'), 1)
            with mock.patch('scripts.browser_smoke.subprocess.Popen', return_value=FakeProcess()), \
                    mock.patch('scripts.browser_smoke.urllib.request.urlopen',
                               return_value=TargetResponse(json.dumps(target).encode())), \
                    mock.patch('scripts.browser_smoke.CDP', FakeCDP):
                browser.start(profile)

        runtime_calls = [params for method, params in FakeCDP.instance.calls if method == 'Runtime.evaluate']
        self.assertEqual(len(runtime_calls), 2)
        self.assertEqual(runtime_calls[0]['expression'], f"location.href.split('#')[0] === {json.dumps(url)}")
