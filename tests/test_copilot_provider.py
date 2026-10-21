import asyncio
import hashlib
import json
from pathlib import Path
import queue
import threading
import time
from types import SimpleNamespace
import zipfile

import unittest
import tempfile
from unittest.mock import patch
from contextlib import ExitStack

from workbench.copilot_provider import CopilotProvider


class FakeData:
    pass


class Delta(FakeData):
    def __init__(self, text="ok", message_id="m1"):
        self.delta_content, self.message_id = text, message_id


class Message(FakeData):
    def __init__(self, content="complete", message_id="m1", phase=None, tool_requests=None):
        self.content = content
        self.message_id = message_id
        self.phase = phase
        self.tool_requests = tool_requests


class Usage(FakeData):
    model, input_tokens, output_tokens = "fake", 1, 2


class Error(FakeData):
    message = "fake error"


class Idle(FakeData):
    pass


class ToolResult:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Tool:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Connection:
    @staticmethod
    def for_stdio(**kwargs):
        return kwargs


class Session:
    def __init__(self):
        self.handler = None
        self.aborted = False

    def on(self, handler):
        self.handler = handler
        return lambda: None

    async def send(self, prompt):
        self.prompt = prompt
        self.handler(SimpleNamespace(data=Delta()))
        self.handler(SimpleNamespace(data=Usage()))
        self.handler(SimpleNamespace(data=Idle()))

    async def abort(self):
        self.aborted = True

    async def disconnect(self):
        pass


class PhasedSession(Session):
    async def send(self, prompt):
        self.prompt = prompt
        for message in (
            Message("Searching sources", "comment-1", "commentary"),
            Message("Checking another source", "comment-2", "commentary"),
            Message("The final answer", "final-1", "final_answer"),
        ):
            self.handler(SimpleNamespace(data=Delta(message.content, message.message_id)))
            self.handler(SimpleNamespace(data=message))
        self.handler(SimpleNamespace(data=Idle()))


class UnphasedSession(Session):
    async def send(self, prompt):
        self.prompt = prompt
        self.handler(SimpleNamespace(data=Message(
            "Tool commentary", "tool-message", None, [object()]
        )))
        self.handler(SimpleNamespace(data=Message("Fallback answer", "answer-message")))
        self.handler(SimpleNamespace(data=Idle()))


class Client:
    last = None

    def __init__(self, **kwargs):
        Client.last = self
        self.options = kwargs
        self.session = Session()

    async def start(self):
        pass

    async def stop(self):
        pass

    async def get_auth_status(self):
        return SimpleNamespace(isAuthenticated=True)

    async def create_session(self, **kwargs):
        self.session_options = kwargs
        return self.session


class PhasedClient(Client):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.session = PhasedSession()


class UnphasedClient(Client):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.session = UnphasedSession()


class FalseAuthClient(Client):
    async def get_auth_status(self):
        return SimpleNamespace(isAuthenticated=False)


class HangingStartClient(Client):
    cancelled = False

    async def start(self):
        try:
            await asyncio.Event().wait()
        finally:
            type(self).cancelled = True


class HangingStopClient(Client):
    cancelled = False

    async def stop(self):
        try:
            await asyncio.Event().wait()
        finally:
            type(self).cancelled = True


def fake_sdk(client=Client):
    return {"CopilotClient": client, "RuntimeConnection": Connection, "Tool": Tool,
            "ToolResult": ToolResult, "AssistantMessageData": Message,
            "AssistantMessageDeltaData": Delta, "AssistantUsageData": Usage,
            "SessionErrorData": Error, "SessionIdleData": Idle}


def config(tmp_path, **extra):
    value = {"cli_path": str(tmp_path / "copilot"), "base_directory": str(tmp_path / "home"),
             "authentication_mode": "logged_in_user", "timeout_seconds": 1, "model": None}
    value.update(extra)
    return value


class CopilotProviderTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tmp_path = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))

    def setattr(self, target, *args):
        if isinstance(target, str):
            self.stack.enter_context(patch(target, args[0]))
        else:
            self.stack.enter_context(patch.object(target, args[0], args[1]))

    def test_validate_defaults_to_no_tools(self):
        messages, tools = CopilotProvider._validate([{"role": "user", "content": "hi"}], [])
        assert messages[-1]["content"] == "hi"
        assert tools == []


    def test_rejects_invalid_and_duplicate_tools(self):
        valid = {"name": "read", "description": "d", "parameters": {}, "handler": lambda: None}
        with self.assertRaises(ValueError):
            CopilotProvider._validate([{"role": "user", "content": "hi"}], [valid, valid])
        with self.assertRaises(ValueError):
            CopilotProvider._validate([{"role": "user", "content": "hi"}], [{**valid, "name": "bad:name"}])


    def test_fake_sdk_adapter_allowlists_only_custom_tools(self):
        tmp_path = self.tmp_path
        monkeypatch = self
        provider = CopilotProvider()
        monkeypatch.setattr(provider, "_import_sdk", fake_sdk)
        events = queue.Queue()
        spec = {"name": "lookup", "description": "read", "parameters": {"type": "object"},
                "handler": lambda **kwargs: kwargs}
        asyncio.run(provider._run_sdk(config(tmp_path), [{"role": "user", "content": "hi"}],
                                      [spec], threading.Event(), events))
        options = Client.last.session_options
        assert options["available_tools"] == ["custom:lookup"]
        assert options["excluded_tools"] == ["builtin:*", "mcp:*"]
        assert options["mcp_servers"] == {} and options["enable_skills"] is False
        assert options["custom_agents"] == [] and options["request_extensions"] is False
        assert options["enable_mcp_apps"] is False
        assert options["on_permission_request"] is None
        contract = options["system_message"]
        assert contract["mode"] == "append"
        assert "only the data tools explicitly loaded" in contract["content"]
        assert "not web access" in contract["content"]
        assert "[claim:id]" in contract["content"] and "[evidence:id]" in contract["content"]
        assert "Never guess or invent numbers" in contract["content"]
        assert "Answer when the requested question is supported" in contract["content"]
        assert "Avoid redundant work" in contract["content"]
        assert "Explicitly identify remaining" in contract["content"]
        assert "Tool-call budget metadata is authoritative" in contract["content"]
        assert "retrieval_closed result is a policy closure, not a no-match" in contract["content"]
        assert "leave unresolved items" in contract["content"]
        assert "Without explicit approval" in contract["content"]
        assert Client.last.options["mode"] == "copilot-cli"
        assert "base_directory" not in Client.last.options
        assert Client.last.options["enable_remote_sessions"] is False
        assert Client.last.options["env"]["COPILOT_SKIP_CLI_DOWNLOAD"] == "1"
        assert Client.last.options["env"]["COPILOT_DISABLE_AUTO_UPDATE"] == "1"
        assert Client.last.options["env"]["COPILOT_AUTO_UPDATE"] == "false"
        assert Client.last.options["env"]["GH_NO_UPDATE_NOTIFIER"] == "1"
        assert Client.last.options["env"]["COPILOT_DISABLE_TELEMETRY"] == "1"

    def test_runtime_controls_do_not_inherit_parent_loader_or_replace_auth_home(self):
        with patch.dict("os.environ", {
                "HOME": "/original-home", "COPILOT_AUTO_UPDATE": "true",
                "COPILOT_CLI_DIST_DIR": "/another-version",
                "COPILOT_CLI_RESOLVED_DIST_DIR": "/another-resolved-version",
                "COPILOT_CLI_BINARY_VERSION": "9.9.9", "COPILOT_LOADER_PID": "123",
        }, clear=True):
            environment = CopilotProvider._runtime_environment()
        assert environment["HOME"] == "/original-home"
        assert environment["COPILOT_AUTO_UPDATE"] == "false"
        assert all(name not in environment for name in (
            "COPILOT_CLI_DIST_DIR", "COPILOT_CLI_RESOLVED_DIST_DIR",
            "COPILOT_CLI_BINARY_VERSION", "COPILOT_LOADER_PID"))

    def test_regression_sdk_wire_preserves_ids_while_full_handler_result_remains_intact(self):
        class CallingSession(Session):
            async def send(self, prompt):
                tool = Client.last.session_options["tools"][0]
                self.response = await tool.handler(SimpleNamespace(arguments={}, tool_call_id="wire-ols"))
                self.handler(SimpleNamespace(data=Idle()))
        class CallingClient(Client):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.session = CallingSession()
        model = {"method": "OLS", "equation": "Y=a+bX", "sample_count": 40, "y_transform": "level",
                 "lag_years": 0, "limitations": ["not causal"]}
        records = [{"calculation_id": "calc-" + letter * 64, "operation": operation,
                    "formula": "recorded formula", "value": value, "unit": unit,
                    "regression": model, "inputs": ["full source record" * 1000]}
                   for letter, operation, value, unit in (
                       ("a", "regression_slope", "2.000000", "x"),
                       ("b", "regression_intercept", "1.000000", "USD millions"),
                       ("c", "regression_r_squared", "100.000000", "%"))]
        full = {"calculations": records, "report_values": [
            {"value_text": f"{item['value']} {item['unit']}", "citation": f"[calculation:{item['calculation_id']}]"}
            for item in records], "interpretation": "unverified"}
        provider = CopilotProvider()
        self.setattr(provider, "_import_sdk", lambda: fake_sdk(CallingClient))
        spec = {"name": "calculate_regression", "description": "recorded OLS",
                "parameters": {"type": "object"}, "handler": lambda: full}
        asyncio.run(provider._run_sdk(config(self.tmp_path), [{"role": "user", "content": "OLS"}],
                                      [spec], threading.Event(), queue.Queue()))
        response = Client.last.session.response
        self.assertEqual(response.result_type, "success")
        wire = json.loads(response.text_result_for_llm)
        self.assertLess(len(response.text_result_for_llm.encode()), 8000)
        self.assertEqual(wire["report_values"], full["report_values"])
        self.assertEqual([item["calculation_id"] for item in wire["calculations"]],
                         [item["calculation_id"] for item in records])
        self.assertGreater(len(json.dumps(full)), 50000)
        self.assertNotIn("inputs", wire["calculations"][0])

    def test_forecast_sdk_wire_keeps_assumptions_and_every_output_reference(self):
        class CallingSession(Session):
            async def send(self, prompt):
                tool = Client.last.session_options["tools"][0]
                self.response = await tool.handler(SimpleNamespace(arguments={}, tool_call_id="wire-forecast"))
                self.handler(SimpleNamespace(data=Idle()))
        class CallingClient(Client):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.session = CallingSession()
        full = {"company_id": "MSFT", "calculations": [{"inputs": [
                    {"evidence_id": "doccell-" + "b" * 64, "value": "100", "unit": "USD millions", "extra": "full source" * 10000}]}],
                "model": {"periods": [{"cfo_growth_percent": "10", "capex_growth_percent": "20",
                                      "classification": "bridge_estimate"}]},
                "report_values": [{"value_text": "62.000000 USD millions", "citation": "[calculation:calc-" + "a" * 64 + "]"}],
                "instruction": "conditional estimates only", "interpretation": "conditional_scenario"}
        provider = CopilotProvider()
        self.setattr(provider, "_import_sdk", lambda: fake_sdk(CallingClient))
        spec = {"name": "calculate_forecast", "description": "recorded scenario",
                "parameters": {"type": "object"}, "handler": lambda: full}
        asyncio.run(provider._run_sdk(config(self.tmp_path), [{"role": "user", "content": "scenario"}],
                                      [spec], threading.Event(), queue.Queue()))
        response = Client.last.session.response
        self.assertEqual(response.result_type, "success")
        wire = json.loads(response.text_result_for_llm)
        self.assertEqual(wire["report_values"], full["report_values"])
        self.assertEqual(wire["model"], full["model"])
        self.assertEqual(wire["baseline_sources"][0]["citation"], "[evidence:doccell-" + "b" * 64 + "]")
        self.assertNotIn("calculations", wire)
        self.assertGreater(len(json.dumps(full)), 100000)
        self.assertLess(len(response.text_result_for_llm), 8000)

    def test_stop_after_success_ignores_rejection_then_closes_without_idle_or_shared_cancel(self):
        class SubmitSession(Session):
            disconnected = False

            async def send(self, prompt):
                self.prompt = prompt
                tool = Client.last.session_options["tools"][0]
                first = await tool.handler(SimpleNamespace(arguments={"attempt": 1}, tool_call_id="bad"))
                self.assert_failure = first.result_type
                # A failed validation must not schedule session completion.
                await asyncio.sleep(0)
                second = await tool.handler(SimpleNamespace(arguments={"attempt": 2}, tool_call_id="good"))
                self.assert_success = second.result_type
                # Deliberately never emit SessionIdleData.

            async def disconnect(self):
                self.disconnected = True

        class SubmitClient(Client):
            stopped = False

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.session = SubmitSession()

            async def stop(self):
                self.stopped = True

        attempts = []

        def submit(attempt):
            attempts.append(attempt)
            if attempt == 1:
                raise ValueError("controlled invalid submission")
            return {"accepted": True}

        provider = CopilotProvider()
        self.setattr(provider, "_import_sdk", lambda: fake_sdk(SubmitClient))
        cancelled = threading.Event()
        spec = {"name": "submit", "description": "submit once", "parameters": {"type": "object"},
                "handler": submit, "stop_after_success": True}
        asyncio.run(provider._run_sdk(
            config(self.tmp_path), [{"role": "user", "content": "review"}],
            [spec], cancelled, queue.Queue()))

        session = SubmitClient.last.session
        self.assertEqual(attempts, [1, 2])
        self.assertEqual((session.assert_failure, session.assert_success), ("failure", "success"))
        self.assertFalse(session.aborted)
        self.assertTrue(session.disconnected)
        self.assertTrue(SubmitClient.last.stopped)
        self.assertFalse(cancelled.is_set())

    def test_tool_validation_failure_preserves_machine_readable_budget(self):
        from workbench.research_tools import ToolError
        provider = CopilotProvider()
        self.setattr(provider, "_import_sdk", fake_sdk)
        def fail(**kwargs):
            error = ToolError("invalid_arguments", "copy the exact cell locator")
            error.tool_budget = {"attempts": 21, "remaining": 3, "limit": 24}
            raise error
        spec = {"name": "extract", "description": "read", "parameters": {"type": "object"}, "handler": fail}
        asyncio.run(provider._run_sdk(config(self.tmp_path), [{"role": "user", "content": "hi"}],
                                     [spec], threading.Event(), queue.Queue()))
        tool = Client.last.session_options["tools"][0]
        result = asyncio.run(tool.handler(SimpleNamespace(arguments={}, tool_call_id="bad-cell")))
        self.assertEqual(result.result_type, "failure")
        payload = json.loads(result.text_result_for_llm)
        self.assertEqual(payload["error"], "invalid_arguments")
        self.assertEqual(payload["_tool_budget"]["remaining"], 3)

    def test_completed_messages_emit_phases_and_explicit_final_answer(self):
        provider = CopilotProvider()
        self.setattr(provider, "_import_sdk", lambda: fake_sdk(PhasedClient))
        events = queue.Queue()
        asyncio.run(provider._run_sdk(
            config(self.tmp_path), [{"role": "user", "content": "hi"}], [],
            threading.Event(), events
        ))
        emitted = [item[1] for item in list(events.queue) if item[0] == "event"]
        messages = [item for item in emitted if item["type"] == "assistant_message"]
        assert [item["phase"] for item in messages] == ["commentary", "commentary", "final_answer"]
        assert [item for item in emitted if item["type"] == "final_answer"] == [
            {"type": "final_answer", "text": "The final answer"}
        ]

    def test_unphased_last_non_tool_message_is_final_fallback(self):
        provider = CopilotProvider()
        self.setattr(provider, "_import_sdk", lambda: fake_sdk(UnphasedClient))
        events = queue.Queue()
        asyncio.run(provider._run_sdk(
            config(self.tmp_path), [{"role": "user", "content": "hi"}], [],
            threading.Event(), events
        ))
        emitted = [item[1] for item in list(events.queue) if item[0] == "event"]
        assert [item for item in emitted if item["type"] == "final_answer"] == [
            {"type": "final_answer", "text": "Fallback answer"}
        ]

    def test_logged_in_user_inherits_complete_environment_without_auth_overrides(self):
        provider = CopilotProvider()
        self.setattr(provider, "_import_sdk", fake_sdk)
        with patch.dict("os.environ", {"HOME": "/host-home", "PATH": "/host-bin",
                                        "GITHUB_TOKEN": "ambient"}, clear=True):
            asyncio.run(provider._run_sdk(
                config(self.tmp_path), [{"role": "user", "content": "hi"}], [],
                threading.Event(), queue.Queue()
            ))
        env = Client.last.options["env"]
        assert env["HOME"] == "/host-home" and env["PATH"] == "/host-bin"
        assert env["GITHUB_TOKEN"] == "ambient"
        assert "COPILOT_HOME" not in env
        assert "base_directory" not in Client.last.options

    def test_explicit_isolated_mode_keeps_legacy_empty_runtime(self):
        provider = CopilotProvider()
        self.setattr(provider, "_import_sdk", fake_sdk)
        asyncio.run(provider._run_sdk(
            config(self.tmp_path, authentication_mode="isolated"),
            [{"role": "user", "content": "hi"}], [], threading.Event(), queue.Queue()
        ))
        assert Client.last.options["mode"] == "empty"
        assert Client.last.options["base_directory"] == str(self.tmp_path / "home")
        assert Client.last.options["env"]["HOME"] == str(self.tmp_path / "home")
        assert Client.last.options["env"]["GITHUB_TOKEN"] == ""

    def test_real_run_rejects_false_sdk_auth_before_session(self):
        provider = CopilotProvider()
        self.setattr(provider, "_import_sdk", lambda: fake_sdk(FalseAuthClient))
        with self.assertRaisesRegex(RuntimeError, "not authenticated"):
            asyncio.run(provider._run_sdk(
                config(self.tmp_path), [{"role": "user", "content": "hi"}], [],
                threading.Event(), queue.Queue()
            ))
        assert not hasattr(FalseAuthClient.last, "session_options")

    def _status_provider(self, authenticated):
        path = self.tmp_path / "status.json"
        path.write_text(json.dumps({
            "provider": "github-copilot-sdk", "automatic_downloads": False,
            "automatic_updates": False, "sdk_version": "1.0.11",
            "cli_version": "1.0.79", "enabled": True,
            "authentication_mode": "logged_in_user",
        }), encoding="utf-8")
        provider = CopilotProvider(path)
        self.setattr(provider, "_verify_locked_runtime", lambda value: None)
        self.setattr(provider, "_import_sdk", lambda: {})
        self.setattr("workbench.copilot_provider.metadata.version", lambda name: "1.0.11")
        calls = []
        self.setattr(provider, "_run_auth_probe",
                     lambda value: calls.append(value) is None and authenticated)
        return provider, calls

    def test_status_uses_real_auth_without_manual_confirmation_and_caches_it(self):
        provider, calls = self._status_provider(True)
        first = provider.status()
        second = provider.status()
        assert first["available"] is True and first["authenticated"] is True
        assert first["authentication_status"] == "ready"
        assert second["available"] is True and len(calls) == 1

    def test_status_false_auth_is_unavailable(self):
        provider, calls = self._status_provider(False)
        state = provider.status()
        assert state["available"] is False and state["authenticated"] is False
        assert state["authentication_status"] == "not_authenticated"
        assert len(calls) == 1

    def test_auth_probe_is_single_flight_for_concurrent_status_requests(self):
        provider = CopilotProvider()
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def probe(value):
            calls.append(value)
            entered.set()
            release.wait(1)
            return True

        self.setattr(provider, "_run_auth_probe", probe)
        results = []
        threads = [threading.Thread(
            target=lambda: results.append(provider._cached_auth_probe({}))
        ) for _ in range(4)]
        for thread in threads:
            thread.start()
        assert entered.wait(1)
        release.set()
        for thread in threads:
            thread.join(1)
        assert results == [True] * 4
        assert len(calls) == 1

    def test_default_sdk_operation_timeout_allows_cli_startup(self):
        assert CopilotProvider._operation_timeout({"timeout_seconds": 120}) == 30

    def test_isolated_review_worker_clones_config_and_revalidates_revision_runtime(self):
        provider = CopilotProvider(self.tmp_path / "runtime.json")
        original = {"timeout_seconds": 120, "nested": {"model": "locked"}}
        provider._verified_revision_config = original

        worker = provider.create_isolated_review_worker()
        assert worker is not provider
        assert worker.config_path == provider.config_path
        assert worker._worker_lock is not provider._worker_lock
        worker._verified_revision_config["nested"]["model"] = "worker-only"
        assert original["nested"]["model"] == "locked"

        verified = []
        cancelled = threading.Event()

        def verify(value, **bounds):
            verified.append(value.copy())
            assert bounds["deadline"] > time.monotonic()
            assert bounds["cancel_event"] is cancelled
            cancelled.set()

        self.setattr(worker, "_verify_locked_runtime", verify)
        assert list(worker._stream(
            [{"role": "user", "content": "review"}], [], cancelled,
            timeout_override=1,
        )) == []
        assert len(verified) == 1
        assert verified[0]["nested"]["model"] == "worker-only"

    def test_artifact_hash_checks_cancellation_after_each_chunk(self):
        cancelled = threading.Event()

        class CancellingReader:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def read(self, size):
                assert size == 1024 * 1024
                cancelled.set()
                return b"artifact data"

        fake_path = SimpleNamespace(open=lambda mode: CancellingReader())
        with self.assertRaisesRegex(RuntimeError, "preflight was cancelled"):
            CopilotProvider._hash_file(fake_path, cancel_event=cancelled)

    def test_cli_version_preflight_honors_deadline_and_cleans_up_child(self):
        cli = self.tmp_path / "slow-copilot"
        cli.write_text(
            "#!/usr/bin/python3\nimport time\ntime.sleep(5)\n"
            "print('GitHub Copilot CLI 1.0.79')\n", encoding="utf-8",
        )
        cli.chmod(0o700)

        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "preflight exceeded"):
            CopilotProvider._verify_cli_version(
                cli, "1.0.79", deadline=started + .05,
                cancel_event=threading.Event(),
            )
        assert time.monotonic() - started < .5

    def test_cli_version_preflight_honors_external_cancellation(self):
        cli = self.tmp_path / "slow-copilot"
        cli.write_text(
            "#!/usr/bin/python3\nimport time\ntime.sleep(5)\n"
            "print('GitHub Copilot CLI 1.0.79')\n", encoding="utf-8",
        )
        cli.chmod(0o700)
        cancelled = threading.Event()
        timer = threading.Timer(.05, cancelled.set)
        timer.start()
        self.addCleanup(timer.cancel)

        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "preflight was cancelled"):
            CopilotProvider._verify_cli_version(
                cli, "1.0.79", deadline=started + 1,
                cancel_event=cancelled,
            )
        assert time.monotonic() - started < .5

    def test_expired_revision_preflight_prevents_sdk_worker_spawn(self):
        provider = CopilotProvider()
        provider._verified_revision_config = {"timeout_seconds": 1}
        spawned = []

        def slow_verify(_config, **_bounds):
            time.sleep(.03)

        self.setattr(provider, "_verify_locked_runtime", slow_verify)
        self.setattr(provider, "_thread_main", lambda *args: spawned.append(args))
        cancelled = threading.Event()
        with self.assertRaisesRegex(TimeoutError, "preflight exceeded"):
            list(provider._stream(
                [{"role": "user", "content": "review"}], [], cancelled,
                timeout_override=.01,
            ))
        assert cancelled.is_set()
        assert spawned == []

    def test_cancelled_revision_preflight_prevents_sdk_worker_spawn(self):
        provider = CopilotProvider()
        provider._verified_revision_config = {"timeout_seconds": 1}
        spawned = []
        cancelled = threading.Event()

        def cancel_verify(_config, **bounds):
            assert bounds["cancel_event"] is cancelled
            cancelled.set()

        self.setattr(provider, "_verify_locked_runtime", cancel_verify)
        self.setattr(provider, "_thread_main", lambda *args: spawned.append(args))
        assert list(provider._stream(
            [{"role": "user", "content": "review"}], [], cancelled,
            timeout_override=1,
        )) == []
        assert spawned == []

    def test_revision_preflight_and_stream_share_one_deadline(self):
        provider = CopilotProvider()
        provider._verified_revision_config = {"timeout_seconds": 1}

        def slow_verify(_config, **_bounds):
            time.sleep(.12)

        def wait_for_cancel(_config, _messages, _tools, cancelled, _out):
            cancelled.wait(1)

        self.setattr(provider, "_verify_locked_runtime", slow_verify)
        self.setattr(provider, "_thread_main", wait_for_cancel)
        cancelled = threading.Event()
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "session exceeded"):
            list(provider._stream(
                [{"role": "user", "content": "review"}], [], cancelled,
                timeout_override=.2,
            ))
        elapsed = time.monotonic() - started
        assert .17 <= elapsed < .28
        assert cancelled.is_set()

    def test_isolated_review_worker_requires_verified_research_revision(self):
        with self.assertRaisesRegex(RuntimeError, "previously verified research stream"):
            CopilotProvider().create_isolated_review_worker()

    def test_start_hang_is_timed_out_and_task_is_cancelled(self):
        HangingStartClient.cancelled = False
        provider = CopilotProvider()
        self.setattr(provider, "_import_sdk", lambda: fake_sdk(HangingStartClient))
        values = config(self.tmp_path, sdk_operation_timeout_seconds=.02)
        started = __import__("time").monotonic()
        with self.assertRaisesRegex(TimeoutError, "client start exceeded operation timeout"):
            asyncio.run(provider._run_sdk(
                values, [{"role": "user", "content": "hi"}], [], threading.Event(),
                queue.Queue()
            ))
        assert __import__("time").monotonic() - started < .5
        assert HangingStartClient.cancelled


    def test_cleanup_hang_is_timed_out_and_task_is_cancelled(self):
        HangingStopClient.cancelled = False
        provider = CopilotProvider()
        self.setattr(provider, "_import_sdk", lambda: fake_sdk(HangingStopClient))
        values = config(self.tmp_path, sdk_operation_timeout_seconds=.02,
                        sdk_cleanup_timeout_seconds=.02)
        with self.assertRaisesRegex(TimeoutError, "SDK cleanup exceeded operation timeout"):
            asyncio.run(provider._run_sdk(
                values, [{"role": "user", "content": "hi"}], [], threading.Event(),
                queue.Queue()
            ))
        assert HangingStopClient.cancelled


    def test_stream_external_cancel_has_bounded_wait(self):
        path = self.tmp_path / "config.json"
        path.write_text(json.dumps({"timeout_seconds": 1,
                                    "sdk_operation_timeout_seconds": .02}), encoding="utf-8")
        provider = CopilotProvider(path)
        self.setattr(provider, "status", lambda: {"available": True})

        def briefly_stuck(*args):
            args[-2].wait()
            __import__("time").sleep(.1)

        self.setattr(provider, "_thread_main", briefly_stuck)
        cancel = threading.Event()
        threading.Timer(.01, cancel.set).start()
        started = __import__("time").monotonic()
        assert list(provider.stream([{"role": "user", "content": "hi"}], [], cancel)) == []
        assert __import__("time").monotonic() - started < .09
        with self.assertRaisesRegex(RuntimeError, "previous Copilot SDK worker"):
            list(provider.stream(
                [{"role": "user", "content": "again"}], [], threading.Event()
            ))


    def test_stream_timeout_sets_cancellation(self):
        tmp_path = self.tmp_path
        monkeypatch = self
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"timeout_seconds": .02}), encoding="utf-8")
        provider = CopilotProvider(path)
        monkeypatch.setattr(provider, "status", lambda: {"available": True})
        def slow(*args):
            while not args[-2].is_set():
                args[-2].wait(.001)
        monkeypatch.setattr(provider, "_thread_main", slow)
        cancel = threading.Event()
        with self.assertRaises(TimeoutError):
            list(provider.stream([{"role": "user", "content": "hi"}], [], cancel))
        assert cancel.is_set()


    def test_locked_artifact_hash_mismatch_is_rejected(self):
        tmp_path = self.tmp_path
        monkeypatch = self
        wheel_dir = tmp_path / "wheels"; wheel_dir.mkdir()
        wheel = wheel_dir / "demo-1-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("demo-1.dist-info/METADATA", "Name: demo\nVersion: 1\n")
        lock = tmp_path / "lock"
        lock.write_text("demo==1 --hash=sha256:" + "0" * 64 + "\n", encoding="utf-8")
        cli = tmp_path / "cli"; cli.write_bytes(b"cli"); cli.chmod(0o700)
        provider = CopilotProvider()
        monkeypatch.setattr("workbench.copilot_provider.metadata.version", lambda name: "1")
        with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
            provider._verify_locked_runtime({"lock_path": str(lock), "wheel_directory": str(wheel_dir),
                "cli_path": str(cli), "cli_sha256": hashlib.sha256(b"cli").hexdigest()})

    def test_runtime_policy_rejects_blocked_cli(self):
        tmp_path = self.tmp_path
        policy = tmp_path / "policy.json"
        policy.write_text(json.dumps({"runtime_candidates": [{
            "name": "github-copilot-cli",
            "version": "1.2.3",
            "decision": "blocked",
            "extracted_binary_sha256": "a" * 64,
        }]}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not approved"):
            CopilotProvider()._verify_runtime_policy({
                "dependency_policy_path": str(policy),
                "cli_version": "1.2.3",
                "cli_sha256": "a" * 64,
            })

    def test_audit_policy_requires_metric_row_period_and_support_checks(self):
        policy = CopilotProvider().audit_policy()
        self.assertEqual(policy["version"], "trust-stack-research-policy/3.3")
        self.assertFalse(CopilotProvider.supports_official_acquisition)
        self.assertIn("request_confirmation", policy["instructions"])
        self.assertIn("complete integrated report", policy["instructions"])
        instructions = policy["instructions"]
        self.assertIn("do not establish metric identity", instructions)
        self.assertIn("Repetition warnings are advisory", instructions)
        self.assertNotIn("'a court decision was reversed'", instructions)
        self.assertNotIn("3 or fewer", instructions)
        self.assertIn("source row/header", instructions)
        self.assertIn("validated citation ID proves only provenance linkage", instructions)
