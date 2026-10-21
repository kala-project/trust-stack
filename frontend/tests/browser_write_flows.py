#!/usr/bin/env python3
"""Controlled Chromium write tests for the real React research components.

All HTTP writes target the ephemeral server in this process; no production API or
persistent database is reachable from this harness.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
sys.path.insert(0, str(ROOT / "scripts"))
from browser_smoke import BrowserSmoke  # noqa: E402

NOW = "2026-10-02T06:00:00Z"


def decision(status="pending"):
    return {
        "decision_id": "decision-1", "run_id": "run-decision", "kind": "coverage",
        "question": "选择研究方案", "explanation": "受控测试决定。", "multiple": False,
        "options": [
            {"id": "option-a", "label": "方案 A", "benefit": "覆盖收入", "limitation": "不含预测"},
            {"id": "option-b", "label": "方案 B", "benefit": "覆盖预测", "limitation": "假设较多"},
        ],
        "groups": [{"id": "scope", "label": "范围", "option_ids": ["option-a", "option-b"], "minimum": 1, "maximum": 1}],
        "created_at": NOW, "status": status, "selected_ids": ["option-a"] if status == "accepted" else [],
        "notes": "immutable note" if status == "accepted" else None, "continuation_run_id": None,
        "base_message_id": None, "request_message": "选择研究方案",
    }


def task(task_id, *, run_id=None, status="ready"):
    return {
        "task_id": task_id, "question": f"controlled {task_id}", "origin": "user", "created_at": NOW,
        "current_scope_version": 3, "status": status, "code": "ok", "message": "controlled fixture",
        "scope": {"subjects": [], "period_text": "FY2025", "metrics": ["revenue"], "basis": ["filings"],
                  "unit": "USD", "assumptions": [], "uncertainties": [], "gaps": []},
        "runs": ([{"run_id": run_id, "conversation_id": "conv-1", "created_at": NOW, "status": "running"}] if run_id else []),
        "latest_run": None, "latest_report": None,
    }


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.posts = []
        self.create_loss_seen = False
        self.decision_loss_seen = False
        self.decision_status = "pending"

    def add(self, path, body):
        with self.lock:
            self.posts.append({"path": path, "body": body})


class Handler(SimpleHTTPRequestHandler):
    state: State
    directory_path: Path

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(self.directory_path), **kwargs)

    def log_message(self, fmt, *args):
        pass

    def json_response(self, value, status=200):
        data = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlsplit(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/api/research/task":
            task_id = query.get("id", [""])[0]
            if task_id == "task-workspace":
                return self.json_response(task(task_id, run_id="run-decision", status="running"))
            if task_id == "task-cancel":
                value = task(task_id, run_id="run-active", status="running")
                value["runs"].insert(0, {"run_id": "run-old", "conversation_id": "conv-1", "created_at": NOW, "status": "completed"})
                return self.json_response(value)
            return self.json_response(task(task_id, status="needs_clarification"))
        if parsed.path == "/api/chat/conversation":
            active = "run-active" if getattr(self.server, "cancel_view", False) else "run-decision"
            return self.json_response({"conversation_id": "conv-1", "company_id": "co-1", "title": "Controlled research",
                "created_at": NOW, "execution_scope": {
                    "version": "research-execution-scope/1", "company_ids": ["co-1"],
                    "scope_id": "scope-828e6795e90d4ca4d5ce6b71565cfa47b1a58d0e158d11dd619c6f8e4573114e",
                    "payload_sha256": "828e6795e90d4ca4d5ce6b71565cfa47b1a58d0e158d11dd619c6f8e4573114e",
                }, "research_scope": {}, "report_versions": [],
                "messages": [{"message_id": "m1", "role": "assistant", "content": "working", "created_at": NOW, "run_id": active}],
                "decision": decision(self.state.decision_status) if active == "run-decision" else None})
        if parsed.path == "/api/verifications":
            return self.json_response({"conversation_id": "conv-1", "candidates": []})
        if parsed.path == "/api/chat/run":
            run_id = query.get("id", [""])[0]
            if run_id == "run-decision":
                return self.json_response({"run_id": run_id, "conversation_id": "conv-1", "status": "blocked", "created_at": NOW,
                    "events": [{"seq": 1, "type": "decision_required", "message": "choose"}],
                    "termination_reason": "confirmation_required", "decision": decision(self.state.decision_status)})
            return self.json_response({"run_id": run_id, "conversation_id": "conv-1",
                "status": "running" if run_id == "run-active" else "completed", "created_at": NOW, "events": []})
        return super().do_GET()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.state.add(self.path, body)
        if self.path == "/api/research/tasks":
            question = body["question"]
            if "leave" in question:
                time.sleep(0.8)
                return self.json_response(task("task-leave", status="ready"))
            if "double" in question:
                time.sleep(0.35)
                return self.json_response(task("task-double", status="needs_clarification"))
            if "loss" in question and not self.state.create_loss_seen:
                self.state.create_loss_seen = True
                return self.json_response({"error": "controlled response unavailable after dispatch"}, 503)
            return self.json_response(task("task-loss", status="needs_clarification"))
        if self.path == "/api/research/decisions/answer":
            if not self.state.decision_loss_seen:
                self.state.decision_loss_seen = True
                return self.json_response({"error": "controlled response unavailable after dispatch"}, 503)
            self.state.decision_status = "accepted"
            return self.json_response({"decision": decision("accepted"), "continuation_run": None})
        if self.path == "/api/chat/cancel":
            return self.json_response({"run_id": body["run_id"], "status": "cancel_requested"})
        if self.path == "/api/research/tasks/start":
            return self.json_response({"run_id": "unexpected-start", "conversation_id": "conv-1", "status": "queued"})
        return self.json_response({"error": "unknown controlled route"}, 404)


def build_fixture(out_dir: Path):
    script = """import { build } from 'vite';
import { resolve } from 'node:path';
await build({configFile:false, root:process.cwd(), build:{outDir:process.argv[1], emptyOutDir:true,
  rollupOptions:{input:resolve('tests/fixtures/write-flows.tsx'), output:{entryFileNames:'app.js', assetFileNames:'[name][extname]'}}}});"""
    subprocess.run(["node", "--input-type=module", "-e", script, str(out_dir)], cwd=FRONTEND, check=True, timeout=90)
    css = next(out_dir.glob("*.css"), None)
    style = f'<link rel="stylesheet" href="/{css.name}">' if css else ""
    (out_dir / "index.html").write_text(f'<!doctype html><html><head><meta charset="utf-8">{style}</head><body><div id="root"></div><script type="module" src="/app.js"></script></body></html>')


def button(text):
    return f"[...document.querySelectorAll('button')].find(e => e.textContent.includes({json.dumps(text)}))"


def set_value(smoke, selector, value):
    smoke.evaluate(f"""(() => {{ const e=document.querySelector({json.dumps(selector)}); const s=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set; s.call(e,{json.dumps(value)}); e.dispatchEvent(new Event('input',{{bubbles:true}})); }})()""")


def navigate(smoke, url):
    smoke.cdp.call("Page.navigate", {"url": url})
    smoke.wait("document.readyState === 'complete' && !!document.querySelector('#root')", f"navigate {url}")


def run(smoke: BrowserSmoke, state: State, base: str):
    smoke.wait("!!document.querySelector('#research-question')", "new research mounted")
    smoke.evaluate("sessionStorage.clear()")
    before = len(state.posts)
    set_value(smoke, "#research-question", "controlled double")
    smoke.click(button("Start Research")); smoke.click(button("Processing"))
    smoke.wait("document.body.innerText.includes('Confirm research subject')", "double create response")
    creates = [p for p in state.posts[before:] if p["path"] == "/api/research/tasks"]
    smoke.check(len(creates) == 1, "create double pointer click dispatches once")

    navigate(smoke, base)
    smoke.evaluate("sessionStorage.clear()")
    before = len(state.posts)
    set_value(smoke, "#research-question", "controlled loss")
    smoke.click(button("Start Research"))
    smoke.wait("document.body.innerText.includes('Recover same submission')", "create response loss shown")
    first = [p for p in state.posts[before:] if p["path"] == "/api/research/tasks"][0]["body"]
    smoke.cdp.call("Page.reload")
    smoke.wait("document.body.innerText.includes('Recover same submission')", "create recovery survives reload")
    smoke.check(True, "create response loss survives reload and requires explicit recovery")
    smoke.click(button("Recover same submission"))
    smoke.wait("document.body.innerText.includes('Confirm research subject')", "create explicit retry acknowledged")
    pair = [p["body"] for p in state.posts[before:] if p["path"] == "/api/research/tasks"]
    smoke.check(len(pair) == 2 and pair[0]["request_id"] == pair[1]["request_id"], "create response loss retry reuses request_id")
    smoke.check(pair[0]["question"] == pair[1]["question"], "create recovery keeps immutable question")

    navigate(smoke, base)
    smoke.evaluate("sessionStorage.clear()")
    before = len(state.posts)
    set_value(smoke, "#research-question", "controlled leave")
    smoke.click(button("Start Research")); smoke.click("document.querySelector('[data-testid=leave]')")
    smoke.wait("!!document.querySelector('[data-testid=left]')", "left page during mutation")
    time.sleep(1.1)
    smoke.check(bool(smoke.evaluate("!!document.querySelector('[data-testid=left]')")), "late create response does not navigate user back")
    smoke.check(not any(p["path"] == "/api/research/tasks/start" for p in state.posts[before:]), "late create response does not start a run")

    navigate(smoke, base + "?view=workspace&task=task-workspace")
    smoke.evaluate("sessionStorage.clear()")
    smoke.cdp.call("Page.reload")
    smoke.wait("document.body.innerText.includes('Confirm and continue research')", "pending decision loaded")
    mount_posts = len(state.posts)
    time.sleep(0.4)
    smoke.check(len(state.posts) == mount_posts, "workspace mount and reload issue no POST")
    smoke.click("document.querySelector('input[value=option-a]')" if smoke.evaluate("!!document.querySelector('input[value=option-a]')") else "document.querySelector('input')")
    set_value(smoke, "textarea[id^=decision-notes]", "immutable note")
    before = len(state.posts)
    smoke.click(button("Confirm and continue research"))
    smoke.wait("document.body.innerText.includes('Check and retry same submission')", "decision response loss shown")
    smoke.cdp.call("Page.reload")
    smoke.wait("document.body.innerText.includes('Check and retry same submission')", "decision recovery survives GET and reload")
    smoke.check(True, "pending decision is GET-verified after reload and requires explicit recovery")
    smoke.click(button("Check and retry same submission"))
    smoke.wait("!document.body.innerText.includes('Confirm and continue research')", "decision retry settled")
    answers = [p["body"] for p in state.posts[before:] if p["path"] == "/api/research/decisions/answer"]
    smoke.check(len(answers) == 2 and answers[0]["request_id"] == answers[1]["request_id"], "decision retry reuses request_id")
    smoke.check(answers[0]["selected_ids"] == answers[1]["selected_ids"] == ["option-a"] and answers[0]["notes"] == answers[1]["notes"] == "immutable note", "decision retry preserves accepted payload")

    state.decision_status = "pending"
    smoke.evaluate("sessionStorage.clear()")
    smoke.evaluate("location.href='/?view=workspace&task=task-cancel'")
    setattr(smoke, "_cancel_marker", True)
    smoke.wait("document.body.innerText.includes('Stop this run')", "active run loaded for cancellation")
    # Tell the controlled server which conversation fixture the task uses.
    smoke.click(button("Stop this run"))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(p["path"] == "/api/chat/cancel" for p in state.posts):
        time.sleep(0.05)
    cancels = [p["body"] for p in state.posts if p["path"] == "/api/chat/cancel"]
    smoke.check(cancels and cancels[-1]["run_id"] == "run-active", "explicit cancel sends the displayed current run id")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="var/browser-tests/write-flows")
    parser.add_argument("--chromium", default="/snap/bin/chromium")
    args = parser.parse_args()
    output = (ROOT / args.output).resolve(); output.mkdir(parents=True, exist_ok=True)
    out_dir = output / "isolated-app"
    result = {"status": "failed", "controlled": True, "started_at": datetime.now(timezone.utc).isoformat(), "checks": []}
    state = State(); server = None; smoke = None; profile = None
    try:
        build_fixture(out_dir)
        Handler.state = state; Handler.directory_path = out_dir
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # Conversation selection is inferred from requested task through this flag, set per request below.
        original = Handler.do_GET
        def routed_get(self):
            self.server.cancel_view = "task=task-cancel" in self.headers.get("Referer", "")
            return original(self)
        Handler.do_GET = routed_get
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}/"
        profile = tempfile.mkdtemp(prefix="write-chrome-", dir=output)
        smoke = BrowserSmoke(base, args.chromium, output, 45)
        smoke.start(profile); run(smoke, state, base)
        result.update(status="passed", checks=smoke.checks)
        return 0
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["checks"] = smoke.checks if smoke else []
        if smoke and smoke.cdp:
            try:
                smoke.screenshot("failure.png")
                result["browser_failure"] = smoke.evaluate("({href:location.href, title:document.title, body:document.body?.innerText?.slice(0,1000)})")
            except Exception: pass
        print("FAIL", result["error"])
        return 1
    finally:
        if smoke: smoke.stop()
        if server: server.shutdown(); server.server_close()
        if profile: shutil.rmtree(profile, ignore_errors=True)
        result["posts"] = state.posts
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        (output / "write-flow-results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
