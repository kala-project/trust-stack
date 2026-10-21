"""Administrator-only, offline document enrollment. Never exposed as an Agent tool."""
from __future__ import annotations

import argparse
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import re
import sys

from .documents import DocumentError, DocumentService

ROOT = Path(__file__).resolve().parents[1]
# Explicit research scope; website terms remain in the original source catalog,
# not presented as financial disclosures. Do not infer company ownership via claims.
OFFICIAL_SOURCES = {
    "msft-fy2025-q4-press-release": "MSFT",
    "msft-fy2025-q4-segment-revenues": "MSFT",
    "msft-fy2024-q4-press-release": "MSFT",
    "msft-fy2024-q4-segment-revenues-original-page": "MSFT",
    "aapl-fy2025-q4-press-release": "AAPL",
    "nvda-fy2025-q4-press-release": "NVDA",
    "nvda-fy2024-q4-press-release": "NVDA",
    "crm-fy2025-q4-press-release": "CRM",
    "crm-fy2024-q4-press-release": "CRM",
    "ibm-2024-q4-results": "IBM",
    "ibm-2025-q4-results": "IBM",
}


def checked_date(value, *, timestamp=False):
    if value is None:
        return None
    try:
        if not isinstance(value, str):
            raise ValueError()
        if timestamp:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", value):
                raise ValueError()
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            date.fromisoformat(value)
        else:
            return checked_date(value, timestamp=True)
    except ValueError as exc:
        raise DocumentError("invalid_date", "Use YYYY-MM-DD for publication or an ISO timestamp with timezone; collection requires a timestamp") from exc
    return value


def bootstrap(root, *, catalog=ROOT / "data/source-catalog.json", workspace=ROOT):
    """Import only the explicit issuer documents, with verified catalog dates.

    Preflight all artifacts before writing; each registration commits independently.
    A disk failure can leave a partial import; re-running resumes idempotently.
    """
    payload = json.loads(Path(catalog).read_text(encoding="utf-8"))
    entries = payload["sources"]
    by_id = {entry["id"]: entry for entry in entries}
    if len(by_id) != len(entries) or not OFFICIAL_SOURCES.keys() <= by_id.keys():
        raise DocumentError("invalid_manifest", "Official source catalog is incomplete or has duplicate source IDs")
    workspace = Path(workspace).resolve()
    prepared = []
    for source_key, company_id in OFFICIAL_SOURCES.items():
        entry = by_id[source_key]
        artifact = entry.get("artifact")
        if not artifact or entry["source_type"] not in {"issuer_earnings_release_html", "issuer_segment_table_html"}:
            raise DocumentError("invalid_manifest", "Expected a saved issuer HTML artifact")
        path = workspace / artifact["path"]
        if not path.resolve().is_relative_to(workspace / "data/raw"):
            raise DocumentError("invalid_manifest", "Official artifact must be within the workspace raw-data directory")
        raw = DocumentService._read_input(path)
        if hashlib.sha256(raw).hexdigest() != artifact["sha256"] or len(raw) != artifact["bytes"]:
            raise DocumentError("source_hash_mismatch", "Official artifact no longer matches its catalog")
        DocumentService._validate_media(raw, "text/html")
        prepared.append((path, dict(
            company_id=company_id, source_key=source_key, title=entry["title"],
            url=entry["url"], published_at=checked_date(entry.get("published_at")),
            collected_at=checked_date(artifact.get("collected_at"), timestamp=True),
            media_type="text/html", rights_status=entry["rights"]["assessment"],
            expected_sha256=artifact["sha256"], expected_byte_length=artifact["bytes"],
        )))
    service = DocumentService(root)
    records = [service.register_file(path, **options) for path, options in prepared]
    return {"records": records, "excluded": [
        {"source_key": entry["id"], "reason": "terms_not_research_evidence" if entry["source_type"] == "website_terms_html" else "not_saved_or_outside_explicit_import_scope"}
        for entry in entries if entry["id"] not in OFFICIAL_SOURCES
    ], "coverage": {company: service.get_document_coverage(company_id=company)
                     for company in sorted(set(OFFICIAL_SOURCES.values()))}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT / "var/documents")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("bootstrap", help="Import the explicit catalog-verified local issuer HTML documents; no network")
    add = sub.add_parser("register", help="Save one local HTML/PDF version and index supported HTML")
    add.add_argument("path", type=Path)
    add.add_argument("--company", required=True)
    add.add_argument("--source-key", required=True, help="Stable logical source ID, reused for later versions")
    add.add_argument("--title", required=True)
    add.add_argument("--url")
    add.add_argument("--published-at")
    add.add_argument("--collected-at", help="Known acquisition timestamp only; omitted remains unknown")
    add.add_argument("--rights-status", default="local_only")
    add.add_argument("--media-type", choices=["text/html", "application/pdf"])
    coverage = sub.add_parser("coverage")
    coverage.add_argument("--company", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "bootstrap":
            result = bootstrap(args.root)
        elif args.command == "register":
            published_at = checked_date(args.published_at)
            collected_at = checked_date(args.collected_at, timestamp=True)
            result = DocumentService(args.root).register_file(
                args.path, company_id=args.company, source_key=args.source_key, title=args.title,
                url=args.url, published_at=published_at, collected_at=collected_at,
                rights_status=args.rights_status, media_type=args.media_type,
            )
        else:
            if not (args.root / "catalog.sqlite3").is_file():
                raise DocumentError("documents_unavailable", "No document catalog has been enrolled")
            result = DocumentService(args.root).get_document_coverage(company_id=args.company)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (DocumentError, OSError, ValueError, KeyError, TypeError) as exc:
        error = exc.as_dict() if isinstance(exc, DocumentError) else {"error": "import_failed", "message": "Check local source accessibility and manifest format"}
        print(json.dumps(error, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
