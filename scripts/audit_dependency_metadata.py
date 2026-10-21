#!/usr/bin/env python3
"""Offline-first validation of frozen PyPI JSON against dependency policy.

Only the explicit ``audit`` subcommand performs network requests. It retrieves
public PyPI JSON metadata; it never installs a package or executes vendor code.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen


EXACT_VERSION = re.compile(r"^[0-9]+(?:[._+-]?[A-Za-z0-9]+)*$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AuditError(ValueError):
    """A policy or metadata validation failure."""


def _timestamp(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise AuditError(f"invalid timestamp: {value!r}") from exc


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _is_exact_version(value: object) -> bool:
    return isinstance(value, str) and bool(EXACT_VERSION.fullmatch(value))


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise AuditError(f"JSON root must be an object: {path}")
    return value


def policy_candidates(policy: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = policy.get("candidates")
    if not isinstance(candidates, list):
        raise AuditError("policy candidates must be a list")
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise AuditError("each policy candidate must be an object")
        if not _is_exact_version(candidate.get("version")):
            raise AuditError(
                f"{candidate.get('name', '<unnamed>')}: version must be exact"
            )
    return candidates


def _release_files(metadata: dict[str, Any], version: str) -> list[dict[str, Any]]:
    releases = metadata.get("releases")
    if isinstance(releases, dict):
        files = releases.get(version, [])
    else:
        files = metadata.get("urls", [])
    if not isinstance(files, list):
        raise AuditError(f"{version}: release files must be a list")
    return [item for item in files if isinstance(item, dict)]


def validate_candidate(
    policy: dict[str, Any], candidate: dict[str, Any], metadata: dict[str, Any]
) -> None:
    """Validate one frozen PyPI JSON document without using the network."""

    name = candidate.get("name")
    version = candidate.get("version")
    if not isinstance(name, str) or not name:
        raise AuditError("candidate name is required")
    if not _is_exact_version(version):
        raise AuditError(f"{name}: version must be exact")

    info = metadata.get("info")
    if not isinstance(info, dict):
        raise AuditError(f"{name}: metadata info is missing")
    if _normalized_name(str(info.get("name", ""))) != _normalized_name(name):
        raise AuditError(f"{name}: metadata package name does not match")
    if info.get("version") != version:
        raise AuditError(
            f"{name}: metadata version {info.get('version')!r} is not {version!r}"
        )

    artifact = candidate.get("artifact")
    if not isinstance(artifact, dict):
        raise AuditError(f"{name}=={version}: artifact policy is missing")
    filename = artifact.get("filename")
    matches = [
        item
        for item in _release_files(metadata, version)
        if item.get("filename") == filename
    ]
    if len(matches) != 1:
        raise AuditError(f"{name}=={version}: expected exactly one {filename!r} artifact")
    published = matches[0]

    if published.get("yanked") is not False:
        raise AuditError(f"{name}=={version}: artifact is yanked or yanked state is missing")
    if artifact.get("yanked") is not False:
        raise AuditError(f"{name}=={version}: policy must explicitly require yanked=false")

    digests = published.get("digests")
    actual_hash = digests.get("sha256") if isinstance(digests, dict) else None
    expected_hash = artifact.get("sha256")
    if not isinstance(actual_hash, str) or not SHA256.fullmatch(actual_hash):
        raise AuditError(f"{name}=={version}: published SHA-256 is missing or invalid")
    if not isinstance(expected_hash, str) or not SHA256.fullmatch(expected_hash):
        raise AuditError(f"{name}=={version}: policy SHA-256 is missing or invalid")
    if actual_hash != expected_hash:
        raise AuditError(f"{name}=={version}: SHA-256 does not match policy")

    upload = published.get("upload_time_iso_8601")
    if upload != artifact.get("upload_time_iso_8601"):
        raise AuditError(f"{name}=={version}: upload timestamp does not match policy")
    cutoff = policy.get("latest_allowed_upload_cutoff")
    if _timestamp(upload) > _timestamp(cutoff):
        raise AuditError(
            f"{name}=={version}: artifact is newer than age cutoff {cutoff}"
        )

    for field in ("packagetype",):
        if published.get(field) != artifact.get(field):
            raise AuditError(f"{name}=={version}: {field} does not match policy")

    observed = candidate.get("observed_requires_dist", [])
    requires_dist = info.get("requires_dist") or []
    if sorted(requires_dist) != sorted(observed):
        raise AuditError(f"{name}=={version}: requires_dist changed from frozen policy")
    if info.get("requires_python") != candidate.get("requires_python"):
        raise AuditError(f"{name}=={version}: requires_python changed from frozen policy")


def validate_policy(
    policy: dict[str, Any], metadata_by_name: dict[str, dict[str, Any]]
) -> None:
    candidates = policy_candidates(policy)
    expected = {_normalized_name(item["name"]) for item in candidates}
    supplied = {_normalized_name(name) for name in metadata_by_name}
    if supplied != expected:
        raise AuditError(
            f"metadata set does not match candidates; expected {sorted(expected)}, got {sorted(supplied)}"
        )
    for candidate in candidates:
        validate_candidate(
            policy, candidate, metadata_by_name[_normalized_name(candidate["name"])]
        )


def fetch_public_metadata(name: str, version: str) -> dict[str, Any]:
    """Fetch public metadata. Called only by the explicit audit operation."""

    url = (
        f"https://pypi.org/pypi/{quote(name, safe='')}/"
        f"{quote(version, safe='')}/json"
    )
    request = Request(url, headers={"User-Agent": "trust-stack-metadata-audit/1"})
    with urlopen(request, timeout=20) as response:
        value = json.load(response)
    if not isinstance(value, dict):
        raise AuditError(f"{name}=={version}: PyPI returned a non-object")
    return value


def _metadata_arguments(values: list[str]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for value in values:
        if "=" not in value:
            raise AuditError("metadata must use PACKAGE=FILE")
        name, path = value.split("=", 1)
        result[_normalized_name(name)] = load_json(path)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    verify = subparsers.add_parser("verify", help="verify frozen metadata offline")
    verify.add_argument("--policy", required=True)
    verify.add_argument(
        "--metadata", action="append", default=[], metavar="PACKAGE=FILE"
    )
    audit = subparsers.add_parser("audit", help="explicitly fetch and audit public PyPI JSON")
    audit.add_argument("--policy", required=True)
    args = parser.parse_args(argv)

    try:
        policy = load_json(args.policy)
        if args.operation == "verify":
            metadata = _metadata_arguments(args.metadata)
        else:
            metadata = {
                _normalized_name(item["name"]): fetch_public_metadata(
                    item["name"], item["version"]
                )
                for item in policy_candidates(policy)
            }
        validate_policy(policy, metadata)
    except (AuditError, OSError, json.JSONDecodeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("METADATA MATCH: frozen version, age and artifact metadata only; not an installation authorization")
    candidates = policy_candidates(policy)
    approved = policy.get("approved_packages", [])
    blocked = [item["name"] for item in candidates if (
        item.get("decision") != "approved"
        or item.get("blockers")
        or not any(isinstance(entry, dict)
                   and entry.get("name") == item["name"]
                   and entry.get("version") == item["version"]
                   and entry.get("sha256") == item["artifact"]["sha256"]
                   for entry in approved)
        or (item.get("embedded_runtime_reference") is not None
            and item["embedded_runtime_reference"].get("approved") is not True)
    )]
    if policy.get("default_decision") != "deny" or blocked:
        print("BLOCKED: deny-by-default admission policy or required package/runtime reviews are incomplete: "
              + ", ".join(blocked), file=sys.stderr)
        return 2
    print("Policy records approval; this metadata check does not replace full lock, security and runtime verification.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
