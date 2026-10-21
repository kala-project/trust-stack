"""Contract drift and real HTTP boundary tests with isolated stores."""
from contextlib import closing
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from workbench.api_contracts import (
    GET_CONTRACTS, POST_CONTRACTS, ROUTES, build_openapi, validate_body,
    validate_query, validate_response,
)
from workbench.build import build_database
from workbench.documents import DocumentService
from workbench.execution_scope import describe
from workbench.server import EvidenceHTTPServer, Handler
from workbench.worker import DurableWorker


ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs/contracts/client-http-api.v1.openapi.json"


class ExplicitUnavailableProvider:
    """A deliberate test adapter; never discovers or calls an installed provider."""
    def status(self):
        return {"available": False, "provider": "contract-test-fake", "reason": "disabled in test"}


class ContractRegistryTests(unittest.TestCase):
    def test_research_task_single_and_multi_subject_coverage_are_closed_variants(self):
        task = {"task_id": "task-test", "question": "Compare two subjects",
                "context_conversation_id": None, "origin": "machine_acceptance",
                "created_at": "2026-10-02T00:00:00Z", "current_scope_version": 1,
                "status": "ready", "code": "scope_ready", "message": "Ready",
                "scope": {}, "options": [], "runs": [],
                "latest_run": None, "latest_report": None}
        single = {"facts": {"available": False, "count": 0}, "documents": None}
        for coverage in (single, {"subjects": {"FIRST": single, "SECOND": single}}):
            validate_response("POST", "/api/research/tasks", 201, {**task, "coverage": coverage})
        for coverage in ({"subjects": {"FIRST": "invalid"}},
                         {"subjects": {"FIRST": {"facts": None}}},
                         {**single, "subjects": {}}, {**single, "unknown": 1}):
            with self.assertRaises(RuntimeError):
                validate_response("POST", "/api/research/tasks", 201, {**task, "coverage": coverage})

    def test_checked_in_openapi_is_generated_from_runtime_registry(self):
        self.assertEqual(json.loads(SPEC.read_text(encoding="utf-8")), build_openapi())
        spec = build_openapi()
        documented = {(method.upper(), path) for path, item in spec["paths"].items()
                      for method in item}
        self.assertEqual(documented, set(ROUTES))
        self.assertNotIn("tools", spec["paths"])
        self.assertEqual(spec["paths"]["/api/wiki/publish"]["post"]["tags"], ["maintenance"])
        self.assertEqual(spec["paths"]["/api/health"]["get"]["x-access-category"], "public_health")
        self.assertEqual(spec["paths"]["/api/chat/messages"]["post"]["x-access-category"], "owner_client")

    def test_reader_contract_declares_optional_required_verifier_projection(self):
        schemas = build_openapi()["components"]["schemas"]
        projection = schemas["ReaderPayload"]["properties"]["required_verifier_findings"]
        self.assertEqual(projection["items"]["$ref"],
                         "#/components/schemas/RequiredVerifierFinding")
        finding = schemas["RequiredVerifierFinding"]
        self.assertEqual(finding["properties"]["outcome"]["enum"], [
            "supported", "corrected_and_rechecked", "conflict_disclosed",
            "insufficient_material_disclosed",
        ])
        self.assertNotIn("required_verifier_findings", schemas["ReaderPayload"]["required"])
        scope = schemas["ResearchExecutionScope"]
        self.assertEqual(scope["required"], ["version", "company_ids", "scope_id", "payload_sha256"])
        self.assertTrue(scope["properties"]["company_ids"]["uniqueItems"])
        self.assertNotIn("legacy", scope["properties"])
        self.assertEqual(finding["properties"]["receipts"]["items"]["$ref"],
                         "#/components/schemas/RequiredVerifierReceipt")

    def test_query_contract_rejects_unknown_ambiguous_and_mistyped_scalars(self):
        validate_query("/api/chat/run", {"id": ["run-1"], "after": ["0"]})
        for params in (
            {"id": ["run-1"], "after": ["-1"]},
            {"id": ["run-1"], "after": ["true"]},
            {"id": ["run-1"], "after": ["1", "2"]},
            {"id": ["run-1"], "provider": ["fake"]},
        ):
            with self.assertRaises(ValueError):
                validate_query("/api/chat/run", params)
        with self.assertRaises(ValueError):
            validate_query("/api/reports/reader", {"report_id": ["r"], "message_id": ["m"]})

    def test_citation_kind_query_is_the_five_value_enum(self):
        base = {"conversation": ["conversation-1"], "message": ["message-1"], "id": ["citation-1"]}
        for kind in ("passage", "evidence", "claim", "calculation", "query"):
            validate_query("/api/chat/citation", {**base, "kind": [kind]})
        for kind in ("unknown", "", "PASSAGE"):
            with self.assertRaises(ValueError):
                validate_query("/api/chat/citation", {**base, "kind": [kind]})

    def test_citation_detail_variants_close_the_top_level_and_scope(self):
        common = {
            "detail_schema_version": "citation-detail/1", "company_id": "MSFT",
            "conversation_id": "conversation-1", "message_id": "message-1",
            "execution_scope": describe(["MSFT"]),
            "id": "citation-1", "title": None, "limitations": [],
        }
        source = {
            "source_url": None, "published_at": None, "collected_at": None,
            "source_sha256": None, "document_version_id": None, "quote": None,
            "context": None, "offset": None, "locator": None, "parser_version": None,
            "rights_status": None, "tables": [],
        }
        variants = [
            {**common, **source, "kind": "passage", "source_company_id": "MSFT",
             "source_visibility": "public", "media_type": "text/html"},
            {**common, **source, "kind": "evidence"},
            {**common, **source, "kind": "claim", "chain": {"facts": []},
             "source_type": "existing_research_statement", "support_status": "not_primary_source"},
            {**common, **source, "kind": "calculation", "calculation": {"version": "calculation/1"},
             "formula": "a+b", "value": "3", "unit": "USD", "inputs": [],
             "input_sources": [], "interpretation": "unverified"},
            {**common, "kind": "query", "query": {"version": "native-sql-execution/2"},
             "replay_matches": True, "display": {"rows": []}, "sources": []},
        ]
        for detail in variants:
            validate_response("GET", "/api/chat/citation", 200, detail)

        invalid = []
        for key, value in (("kind", "unknown"), ("detail_schema_version", None)):
            changed = dict(variants[0])
            if value is None:
                changed.pop(key)
            else:
                changed[key] = value
            invalid.append(changed)
        wrong_scope = json.loads(json.dumps(variants[0]))
        wrong_scope["execution_scope"]["version"] = "research-execution-scope/2"
        invalid.append(wrong_scope)
        for change in (
            {"company_ids": []},
            {"company_ids": ["MSFT", "MSFT"]},
            {"payload_sha256": "wrong"},
            {"scope_id": "scope-wrong"},
            {"legacy": False},
        ):
            changed = json.loads(json.dumps(variants[0]))
            changed["execution_scope"].update(change)
            invalid.append(changed)
        invalid.append({**variants[0], "unknown_top_level": True})
        invalid.append({**variants[0], "chain": {}})
        invalid.append({**variants[-1], "source_url": None})
        for detail in invalid:
            with self.assertRaises(RuntimeError):
                validate_response("GET", "/api/chat/citation", 200, detail)

    def test_body_contract_does_not_coerce_bool_integer_or_null(self):
        valid = {"task_id": "task-1", "expected_scope_version": 1}
        validate_body("/api/research/tasks/start", valid)
        for invalid in (
            {**valid, "expected_scope_version": True},
            {**valid, "expected_scope_version": "1"},
            {**valid, "expected_scope_version": None},
            {**valid, "provider": "fake"},
        ):
            with self.assertRaises(ValueError):
                validate_body("/api/research/tasks/start", invalid)
        validate_body("/api/research/tasks", {
            "question": "q", "context_conversation_id": None, "request_id": "request-1",
        })

    def test_critical_response_types_are_runtime_checked(self):
        validate_response("GET", "/api/health", 200, {"status": "ok"})
        validate_response("POST", "/api/chat/messages", 202, {
            "run_id": "run-1", "conversation_id": "conv-1", "status": "queued",
        })
        validate_response("POST", "/api/research/tasks/start", 202, {
            "task_id": "task-1", "scope_version": 1, "company_id": "MSFT",
            "company_ids": ["MSFT"], "conversation_id": "conv-1", "run_id": "run-1",
        })
        for invalid in (
            {"run_id": "run-1", "conversation_id": "conv-1", "status": "unknown"},
            {"run_id": "run-1", "conversation_id": "conv-1", "status": "queued", "extra": True},
            {"task_id": "task-1", "current_scope_version": 1},
        ):
            with self.assertRaises(RuntimeError):
                validate_response("POST", "/api/chat/messages" if "run_id" in invalid else "/api/research/tasks",
                                  202 if "run_id" in invalid else 201, invalid)

    def test_wiki_and_named_nested_schemas_are_closed_at_known_boundaries(self):
        spec = build_openapi()
        schemas = spec["components"]["schemas"]
        for name in ("ChatStatus", "Conversation", "ResearchTask", "ResearchResult", "ReaderPayload",
                     "WikiPage", "WikiChange", "WikiChangeList"):
            self.assertFalse(schemas[name].get("additionalProperties", True), name)
        self.assertEqual(len(schemas["RunAccepted"]["oneOf"]), 2)
        self.assertNotIn(("POST", "/api/wiki/publish"), {
            (item["method"], item["path"]) for item in spec["x-partial-response-contracts"]
        })

    def test_response_status_error_shape_and_enums_are_enforced(self):
        with self.assertRaises(RuntimeError):
            validate_response("POST", "/api/research/tasks", 200,
                              {"task_id": "task-1", "current_scope_version": 1})
        with self.assertRaises(RuntimeError):
            validate_response("GET", "/missing", 404, {"message": "unsafe missing error code"})
        validate_response("GET", "/missing", 599, {"error": "dependency_failed"})
        with self.assertRaises(RuntimeError):
            validate_response("GET", "/api/auth/status", 200, {
                "authentication": "optional", "initialized": True, "authenticated": False,
            })

    def test_nested_list_identity_and_source_scope_are_enforced(self):
        with self.assertRaises(RuntimeError):
            validate_response("GET", "/api/research/materials", 200, {
                "conversation_id": "c", "materials": [{"source_scope": "session_material"}],
                "supported_upload_types": [], "max_upload_bytes": 1, "limitations": [],
            })

    def test_partial_response_contracts_are_explicitly_listed(self):
        spec = build_openapi()
        listed = {(item["method"], item["path"]) for item in spec["x-partial-response-contracts"]}
        self.assertNotIn(("GET", "/api/overview"), listed)
        self.assertIn(("GET", "/api/reports"), listed)
        self.assertIn(("GET", "/api/reports/evidence"), listed)
        self.assertNotIn(("GET", "/api/health"), listed)
        self.assertEqual(spec["paths"]["/api/reports"]["get"]["x-response-schema-completeness"],
                         "nested-critical-fields")

    def test_targeted_routes_no_longer_use_the_generic_json_object(self):
        targeted = {
            ("GET", path) for path in (
                "/api/overview", "/api/company", "/api/evidence", "/api/source",
                "/api/chat/conversations", "/api/chat/conversation", "/api/chat/run",
                "/api/chat/citation", "/api/research/tasks", "/api/research/task",
                "/api/report", "/api/reports/evidence", "/api/reports/source",
                "/api/sample-research/options", "/api/documents/passage",
                "/api/verification/candidate", "/api/verification/version",
            )
        } | {
            ("POST", path) for path in (
                "/api/auth/initialize", "/api/auth/login", "/api/auth/logout",
                "/api/chat/cancel", "/api/verification/cancel", "/api/research/intake",
                "/api/research/decisions/answer",
            )
        }
        self.assertTrue(all(ROUTES[route]["response"] != "JsonObject" for route in targeted))

    def test_auth_candidate_version_and_dynamic_json_boundaries_are_enforced(self):
        validate_response("POST", "/api/auth/initialize", 200, {"status": "initialized"})
        validate_response("POST", "/api/auth/logout", 200, {"status": "logged_out"})
        candidate = {
            "candidate_id": "candidate-1", "conversation_id": "conversation-1",
            "author_run_id": "run-1", "verify_job_id": "job-1", "question": "q",
            "scope": {}, "input_snapshot": {}, "base_message_id": None, "input_hash": "hash",
            "status": "queued", "formal_message_id": None,
            "semantic_truth_guaranteed": False, "versions": [],
        }
        validate_response("GET", "/api/verification/candidate", 200, candidate)
        with self.assertRaises(RuntimeError):
            validate_response("GET", "/api/verification/candidate", 200, {**candidate, "extra": True})
        version = {
            "candidate_id": "candidate-1", "version_id": "version-1", "version_number": 1,
            "parent_version_id": None, "publication": {"schema_version": "publication/1"},
            "publication_sha256": "a" * 64,
            "nodes": {"report-title": {"node_hash": "b" * 64, "dependency_hash": "c" * 64}},
            "dependency_hash": "d" * 64,
            "evidence_refs": [], "calculation_refs": [],
        }
        validate_response("GET", "/api/verification/version", 200, version)
        with self.assertRaises(RuntimeError):
            validate_response("GET", "/api/verification/version", 200, {**version, "extra": True})
        validate_response("GET", "/api/source", 200, {
            "source": {"source_id": "s", "title": "t", "url": "u", "published_at": None,
                       "collected_at": "now", "sha256": "h", "rights_status": "public", "bytes": 1},
            "text": "body", "tables": [{"label": "row", "count": 2, "cells": [None, True]}],
            "parser_version": "p", "warning": None,
        })

    def test_registry_contains_all_current_business_route_groups(self):
        expected_get_prefixes = ("/api/reports", "/api/research", "/api/chat", "/api/wiki", "/api/documents")
        self.assertTrue(all(any(path.startswith(prefix) for path in GET_CONTRACTS)
                            for prefix in expected_get_prefixes))
        self.assertTrue({"/api/research/materials", "/api/research/tasks", "/api/chat/messages",
                         "/api/wiki/publish"} <= set(POST_CONTRACTS))


class ContractHTTPBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.db = Path(cls.temp.name) / "evidence.sqlite3"
        build_database(db_path=cls.db)
        cls.documents_root = Path(cls.temp.name) / "documents"
        passage_source = Path(cls.temp.name) / "contract-passage.html"
        passage_source.write_text(
            "<html><body><p>NaturalContractToken supports the passage contract.</p>"
            "<table><tr><td>Metric</td><td>Value</td></tr></table></body></html>",
            encoding="utf-8",
        )
        DocumentService(cls.documents_root).register_file(
            passage_source, company_id="MSFT", source_key="contract-passage",
            title="Contract passage", url=None, published_at=None, collected_at=None,
            rights_status="public",
        )
        cls.server = EvidenceHTTPServer(
            0, cls.db, provider=ExplicitUnavailableProvider(), documents_root=cls.documents_root)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port
        cls.origin = f"http://127.0.0.1:{cls.port}"
        cls.auth = MagicMock(db_path=Path(cls.temp.name) / "owner.sqlite3")
        cls.auth.authenticated.return_value = False
        cls.auth_server = EvidenceHTTPServer(
            0, cls.db, provider=ExplicitUnavailableProvider(), auth=cls.auth,
            chat_db=Path(cls.temp.name) / "auth-chat.sqlite3",
            wiki_db=Path(cls.temp.name) / "auth-wiki.sqlite3",
            sample_db=Path(cls.temp.name) / "auth-sample.sqlite3",
            documents_root=Path(cls.temp.name) / "auth-documents",
        )
        cls.auth_thread = threading.Thread(target=cls.auth_server.serve_forever, daemon=True)
        cls.auth_thread.start()
        cls.auth_port = cls.auth_server.server_port
        cls.auth_origin = f"http://127.0.0.1:{cls.auth_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.auth_server.shutdown()
        cls.server.server_close()
        cls.auth_server.server_close()
        cls.thread.join(3)
        cls.auth_thread.join(3)
        cls.temp.cleanup()

    def request(self, method, path, body=None):
        headers = {}
        raw = None
        if body is not None:
            raw = json.dumps(body).encode()
            headers = {"Content-Type": "application/json", "Origin": self.origin}
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_runtime_uses_contract_for_unknown_and_scalar_rejection(self):
        self.assertEqual(self.request("GET", "/api/chat/status?provider=fake")[0], 400)
        self.assertEqual(self.request("GET", "/api/chat/run?id=x&after=true")[0], 400)
        status, error = self.request("POST", "/api/research/tasks/start", {
            "task_id": "task-x", "expected_scope_version": True,
        })
        self.assertEqual(status, 400)
        self.assertEqual(error, {"error": "Invalid research request"})

    def test_published_task_list_exposes_real_reader_navigation_and_no_report_variant(self):
        from tests.test_report_publications import ReportProvider

        class ControlledContractReportProvider(ReportProvider):
            """Author/verifier protocol double; it is neither a user nor a real model."""

            def stream_verification(self, messages, tools, cancel_event):
                packet = json.loads(messages[0]["content"])
                handlers = {tool["name"]: tool["handler"] for tool in tools}
                facts_read = handlers["get_company_facts"](metric="revenue")
                facts = facts_read["facts"]
                current = next(item for item in facts if item["period_end"] == "2025-06-30"
                               and item["period_kind"] == "quarter")
                prior = next(item for item in facts if item["period_end"] == "2023-06-30"
                             and item["period_kind"] == "quarter")
                recalculated = handlers["calculate_facts"](
                    operation="difference", fact_ids=[current["fact_id"], prior["fact_id"]])
                submitted = packet["calculation_refs"][0]
                candidate = handlers["get_candidate_reference"](
                    kind=submitted["kind"], identifier=submitted["id"])
                supported = (
                    candidate["state"] == "available"
                    and candidate["record"]["value"] == recalculated["value"]
                    and candidate["record"]["unit"] == recalculated["unit"]
                )
                receipts = [result["_verification_receipt"]["identifier"] for result in (
                    facts_read, recalculated, candidate)]
                for node_id in packet["content_nodes"]:
                    for check_kind in packet["verification_checks"][node_id]["required"]:
                        handlers["record_verification_finding"](
                            version_id=packet["version_id"], node_id=node_id,
                            check_kind=check_kind,
                            outcome="supported" if supported else "insufficient_material_disclosed",
                            explanation=(
                                "Controlled verifier reread frozen revenue facts and recalculated the difference."
                                if supported else
                                "Controlled verifier could not match the frozen calculation to its recalculation."
                            ), receipt_ids=receipts)
                handlers["finalize_verification"](
                    version_id=packet["version_id"],
                    outcome="verified" if supported else "verified_with_limitations",
                    limitations=[] if supported else ["The controlled recalculation did not match."],
                    semantic_review={"original_question_coverage":
                                     "Controlled test assessment of the requested revenue comparison."},
                    verification={"method": "controlled-contract-test-double",
                                  "semantic_truth_guaranteed": False})
                yield {"type": "usage", "tokens": 1}

        provider = ControlledContractReportProvider()
        previous_provider = self.server.chat.provider
        previous_verification_provider = self.server.chat.verification_executor.provider
        previous_intent_resolver = self.server.research_tasks._intent_resolver
        self.server.chat.provider = provider
        self.server.chat.verification_executor.provider = provider
        self.server.research_tasks._intent_resolver = provider.resolve_research_intent
        try:
            status, no_report = self.request("POST", "/api/research/tasks", {
                "question": "Contract task without a published report",
                "context_conversation_id": None,
                "request_id": "contract-task-no-report",
            })
            self.assertEqual(status, 201, no_report)
            status, published = self.request("POST", "/api/research/tasks", {
                "question": "微软收入是多少",
                "context_conversation_id": None,
                "request_id": "contract-task-published-report",
            })
            self.assertEqual(status, 201, published)
            status, started = self.request("POST", "/api/research/tasks/start", {
                "task_id": published["task_id"],
                "expected_scope_version": published["current_scope_version"],
            })
            self.assertEqual(status, 202, started)

            worker = DurableWorker(
                self.server.chat, worker_id=f"contract-report-{started['run_id']}",
                heartbeat_seconds=.05)
            deadline = time.monotonic() + 10
            worked = 0
            while time.monotonic() < deadline:
                worked += bool(worker.run_once())
                candidates = self.server.chat.list_verification_candidates(started["conversation_id"])
                if candidates:
                    candidate = self.server.chat.get_verification_candidate(
                        started["conversation_id"], candidates[-1]["candidate_id"])
                    if candidate["formal_message_id"] is not None:
                        break
                time.sleep(.01)
            else:
                self.fail("explicit author and verifier workers did not publish the report")
            self.assertGreaterEqual(worked, 2)
            status, run = self.request("GET", f"/api/chat/run?id={started['run_id']}")
            self.assertEqual(status, 200, run)
            self.assertEqual(run["status"], "completed", run.get("error"))

            status, initial_listing = self.request(
                "GET", "/api/research/tasks?include_internal=false")
            self.assertEqual(status, 200, initial_listing)
            initial_tasks = {task["task_id"]: task for task in initial_listing["tasks"]}
            report_id = initial_tasks[published["task_id"]]["reader_report_id"]
            status, reader = self.request("GET", f"/api/reports/reader?report_id={report_id}")
            self.assertEqual(status, 200, reader)
            self.assertEqual(reader["report"]["title"], "完整研究报告")

            status, listing = self.request("GET", "/api/research/tasks?include_internal=false")
            self.assertEqual(status, 200, listing)
        finally:
            self.server.chat.provider = previous_provider
            self.server.chat.verification_executor.provider = previous_verification_provider
            self.server.research_tasks._intent_resolver = previous_intent_resolver

        tasks = {task["task_id"]: task for task in listing["tasks"]}
        published_summary = tasks[published["task_id"]]
        self.assertRegex(published_summary["reader_report_id"], r"^report_[0-9a-f]{16}$")
        self.assertEqual(
            published_summary["reader_href"],
            f"/report-reader.html?report_id={published_summary['reader_report_id']}",
        )
        self.assertEqual(published_summary["reader_title"], "完整研究报告")
        self.assertTrue({"report_id", "report_message_id", "report_status", "report_href"}.isdisjoint(
            published_summary))

        no_report_summary = tasks[no_report["task_id"]]
        self.assertTrue({"reader_report_id", "reader_href", "reader_title"}.isdisjoint(no_report_summary))
        validate_response("GET", "/api/research/tasks", 200, listing)
        invalid = json.loads(json.dumps(listing))
        invalid["tasks"][0]["unknown_reader_field"] = True
        with self.assertRaises(RuntimeError):
            validate_response("GET", "/api/research/tasks", 200, invalid)

    def test_unauthenticated_private_get_does_not_disclose_query_contract(self):
        def auth_request(path):
            connection = http.client.HTTPConnection("127.0.0.1", self.auth_port, timeout=5)
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()
        valid = auth_request("/api/chat/run?id=run-1&after=0")
        malformed = auth_request("/api/chat/run?id=run-1&after=true&provider=fake")
        self.assertEqual(valid, malformed)
        self.assertEqual(valid[0], 401)
        self.assertEqual(auth_request("/api/auth/status?unknown=1")[0], 400)

    def test_auth_content_length_with_thousands_of_digits_is_controlled(self):
        length = "9" * 5000
        request = (f"POST /api/auth/login HTTP/1.1\r\nHost: 127.0.0.1:{self.auth_port}\r\n"
                   f"Origin: {self.auth_origin}\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {length}\r\nConnection: close\r\n\r\n").encode("ascii")
        with socket.create_connection(("127.0.0.1", self.auth_port), timeout=5) as connection:
            connection.sendall(request)
            response = b""
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                response += chunk
        status_line, _headers, body = response.partition(b"\r\n\r\n")
        self.assertIn(b" 413 ", status_line.split(b"\r\n", 1)[0])
        self.assertEqual(json.loads(body), {"error": "invalid_request"})

    def test_invalid_output_uses_fixed_non_recursive_fallback(self):
        handler = object.__new__(Handler)
        handler.command = "GET"
        handler.path = "/api/auth/status"
        handler._send = MagicMock()
        with patch("workbench.server.validate_response", side_effect=RuntimeError("bad output")) as validate:
            handler._json(200, {"authentication": "bad"})
        validate.assert_called_once()
        handler._send.assert_called_once_with(500, '{"error":"invalid_response"}')

    def test_private_material_scope_is_still_rejected_by_service(self):
        status, body = self.request(
            "GET", "/api/research/materials/raw?conversation_id=other&document_version_id=missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "conversation_not_found")

    def _pending_decision(self, suffix):
        status, conversation = self.request("POST", "/api/chat/conversations", {"company_id": "MSFT"})
        self.assertEqual(status, 201)
        from tests.test_chat import FakeProvider
        from workbench.worker import DurableWorker

        class ConfirmationProvider(FakeProvider):
            def stream(self, messages, tools, cancel_event):
                handler = next(tool["handler"] for tool in tools
                               if tool["name"] == "request_confirmation")
                handler(kind="basis", question="Choose a basis",
                        explanation="The choice controls the continuation.", multiple=False,
                        options=[{"id": "reported", "label": "Reported", "benefit": "Direct",
                                  "limitation": "May not be comparable"}])
                yield {"type": "usage", "tokens": 1}

        previous = self.server.chat.provider
        self.server.chat.provider = ConfirmationProvider()
        try:
            submitted = self.server.chat.submit(
                conversation["conversation_id"], f"contract decision {suffix}")
            worker = DurableWorker(self.server.chat)
            self.assertTrue(worker.run_once())
            self.assertEqual(self.server.chat.get_run(submitted["run_id"])["status"], "blocked")
            return self.server.chat.decisions.for_run(submitted["run_id"])
        finally:
            self.server.chat.provider = previous

    def test_remaining_strict_contracts_accept_real_http_producer_variants(self):
        status, report = self.request("GET", "/api/report?company=MSFT&format=json")
        self.assertEqual(status, 200)
        validate_response("GET", "/api/report", status, report)
        evidence = report["evidence"][0]
        status, source = self.request("GET", f"/api/source?id={evidence['source_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(source["source"]["sha256"], evidence["source_sha256"])

        status, search = self.request(
            "GET", "/api/documents/search?company=MSFT&query=NaturalContractToken&limit=1")
        self.assertEqual(status, 200)
        passage_id = search["results"][0]["passage_id"]
        status, passage = self.request(
            "GET", f"/api/documents/passage?company=MSFT&id={passage_id}&context=20")
        self.assertEqual(status, 200)
        self.assertIsNone(passage["source_url"])
        self.assertIsNone(passage["published_at"])
        self.assertTrue(passage["tables"])
        self.assertEqual(passage["evidence_ref"]["locator"]["type"], "text_span")

        declined = self._pending_decision("declined")
        status, body = self.request("POST", "/api/research/decisions/answer", {
            "decision_id": declined["decision_id"], "selected_ids": [], "notes": "not now",
            "action": "declined", "request_id": "contract-declined",
        })
        self.assertEqual((status, body["status"], body["run_id"]), (200, "declined", None))

        accepted = self._pending_decision("accepted")
        available = MagicMock()
        available.status.return_value = {
            "available": True, "provider": "contract-test-fake", "reason": "available in test"}
        previous_provider = self.server.chat.provider
        self.server.chat.provider = available
        try:
            status, body = self.request("POST", "/api/research/decisions/answer", {
                "decision_id": accepted["decision_id"], "selected_ids": ["reported"],
                "notes": "", "action": "accepted", "request_id": "contract-accepted",
            })
            with closing(self.server.chat._connect()) as connection:
                queued = connection.execute(
                    "SELECT count(*) FROM jobs WHERE run_id=? AND status='queued'",
                    (body["run_id"],)).fetchone()[0]
            self.assertEqual(queued, 1)
            available.stream.assert_not_called()
        finally:
            self.server.chat.provider = previous_provider
        self.assertEqual((status, body["status"]), (202, "queued"))

    def test_tightened_read_and_intake_contracts_accept_real_http_producers(self):
        status, overview = self.request("GET", "/api/overview")
        self.assertEqual(status, 200)
        company_id = next(item["id"] for item in overview["companies"] if item["id"] == "MSFT")
        source_id = overview["sources"][0]["source_id"]
        status, company = self.request("GET", f"/api/company?id={company_id}")
        self.assertEqual(status, 200)
        claim_id = company["claims"][0]["claim_id"]
        for path in (
            f"/api/evidence?id={claim_id}", f"/api/source?id={source_id}",
            "/api/chat/conversations", "/api/research/tasks", "/api/sample-research/options",
        ):
            self.assertEqual(self.request("GET", path)[0], 200, path)
        status, conversation = self.request("POST", "/api/chat/conversations", {"company_id": company_id})
        self.assertEqual(status, 201)
        self.assertEqual(self.request("GET", "/api/chat/conversations")[0], 200)
        self.assertEqual(self.request("GET", f"/api/chat/conversation?id={conversation['conversation_id']}")[0], 200)
        status, intake = self.request("POST", "/api/research/intake", {
            "question": "Compare available company evidence", "context_conversation_id": None,
        })
        self.assertEqual(status, 200)
        self.assertIn("coverage", intake)


if __name__ == "__main__":
    unittest.main()
