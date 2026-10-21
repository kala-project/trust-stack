#!/usr/bin/env python3
"""Download, validate, and enroll the public three-scenario test-data bundle."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from workbench.parser import ParseError, parse_html  # noqa: E402
from workbench.source_archive import (  # noqa: E402
    SourceArchive,
    SourceArchiveError,
    load_source_config,
)

DEFAULT_CONFIG = ROOT / "config/public-test-data.json"
DEFAULT_ROOT = ROOT / "var/documents"
_SCHEMA = "public-test-data-bundle/1"
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_bundle(path: Path) -> dict[str, Any]:
    try:
        bundle = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("bundle configuration cannot be read") from exc
    if not isinstance(bundle, dict) or bundle.get("schema_version") != _SCHEMA:
        raise ValueError(f"bundle must use {_SCHEMA}")
    sources = bundle.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("bundle sources must be a non-empty list")
    identifiers: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("bundle source entries must be objects")
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or not source_id or source_id in identifiers:
            raise ValueError("bundle source_id values must be unique non-empty strings")
        identifiers.add(source_id)
        validation = source.get("bootstrap_validation")
        if not isinstance(validation, dict):
            raise ValueError(f"{source_id}: bootstrap_validation is required")
        exact = validation.get("exact_sha256")
        reference = validation.get("reference_sha256")
        markers = validation.get("required_text")
        if exact is not None and (not isinstance(exact, str) or _SHA256.fullmatch(exact) is None):
            raise ValueError(f"{source_id}: exact_sha256 must be a lowercase SHA-256")
        if reference is not None and (not isinstance(reference, str)
                                      or _SHA256.fullmatch(reference) is None):
            raise ValueError(f"{source_id}: reference_sha256 must be a lowercase SHA-256")
        if markers is not None and (not isinstance(markers, list) or not markers
                                    or any(not isinstance(item, str) or not item for item in markers)):
            raise ValueError(f"{source_id}: required_text must contain non-empty strings")
        if exact is None and markers is None:
            raise ValueError(f"{source_id}: exact_sha256 or required_text is required")
        if markers is not None:
            minimum = validation.get("minimum_bytes")
            maximum = validation.get("maximum_bytes")
            visible = validation.get("minimum_visible_chars")
            if (type(minimum) is not int or type(maximum) is not int
                    or type(visible) is not int or not 1 <= minimum <= maximum
                    or visible < 1):
                raise ValueError(
                    f"{source_id}: dynamic HTML requires valid byte and visible-text bounds")
    return bundle


def _validate_identity(source: dict[str, Any], raw: bytes, media_type: str) -> dict[str, Any]:
    validation = source["bootstrap_validation"]
    source_id = source["source_id"]
    digest = hashlib.sha256(raw).hexdigest()
    exact = validation.get("exact_sha256")
    if exact is not None:
        if media_type != "application/pdf" or not raw.startswith(b"%PDF-"):
            raise ValueError(f"{source_id}: pinned document is not a PDF")
        if digest != exact:
            raise ValueError(f"{source_id}: downloaded bytes differ from the pinned public document")
    markers = validation.get("required_text")
    if markers is not None:
        if media_type not in {"text/html", "application/xhtml+xml"}:
            raise ValueError(f"{source_id}: publisher page is not HTML")
        minimum, maximum = validation["minimum_bytes"], validation["maximum_bytes"]
        if not minimum <= len(raw) <= maximum:
            raise ValueError(f"{source_id}: publisher page size is outside its reviewed range")
        markup = raw.decode("utf-8", "replace").casefold()
        if "<html" not in markup and "<!doctype html" not in markup:
            raise ValueError(f"{source_id}: publisher response has no HTML document structure")
        try:
            visible_text = parse_html(raw).text.casefold()
        except ParseError as exc:
            raise ValueError(f"{source_id}: publisher page cannot be parsed as visible HTML") from exc
        if len(visible_text) < validation["minimum_visible_chars"]:
            raise ValueError(f"{source_id}: publisher page has too little visible report text")
        challenge_signatures = (
            "just a moment", "attention required", "access denied", "verify you are human",
            "enable cookies to continue", "request unsuccessful", "captcha", "page not found",
            "404 not found", "temporarily unavailable",
        )
        if any(signature in visible_text for signature in challenge_signatures):
            raise ValueError(f"{source_id}: publisher returned an error or access-challenge page")
        missing = [item for item in markers if item.casefold() not in visible_text]
        if missing:
            raise ValueError(f"{source_id}: downloaded publisher page is missing required identity text")
    reference = validation.get("reference_sha256")
    return {
        "source_sha256": digest,
        "byte_length": len(raw),
        "identity_policy": "exact_sha256" if exact is not None else "publisher_text",
        "changed_from_reference": isinstance(reference, str) and reference != digest,
    }


def _validate_download(source: dict[str, Any], result: dict[str, Any], raw: bytes) -> dict[str, Any]:
    source_id = source["source_id"]
    digest = result.get("source_sha256")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise ValueError(f"{source_id}: archive returned an invalid SHA-256")
    if len(raw) != result.get("byte_length") or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f"{source_id}: archived bytes changed before validation")
    return _validate_identity(source, raw, result.get("media_type", ""))


def _atomic_report(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(encoded)
        handle.flush()
    temporary.replace(path)


def bootstrap(*, root: Path, config_path: Path, refresh: bool,
              enroll: bool, selected: set[str] | None = None) -> tuple[int, dict[str, Any]]:
    bundle = _load_bundle(config_path)
    configured = [item for item in bundle["sources"]
                  if selected is None or item["source_id"] in selected]
    known = {item["source_id"] for item in bundle["sources"]}
    unknown = sorted((selected or set()) - known)
    if unknown:
        raise ValueError("unknown source selections: " + ", ".join(unknown))
    archive = SourceArchive(root)
    outcomes: list[dict[str, Any]] = []
    failed = 0
    for source in configured:
        source_id = source["source_id"]
        try:
            normalized = load_source_config(config_path, source_id)

            def validate_before_commit(raw, media_type, final_url):
                try:
                    _validate_identity(source, raw, media_type)
                except ValueError as exc:
                    raise SourceArchiveError("source_identity_mismatch", str(exc)) from exc

            fetched = archive.fetch(
                normalized, refresh=refresh, precommit_validator=validate_before_commit)
            raw = archive.read(source_id, source_version=fetched["source_version"])
            checked = _validate_download(source, fetched, raw)
            enrollment = archive.enroll(
                source_id, source_version=fetched["source_version"]) if enroll else None
            parse_status = (enrollment.get("parse_status")
                            if isinstance(enrollment, dict) else None)
            if enroll and parse_status != "parsed":
                failed += 1
                outcomes.append({
                    "source_id": source_id,
                    "company_id": source.get("company_id"),
                    "status": "registered_unusable",
                    "source_version": fetched["source_version"],
                    "retrieval": fetched["retrieval"],
                    **checked,
                    "document_version_id": enrollment.get("document_version_id"),
                    "parse_status": parse_status,
                    "error": "document_not_searchable",
                    "message": "document was archived but its text was not parsed for research",
                })
                continue
            outcomes.append({
                "source_id": source_id,
                "company_id": source.get("company_id"),
                "status": "enrolled" if enroll else "archived",
                "source_version": fetched["source_version"],
                "retrieval": fetched["retrieval"],
                **checked,
                "document_version_id": (
                    enrollment.get("document_version_id") if isinstance(enrollment, dict) else None),
                "parse_status": parse_status,
            })
        except (SourceArchiveError, OSError, ValueError, TypeError) as exc:
            failed += 1
            outcomes.append({
                "source_id": source_id,
                "company_id": source.get("company_id"),
                "status": "failed",
                "error": exc.code if isinstance(exc, SourceArchiveError) else "validation_failed",
                "message": exc.message if isinstance(exc, SourceArchiveError) else str(exc),
            })
    report = {
        "schema_version": "public-test-data-bootstrap-report/1",
        "bundle_id": bundle.get("bundle_id"),
        "bundle_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "generated_at": _now(),
        "root": str(root.resolve()),
        "refresh_requested": refresh,
        "enrollment_requested": enroll,
        "summary": {
            "requested": len(configured),
            "succeeded": len(configured) - failed,
            "failed": failed,
            "validated_bytes": sum(item.get("byte_length", 0) for item in outcomes),
            "searchable": sum(item.get("parse_status") == "parsed" for item in outcomes),
        },
        "sources": outcomes,
        "limitations": bundle.get("limitations", []),
    }
    return (2 if failed else 0), report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help="New or existing local archive/document root")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--refresh", action="store_true",
                        help="Re-download every source even when the exact configured source is local")
    parser.add_argument("--archive-only", action="store_true",
                        help="Validate and archive bytes without enrolling them for research")
    parser.add_argument("--source", action="append", default=[],
                        help="Fetch only one source_id; repeat to select several")
    parser.add_argument("--report", type=Path,
                        help="Write a machine-readable run report (default: ROOT/bootstrap-report.json)")
    args = parser.parse_args(argv)
    try:
        code, report = bootstrap(
            root=args.root,
            config_path=args.config,
            refresh=args.refresh,
            enroll=not args.archive_only,
            selected=set(args.source) or None,
        )
        _atomic_report(args.report or args.root / "bootstrap-report.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return code
    except (OSError, ValueError, TypeError, SourceArchiveError) as exc:
        message = exc.message if isinstance(exc, SourceArchiveError) else str(exc)
        print(json.dumps({"error": "bootstrap_failed", "message": message}, ensure_ascii=False),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
