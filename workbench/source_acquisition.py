"""Bounded acquisition of configured Big Tech annual reports.

This module is an administrator-configured data-acquisition layer, not a general
research network tool.  Callers select only a company and fiscal year.  Every
network URL must either be an exact issuer-hosted catalog URL or be returned by
an exact configured issuer feed and match that issuer's configured CDN prefix.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urljoin, urlsplit

from workbench.documents import DocumentService
from workbench.source_archive import SourceArchive, normalize_source_config


MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
MAX_FEED_BYTES = 2 * 1024 * 1024
_SUPPORTED_COMPANIES = frozenset({"AAPL", "MSFT", "GOOG", "AMZN", "META", "NVDA", "TSLA", "NFLX"})
_ALIASES = {
    "AAPL": r"\bapple(?:\s+computer)?(?:\s+inc\.?)?\b", "MSFT": r"\bmicrosoft\b",
    "GOOG": r"\b(?:alphabet|google)\b", "AMZN": r"\bamazon\b",
    "META": r"\b(?:meta platforms|facebook)\b", "NVDA": r"\bnvidia\b",
    "TSLA": r"\btesla\b", "NFLX": r"\bnetflix\b",
}
_GLOBAL_LOCKS: dict[tuple[str, str, int], threading.Lock] = {}
_GLOBAL_HOST_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_GLOBAL_FEED_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_GLOBAL_LOCKS_GUARD = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class AcquisitionError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message

    def as_dict(self) -> dict[str, str]:
        return {"error": self.code, "message": self.message}


class _VisibleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag.casefold() in {"script", "style", "noscript"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag.casefold() in {"script", "style", "noscript"}:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


class SystemCurlTransport:
    """Exact-URL HTTPS transport using the deployment's verified system proxy."""

    def get(self, url: str, *, max_bytes: int) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="public-acquisition-") as temporary:
            output = Path(temporary) / "body"
            command = [
                "/usr/bin/curl", "--disable", "--proto", "=https", "--proto-redir", "=https",
                "--max-redirs", "0", "--connect-timeout", "15", "--max-time", "60",
                "--max-filesize", str(max_bytes), "--fail", "--show-error", "--silent",
                "--output", str(output), "--write-out", "%{json}", url,
            ]
            try:
                completed = subprocess.run(command, capture_output=True, text=True, timeout=70)
                metadata = json.loads(completed.stdout or "{}")
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                raise AcquisitionError("network_failure", "system curl request failed") from exc
            body = output.read_bytes() if output.is_file() else b""
            return {
                "body": body, "curl_exit": completed.returncode,
                "http_status": metadata.get("http_code"),
                "tls_verify_result": metadata.get("ssl_verify_result"),
                "final_url": metadata.get("url_effective"),
                "content_type": metadata.get("content_type"),
                "size_download": metadata.get("size_download"),
                "transport": "system_curl_tls_verified",
            }


class PublicSourceAcquisition:
    def __init__(self, documents_root, *, config_path=None, transport=None, clock=None, sleep=None,
                 object_store=None):
        self.root = Path(documents_root)
        self.root.mkdir(parents=True, exist_ok=True)
        default = Path(__file__).resolve().parents[1] / "config" / "public-source-acquisition.json"
        self.config_path = Path(config_path) if config_path is not None else default
        self.transport = transport or SystemCurlTransport()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleep = sleep or time.sleep
        self.documents = DocumentService(self.root, object_store=object_store)
        self.archive = SourceArchive(self.root, object_store=object_store)
        self.state = self.root / "public-source-acquisition"
        for name in ("receipts", "feeds", "locks", "cooldowns", "host-requests", "quarantine"):
            (self.state / name).mkdir(parents=True, exist_ok=True)
        self.config, self.catalog = self._load_configuration()

    def _load_configuration(self) -> tuple[dict[str, Any], dict[tuple[str, int], dict[str, Any]]]:
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise AcquisitionError("invalid_config", "acquisition configuration cannot be read") from exc
        if not isinstance(config, dict) or config.get("version") != 1:
            raise AcquisitionError("invalid_config", "acquisition configuration version must be 1")
        companies = config.get("companies")
        if not isinstance(companies, dict) or set(companies) != _SUPPORTED_COMPANIES:
            raise AcquisitionError("invalid_config", "configuration must contain exactly the eight supported companies")
        for company, item in companies.items():
            if not isinstance(item, dict):
                raise AcquisitionError("invalid_config", f"configuration for {company} must be an object")
            prefixes = item.get("official_prefixes")
            if not isinstance(prefixes, list) or not prefixes:
                raise AcquisitionError("invalid_config", f"official_prefixes for {company} must be non-empty")
            for prefix in prefixes:
                self._require_https(prefix, prefix=True)
            feed = item.get("feed_url")
            if feed is not None:
                self._require_https(feed)
        minimum = config.get("min_text_chars", 10_000)
        if type(minimum) is not int or not 100 <= minimum <= 100_000:
            raise AcquisitionError("invalid_config", "min_text_chars is outside 100..100000")
        config["min_text_chars"] = minimum
        bounds = {
            "feed_cache_seconds": (1, 86_400, 3_600),
            "cooldown_seconds": (1, 86_400, 900),
            "host_interval_seconds": (3, 60, 3),
        }
        for name, (lower, upper, default_value) in bounds.items():
            value = config.get(name, default_value)
            if type(value) is not int or not lower <= value <= upper:
                raise AcquisitionError("invalid_config", f"{name} is outside {lower}..{upper}")
            config[name] = value
        catalog_value = config.get("catalog_path", "public-sources-bigtech-annual.json")
        if not isinstance(catalog_value, str) or not catalog_value:
            raise AcquisitionError("invalid_config", "catalog_path must be a non-empty relative path")
        catalog_path = Path(catalog_value)
        if not catalog_path.is_absolute():
            catalog_path = self.config_path.parent / catalog_path
        try:
            sources = json.loads(catalog_path.read_text(encoding="utf-8"))["sources"]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise AcquisitionError("invalid_config", "annual-report catalog cannot be read") from exc
        catalog: dict[tuple[str, int], dict[str, Any]] = {}
        if not isinstance(sources, list):
            raise AcquisitionError("invalid_config", "annual-report catalog sources must be a list")
        for source in sources:
            if not isinstance(source, dict):
                raise AcquisitionError("invalid_config", "annual-report catalog contains a non-object")
            company, year = source.get("company_id"), source.get("fiscal_year")
            if company not in _SUPPORTED_COMPANIES or type(year) is not int:
                raise AcquisitionError("invalid_config", "annual-report catalog has an unsupported subject")
            key = (company, year)
            if key in catalog:
                raise AcquisitionError("invalid_config", "annual-report catalog has a duplicate company-year")
            catalog[key] = source
        return config, catalog

    @staticmethod
    def _require_https(url: Any, *, prefix: bool = False) -> str:
        if not isinstance(url, str) or len(url) > 4000:
            raise AcquisitionError("invalid_config", "configured URL is invalid")
        parts = urlsplit(url)
        try:
            port = parts.port
        except ValueError as exc:
            raise AcquisitionError("invalid_config", "configured URL has an invalid port") from exc
        if (parts.scheme != "https" or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.fragment or port not in (None, 443)):
            raise AcquisitionError("invalid_config", "configured URLs must be exact HTTPS port 443 URLs")
        if prefix and not url.endswith("/"):
            raise AcquisitionError("invalid_config", "official URL prefixes must end with a slash")
        return url

    @contextlib.contextmanager
    def _annual_lock(self, company: str, year: int):
        key = (str(self.root.resolve()), company, year)
        with _GLOBAL_LOCKS_GUARD:
            lock = _GLOBAL_LOCKS.setdefault(key, threading.Lock())
        with lock:
            path = self.state / "locks" / f"{company}-{year}.lock"
            with path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _receipt(self, payload: dict[str, Any]) -> None:
        payload = {"recorded_at": _now(), **payload}
        name = f"{time.time_ns()}-{os.getpid()}-{threading.get_ident()}.json"
        target = self.state / "receipts" / name
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)

    @staticmethod
    def _host_token(host: str) -> str:
        return hashlib.sha256(host.encode("ascii")).hexdigest()

    def _host_state(self, directory: str, host: str) -> Path:
        return self.state / directory / f"{self._host_token(host)}.json"

    def _read_host_timestamp(self, directory: str, host: str, field: str) -> datetime | None:
        path = self._host_state(directory, host)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or value.get("host") != host
                    or not isinstance(value.get(field), str)):
                raise ValueError
            timestamp = datetime.fromisoformat(value[field].replace("Z", "+00:00"))
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise ValueError
            return timestamp
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise AcquisitionError("state_tampered", "host timing state is invalid") from exc

    def _write_host_timestamp(self, directory: str, host: str, field: str, value: datetime) -> None:
        target = self._host_state(directory, host)
        encoded = json.dumps(
            {"host": host, field: value.isoformat().replace("+00:00", "Z")}, sort_keys=True) + "\n"
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=target.parent, prefix=target.name + ".", delete=False) as handle:
            handle.write(encoded)
            temporary = Path(handle.name)
        os.replace(temporary, target)

    def _check_cooldown(self, host: str) -> None:
        until = self._read_host_timestamp("cooldowns", host, "until")
        if until is not None and self.clock() < until:
            raise AcquisitionError("host_cooldown", f"official host {host} is cooling down after HTTP 429")

    def _set_cooldown(self, host: str) -> None:
        until = self.clock() + timedelta(seconds=self.config["cooldown_seconds"])
        self._write_host_timestamp("cooldowns", host, "until", until)

    def _mark_host_request(self, host: str) -> None:
        previous = self._read_host_timestamp("host-requests", host, "started_at")
        now = self.clock()
        if previous is not None:
            remaining = self.config["host_interval_seconds"] - (now - previous).total_seconds()
            if remaining > 0:
                self.sleep(remaining)
                now = self.clock()
        self._write_host_timestamp("host-requests", host, "started_at", now)

    @staticmethod
    def _strict_url(url: str) -> str:
        try:
            normalized = normalize_source_config({
                "source_id": "public-acquisition-url-check", "title": "URL check",
                "company_id": None, "url": url, "allowed_urls": [url],
                "media_types": ["text/html"], "max_bytes": 1, "timeout_seconds": 1,
            })
        except ValueError as exc:
            raise AcquisitionError("unsafe_url", "network URL failed strict archive normalization") from exc
        return normalized["url"]

    def _network(self, url: str, *, max_bytes: int, purpose: str) -> tuple[bytes, str, dict[str, Any]]:
        url = self._strict_url(url)
        host = urlsplit(url).hostname or ""
        key = (str(self.root.resolve()), host)
        with _GLOBAL_LOCKS_GUARD:
            lock = _GLOBAL_HOST_LOCKS.setdefault(key, threading.Lock())
        with lock:
            lock_path = self.state / "locks" / f"host-{self._host_token(host)}.lock"
            with lock_path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    self._check_cooldown(host)
                    self._mark_host_request(host)
                    return self._network_unlocked(
                        url, host=host, max_bytes=max_bytes, purpose=purpose)
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _network_unlocked(self, url: str, *, host: str, max_bytes: int,
                          purpose: str) -> tuple[bytes, str, dict[str, Any]]:
        started = _now()
        try:
            response = self.transport.get(url, max_bytes=max_bytes)
        except AcquisitionError as exc:
            self._receipt({"status": "failed", "purpose": purpose, "url": url,
                           "started_at": started, "error_code": exc.code, "error_message": exc.message})
            raise
        except Exception as exc:
            self._receipt({"status": "failed", "purpose": purpose, "url": url,
                           "started_at": started, "error_code": "network_failure"})
            raise AcquisitionError("network_failure", "official source request failed") from exc
        if not isinstance(response, dict):
            raise AcquisitionError("network_failure", "transport returned invalid response metadata")
        status = response.get("http_status")
        if status == 429:
            self._set_cooldown(host)
            self._receipt({"status": "failed", "purpose": purpose, "url": url,
                           "started_at": started, "http_status": 429, "error_code": "rate_limited"})
            raise AcquisitionError("rate_limited", f"official host {host} returned HTTP 429")
        body = response.get("body")
        media = str(response.get("content_type") or "").split(";", 1)[0].strip().casefold()
        valid = (status == 200 and response.get("tls_verify_result") == 0
                 and response.get("final_url") == url and response.get("curl_exit", 0) == 0
                 and isinstance(body, bytes) and 0 < len(body) <= max_bytes
                 and response.get("size_download") in (None, len(body)))
        if not valid:
            self._receipt({"status": "failed", "purpose": purpose, "url": url,
                           "started_at": started, "http_status": status,
                           "final_url": response.get("final_url"), "error_code": "invalid_response"})
            raise AcquisitionError("invalid_response", "official source response failed exact URL, HTTPS 200, TLS, or length checks")
        metadata = {
            "method": "system_curl_tls_verified", "url": url, "final_url": url,
            "http_status": 200, "tls_verify_result": 0, "media_type": media,
            "byte_length": len(body), "sha256": hashlib.sha256(body).hexdigest(),
            "fetched_at": _now(),
        }
        self._receipt({"status": "downloaded", "purpose": purpose, "started_at": started, **metadata})
        return body, media, metadata

    def _source_config(self, source: dict[str, Any], *, url: str | None = None, title: str | None = None) -> dict[str, Any]:
        chosen_url = url or source.get("url")
        value = {
            "source_id": source.get("source_id"), "company_id": source.get("company_id"),
            "title": title or source.get("title"), "url": chosen_url, "allowed_urls": [chosen_url],
            "media_types": ["application/pdf", "text/html"], "max_bytes": MAX_DOCUMENT_BYTES,
            "timeout_seconds": 60, "published_at": source.get("published_at"),
            "rights_status": source.get("rights_status", "review_required"),
            "fiscal_year": source.get("fiscal_year"),
            "source_class": source.get("source_class"),
            "discovered_from": source.get("discovered_from"),
        }
        try:
            return normalize_source_config(value)
        except ValueError as exc:
            raise AcquisitionError("invalid_config", "annual-report source configuration is invalid") from exc

    def _verified_document(self, company: str, item: dict[str, Any], year: int,
                           title: str) -> dict[str, Any]:
        raw = self.documents.read_raw_version(
            company_id=company, document_version_id=item["document_version_id"])
        self._validate_content(raw, item["media_type"], company, year, title)
        if item.get("parse_status") != "parsed":
            raise AcquisitionError("parse_failed", "annual report document is not successfully parsed")
        self.documents.read_frozen_version(
            company_id=company, document_version_id=item["document_version_id"])
        connection = self.documents._connect()
        try:
            row = connection.execute(
                "SELECT * FROM document_versions WHERE company_id=? AND document_version_id=?",
                (company, item["document_version_id"]),
            ).fetchone()
            if row is None:
                raise AcquisitionError("local_validation_failed", "verified document record disappeared")
            return self.documents._record(row)
        finally:
            connection.close()

    def _document_with_sha(self, company: str, digest: str, year: int,
                           title: str) -> dict[str, Any] | None:
        coverage = self.documents.get_document_coverage(company_id=company)
        matches = [item for item in coverage["documents"] if item["source_sha256"] == digest]
        if not matches:
            return None
        return self._verified_document(company, matches[-1], year, title)

    def _existing(self, company: str, source: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
        source_id = source.get("source_id")
        history = self.archive.history(source_id)
        if history["versions"]:
            latest = history["versions"][-1]
            try:
                raw = self.archive.read(source_id, source_version=latest["source_version"])
                self._validate_content(raw, latest["media_type"], company, source["fiscal_year"], source.get("title", ""))
                document = self._document_with_sha(
                    company, latest["source_sha256"], source["fiscal_year"], source.get("title", ""))
                if document is None:
                    document = self.archive.enroll(
                        source_id, source_version=latest["source_version"], document_service=self.documents)
                    if document.get("parse_status") != "parsed":
                        raise AcquisitionError("parse_failed", "archived annual report could not be parsed")
            except ValueError as exc:
                code = getattr(exc, "code", "local_validation_failed")
                raise AcquisitionError(code, f"existing archived annual report was rejected: {getattr(exc, 'message', str(exc))}") from exc
            return document, "local_archive"
        try:
            coverage = self.documents.get_document_coverage(company_id=company)
        except ValueError as exc:
            raise AcquisitionError(getattr(exc, "code", "local_validation_failed"),
                                   "existing document storage failed integrity checks") from exc
        matches = [item for item in coverage["documents"] if item["source_key"] == source_id]
        if not matches:
            return None
        try:
            document = self._verified_document(
                company, matches[-1], source["fiscal_year"], source.get("title", ""))
        except ValueError as exc:
            raise AcquisitionError(getattr(exc, "code", "local_validation_failed"),
                                   "existing annual-report document was rejected") from exc
        return document, "local_document"

    def _official_url(self, company: str, url: str) -> bool:
        try:
            safe = self._strict_url(url)
        except AcquisitionError:
            return False
        return any(safe.startswith(prefix) for prefix in self.config["companies"][company]["official_prefixes"])

    def _feed_candidates(self, value: Any, inherited_year: int | None = None):
        if isinstance(value, dict):
            report_title = value.get("ReportTitle")
            report_year = value.get("ReportYear")
            title_years = ([int(item) for item in re.findall(r"(?<!\d)(20\d{2})(?!\d)", report_title)]
                           if isinstance(report_title, str) else [])
            context_year = title_years[0] if title_years else (
                report_year if type(report_year) is int else inherited_year)
            documents = value.get("Documents")
            if isinstance(documents, list):
                for document in documents:
                    if not isinstance(document, dict):
                        continue
                    document_title = document.get("DocumentTitle")
                    # Q4 feeds may label an actual 10-K simply "PDF"; the
                    # issuer's structured category supplies the document type.
                    category = str(document.get("DocumentCategory", "")).casefold()
                    if (isinstance(document_title, str) and category in {"tenk", "10-k"}
                            and not re.search(r"\b10-k\b", document_title, re.I)):
                        document_title += " Form 10-K"
                    document_url = document.get("DocumentPath")
                    if isinstance(document_title, str) and isinstance(document_url, str):
                        document_years = [int(item) for item in re.findall(
                            r"(?<!\d)(20\d{2})(?!\d)", document_title)]
                        candidate_year = document_years[0] if document_years else context_year
                        yield document_title, document_url, candidate_year
            title = next((v for k, v in value.items() if isinstance(v, str) and k.casefold() in
                          {"title", "documenttitle", "reporttitle", "name"}), None)
            url = next((v for k, v in value.items() if isinstance(v, str) and k.casefold() in
                        {"url", "documenturl", "documentpath", "fileurl", "downloadurl", "link"}), None)
            if title and url:
                years = [int(item) for item in re.findall(r"(?<!\d)(20\d{2})(?!\d)", title)]
                yield title, url, years[0] if years else context_year
            for child in value.values():
                yield from self._feed_candidates(child, context_year)
        elif isinstance(value, list):
            for child in value:
                yield from self._feed_candidates(child, inherited_year)

    def _feed_body(self, company: str, feed_url: str) -> bytes:
        key = (str(self.root.resolve()), company)
        with _GLOBAL_LOCKS_GUARD:
            lock = _GLOBAL_FEED_LOCKS.setdefault(key, threading.Lock())
        with lock:
            lock_path = self.state / "locks" / f"feed-{company}.lock"
            with lock_path.open("a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    return self._feed_body_unlocked(company, feed_url)
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _feed_body_unlocked(self, company: str, feed_url: str) -> bytes:
        body_path = self.state / "feeds" / f"{company}.json"
        meta_path = self.state / "feeds" / f"{company}.meta.json"
        ttl = int(self.config.get("feed_cache_seconds", 3600))
        if body_path.exists() or meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                body = body_path.read_bytes()
                fetched = datetime.fromisoformat(meta["fetched_at"].replace("Z", "+00:00"))
                if (hashlib.sha256(body).hexdigest() != meta["sha256"] or meta["url"] != feed_url):
                    raise ValueError
                if self.clock() - fetched <= timedelta(seconds=ttl):
                    return body
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise AcquisitionError("feed_cache_tampered", "official feed cache failed integrity checks") from exc
        body, media, metadata = self._network(feed_url, max_bytes=MAX_FEED_BYTES, purpose="official_feed")
        if media not in {"application/json", "text/json", "text/plain"}:
            raise AcquisitionError("invalid_feed", "official feed did not return JSON media")
        try:
            json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AcquisitionError("invalid_feed", "official feed returned invalid JSON") from exc
        temporary = body_path.with_suffix(".tmp")
        temporary.write_bytes(body)
        os.replace(temporary, body_path)
        meta_temporary = meta_path.with_suffix(".tmp")
        meta_temporary.write_text(json.dumps({
            "url": feed_url, "fetched_at": metadata["fetched_at"],
            "sha256": metadata["sha256"]}, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(meta_temporary, meta_path)
        return body

    def _discover(self, company: str, year: int, source: dict[str, Any] | None) -> dict[str, Any]:
        company_config = self.config["companies"][company]
        feed_url = company_config.get("feed_url")
        if feed_url is None:
            raise AcquisitionError("period_not_configured", f"no official annual report source is configured for {company} FY{year}")
        body = self._feed_body(company, feed_url)
        payload = json.loads(body)
        accepted = []
        for title, candidate, candidate_year in self._feed_candidates(payload):
            flat_title = re.sub(r"\s+", " ", title).strip()
            low = flat_title.casefold()
            if "proxy" in low or not re.search(r"\b(?:form\s+)?10-k\b|\bannual report\b", low):
                continue
            if candidate_year != year:
                continue
            url = urljoin(feed_url, candidate)
            if self._official_url(company, url):
                accepted.append((f"{year} {flat_title}", url))
        unique = list(dict.fromkeys(accepted))
        ten_k = [item for item in unique if re.search(r"\b(?:form\s+)?10-k\b", item[0], re.I)]
        if len(ten_k) == 1:
            unique = ten_k
        if len(unique) != 1:
            code = "period_not_found" if not unique else "ambiguous_period"
            raise AcquisitionError(code, f"official feed did not identify exactly one {company} FY{year} annual report")
        title, url = unique[0]
        base = source or {
            "company_id": company, "fiscal_year": year,
            "source_id": f"bigtech-{company.casefold()}-fy{year}-annual",
            "title": f"{company} FY{year} Annual Report / Form 10-K",
            "rights_status": "review_required", "published_at": None,
        }
        return {**base, "url": url, "title": title, "source_class": "issuer_hosted", "discovered_from": feed_url}

    def _persisted_source(self, company: str, year: int) -> dict[str, Any] | None:
        source_id = f"bigtech-{company.casefold()}-fy{year}-annual"
        history = self.archive.history(source_id)
        if not history["versions"]:
            return None
        config = history["versions"][-1].get("config")
        valid = (isinstance(config, dict) and config.get("source_id") == source_id
                 and config.get("company_id") == company and config.get("fiscal_year") == year
                 and config.get("source_class") == "issuer_hosted"
                 and isinstance(config.get("discovered_from"), str)
                 and self._official_url(company, config.get("url", "")))
        if not valid:
            raise AcquisitionError("state_tampered", "persisted discovered source identity is invalid")
        return config

    def _network_source(self, company: str, year: int, source: dict[str, Any] | None) -> dict[str, Any]:
        if source is not None and source.get("source_class") == "issuer_hosted":
            if not self._official_url(company, source.get("url", "")):
                raise AcquisitionError("unapproved_source", "issuer-hosted catalog URL is outside configured official prefixes")
            return source
        return self._discover(company, year, source)

    def _quarantine(self, raw: bytes, *, url: str, media: str, error: AcquisitionError) -> str:
        digest = hashlib.sha256(raw).hexdigest()
        target = self.state / "quarantine" / f"{digest}.raw"
        if target.exists():
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise AcquisitionError("state_tampered", "quarantine object failed its hash check")
        else:
            with tempfile.NamedTemporaryFile(
                    dir=target.parent, prefix=digest + ".", delete=False) as handle:
                handle.write(raw)
                temporary = Path(handle.name)
            os.replace(temporary, target)
        metadata = self.state / "quarantine" / f"{digest}.json"
        encoded = json.dumps({
            "sha256": digest, "byte_length": len(raw), "url": url, "media_type": media,
            "error_code": error.code, "error_message": error.message, "recorded_at": _now(),
        }, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=metadata.parent,
                prefix=digest + ".meta.", delete=False) as handle:
            handle.write(encoded)
            temporary_meta = Path(handle.name)
        os.replace(temporary_meta, metadata)
        return digest

    def _validate_content(self, raw: bytes, media: str, company: str, year: int, title: str) -> dict[str, Any]:
        low_title = title.casefold()
        if "proxy" in low_title or not re.search(r"\b(?:form\s+)?10-k\b|\bannual report\b", low_title):
            raise AcquisitionError("not_annual_report", "source title is not an Annual Report or Form 10-K")
        if media == "application/pdf":
            if not raw.startswith(b"%PDF-"):
                raise AcquisitionError("invalid_content", "downloaded bytes do not have a PDF signature")
            with tempfile.TemporaryDirectory(prefix="annual-validation-") as temporary:
                pdf, text_path = Path(temporary) / "report.pdf", Path(temporary) / "report.txt"
                pdf.write_bytes(raw)
                try:
                    completed = subprocess.run(["/usr/bin/pdftotext", "-layout", str(pdf), str(text_path)],
                                               capture_output=True, timeout=60)
                    if completed.returncode:
                        raise AcquisitionError("content_unreadable", "PDF annual report text extraction failed")
                    text = text_path.read_text(encoding="utf-8", errors="replace")
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise AcquisitionError("content_unreadable", "PDF annual report text extraction failed") from exc
        elif media == "text/html":
            try:
                parser = _VisibleText()
                parser.feed(raw.decode("utf-8-sig"))
                text = " ".join(parser.parts)
            except (UnicodeDecodeError, ValueError) as exc:
                raise AcquisitionError("invalid_content", "HTML annual report is not valid UTF-8") from exc
        else:
            raise AcquisitionError("unsupported_media_type", "annual report must be PDF or HTML")
        flat = re.sub(r"\s+", " ", text).strip()
        low = flat.casefold()
        cover = low[:20_000]
        annual_cover = re.search(
            r"\bform\s+10-k\b|\bannual report pursuant to section\b|\b20\d{2}\s+annual report\b|\bannual report\s+20\d{2}\b",
            cover[:8_000])
        proxy_cover = re.search(r"\bschedule\s+14a\b|\bdef(?:initive)?\s*14a\b", cover[:8_000])
        if proxy_cover is not None and (
                annual_cover is None or proxy_cover.start() < annual_cover.start()):
            raise AcquisitionError("proxy_rejected", "proxy statements are not annual-report originals")

        alias = _ALIASES[company]
        registrant_identity = re.search(
            alias + r".{0,300}\bexact name of (?:the )?registrant\b", cover, re.I) is not None
        title_heading = re.search(
            r"\b(?:20\d{2}\s+)?annual report(?:\s+20\d{2})?\b", cover[:3_000], re.I)
        title_identity = False
        if title_heading is not None:
            title_neighborhood = cover[
                max(0, title_heading.start() - 80):title_heading.end() + 80]
            title_identity = re.search(alias, title_neighborhood, re.I) is not None

        period_patterns = (
            r"\bfor the fiscal year ended\b.{0,80}?(20\d{2})\b",
            r"\byear ended\b.{0,80}?(20\d{2})\b",
            r"\b(20\d{2})\s+annual report\b",
            r"\bannual report\s+(20\d{2})\b",
        )
        periods = []
        for pattern in period_patterns:
            match = re.search(pattern, low[:120_000], re.I)
            if match is not None:
                periods.append((match.start(), int(match.group(1))))
        main_year = min(periods)[1] if periods else None
        checks = {
            "substantial_text": len(flat) >= self.config["min_text_chars"],
            "company_identity": registrant_identity or title_identity,
            "balance_sheet": re.search(r"balance sheets?", low) is not None,
            "cash_flows": re.search(r"cash flows?", low) is not None,
            "income_statement": re.search(r"statements? of (?:consolidated )?(?:income|operations|earnings)|income statements?", low) is not None,
            "fiscal_year": main_year == year,
        }
        failed = [name for name, passed in checks.items() if not passed]
        if failed:
            raise AcquisitionError("content_validation_failed", "annual-report content failed checks: " + ", ".join(failed))
        return {"text_length": len(flat), "main_fiscal_year": main_year, "checks": checks}

    def ensure_annual(self, *, company_id: str, fiscal_year: int) -> dict[str, Any]:
        try:
            return self._ensure_annual(company_id=company_id, fiscal_year=fiscal_year)
        except AcquisitionError as exc:
            self._receipt({
                "status": "failed", "purpose": "ensure_annual", "company_id": company_id,
                "fiscal_year": fiscal_year, "error_code": exc.code, "error_message": exc.message,
            })
            raise

    def _ensure_annual(self, *, company_id: str, fiscal_year: int) -> dict[str, Any]:
        if company_id not in _SUPPORTED_COMPANIES:
            raise AcquisitionError("unsupported_company", "company_id must be one of the eight configured Big Tech companies")
        if type(fiscal_year) is not int or not 1990 <= fiscal_year <= 2100:
            raise AcquisitionError("invalid_fiscal_year", "fiscal_year must be an integer from 1990 through 2100")
        with self._annual_lock(company_id, fiscal_year):
            source = self.catalog.get((company_id, fiscal_year))
            if source is None:
                source = self._persisted_source(company_id, fiscal_year)
            if source is not None:
                existing = self._existing(company_id, source)
                if existing is not None:
                    document, method = existing
                    self._receipt({
                        "status": "available", "purpose": "annual_report_local", "company_id": company_id,
                        "fiscal_year": fiscal_year, "method": method, "source_id": source["source_id"],
                        "source_sha256": document["source_sha256"],
                        "document_version_id": document["document_version_id"],
                    })
                    return {"status": "available", "document": document,
                            "acquisition": {"method": method, "source_id": source["source_id"],
                                            "source_sha256": document["source_sha256"]}}
            network_source = self._network_source(company_id, fiscal_year, source)
            url = network_source["url"]
            if not self._official_url(company_id, url):
                raise AcquisitionError("unapproved_source", "annual-report URL is outside configured official prefixes")
            body, media, metadata = self._network(url, max_bytes=MAX_DOCUMENT_BYTES, purpose="annual_report")
            try:
                if media not in {"application/pdf", "text/html"}:
                    raise AcquisitionError("unsupported_media_type", "official annual report did not return PDF or HTML")
                validation = self._validate_content(
                    body, media, company_id, fiscal_year, network_source["title"])
            except AcquisitionError as exc:
                quarantine_sha = self._quarantine(body, url=url, media=media, error=exc)
                self._receipt({"status": "failed", "purpose": "annual_report_validation", "url": url,
                               "source_sha256": metadata["sha256"], "quarantine_sha256": quarantine_sha,
                               "error_code": exc.code, "error_message": exc.message})
                raise
            cfg = self._source_config(network_source)
            with tempfile.NamedTemporaryFile(dir=self.state, prefix="annual-", delete=False) as handle:
                handle.write(body)
                temporary = Path(handle.name)
            try:
                archived = self.archive.import_local(
                    cfg, temporary, expected_sha256=metadata["sha256"], expected_size=len(body),
                    media_type=media, fetched_at=metadata["fetched_at"])
                document = self._document_with_sha(
                    company_id, archived["source_sha256"], fiscal_year, network_source["title"])
                reused_sha = document is not None
                if document is None:
                    document = self.archive.enroll(
                        cfg["source_id"], source_version=archived["source_version"],
                        document_service=self.documents)
                if document.get("parse_status") != "parsed":
                    raise AcquisitionError("parse_failed", "annual report parsing failed")
            except ValueError as exc:
                raise AcquisitionError(getattr(exc, "code", "archive_failed"),
                                       getattr(exc, "message", "annual report could not be archived")) from exc
            finally:
                temporary.unlink(missing_ok=True)
            self._receipt({"status": "available", "purpose": "annual_report", "url": url,
                           "source_id": cfg["source_id"], "source_sha256": archived["source_sha256"],
                           "document_version_id": document["document_version_id"], "validation": validation,
                           "reused_company_sha": reused_sha})
            return {"status": "available", "document": document,
                    "acquisition": {**metadata, "source_id": cfg["source_id"],
                                    "source_sha256": archived["source_sha256"],
                                    "source_version": archived["source_version"],
                                    "identity_reused": reused_sha,
                                    "discovery_method": ("official_feed" if network_source.get("discovered_from") else "exact_static_config")}}
