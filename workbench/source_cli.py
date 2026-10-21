"""Administrator-only CLI for configured source archival; never an Agent URL tool."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

from workbench.source_archive import SourceArchive, SourceArchiveError, load_source_config

ROOT = Path(__file__).resolve().parents[1]


def _write_new_file(path: Path, raw: bytes) -> None:
    """Publish complete bytes atomically, without replacing any directory entry."""
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    published = False
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise SourceArchiveError("output_exists", "export output already exists") from None
        published = True
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            if not published:
                raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "var/documents")
    parser.add_argument("--config", type=Path, required=True, help="Administrator-maintained exact source configuration JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    fetch = sub.add_parser("fetch", help="Use a verified local version by default; --refresh performs HTTPS")
    fetch.add_argument("source_id")
    fetch.add_argument("--refresh", action="store_true")

    local = sub.add_parser("import-local", help="Archive a pre-acquired regular file after mandatory hash and size checks")
    local.add_argument("source_id")
    local.add_argument("path", type=Path)
    local.add_argument("--expected-sha256", required=True)
    local.add_argument("--expected-size", required=True, type=int)
    local.add_argument("--media-type", required=True)
    local.add_argument("--fetched-at")

    history = sub.add_parser("history", help="List successful immutable versions and all acquisition attempts")
    history.add_argument("source_id")

    links = sub.add_parser("list-links", help="List offline link candidates from one archived HTML version")
    links.add_argument("source_id")
    links.add_argument("--source-version", required=True, type=int)
    links.add_argument("--limit", type=int, default=100)

    export = sub.add_parser("export", help="Verify and export one immutable version to a new file")
    export.add_argument("source_id")
    export.add_argument("--source-version", required=True, type=int)
    export.add_argument("--output", required=True, type=Path)

    enroll = sub.add_parser("enroll", help="Explicitly enroll one archived HTML/PDF version in DocumentService")
    enroll.add_argument("source_id")
    enroll.add_argument("--source-version", required=True, type=int)

    args = parser.parse_args(argv)
    try:
        archive = SourceArchive(args.root)
        config = load_source_config(args.config, args.source_id)
        if args.command == "fetch":
            result = archive.fetch(config, refresh=args.refresh)
        elif args.command == "import-local":
            result = archive.import_local(
                config, args.path, expected_sha256=args.expected_sha256,
                expected_size=args.expected_size, media_type=args.media_type,
                fetched_at=args.fetched_at,
            )
        elif args.command == "history":
            result = archive.history(args.source_id)
        elif args.command == "list-links":
            result = archive.list_link_candidates(
                args.source_id, source_version=args.source_version, limit=args.limit)
        elif args.command == "export":
            raw = archive.read(args.source_id, source_version=args.source_version)
            _write_new_file(args.output, raw)
            result = {
                "source_id": args.source_id,
                "source_version": args.source_version,
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "byte_length": len(raw),
                "output_path": str(args.output),
            }
        else:
            result = archive.enroll(args.source_id, source_version=args.source_version)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (SourceArchiveError, OSError, ValueError, TypeError) as exc:
        error = exc.as_dict() if isinstance(exc, SourceArchiveError) else {
            "error": "source_operation_failed", "message": "source operation failed safely"
        }
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
