#!/usr/bin/env python3
"""Inspect ObjectStore migration manifests; never copy, cut over, or delete data."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from workbench.object_migration import MigrationError, manifest_diff, manifest_sha256, validate_manifest


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("manifest", type=Path)
    diff = commands.add_parser("diff")
    diff.add_argument("baseline", type=Path)
    diff.add_argument("candidate", type=Path)
    complete = commands.add_parser("completion")
    complete.add_argument("manifest", type=Path)
    complete.add_argument("progress", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            manifest = _load(args.manifest)
            result = {"status": "valid", "objects": len(validate_manifest(manifest)),
                      "manifest_sha256": manifest_sha256(manifest)}
        elif args.command == "diff":
            result = manifest_diff(_load(args.baseline), _load(args.candidate))
            result["status"] = "diff"
        else:
            manifest, progress = _load(args.manifest), _load(args.progress)
            valid = (progress.get("complete") is True
                     and progress.get("generation") == manifest.get("generation")
                     and progress.get("manifest_sha256") == manifest_sha256(manifest))
            if not valid:
                raise MigrationError("completion does not bind the whole candidate manifest")
            result = {"status": "complete", "objects": len(validate_manifest(manifest))}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
