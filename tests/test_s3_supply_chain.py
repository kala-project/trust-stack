"""Offline policy tests; synthetic wheels never imported or installed."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.prepare_s3_runtime import PreparationError, validate_inputs


class S3AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.wheel = self.root / 'review_test-1.0.0-py3-none-any.whl'
        with zipfile.ZipFile(self.wheel, 'w') as z:
            z.writestr('review_test-1.0.0.dist-info/METADATA',
                       'Name: review-test\nVersion: 1.0.0\nLicense: MIT\n')
        self.digest = hashlib.sha256(self.wheel.read_bytes()).hexdigest()
        self.entry = dict(name='review-test', version='1.0.0', sha256=self.digest,
                          filename=self.wheel.name, yanked=False, license='MIT',
                          url='https://files.pythonhosted.org/packages/test.whl',
                          uploaded_at='2026-08-01T00:00:00Z')
        self.policy = dict(sdk_installation_allowed=True,
                           sdk_installation_scope='isolated-local-S3-SDK-only',
                           reviewed_at='2026-10-02T00:00:00+00:00',
                           minimum_release_age_days=30, approved_wheels=[self.entry])
        self.lock = f'review-test==1.0.0 --hash=sha256:{self.digest}\n'

    def check_policy(self):
        return validate_inputs(self.policy, self.lock, self.root)

    def test_exact_cached_wheel(self):
        self.assertEqual(self.check_policy(), {'review-test': ('1.0.0', self.digest)})

    def test_denied_scope(self):
        for key, value in [('sdk_installation_allowed', False), ('sdk_installation_scope', 'production')]:
            p = copy.deepcopy(self.policy)
            p[key] = value
            with self.assertRaises(PreparationError):
                validate_inputs(p, self.lock, self.root)

    def test_age_exception_requires_exact_name_version_hash_and_authorization(self):
        self.entry['uploaded_at'] = '2026-09-15T00:00:00Z'
        with self.assertRaises(PreparationError): self.check_policy()
        exception = dict(name='review-test', version='1.0.0', sha256=self.digest, authorization='test only')
        self.policy['age_exceptions'] = [exception]
        self.check_policy()
        for key in ['name', 'version', 'sha256', 'authorization']:
            original = exception[key]
            exception[key] = ''
            with self.assertRaises(PreparationError): self.check_policy()
            exception[key] = original

    def test_hash_mismatch(self):
        self.wheel.write_bytes(b'not the reviewed bytes')
        with self.assertRaises(PreparationError): self.check_policy()

    def test_empty_float_duplicate_and_unlisted_lock_rejected(self):
        for lock in ['', 'review-test>=1.0.0', self.lock * 2, self.lock + f'extra==1 --hash=sha256:{self.digest}']:
            with self.assertRaises(PreparationError):
                validate_inputs(self.policy, lock, self.root)

    def test_yanked_host_and_path_rejected(self):
        for key, value in [('yanked', True), ('url', 'https://other.example/test.whl'), ('filename', '../escape.whl')]:
            original = self.entry[key]
            self.entry[key] = value
            with self.assertRaises(PreparationError): self.check_policy()
            self.entry[key] = original

    def test_pth_rejected_even_if_hash_updated(self):
        with zipfile.ZipFile(self.wheel, 'a') as z: z.writestr('startup.pth', 'import unreviewed')
        self.entry['sha256'] = hashlib.sha256(self.wheel.read_bytes()).hexdigest()
        self.lock = f"review-test==1.0.0 --hash=sha256:{self.entry['sha256']}"
        with self.assertRaises(PreparationError): self.check_policy()


if __name__ == '__main__':
    unittest.main()
