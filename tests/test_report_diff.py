from copy import deepcopy
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from workbench.build import build_database
from workbench.chat import ChatError
from workbench.report_reader import build_report_diff, load_report_diff
from workbench.server import EvidenceHTTPServer


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def closure(report_id, conversation_id, *, claim, evidence_id, calculation_id, limitation):
    artifact_id = f"artifact-{report_id}"
    payload = {
        "report": {
            "id": report_id,
            "version_id": f"version-{report_id}",
            "conversation_id": conversation_id,
            "message_id": f"message-{report_id}",
        },
        "payload_sha256": ("a" if report_id == "old" else "b") * 64,
    }
    artifact = {
        "artifact_id": artifact_id,
        "findings": [{
            "text": claim,
            "classification": "source_statement",
            "reasoning": "只比较保存的推理文本",
            "limitations": [],
        }],
        "traces": [{
            "reference": {"kind": "evidence", "id": evidence_id},
            "source": {"source_sha256": evidence_id.ljust(64, "0"), "locator": {"row": 1}},
        }],
        "calculations": [{
            "calculation_id": calculation_id,
            "formula": "a/b",
            "value": "1",
            "status": "recorded",
        }],
        "limitations": [limitation],
        "verification": {"limitations": []},
    }
    return payload, artifact


class ReportDiffPureFunctionTests(unittest.TestCase):
    def test_deterministic_hash_conservative_claims_and_all_sections(self):
        old_payload, old_artifact = closure(
            "old", "conversation", claim="收入为 10", evidence_id="e-old",
            calculation_id="calc-shared", limitation="旧限制")
        new_payload, new_artifact = closure(
            "new", "conversation", claim="收入为 11", evidence_id="e-new",
            calculation_id="calc-shared", limitation="新限制")
        old_before = deepcopy((old_payload, old_artifact))
        new_before = deepcopy((new_payload, new_artifact))

        first = build_report_diff(old_payload, old_artifact, new_payload, new_artifact)
        second = build_report_diff(old_payload, old_artifact, new_payload, new_artifact)

        self.assertEqual(first, second)
        self.assertEqual((old_payload, old_artifact), old_before)
        self.assertEqual((new_payload, new_artifact), new_before)
        self.assertEqual(first["schema_version"], "report-diff/1")
        unhashed = dict(first)
        digest = unhashed.pop("diff_sha256")
        self.assertEqual(digest, hashlib.sha256(canonical(unhashed).encode()).hexdigest())
        self.assertEqual([item["text"] for item in first["sections"]["claims"]["removed"]],
                         ["收入为 10"])
        self.assertEqual([item["text"] for item in first["sections"]["claims"]["added"]],
                         ["收入为 11"])
        self.assertEqual(first["sections"]["claims"]["unchanged"], [])
        self.assertEqual(len(first["sections"]["evidence"]["removed"]), 1)
        self.assertEqual(len(first["sections"]["evidence"]["added"]), 1)
        self.assertEqual(len(first["sections"]["calculations"]["unchanged"]), 1)
        self.assertEqual(len(first["sections"]["limitations"]["removed"]), 1)
        self.assertEqual(len(first["sections"]["limitations"]["added"]), 1)

    def test_value_formation_and_attribution_changes_are_claim_changes(self):
        old_payload, old_artifact = closure(
            "old", "conversation", claim="收入为 10", evidence_id="e",
            calculation_id="calc", limitation="相同限制")
        new_payload, new_artifact = closure(
            "new", "conversation", claim="收入为 10", evidence_id="e",
            calculation_id="calc", limitation="相同限制")
        for artifact, formation, actor in (
                (old_artifact, "observed", "issuer-a"),
                (new_artifact, "estimated", "issuer-b")):
            artifact["findings"][0].update(id="claim", references=[
                {"kind": "evidence", "id": "e", "valid": True}])
            artifact["anchors"] = [{
                "finding_id": "claim", "text": "10", "value_formation": formation,
                "attribution": {
                    "principal": {"type": "organization", "identifier": actor, "label": actor},
                    "stated_at": "2026-01-01T00:00:00Z",
                    "statement_reference": {"kind": "evidence", "id": "e"},
                },
                "references": [{"kind": "evidence", "id": "e", "valid": True}],
                "calculation_id": None,
            }]

        result = build_report_diff(old_payload, old_artifact, new_payload, new_artifact)

        self.assertEqual(result["sections"]["claims"]["unchanged"], [])
        self.assertEqual(result["sections"]["claims"]["removed"][0]["value_bindings"][0]["value_formation"],
                         "observed")
        self.assertEqual(result["sections"]["claims"]["added"][0]["value_bindings"][0]["value_formation"],
                         "estimated")

    def test_swapping_claim_references_is_not_unchanged(self):
        old_payload, old_artifact = closure(
            "old", "conversation", claim="第一项", evidence_id="e1",
            calculation_id="calc", limitation="相同限制")
        new_payload, new_artifact = closure(
            "new", "conversation", claim="第一项", evidence_id="e1",
            calculation_id="calc", limitation="相同限制")
        second = {"text": "第二项", "classification": "source_statement",
                  "reasoning": "只比较保存的推理文本", "limitations": []}
        old_artifact["findings"].append(deepcopy(second))
        new_artifact["findings"].append(deepcopy(second))
        old_artifact["findings"][0]["references"] = [{"kind": "evidence", "id": "e1"}]
        old_artifact["findings"][1]["references"] = [{"kind": "evidence", "id": "e2"}]
        new_artifact["findings"][0]["references"] = [{"kind": "evidence", "id": "e2"}]
        new_artifact["findings"][1]["references"] = [{"kind": "evidence", "id": "e1"}]

        result = build_report_diff(old_payload, old_artifact, new_payload, new_artifact)

        self.assertEqual(len(result["sections"]["claims"]["removed"]), 2)
        self.assertEqual(len(result["sections"]["claims"]["added"]), 2)
        self.assertEqual(result["sections"]["claims"]["unchanged"], [])

    def test_report_title_change_is_not_unchanged(self):
        old_payload, old_artifact = closure(
            "old", "conversation", claim="相同文本", evidence_id="e",
            calculation_id="calc", limitation="相同限制")
        new_payload, new_artifact = closure(
            "new", "conversation", claim="相同文本", evidence_id="e",
            calculation_id="calc", limitation="相同限制")
        old_artifact["publication"] = {"title": "旧标题"}
        new_artifact["publication"] = {"title": "新标题"}

        result = build_report_diff(old_payload, old_artifact, new_payload, new_artifact)

        removed_titles = [item for item in result["sections"]["claims"]["removed"]
                          if item["object_kind"] == "report_title"]
        added_titles = [item for item in result["sections"]["claims"]["added"]
                        if item["object_kind"] == "report_title"]
        self.assertEqual((removed_titles[0]["text"], added_titles[0]["text"]), ("旧标题", "新标题"))

    def test_same_calculation_id_with_changed_content_is_not_unchanged(self):
        old_payload, old_artifact = closure(
            "old", "conversation", claim="相同文本", evidence_id="e",
            calculation_id="calc-stable", limitation="相同限制")
        new_payload, new_artifact = closure(
            "new", "conversation", claim="相同文本", evidence_id="e",
            calculation_id="calc-stable", limitation="相同限制")
        new_artifact["calculations"][0]["value"] = "2"

        result = build_report_diff(old_payload, old_artifact, new_payload, new_artifact)

        self.assertEqual(result["sections"]["calculations"]["unchanged"], [])
        self.assertEqual(result["sections"]["calculations"]["removed"][0]["value"], "1")
        self.assertEqual(result["sections"]["calculations"]["added"][0]["value"], "2")

    def test_claim_requires_exact_text_classification_and_reasoning(self):
        old_payload, old_artifact = closure(
            "old", "conversation", claim="相同文本", evidence_id="e",
            calculation_id="old-calc", limitation="相同限制")
        new_payload, new_artifact = closure(
            "new", "conversation", claim="相同文本", evidence_id="e",
            calculation_id="new-calc", limitation="相同限制")
        new_artifact["findings"][0]["reasoning"] = "不同推理"
        result = build_report_diff(old_payload, old_artifact, new_payload, new_artifact)
        self.assertEqual(len(result["sections"]["claims"]["removed"]), 1)
        self.assertEqual(len(result["sections"]["claims"]["added"]), 1)
        self.assertEqual(result["sections"]["claims"]["unchanged"], [])


class ReportDiffLoadingTests(unittest.TestCase):
    def test_cross_conversation_is_rejected_before_loading_snapshots(self):
        identities = {
            "old": {"report_id": "old", "conversation_id": "one", "message_id": "m1"},
            "new": {"report_id": "new", "conversation_id": "two", "message_id": "m2"},
        }
        with mock.patch("workbench.report_reader.resolve_report",
                        side_effect=lambda _chat, report_id: identities[report_id]), mock.patch(
                "workbench.report_reader.load_required_reader_snapshots") as snapshots:
            with self.assertRaises(ChatError) as caught:
                load_report_diff(object(), old_report_id="old", new_report_id="new")
        self.assertEqual(caught.exception.code, "report_diff_conversation_mismatch")
        snapshots.assert_not_called()


class ReportDiffHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        evidence = Path(self.temp.name) / "evidence.sqlite3"
        build_database(db_path=evidence)
        self.server = EvidenceHTTPServer(
            0, evidence, chat_db=Path(self.temp.name) / "chat.sqlite3")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def get(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_http_parameters_and_success(self):
        status, body = self.get("/api/reports/diff?old_report_id=old")
        self.assertEqual((status, body["error"]), (400, "Invalid request parameters"))
        status, body = self.get(
            "/api/reports/diff?old_report_id=old&old_report_id=again&new_report_id=new")
        self.assertEqual((status, body["error"]), (400, "Invalid request parameters"))

        expected = {"schema_version": "report-diff/1", "diff_sha256": "d" * 64}
        with mock.patch("workbench.server.load_report_diff", return_value=expected) as loader:
            status, body = self.get(
                "/api/reports/diff?old_report_id=old&new_report_id=new")
        self.assertEqual((status, body), (200, expected))
        loader.assert_called_once_with(
            self.server.chat, old_report_id="old", new_report_id="new")


if __name__ == "__main__":
    unittest.main()
