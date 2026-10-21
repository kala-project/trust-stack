from workbench.candidate_contracts import AuthorInputs
from workbench.execution_scope import ExecutionScope
from workbench.publication_contracts import CanonicalPublication
from workbench.research_records import freeze_object
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import hashlib
import sqlite3
import tempfile
import unittest

from workbench.build import build_database
from workbench.chat import ChatService, _id, _json, _now
from workbench.job_queue import LeaseLost
from workbench.report_publications import prepare, ReportPublications
from workbench.verification_contracts import (
    SemanticReview, VerificationMetadata, CheckCoverage, RequiredFinding,
    VerificationContractError, PreparedPublication, VerificationFinding, verification_checks,
)
from workbench.verification_store import (
    ArtifactRef, CandidateSubmission, RevisedCandidate,
    VerificationCheckpoint, VerificationConflict,
    VerificationIntegrityError, VerificationNotFound, VerificationStore,
    VerificationToolReceipt, required_verifier_projection,
)
from tests.test_chat import FakeProvider


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/source-catalog.json"
EMPTY_SHA = hashlib.sha256(b"").hexdigest()
EMPTY_OBJECT_SHA = hashlib.sha256(b"{}").hexdigest()


def publication(body="Body", limitation="limited"):
    return prepare(
        title="Candidate report",
        blocks=[{
            "classification": "evidence_gap",
            "parts": [{"kind": "text", "text": body}],
            "support": [], "reasoning": "", "limitations": [limitation],
        }],
        refs={}, facts={}, calculations={}, max_chars=10000,
    )


class VerificationStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.evidence_db = Path(cls.fixture.name) / "evidence.sqlite3"
        build_database(CATALOG, cls.evidence_db, ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.chat_db = Path(self.temp.name) / "chat.sqlite3"
        self.service = ChatService(
            self.evidence_db, self.chat_db, FakeProvider(),
            job_lease_seconds=30.0, job_retry_delays=(0.0, 0.0))
        self.store = VerificationStore(
            self.service._connect, self.service.jobs, self.service.publications)
        self.store.initialize_feature()

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def author_claim(self, request="author-request"):
        conversation = self.service.create_conversation("MSFT")
        submitted = self.service.submit(
            conversation["conversation_id"], "exact user question", request_id=request)
        claim = self.service.jobs.claim("author-worker")
        self.assertEqual(claim.run_id, submitted["run_id"])
        with closing(self.service._connect()) as connection, connection:
            connection.execute(
                "UPDATE runs SET status='running',started_at=? WHERE run_id=? AND status='queued'",
                (_now(), claim.run_id))
            self.service.records.append_snapshot(
                connection, run_id=claim.run_id,
                snapshot={"scope_version": "scope-1", "authorization_version": "auth-1"},
                created_at=_now())
            self.service.records.append_receipt(
                connection, run_id=claim.run_id, call_id="fixture-call",
                tool_name="fixture_catalog", arguments={},
                result={"evidence": {"evidence-1": {}},
                        "calculation": {"calculation-1": {}}}, created_at=_now())
        return conversation, submitted, claim

    def submission(self, request="verify-request", *, base=None, report=None):
        return CandidateSubmission(
            question="exact user question",
            scope=ExecutionScope.create(["MSFT"]),
            input_snapshot=freeze_object({"scope_version": "scope-1", "authorization_version": "auth-1"}, "snapshot"),
            base_message_id=base,
            publication=CanonicalPublication.decode(report or publication()),
            evidence_refs=(ArtifactRef("evidence", "evidence-1", EMPTY_OBJECT_SHA),),
            calculation_refs=(ArtifactRef("calculation", "calculation-1", EMPTY_OBJECT_SHA),),
            request_identity=request,
            author_inputs=AuthorInputs.decode({
                "claim": {}, "passage": {}, "query": {},
                "evidence": {"evidence-1": {}},
                "calculation": {"calculation-1": {}},
            }),
        )

    def saved_candidate(self, request="verify-request"):
        conversation, submitted, author = self.author_claim(request=f"author-{request}")
        saved = self.store.save_candidate_and_enqueue(author, self.submission(request=request))
        self.service.jobs.release_runtime_slot(author)
        claim = self.service.jobs.claim("verify-worker", job_types=("verify",))
        self.assertEqual(claim.job_id, saved["verify_job_id"])
        return conversation, submitted, saved, claim

    def record_terminal_findings(self, saved, claim, *, version_id=None, omit=()):
        version_id = version_id or saved["version_id"]
        version = self.store.read_version(
            claim.conversation_id, saved["candidate_id"], version_id)
        checks = verification_checks(version["publication"])
        for node_id, identity in version["nodes"].items():
            for check_kind in checks[node_id]["required"]:
                if (node_id, check_kind) in omit:
                    continue
                self.store.append_finding(claim, VerificationFinding(
                    candidate_id=saved["candidate_id"], version_id=version_id,
                    node_id=node_id, node_hash=identity["node_hash"],
                    dependency_hash=identity["dependency_hash"], check_kind=check_kind,
                    outcome="supported", explanation="Checked against authorized material.",
                    tool_result_refs=(),
                ))
        return version

    def prepared(self, saved, claim, version, *, outcome="verified", checked=None,
                 unresolved=(), limitations=()):
        required = tuple(version["nodes"])
        return PreparedPublication(
            candidate_id=saved["candidate_id"], version_id=version["version_id"],
            input_hash=saved["input_hash"], outcome=outcome,
            publication=CanonicalPublication.decode(version["publication"]), semantic_review=SemanticReview("The controlled report covers the test question."),
            citations=(), verification=VerificationMetadata(method="controlled-store-test"),
            coverage=CheckCoverage(required, required if checked is None else tuple(checked), tuple(unresolved)),
            limitations=tuple(limitations),
            required_verifier_findings=required_verifier_projection(
                self.store.read_findings(
                    claim, candidate_id=saved["candidate_id"], version_id=version["version_id"])),
        )

    def test_candidate_rejects_missing_author_receipts_and_wrong_formal_scope(self):
        _conversation, _submitted, author = self.author_claim("formal-record-rejections")
        with closing(self.service._connect()) as connection, connection:
            connection.execute("DROP TRIGGER author_tool_receipts_no_delete")
            connection.execute(
                "DELETE FROM author_tool_receipts WHERE run_id=?", (author.run_id,))
        with self.assertRaisesRegex(VerificationIntegrityError, "no formal author tool receipts"):
            self.store.save_candidate_and_enqueue(
                author, self.submission("missing-author-receipts"))
        with closing(self.service._connect()) as connection, connection:
            self.service.records.append_receipt(
                connection, run_id=author.run_id, call_id="replacement-fixture-call",
                tool_name="fixture_catalog", arguments={}, result={"restored": True},
                created_at=_now())
        wrong = replace(
            self.submission("wrong-formal-scope-candidate"),
            scope=ExecutionScope.create(["OTHER"]),
        )
        with self.assertRaisesRegex(VerificationIntegrityError, "scope differs"):
            self.store.save_candidate_and_enqueue(author, wrong)

    def test_unresolved_independent_check_blocks_earlier_supported_publication(self):
        _conversation, _submitted, saved, claim = self.saved_candidate()
        initial_version = self.store.read_version(
            claim.conversation_id, saved["candidate_id"], saved["version_id"])
        node_id = "report-block-1"
        version = self.record_terminal_findings(
            saved, claim, omit=((node_id, "limitation_disclosure"),))
        identity = initial_version["nodes"][node_id]
        self.store.append_finding(claim, VerificationFinding(
            saved["candidate_id"], version["version_id"], node_id,
            identity["node_hash"], identity["dependency_hash"],
            "limitation_disclosure", "unresolved", "Later check found an unresolved conflict.", ()))
        with self.assertRaises(VerificationConflict):
            self.store.finalize(claim, self.prepared(saved, claim, version))
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM verification_records").fetchone()[0], 0)

    def test_effective_checks_never_hide_an_independent_unresolved_issue(self):
        from workbench.verification_store import checked_finding_nodes
        def finding(kind, outcome):
            return {"node_id": "n", "check_kind": kind, "outcome": outcome}
        self.assertEqual(checked_finding_nodes([
            finding("source_identity", "supported"), finding("temporal_alignment", "unresolved")]), set())
        self.assertEqual(checked_finding_nodes([
            finding("source_identity", "supported"), finding("source_identity", "unresolved")]), set())
        self.assertEqual(checked_finding_nodes([
            finding("source_identity", "unresolved"), finding("source_identity", "corrected_and_rechecked")]), {"n"})

    def test_usage_schema_v1_is_additively_migrated_to_v2(self):
        with closing(self.service._connect()) as connection, connection:
            connection.execute("DROP TABLE verification_usage")
            connection.execute(
                "CREATE TABLE verification_usage("
                "job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),"
                "provider_events INTEGER NOT NULL DEFAULT 0,"
                "output_chars INTEGER NOT NULL DEFAULT 0,"
                "tool_calls INTEGER NOT NULL DEFAULT 0,"
                "elapsed_seconds REAL NOT NULL DEFAULT 0,"
                "active_attempt_id TEXT,active_started_at REAL)")
            connection.execute(
                "UPDATE verification_feature SET schema_version=1 "
                "WHERE feature='durable_verifier'")
        self.store.initialize_feature()
        with closing(self.service._connect()) as connection:
            version = connection.execute(
                "SELECT schema_version FROM verification_feature "
                "WHERE feature='durable_verifier'").fetchone()[0]
            columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(verification_usage)")}
        self.assertEqual(version, 2)
        self.assertTrue({"payload_bytes", "exhaustion_stage", "exhaustion_code",
                         "exhaustion_requested_size"} <= columns)

    def test_receipt_id_reader_survives_restart_and_rejects_cross_candidate(self):
        conversation_a, _submitted_a, author_a = self.author_claim("receipt-author-a")
        saved_a = self.store.save_candidate_and_enqueue(
            author_a, self.submission("receipt-verify-a"))
        self.service.jobs.release_runtime_slot(author_a)
        conversation_b, _submitted_b, author_b = self.author_claim("receipt-author-b")
        saved_b = self.store.save_candidate_and_enqueue(
            author_b, self.submission("receipt-verify-b"))
        self.service.jobs.release_runtime_slot(author_b)

        claim = self.service.jobs.claim(
            "receipt-attempt-a", job_types=("verify",), worker_pid=99999999)
        self.assertEqual(claim.job_id, saved_a["verify_job_id"])
        reference = self.store.append_tool_receipt(claim, VerificationToolReceipt(
            saved_a["candidate_id"], saved_a["version_id"], "persisted-read",
            "search_evidence", {"query": "bounded"}, {"results": []}))
        self.assertEqual(self.store.resolve_receipt_ids(
            claim, (reference.identifier,), candidate_id=saved_a["candidate_id"],
            version_id=saved_a["version_id"]), (reference,))
        with self.assertRaises(VerificationConflict):
            self.store.resolve_receipt_ids(
                claim, (reference.identifier,), candidate_id=saved_b["candidate_id"],
                version_id=saved_b["version_id"])

        with closing(self.service._connect()) as connection, connection:
            self.service.jobs.cancel_in_transaction(connection, saved_b["verify_job_id"])
            connection.execute("UPDATE jobs SET lease_expires_at=0 WHERE job_id=?", (claim.job_id,))
            connection.execute(
                "UPDATE model_runtime_slot SET lease_expires_at=0 WHERE attempt_id=?",
                (claim.attempt_id,))
        reclaimed = self.service.jobs.claim("receipt-attempt-restarted", job_types=("verify",))
        self.assertEqual(reclaimed.job_id, saved_a["verify_job_id"])
        self.assertEqual(self.store.resolve_receipt_ids(
            reclaimed, (reference.identifier,), candidate_id=saved_a["candidate_id"],
            version_id=saved_a["version_id"]), (reference,))
        self.assertNotEqual(conversation_a["conversation_id"], conversation_b["conversation_id"])

    def test_successful_receipt_resolver_excludes_failures_and_checks_integrity(self):
        _conversation, _submitted, saved, claim = self.saved_candidate("successful-receipts")
        success = self.store.append_tool_receipt(claim, VerificationToolReceipt(
            saved["candidate_id"], saved["version_id"], "successful-empty-read",
            "search_evidence", {"query": "bounded"}, {"results": []}))
        failure = self.store.append_tool_receipt(claim, VerificationToolReceipt(
            saved["candidate_id"], saved["version_id"], "failed-read",
            "search_evidence", {"query": "failure"},
            {"error": "ToolError", "completed": False}))
        incomplete = self.store.append_tool_receipt(claim, VerificationToolReceipt(
            saved["candidate_id"], saved["version_id"], "incomplete-read",
            "get_candidate_reference", {"kind": "passage", "identifier": "p1"},
            {"state": "incomplete", "candidate_reference_sha256": "a" * 64}))
        for state in ("missing", "unsupported"):
            self.store.append_tool_receipt(claim, VerificationToolReceipt(
                saved["candidate_id"], saved["version_id"], state + "-read",
                "get_candidate_reference", {"kind": "passage", "identifier": "p1"},
                {"state": state, "candidate_reference_sha256": "a" * 64}))

        self.assertEqual(self.store.resolve_successful_receipts(
            claim, candidate_id=saved["candidate_id"], version_id=saved["version_id"]),
            (success,))
        self.assertEqual(self.store.resolve_receipt_ids(
            claim, (failure.identifier, incomplete.identifier),
            candidate_id=saved["candidate_id"], version_id=saved["version_id"]),
            (failure, incomplete))
        with self.assertRaisesRegex(VerificationIntegrityError, "missing, stale"):
            self.store.resolve_receipt_ids(
                claim, ("guessed-receipt",), candidate_id=saved["candidate_id"],
                version_id=saved["version_id"])
        revised = self.store.append_revision(claim, RevisedCandidate(
            candidate_id=saved["candidate_id"], base_version_id=saved["version_id"],
            publication=CanonicalPublication.decode(publication(body="Changed receipt fence")),
            changed_node_ids=("report-block-1",), revision_summary="Controlled revision."))
        with self.assertRaisesRegex(VerificationIntegrityError, "missing, stale"):
            self.store.resolve_receipt_ids(
                claim, (success.identifier,), candidate_id=saved["candidate_id"],
                version_id=revised["version_id"])

        with closing(self.service._connect()) as connection, connection:
            connection.execute("DROP TRIGGER verification_tool_receipts_no_update")
            connection.execute(  # Simulate storage tampering, not a supported write.
                "UPDATE verification_tool_receipts SET result_json='{}' WHERE receipt_id=?",
                (success.identifier,))
        with self.assertRaisesRegex(VerificationIntegrityError, "checksum"):
            self.store.resolve_successful_receipts(
                claim, candidate_id=saved["candidate_id"], version_id=saved["version_id"])
        with self.assertRaisesRegex(VerificationIntegrityError, "checksum"):
            self.store.resolve_receipt_ids(
                claim, (success.identifier,), candidate_id=saved["candidate_id"],
                version_id=saved["version_id"])
        with self.assertRaisesRegex(VerificationIntegrityError, "checksum"):
            self.store.read_receipts(
                claim, (success,), candidate_id=saved["candidate_id"],
                version_id=saved["version_id"])

    def test_candidate_and_verify_enqueue_are_atomic_after_author_terminalization(self):
        other = self.service.create_conversation("MSFT")
        with closing(self.service._connect()) as connection, connection:
            self.service.jobs.enqueue_verify(
                connection, conversation_id=other["conversation_id"],
                request_identity="collision", input_hash="different")
        conversation, submitted, author = self.author_claim()
        with self.assertRaises(Exception):
            self.store.save_candidate_and_enqueue(
                author, self.submission(request="collision"))
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM runs WHERE run_id=?", (submitted["run_id"],)).fetchone()[0],
                "running")
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (author.job_id,)).fetchone()[0],
                "running")
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM verification_candidates").fetchone()[0], 0)
        self.assertEqual(conversation["conversation_id"], author.conversation_id)

    def test_explicit_initialization_does_not_upgrade_legacy_publications(self):
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM report_current").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM verification_records").fetchone()[0], 0)

    def test_owner_scope_tamper_and_stale_claim_are_rejected(self):
        conversation, _, saved, claim = self.saved_candidate("ownership")
        with self.assertRaises(VerificationNotFound):
            self.store.get_candidate("forged-owner", saved["candidate_id"])
        stale = replace(claim, worker_id="forged-worker")
        with self.assertRaises(LeaseLost):
            self.store.append_checkpoint(stale, VerificationCheckpoint(
                candidate_id=saved["candidate_id"], version_id=saved["version_id"],
                checkpoint_key="first", covered_node_ids=(), remaining_node_ids=(),
            ))
        with closing(self.service._connect()) as connection, connection:
            connection.execute("UPDATE jobs SET input_hash='tampered' WHERE job_id=?", (claim.job_id,))
        version = self.store.read_version(
            conversation["conversation_id"], saved["candidate_id"], saved["version_id"])
        with self.assertRaises(VerificationConflict):
            self.store.append_finding(claim, VerificationFinding(
                candidate_id=saved["candidate_id"], version_id=saved["version_id"],
                node_id="report-title", node_hash=version["nodes"]["report-title"]["node_hash"],
                dependency_hash=version["nodes"]["report-title"]["dependency_hash"],
                check_kind="semantic_support", outcome="supported", explanation="not bindable"))

    def test_cancel_wins_commit_race(self):
        _, _, saved, claim = self.saved_candidate("cancel-race")
        version = self.record_terminal_findings(saved, claim)
        with closing(self.service._connect()) as connection, connection:
            self.service.jobs.cancel_in_transaction(connection, claim.job_id)
        with self.assertRaises(LeaseLost):
            self.store.finalize(claim, self.prepared(saved, claim, version))
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM report_publications").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM verification_records").fetchone()[0], 0)

    def test_forged_required_verifier_projection_publishes_nothing(self):
        _conversation, _submitted, saved, claim = self.saved_candidate("forged-projection")
        version = self.record_terminal_findings(saved, claim)
        prepared = self.prepared(saved, claim, version)
        forged = [item.to_json() for item in prepared.required_verifier_findings]
        forged[0] = {**forged[0], "outcome": "conflict_disclosed",
                     "explanation": "forged public explanation"}
        with self.assertRaisesRegex(VerificationIntegrityError, "persisted effective findings"):
            self.store.finalize(claim, replace(
                prepared, required_verifier_findings=tuple(RequiredFinding.decode(item) for item in forged)))
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM verification_records").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM report_publications").fetchone()[0], 0)

    def test_snapshot_preparation_failure_publishes_nothing(self):
        conversation, _, saved, claim = self.saved_candidate("snapshot-prepare-failure")
        version = self.record_terminal_findings(saved, claim)

        def fail_preparation(*_args):
            raise RuntimeError("controlled snapshot failure")

        self.store.set_snapshot_preparer(fail_preparation)
        with self.assertRaisesRegex(RuntimeError, "controlled snapshot failure"):
            self.store.finalize(claim, self.prepared(saved, claim, version))
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM report_publications").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM verification_records").fetchone()[0], 0)
            self.assertIsNone(connection.execute(
                "SELECT message_id FROM report_current WHERE conversation_id=?",
                (conversation["conversation_id"],)).fetchone()[0])

    def test_snapshot_bind_failure_rolls_back_message_publication_and_pointer(self):
        conversation, _, saved, claim = self.saved_candidate("snapshot-bind-failure")
        version = self.record_terminal_findings(saved, claim)
        self.store.set_snapshot_preparer(lambda *_args: {
            "artifact": {}, "payload": {}, "receipt_manifest": [],
        })
        with self.assertRaises(ValueError):
            self.store.finalize(claim, self.prepared(saved, claim, version))
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM messages WHERE role='assistant'").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM report_publications").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM verification_records").fetchone()[0], 0)
            self.assertIsNone(connection.execute(
                "SELECT message_id FROM report_current WHERE conversation_id=?",
                (conversation["conversation_id"],)).fetchone()[0])

    def test_revised_candidate_is_append_only_and_identical_commit_is_idempotent(self):
        conversation, _, saved, claim = self.saved_candidate("revision")
        original = self.store.read_version(
            conversation["conversation_id"], saved["candidate_id"], saved["version_id"])
        revised_publication = publication(body="Copy")
        revised = self.store.append_revision(claim, RevisedCandidate(
            candidate_id=saved["candidate_id"], base_version_id=saved["version_id"],
            publication=CanonicalPublication.decode(revised_publication), changed_node_ids=("report-block-1",),
            revision_summary="Narrowed wording after checking the source."))
        current = self.record_terminal_findings(saved, claim, version_id=revised["version_id"])
        prepared = self.prepared(saved, claim, current, outcome="verified_with_limitations",
                                 limitations=("Authorized material cannot establish deployment.",))
        first = self.store.finalize(claim, prepared)
        second = self.store.finalize(claim, prepared)
        self.assertEqual(first, second)
        with self.assertRaises(VerificationConflict):
            self.store.finalize(claim, replace(prepared, verification=VerificationMetadata(method="different")))
        self.assertIsNotNone(first["formal_message_id"])
        published = self.store.read_published_evidence(
            conversation["conversation_id"], first["formal_message_id"])
        self.assertEqual(published["formal_message_id"], first["formal_message_id"])
        self.assertEqual(published["candidate_id"], saved["candidate_id"])
        self.assertEqual(published["version_id"], revised["version_id"])
        self.assertEqual(published["author_run_id"],
                         self.store.get_candidate(conversation["conversation_id"],
                                                  saved["candidate_id"])["author_run_id"])
        self.assertEqual(published["receipts"], [])
        old = self.store.read_version(
            conversation["conversation_id"], saved["candidate_id"], saved["version_id"])
        self.assertEqual(old["publication"], original["publication"])
        self.assertNotEqual(old["publication_sha256"], current["publication_sha256"])
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM report_publications").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM verification_records").fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (claim.job_id,)).fetchone()[0], "succeeded")

    def test_newer_formal_report_refuses_pointer_takeover(self):
        conversation, _, saved, claim = self.saved_candidate("newer-base")
        version = self.record_terminal_findings(saved, claim)
        newer_run, newer_message = _id("run"), _id("msg")
        newer = publication(body="Newer")
        with closing(self.service._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO runs(run_id,conversation_id,status,created_at,finished_at) VALUES(?,?,'completed',?,?)",
                (newer_run, conversation["conversation_id"], _now(), _now()))
            connection.execute(
                "INSERT INTO messages(message_id,conversation_id,role,content,created_at,run_id,status,citations_json,verification_json) "
                "VALUES(?,?,'assistant',?,?,?,'completed','[]','{}')",
                (newer_message, conversation["conversation_id"], newer["answer"], _now(), newer_run))
            ReportPublications.save(connection, message_id=newer_message, run_id=newer_run,
                                    publication=newer, semantic_review={})
        with self.assertRaises(VerificationConflict):
            self.store.finalize(claim, self.prepared(saved, claim, version))
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM verification_records").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM report_publications").fetchone()[0], 1)

    def test_missing_check_coverage_cannot_be_labeled_verified(self):
        _, _, saved, claim = self.saved_candidate("missing-checks")
        version = self.store.read_version(
            claim.conversation_id, saved["candidate_id"], saved["version_id"])
        required = tuple(version["nodes"])
        with self.assertRaisesRegex(VerificationContractError, "partition"):
            self.prepared(saved, claim, version, checked=required[:-1])
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM verification_records").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT count(*) FROM report_publications").fetchone()[0], 0)
        view = self.store.get_candidate(claim.conversation_id, saved["candidate_id"])
        self.assertEqual(view["status"], "running")
        self.assertFalse(view["semantic_truth_guaranteed"])

    def test_unresolved_completion_is_separate_and_creates_no_formal_report(self):
        _, _, saved, claim = self.saved_candidate("unresolved")
        version = self.store.read_version(
            claim.conversation_id, saved["candidate_id"], saved["version_id"])
        required = tuple(version["nodes"])
        result = self.store.finalize(claim, self.prepared(
            saved, claim, version, outcome="unresolved", checked=(),
            unresolved=required, limitations=("Verification budget exhausted.",)))
        self.assertEqual(result["status"], "unresolved")
        self.assertIsNone(result["formal_message_id"])
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM report_publications").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (claim.job_id,)).fetchone()[0], "succeeded")

    def test_unresolved_completion_cannot_claim_unchecked_node_as_checked(self):
        _, _, saved, claim = self.saved_candidate("unresolved-false-checked")
        version = self.store.read_version(
            claim.conversation_id, saved["candidate_id"], saved["version_id"])
        required = tuple(version["nodes"])
        with self.assertRaisesRegex(VerificationConflict, "cannot claim checks"):
            self.store.finalize(claim, self.prepared(
                saved, claim, version, outcome="unresolved", checked=required[:1],
                unresolved=required[1:], limitations=("Verification incomplete.",)))
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM verification_records").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (claim.job_id,)).fetchone()[0], "running")

    def test_budget_failure_continues_with_fresh_job_and_inherits_same_version_findings(self):
        _, _, saved, failed_claim = self.saved_candidate("continue-budget")
        version = self.store.read_version(
            failed_claim.conversation_id, saved["candidate_id"], saved["version_id"])
        node_id, identity = next(iter(version["nodes"].items()))
        check_kind = verification_checks(version["publication"])[node_id]["required"][0]
        self.store.append_finding(failed_claim, VerificationFinding(
            candidate_id=saved["candidate_id"], version_id=saved["version_id"],
            node_id=node_id, node_hash=identity["node_hash"],
            dependency_hash=identity["dependency_hash"], check_kind=check_kind,
            outcome="supported", explanation="Old failed attempt finding.", tool_result_refs=(),
        ))
        with closing(self.service._connect()) as connection, connection:
            self.service.jobs.fail_in_transaction(
                connection, failed_claim,
                {"code": "verification_budget_exhausted", "message": "budget exhausted"},
                retryable=False)
        self.service.jobs.release_runtime_slot(failed_claim)

        continued = self.store.continue_verification(
            failed_claim.conversation_id, saved["candidate_id"], saved["version_id"],
            "continue-budget-request")
        replay = self.store.continue_verification(
            failed_claim.conversation_id, saved["candidate_id"], saved["version_id"],
            "continue-budget-request")
        self.assertEqual(replay, continued)
        self.assertNotEqual(continued["verify_job_id"], failed_claim.job_id)
        self.assertEqual(continued["previous_verify_job_id"], failed_claim.job_id)
        self.assertEqual(continued["status"], "queued")
        with closing(self.service._connect()) as connection, connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE verification_continuations SET candidate_id=? WHERE verify_job_id=?",
                    ("candidate_tampered", continued["verify_job_id"]))
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "DELETE FROM verification_continuations WHERE verify_job_id=?",
                    (continued["verify_job_id"],))

        new_claim = self.service.jobs.claim("continued-worker", job_types=("verify",))
        self.assertEqual(new_claim.job_id, continued["verify_job_id"])
        packet = self.store.read_claim_input(new_claim)
        self.assertEqual(packet["version_id"], saved["version_id"])
        self.assertEqual(packet["cumulative_tool_calls"], 0)
        inherited = self.store.read_findings(
            new_claim, candidate_id=saved["candidate_id"], version_id=saved["version_id"])
        self.assertEqual(len(inherited), 1)
        self.assertEqual(inherited[0]["explanation"], "Old failed attempt finding.")
        with self.assertRaisesRegex(VerificationConflict, "every applicable terminal finding"):
            self.store.finalize(new_claim, self.prepared(saved, new_claim, version))
        with closing(self.service._connect()) as connection:
            self.assertEqual(connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (failed_claim.job_id,)).fetchone()[0], "failed")
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM verification_findings WHERE job_id=?",
                (failed_claim.job_id,)).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM verification_findings WHERE job_id=?",
                (new_claim.job_id,)).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM verification_continuations WHERE candidate_id=?",
                (saved["candidate_id"],)).fetchone()[0], 1)

    def test_continuation_rejects_non_budget_failure_and_stale_version(self):
        _, _, saved, claim = self.saved_candidate("continue-preconditions")
        with self.assertRaisesRegex(VerificationConflict, "only a verification budget failure"):
            self.store.continue_verification(
                claim.conversation_id, saved["candidate_id"], saved["version_id"], "while-running")
        with closing(self.service._connect()) as connection, connection:
            self.service.jobs.fail_in_transaction(
                connection, claim, {"code": "worker_execution_error", "message": "failed"},
                retryable=False)
        self.service.jobs.release_runtime_slot(claim)
        with self.assertRaisesRegex(VerificationConflict, "only a verification budget failure"):
            self.store.continue_verification(
                claim.conversation_id, saved["candidate_id"], saved["version_id"], "wrong-failure")
        with closing(self.service._connect()) as connection, connection:
            connection.execute(
                "UPDATE jobs SET error_json=? WHERE job_id=?",
                (_json({"code": "verification_budget_exhausted"}), claim.job_id))
        with self.assertRaisesRegex(VerificationConflict, "latest immutable version"):
            self.store.continue_verification(
                claim.conversation_id, saved["candidate_id"], "candidate_version_stale", "stale")


if __name__ == "__main__":
    unittest.main()
