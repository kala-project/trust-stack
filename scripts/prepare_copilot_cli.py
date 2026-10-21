#!/usr/bin/env python3
"""Fetch, verify, inspect, and install the fixed GitHub Copilot CLI artifact."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "config" / "dependency-policy.json"
RUNTIME = ROOT / "var" / "runtime-recovery"
ARCHIVE_DIR = RUNTIME / "archive"
METADATA_DIR = ARCHIVE_DIR / "metadata"
BIN_DIR = RUNTIME / "bin"

VERSION = "1.0.79"
TAG = f"v{VERSION}"
ASSET_NAME = "copilot-linux-x64.tar.gz"
ARCHIVE_NAME = f"copilot-linux-x64-{TAG}.tar.gz"
ARCHIVE_SIZE = 110_460_852
ARCHIVE_SHA256 = "9248d81ead3055c51e90067ae7adf0cfb84b2f8b456d8580f18705ae181ca624"
PUBLISHED_AT = "2026-08-10T16:19:16Z"
LICENSE_BLOB_SHA1 = "49e10861fde20635ab863373da620d99fa180a15"
RELEASE_URL = f"https://api.github.com/repos/github/copilot-cli/releases/tags/{TAG}"
ASSET_URL = f"https://github.com/github/copilot-cli/releases/download/{TAG}/{ASSET_NAME}"
SUMS_URL = f"https://github.com/github/copilot-cli/releases/download/{TAG}/SHA256SUMS.txt"
LICENSE_URL = f"https://raw.githubusercontent.com/github/copilot-cli/{TAG}/LICENSE.md"


class PreparationError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_bytes(destination: Path, value: bytes, mode: int = 0o644) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, destination)
    finally:
        temporary_path.unlink(missing_ok=True)


def download(url: str, destination: Path, timeout: int, max_size: int) -> bytes:
    """Download one fixed URL with connection and total limits, then atomically store it."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
    os.close(fd)
    temporary_path = Path(temporary)
    part_paths: list[Path] = []
    try:
        if max_size > 10_000_000:
            segments = 8
            chunk_size = (max_size + segments - 1) // segments

            def fetch_part(index: int) -> None:
                start = index * chunk_size
                end = min(max_size, start + chunk_size) - 1
                expected = end - start + 1
                part = Path(f"{temporary_path}.part{index}")
                part_paths.append(part)
                command = [
                    "curl", "--fail", "--location", "--silent", "--show-error",
                    "--proto", "=https", "--tlsv1.2", "--connect-timeout", "15",
                    "--max-time", str(timeout), "--max-filesize", str(expected),
                    "--user-agent", "trust-stack-copilot-cli-preparer/1",
                    "--range", f"{start}-{end}", "--output", str(part), url,
                ]
                subprocess.run(command, check=True, timeout=timeout + 10)
                if part.stat().st_size != expected:
                    raise PreparationError(f"range response size mismatch for bytes {start}-{end}")

            with ThreadPoolExecutor(max_workers=segments) as executor:
                list(executor.map(fetch_part, range(segments)))
            with temporary_path.open("wb") as output:
                for part in sorted(part_paths, key=lambda path: int(path.suffix.removeprefix(".part"))):
                    with part.open("rb") as source:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
        else:
            command = [
                "curl", "--fail", "--location", "--silent", "--show-error",
                "--proto", "=https", "--tlsv1.2", "--connect-timeout", "15",
                "--max-time", str(timeout), "--max-filesize", str(max_size),
                "--user-agent", "trust-stack-copilot-cli-preparer/1",
                "--output", str(temporary_path), url,
            ]
            subprocess.run(command, check=True, timeout=timeout + 10)
        size = temporary_path.stat().st_size
        if size > max_size:
            raise PreparationError(f"download exceeds size bound for {url}")
        value = b"" if max_size > 10_000_000 else temporary_path.read_bytes()
        os.chmod(temporary_path, 0o644)
        os.replace(temporary_path, destination)
        return value
    except FileNotFoundError as exc:
        raise PreparationError("curl is required for bounded HTTPS downloads") from exc
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise PreparationError(f"bounded download failed; rerun this script to retry: {url}") from exc
    finally:
        temporary_path.unlink(missing_ok=True)
        for part in part_paths:
            part.unlink(missing_ok=True)


def obtain_small(url: str, destination: Path, timeout: int, max_size: int) -> bytes:
    if destination.exists():
        value = destination.read_bytes()
        if len(value) > max_size:
            raise PreparationError(f"existing metadata exceeds size bound: {destination}")
        return value
    return download(url, destination, timeout, max_size)


def verify_public_metadata(timeout: int) -> None:
    release_path = METADATA_DIR / f"release-{TAG}.json"
    release_bytes = obtain_small(RELEASE_URL, release_path, timeout, 2_000_000)
    try:
        release = json.loads(release_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreparationError("invalid GitHub release metadata") from exc
    if release.get("tag_name") != TAG or release.get("published_at") != PUBLISHED_AT:
        raise PreparationError("release tag or publication time differs from reviewed metadata")
    matches = [item for item in release.get("assets", []) if item.get("name") == ASSET_NAME]
    if len(matches) != 1:
        raise PreparationError("release metadata does not identify exactly one reviewed asset")
    asset = matches[0]
    if asset.get("size") != ARCHIVE_SIZE or asset.get("browser_download_url") != ASSET_URL:
        raise PreparationError("release asset URL or size differs from reviewed metadata")

    sums_path = METADATA_DIR / f"SHA256SUMS-{TAG}.txt"
    sums = obtain_small(SUMS_URL, sums_path, timeout, 1_000_000).decode("utf-8")
    entries = {}
    for line in sums.splitlines():
        fields = line.strip().split()
        if len(fields) == 2 and len(fields[0]) == 64:
            entries[fields[1].lstrip("*")] = fields[0].lower()
    if entries.get(ASSET_NAME) != ARCHIVE_SHA256:
        raise PreparationError("official SHA256SUMS does not match the reviewed archive digest")

    license_path = METADATA_DIR / f"LICENSE-{TAG}.md"
    license_bytes = obtain_small(LICENSE_URL, license_path, timeout, 2_000_000)
    blob_header = f"blob {len(license_bytes)}\0".encode("ascii")
    if hashlib.sha1(blob_header + license_bytes).hexdigest() != LICENSE_BLOB_SHA1:
        raise PreparationError("LICENSE content does not match the reviewed Git blob")


def obtain_archive(timeout: int) -> Path:
    destination = ARCHIVE_DIR / ARCHIVE_NAME
    if not destination.exists():
        download(ASSET_URL, destination, timeout, ARCHIVE_SIZE)
    if destination.stat().st_size != ARCHIVE_SIZE:
        raise PreparationError("CLI archive size mismatch")
    if sha256(destination) != ARCHIVE_SHA256:
        raise PreparationError("CLI archive SHA-256 mismatch")
    return destination


def safe_member_name(name: str) -> PurePosixPath:
    value = PurePosixPath(name)
    if not name or value.is_absolute() or ".." in value.parts or "\x00" in name:
        raise PreparationError(f"unsafe tar member path: {name!r}")
    return value


def approved_binary_sha256() -> str:
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    candidates = policy.get("runtime_candidates")
    if not isinstance(candidates, list):
        raise PreparationError("runtime dependency policy candidates are missing")
    matches = [
        item for item in candidates
        if isinstance(item, dict)
        and item.get("name") == "github-copilot-cli"
        and item.get("version") == VERSION
    ]
    if len(matches) != 1:
        raise PreparationError("fixed Copilot CLI is not uniquely recorded in dependency policy")
    candidate = matches[0]
    if candidate.get("decision") != "approved_for_isolated_candidate_execution":
        raise PreparationError("fixed Copilot CLI is not approved for isolated candidate execution")
    artifact = candidate.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("sha256") != ARCHIVE_SHA256:
        raise PreparationError("fixed Copilot CLI archive hash is not approved by dependency policy")
    digest = candidate.get("extracted_binary_sha256")
    if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise PreparationError("fixed Copilot CLI binary hash is not approved by dependency policy")
    return digest


def inspect_and_install(archive: Path) -> str:
    approved_digest = approved_binary_sha256()
    inventory = []
    copilot_member: tarfile.TarInfo | None = None
    with tarfile.open(archive, mode="r:gz") as bundle:
        for member in bundle.getmembers():
            path = safe_member_name(member.name)
            kind = "file" if member.isfile() else "directory" if member.isdir() else "other"
            inventory.append({"path": member.name, "size": member.size, "type": kind})
            if member.size < 0 or member.size > 512 * 1024 * 1024:
                raise PreparationError(f"unreasonable tar member size: {member.name}")
            if path.name == "copilot" and member.isfile():
                if len(path.parts) != 1 or copilot_member is not None:
                    raise PreparationError("archive has an ambiguous copilot binary path")
                copilot_member = member
        if copilot_member is None:
            raise PreparationError("archive has no top-level regular copilot file")

        source = bundle.extractfile(copilot_member)
        if source is None:
            raise PreparationError("cannot read copilot archive member")
        BIN_DIR.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="copilot.", dir=BIN_DIR)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            if temporary_path.stat().st_size != copilot_member.size:
                raise PreparationError("extracted copilot size differs from tar metadata")
            binary_digest = sha256(temporary_path)
            if binary_digest != approved_digest:
                raise PreparationError("extracted Copilot binary SHA-256 is not approved by dependency policy")
            os.chmod(temporary_path, 0o755)
            environment = dict(os.environ)
            for name in ("COPILOT_CLI_DIST_DIR", "COPILOT_CLI_RESOLVED_DIST_DIR",
                         "COPILOT_CLI_BINARY_VERSION", "COPILOT_LOADER_PID"):
                environment.pop(name, None)
            environment.update(COPILOT_AUTO_UPDATE="false", COPILOT_SKIP_CLI_DOWNLOAD="1", COPILOT_DISABLE_AUTO_UPDATE="1",
                               GH_NO_UPDATE_NOTIFIER="1")
            try:
                version_result = subprocess.run(
                    [str(temporary_path), "--version"], capture_output=True, text=True,
                    timeout=15, check=False, env=environment)
            except subprocess.TimeoutExpired as exc:
                raise PreparationError("executable version check timed out") from exc
            match = re.search(r"GitHub Copilot CLI\s+(\d+\.\d+\.\d+)", version_result.stdout)
            reported_version = match.group(1) if match else "unrecognized"
            if version_result.returncode != 0 or reported_version != VERSION:
                raise PreparationError(f"verified archive executable reports {reported_version}, expected {VERSION}")
            os.replace(temporary_path, BIN_DIR / "copilot")
        finally:
            temporary_path.unlink(missing_ok=True)

    inventory_record = {
        "version": VERSION,
        "source_url": ASSET_URL,
        "archive_size": ARCHIVE_SIZE,
        "archive_sha256": ARCHIVE_SHA256,
        "binary_path": str((BIN_DIR / "copilot").relative_to(ROOT)),
        "binary_sha256": binary_digest,
        "reported_version": reported_version,
        "members": inventory,
        "extracted_members": [copilot_member.name],
    }
    atomic_bytes(
        METADATA_DIR / f"tar-members-{TAG}.json",
        (json.dumps(inventory_record, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return binary_digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download-timeout", type=int, default=300,
                        help="per-download total timeout in seconds (default: 300)")
    args = parser.parse_args(argv)
    if args.download_timeout < 30 or args.download_timeout > 1800:
        parser.error("--download-timeout must be between 30 and 1800 seconds")
    try:
        verify_public_metadata(args.download_timeout)
        archive = obtain_archive(args.download_timeout)
        binary_digest = inspect_and_install(archive)
    except (PreparationError, OSError, tarfile.TarError, ValueError) as exc:
        print(f"FAIL: {exc}", file=os.sys.stderr)
        return 1
    print(f"VERIFIED archive sha256={ARCHIVE_SHA256} size={ARCHIVE_SIZE}")
    print(f"INSTALLED {BIN_DIR / 'copilot'} sha256={binary_digest}")
    print("Existing configuration and authentication were not changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
