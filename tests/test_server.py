"""Loopback integration/security tests with an isolated evidence database."""
from copy import deepcopy
import http.client
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import threading
import unittest

from workbench.build import build_database
from workbench.chat import _UnavailableProvider
from workbench.server import EvidenceHTTPServer, report_html


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.tmp.name) / "evidence.sqlite3"
        build_database(db_path=cls.db)
        # This suite checks a server without a model, independent of an installed SDK.
        cls.server = EvidenceHTTPServer(0, cls.db, provider=_UnavailableProvider())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def request(self, path, headers=None, method="GET"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            c.request(method, path, headers=headers or {})
            response = c.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            c.close()

    def test_health_and_real_data(self):
        status, headers, body = self.request('/api/overview')
        result = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(result['stats']['facts'], 60)
        self.assertFalse(result['agent']['enabled'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_public_health_is_minimal_and_does_not_touch_private_store(self):
        self.assertEqual(json.loads(self.request('/api/health')[2]), {'status': 'ok'})
        with patch.object(self.server.service, 'validate_store', side_effect=RuntimeError('tampered')):
            status, headers, body = self.request('/api/health')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {'status': 'ok'})
            self.assertNotIn(b'tampered', body)

    def test_host_origin_fetch_metadata(self):
        for headers in [
            {'Host': 'evil.example'},
            {'Host': '127.0.0.1:1234'},
            {'Origin': 'https://evil.example'},
            {'Origin': 'null'},
            {'Sec-Fetch-Site': 'cross-site'},
            {'Sec-Fetch-Site': 'same-site'},
        ]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request('/api/overview', headers)[0], 403)
        self.assertEqual(self.request('/api/overview', {'Origin': f'http://127.0.0.1:{self.port}'})[0], 200)

    def test_unknown_paths_and_traversal(self):
        for path in ['/data/raw/apple-fy2025-q4-press-release.html', '/../../etc/passwd',
                     '/%2e%2e/README.md', '/api/query?sql=select+1', '/scripts/build_task_graph.py']:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_writes_disabled(self):
        for method in ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']:
            self.assertEqual(self.request('/api/company?id=MSFT', method=method)[0], 405)

    def test_parameters_and_unknown_ids(self):
        for path in ['/api/company', '/api/company?id=MSFT&id=AAPL', '/api/company?id=MSFT&as_of=2020',
                     '/api/report?company=MSFT&format=pdf', '/api/report?company=../../a',
                     '/api/source?id=' + 'a'*181]:
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 400)
        self.assertEqual(self.request('/api/company?id=UNKNOWN')[0], 404)
        self.assertEqual(self.request('/api/source?id=unknown')[0], 404)

    def test_static_safety_headers(self):
        for path in ['/', '/workbench-app.js', '/workbench-reader.js', '/workbench-shared.js',
                     '/workbench-base.css', '/workbench-app.css', '/research-navigation.js',
                     '/vendor/echarts.min.js', '/vendor/echarts.LICENSE.txt', '/vendor/echarts.NOTICE.txt',
                     '/report-reader.html', '/report-reader.css', '/report-reader.js', '/report-reader-bindings.js']:
            status, headers, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertTrue(body)
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
            self.assertNotIn('unsafe-inline', headers['Content-Security-Policy'])

    def test_report_reader_static_allows_only_reader_query(self):
        self.assertEqual(self.request('/report-reader.html?report_id=report_abc')[0], 200)
        self.assertEqual(self.request('/report-reader.html?conversation_id=c&message_id=m')[0], 200)
        self.assertEqual(self.request('/report-reader.html?report_id=report_abc&debug=1')[0], 400)
        self.assertEqual(self.request('/workbench-app.js?report_id=report_abc')[0], 400)
        self.assertEqual(self.request('/report-reader.html?report_id=report_abc&page=2&reading=two')[0], 200)
        for suffix in ('page=0', 'page=-1', 'page=10001', 'page=2.5', 'page=2&page=3', 'reading=debug'):
            self.assertEqual(self.request('/report-reader.html?report_id=report_abc&' + suffix)[0], 400)

    def test_claim_to_source_full_chain(self):
        company = json.loads(self.request('/api/company?id=MSFT')[2])
        calculation = next(c for c in company['claims'] if c['kind'] == 'calculation')
        bundle = json.loads(self.request('/api/evidence?id=' + calculation['claim_id'])[2])
        self.assertGreaterEqual(len(bundle['activities']), 3)
        for evidence in bundle['evidence']:
            source = json.loads(self.request('/api/source?id=' + evidence['source_id'])[2])
            start, end = evidence['locator']['start'], evidence['locator']['end']
            self.assertEqual(source['text'][start:end], evidence['quote'])
            self.assertEqual(source['source']['sha256'], evidence['source_sha256'])

    def test_unreviewed_reports_have_provenance(self):
        status, headers, body = self.request('/api/report?company=MSFT&format=json')
        report = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(report['status'], 'unreviewed')
        self.assertTrue(report['activities'])
        self.assertTrue(report['sources'])
        self.assertTrue(report['exported_at'])
        status, headers, body = self.request('/api/report?company=AAPL&format=html')
        self.assertEqual(status, 200)
        self.assertIn('attachment;', headers['Content-Disposition'])
        self.assertIn(b'Unreviewed research brief', body)
        self.assertIn(b'adjusted basis', body)
        self.assertIn(b'SHA-256', body)
        self.assertIn(b'evidence_chains', body)

    def test_report_html_escapes_injection(self):
        report = deepcopy(self.server.service.report('AAPL'))
        report['exported_at'] = 'test'
        report['company']['name'] = '<img src=x onerror=alert(1)>'
        report['claims'][0]['text'] = '<script>alert(1)</script>'
        report['evidence'][0]['quote'] = '</blockquote><script>alert(2)</script>'
        report['sources'][0]['title'] = '<b>unsafe title</b>'
        report['sources'][0]['url'] = 'javascript:alert(1)'
        html = report_html(report)
        self.assertNotIn('<script>', html)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<img ', html)
        self.assertNotIn('<b>unsafe', html)
        self.assertNotIn('href="javascript:', html)

    def test_report_html_is_a_readable_brief_for_both_companies(self):
        expectations = {
            'MSFT': ('Microsoft Corporation', 'USD millions', 'Year-over-year growth = (current value ÷ prior value − 1) × 100'),
            'AAPL': ('Apple Inc.', 'USD billions', 'Reported in issuer newsroom release; accompanying PDF not checked'),
        }
        for company_id, expected in expectations.items():
            with self.subTest(company_id=company_id):
                report = self.server.service.report(company_id)
                report['exported_at'] = '2026-09-23T14:00:00Z'
                html = report_html(report)
                self.assertIn(f'<h1>{expected[0]} ({company_id}) Research brief</h1>', html)
                self.assertIn('<strong>Status: unreviewed</strong>', html)
                self.assertIn('<dt>Report period</dt>', html)
                self.assertIn('<h2>Key conclusions</h2>', html)
                self.assertIn('<h2>Reading notes and overall limitations</h2>', html)
                self.assertIn('<h2>Conclusion evidence and basis</h2>', html)
                self.assertIn(expected[1], html)
                self.assertIn(expected[2], html)
                for claim in report['claims']:
                    self.assertIn(claim['text'], html)

    def test_report_html_keeps_limits_quotes_and_full_chain_folded(self):
        report = self.server.service.report('MSFT')
        report['exported_at'] = 'test'
        html = report_html(report)
        details_at = html.index('<details>')
        self.assertNotIn('<details open', html)
        self.assertGreater(html.index('<pre>'), details_at)
        self.assertGreater(html.index('&quot;evidence_chains&quot;'), details_at)
        self.assertGreater(html.index('SELECT fact_id,value,period_end'), details_at)
        for field in ('eps_diluted', 'operating_income', 'segment_revenue', '从SQLite读取'):
            self.assertNotIn(field, html[:details_at])
        self.assertIn('Year-over-year change is calculated from issuer-reported current and prior-year values',
                      html[:details_at])
        self.assertIn(report['evidence'][0]['quote'], html)
        self.assertIn(report['evidence'][0]['evidence_id'], html)
        self.assertIn(report['sources'][0]['sha256'], html)
        for limitation in report['limitations']:
            self.assertIn(limitation, html)
        for claim in report['claims']:
            for limitation in claim['limitations']:
                self.assertIn(limitation, html)

    def test_report_html_external_links_are_https_only(self):
        report = deepcopy(self.server.service.report('AAPL'))
        report['exported_at'] = 'test'
        report['sources'][0]['url'] = 'https://user:pass@example.test/source'
        html = report_html(report)
        self.assertNotIn('href="https://user:', html)
        report['sources'][0]['url'] = 'https://example.test/source?a=1&amp;b=2'
        html = report_html(report)
        self.assertIn('target="_blank" rel="noreferrer noopener"', html)
        self.assertIn('href="https://example.test/source?a=1&amp;amp;b=2"', html)


if __name__ == '__main__':
    unittest.main()
