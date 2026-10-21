import hashlib
from pathlib import Path
import tempfile
import unittest

from workbench.documents import DocumentError, DocumentService


class DocumentBomTests(unittest.TestCase):
    def test_utf8_bom_retains_exact_original_and_is_searchable(self):
        raw = b'\xef\xbb\xbf<!DOCTYPE html><html><body><h1>Annual report</h1><p>Revenue and cash flows disclosed.</p></body></html>'
        with tempfile.TemporaryDirectory() as directory:
            service = DocumentService(Path(directory))
            record = service.register_bytes(raw, company_id='EXAMPLE', source_key='annual',
                                            title='Annual report', media_type='text/html')
            self.assertEqual(record['parse_status'], 'parsed')
            self.assertEqual(record['source_sha256'], hashlib.sha256(raw).hexdigest())
            self.assertEqual(service.read_raw_version(company_id='EXAMPLE',
                             document_version_id=record['document_version_id']), raw)
            found = service.search_documents(company_id='EXAMPLE', query='revenue',
                                             document_version_ids=[record['document_version_id']])
            self.assertTrue(found['results'])

    def test_bom_does_not_allow_wrong_media_or_non_html(self):
        for raw, media in [(b'\xef\xbb\xbf<html></html>', 'application/pdf'),
                           (b'\xef\xbb\xbfnot an HTML document', 'text/html'),
                           ('<html></html>'.encode('utf-16'), 'text/html'),
                           (b'\xef\xbb\xbf\xff<html></html>', 'text/html'),
                           (b'\xef\xbb\xbf%PDF-not-valid', 'application/pdf')]:
            with self.subTest(raw=raw, media=media), self.assertRaises(DocumentError):
                DocumentService._validate_media(raw, media)


if __name__ == '__main__':
    unittest.main()
