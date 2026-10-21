"""Offline extraction of navigational link candidates from archived HTML."""
from __future__ import annotations

from html.parser import HTMLParser
import re
from typing import Callable
from urllib.parse import urldefrag, urljoin


MAX_LABEL_LENGTH = 500
MAX_LINK_EVENTS = 4096
MAX_HREF_LENGTH = 8192
_DOCUMENT_LINK_RELS = frozenset({
    "alternate", "appendix", "canonical", "chapter", "contents", "copyright",
    "first", "glossary", "help", "index", "last", "license", "next", "prev",
    "search", "section", "start", "subsection", "up",
})


def _bounded_text(value: str, maximum: int = MAX_LABEL_LENGTH) -> str:
    return re.sub(r"\s+", " ", value).strip()[:maximum]


class _LinkHTMLParser(HTMLParser):
    """Collect link syntax only; this deliberately does not alter body parsing."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.bases: list[str] = []
        self.links: list[dict[str, object]] = []
        self._anchors: list[dict[str, object]] = []
        self.extra_bases = 0
        self.rejected_links = 0
        self.scan_truncated = False

    @staticmethod
    def _attributes(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        # HTML uses the first attribute when duplicate names are present.
        result: dict[str, str] = {}
        for name, value in attrs:
            result.setdefault(name.casefold(), value or "")
        return result

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        values = self._attributes(attrs)
        if tag == "base" and "href" in values:
            if self.bases:
                self.extra_bases += 1
            else:
                self.bases.append(values["href"][:MAX_HREF_LENGTH + 1])
            return
        if tag == "a":
            # A new anchor closes the preceding one in HTML; never accumulate
            # malformed nested anchors or copy every text node to all of them.
            self._anchors.clear()
        if tag in {"a", "link"} and "href" in values:
            if len(values["href"]) > MAX_HREF_LENGTH:
                self.rejected_links += 1
                return
            if len(self.links) >= MAX_LINK_EVENTS:
                self.scan_truncated = True
                return
        if tag == "a" and "href" in values:
            event: dict[str, object] = {
                "raw_href": values["href"], "tag": "a", "rel": _bounded_text(values.get("rel", "")),
                "label": "", "fallback_label": _bounded_text(values.get("aria-label") or values.get("title", "")),
            }
            self.links.append(event)
            self._anchors.append(event)
            return
        if tag == "link" and "href" in values:
            rel = _bounded_text(values.get("rel", "")).casefold()
            if set(rel.split()).intersection(_DOCUMENT_LINK_RELS):
                self.links.append({
                    "raw_href": values["href"], "tag": "link", "rel": rel,
                    "label": "", "fallback_label": _bounded_text(values.get("title", "")),
                })

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() == "a":
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        for anchor in self._anchors:
            label = str(anchor["label"])
            remaining = MAX_LABEL_LENGTH - len(label)
            if remaining > 0:
                chunk = re.sub(r"\s+", " ", data)
                anchor["label"] = label + (chunk if label else chunk.lstrip())[:remaining]

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._anchors:
            self._anchors.pop()


def extract_link_candidates(
    raw: bytes,
    *,
    document_url: str,
    limit: int,
    validate_url: Callable[[str], str],
) -> dict[str, object]:
    """Return safe, de-duplicated links without performing network resolution."""
    parser = _LinkHTMLParser()
    parser.feed(raw.decode("utf-8", errors="replace"))
    parser.close()

    rejected_count = parser.extra_bases + parser.rejected_links
    base_url = document_url
    base_status = "absent"
    if parser.bases:
        try:
            if len(parser.bases[0]) > MAX_HREF_LENGTH:
                raise ValueError('base URL too long')
            base_url = validate_url(urljoin(document_url, parser.bases[0].strip()))
            base_status = "accepted"
        except (TypeError, ValueError):
            rejected_count += 1
            base_status = "rejected"
            base_url = document_url

    ordered: list[dict[str, object]] = []
    by_url: dict[str, dict[str, object]] = {}
    for event in parser.links:
        raw_href = str(event["raw_href"])
        href = raw_href.strip()
        if not href or href.startswith("#"):
            continue
        try:
            resolved, _fragment = urldefrag(urljoin(base_url, href))
            resolved = validate_url(resolved)
        except (TypeError, ValueError):
            rejected_count += 1
            continue
        existing = by_url.get(resolved)
        if existing is not None:
            existing["occurrence_count"] = int(existing["occurrence_count"]) + 1
            continue
        label = _bounded_text(str(event["label"]))
        if not label:
            label = _bounded_text(str(event["fallback_label"]))
        candidate: dict[str, object] = {
            "raw_href": raw_href,
            "resolved_url": resolved,
            "tag": event["tag"],
            "label": label,
            "rel": event["rel"],
            "occurrence_count": 1,
        }
        by_url[resolved] = candidate
        ordered.append(candidate)

    return {
        "candidates": ordered[:limit],
        "truncated": parser.scan_truncated or len(ordered) > limit,
        "scan_truncated": parser.scan_truncated,
        "effective_base_url": base_url,
        "rejected_count": rejected_count,
        "base_status": base_status,
    }
