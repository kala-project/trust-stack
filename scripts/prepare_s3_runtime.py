#!/usr/bin/env python3
"""Validate cached S3 wheels; optionally install a NEW isolated offline venv.

No dependency resolution against a registry, downloads, or existing-venv changes.
This deliberately does not install into the fixed Copilot runtime.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.prepare_copilot_runtime import LOCK_LINE, PreparationError, normalized, validate_wheel


def validate_inputs(policy, lock_text, wheel_dir):
    if (policy.get('sdk_installation_allowed') is not True
            or policy.get('sdk_installation_scope') != 'isolated-local-S3-SDK-only'):
        raise PreparationError('S3 SDK is not approved for this scope')
    locked = {}
    for line in lock_text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = LOCK_LINE.fullmatch(line)
        if not match:
            raise PreparationError('non-exact S3 lock entry')
        name, version, digest = match.groups()
        name = normalized(name)
        if name in locked:
            raise PreparationError('duplicate lock entry')
        locked[name] = (version, digest)
    approved = policy.get('approved_wheels', [])
    if not locked or len(approved) != len(locked) or {normalized(w['name']) for w in approved} != set(locked):
        raise PreparationError('approved wheel set differs from lock')
    reviewed = datetime.fromisoformat(policy['reviewed_at'])
    cutoff = reviewed - timedelta(days=policy['minimum_release_age_days'])
    for w in approved:
        identity = (w['version'], w['sha256'])
        if locked[normalized(w['name'])] != identity or w.get('yanked') is not False:
            raise PreparationError('unapproved or yanked wheel')
        if not w['url'].startswith('https://files.pythonhosted.org/'):
            raise PreparationError('unapproved wheel host')
        uploaded = datetime.fromisoformat(w['uploaded_at'].replace('Z', '+00:00'))
        if uploaded > cutoff:
            exceptions = [e for e in policy.get('age_exceptions', [])
                          if (e.get('name'), e.get('version'), e.get('sha256'))
                          == (w['name'], *identity) and e.get('authorization')]
            if len(exceptions) != 1:
                raise PreparationError('wheel has no exact authorized age exception')
        filename = w['filename']
        if Path(filename).name != filename or not filename.endswith('.whl'):
            raise PreparationError('invalid wheel filename')
        path = wheel_dir / filename
        if path.is_symlink() or not path.is_file():
            raise PreparationError('wheel must be a regular cached file')
        validate_wheel(path, {'name': w['name'], 'version': w['version'],
                            'artifact': {'sha256': w['sha256']}, 'license_expression': w['license']})
        with zipfile.ZipFile(path) as z:
            if len(z.namelist()) != len(set(z.namelist())):
                raise PreparationError('duplicate wheel member')
            for item in z.infolist():
                if (stat.S_ISLNK(item.external_attr >> 16) or '\\' in item.filename
                        or item.filename.endswith(('.pth', 'entry_points.txt'))):
                    raise PreparationError('unreviewed executable wheel hook or member')
    return locked


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true')
    args = parser.parse_args(argv)
    try:
        policy = json.loads((ROOT / 'config/s3-dependency-policy.json').read_text())
        lock = ROOT / 'requirements-s3.lock'
        wheels = ROOT / 'var/s3-runtime/supply-chain/wheels'
        validate_inputs(policy, lock.read_text(), wheels)
        if args.install:
            if sys.version_info[:3] != (3, 14, 4) or sys.platform != 'linux' or platform.machine() != 'x86_64':
                raise PreparationError('reviewed target is CPython 3.14.4 Linux x86_64')
            venv = ROOT / 'var/s3-runtime/venv'
            if venv.exists() or venv.is_symlink():
                raise PreparationError('refusing to replace an existing S3 venv')
            uv = shutil.which('uv')
            if not uv:
                raise PreparationError('existing uv is required')
            subprocess.run([uv, 'venv', '--offline', '--python', sys.executable, str(venv)], check=True)
            python = venv / 'bin/python'
            subprocess.run([uv, 'pip', 'install', '--offline', '--python', str(python), '--no-index',
                            '--find-links', str(wheels), '--only-binary', ':all:', '--require-hashes',
                            '-r', str(lock)], check=True)
            subprocess.run([uv, 'pip', 'check', '--python', str(python)], check=True)
            subprocess.run([str(python), '-I', '-c', "import boto3, urllib3; assert boto3.__version__ == '1.43.86'; assert urllib3.__version__ == '2.8.0'"], check=True)
        print('S3 cached wheels verified; isolated installation completed' if args.install else 'S3 cached wheels verified; no installation requested')
    except (PreparationError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'FAIL: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
