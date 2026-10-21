"""Single-owner evidence workbench server. Run: python3 -m workbench.server."""
from argparse import ArgumentParser
from datetime import datetime, timezone
import getpass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import socket
import ssl
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

from .api_contracts import (
    GET_CONTRACTS, POST_CONTRACTS, allowed_fields, validate_body, validate_query,
    validate_response,
)
from .chat import ChatError
from .documents import DocumentError
from .runtime import create_runtime_services
from .runtime_config import RuntimeConfig, RuntimePaths
from .session_materials import MAX_UPLOAD_BODY
from .research_results import ResearchResultsService
from .answer_artifacts import AnswerArtifactService
from .report_reader import (
    build_reader_evidence_from_payload, build_reader_source_from_payload,
    frozen_trace_details, list_reader_reports, load_report_diff,
    load_required_reader_snapshots, resolve_report,
)
from .research_tasks import ResearchTaskService
from .sample_research import SampleResearchError, SampleResearchService
from .wiki import WikiService, WikiError
from .owner_auth import AuthError, COOKIE_NAME, OwnerAuth
from .verification_reader import candidate_reader_payload
from .verification_store import (
    VerificationConflict, VerificationFeatureNotInitialized, VerificationIntegrityError,
    VerificationNotFound,
)

ROOT = Path(__file__).resolve().parent.parent
STATIC = {"/": ("index.html", "text/html"), "/index.html": ("index.html", "text/html"),
          "/theme-init.js": ("theme-init.js", "text/javascript"),
          "/workbench-app.js": ("workbench-app.js", "text/javascript"),
          "/workbench-reader.js": ("workbench-reader.js", "text/javascript"),
          "/workbench-shared.js": ("workbench-shared.js", "text/javascript"),
          "/workbench-app.css": ("workbench-app.css", "text/css"),
          "/workbench-reader.css": ("workbench-reader.css", "text/css"),
          "/workbench-base.css": ("workbench-base.css", "text/css"),
          "/research-navigation.js": ("research-navigation.js", "text/javascript"),
          "/report-content.css": ("report-content.css", "text/css"),
          "/report-reader.html": ("report-reader.html", "text/html"),
          "/report-reader.css": ("report-reader.css", "text/css"),
          "/report-reader.js": ("report-reader.js", "text/javascript"),
          "/report-reader-bindings.js": ("report-reader-bindings.js", "text/javascript"),
          "/report-reader-tables.js": ("report-reader-tables.js", "text/javascript"),
          "/report-reader-evidence.js": ("report-reader-evidence.js", "text/javascript"),
          "/report-continuation.js": ("report-continuation.js", "text/javascript"),
          "/report-reader-evidence.css": ("report-reader-evidence.css", "text/css"),
          "/report-theme-init.js": ("report-theme-init.js", "text/javascript"),
          "/vendor/echarts.min.js": ("vendor/echarts.min.js", "text/javascript"),
          "/vendor/echarts.LICENSE.txt": ("vendor/echarts.LICENSE.txt", "text/plain"),
          "/vendor/echarts.NOTICE.txt": ("vendor/echarts.NOTICE.txt", "text/plain")}
AUTH_STATIC = {"/auth.css": ("auth.css", "text/css"), "/auth.js": ("auth.js", "text/javascript"),
               "/ui-preferences.js": ("ui-preferences.js", "text/javascript")}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self' data:; font-src 'self'; base-uri 'none'; form-action 'none'; "
       "frame-ancestors 'none'; object-src 'none'")


def _valid_https_origin(value):
    try:
        origin = urlsplit(value)
        origin.port  # Validate malformed and out-of-range ports.
    except (TypeError, ValueError):
        return False
    return (value == f"https://{origin.netloc}" and origin.scheme == "https"
            and bool(origin.hostname) and origin.username is None and origin.password is None
            and not origin.netloc.endswith(":") and "%" not in origin.netloc and "\\" not in origin.netloc
            and not origin.path and not origin.query and not origin.fragment
            and not any(character.isspace() or ord(character) < 32 for character in value))


def _server_tls_context(cert_path, key_path):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=cert_path, keyfile=key_path)
    return context


def _load_report_closure(server, identity: dict[str, str]) -> tuple[dict, dict]:
    """Read the hash-checked publication-time closure without replaying its sources.

    Source and calculation replay belongs to publication, correction, and explicit
    audit.  Replaying every citation here made opening one frozen report—and every
    evidence click—revalidate the entire report before returning one saved object.
    """
    return load_required_reader_snapshots(server.chat, report_id=identity["report_id"])


def report_html(report: dict) -> str:
    """Render an escaped, printable brief while retaining full audit details."""
    e = lambda value: escape(str(value), quote=True)
    company = report["company"]
    facts = report.get("facts", [])
    evidence = {item["evidence_id"]: item for item in report.get("evidence", [])}
    sources = {item["source_id"]: item for item in report.get("sources", [])}
    activities = {item["activity_id"]: item for item in report.get("activities", [])}

    metric_labels = {
        "revenue": "Revenue", "operating_income": "Operating income", "net_income": "Net income",
        "eps_diluted": "Diluted EPS",
        "segment_revenue_intelligent_cloud": "Intelligent Cloud segment revenue",
        "segment_revenue_more_personal_computing": "More Personal Computing segment revenue",
        "segment_revenue_productivity_and_business_processes": "Productivity and Business Processes segment revenue",
        "segment_operating_income_intelligent_cloud": "Intelligent Cloud segment operating income",
        "segment_operating_income_more_personal_computing": "More Personal Computing segment operating income",
        "segment_operating_income_productivity_and_business_processes": "Productivity and Business Processes segment operating income",
    }
    unit_labels = {"USD millions": "USD millions", "USD billions": "USD billions", "USD/share": "USD/share"}
    period_labels = {"quarter": "Quarter", "year": "Year"}
    basis_labels = {
        "GAAP issuer reconciliation table": "Issuer GAAP reconciliation table",
        "FY2024-era presentation": "FY2024 as-presented basis",
        "FY2025-page recast presentation": "FY2025 page recast basis",
        "issuer newsroom source_reported; accompanying PDF not checked": "Reported in issuer newsroom release; accompanying PDF not checked",
    }
    status_labels = {
        "verified_calculation": "Recalculated from the listed facts", "source_reported": "Reported by the issuer",
        "qualified": "Qualified conclusion", "insufficient": "Insufficient evidence",
    }
    kind_labels = {"calculation": "Calculated conclusion", "fact": "Factual statement", "inference": "Research judgment"}

    def readable_text(value):
        text = str(value or "")
        for key in sorted(metric_labels, key=len, reverse=True):
            text = text.replace(key, metric_labels[key])
        return text.replace("adjusted basis", "adjusted basis")

    def explanation(claim):
        if claim.get("kind") == "calculation" and claim.get("verification_status") == "verified_calculation":
            return "Year-over-year change is calculated from issuer-reported current and prior-year values; verifying the calculation does not constitute an independent audit of the source."
        return readable_text(claim.get("explanation", ""))

    def safe_link(source):
        url = str(source.get("url") or "")
        try:
            parsed = urlsplit(url)
            allowed = (parsed.scheme == "https" and bool(parsed.hostname)
                       and parsed.username is None and parsed.password is None)
        except ValueError:
            allowed = False
        title = e(source.get("title") or "Untitled source")
        if not allowed:
            return title
        return f'<a href="{e(url)}" target="_blank" rel="noreferrer noopener">{title}</a>'

    dates = [str(fact[key]) for fact in facts for key in ("period_start", "period_end") if fact.get(key)]
    if dates:
        first, last = min(dates), max(dates)
        period = f"As of {last}" if first == last else f"{first} to {last}"
    else:
        period = "The report does not provide a verifiable period"

    claim_sections = []
    for number, claim in enumerate(report.get("claims", []), 1):
        claim_facts = [fact for fact in facts if fact.get("fact_id") in claim.get("fact_ids", [])]
        fact_rows = []
        for fact in claim_facts:
            fact_rows.append(
                '<tr>'
                f'<td>{e(metric_labels.get(fact.get("metric"), fact.get("metric")))}</td>'
                f'<td>{e(fact.get("value"))} {e(unit_labels.get(fact.get("unit"), fact.get("unit")))}</td>'
                f'<td>{e(fact.get("period_end") or "Not recorded")} ({e(period_labels.get(fact.get("period_kind"), fact.get("period_kind")))})</td>'
                f'<td>{e(basis_labels.get(fact.get("basis"), fact.get("basis")))}</td>'
                '</tr>')
        facts_html = ""
        if fact_rows:
            facts_html = ('<h4>Values used in the conclusion</h4><table><thead><tr>'
                          '<th scope="col">Metric</th><th scope="col">Value and unit</th>'
                          '<th scope="col">Period</th><th scope="col">Basis</th>'
                          '</tr></thead><tbody>' + ''.join(fact_rows) + '</tbody></table>')

        formulas = []
        for activity_id in claim.get("activity_ids", []):
            parameters = activities.get(activity_id, {}).get("parameters", {})
            formula = parameters.get("formula")
            if formula:
                readable = ("Year-over-year growth = (current value ÷ prior value − 1) × 100"
                            if formula == "((current/prior)-1)*100" else str(formula))
                rounding = parameters.get("rounding")
                if rounding == "0.01 HALF_UP":
                    suffix = "; result rounded to two decimal places using standard rounding"
                else:
                    suffix = f"; result rounded using the {rounding} rule" if rounding else ""
                formulas.append(f'<li>{e(readable)}{e(suffix)}</li>')
        formula_html = '<h4>Calculation method</h4><ul>' + ''.join(formulas) + '</ul>' if formulas else ""

        quotes = []
        for quote_number, evidence_id in enumerate(claim.get("evidence_ids", []), 1):
            item = evidence.get(evidence_id)
            if not item:
                continue
            source = sources.get(item.get("source_id"), {})
            published = source.get("published_at") or "Not recorded"
            quotes.append(
                f'<figure><blockquote>{e(item.get("quote", ""))}</blockquote>'
                f'<figcaption>Quote {quote_number}: {safe_link(source)}; published {e(published)},'
                f' collected {e(source.get("collected_at") or "Not recorded")}</figcaption></figure>')
        if quotes:
            quotes_html = '<h4>Source passages</h4>' + ''.join(quotes)
        else:
            quotes_html = '<p><strong>Source passages:</strong> No direct quotation is linked in the current materials.</p>'

        limitations = ''.join(f'<li>{e(item)}</li>' for item in claim.get("limitations", []))
        claim_sections.append(
            f'<article><h3>{number}. {e(readable_text(claim.get("text", "")))}</h3>'
            f'<p><strong>{e(status_labels.get(claim.get("verification_status"), claim.get("verification_status")))}</strong>'
            f' · {e(kind_labels.get(claim.get("kind"), claim.get("kind")))}</p>'
            f'<p><strong>Explanation:</strong> {e(explanation(claim))}</p>'
            + ('<h4>Limitations of this conclusion</h4><ul>' + limitations + '</ul>' if limitations else "")
            + facts_html + formula_html + quotes_html
            + '</article>')

    source_items = []
    rights_labels = {
        "review_required": "Use and redistribution require review",
        "automation_and_redistribution_not_approved": "Automated use and redistribution not approved",
    }
    for source in report.get("sources", []):
        source_items.append(
            f'<li>{safe_link(source)}; published: {e(source.get("published_at") or "Not recorded")}; '
            f'collected: {e(source.get("collected_at") or "Not recorded")}; '
            f'usage restrictions: {e(rights_labels.get(source.get("rights_status"), source.get("rights_status") or "Not recorded"))}</li>')

    overall_limits = ''.join(f'<li>{e(item)}</li>' for item in report.get("limitations", []))
    conclusion_summary = ''.join(
        f'<li>{e(readable_text(claim.get("text", "")))}</li>' for claim in report.get("claims", []))
    technical_json = e(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{e(company.get("name"))} · Unreviewed research brief</title></head><body><main>'
        '<header>'
        f'<h1>{e(company.get("name"))} ({e(company.get("ticker"))}) Research brief</h1>'
        '<p><strong>Status: unreviewed</strong> · Research snapshot · Not an approval, publication, or investment recommendation</p>'
        f'<dl><dt>Company</dt><dd>{e(company.get("name"))}</dd>'
        f'<dt>Report period</dt><dd>{e(period)}</dd>'
        f'<dt>Data built</dt><dd>{e(report.get("generated_at"))}</dd>'
        f'<dt>Exported</dt><dd>{e(report.get("exported_at"))}</dd></dl>'
        '</header>'
        '<section><h2>Key conclusions</h2><ol>' + conclusion_summary + '</ol></section>'
        '<section><h2>Reading notes and overall limitations</h2><ul>' + overall_limits + '</ul></section>'
        '<section><h2>Conclusion evidence and basis</h2>' + ''.join(claim_sections) + '</section>'
        '<section><h2>Source list</h2><ol>' + ''.join(source_items) + '</ol></section>'
        '<details><summary><strong>Technical verification record</strong> (IDs, SHA-256 hashes, SQL, and complete JSON)</summary>'
        '<p>For reviewers to inspect complete engineering lineage; collapsed by default.</p><pre>' + technical_json + '</pre></details>'
        '</main></body></html>')


class EvidenceHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port=8765, db_path=None, *, chat_db=None, wiki_db=None,
                 sample_db=None, provider=None, documents_root=None, host="127.0.0.1",
                 auth=None, public_origin=None, ssl_context=None, max_workers=32,
                 read_timeout=10, allow_lan_http=False, enable_public_acquisition=False,
                 storage_config=None):
        if (auth is not None and host == "0.0.0.0" and ssl_context is None
                and not allow_lan_http):
            raise RuntimeError("authenticated non-loopback binding requires native TLS or explicit LAN HTTP")
        if public_origin is not None and (host != "127.0.0.1" or ssl_context is not None
                                          or not _valid_https_origin(public_origin)
                                          or (auth is not None and not getattr(auth, "secure_cookie", False))):
            raise RuntimeError("public_origin requires a secure-cookie HTTPS proxy to a loopback backend")
        if not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        paths = RuntimePaths.resolve(
            db_path, chat_db=chat_db, wiki_db=wiki_db, sample_db=sample_db,
            documents_root=documents_root, auth_path=auth.db_path if auth is not None else None)
        config = RuntimeConfig(
            paths, enable_public_acquisition=enable_public_acquisition,
            storage_config=storage_config)
        self.auth = auth
        self.public_origin = public_origin
        self.transport_scheme = "https" if ssl_context is not None or public_origin else "http"
        self.ssl_context = ssl_context
        self.read_timeout = read_timeout
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        self.runtime = create_runtime_services(config, provider=provider)
        self.service = self.runtime.evidence
        self.documents = self.runtime.documents
        self.native_data = self.runtime.native_data
        self.chat = self.runtime.chat
        try:
            self.research_results = ResearchResultsService(self.chat)
            self.answer_artifacts = AnswerArtifactService(self.chat)
            self.research_tasks = ResearchTaskService(self.chat)
            self.sample_research = SampleResearchService(
                paths.evidence, paths.sample, document_service=self.documents)
            self.wiki = WikiService(paths.evidence, paths.wiki)
            super().__init__((host, port), Handler)
        except BaseException:
            self.runtime.close()
            raise

    def process_request(self, request, client_address):
        if not self._worker_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        delegated = False
        try:
            request.settimeout(self.read_timeout)
            if self.ssl_context is not None:
                request = self.ssl_context.wrap_socket(request, server_side=True)
            delegated = True
            super().process_request_thread(request, client_address)
        except Exception:
            if not delegated:
                self.shutdown_request(request)
        finally:
            self._worker_slots.release()

    def server_close(self):
        self.runtime.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "EvidencePreview/0.1"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        # Avoid recording queries, user text, secrets or arbitrary header content.
        pass

    def _send(self, status, body, content_type="application/json", filename=None, headers=None, stream_length=None):
        streaming = stream_length is not None
        self.connection.settimeout(self.server.read_timeout if streaming else None)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(stream_length if streaming else len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        for name, value in headers or ():
            self.send_header(name, value)
        self.end_headers()
        if streaming:
            remaining = stream_length
            while remaining:
                chunk = data.read(min(64 * 1024, remaining))
                if not chunk:
                    raise OSError("Frozen source stream ended before its declared length")
                self.wfile.write(chunk)
                remaining -= len(chunk)
        else:
            self.wfile.write(data)

    def _json(self, status, body, headers=None, encoded=None):
        try:
            validate_response(self.command, urlsplit(self.path).path, status, body)
        except RuntimeError:
            # This fixed emitter intentionally bypasses contract validation.  Calling
            # _json again would recurse if the application produced an invalid shape.
            self._send(500, '{"error":"invalid_response"}')
            return
        self._send(status, encoded if encoded is not None else json.dumps(body, ensure_ascii=False),
                   headers=headers)

    def _authorize_local_request(self):
        hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        # Accept only the actual local destination IP, never an arbitrary Host
        # supplied by a remote client (DNS rebinding protection remains enabled).
        if self.server.server_address[0] != "127.0.0.1":
            hosts.add(f"{self.connection.getsockname()[0]}:{self.server.server_port}")
        if self.server.public_origin:
            hosts.add(urlsplit(self.server.public_origin).netloc)
        host_values = self.headers.get_all("Host", [])
        if len(host_values) != 1 or host_values[0] not in hosts:
            self._json(403, {"error": "Local Host required"})
            return False
        origins = self.headers.get_all("Origin", [])
        expected_origin = self.server.public_origin or f"{self.server.transport_scheme}://{host_values[0]}"
        if len(origins) > 1 or (origins and origins[0] != expected_origin):
            self._json(403, {"error": "Cross-origin access denied"})
            return False
        if self.headers.get("Sec-Fetch-Site") in {"cross-site", "same-site"}:
            self._json(403, {"error": "Cross-site access denied"})
            return False
        return True

    def _send_raw_document(self, raw, digest):
        # Content has already passed ownership and digest verification. Detection
        # only chooses a useful attachment extension; it never changes the bytes.
        try:
            media_type = self.server.documents._validate_media(raw, None)
        except DocumentError:
            media_type = "application/octet-stream"
        extension = {"text/html": "html", "application/pdf": "pdf"}.get(media_type, "bin")
        self._send(200, raw, media_type, f"original-{digest[:12]}.{extension}",
                   headers=[("X-Verified-Source-SHA256", digest)])

    def _session_token(self):
        values = self.headers.get_all("Cookie", [])
        if len(values) != 1 or len(values[0]) > 4096:
            return None
        found = []
        for part in values[0].split(";"):
            name, separator, value = part.strip().partition("=")
            if separator and name == self.server.auth.cookie_name:
                found.append(value)
        return found[0] if len(found) == 1 else None

    def _authenticated(self):
        return self.server.auth is None or self.server.auth.authenticated(self._session_token())

    def _require_auth(self):
        if self._authenticated():
            return True
        self._json(401, {"error": "authentication_required", "message": "Your session has expired. Sign in again."})
        return False

    def _same_origin(self):
        expected = self.server.public_origin or f"{self.server.transport_scheme}://{self.headers['Host']}"
        return self.headers.get_all("Origin", []) == [expected]

    def _read_strict_json(self, expected_fields, maximum=4096):
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
            raise AuthError("application_json_required", 415)
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding", []) or len(lengths) != 1:
            raise AuthError("invalid_request", 400)
        if not lengths[0].isascii() or not lengths[0].isdecimal():
            raise AuthError("invalid_request", 400)
        normalized_length = lengths[0].lstrip("0") or "0"
        if len(normalized_length) > len(str(maximum)):
            raise AuthError("invalid_request", 413)
        try:
            length = int(normalized_length)
        except ValueError as exc:
            raise AuthError("invalid_request", 400) from exc
        if not 0 < length <= maximum:
            raise AuthError("invalid_request", 413)
        self.connection.settimeout(5)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise AuthError("invalid_request", 400)
        def strict_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON field")
                result[key] = value
            return result
        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=strict_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise AuthError("invalid_request", 400) from exc
        if not isinstance(data, dict) or set(data) != set(expected_fields):
            raise AuthError("invalid_request", 400)
        try:
            validate_body(self.path, data)
        except ValueError as exc:
            raise AuthError("invalid_request", 400) from exc
        return data

    def _auth_status(self):
        auth = self.server.auth
        return {"authentication": "disabled" if auth is None else "required",
                "initialized": True if auth is None else auth.has_owner(),
                "authenticated": self._authenticated()}

    def _agent_status(self):
        chat_status = self.server.chat.status()
        provider = chat_status.get("provider", {})
        return {
            "enabled": bool(chat_status.get("available")),
            "reason": provider.get("reason", "provider status unavailable"),
            "chat_implemented": True,
            "live_verification": "not_performed",
        }

    def do_GET(self):
        if not self._authorize_local_request():
            return
        if len(self.path) > 4096:
            self._json(414, {"error": "URI too long"})
            return
        try:
            parts = urlsplit(self.path)
            if parts.scheme or parts.netloc or parts.fragment:
                raise ValueError("absolute or fragmented URI not allowed")
            route = parts.path
            # A private registered route must not expose its query schema as an
            # authentication oracle. Host/origin and URI framing checks remain first.
            if ((route == "/api/reports/diff" or route in GET_CONTRACTS)
                    and (route == "/api/reports/diff"
                         or GET_CONTRACTS[route]["access"] not in {"public_health", "public_auth"})
                    and not self._require_auth()):
                return
            params = parse_qs(parts.query, keep_blank_values=True, max_num_fields=8)
            if any(len(values) != 1 for values in params.values()):
                raise ValueError("duplicate parameters")
            if route in GET_CONTRACTS:
                validate_query(route, params)
            if route == "/api/health":
                self._json(200, {"status": "ok"})
                return
            if route == "/api/auth/status":
                self._json(200, self._auth_status())
                return
            if route in AUTH_STATIC:
                if params:
                    raise ValueError("static resource has no query parameters")
                filename, content_type = AUTH_STATIC[route]
                self._send(200, (ROOT / "web" / filename).read_bytes(), content_type)
                return
            if route in STATIC:
                if params:
                    identity = set(params) - {"page", "reading"}
                    if route not in {"/report-reader.html", "/report-reader-legacy.html"} or identity not in ({"report_id"}, {"conversation_id", "message_id"}, {"conversation_id", "candidate_id", "version_id"}):
                        raise ValueError("static resource has no query parameters")
                    if "page" in params and (not params["page"][0].isascii() or not params["page"][0].isdecimal()
                                             or not 1 <= int(params["page"][0]) <= 10000):
                        raise ValueError("invalid reader page")
                    if "reading" in params and params["reading"][0] not in {"single", "two", "all"}:
                        raise ValueError("invalid reading mode")
                if not self._authenticated():
                    if route in {"/", "/index.html", "/legacy.html"}:
                        self._send(200, (ROOT / "web" / "login.html").read_bytes(), "text/html")
                    elif route in {"/report-reader.html", "/report-reader-legacy.html"}:
                        ordered = [(key, params[key][0]) for key in
                                   ("report_id", "conversation_id", "message_id", "page", "reading")
                                   if key in params]
                        if {"conversation_id", "candidate_id", "version_id"} <= set(params):
                            ordered = [(key, params[key][0]) for key in
                                       ("conversation_id", "candidate_id", "version_id", "page", "reading")
                                       if key in params]
                        self._send(303, b"", "text/plain",
                                   headers=[("Location", "/#/report-reader?" + urlencode(ordered))])
                    else:
                        self._json(401, {"error": "authentication_required", "message": "Sign in first."})
                    return
                filename, content_type = STATIC[route]
                self._send(200, (ROOT / "web" / filename).read_bytes(), content_type)
                return
            allowed = {path: allowed_fields("GET", path) for path in GET_CONTRACTS
                       if path not in {"/api/health", "/api/auth/status"}}
            allowed["/api/reports/diff"] = {"old_report_id", "new_report_id"}
            if route not in allowed:
                self._json(404, {"error": "Not found"})
                return
            service = self.server.service
            if route.startswith("/api/sample-research/"):
                samples = self.server.sample_research
                if route == "/api/sample-research/options":
                    result = samples.options()
                else:
                    version_text = params.get("version", [None])[0]
                    if version_text is not None:
                        if not version_text.isascii() or not version_text.isdecimal() or len(version_text) > 12:
                            raise ValueError("invalid version")
                        version = int(version_text)
                    else:
                        version = None
                    if route == '/api/sample-research/raw-source':
                        if set(params) != {'id', 'version', 'company_id', 'source_sha256'}:
                            raise ValueError('missing sample source ownership')
                        raw = samples.read_raw_source(params['id'][0], version, params['company_id'][0], params['source_sha256'][0])
                        self._send_raw_document(raw, params['source_sha256'][0])
                        return
                    result = samples.get(params.get("id", [""])[0], version)
            elif route.startswith("/api/documents/"):
                documents = self.server.documents
                if documents is None:
                    self._json(503, {"error": "document_service_unavailable", "message": "Document catalog is not installed"})
                    return
                company_id = params.get("company", [""])[0]
                if route == "/api/documents/search":
                    limit_text = params.get("limit", ["8"])[0]
                    if not limit_text.isascii() or not limit_text.isdecimal() or len(limit_text) > 2:
                        raise ValueError("invalid limit")
                    result = documents.search_documents(
                        company_id=company_id, query=params.get("query", [""])[0],
                        limit=int(limit_text),
                    )
                elif route == "/api/documents/passage":
                    context_text = params.get("context", ["500"])[0]
                    if not context_text.isascii() or not context_text.isdecimal() or len(context_text) > 5:
                        raise ValueError("invalid context")
                    result = documents.read_passage(
                        company_id=company_id, passage_id=params.get("id", [""])[0],
                        context_chars=int(context_text),
                    )
                else:
                    result = documents.get_document_coverage(company_id=company_id)
            elif route.startswith("/api/wiki/"):
                wiki = self.server.wiki
                if route == "/api/wiki/page":
                    version_text = params.get("version", [None])[0]
                    if version_text is not None:
                        if (not version_text.isascii() or not version_text.isdecimal()
                                or len(version_text) > 12):
                            raise ValueError("invalid version")
                        version = int(version_text)
                    else:
                        version = None
                    result = wiki.get_page(params.get("company", [""])[0], version)
                elif route == "/api/wiki/changes":
                    result = {"changes": wiki.list_changes(params.get("company", [""])[0])}
                else:
                    result = wiki.get_change(params.get("id", [""])[0])
            elif route == "/api/research/materials":
                result = self.server.chat.materials.list(params.get("conversation_id", [""])[0])
            elif route == "/api/research/materials/raw":
                raw, media_type, filename = self.server.chat.materials.raw(
                    params.get("conversation_id", [""])[0], params.get("document_version_id", [""])[0])
                self._send(200, raw, media_type, filename)
                return
            elif route == "/api/reports":
                if set(params) != {"conversation_id"}:
                    raise ValueError("missing report conversation")
                result = list_reader_reports(self.server.chat, params["conversation_id"][0])
            elif route == "/api/reports/diff":
                if set(params) != {"old_report_id", "new_report_id"}:
                    raise ValueError("missing report diff identities")
                result = load_report_diff(
                    self.server.chat, old_report_id=params["old_report_id"][0],
                    new_report_id=params["new_report_id"][0])
            elif route in {"/api/verifications", "/api/verification/candidate", "/api/verification/version", "/api/verification/reader"}:
                chat = self.server.chat
                conversation_id = params["conversation_id"][0]
                if route == "/api/verifications":
                    limit_text = params.get("limit", ["50"])[0]
                    if not limit_text.isascii() or not limit_text.isdecimal() or len(limit_text) > 3:
                        raise ValueError("invalid candidate limit")
                    result = {"conversation_id": conversation_id,
                              "candidates": chat.list_verification_candidates(conversation_id, limit=int(limit_text))}
                else:
                    candidate_id = params["candidate_id"][0]
                    candidate = chat.get_verification_candidate(conversation_id, candidate_id)
                    if route == "/api/verification/candidate":
                        result = candidate
                    else:
                        version = chat.get_verification_version(
                            conversation_id, candidate_id, params["version_id"][0])
                        result = (candidate_reader_payload(candidate, version)
                                  if route == "/api/verification/reader" else version)
            elif route in {"/api/research/tasks", "/api/research/task"}:
                tasks = self.server.research_tasks
                if route == "/api/research/tasks":
                    include_internal = params.get("include_internal", ["false"])[0]
                    if include_internal not in {"true", "false"}:
                        raise ValueError("invalid include_internal")
                    result = tasks.list(include_internal == "true")
                else:
                    result = tasks.get(params.get("id", [""])[0])
            elif route == "/api/research/native-source":
                if set(params) != {"conversation_id", "message_id", "snapshot_id"}:
                    raise ValueError("missing native source ownership")
                from .native_sql import NativeSqlError
                chat = self.server.chat
                answer = chat.answer_context(params["conversation_id"][0], params["message_id"][0])
                if chat.native_data is None:
                    raise ChatError("native_source_unavailable", "Native source service is unavailable", 503)
                try:
                    scope = answer["source_snapshot"].get("native_sources")
                    available = chat.native_data.list_sources(owner=answer["conversation_id"], scope=scope)
                    identifier = params["snapshot_id"][0]
                    if identifier not in {item["snapshot_id"] for item in available}:
                        raise ChatError("source_not_in_snapshot", "Native source was not frozen for this answer", 404)
                    with chat.native_data.open_snapshot(identifier, owner=answer["conversation_id"]) as (source, handle):
                        self._send(200, handle, source["media_type"], f"original-{source['sha256'][:12]}.sqlite",
                                   headers=[("X-Verified-Source-SHA256", source["sha256"])],
                                   stream_length=source["byte_length"])
                except NativeSqlError as exc:
                    raise ChatError(exc.code, exc.message, 409) from exc
                return
            elif route == "/api/research/raw-source":
                if set(params) != {"conversation_id", "message_id", "source_sha256"}:
                    raise ValueError("missing raw source ownership")
                raw = self.server.chat.read_raw_source(params['conversation_id'][0], params['message_id'][0], params['source_sha256'][0])
                self._send_raw_document(raw, params['source_sha256'][0])
                return
            elif route == "/api/research/artifact":
                if set(params) != {"conversation_id", "message_id"}:
                    raise ValueError("missing artifact ownership")
                result = self.server.answer_artifacts.get(params["conversation_id"][0], params["message_id"][0])
            elif route == "/api/reports/reader":
                if set(params) == {"report_id"}:
                    identity = resolve_report(self.server.chat, report_id=params["report_id"][0])
                elif set(params) == {"conversation_id", "message_id"}:
                    identity = resolve_report(self.server.chat, conversation_id=params["conversation_id"][0],
                                              message_id=params["message_id"][0])
                else:
                    raise ValueError("missing report ownership")
                result, _artifact = _load_report_closure(self.server, identity)
            elif route == "/api/reports/evidence":
                if set(params) == {"report_id", "anchor_id"}:
                    identity = resolve_report(self.server.chat, report_id=params["report_id"][0])
                elif set(params) == {"conversation_id", "message_id", "anchor_id"}:
                    identity = resolve_report(self.server.chat, conversation_id=params["conversation_id"][0],
                                              message_id=params["message_id"][0])
                else:
                    raise ValueError("missing report evidence ownership")
                payload, artifact = _load_report_closure(self.server, identity)
                try:
                    result = build_reader_evidence_from_payload(payload, params["anchor_id"][0])
                    result["frozen_detail"] = frozen_trace_details(
                        payload, artifact, params["anchor_id"][0])
                except KeyError:
                    self._json(404, {"error": "report_anchor_not_found"})
                    return
            elif route == "/api/reports/source":
                if set(params) == {"report_id", "source_id"}:
                    identity = resolve_report(self.server.chat, report_id=params["report_id"][0])
                elif set(params) == {"conversation_id", "message_id", "source_id"}:
                    identity = resolve_report(self.server.chat, conversation_id=params["conversation_id"][0],
                                              message_id=params["message_id"][0])
                else:
                    raise ValueError("missing report source ownership")
                payload, _artifact = _load_report_closure(self.server, identity)
                try:
                    result = build_reader_source_from_payload(payload, params["source_id"][0])
                except KeyError:
                    self._json(404, {"error": "report_source_not_found"})
                    return
            elif route == "/api/reports/raw-source":
                if set(params) == {"report_id", "source_id"}:
                    identity = resolve_report(self.server.chat, report_id=params["report_id"][0])
                elif set(params) == {"conversation_id", "message_id", "source_id"}:
                    identity = resolve_report(self.server.chat, conversation_id=params["conversation_id"][0],
                                              message_id=params["message_id"][0])
                else:
                    raise ValueError("missing report raw source ownership")
                payload, _artifact = _load_report_closure(self.server, identity)
                try:
                    source = build_reader_source_from_payload(payload, params["source_id"][0])
                except KeyError:
                    self._json(404, {"error": "report_source_not_found"})
                    return
                if not source["raw_download_available"]:
                    self._json(404, {"error": "report_raw_source_unavailable"})
                    return
                raw = self.server.chat.read_raw_source(
                    identity["conversation_id"], identity["message_id"], source["source"]["source_sha256"])
                self._send_raw_document(raw, source["source"]["source_sha256"])
                return
            elif route.startswith("/api/research/"):
                results = self.server.research_results
                version_text = params.get("version", [None])[0]
                if version_text is not None:
                    if not version_text.isascii() or not version_text.isdecimal() or len(version_text) > 12:
                        raise ValueError("invalid version")
                    version = int(version_text)
                else:
                    version = None
                if route == "/api/research/results":
                    result = results.list(params.get("conversation", [""])[0])
                elif route == "/api/research/result":
                    if not {"conversation_id", "company_id"} <= set(params):
                        raise ValueError("missing result scope")
                    result = results.get(
                        params.get("id", [""])[0], params["conversation_id"][0],
                        params["company_id"][0], version)
                else:
                    if not {"conversation_id", "company_id"} <= set(params):
                        raise ValueError("missing result scope")
                    body, content_type, filename = results.export(
                        params.get("id", [""])[0], params["conversation_id"][0],
                        params["company_id"][0], version, params.get("format", ["json"])[0])
                    self._send(200, body, content_type, filename)
                    return
            elif route.startswith("/api/chat/"):
                chat = self.server.chat
                if route == "/api/chat/status":
                    result = chat.status()
                elif route == "/api/chat/conversations":
                    include_internal = params.get("include_internal", ["false"])[0]
                    if include_internal not in {"true", "false"}:
                        raise ValueError("invalid include_internal")
                    result = chat.list_conversations(
                        params.get("company", [None])[0], include_internal=include_internal == "true")
                elif route == "/api/chat/conversation":
                    result = chat.get_conversation(params.get("id", [""])[0])
                elif route == "/api/chat/citation":
                    result = chat.read_citation(
                        params.get("conversation", [""])[0], params.get("message", [""])[0],
                        params.get("kind", [""])[0], params.get("id", [""])[0])
                else:
                    after = params.get("after", ["0"])[0]
                    if not after.isascii() or not after.isdecimal() or len(after) > 12:
                        raise ValueError("invalid cursor")
                    result = chat.get_run(params.get("id", [""])[0], int(after))
            elif route == "/api/overview":
                result = service.overview()
                result["agent"] = self._agent_status()
            elif route in {"/api/company", "/api/evidence", "/api/source"}:
                identifier = params.get("id", [""])[0]
                if not identifier or len(identifier) > 180:
                    raise ValueError("id required (max 180 characters)")
                method = {"/api/company": service.company, "/api/evidence": service.evidence,
                          "/api/source": service.source}[route]
                result = method(identifier)
            else:
                company_id = params.get("company", [""])[0]
                if company_id not in {"MSFT", "AAPL"}:
                    raise ValueError("company must be MSFT or AAPL")
                fmt = params.get("format", ["json"])[0]
                if fmt not in {"json", "html"}:
                    raise ValueError("format must be json or html")
                result = service.report(company_id)
                result["exported_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                if fmt == "html":
                    self._send(200, report_html(result), "text/html", f"{company_id}-unreviewed.html")
                else:
                    self._send(200, json.dumps(result, ensure_ascii=False, indent=2), "application/json",
                               f"{company_id}-unreviewed.json")
                return
            if route == "/api/reports/diff":
                # This approved read-only endpoint is intentionally local to the report
                # reader and is not added to the separately frozen API contract registry.
                self._send(200, json.dumps(result, ensure_ascii=False))
                return
            self._json(200, result)
        except SampleResearchError as exc:
            self._json(exc.status_code, exc.as_dict())
        except ChatError as exc:
            self._json(exc.status_code, exc.as_dict())
        except VerificationNotFound as exc:
            self._json(404, {"error": "verification_not_found", "message": str(exc)})
        except VerificationConflict as exc:
            self._json(409, {"error": "verification_conflict", "message": str(exc)})
        except (VerificationFeatureNotInitialized, VerificationIntegrityError) as exc:
            self._json(503, {"error": "verification_unavailable", "message": str(exc)})
        except WikiError as exc:
            self._json(exc.status_code, exc.as_dict())
        except DocumentError as exc:
            status = 404 if exc.code in {"document_not_found", "passage_not_found"} else 400
            if exc.code in {"object_tampered", "catalog_read_failed", "search_failed"}:
                status = 503
            self._json(status, exc.as_dict())
        except KeyError:
            self._json(404, {"error": "Unknown object"})
        except ValueError:
            self._json(400, {"error": "Invalid request parameters"})
        except (BrokenPipeError, ConnectionResetError):
            # A cancelled read must not produce a second response on a closed socket.
            self.close_connection = True
        except (sqlite3.Error, OSError, RuntimeError):
            self._json(503, {"error": "Evidence store unavailable. Run the verified local build first."})

    def do_POST(self):
        if not self._authorize_local_request():
            return
        if self.path in {"/api/auth/initialize", "/api/auth/login", "/api/auth/logout"}:
            self._handle_auth_post()
            return
        if not self._require_auth():
            return
        routes = set(POST_CONTRACTS) - {"/api/auth/initialize", "/api/auth/login", "/api/auth/logout"}
        if self.path not in routes:
            self._json(405, {"error": "Only explicit chat and wiki operations support POST; evidence remains read-only"})
            return
        # Browser-facing local writes require an exact same-origin Origin, not merely Host.
        if not self._same_origin():
            self._json(403, {"error": "Same-origin Origin required for write operations"})
            return
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
            self._json(415, {"error": "application/json required"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding", []) or len(lengths) != 1:
            self._json(400, {"error": "One Content-Length and no Transfer-Encoding required"})
            return
        try:
            if not lengths[0].isascii() or not lengths[0].isdecimal():
                raise ValueError("invalid length")
            maximum = MAX_UPLOAD_BODY if self.path == "/api/research/materials" else 65536
            normalized_length = lengths[0].lstrip("0") or "0"
            if len(normalized_length) > len(str(maximum)):
                self._json(413, {"error": f"Request body must contain 1..{maximum} bytes"})
                return
            length = int(normalized_length)
            if not 0 < length <= maximum:
                self._json(413, {"error": f"Request body must contain 1..{maximum} bytes"})
                return
            self.connection.settimeout(5)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("incomplete body")
            def strict_object(pairs):
                data = {}
                for key, value in pairs:
                    if key in data:
                        raise ValueError("duplicate JSON field")
                    data[key] = value
                return data
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=strict_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
            if not isinstance(data, dict):
                raise ValueError("unexpected fields")
            validate_body(self.path, data)
            if self.path == "/api/verification/cancel":
                result = self.server.chat.cancel_verification(data["conversation_id"], data["candidate_id"])
                code = 200
            elif self.path == "/api/verification/continue":
                result = self.server.chat.continue_verification(
                    data["conversation_id"], data["candidate_id"],
                    data["expected_version_id"], data["request_id"])
                code = 202
            elif self.path == "/api/research/materials":
                result = self.server.chat.materials.upload(**data)
                code = 201
            elif self.path == "/api/research/decisions/answer":
                result = self.server.chat.decisions.answer(
                    data["decision_id"], data["selected_ids"], data["notes"], data["action"], data["request_id"])
                code = 202 if result.get("run_id") else 200
            elif self.path == "/api/sample-research/results":
                self.server.service.validate_store()
                result = self.server.sample_research.create(data)
                code = 201
            elif self.path in {"/api/research/intake", "/api/research/tasks", "/api/research/tasks/retry-intent", "/api/research/tasks/resolve", "/api/research/tasks/start"}:
                tasks = self.server.research_tasks
                if self.path == "/api/research/intake":
                    result = tasks.intake(data["question"], data["context_conversation_id"])
                    code = 200
                elif self.path == "/api/research/tasks":
                    result = tasks.create(
                        data["question"], data["context_conversation_id"], data["request_id"],
                        data.get("origin", "user"))
                    code = 201
                elif self.path == "/api/research/tasks/retry-intent":
                    result = tasks.retry_intent(data["task_id"], data["expected_scope_version"])
                    code = 200
                elif self.path == "/api/research/tasks/resolve":
                    result = tasks.resolve(data["task_id"], data["expected_scope_version"], data["company_id"])
                    code = 200
                else:
                    result = tasks.start(data["task_id"], data["expected_scope_version"])
                    code = 202
            elif self.path.startswith("/api/research/"):
                results = self.server.research_results
                if self.path == "/api/research/results":
                    result = results.create(data["conversation_id"], data["message_id"], data["title"],
                                            data["body"], data["limitations"])
                    code = 201
                else:
                    result = results.revise(
                        data["result_id"], data["conversation_id"], data["company_id"],
                        data["expected_version"], data["title"], data["body"], data["limitations"])
                    code = 200
            elif self.path.startswith("/api/chat/"):
                if any(not isinstance(value, str) for value in data.values()):
                    raise ValueError("string fields required")
                chat = self.server.chat
                if self.path == "/api/chat/conversations":
                    result, code = chat.create_conversation(data["company_id"]), 201
                elif self.path == "/api/chat/messages":
                    # Fail closed before providing a model with data from a damaged store.
                    self.server.service.validate_store()
                    result, code = chat.submit(
                        data["conversation_id"], data["message"],
                        request_id=data.get("request_id"), base_message_id=data.get("base_message_id")), 202
                else:
                    result, code = chat.cancel(data["run_id"]), 200
            else:
                # Structured Wiki fields are validated by WikiService; evidence is
                # fully scanned before every write-facing lifecycle operation.
                self.server.service.validate_store()
                wiki = self.server.wiki
                if self.path == "/api/wiki/changes":
                    result = wiki.create_change(data["company_id"], data["base_version"],
                                                data["title"], data["statements"])
                    code = 201
                elif self.path == "/api/wiki/review":
                    result = wiki.review(data["change_id"], data["expected_hash"], data["decision"])
                    code = 200
                else:
                    result = wiki.publish(data["change_id"], data["expected_hash"])
                    code = 200
            self._json(code, result)
        except SampleResearchError as exc:
            self._json(exc.status_code, exc.as_dict())
        except ChatError as exc:
            self._json(exc.status_code, exc.as_dict())
        except VerificationNotFound as exc:
            self._json(404, {"error": "verification_not_found", "message": str(exc)})
        except VerificationConflict as exc:
            self._json(409, {"error": "verification_conflict", "message": str(exc)})
        except (VerificationFeatureNotInitialized, VerificationIntegrityError) as exc:
            self._json(503, {"error": "verification_unavailable", "message": str(exc)})
        except DocumentError as exc:
            self._json(400, exc.as_dict())
        except WikiError as exc:
            self._json(exc.status_code, exc.as_dict())
        except (ValueError, UnicodeError, RecursionError):
            kind = ("research" if self.path.startswith("/api/research/") else
                    "chat" if self.path.startswith("/api/chat/") else "wiki")
            self._json(400, {"error": f"Invalid {kind} request"})
        except (socket.timeout, TimeoutError):
            self._json(408, {"error": "Request body timeout"})
        except (sqlite3.Error, OSError, RuntimeError):
            self._json(503, {"error": "Chat, wiki, or evidence store unavailable"})

    def _handle_auth_post(self):
        auth = self.server.auth
        if auth is None:
            self._json(404, {"error": "not_found"})
            return
        if not self._same_origin():
            self._json(403, {"error": "same_origin_required"})
            return
        try:
            if self.path == "/api/auth/initialize":
                data = self._read_strict_json(allowed_fields("POST", self.path))
                auth.initialize(data["bootstrap"], data["password"])
                self._json(200, {"status": "initialized"})
            elif self.path == "/api/auth/login":
                data = self._read_strict_json(allowed_fields("POST", self.path))
                token, expires = auth.login(data["password"], self.client_address[0])
                self._json(200, {"status": "authenticated", "expires_at": expires},
                           headers=[("Set-Cookie", auth.cookie(token, auth.session_seconds))])
            else:
                self._read_strict_json(allowed_fields("POST", self.path))
                auth.logout(self._session_token())
                self._json(200, {"status": "logged_out"},
                           headers=[("Set-Cookie", auth.cookie("", 0))],
                           encoded='{"status":"logged_out"}')
        except AuthError as exc:
            self._json(exc.status, {"error": exc.code})
        except (socket.timeout, TimeoutError):
            self._json(408, {"error": "request_timeout"})

    def _reject_write(self):
        if self._authorize_local_request():
            self._json(405, {"error": "Unsupported method; evidence remains read-only"})

    do_PUT = _reject_write
    do_PATCH = _reject_write
    do_DELETE = _reject_write
    do_OPTIONS = _reject_write


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", choices=["127.0.0.1", "0.0.0.0"], default="127.0.0.1",
                        help="Bind address; authenticated wildcard binding requires native TLS or explicit trusted-LAN HTTP")
    parser.add_argument("--db", type=Path, default=ROOT / "var/evidence.sqlite3")
    parser.add_argument("--chat-db", type=Path, default=None)
    parser.add_argument("--wiki-db", type=Path, default=None)
    parser.add_argument("--sample-db", type=Path, default=None)
    parser.add_argument("--documents-root", type=Path, default=None)
    parser.add_argument("--storage-config", type=Path, default=None,
                        help="Shared strict JSON object-storage configuration for API and worker")
    parser.add_argument("--auth-dir", type=Path, default=ROOT / "var/auth")
    parser.add_argument("--enable-public-acquisition", action="store_true",
                        help="Allow current-data research to acquire configured Big Tech annual reports")
    parser.add_argument("--no-auth", action="store_true", help="Development only; loopback unless explicitly allowed for LAN testing")
    parser.add_argument("--allow-insecure-no-auth", action="store_true",
                        help="TEST ONLY: allow --no-auth on --host 0.0.0.0; all reachable clients can read and change data")
    parser.add_argument(
        "--allow-lan-http", action="store_true",
        help="Allow authenticated plaintext HTTP only on trusted LAN via --host 0.0.0.0; never expose to WAN")
    parser.add_argument("--init-owner", action="store_true", help="Initialize owner password interactively and exit")
    parser.add_argument("--secure-cookie", action="store_true", help="Require Secure owner cookies")
    parser.add_argument("--public-origin", help="Exact HTTPS origin of a trusted loopback reverse proxy")
    parser.add_argument("--tls-cert", type=Path, help="PEM certificate for native TLS")
    parser.add_argument("--tls-key", type=Path, help="PEM private key for native TLS")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port out of range")
    if (args.no_auth or args.allow_insecure_no_auth) and args.init_owner:
        parser.error("no-auth test options and --init-owner cannot be combined")
    if args.allow_lan_http and args.init_owner:
        parser.error("--allow-lan-http cannot be combined with --init-owner")
    try:
        auth_path = args.auth_dir / "owner.sqlite3"
        RuntimePaths.resolve(
            args.db, chat_db=args.chat_db, wiki_db=args.wiki_db, sample_db=args.sample_db,
            documents_root=args.documents_root, auth_path=None if args.no_auth else auth_path)

        # This command is local and exits without binding; listen transport options are irrelevant.
        if args.init_owner:
            password = getpass.getpass("New owner password: ")
            confirmation = getpass.getpass("Confirm owner password: ")
            if password != confirmation:
                parser.error("password confirmation does not match")
            auth = OwnerAuth(auth_path, secure_cookie=args.secure_cookie, create_bootstrap=False)
            auth.initialize_local(password)
            print("Owner initialized. No bootstrap secret was created or displayed.", flush=True)
            return

        if args.allow_insecure_no_auth:
            if not args.no_auth or args.host != "0.0.0.0":
                parser.error("--allow-insecure-no-auth requires --no-auth and --host 0.0.0.0")
            if args.allow_lan_http or args.public_origin or args.secure_cookie or args.tls_cert or args.tls_key:
                parser.error("test no-auth mode cannot be combined with authenticated transport options")
        if args.no_auth and args.host != "127.0.0.1" and not args.allow_insecure_no_auth:
            parser.error("--no-auth on LAN requires explicit --allow-insecure-no-auth for testing")
        if args.allow_lan_http:
            if args.no_auth or args.host != "0.0.0.0":
                parser.error("--allow-lan-http requires authentication and --host 0.0.0.0")
            if (args.tls_cert or args.tls_key or args.secure_cookie
                    or args.public_origin):
                parser.error("--allow-lan-http cannot be combined with TLS, --secure-cookie, or --public-origin")
        if bool(args.tls_cert) != bool(args.tls_key):
            parser.error("--tls-cert and --tls-key must be provided together")
        native_tls = bool(args.tls_cert)
        if args.public_origin:
            if not _valid_https_origin(args.public_origin):
                parser.error("--public-origin must be an exact HTTPS origin without userinfo, path, query, or fragment")
            if args.host != "127.0.0.1":
                parser.error("--public-origin requires a loopback backend")
            if not args.secure_cookie:
                parser.error("--public-origin and --secure-cookie must be used together")
            if native_tls:
                parser.error("--public-origin is only for an HTTPS reverse proxy, not native TLS")
        elif args.secure_cookie and not native_tls:
            parser.error("--secure-cookie requires HTTPS --public-origin or native TLS")
        if (not args.no_auth and args.host == "0.0.0.0" and not native_tls
                and not args.allow_lan_http):
            parser.error("authenticated --host 0.0.0.0 requires --tls-cert and --tls-key or explicit --allow-lan-http")

        tls_context = _server_tls_context(args.tls_cert, args.tls_key) if native_tls else None
        auth = None if args.no_auth else OwnerAuth(auth_path,
                                                   secure_cookie=args.secure_cookie or native_tls,
                                                   lan_http_cookie=args.allow_lan_http)
        server = EvidenceHTTPServer(args.port, args.db, chat_db=args.chat_db, wiki_db=args.wiki_db,
                                    sample_db=args.sample_db, documents_root=args.documents_root, host=args.host,
                                    auth=auth, public_origin=args.public_origin, ssl_context=tls_context,
                                    allow_lan_http=args.allow_lan_http,
                                    enable_public_acquisition=args.enable_public_acquisition,
                                    storage_config=args.storage_config)
        server.service.validate_store()
    except (OSError, sqlite3.Error, RuntimeError, WikiError, DocumentError, AuthError) as exc:
        parser.error(f"Cannot start verified evidence workbench: {exc}. Run python3 -m workbench.init_public")
    scheme = "https" if tls_context else "http"
    if args.allow_insecure_no_auth:
        print("WARNING: TEST MODE, authentication disabled. All reachable clients can read and change data. Never expose to WAN.", flush=True)
    if args.allow_lan_http:
        print("WARNING: plaintext authenticated HTTP is enabled for a trusted LAN only; never expose this listener to the WAN.",
              flush=True)
    print(f"Evidence workbench listening on {scheme}://{args.host}:{server.server_port}; personal single-owner mode.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
