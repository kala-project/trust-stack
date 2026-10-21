"""Read-only candidate preview; local by default, explicit --host for LAN access.
API proxy is a narrow GET allowlist; all writes fail.

Serves frontend/dist, never alters web/, never constructs a service against live
DBs. Report links deliberately leave the candidate for the existing reader until
its outer-shell migration is separately verified. Not a production server.
"""
from __future__ import annotations
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import threading
from urllib.error import HTTPError
from urllib.parse import parse_qsl, unquote, urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = "http://127.0.0.1:5432"
READS = {
    "/api/auth/status": set(),
    "/api/chat/status": set(),
    "/api/overview": set(),
    "/api/research/tasks": {"include_internal"},
    "/api/research/task": {"id"},
    "/api/chat/conversation": {"id"},
    "/api/chat/run": {"id", "after"},
    "/api/documents/coverage": {"company"},
    "/api/verifications": {"conversation_id", "limit"},
    "/api/verification/candidate": {"conversation_id", "candidate_id"},
    "/api/reports": {"conversation_id"},
    "/api/reports/reader": {"report_id", "conversation_id", "message_id"},
    "/api/reports/evidence": {"report_id", "conversation_id", "message_id", "anchor_id"},
    "/api/reports/source": {"report_id", "conversation_id", "message_id", "source_id"},
    "/api/reports/raw-source": {"report_id", "conversation_id", "message_id", "source_id"},
}

def validate_http_origin(value, label="origin"):
    """Return a canonical HTTP(S) origin, rejecting URL ambiguity."""
    if not isinstance(value, str) or not value or value.strip() != value or "\\" in value:
        raise ValueError(f"{label} must be an HTTP(S) origin without credentials or path")
    parsed = urlsplit(value)
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{label} has an invalid port") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label} must be an HTTP(S) origin without credentials or path")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{parsed.scheme}://{host}{':' + str(port) if port is not None else ''}"


def _strict_query(raw_query):
    if not raw_query:
        return {}
    # parse_qsl accepts malformed percent escapes; reject those before decoding.
    for index, char in enumerate(raw_query):
        if char == "%" and (
            index + 2 >= len(raw_query)
            or any(item not in "0123456789abcdefABCDEF" for item in raw_query[index + 1:index + 3])
        ):
            raise ValueError("malformed percent escape")
    pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=True, errors="strict")
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate query parameter: {key}")
        result[key] = value
    return result


def make_server(port=0, host="127.0.0.1", reader_origin=UPSTREAM, unified_reader=False, static_root=None, api_origin=UPSTREAM):
    reader_origin = validate_http_origin(reader_origin, "reader origin")
    api_origin = validate_http_origin(api_origin, "API origin")
    root = (Path(static_root) if static_root else ROOT / "frontend/dist").resolve()
    records = []
    lock = threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def send_bytes(self, status, raw, media):
            self.send_response(status)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            try: self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError): pass
        def do_GET(self):
            parsed = urlsplit(self.path)
            if not unified_reader and parsed.path in {"/report-reader.html", "/legacy.html"}:
                # Explicit boundary: no second report renderer, no transparent
                # proxy to endpoints which might materialize a reader snapshot.
                target = self.path if parsed.path == "/report-reader.html" else "/"
                self.send_response(302); self.send_header("Location", reader_origin + target)
                self.send_header("Cache-Control", "no-store"); self.end_headers(); return
            if parsed.path.startswith("/api/"):
                try:
                    query = _strict_query(parsed.query)
                except (UnicodeError, ValueError):
                    self.send_error(403, "Invalid API URL in candidate preview"); return
                if parsed.path not in READS or (parsed.path.startswith("/api/reports") and not unified_reader) or not set(query).issubset(READS[parsed.path]):
                    self.send_error(403, "API not allowed in candidate preview"); return
                try:
                    with urlopen(api_origin + self.path, timeout=30) as response:
                        raw = response.read(); status = response.status
                        media = response.headers.get("Content-Type", "application/json")
                except HTTPError as error: raw = error.read(); status = error.code; media = "application/json"
                except OSError:
                    raw = b'{"error":"preview_upstream_unavailable","message":"Existing local research service is unavailable"}'; status = 502; media = "application/json"
                with lock: records.append({"method":"GET", "path":self.path, "status":status,"sha256":hashlib.sha256(raw).hexdigest()})
                self.send_bytes(status, raw, media); return
            path = (root / (unquote(parsed.path).lstrip("/") or "index.html")).resolve()
            if not path.is_relative_to(root) or not path.is_file(): self.send_error(404); return
            self.send_bytes(200, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        def deny_write(self):
            with lock: records.append({"method": self.command, "path": self.path, "blocked": True})
            self.send_bytes(405, b'{"error":"read_only_preview"}', "application/json")
        do_POST = do_PUT = do_PATCH = do_DELETE = deny_write
        def log_message(self, *_): pass
    server = ThreadingHTTPServer((host, port), Handler)
    server.records = records
    return server

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5433)
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1")
    parser.add_argument("--reader-origin", default=UPSTREAM, help="Explicit reader origin reachable by preview clients")
    parser.add_argument("--api-origin", default=UPSTREAM, help="Explicit read-only API origin; defaults to the legacy local service")
    parser.add_argument("--static-root", type=Path)
    parser.add_argument("--unified-reader", action="store_true")
    parser.add_argument("--log", type=Path)
    args = parser.parse_args()
    server = make_server(args.port, args.host, args.reader_origin, args.unified_reader, args.static_root, args.api_origin)
    print(f"Read-only frontend candidate: http://{args.host}:{server.server_port}/", flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        server.server_close()
        if args.log:
            args.log.parent.mkdir(parents=True, exist_ok=True)
            args.log.write_text(json.dumps(server.records, indent=2) + "\n")

if __name__ == "__main__": main()
