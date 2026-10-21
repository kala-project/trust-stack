"""Protocol-test chat provider for browser E2E only; this is never a live Agent/model."""
from __future__ import annotations

import json
import threading
import time


class ExplicitMSFTIntentAdapter:
    """Explicit structured-intent substitute for tests whose subject is fixed to MSFT."""

    def resolve_research_intent(self, prompt, timeout_seconds):
        payload = json.loads(prompt.split("\n", 1)[1])
        company_id = payload.get("current_company_id") or "MSFT"
        value = {
            "kind": "company_research",
            "subjects": [{"kind": "company", "name": company_id, "candidate_id": company_id}],
            "directions": ["execute test research"], "metrics": [], "period_text": None,
            "basis": [], "unit": None, "assumptions": [], "uncertainties": [],
            "excluded_entities": [], "is_continuation": bool(payload.get("current_company_id")),
            "identity_ambiguity": False, "summary": payload["question"],
        }
        return {"intent": value, "provenance": {
            "prompt": prompt, "prompt_version": "research-intent/1.0",
            "provider": "explicit-test-adapter", "model": "test-intent-v1", "result": value,
        }}


class BrowserProtocolTestProvider(ExplicitMSFTIntentAdapter):
    """Mutable scripted substitute injected directly into EvidenceHTTPServer by the test."""

    provider_name = "protocol-test 测试替身/非模型"

    def __init__(self) -> None:
        self.available = False
        self.histories: list[list[dict]] = []
        self.tool_results: list[dict] = []
        self.started = threading.Event()

    def status(self) -> dict:
        return {
            "available": self.available,
            "provider": self.provider_name,
            "reason": (
                "protocol-test 测试替身已启用；明确非 live Agent、非真实模型"
                if self.available
                else "测试状态：live Agent 不可用；可先写中文草稿，当前不可发送"
            ),
            "test_only": True,
        }

    def stream(self, messages, tools, cancel_event):
        self.histories.append([dict(message) for message in messages])
        question = messages[-1]["content"]
        self.started.set()

        if "取消" in question:
            yield {
                "type": "tool_call", "call_id": "protocol-test-cancel-audit",
                "name": "protocol_test_cancel_probe_non_model", "arguments": {"test_only": True},
            }
            cancel_event.wait(8)
            return
        if "失败" in question:
            yield {
                "type": "tool_call", "call_id": "protocol-test-failure-audit",
                "name": "protocol_test_failure_probe_non_model", "arguments": {"test_only": True},
            }
            raise RuntimeError("intentional protocol-test provider failure")
        if "竞态" in question:
            yield {"type": "delta", "text": "protocol-test：竞态测试替身/非模型回答。"}
            return

        tool = next(item for item in tools if item["name"] == "search_evidence")
        result = tool["handler"](query="revenue", limit=3)
        self.tool_results.append(result)
        evidence = result.get("results", [])
        if not evidence:
            raise RuntimeError("frozen evidence returned no revenue result")
        evidence_id = evidence[0]["evidence_id"]

        yield {
            "type": "tool_call",
            "call_id": f"protocol-test-{len(self.histories)}",
            "name": "search_evidence",
            "arguments": {"query": "revenue", "limit": 3},
        }
        time.sleep(2.0)
        yield {"type": "delta", "text": "protocol-test：测试替身/非模型回答；依据来自真实只读工具。"}
        time.sleep(0.7)
        yield {
            "type": "delta",
            "text": (
                f" [evidence:{evidence_id}] "
                "<img id=\"model-html-must-not-run\" src=x onerror=\"window.__modelExecuted=true\">"
            ),
        }
