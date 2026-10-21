"""Offline API compatibility check for the pinned Copilot SDK.

This imports and constructs Python API objects only. It must never create a client,
start the CLI, authenticate, contact a model, or inspect host credentials.
"""

from __future__ import annotations

import asyncio
from importlib import metadata
import inspect
import json
import threading

from copilot import CopilotClient
from copilot.client import RuntimeConnection
from copilot.generated.session_events import (
    AssistantMessageData,
    AssistantMessageDeltaData,
    AssistantUsageData,
    SessionErrorData,
    SessionIdleData,
)
from copilot.session import CopilotSession, SystemMessageAppendConfig
from copilot.tools import Tool, ToolResult


SYSTEM_CONFIG = {"mode": "append", "content": "offline contract probe"}


async def _tool_handler(_invocation):
    return ToolResult(text_result_for_llm="{}", result_type="success")


async def _check_default_permission_denial() -> str:
    # Exercise the SDK's real fail-closed branch without constructing a session/client.
    session = object.__new__(CopilotSession)
    session._permission_handler_lock = threading.Lock()  # noqa: SLF001
    session._permission_handler = None  # noqa: SLF001
    decision = await session._handle_permission_request(object())  # noqa: SLF001
    return decision.kind


def main() -> int:
    if metadata.version("github-copilot-sdk") != "1.0.11":
        raise RuntimeError("pinned github-copilot-sdk 1.0.11 is not active")

    inspect.signature(CopilotClient).bind(
        connection=None,
        working_directory="/offline-api-check",
        use_logged_in_user=True,
        telemetry=None,
        env={"PATH": "/offline-api-check"},
        mode="copilot-cli",
        enable_remote_sessions=False,
        log_level="error",
    )
    inspect.signature(CopilotClient).bind(
        connection=None,
        working_directory="/offline-api-check",
        base_directory="/offline-api-check",
        use_logged_in_user=True,
        telemetry=None,
        env={"HOME": "/offline-api-check"},
        mode="empty",
        enable_remote_sessions=False,
        log_level="error",
    )
    inspect.signature(CopilotClient.get_auth_status).bind(object())
    inspect.signature(RuntimeConnection.for_stdio).bind(path="/offline-api-check/copilot")
    inspect.signature(CopilotClient.create_session).bind(
        object(),
        model=None,
        tools=[],
        system_message=SYSTEM_CONFIG,
        available_tools=[],
        excluded_tools=["builtin:*", "mcp:*"],
        on_permission_request=None,
        enable_experimental_mode=False,
        enable_session_telemetry=False,
        enable_file_change_tracking=False,
        mcp_servers={},
        custom_agents=[],
        skip_custom_instructions=True,
        custom_agents_local_only=True,
        coauthor_enabled=False,
        manage_schedule_enabled=False,
        enable_config_discovery=False,
        skip_embedding_retrieval=True,
        enable_on_demand_instruction_discovery=False,
        enable_file_hooks=False,
        enable_host_git_operations=False,
        enable_session_store=False,
        enable_skills=False,
        plugin_directories=[],
        instruction_directories=[],
        memory={"enabled": False},
        request_extensions=False,
        enable_mcp_apps=False,
        streaming=True,
    )

    if not {"mode", "content"}.issubset(SystemMessageAppendConfig.__optional_keys__):
        raise RuntimeError("SystemMessageAppendConfig no longer accepts mode/content")
    typed_system_config = SystemMessageAppendConfig(**SYSTEM_CONFIG)
    if typed_system_config != SYSTEM_CONFIG:
        raise RuntimeError("system message TypedDict construction changed")

    tool = Tool(
        name="offline_probe",
        description="offline API construction probe",
        parameters={"type": "object", "additionalProperties": False},
        handler=_tool_handler,
        skip_permission=True,
        defer="never",
    )
    result = ToolResult(text_result_for_llm="{}", result_type="success")
    events = [
        AssistantMessageData(content="complete", message_id="m1"),
        AssistantMessageDeltaData(delta_content="delta", message_id="m1"),
        AssistantUsageData(model="offline", input_tokens=1, output_tokens=1),
        SessionErrorData(error_type="offline", message="probe"),
        SessionIdleData(aborted=False),
    ]
    if tool.name != "offline_probe" or result.result_type != "success":
        raise RuntimeError("Tool or ToolResult construction changed")
    if [event.__class__.__name__ for event in events] != [
        "AssistantMessageData",
        "AssistantMessageDeltaData",
        "AssistantUsageData",
        "SessionErrorData",
        "SessionIdleData",
    ]:
        raise RuntimeError("session event class construction changed")

    permission_kind = asyncio.run(_check_default_permission_denial())
    if permission_kind != "user-not-available":
        raise RuntimeError(f"missing permission handler is not fail-closed: {permission_kind}")

    print(json.dumps({
        "check": "offline-api-check",
        "live": False,
        "sdk_version": "1.0.11",
        "permission_without_handler": permission_kind,
        "result": "passed",
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
