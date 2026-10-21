#!/usr/bin/env python3
"""Real Chromium smoke test for the read-only evidence workbench (stdlib only)."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import queue
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from datetime import datetime, timezone
from typing import Any


class SmokeFailure(AssertionError):
    pass


class CDP:
    """Small RFC 6455 client sufficient for a local Chromium CDP target."""

    def __init__(self, url: str):
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {parsed.netloc}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        response = self._read_until(b"\r\n\r\n")
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise RuntimeError(f"WebSocket upgrade failed: {response[:200]!r}")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        )
        headers = response.decode("latin1").lower()
        if expected.decode().lower() not in headers:
            raise RuntimeError("WebSocket accept key mismatch")
        # The connection timeout is for the handshake, not CDP's idle lifetime.
        # Model execution may produce no browser events for many seconds. Each
        # call below has its own bounded wait; close() shuts down the reader.
        self.sock.settimeout(None)
        self.next_id = 0
        self.pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self.events: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.closed = False
        self.reader = threading.Thread(target=self._reader, daemon=True)
        self.reader.start()

    def _read_until(self, marker: bytes) -> bytes:
        data = bytearray()
        while marker not in data:
            part = self.sock.recv(4096)
            if not part:
                raise EOFError("socket closed")
            data.extend(part)
        return bytes(data)

    def _exact(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            part = self.sock.recv(size - len(chunks))
            if not part:
                raise EOFError("socket closed")
            chunks.extend(part)
        return bytes(chunks)

    def _frame(self) -> tuple[int, bytes]:
        first, second = self._exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._exact(8))[0]
        mask = self._exact(4) if second & 0x80 else None
        payload = self._exact(length)
        if mask:
            payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        return opcode, payload

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        mask = os.urandom(4)
        length = len(payload)
        head = bytearray([0x80 | opcode])
        if length < 126:
            head.append(0x80 | length)
        elif length < 65536:
            head.extend([0x80 | 126])
            head.extend(struct.pack("!H", length))
        else:
            head.extend([0x80 | 127])
            head.extend(struct.pack("!Q", length))
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        with self.lock:
            self.sock.sendall(bytes(head) + mask + masked)

    def _reader(self) -> None:
        try:
            while not self.closed:
                opcode, payload = self._frame()
                if opcode == 1:
                    message = json.loads(payload.decode("utf-8"))
                    if "id" in message:
                        waiter = self.pending.get(message["id"])
                        if waiter:
                            waiter.put(message)
                    else:
                        self.events.append(message)
                elif opcode == 9:
                    self._send_frame(10, payload)
                elif opcode == 8:
                    break
        except (EOFError, OSError, ValueError):
            if not self.closed:
                self.events.append({"method": "Client.readerError"})

    def call(self, method: str, params: dict[str, Any] | None = None, timeout: float = 10) -> dict[str, Any]:
        self.next_id += 1
        call_id = self.next_id
        waiter: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        self.pending[call_id] = waiter
        self._send_frame(1, json.dumps({"id": call_id, "method": method, "params": params or {}}).encode())
        try:
            response = waiter.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"CDP timeout: {method}") from exc
        finally:
            self.pending.pop(call_id, None)
        if "error" in response:
            raise RuntimeError(f"CDP {method}: {response['error']}")
        return response.get("result", {})

    def close(self) -> None:
        self.closed = True
        try:
            self._send_frame(8)
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class BrowserSmoke:
    def __init__(self, base_url: str, chromium: str, output: Path, timeout: float):
        parsed = urllib.parse.urlsplit(base_url)
        self.base_url = urllib.parse.urlunsplit(parsed._replace(path=parsed.path or "/"))
        self.chromium = chromium
        self.output = output
        self.timeout = timeout
        self.browser: subprocess.Popen[bytes] | None = None
        self.cdp: CDP | None = None
        self.checks: list[str] = []

    def check(self, condition: Any, message: str) -> None:
        if not condition:
            raise SmokeFailure(message)
        self.checks.append(message)
        print(f"PASS  {message}")

    def start(self, profile: str) -> None:
        flags = [
            self.chromium,
            f"--user-data-dir={profile}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=0",
            "--headless=new",
            "--window-size=1440,1000",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-default-apps",
            "--disable-domain-reliability",
            "--disable-extensions",
            "--disable-features=OptimizationHints,MediaRouter,Translate",
            "--disable-sync",
            "--metrics-recording-only",
            "--no-first-run",
            "--no-default-browser-check",
            self.base_url,
        ]
        # Deliberately do not use --no-sandbox.
        self.browser = subprocess.Popen(flags, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        port_file = Path(profile) / "DevToolsActivePort"
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.browser.poll() is not None:
                raise RuntimeError(f"Chromium exited early ({self.browser.returncode}); sandbox remains enabled")
            if port_file.exists():
                lines = port_file.read_text().splitlines()
                if lines:
                    port = int(lines[0])
                    break
            time.sleep(0.05)
        else:
            raise TimeoutError("Chromium did not expose DevToolsActivePort")
        targets_url = f"http://127.0.0.1:{port}/json/list"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(targets_url, timeout=1) as response:
                    targets = json.load(response)
                pages = [target for target in targets if target.get("type") == "page"]
                matching = [target for target in pages if target.get("url", "").startswith(self.base_url)]
                if matching:
                    self.cdp = CDP(matching[0]["webSocketDebuggerUrl"])
                    break
            except (OSError, ValueError):
                time.sleep(0.05)
        if not self.cdp:
            raise TimeoutError("No Chromium page target")
        for domain in ("Page.enable", "Runtime.enable", "Log.enable"):
            self.cdp.call(domain)
        # /json/list exposes Chromium's requested URL before the renderer has
        # committed it.  On a busy sandbox that speculative state can last for
        # tens of seconds, while Runtime.evaluate still runs in about:blank.
        # Synchronize on the page's actual main-world location rather than the
        # target metadata; issuing a second Page.navigate can race the pending
        # startup navigation and hang as well.
        self.wait(
            f"location.href.split('#')[0] === {json.dumps(self.base_url.split('#')[0])}"
            + (f" && location.hash === {json.dumps('#' + self.base_url.split('#', 1)[1])}" if '#' in self.base_url else ''),
            f"Chromium 提交初始页面 {self.base_url}",
        )

    def stop(self) -> None:
        if self.cdp:
            self.cdp.close()
        # Only terminate the exact browser process created by this instance.
        if self.browser and self.browser.poll() is None:
            self.browser.terminate()
            try:
                self.browser.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.browser.kill()
                self.browser.wait(timeout=5)

    def evaluate(self, expression: str) -> Any:
        assert self.cdp
        result = self.cdp.call("Runtime.evaluate", {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        })
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            description = details.get("exception", {}).get("description", details.get("text"))
            raise SmokeFailure(f"evaluation failed: {description}")
        return result.get("result", {}).get("value")

    def wait(self, expression: str, description: str) -> Any:
        deadline = time.monotonic() + self.timeout
        last = None
        while time.monotonic() < deadline:
            last = self.evaluate(expression)
            if last:
                return last
            time.sleep(0.1)
        raise SmokeFailure(f"等待超时：{description}（last={last!r}）")

    def point(self, expression: str) -> dict[str, float]:
        value = self.evaluate(f"""(async () => {{
          const el = ({expression});
          if (!el) return null;
          el.scrollIntoView({{block:'center', inline:'center', behavior:'instant'}});
          await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          const r = el.getBoundingClientRect();
          if (!r.width || !r.height) return null;
          return {{x:r.left+r.width/2, y:r.top+r.height/2}};
        }})()""")
        if not value:
            raise SmokeFailure(f"找不到可点击元素：{expression}")
        return value

    def click(self, expression: str) -> None:
        assert self.cdp
        point = self.point(expression)
        common = {"x": point["x"], "y": point["y"], "button": "left", "clickCount": 1}
        self.cdp.call("Input.dispatchMouseEvent", {"type": "mousePressed", **common})
        self.cdp.call("Input.dispatchMouseEvent", {"type": "mouseReleased", **common})

    def escape(self) -> None:
        assert self.cdp
        self.cdp.call("Input.dispatchKeyEvent", {"type": "keyDown", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})
        self.cdp.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27})

    def open_legacy_navigation(self) -> None:
        """Expose the collapsed advanced views and legacy company switcher."""
        self.evaluate("""(() => {
          const owners = [
            document.querySelector('.advanced-nav'),
            document.querySelector('#company-switcher')?.closest('details')
          ];
          for (const details of owners) {
            if (details && !details.open) details.querySelector('summary').click();
          }
        })()""")

    def open_legacy_view(self, view: str) -> None:
        """Enter one of the explicitly exposed advanced views."""
        self.open_legacy_navigation()
        self.evaluate(f"""(() => {{
          const button = document.querySelector(`[data-view=${{{json.dumps(view)}}}]`);
          if (!button) throw new Error(`missing legacy view: ${{{json.dumps(view)}}}`);
          button.click();
        }})()""")

    def screenshot(self, name: str) -> None:
        assert self.cdp
        data = self.cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})["data"]
        path = self.output / name
        path.write_bytes(base64.b64decode(data))
        print(f"SHOT  {path}")

    def run(self) -> None:
        if __package__:
            from .browser_readability import run_readability
        else:
            from browser_readability import run_readability
        self.wait("document.readyState === 'complete' && document.querySelector('#loading-state')?.hidden && !document.querySelector('#content')?.hidden && !!document.querySelector('#company-switcher [data-company-id]')", "新主页与旧版公司入口载入")
        self.open_legacy_navigation()
        run_readability(self)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765/")
    parser.add_argument("--chromium", default="/snap/bin/chromium")
    parser.add_argument("--output", default="var/browser")
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--isolated", action="store_true", help="run against one ephemeral local evidence server")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[1]
    temporary = server = server_thread = None
    if args.isolated:
        import sys
        sys.path.insert(0, str(root))
        from workbench.build import build_database
        from workbench.server import EvidenceHTTPServer
        temporary = tempfile.TemporaryDirectory(prefix="browser-smoke-e2e-")
        temp_root = Path(temporary.name)
        evidence_db = temp_root / "evidence.sqlite3"
        build_database(db_path=evidence_db)
        server = EvidenceHTTPServer(0, evidence_db, chat_db=temp_root / "chat.sqlite3", wiki_db=temp_root / "wiki.sqlite3")
        server_thread = threading.Thread(target=server.serve_forever, name="smoke-test-http", daemon=True)
        server_thread.start()
        args.url = f"http://127.0.0.1:{server.server_port}/"
    smoke = BrowserSmoke(args.url, args.chromium, output, args.timeout)
    tracked = [*sorted((root / "workbench").glob("*.py")), *sorted((root / "web").glob("*")),
               root / "scripts/browser_smoke.py", root / "scripts/browser_readability.py", root / "data/source-catalog.json"]
    db = root / "var/evidence.sqlite3"
    if db.exists():
        tracked.append(db)
    hashes = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in tracked if p.is_file()}
    result: dict[str, Any] = {
        "url": args.url, "status": "failed", "checks": [],
        "server_mode": "isolated" if args.isolated else "existing",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "chromium_version": subprocess.run([args.chromium, "--version"], check=True,
                                           capture_output=True, text=True, timeout=15).stdout.strip(),
        "local_artifact_sha256": hashes,
        "binding_note": "Local artifact hashes; remote URLs are not attested by these local hashes.",
    }
    started = time.time()
    profile = tempfile.mkdtemp(prefix="chromium-profile-", dir=output)
    try:
        smoke.start(profile)
        smoke.run()
        result.update(status="passed", checks=smoke.checks)
        return_code = 0
    except Exception as error:  # report exact automated outcome while retaining artifacts
        result["error"] = f"{type(error).__name__}: {error}"
        result["checks"] = smoke.checks
        print(f"FAIL  {result['error']}")
        if smoke.cdp:
            try:
                smoke.screenshot("failure.png")
                (output / "failure.txt").write_text(smoke.evaluate("document.body.innerText + '\\nACTIVE: ' + document.activeElement?.outerHTML"), encoding="utf-8")
            except Exception:
                pass
        return_code = 1
    finally:
        smoke.stop()
        if server:
            server.shutdown()
            server.server_close()
        if server_thread:
            server_thread.join(3)
        if temporary:
            temporary.cleanup()
        shutil.rmtree(profile, ignore_errors=True)
        result["duration_seconds"] = round(time.time() - started, 3)
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        result["screenshots_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in sorted(output.glob("*.png"))}
        (output / "smoke-results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"RESULT {result['status']} ({len(smoke.checks)} checks)")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
