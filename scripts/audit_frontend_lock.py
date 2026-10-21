"""Check every locked frontend package BEFORE installation; never executes package code.

Uses official npm metadata to check release age, tarball identity, SRI and license.
This supplements (not replaces) npm audit, provenance checks and manual review.
The fixed cutoff intentionally does not advance automatically on future builds.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote, urlparse
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
CUTOFF = "2026-09-02T05:32:42Z"
# Deliberately small allowlist. Additions require upstream/necessity review.
REVIEWED = {
    "react", "react-dom", "scheduler", "typescript", "vite", "rollup", "esbuild",
    "@types/react", "@types/react-dom", "@types/estree", "csstype", "fdir",
    "tinyglobby", "picomatch", "picocolors", "nanoid", "postcss", "source-map-js", "fsevents",
}
LICENSES = {"MIT", "ISC", "BSD-3-Clause", "Apache-2.0"}

def require(condition, message):
    if not condition:
        raise ValueError(message)

def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

def validate_entry(name, entry, metadata, cutoff=CUTOFF):
    require(name in REVIEWED or name.startswith(("@esbuild/", "@rollup/rollup-")), f"unreviewed package: {name}")
    version = entry["version"]
    require(re.fullmatch(r"\d+\.\d+\.\d+", version), f"not a stable exact version: {name}@{version}")
    record = metadata["versions"][version]
    published = metadata["time"][version]
    require(stamp(published) <= stamp(cutoff), f"too young: {name}@{version} ({published})")
    require(record["name"] == name and record["version"] == version, f"package identity mismatch: {name}")
    resolved = entry.get("resolved", "")
    require(urlparse(resolved).scheme == "https" and urlparse(resolved).netloc == "registry.npmjs.org", f"not official registry: {name}")
    require(resolved == record["dist"]["tarball"], f"tarball mismatch: {name}")
    integrity = entry.get("integrity", "")
    require(integrity.startswith("sha512-") and integrity == record["dist"]["integrity"], f"SRI mismatch: {name}")
    license_id = record.get("license")
    require(license_id in LICENSES, f"license requires review: {name}: {license_id}")
    require(not record.get("deprecated"), f"deprecated: {name}@{version}")
    return {"name": name, "version": version, "published": published, "license": license_id,
            "repository": record.get("repository"), "resolved": resolved, "integrity": integrity,
            "install_script_disabled": bool(entry.get("hasInstallScript")),
            "registry_signatures": record["dist"].get("signatures", []),
            "attestations": record["dist"].get("attestations")}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    path = ROOT / "frontend/package-lock.json"
    lock = json.loads(path.read_text())
    require(lock["lockfileVersion"] == 3, "expected npm lockfile v3")
    entries = [(key.rsplit("node_modules/", 1)[1], item) for key, item in lock["packages"].items() if key]
    def check(pair):
        name, entry = pair
        url = "https://registry.npmjs.org/" + quote(name, safe="")
        with urlopen(url, timeout=40) as response:
            require(urlparse(response.url).netloc == "registry.npmjs.org", "registry response redirected off-host")
            metadata = json.load(response)
        return validate_entry(name, entry, metadata)
    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(check, entries))
    result = {"recorded_at": datetime.now(timezone.utc).isoformat(), "cutoff": CUTOFF,
              "lock_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "status": "metadata-age-identity-integrity-license-checked", "packages": records,
              "limitations": "Registry signature fields recorded, not cryptographically verified here; run npm audit signatures separately. No audit proves absence of unknown vulnerabilities."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(f"Checked {len(records)} lock entries; latest publication: {max(r['published'] for r in records)}")
    print(f"Result: {args.output}")

if __name__ == "__main__":
    main()
