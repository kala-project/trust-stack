import importlib.util
import contextlib
import hashlib
import io
import json
import subprocess
import tarfile
import tempfile
from pathlib import Path
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "audit_dependency_metadata", ROOT / "scripts" / "audit_dependency_metadata.py"
)
audit = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(audit)

CLI_SPEC = importlib.util.spec_from_file_location(
    "prepare_copilot_cli", ROOT / "scripts" / "prepare_copilot_cli.py"
)
prepare_cli = importlib.util.module_from_spec(CLI_SPEC)
assert CLI_SPEC.loader is not None
CLI_SPEC.loader.exec_module(prepare_cli)

RUNTIME_SPEC = importlib.util.spec_from_file_location(
    "prepare_copilot_runtime", ROOT / "scripts" / "prepare_copilot_runtime.py"
)
prepare_runtime = importlib.util.module_from_spec(RUNTIME_SPEC)
assert RUNTIME_SPEC.loader is not None
RUNTIME_SPEC.loader.exec_module(prepare_runtime)


HASH = "a" * 64


def policy():
    return {
        "minimum_artifact_age_days": 30,
        "latest_allowed_upload_cutoff": "2026-08-29T10:40:45Z",
        "candidates": [
            {
                "name": "example-package",
                "version": "1.2.3",
                "artifact": {
                    "filename": "example_package-1.2.3-py3-none-any.whl",
                    "packagetype": "bdist_wheel",
                    "upload_time_iso_8601": "2026-08-01T00:00:00Z",
                    "yanked": False,
                    "sha256": HASH,
                },
                "requires_python": ">=3.11",
                "observed_requires_dist": ["child>=1"],
            }
        ],
    }


def metadata():
    return {
        "info": {
            "name": "example-package",
            "version": "1.2.3",
            "requires_python": ">=3.11",
            "requires_dist": ["child>=1"],
        },
        "releases": {
            "1.2.3": [
                {
                    "filename": "example_package-1.2.3-py3-none-any.whl",
                    "packagetype": "bdist_wheel",
                    "upload_time_iso_8601": "2026-08-01T00:00:00Z",
                    "yanked": False,
                    "digests": {"sha256": HASH},
                }
            ]
        },
    }


class SupplyChainMetadataTests(unittest.TestCase):
    def test_accepts_matching_old_unyanked_hashed_artifact(self):
        audit.validate_candidate(policy(), policy()["candidates"][0], metadata())

    def test_rejects_new_package(self):
        p = policy()
        m = metadata()
        timestamp = "2026-08-30T00:00:00Z"
        p["candidates"][0]["artifact"]["upload_time_iso_8601"] = timestamp
        m["releases"]["1.2.3"][0]["upload_time_iso_8601"] = timestamp
        with self.assertRaisesRegex(audit.AuditError, "newer than age cutoff"):
            audit.validate_candidate(p, p["candidates"][0], m)

    def test_rejects_yanked_artifact(self):
        m = metadata()
        m["releases"]["1.2.3"][0]["yanked"] = True
        with self.assertRaisesRegex(audit.AuditError, "yanked"):
            audit.validate_candidate(policy(), policy()["candidates"][0], m)

    def test_rejects_missing_hash(self):
        m = metadata()
        del m["releases"]["1.2.3"][0]["digests"]["sha256"]
        with self.assertRaisesRegex(audit.AuditError, "SHA-256"):
            audit.validate_candidate(policy(), policy()["candidates"][0], m)

    def test_rejects_wrong_version(self):
        m = metadata()
        m["info"]["version"] = "1.2.4"
        with self.assertRaisesRegex(audit.AuditError, "metadata version"):
            audit.validate_candidate(policy(), policy()["candidates"][0], m)

    def test_rejects_hash_mismatch(self):
        m = metadata()
        m["releases"]["1.2.3"][0]["digests"]["sha256"] = "b" * 64
        with self.assertRaisesRegex(audit.AuditError, "does not match"):
            audit.validate_candidate(policy(), policy()["candidates"][0], m)

    def test_metadata_match_does_not_admit_blocked_package(self):
        p = policy()
        p.update(default_decision="deny", approved_packages=[])
        p["candidates"][0]["decision"] = "blocked"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "policy.json").write_text(json.dumps(p))
            (root / "metadata.json").write_text(json.dumps(metadata()))
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                result = audit.main(["verify", "--policy", str(root / "policy.json"),
                                     "--metadata", "example-package=" + str(root / "metadata.json")])
            self.assertEqual(result, 2)
            self.assertIn("METADATA MATCH", out.getvalue())
            self.assertIn("BLOCKED", err.getvalue())
            self.assertNotIn("PASS:", out.getvalue())

    def test_rejects_non_exact_policy_version(self):
        p = policy()
        p["candidates"][0]["version"] = ">=1.2.3"
        with self.assertRaisesRegex(audit.AuditError, "version must be exact"):
            audit.policy_candidates(p)


class CopilotPreparationTests(unittest.TestCase):
    def _write_cli_fixture(self, root: Path, binary: bytes, approved_digest: str) -> Path:
        policy_path = root / "dependency-policy.json"
        policy_path.write_text(json.dumps({
            "runtime_candidates": [{
                "name": "github-copilot-cli",
                "version": prepare_cli.VERSION,
                "decision": "approved_for_isolated_candidate_execution",
                "artifact": {"sha256": prepare_cli.ARCHIVE_SHA256},
                "extracted_binary_sha256": approved_digest,
            }],
        }), encoding="utf-8")
        archive = root / "copilot.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            member = tarfile.TarInfo("copilot")
            member.size = len(binary)
            bundle.addfile(member, io.BytesIO(binary))
        return archive

    def test_binary_hash_mismatch_never_executes_or_replaces_existing_binary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            installed = bin_dir / "copilot"
            installed.write_bytes(b"previous-approved-binary")
            archive = self._write_cli_fixture(root, b"unapproved-binary", "b" * 64)

            with mock.patch.object(prepare_cli, "ROOT", root), \
                    mock.patch.object(prepare_cli, "POLICY", root / "dependency-policy.json"), \
                    mock.patch.object(prepare_cli, "BIN_DIR", bin_dir), \
                    mock.patch.object(prepare_cli, "METADATA_DIR", root / "metadata"), \
                    mock.patch.object(prepare_cli.subprocess, "run") as run:
                with self.assertRaisesRegex(prepare_cli.PreparationError, "binary SHA-256"):
                    prepare_cli.inspect_and_install(archive)

            run.assert_not_called()
            self.assertEqual(installed.read_bytes(), b"previous-approved-binary")
            self.assertEqual(sorted(bin_dir.iterdir()), [installed])

    def test_approved_binary_hash_reaches_mocked_version_check(self):
        binary = b"approved-but-never-executed-test-binary"
        digest = hashlib.sha256(binary).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            archive = self._write_cli_fixture(root, binary, digest)
            result = subprocess.CompletedProcess([], 0, "GitHub Copilot CLI 1.0.79\n", "")

            with mock.patch.object(prepare_cli, "ROOT", root), \
                    mock.patch.object(prepare_cli, "POLICY", root / "dependency-policy.json"), \
                    mock.patch.object(prepare_cli, "BIN_DIR", bin_dir), \
                    mock.patch.object(prepare_cli, "METADATA_DIR", root / "metadata"), \
                    mock.patch.object(prepare_cli.subprocess, "run", return_value=result) as run:
                self.assertEqual(prepare_cli.inspect_and_install(archive), digest)

            run.assert_called_once()
            self.assertEqual(run.call_args.args[0][1], "--version")
            self.assertEqual((bin_dir / "copilot").read_bytes(), binary)

    def test_wrong_python_micro_version_is_rejected_before_installer(self):
        with mock.patch.object(prepare_runtime.sys, "version_info", (3, 14, 3)), \
                mock.patch.object(prepare_runtime.shutil, "which") as which, \
                mock.patch.object(prepare_runtime.subprocess, "run") as run:
            with self.assertRaisesRegex(prepare_runtime.PreparationError, "CPython 3.14.4"):
                prepare_runtime.install_venv()

        which.assert_not_called()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
