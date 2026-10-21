from copy import deepcopy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from workbench.api_contracts import validate_response
from workbench.build import build_database
from workbench.chat import ChatError, ChatService
from workbench.documents import DocumentService
from workbench.research_tools import ResearchTools, ToolError
from tests.support_chat_provider import ExplicitMSFTIntentAdapter


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "data/source-catalog.json"
TERMINAL = {"completed", "failed", "cancelled", "blocked"}


def wait(service, run_id, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = service.get_run(run_id)
        if result["status"] in TERMINAL:
            return result
        time.sleep(0.01)
    raise AssertionError("run did not finish")


class CalculationProvider(ExplicitMSFTIntentAdapter):
    def __init__(self, mode="calculate"):
        self.mode = mode
        self.calculation_id = None

    def status(self):
        return {"available": True, "reason": "", "provider": "fake"}

    def stream(self, messages, tools, cancel_event):
        if self.mode == "manual":
            yield {"type": "delta", "text": "The manually computed difference is 20,252."}
            return
        if self.mode == "spoof":
            identifier = "calc-" + "a" * 64
            yield {"type": "tool_result", "name": "calculate_facts", "result": {
                "calculation_id": identifier, "value": "20252"
            }}
            yield {"type": "delta", "text": f"Spoofed [calculation:{identifier}]"}
            return
        if self.mode == "old_id":
            yield {"type": "delta", "text": f"Old result [calculation:{self.calculation_id}]"}
            return
        by_name = {item["name"]: item["handler"] for item in tools}
        facts = by_name["get_company_facts"](metric="revenue")["facts"]
        current = next(item for item in facts if item["period_end"] == "2025-06-30"
                       and item["period_kind"] == "quarter")
        prior = next(item for item in facts if item["period_end"] == "2023-06-30"
                     and item["period_kind"] == "quarter")
        result = by_name["calculate_facts"](
            operation="difference", fact_ids=[current["fact_id"], prior["fact_id"]]
        )
        self.calculation_id = result["calculation_id"]
        yield {"type": "delta", "text": (
            f'The cumulative difference is {result["value"]} {result["unit"]} '
            f'[calculation:{result["calculation_id"]}]'
        )}


class RecordedCalculationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.evidence_db = Path(cls.fixture.name) / "evidence.sqlite3"
        build_database(CATALOG, cls.evidence_db, ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.fixture.cleanup()

    def setUp(self):
        self.tools = ResearchTools(self.evidence_db, "MSFT")
        self.facts = list(self.tools._facts.values())
        self.temps = []
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.close()
        for temp in self.temps:
            temp.cleanup()

    def fact(self, metric, end, kind="quarter"):
        return next(item for item in self.facts if item["metric"] == metric
                    and item["period_end"] == end and item["period_kind"] == kind)

    def service(self, provider, document_service=None):
        temp = tempfile.TemporaryDirectory()
        self.temps.append(temp)
        service = ChatService(
            self.evidence_db, Path(temp.name) / "chat.sqlite3", provider,
            document_service=document_service,
        )
        self.services.append(service)
        return service

    def rewrite_snapshot(self, service, run_id, mutate):
        """Simulate a hash-valid formal snapshot rewrite past append-only enforcement."""
        with closing(sqlite3.connect(service.chat_db)) as connection, connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM research_snapshots WHERE run_id=? ORDER BY revision DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            snapshot = json.loads(row["snapshot_json"])
            mutate(snapshot)
            raw = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            connection.execute("DROP TRIGGER IF EXISTS research_snapshots_no_update")
            connection.execute(
                "UPDATE research_snapshots SET snapshot_json=?,snapshot_sha256=? "
                "WHERE run_id=? AND revision=?",
                (raw, hashlib.sha256(raw.encode()).hexdigest(), run_id, row["revision"]),
            )
        return snapshot

    def rewrite_receipt_result(self, service, run_id, tool_name, mutate, *, occurrence=-1):
        """Simulate a hash-valid formal receipt rewrite past append-only enforcement."""
        with closing(sqlite3.connect(service.chat_db)) as connection, connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM author_tool_receipts WHERE run_id=? AND tool_name=? ORDER BY revision",
                (run_id, tool_name),
            ).fetchall()
            row = rows[occurrence]
            result = json.loads(row["result_json"])
            mutate(result)
            result_raw = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            envelope = json.dumps({
                "arguments_sha256": hashlib.sha256(row["arguments_json"].encode()).hexdigest(),
                "call_id": row["call_id"],
                "record_version": "author-tool-receipt/1",
                "result_sha256": hashlib.sha256(result_raw.encode()).hexdigest(),
                "revision": row["revision"],
                "run_id": row["run_id"],
                "tool_name": row["tool_name"],
            }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            connection.execute("DROP TRIGGER IF EXISTS author_tool_receipts_no_update")
            connection.execute(
                "UPDATE author_tool_receipts SET result_json=?,receipt_sha256=? "
                "WHERE run_id=? AND revision=?",
                (result_raw, hashlib.sha256(envelope.encode()).hexdigest(), run_id, row["revision"]),
            )
        return result

    def documents(self, *, duplicate_fy25=False):
        temp = tempfile.TemporaryDirectory()
        self.temps.append(temp)
        documents = DocumentService(Path(temp.name) / "documents")
        for year in (2025, 2024):
            source = ROOT / f"data/raw/microsoft-fy{year}-q4-press-release.html"
            documents.register_file(
                source, company_id="MSFT", source_key=f"fy{year}", title=f"FY{year}",
            )
            if year == 2025 and duplicate_fy25:
                duplicate = documents.register_file(
                    source, company_id="MSFT", source_key="aaa-other-parser",
                    title="FY2025 duplicate parser output",
                )
                with closing(sqlite3.connect(documents.db_path)) as connection, connection:
                    connection.execute(
                        "UPDATE parsed_documents SET parser_version='malicious-parser/9' "
                        "WHERE document_version_id=?", (duplicate["document_version_id"],))
                    connection.execute(
                        "UPDATE parsed_tables SET table_index=table_index+100 "
                        "WHERE document_version_id=?", (duplicate["document_version_id"],))
        return documents

    def test_definition_is_closed_and_bounded(self):
        definition = next(item for item in self.tools.definitions()
                          if item["name"] == "calculate_facts")
        schema = definition["parameters"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["operation"]["enum"],
                         ["difference", "percent_change", "ratio", "sum", "percent_ratio", "percentage_point_difference"])
        self.assertEqual(schema["properties"]["fact_ids"]["minItems"], 2)
        self.assertEqual(schema["properties"]["fact_ids"]["maxItems"], 50)
        self.assertEqual(schema["properties"]["comparison_mode"]["default"], "comparable")

    def test_percentage_ratio_is_same_period_exact_and_replayable(self):
        numerator = deepcopy(self.fact("revenue", "2025-06-30"))
        denominator = deepcopy(numerator)
        numerator.update(fact_id="margin-profit", metric="operating_income", value="7205")
        denominator.update(fact_id="margin-revenue", metric="revenue", value="37895")
        self.tools._facts.update({item["fact_id"]: item for item in (numerator, denominator)})
        ids = [numerator["fact_id"], denominator["fact_id"]]
        result = self.tools.calculate_facts(operation="percent_ratio", fact_ids=ids)
        self.assertEqual((result["value"], result["unit"]), ("19.01", "%"))
        self.assertEqual(result["formula"], "operand[0] / operand[1] * 100")
        self.assertEqual(result, self.tools.replay_arithmetic(result))
        ordinary = self.tools.calculate_facts(operation="ratio", fact_ids=ids)
        self.assertEqual(ordinary["unit"], "x")
        self.assertEqual(ordinary, self.tools.replay_arithmetic(ordinary))
        denominator["value"] = "0"
        with self.assertRaisesRegex(ToolError, "zero"):
            self.tools.calculate_facts(operation="percent_ratio", fact_ids=ids)
        denominator.update(value="37895", period_end="2024-06-30")
        with self.assertRaises(ToolError):
            self.tools.calculate_facts(operation="percent_ratio", fact_ids=ids)

    def test_percentage_point_difference_has_explicit_unit_without_rewriting_old_difference(self):
        # Synthetic values exercise units, not source-backed financial acceptance.
        left = deepcopy(self.fact("revenue", "2025-06-30"))
        right = deepcopy(left)
        left.update(fact_id="percent-left", metric="revenue_growth", value="18.43", unit="%")
        right.update(fact_id="percent-right", metric="profit_growth", value="12.49", unit="%")
        self.tools._facts.update({item["fact_id"]: item for item in (left, right)})
        ids = [left["fact_id"], right["fact_id"]]
        old = self.tools.calculate_facts(operation="difference", fact_ids=ids)
        result = self.tools.calculate_facts(operation="percentage_point_difference", fact_ids=ids)
        self.assertEqual((old["value"], old["unit"]), ("5.94", "%"))
        self.assertEqual((result["value"], result["unit"]), ("5.94", "percentage points"))
        self.assertEqual(old, self.tools.replay_arithmetic(old))
        self.assertEqual(result, self.tools.replay_arithmetic(result))
        right.update(period_start="2024-04-01", period_end="2024-06-30")
        other = self.tools.calculate_facts(operation="percentage_point_difference", fact_ids=ids)
        self.assertEqual(len(other["arithmetic"]["output_context"]["periods"]), 2)
        self.assertIsNone(other["arithmetic"]["output_context"]["period_start"])
        right["unit"] = "USD millions"
        with self.assertRaisesRegex(ToolError, "percentage"):
            self.tools.calculate_facts(operation="percentage_point_difference", fact_ids=ids)

    def test_percentage_points_use_exact_upstream_fractions_not_display_rounding(self):
        facts = []
        for index, value in enumerate(("2", "1", "3")):
            fact = deepcopy(self.fact("revenue", "2025-06-30"))
            fact.update(fact_id=f"ratio-part-{index}", value=value)
            self.tools._facts[fact["fact_id"]] = fact
            facts.append(fact["fact_id"])
        high = self.tools.calculate_facts(operation="percent_ratio", fact_ids=[facts[0], facts[2]])
        low = self.tools.calculate_facts(operation="percent_ratio", fact_ids=[facts[1], facts[2]])
        self.assertEqual((high["value"], low["value"]), ("66.67", "33.33"))
        result = self.tools.calculate_facts(operation="percentage_point_difference",
                                          fact_ids=[high["calculation_id"], low["calculation_id"]])
        self.assertEqual(result["value"], "33.33")  # not the rounded-input subtraction 33.34
        self.assertEqual(result["arithmetic"]["exact_value"], {"numerator": "100", "denominator": "3"})
        self.assertEqual(result, self.tools.replay_arithmetic(result))

    def test_multi_year_difference_and_percent_are_stable_audited_records(self):
        current = self.fact("revenue", "2025-06-30")
        prior = self.fact("revenue", "2023-06-30")
        ids = [current["fact_id"], prior["fact_id"]]
        difference = self.tools.calculate_facts(operation="difference", fact_ids=ids)
        repeated = self.tools.calculate_facts(operation="difference", fact_ids=ids)
        percent = self.tools.calculate_facts(operation="percent_change", fact_ids=ids)
        self.assertEqual(difference["value"], "20252")
        self.assertEqual(percent["value"], "36.04")
        self.assertEqual(difference["calculation_id"], repeated["calculation_id"])
        self.assertRegex(difference["calculation_id"], r"^calc-[0-9a-f]{64}$")
        self.assertEqual(difference["inputs"], [current, prior])
        self.assertEqual(difference["company_id"], "MSFT")
        self.assertEqual(difference["interpretation"], "unverified")
        self.assertEqual(difference["data_version"], self.tools.data_version)

    def test_week_based_fiscal_years_preserve_actual_dates_and_arithmetic(self):
        # Protocol inputs using real fiscal calendar shapes; not a live acceptance.
        prior = deepcopy(self.fact("revenue", "2023-06-30"))
        current = deepcopy(self.fact("revenue", "2025-06-30"))
        prior.update(fact_id="weekly-prior", period_kind="year", period_start="2023-01-30",
                     period_end="2024-01-28", value="60922")
        current.update(fact_id="weekly-current", period_kind="year", period_start="2024-01-29",
                       period_end="2025-01-26", value="130497")
        self.tools._facts.update({prior["fact_id"]: prior, current["fact_id"]: current})
        for operation, expected in (("difference", "69575"), ("percent_change", "114.20")):
            result = self.tools.calculate_facts(operation=operation,
                                                fact_ids=[current["fact_id"], prior["fact_id"]])
            self.assertEqual(result["value"], expected)
            self.assertEqual([fact["period_end"] for fact in result["inputs"]],
                             ["2025-01-26", "2024-01-28"])
            self.assertTrue(any("52/53-week" in item for item in result["limitations"]))
        # 53 weeks ending the same weekday are also valid, but are not normalized.
        current.update(period_end="2025-02-02")
        self.assertEqual(self.tools._require_corresponding_periods(current, prior), "week_based_fiscal_year")
        for changes in ({"period_kind": "quarter"}, {"period_end": "2025-02-01"},
                        {"period_start": "2024-01-28"},
                        {"period_start": "2024-03-04", "period_end": "2025-03-02"}):
            with self.subTest(changes=changes), self.assertRaises(ToolError):
                self.tools._require_corresponding_periods({**current, **changes}, prior)

    def test_same_period_difference_is_arithmetic_not_claimed_growth(self):
        editions = [item for item in self.facts if item["metric"] == "revenue"
                    and item["period_end"] == "2024-06-30"
                    and item["period_kind"] == "quarter"]
        self.assertGreaterEqual(len(editions), 2)
        result = self.tools.calculate_facts(
            operation="difference", fact_ids=[item["fact_id"] for item in editions[:2]])
        self.assertEqual(result["operation"], "difference")
        self.assertEqual(result["interpretation"], "unverified")
        current = self.fact("revenue", "2025-06-30")
        altered = deepcopy(self.fact("revenue", "2024-06-30"))
        altered["fact_id"] = "wrong-calendar-period"
        altered["period_start"] = "2024-01-01"
        self.tools._facts[altered["fact_id"]] = altered
        with self.assertRaisesRegex(ToolError, "corresponding periods"):
            self.tools.calculate_facts(
                operation="difference", fact_ids=[current["fact_id"], altered["fact_id"]])

    def test_presentation_difference_is_same_period_different_basis_only(self):
        left = deepcopy(self.fact("revenue", "2024-06-30"))
        right = deepcopy(left)
        left["fact_id"], right["fact_id"] = "presentation-a", "presentation-b"
        left["basis"], right["basis"] = "as originally presented", "recast presentation"
        left["value"], right["value"] = "100", "90"
        self.tools._facts.update({left["fact_id"]: left, right["fact_id"]: right})
        result = self.tools.calculate_facts(
            operation="difference", fact_ids=[left["fact_id"], right["fact_id"]],
            comparison_mode="presentation_difference")
        self.assertEqual(result["value"], "10")
        self.assertTrue(any("NOT growth" in item for item in result["limitations"]))
        with self.assertRaisesRegex(ToolError, "difference only"):
            self.tools.calculate_facts(
                operation="percent_change", fact_ids=[left["fact_id"], right["fact_id"]],
                comparison_mode="presentation_difference")
        right["period_end"] = "2023-06-30"
        with self.assertRaisesRegex(ToolError, "period_end"):
            self.tools.calculate_facts(
                operation="difference", fact_ids=[left["fact_id"], right["fact_id"]],
                comparison_mode="presentation_difference")

    def test_sum_checks_dimensions_not_financial_metric_whitelists(self):
        dividends = self.fact("payments_of_dividends", "2025-06-30")
        repurchases = self.fact("payments_for_repurchase_of_common_stock", "2025-06-30")
        result = self.tools.calculate_facts(
            operation="sum", fact_ids=[dividends["fact_id"], repurchases["fact_id"]])
        self.assertEqual(result["value"], "10715")
        self.assertTrue(any("not an issuer-reported" in item for item in result["limitations"]))
        reported = self.fact("issuer_reported_capital_return_total", "2025-06-30")
        repeated = self.tools.calculate_facts(operation="sum", fact_ids=[dividends["fact_id"], dividends["fact_id"]])
        self.assertEqual(repeated["value"], str(int(dividends["value"]) * 2))
        # Selecting overlapping amounts is visible, not silently described as a
        # legitimate issuer total. Different bases still require reconciliation.
        with self.assertRaises(ToolError):
            self.tools.calculate_facts(operation="sum", fact_ids=[dividends["fact_id"], reported["fact_id"]])
        prior_repurchases = self.fact("payments_for_repurchase_of_common_stock", "2024-06-30")
        with self.assertRaisesRegex(ToolError, "period_start"):
            self.tools.calculate_facts(
                operation="sum", fact_ids=[dividends["fact_id"], prior_repurchases["fact_id"]])

    def test_nonfinite_zero_cross_company_and_scope_are_rejected(self):
        current = deepcopy(self.fact("revenue", "2025-06-30"))
        prior = deepcopy(self.fact("revenue", "2024-06-30"))
        current["fact_id"], prior["fact_id"] = "finite-current", "finite-prior"
        self.tools._facts.update({current["fact_id"]: current, prior["fact_id"]: prior})
        current["value"] = "NaN"
        with self.assertRaisesRegex(ToolError, "finite"):
            self.tools.calculate_facts(operation="difference",
                                       fact_ids=[current["fact_id"], prior["fact_id"]])
        current["value"], prior["value"] = "1", "-0"
        with self.assertRaisesRegex(ToolError, "zero"):
            self.tools.calculate_facts(operation="percent_change",
                                       fact_ids=[current["fact_id"], prior["fact_id"]])
        current["company_id"] = "AAPL"
        with self.assertRaisesRegex(ToolError, "authorized research scope"):
            self.tools.calculate_facts(operation="difference",
                                       fact_ids=[current["fact_id"], prior["fact_id"]])

    def test_calculate_yoy_keeps_strict_rule_and_adds_audit_contract(self):
        current = self.fact("revenue", "2025-06-30")
        prior = self.fact("revenue", "2024-06-30")
        result = self.tools.calculate_yoy(
            current_fact_id=current["fact_id"], prior_fact_id=prior["fact_id"])
        self.assertEqual(result["value"], "18.10")
        self.assertEqual(result["inputs"], [current, prior])
        self.assertEqual(result["current_fact"], current)
        self.assertEqual(result["prior_fact"], prior)
        self.assertRegex(result["calculation_id"], r"^calc-[0-9a-f]{64}$")

    def test_real_worker_persists_and_reads_authoritative_calculation(self):
        before = self.evidence_db.read_bytes()
        provider = CalculationProvider()
        service = self.service(provider)
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "What was Microsoft revenue?")
        run = wait(service, submitted["run_id"])
        self.assertEqual(run["status"], "completed")
        answer = service.get_conversation(conversation["conversation_id"])["messages"][-1]
        citation = answer["citations"][0]
        self.assertEqual(citation["kind"], "calculation")
        self.assertTrue(citation["valid"])
        detail = service.read_citation(
            conversation["conversation_id"], answer["message_id"], "calculation", citation["id"])
        validate_response("GET", "/api/chat/citation", 200, detail)
        self.assertEqual(detail["detail_schema_version"], "citation-detail/1")
        self.assertEqual(detail["value"], "20252")
        self.assertEqual(detail["interpretation"], "unverified")
        self.assertEqual(len(detail["inputs"]), 2)
        self.assertEqual(len(detail["input_sources"]), 2)
        self.assertTrue(all(item["quote"] and item["locator"] and item["source_sha256"]
                            for item in detail["input_sources"]))
        self.assertIsNone(detail["quote"])
        self.assertEqual(self.evidence_db.read_bytes(), before)

    def test_frozen_tables_require_unique_version_hash_quote_row_and_target_cell(self):
        documents = self.documents()
        provider = CalculationProvider()
        service = self.service(provider, documents)
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "What was Microsoft revenue?")
        wait(service, submitted["run_id"])
        answer = service.get_conversation(conversation["conversation_id"])["messages"][-1]
        detail = service.read_citation(
            conversation["conversation_id"], answer["message_id"], "calculation", provider.calculation_id)
        self.assertTrue(all(item.get("table", {}).get("table_sha256")
                            for item in detail["input_sources"]))
        self.assertEqual(
            [item["table"]["target_cell"]["raw_value"] for item in detail["input_sources"]],
            ["$76,441", "$56,189"],
        )
        self.assertTrue(all(
            item["table"]["origin"] == "historical_frozen_parse"
            and item["table"]["historical_processing_verified"] is True
            for item in detail["input_sources"]
        ))

        with closing(sqlite3.connect(service.chat_db)) as connection, connection:
            row = connection.execute(
                "SELECT seq,payload_json FROM events WHERE run_id=? AND type='data_snapshot'",
                (submitted["run_id"],),
            ).fetchone()
            snapshot = json.loads(row[1])
            pristine = deepcopy(snapshot)
            target_fact = detail["inputs"][0]
            target_source = detail["input_sources"][0]
            version = next(item for item in snapshot["documents"]["coverage"]["documents"]
                           if item["source_sha256"] == target_source["source_sha256"])
            manifest = next(item for item in version["tables"]
                            if item["table_index"] == target_source["table"]["table_index"])
            manifest["table_sha256"] = "0" * 64
            connection.execute(
                "UPDATE events SET payload_json=? WHERE run_id=? AND seq=?",
                (json.dumps(snapshot), submitted["run_id"], row[0]),
            )
        display_only = service.read_citation(
            conversation["conversation_id"], answer["message_id"], "calculation", provider.calculation_id)
        self.assertEqual(display_only["input_sources"][0]["table"], target_source["table"])

        def change_manifest_hash(formal):
            version = next(item for item in formal["documents"]["coverage"]["documents"]
                           if item["source_sha256"] == target_source["source_sha256"])
            manifest = next(item for item in version["tables"]
                            if item["table_index"] == target_source["table"]["table_index"])
            manifest["table_sha256"] = "0" * 64

        self.rewrite_snapshot(service, submitted["run_id"], change_manifest_hash)
        changed_hash = service.read_citation(
            conversation["conversation_id"], answer["message_id"],
            "calculation", provider.calculation_id)
        self.assertNotIn("table", changed_hash["input_sources"][0])

        def change_target_cell(formal):
            formal.clear()
            formal.update(deepcopy(pristine))
            activity = next(item for item in formal["evidence"]["activities"]
                            if item["activity_id"] == target_fact["activity_id"])
            activity["parameters"]["cell_locators"] = {
                target_fact["period_kind"]: {
                    "table_index": target_fact.get("table_index", 0),
                    "row_index": target_source["locator"]["row_index"],
                    "column_index": 2,
                }
            }

        self.rewrite_snapshot(service, submitted["run_id"], change_target_cell)
        changed_cell = service.read_citation(
            conversation["conversation_id"], answer["message_id"],
            "calculation", provider.calculation_id)
        self.assertNotIn("table", changed_cell["input_sources"][0])

    def test_missing_formal_manifest_is_rejected(self):
        documents = self.documents(duplicate_fy25=True)
        provider = CalculationProvider()
        service = self.service(provider, documents)
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "revenue")
        wait(service, submitted["run_id"])
        answer = service.get_conversation(conversation["conversation_id"])["messages"][-1]
        initial = service.read_citation(
            conversation["conversation_id"], answer["message_id"],
            "calculation", provider.calculation_id)
        target = initial["input_sources"][0]

        with closing(sqlite3.connect(service.chat_db)) as connection, connection:
            row = connection.execute(
                "SELECT seq,payload_json FROM events WHERE run_id=? AND type='data_snapshot'",
                (submitted["run_id"],),
            ).fetchone()
            snapshot = json.loads(row[1])
            for version in snapshot["documents"]["coverage"]["documents"]:
                if version.get("source_sha256") == target["source_sha256"]:
                    version.pop("tables", None)
            connection.execute(
                "UPDATE events SET payload_json=? WHERE run_id=? AND seq=?",
                (json.dumps(snapshot), submitted["run_id"], row[0]),
            )

        display_only = service.read_citation(
            conversation["conversation_id"], answer["message_id"],
            "calculation", provider.calculation_id)
        self.assertEqual(display_only["input_sources"][0]["table"], target["table"])

        def remove_manifest(formal):
            for version in formal["documents"]["coverage"]["documents"]:
                if version.get("source_sha256") == target["source_sha256"]:
                    version.pop("tables", None)

        self.rewrite_snapshot(service, submitted["run_id"], remove_manifest)
        # TODO(parent): remove ChatService._frozen_fact_table raw reconstruction branch;
        # a formal current-path snapshot without its manifest must fail closed.
        with self.assertRaises(ChatError):
            service.read_citation(
                conversation["conversation_id"], answer["message_id"],
                "calculation", provider.calculation_id)

    def test_formal_quote_and_archive_tamper_are_rejected(self):
        for attack in ("archive", "quote"):
            with self.subTest(attack=attack):
                documents = self.documents()
                provider = CalculationProvider()
                service = self.service(provider, documents)
                conversation = service.create_conversation("MSFT")
                submitted = service.submit(conversation["conversation_id"], "revenue")
                wait(service, submitted["run_id"])
                answer = service.get_conversation(conversation["conversation_id"])["messages"][-1]
                initial = service.read_citation(
                    conversation["conversation_id"], answer["message_id"],
                    "calculation", provider.calculation_id)
                target = initial["input_sources"][0]
                with closing(sqlite3.connect(service.chat_db)) as connection, connection:
                    row = connection.execute(
                        "SELECT seq,payload_json FROM events WHERE run_id=? AND type='data_snapshot'",
                        (submitted["run_id"],),
                    ).fetchone()
                    snapshot = json.loads(row[1])
                    for version in snapshot["documents"]["coverage"]["documents"]:
                        if version.get("source_sha256") == target["source_sha256"]:
                            version.pop("tables", None)
                    if attack == "quote":
                        evidence = next(
                            item for item in snapshot["evidence"]["evidence"]
                            if item["evidence_id"] == target["evidence_id"]
                        )
                        evidence["quote"] += " mismatch"
                    connection.execute(
                        "UPDATE events SET payload_json=? WHERE run_id=? AND seq=?",
                        (json.dumps(snapshot), submitted["run_id"], row[0]),
                    )
                display_only = service.read_citation(
                    conversation["conversation_id"], answer["message_id"],
                    "calculation", provider.calculation_id)
                self.assertEqual(display_only["input_sources"][0]["quote"], target["quote"])
                if attack == "archive":
                    (documents.objects_path / target["source_sha256"]).write_bytes(b"tampered")
                    detail = service.read_citation(
                        conversation["conversation_id"], answer["message_id"],
                        "calculation", provider.calculation_id)
                    self.assertNotIn("table", detail["input_sources"][0])
                else:
                    def change_quote(formal):
                        evidence = next(
                            item for item in formal["evidence"]["evidence"]
                            if item["evidence_id"] == target["evidence_id"])
                        evidence["quote"] += " mismatch"

                    self.rewrite_snapshot(service, submitted["run_id"], change_quote)
                    with self.assertRaisesRegex(ChatError, "quote or locator"):
                        service.read_citation(
                            conversation["conversation_id"], answer["message_id"],
                            "calculation", provider.calculation_id)

    def test_same_sha_different_parser_and_indices_selects_activity_parser(self):
        documents = self.documents(duplicate_fy25=True)
        provider = CalculationProvider()
        service = self.service(provider, documents)
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "What was Microsoft revenue?")
        wait(service, submitted["run_id"])
        answer = service.get_conversation(conversation["conversation_id"])["messages"][-1]
        detail = service.read_citation(
            conversation["conversation_id"], answer["message_id"], "calculation", provider.calculation_id)
        current = next(item for item in detail["input_sources"]
                       if item["fact_id"] == detail["inputs"][0]["fact_id"])
        self.assertEqual(current["table"]["parser_version"], "visible-html-tables/1.0.0")
        self.assertEqual(current["table"]["table_index"], 0)
        self.assertEqual(current["table"]["target_cell"]["raw_value"], "$76,441")

    def test_spoof_old_run_and_manual_arithmetic_do_not_create_records(self):
        for mode in ("spoof", "manual"):
            provider = CalculationProvider(mode)
            service = self.service(provider)
            conversation = service.create_conversation("MSFT")
            submitted = service.submit(conversation["conversation_id"], "derive a number")
            run = wait(service, submitted["run_id"])
            answer = service.get_conversation(conversation["conversation_id"])["messages"][-1]
            self.assertEqual(answer["verification"]["status"], "unverified")
            self.assertFalse(any(event["type"] == "tool_result" and
                                 event.get("name") in {"calculate_facts", "calculate_yoy"}
                                 for event in run["events"]))

        provider = CalculationProvider()
        service = self.service(provider)
        conversation = service.create_conversation("MSFT")
        first = service.submit(conversation["conversation_id"], "first")
        wait(service, first["run_id"])
        provider.mode = "old_id"
        second = service.submit(conversation["conversation_id"], "reuse it")
        wait(service, second["run_id"])
        answer = service.get_conversation(conversation["conversation_id"])["messages"][-1]
        self.assertFalse(answer["citations"][0]["valid"])

    def test_read_rejects_recomputed_mismatch(self):
        provider = CalculationProvider()
        service = self.service(provider)
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "What was Microsoft revenue?")
        wait(service, submitted["run_id"])
        answer = service.get_conversation(conversation["conversation_id"])["messages"][-1]
        identifier = answer["citations"][0]["id"]
        with closing(sqlite3.connect(service.chat_db)) as connection, connection:
            row = connection.execute(
                "SELECT seq,payload_json FROM events WHERE run_id=? AND type='tool_result' "
                "AND json_extract(payload_json,'$.name')='calculate_facts'",
                (submitted["run_id"],),
            ).fetchone()
            payload = json.loads(row[1])
            payload["result"]["value"] = "invented"
            connection.execute("UPDATE events SET payload_json=? WHERE run_id=? AND seq=?",
                               (json.dumps(payload), submitted["run_id"], row[0]))
        unchanged = service.read_citation(
            conversation["conversation_id"], answer["message_id"], "calculation", identifier)
        self.assertEqual(unchanged["value"], "20252")
        self.rewrite_receipt_result(
            service, submitted["run_id"], "calculate_facts",
            lambda result: result.__setitem__("value", "invented"),
        )
        with self.assertRaisesRegex(ChatError, "recomputation"):
            service.read_citation(conversation["conversation_id"], answer["message_id"],
                                  "calculation", identifier)


    def test_read_rejects_missing_frozen_input_hash(self):
        provider = CalculationProvider()
        service = self.service(provider)
        conversation = service.create_conversation("MSFT")
        submitted = service.submit(conversation["conversation_id"], "What was Microsoft revenue?")
        wait(service, submitted["run_id"])
        answer = service.get_conversation(conversation["conversation_id"])["messages"][-1]
        identifier = answer["citations"][0]["id"]
        with closing(sqlite3.connect(service.chat_db)) as connection, connection:
            row = connection.execute(
                "SELECT seq,payload_json FROM events WHERE run_id=? AND type='data_snapshot'",
                (submitted["run_id"],),
            ).fetchone()
            payload = json.loads(row[1])
            input_ids = {item["evidence_id"] for item in next(
                event["result"]["inputs"] for event in
                [json.loads(value[0]) for value in connection.execute(
                    "SELECT payload_json FROM events WHERE run_id=? AND type='tool_result'",
                    (submitted["run_id"],))]
                if event.get("name") == "calculate_facts"
            )}
            for evidence in payload["evidence"]["evidence"]:
                if evidence["evidence_id"] in input_ids:
                    evidence.pop("source_sha256", None)
                    break
            connection.execute("UPDATE events SET payload_json=? WHERE run_id=? AND seq=?",
                               (json.dumps(payload), submitted["run_id"], row[0]))
        unchanged = service.read_citation(
            conversation["conversation_id"], answer["message_id"], "calculation", identifier)
        self.assertEqual(unchanged["value"], "20252")

        def remove_input_hash(snapshot):
            for evidence in snapshot["evidence"]["evidence"]:
                if evidence["evidence_id"] in input_ids:
                    evidence.pop("source_sha256", None)
                    break

        self.rewrite_snapshot(service, submitted["run_id"], remove_input_hash)
        with self.assertRaisesRegex(ChatError, "hash or locator"):
            service.read_citation(conversation["conversation_id"], answer["message_id"],
                                  "calculation", identifier)


if __name__ == "__main__":
    unittest.main()
