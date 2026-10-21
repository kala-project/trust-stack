#!/usr/bin/env python3
"""Download verified locked wheels and optionally install the isolated SDK venv.

This script never downloads, extracts, or executes Copilot CLI. The CLI uses a
separate, scope-limited isolated-candidate gate in config/dependency-policy.json.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.request import Request, urlopen
import zipfile


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "config" / "dependency-policy.json"
LOCK = ROOT / "requirements-copilot.lock"
RUNTIME = ROOT / "var" / "copilot-runtime"
WHEELS = RUNTIME / "wheels"
VENV = RUNTIME / "venv"
LOCK_LINE = re.compile(r"^([A-Za-z0-9_.-]+)==([^ ]+) --hash=sha256:([0-9a-f]{64})$")
ALLOWED_ENTRY_POINTS = {
    "httpx": "[console_scripts]\nhttpx = httpx:main\n",
    # Reviewed locked 3.19 cli.py: argparse/stdin domain encoding only; not executed at install.
    "idna": "[console_scripts]\nidna=idna.cli:main\n\n",
}


class PreparationError(RuntimeError):
    pass


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def normalized(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def normalized_requirement(value: str) -> str:
    # Core Metadata permits either quote style around marker string values.
    return re.sub(r"\s+", " ", value.replace("'", '"').replace("(", "").replace(")", "")).strip()


def read_inputs():
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    locked = {}
    for raw in LOCK.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = LOCK_LINE.fullmatch(line)
        if not match:
            raise PreparationError(f"non-exact lock line: {line}")
        name, version, digest = match.groups()
        locked[normalized(name)] = (version, digest)
    candidates = {normalized(item["name"]): item for item in policy["candidates"]}
    approved = {normalized(item["name"]): item for item in policy["approved_packages"]}
    if set(locked) != set(candidates) or set(locked) != set(approved):
        raise PreparationError("lock, candidates, and approved package sets differ")
    cutoff = timestamp(policy["latest_allowed_upload_cutoff"])
    for name, (version, digest) in locked.items():
        item, admission = candidates[name], approved[name]
        artifact = item["artifact"]
        if item.get("decision") != "approved" or artifact.get("packagetype") != "bdist_wheel":
            raise PreparationError(f"{name} is not an approved wheel")
        if artifact.get("yanked") is not False:
            raise PreparationError(f"{name} artifact is yanked or has unknown yank status")
        if not item.get("license_expression"):
            raise PreparationError(f"{name} has no reviewed license expression")
        source = item.get("source")
        if source is not None and not str(source).startswith("https://"):
            raise PreparationError(f"{name} has an invalid source URL")
        if (item["version"], artifact["sha256"]) != (version, digest):
            raise PreparationError(f"{name} lock does not match policy")
        if (admission.get("version"), admission.get("sha256")) != (version, digest):
            raise PreparationError(f"{name} approval does not match lock")
        if timestamp(artifact["upload_time_iso_8601"]) > cutoff:
            raise PreparationError(f"{name} is newer than the release cutoff")
        if not str(artifact.get("url", "")).startswith("https://files.pythonhosted.org/"):
            raise PreparationError(f"{name} artifact is not from files.pythonhosted.org")
    return candidates


def validate_wheel(path: Path, candidate: dict) -> None:
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != candidate["artifact"]["sha256"]:
        raise PreparationError(f"SHA-256 mismatch for {path.name}")
    try:
        with zipfile.ZipFile(path) as wheel:
            names = wheel.namelist()
            for name in names:
                pure = PurePosixPath(name)
                if pure.is_absolute() or ".." in pure.parts:
                    raise PreparationError(f"unsafe wheel member in {path.name}: {name}")
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            if len(metadata_names) != 1:
                raise PreparationError(f"wheel has ambiguous METADATA: {path.name}")
            headers = Parser().parsestr(wheel.read(metadata_names[0]).decode("utf-8"))
            if normalized(headers.get("Name", "")) != normalized(candidate["name"]):
                raise PreparationError(f"wheel name does not match policy: {path.name}")
            if headers.get("Version") != candidate["version"]:
                raise PreparationError(f"wheel version does not match policy: {path.name}")
            wheel_license = headers.get("License-Expression") or headers.get("License")
            if wheel_license != candidate["license_expression"]:
                raise PreparationError(f"wheel license does not match policy: {path.name}")
            project_urls = "\n".join(headers.get_all("Project-URL", []))
            source = candidate.get("source")
            if source and source not in project_urls and source != headers.get("Home-page"):
                raise PreparationError(f"wheel source does not match policy: {path.name}")
            # Plugin registrations are data, but executable console/gui launchers
            # would create startup scripts and require a separate review.
            for entry in (name for name in names if name.endswith(".dist-info/entry_points.txt")):
                text = wheel.read(entry).decode("utf-8", errors="replace")
                allowed = ALLOWED_ENTRY_POINTS.get(normalized(candidate["name"]))
                if ("[console_scripts]" in text or "[gui_scripts]" in text) and text != allowed:
                    raise PreparationError(f"wheel installs executable startup scripts: {path.name}")
    except zipfile.BadZipFile as exc:
        raise PreparationError(f"invalid wheel: {path.name}") from exc


def obtain_wheels(candidates) -> None:
    WHEELS.mkdir(parents=True, exist_ok=True)
    for name in sorted(candidates):
        artifact = candidates[name]["artifact"]
        destination = WHEELS / artifact["filename"]
        if destination.exists():
            validate_wheel(destination, candidates[name])
            print(f"VERIFIED {destination.relative_to(ROOT)}")
            continue
        request = Request(artifact["url"], headers={"User-Agent": "trust-stack-runtime-preparer/1"})
        fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", dir=WHEELS)
        try:
            with os.fdopen(fd, "wb") as output, urlopen(request, timeout=60) as response:
                shutil.copyfileobj(response, output)
            temp_path = Path(temporary)
            validate_wheel(temp_path, candidates[name])
            temp_path.replace(destination)
            print(f"DOWNLOADED+VERIFIED {destination.relative_to(ROOT)}")
        finally:
            Path(temporary).unlink(missing_ok=True)


def install_venv() -> None:
    if sys.version_info[:3] != (3, 14, 4):
        raise PreparationError("the reviewed runtime target is CPython 3.14.4")
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise PreparationError("the reviewed runtime target is Linux x86_64")
    uv = shutil.which("uv")
    if uv is None:
        raise PreparationError("uv is required but was not found")
    if VENV.exists():
        raise PreparationError("isolated venv already exists; refusing to delete or replace it")
    subprocess.run([uv, "venv", "--python", sys.executable, str(VENV)], check=True)
    python = VENV / "bin" / "python"
    subprocess.run([
        uv, "pip", "install", "--python", str(python), "--no-index",
        "--find-links", str(WHEELS), "--require-hashes", "-r", str(LOCK),
    ], check=True)
    subprocess.run([
        str(python), "-I", "-c",
        "import importlib.metadata as m; import copilot; assert m.version('github-copilot-sdk') == '1.0.11'",
    ], check=True)
    print(f"INSTALLED+IMPORT-VERIFIED {VENV.relative_to(ROOT)}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", help="create and install a new isolated venv; refuse an existing directory")
    args = parser.parse_args(argv)
    try:
        candidates = read_inputs()
        obtain_wheels(candidates)
        if args.install:
            install_venv()
    except (PreparationError, OSError, ValueError, json.JSONDecodeError,
            subprocess.CalledProcessError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print("SDK preparation complete; CLI admission and user authentication are separate gates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
