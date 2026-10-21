from contextlib import closing
from fractions import Fraction
import json
from pathlib import Path
import tempfile
import time
import unittest

from tests.test_report_publications import block
from tests.test_research_intent import ControlledIntentResolver, intent
from workbench.answer_artifacts import AnswerArtifactService
from workbench.build import build_database
from workbench.chat import ChatService
from workbench.documents import DocumentService
from workbench.research_tasks import ResearchTaskService
from workbench.worker import DurableWorker


TERMINAL = {"completed", "failed", "cancelled", "blocked"}


def pump(service, run_id, timeout=3):
    """Run explicit durable author/verify jobs; no automatic test thread is assumed."""
    worker = DurableWorker(service, worker_id=f"multi-subject-{run_id}", heartbeat_seconds=.05)
    deadline = time.monotonic() + timeout
    jobs = 0
    while time.monotonic() < deadline:
        jobs += bool(worker.run_once())
        result = service.get_run(run_id)
        if result["status"] in TERMINAL:
            with closing(service._connect()) as connection:
                pending = connection.execute(
                    "SELECT count(*) FROM jobs WHERE status IN ('queued','retry_wait','running')"
                ).fetchone()[0]
            if not pending:
                return result, jobs
        time.sleep(.01)
    raise AssertionError("run did not finish")


class MultiSubjectProvider:
    """Controlled author/verifier protocol double, not a user or a real model."""

    requires_structured_reports = True

    def __init__(self, versions):
        self.versions = versions

    def status(self):
        return {"available": True, "provider": "multi-subject-protocol"}

    def stream(self, messages, tools, cancel_event):
        handlers = {item["name"]: item["handler"] for item in tools}
        self.facts = []
        for company_id in ("MSFT", "AAPL"):
            result = handlers["extract_document_table_fact"](
                company_id=company_id, document_version_id=self.versions[company_id], table_index=0,
                header_row_indices=[0], row_label_column_index=0, row_index=1, column_index=1,
                expected_value="7" if company_id == "MSFT" else "13",
                metric="unseen_workload", unit="service units", period_kind="year",
                period_start="2025-01-01", period_end="2025-12-31",
                basis="synthetic protocol fixture", metadata_mode="explicit_interpretation")
            self.facts.append(result["fact"])
        self.calculation = handlers["calculate_facts"](
            operation="ratio", fact_ids=[fact["fact_id"] for fact in self.facts], comparison_mode="cross_subject")
        handlers["publish_research_report"](
            title="Two-subject synthetic protocol report, not financial results",
            blocks=[
                block([{"kind": "text", "text": f'{fact["company_id"]}: '},
                       {"kind": "fact", "id": fact["fact_id"],
                        "value_formation": "observed", "attribution": None}],
                      classification="source_statement")
                for fact in self.facts
            ] + [block([{"kind": "text", "text": "Ratio of the explicitly selected periods: "},
                        {"kind": "calculation", "id": self.calculation["calculation_id"],
                         "value_formation": "derived", "attribution": None}])],
            datasets=[{
                "title": "Selected synthetic records", "description": "Synthetic service workloads, not issuer results.",
                "display": "bar", "points": [
                    {"label": f'{fact["company_id"]} {fact["period_end"]}', "kind": "fact", "id": fact["fact_id"],
                     "value_formation": "observed", "attribution": None}
                    for fact in self.facts],
            }])
        yield {"type": "delta", "text": "Structured publication prepared."}

    def stream_verification(self, messages, tools, cancel_event):
        packet = json.loads(messages[0]["content"])
        handlers = {tool["name"]: tool["handler"] for tool in tools}
        reads = []
        verified_facts = []
        for company_id in ("MSFT", "AAPL"):
            result = handlers["extract_document_table_fact"](
                company_id=company_id, document_version_id=self.versions[company_id], table_index=0,
                header_row_indices=[0], row_label_column_index=0, row_index=1, column_index=1,
                expected_value="7" if company_id == "MSFT" else "13",
                metric="unseen_workload", unit="service units", period_kind="year",
                period_start="2025-01-01", period_end="2025-12-31",
                basis="synthetic protocol fixture", metadata_mode="explicit_interpretation")
            reads.append(result)
            verified_facts.append(result["fact"])
        recalculated = handlers["calculate_facts"](
            operation="ratio", fact_ids=[fact["fact_id"] for fact in verified_facts],
            comparison_mode="cross_subject")
        reads.append(recalculated)
        submitted = packet["calculation_refs"][0]
        candidate = handlers["get_candidate_reference"](
            kind=submitted["kind"], identifier=submitted["id"])
        reads.append(candidate)
        supported = (
            candidate["state"] == "available"
            and [fact["value"] for fact in verified_facts] == ["7", "13"]
            and candidate["record"]["arithmetic"]["exact_value"]
            == recalculated["arithmetic"]["exact_value"]
        )
        receipts = [item["_verification_receipt"]["identifier"] for item in reads]
        for node_id in packet["content_nodes"]:
            for check_kind in packet["verification_checks"][node_id]["required"]:
                handlers["record_verification_finding"](
                    version_id=packet["version_id"], node_id=node_id,
                    check_kind=check_kind,
                    outcome="supported" if supported else "insufficient_material_disclosed",
                    explanation=(
                        "Controlled verifier re-extracted both frozen document cells and recalculated the ratio."
                        if supported else
                        "Controlled verifier could not reproduce the candidate ratio from both frozen documents."
                    ), receipt_ids=receipts)
        handlers["finalize_verification"](
            version_id=packet["version_id"],
            outcome="verified" if supported else "verified_with_limitations",
            limitations=[] if supported else ["The controlled document re-extraction did not match."],
            semantic_review={"original_question_coverage":
                             "Controlled test assessment of both requested synthetic subjects."},
            verification={"method": "controlled-multi-subject-test-double",
                          "semantic_truth_guaranteed": False})
        yield {"type": "usage", "tokens": 1}


class MultiSubjectResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.temp.name) / "evidence.sqlite3"
        build_database(db_path=cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_scope_tools_publication_citations_and_restart(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "chat.sqlite3"
            documents = DocumentService(Path(root) / "documents")
            versions = {}
            for company_id, value in (("MSFT", "7"), ("AAPL", "13")):
                version = documents.register_bytes(
                    ("<html><body><h1>Synthetic fixture, not company results</h1><table>"
                     "<tr><th>Service units</th><th>2025</th></tr>"
                     f"<tr><td>Unseen workload</td><td>{value}</td></tr></table></body></html>").encode(),
                    company_id=company_id, source_key="synthetic-workload",
                    title="Synthetic workload fixture", media_type="text/html")
                versions[company_id] = version["document_version_id"]
            provider = MultiSubjectProvider(versions)
            provider.resolve_research_intent = ControlledIntentResolver([
                intent(subjects=[{"kind": "company", "name": "Microsoft", "candidate_id": "MSFT"},
                                 {"kind": "company", "name": "Apple", "candidate_id": "AAPL"}]),
                intent(subjects=[{"kind": "company", "name": "Apple", "candidate_id": "AAPL"}],
                       is_continuation=True),
            ])
            chat = ChatService(self.db, path, provider, document_service=documents)
            self.addCleanup(chat.close)
            tasks = ResearchTaskService(chat)
            task = tasks.create("Compare the two named issuers", None, "multi-subject-protocol")
            self.assertEqual(task["status"], "ready")
            initial = tasks.start(task["task_id"], task["current_scope_version"])
            self.assertEqual(initial["company_ids"], ["MSFT", "AAPL"])
            run, jobs = pump(chat, initial["run_id"])
            self.assertGreaterEqual(jobs, 2, "author and verifier must run as separate durable jobs")
            self.assertEqual(run["status"], "completed", (run.get("error"), [
                event for event in run["events"] if event["type"] == "tool_result"]))
            conversation = chat.get_conversation(initial["conversation_id"])
            self.assertEqual(conversation["execution_scope"]["company_ids"], ["AAPL", "MSFT"])
            message = conversation["messages"][-1]
            answer = chat.answer_context(initial["conversation_id"], message["message_id"])
            artifact = AnswerArtifactService(chat).get(initial["conversation_id"], message["message_id"])
            self.assertTrue(artifact["publication"]["immutable"])
            self.assertEqual({fact["company_id"] for fact in provider.facts}, {"MSFT", "AAPL"})
            expected = Fraction(provider.facts[0]["value"]) / Fraction(provider.facts[1]["value"])
            self.assertEqual(provider.calculation["arithmetic"]["exact_value"],
                             {"numerator": str(expected.numerator), "denominator": str(expected.denominator)})
            for citation in answer["citations"]:
                self.assertTrue(citation["valid"])
                detail = chat.read_citation(initial["conversation_id"], message["message_id"],
                                            citation["kind"], citation["id"])
                self.assertEqual(detail["execution_scope"]["company_ids"], ["AAPL", "MSFT"])
            followup = chat.submit(initial["conversation_id"], "Focus on Apple while preserving the overall report")
            followup_run, followup_jobs = pump(chat, followup["run_id"])
            self.assertGreaterEqual(followup_jobs, 2, "continuation also requires author then verifier")
            self.assertEqual(followup_run["status"], "completed")
            self.assertEqual(chat.scopes.read(initial["conversation_id"])["company_ids"], ["AAPL", "MSFT"])
            self.assertEqual(chat.answer_context(initial["conversation_id"], message["message_id"]), answer)
            chat.close()
            reopened = ChatService(self.db, path, MultiSubjectProvider(versions), document_service=documents)
            self.addCleanup(reopened.close)
            self.assertEqual(reopened.answer_context(initial["conversation_id"], message["message_id"]), answer)
            self.assertEqual(AnswerArtifactService(reopened).get(
                initial["conversation_id"], message["message_id"])["findings"], artifact["findings"])


if __name__ == "__main__":
    unittest.main()
