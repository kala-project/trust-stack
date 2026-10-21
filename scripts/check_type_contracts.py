"""Check enforced contracts and prove invalid callers fail before execution."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
INVALID_CALLERS = {
    "unknown_finding_outcome": '''from workbench.verification_contracts import VerificationFinding
VerificationFinding("candidate", "version", "node", "a" * 64, "b" * 64, "numeric", "looks-ok", "Checked.")
''',
    "mutable_check_coverage": '''from workbench.verification_contracts import CheckCoverage
CheckCoverage(["node"], ("node",), ())
''',
    "guaranteed_semantic_truth": '''from workbench.verification_contracts import VerificationMetadata
VerificationMetadata(semantic_truth_guaranteed=True)
''',
    "untyped_citation_receipt": '''from workbench.verification_contracts import VerifiedCitation
VerifiedCitation("evidence", "e1", {})
''',
    "untyped_publication_review": '''from workbench.verification_contracts import PreparedPublication, CheckCoverage, VerificationMetadata
from workbench.publication_contracts import CanonicalPublication
PreparedPublication("candidate", "version", "a" * 64, "verified", CanonicalPublication.decode({}),
                    {}, (), VerificationMetadata(), CheckCoverage(("n",), ("n",), ()))
''',
    "wrong_publication_kind": '''from workbench.publication_contracts import Reference
Reference("made-up-kind", "reference-1")
''',
    "mutable_author_catalog": '''from workbench.candidate_contracts import AuthorInputs
AuthorInputs([], (), (), (), ())
''',
    "untyped_candidate_publication": '''from workbench.candidate_contracts import CandidateSubmission, AuthorInputs
from workbench.execution_scope import ExecutionScope
from workbench.research_records import FrozenObject
CandidateSubmission("question", ExecutionScope.create(["MSFT"]), FrozenObject(()), None,
                    {}, (), (), "request", AuthorInputs((), (), (), (), ()))
''',
    "untyped_author_call": '''from workbench.author_execution import candidate_submission
from workbench.candidate_contracts import AuthorInputs
from workbench.publication_contracts import CanonicalPublication
from workbench.research_records import FrozenObject
candidate_submission(question="question", scope={}, input_snapshot=FrozenObject(()),
                     base_message_id=None, publication=CanonicalPublication.decode({}),
                     refs=AuthorInputs((), (), (), (), ()), request_identity="request")
''',
    "wrong_scope": '''from workbench.data_contracts import SourceVersion
SourceVersion("source", "version", 1, "a" * 64, 1, "secret")
''',
    "fake_source": '''from workbench.data_contracts import EvidenceRef, TextSpanLocator
EvidenceRef({}, TextSpanLocator("text_span", 0, 1), "parsed", "parser/1")
''',
    "fake_receipt_payload": '''from workbench.research_records import AuthorToolReceipt, FrozenObject
AuthorToolReceipt("run", 1, "call", "tool", {}, FrozenObject(()), "a" * 64, "2026-10-03T00:00:00Z")
''',
    "wrong_snapshot_revision": '''from workbench.research_records import ResearchSnapshot, FrozenObject
ResearchSnapshot("run", "first", FrozenObject(()), "a" * 64, "2026-10-03T00:00:00Z")
''',
}


def main() -> int:
    command = [sys.executable, "-m", "mypy", "--config-file", str(ROOT / "mypy.ini")]
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode:
        return result.returncode
    with tempfile.TemporaryDirectory(prefix="contract-callers-") as directory:
        for name, source in INVALID_CALLERS.items():
            path = Path(directory) / f"{name}.py"
            path.write_text(source, encoding="utf-8")
            result = subprocess.run(
                [*command, "--no-incremental", str(path)], cwd=ROOT,
                capture_output=True, text=True, check=False,
            )
            if result.returncode != 1 or "[arg-type]" not in result.stdout:
                print(f"Invalid caller was not rejected by argument typing: {name}", file=sys.stderr)
                print(result.stdout + result.stderr, file=sys.stderr)
                return 1
            print(f"Rejected before execution: {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
