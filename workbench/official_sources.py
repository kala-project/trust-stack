"""Bounded acquisition of issuer/SEC annual reports from explicit official sources.

The module deliberately has no general-purpose URL tool. Discovery starts only from
hard-coded official indexes and every request (including redirects) is revalidated.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import queue
import re
import socket
import ssl
import tempfile
import threading
import time
from typing import Any, Callable
from urllib.parse import unquote, urljoin, urlsplit


MSFT_CIK = "0000789019"
MSFT_INDEX = "https://www.microsoft.com/en-us/Investor/annual-reports.aspx"
MSFT_SUBMISSIONS = f"https://data.sec.gov/submissions/CIK{MSFT_CIK}.json"
MAX_DISCOVERY_BYTES = 2_000_000
MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_REDIRECTS = 4
DEFAULT_TIMEOUT_SECONDS = 12.0
_ALLOWED_MIME = {"text/html", "application/xhtml+xml", "application/pdf"}
_DISCOVERY_MIME = _ALLOWED_MIME | {"application/json", "text/json", "text/plain"}
_MS_REPORT_PATH = re.compile(r"/(?:en-us/)?investor/reports/ar\d{2}/[A-Za-z0-9._~!$&'()*+,;=:@%/-]+", re.I)
_SEC_REPORT_PATH = re.compile(r"/Archives/edgar/data/789019/[A-Za-z0-9._~!$&'()*+,;=:@%/-]+", re.I)


class OfficialSourceError(ValueError):
    """Stable, non-speculative error returned by the tool adapter."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message

    def as_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


@dataclass(frozen=True)
class NetworkResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class _Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "a":
            self._href = next((value for key, value in attrs if key.casefold() == "href"), None)
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href, self._text = None, []


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _check_active(cancel_event: threading.Event | None, deadline: float | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise OfficialSourceError("cancelled", "official source acquisition was cancelled")
    if deadline is not None and time.monotonic() >= deadline:
        raise OfficialSourceError("deadline_exceeded", "official source acquisition deadline was exceeded")


def _remaining(deadline: float | None, maximum: float) -> float:
    _check_active(None, deadline)
    return maximum if deadline is None else min(maximum, max(0.001, deadline - time.monotonic()))


def _public_addresses(host: str, resolver: Callable[[str], list[str]], *,
                      cancel_event: threading.Event | None = None,
                      deadline: float | None = None) -> list[str]:
    """Bound caller wait even when the system resolver itself cannot be interrupted."""
    _check_active(cancel_event, deadline)
    outcome: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            outcome.put((True, resolver(host)))
        except BaseException as exc:
            outcome.put((False, exc))

    threading.Thread(target=resolve, name="official-dns", daemon=True).start()
    while True:
        _check_active(cancel_event, deadline)
        try:
            succeeded, value = outcome.get(timeout=_remaining(deadline, 0.05))
            break
        except queue.Empty:
            continue
    if not succeeded:
        if not isinstance(value, (OSError, socket.error)):
            raise value
        raise OfficialSourceError("missing_retrieval", "official host DNS lookup failed") from value
    addresses = list(dict.fromkeys(value))
    if not addresses:
        raise OfficialSourceError("missing_retrieval", "official host DNS lookup returned no address")
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError as exc:
            raise OfficialSourceError("unsafe_network_target", "official host resolved to an invalid address") from exc
        if not address.is_global:
            raise OfficialSourceError("unsafe_network_target", "private, local, or non-global network targets are blocked")
    return addresses


def system_resolver(host: str) -> list[str]:
    return [item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]


class PinnedHTTPSHTTPTransport:
    """One HTTPS request pinned to a previously validated public address."""

    def __init__(self, resolver: Callable[[str], list[str]] = system_resolver):
        self.resolver = resolver
        self.context = ssl.create_default_context()

    def get(self, url: str, *, timeout: float, max_bytes: int,
            cancel_event: threading.Event | None = None,
            deadline: float | None = None) -> NetworkResponse:
        parts = urlsplit(url)
        addresses = _public_addresses(parts.hostname or "", self.resolver,
                                      cancel_event=cancel_event, deadline=deadline)
        last_error: OSError | None = None
        for address in addresses:
            _check_active(cancel_event, deadline)
            try:
                raw = socket.create_connection((address, 443), timeout=_remaining(deadline, timeout))
                tls = self.context.wrap_socket(raw, server_hostname=parts.hostname)
                path = parts.path or "/"
                if parts.query:
                    path += "?" + parts.query
                connection = http.client.HTTPConnection(parts.hostname, timeout=_remaining(deadline, timeout))
                connection.sock = tls
                connection.request("GET", path, headers={
                    "Host": parts.hostname or "", "Accept": "application/json,text/html,application/pdf",
                    "User-Agent": "trust-stack-official-research/1.0 contact=local-research",
                    "Connection": "close",
                })
                response = connection.getresponse()
                chunks, size = [], 0
                while size <= max_bytes:
                    _check_active(cancel_event, deadline)
                    if connection.sock is not None:
                        connection.sock.settimeout(_remaining(deadline, timeout))
                    chunk = response.read(min(64 * 1024, max_bytes + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                body = b"".join(chunks)
                headers = {key.casefold(): value for key, value in response.getheaders()}
                connection.close()
                return NetworkResponse(response.status, headers, body)
            except OSError as exc:
                last_error = exc
        _check_active(cancel_event, deadline)
        raise OfficialSourceError("missing_retrieval", "official source request failed") from last_error


def _validate_url(url: str, resolver: Callable[[str], list[str]], *,
                  cancel_event: threading.Event | None = None,
                  deadline: float | None = None) -> str:
    _check_active(cancel_event, deadline)
    if not isinstance(url, str) or len(url) > 4000 or "\x00" in url:
        raise OfficialSourceError("unsafe_url", "source URL is invalid")
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.username is not None or parts.password is not None
            or parts.fragment or parts.port not in (None, 443)):
        raise OfficialSourceError("unsafe_url", "only validated HTTPS port 443 official URLs are allowed")
    host = (parts.hostname or "").casefold()
    if host not in {"www.microsoft.com", "data.sec.gov", "www.sec.gov"}:
        raise OfficialSourceError("unsafe_url", "source host is not on the official allowlist")
    lowered = parts.path.casefold()
    decoded = unquote(parts.path)
    if "\\" in decoded or any(piece in {".", ".."} for piece in decoded.split("/")) or re.search(r"%2f|%5c", lowered):
        raise OfficialSourceError("unsafe_url", "encoded separators and path traversal are blocked")
    allowed = False
    if host == "www.microsoft.com":
        allowed = (lowered.rstrip("/") == "/en-us/investor/annual-reports.aspx"
                   or _MS_REPORT_PATH.fullmatch(parts.path) is not None)
    elif host == "data.sec.gov":
        allowed = parts.path == f"/submissions/CIK{MSFT_CIK}.json"
    elif host == "www.sec.gov":
        allowed = _SEC_REPORT_PATH.fullmatch(parts.path) is not None
    if not allowed:
        raise OfficialSourceError("unsafe_url", "source path is outside the official annual-report allowlist")
    _public_addresses(host, resolver, cancel_event=cancel_event, deadline=deadline)
    return url


def _media_type(headers: dict[str, str], allowed: set[str]) -> str:
    value = headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    if value not in allowed:
        raise OfficialSourceError("unsupported_media_type", "official response MIME type is not allowed")
    return value


class OfficialSources:
    """MSFT-scoped discovery/acquisition facade with injectable network transport."""

    def __init__(self, document_service, *, transport=None, resolver=system_resolver,
                 snapshot_addition_callback=None, timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
                 max_document_bytes=MAX_DOCUMENT_BYTES, max_concurrency=2,
                 cancel_event=None, deadline=None, cancellation_lock=None):
        self.documents = document_service
        self.resolver = resolver
        self.transport = transport or PinnedHTTPSHTTPTransport(resolver)
        self.snapshot_addition_callback = snapshot_addition_callback
        self.timeout_seconds = float(timeout_seconds)
        self.max_document_bytes = int(max_document_bytes)
        self.cancel_event = cancel_event
        self.deadline = deadline
        self.cancellation_lock = cancellation_lock or threading.RLock()
        if not 0 < self.timeout_seconds <= 60 or not 1 <= self.max_document_bytes <= MAX_DOCUMENT_BYTES:
            raise ValueError("timeout and document byte budgets are outside safe bounds")
        if type(max_concurrency) is not int or not 1 <= max_concurrency <= 4:
            raise ValueError("max_concurrency must be an integer from 1 through 4")
        self._semaphore = threading.BoundedSemaphore(max_concurrency)
        self._candidates: dict[str, dict[str, Any]] = {}
        self._acquired: dict[str, dict[str, Any]] = {}
        self._state_lock = threading.RLock()
        self._acquisition_locks: dict[str, threading.RLock] = {}

    def _operation_deadline(self) -> float:
        local = time.monotonic() + self.timeout_seconds
        return min(local, self.deadline) if self.deadline is not None else local

    def _check_active(self, deadline: float) -> None:
        _check_active(self.cancel_event, deadline)

    def _acquire_budgeted(self, lock, deadline: float) -> None:
        while True:
            self._check_active(deadline)
            if lock.acquire(timeout=_remaining(deadline, 0.05)):
                return

    def _fetch(self, url: str, *, max_bytes: int, allowed_mime: set[str],
               deadline: float) -> tuple[bytes, str, str]:
        current = url
        self._acquire_budgeted(self._semaphore, deadline)
        try:
            for redirects in range(MAX_REDIRECTS + 1):
                self._check_active(deadline)
                _validate_url(current, self.resolver, cancel_event=self.cancel_event, deadline=deadline)
                response = self.transport.get(
                    current, timeout=_remaining(deadline, self.timeout_seconds), max_bytes=max_bytes,
                    cancel_event=self.cancel_event, deadline=deadline)
                self._check_active(deadline)
                if len(response.body) > max_bytes:
                    raise OfficialSourceError("response_too_large", "official response exceeded the byte budget")
                if response.status in {301, 302, 303, 307, 308}:
                    if redirects == MAX_REDIRECTS:
                        raise OfficialSourceError("too_many_redirects", "official response exceeded the redirect budget")
                    location = response.headers.get("location")
                    if not location:
                        raise OfficialSourceError("missing_retrieval", "official redirect omitted its destination")
                    redirected = urljoin(current, location)
                    if (urlsplit(redirected).hostname or "").casefold() != (urlsplit(current).hostname or "").casefold():
                        raise OfficialSourceError("unsafe_url", "cross-host redirects are blocked")
                    _validate_url(redirected, self.resolver, cancel_event=self.cancel_event, deadline=deadline)
                    current = redirected
                    continue
                if response.status != 200:
                    raise OfficialSourceError("missing_retrieval", f"official source returned HTTP {response.status}")
                return response.body, _media_type(response.headers, allowed_mime), current
        finally:
            self._semaphore.release()
        raise AssertionError("unreachable")

    @staticmethod
    def _years(fiscal_years: Any) -> list[int]:
        if not isinstance(fiscal_years, list) or not 1 <= len(fiscal_years) <= 20:
            raise OfficialSourceError("invalid_arguments", "fiscal_years must contain 1..20 integer years")
        if any(type(year) is not int for year in fiscal_years):
            raise OfficialSourceError("invalid_arguments", "fiscal_years must contain only integers")
        years = list(dict.fromkeys(fiscal_years))
        current = datetime.now(timezone.utc).year
        if any(year < 1986 or year > current for year in years):
            raise OfficialSourceError("invalid_arguments", "fiscal_years cannot predate Microsoft or include a future year")
        return years

    def _remember(self, candidate: dict[str, Any]) -> None:
        identity = json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()
        candidate["candidate_id"] = "official-" + hashlib.sha256(identity).hexdigest()
        with self._state_lock:
            self._candidates[candidate["candidate_id"]] = candidate
            self._acquisition_locks.setdefault(candidate["candidate_id"], threading.RLock())

    def discover_official_reports(self, company_id: str, fiscal_years: list[int]) -> dict[str, Any]:
        if company_id != "MSFT":
            raise OfficialSourceError("unsupported_company", "official-source discovery is currently limited to MSFT")
        deadline = self._operation_deadline()
        self._check_active(deadline)
        years, found, failures = set(self._years(fiscal_years)), [], []
        fetched_at = _now()
        try:
            body, _, final_url = self._fetch(MSFT_INDEX, max_bytes=MAX_DISCOVERY_BYTES,
                                              allowed_mime=_DISCOVERY_MIME, deadline=deadline)
            parser = _Links()
            parser.feed(body.decode("utf-8", "replace"))
            index_hash = hashlib.sha256(body).hexdigest()
            for href, label in parser.links:
                self._check_active(deadline)
                url = urljoin(final_url, href)
                match = re.search(r"/ar(?P<yy>\d{2})/", url, re.I)
                if not match:
                    continue
                year = 2000 + int(match.group("yy"))
                if year not in years:
                    continue
                try:
                    _validate_url(url, self.resolver, cancel_event=self.cancel_event, deadline=deadline)
                except OfficialSourceError as exc:
                    if exc.code in {"cancelled", "deadline_exceeded"}:
                        raise
                    continue
                candidate = {"company_id": "MSFT", "fiscal_year": year, "title": label or f"Microsoft FY{year} Annual Report",
                             "source_url": url, "publication_date": None, "official_source": "microsoft_investor",
                             "discovered_from_url": final_url, "discovered_at": fetched_at,
                             "discovery_sha256": index_hash}
                self._remember(candidate); found.append(candidate)
        except (OfficialSourceError, UnicodeError) as exc:
            if getattr(exc, "code", None) in {"cancelled", "deadline_exceeded"}:
                raise
            failures.append({"source": "microsoft_investor", "error": getattr(exc, "code", "missing_retrieval"),
                             "message": str(exc) if isinstance(exc, OfficialSourceError) else "official index could not be decoded"})
        try:
            body, _, final_url = self._fetch(MSFT_SUBMISSIONS, max_bytes=MAX_DISCOVERY_BYTES,
                                              allowed_mime=_DISCOVERY_MIME, deadline=deadline)
            metadata = json.loads(body)
            if str(metadata.get("cik", "")).zfill(10) != MSFT_CIK or "MSFT" not in metadata.get("tickers", []):
                raise OfficialSourceError("wrong_company", "SEC submissions metadata did not identify Microsoft")
            recent = metadata.get("filings", {}).get("recent", {})
            fields = (recent.get("form", []), recent.get("reportDate", []), recent.get("filingDate", []),
                      recent.get("accessionNumber", []), recent.get("primaryDocument", []))
            metadata_hash = hashlib.sha256(body).hexdigest()
            for form, report_date, filing_date, accession, primary in zip(*fields):
                self._check_active(deadline)
                if form != "10-K" or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(report_date)):
                    continue
                year = int(report_date[:4])
                if year not in years or not re.fullmatch(r"\d{10}-\d{2}-\d{6}", str(accession)):
                    continue
                if not re.fullmatch(r"[A-Za-z0-9._-]+", str(primary)):
                    continue
                compact = accession.replace("-", "")
                url = f"https://www.sec.gov/Archives/edgar/data/789019/{compact}/{primary}"
                _validate_url(url, self.resolver, cancel_event=self.cancel_event, deadline=deadline)
                candidate = {"company_id": "MSFT", "fiscal_year": year, "title": f"Microsoft FY{year} Form 10-K",
                             "source_url": url, "publication_date": filing_date, "official_source": "sec_edgar",
                             "discovered_from_url": final_url, "discovered_at": fetched_at,
                             "discovery_sha256": metadata_hash, "accession_number": accession}
                self._remember(candidate); found.append(candidate)
        except (OfficialSourceError, json.JSONDecodeError, TypeError, ValueError) as exc:
            if getattr(exc, "code", None) in {"cancelled", "deadline_exceeded"}:
                raise
            failures.append({"source": "sec_edgar", "error": getattr(exc, "code", "missing_retrieval"),
                             "message": str(exc) if isinstance(exc, OfficialSourceError) else "official index metadata could not be decoded"})
        self._check_active(deadline)
        unique = {item["candidate_id"]: item for item in found}
        candidates = sorted(unique.values(), key=lambda item: (item["fiscal_year"], item["official_source"]), reverse=True)
        if not candidates:
            reason = "; ".join(f"{item['source']}: {item['message']}" for item in failures)
            raise OfficialSourceError("missing_retrieval", "official indexes provided no matching report candidate; this is not evidence of issuer non-disclosure" + (f". Retrieval failures: {reason}" if reason else ""))
        covered = {item["fiscal_year"] for item in candidates}
        missing = sorted(years - covered)
        return {"company_id": company_id, "requested_fiscal_years": sorted(years), "candidates": candidates,
                "missing_fiscal_years": missing,
                "coverage_status": "complete" if not missing else "partial_missing_retrieval",
                "source_failures": failures, "coverage_note": "Missing retrieval is not evidence of issuer non-disclosure."}

    def acquire_official_report(self, candidate_id: str) -> dict[str, Any]:
        deadline = self._operation_deadline()
        self._check_active(deadline)
        with self._state_lock:
            candidate = self._candidates.get(candidate_id) if isinstance(candidate_id, str) else None
            acquisition_lock = self._acquisition_locks.get(candidate_id) if candidate is not None else None
        if candidate is None or acquisition_lock is None:
            raise OfficialSourceError("unknown_candidate", "candidate_id was not produced by this authorized discovery session")
        self._acquire_budgeted(acquisition_lock, deadline)
        try:
            self._check_active(deadline)
            with self._state_lock:
                acquired = self._acquired.get(candidate_id)
            if acquired is not None:
                return acquired
            if candidate["company_id"] != "MSFT":
                raise OfficialSourceError("wrong_company", "candidate does not belong to the selected company")
            raw, media_type, final_url = self._fetch(
                candidate["source_url"], max_bytes=self.max_document_bytes,
                allowed_mime=_ALLOWED_MIME, deadline=deadline)
            self._check_active(deadline)
            acquired_at, digest = _now(), hashlib.sha256(raw).hexdigest()
            suffix = ".pdf" if media_type == "application/pdf" else ".html"
            temporary: str | None = None
            try:
                fd, temporary = tempfile.mkstemp(prefix="official-report-", suffix=suffix)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(raw)
                # This is the cancellation boundary: registration and grant win together,
                # or a cancellation that owns the shared lock prevents both.
                with self.cancellation_lock:
                    self._check_active(deadline)
                    record = self.documents.register_file(
                        Path(temporary), company_id="MSFT",
                        source_key=f"official:{candidate['official_source']}:fy{candidate['fiscal_year']}",
                        title=candidate["title"], url=final_url, published_at=candidate["publication_date"],
                        media_type=media_type, rights_status="public", collected_at=acquired_at,
                        expected_sha256=digest, expected_byte_length=len(raw),
                    )
                    provenance = {"company_id": "MSFT", "fiscal_year": candidate["fiscal_year"],
                                  "source_url": final_url, "publication_date": candidate["publication_date"],
                                  "acquired_at": acquired_at, "source_sha256": digest, "byte_length": len(raw),
                                  "official_source": candidate["official_source"],
                                  "discovered_from_url": candidate["discovered_from_url"],
                                  "discovered_at": candidate["discovered_at"],
                                  "discovery_sha256": candidate["discovery_sha256"]}
                    event = {"type": "data_snapshot_addition", "company_id": "MSFT",
                             "document_version_ids": [record["document_version_id"]],
                             "document": record, "source": provenance}
                    callback_result = (self.snapshot_addition_callback(event)
                                       if self.snapshot_addition_callback else None)
            finally:
                if temporary is not None:
                    try:
                        os.unlink(temporary)
                    except FileNotFoundError:
                        pass
            result = {"candidate_id": candidate_id, "document": record, "provenance": provenance,
                      "snapshot_addition": event, "snapshot_callback_result": callback_result,
                      "requires_new_research_snapshot": self.snapshot_addition_callback is None}
            with self._state_lock:
                self._acquired[candidate_id] = result
            return result
        finally:
            acquisition_lock.release()

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {"name": "discover_official_reports", "description": "Discover MSFT annual reports from fetched Microsoft Investor and SEC submissions indexes; missing retrieval is not non-disclosure.",
             "parameters": {"type": "object", "additionalProperties": False,
                            "properties": {"company_id": {"const": "MSFT"}, "fiscal_years": {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "integer", "minimum": 1986}}},
                            "required": ["company_id", "fiscal_years"]}, "handler": self.discover_official_reports},
            {"name": "acquire_official_report", "description": "Fetch and immutably archive one candidate returned by this discovery session.",
             "parameters": {"type": "object", "additionalProperties": False,
                            "properties": {"candidate_id": {"type": "string", "pattern": "^official-[0-9a-f]{64}$"}}, "required": ["candidate_id"]},
             "handler": self.acquire_official_report},
        ]
