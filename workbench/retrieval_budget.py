"""Scoped search caching and progress signals; semantic coverage belongs to the agent."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import json
import threading
from typing import Any, Callable


POLICY_VERSION = "scoped-retrieval-v2"
SEARCH_TOOLS = frozenset({"search_documents", "search_evidence"})
DEFAULT_SEARCH_LIMIT = None
DEFAULT_SATURATION_LIMIT = 3
DEFAULT_DENIAL_LIMIT = 5
MAX_KNOWN_PASSAGE_IDS = 20
MAX_KNOWN_PASSAGE_CANDIDATES = 8
MAX_KNOWN_QUOTE_CHARS = 1_200


@dataclass(frozen=True)
class RetrievalOutcome:
    """One policy decision and the value that should be returned to the model."""

    action: str
    result: dict[str, Any]
    terminate: bool = False


class RetrievalBudget:
    """Thread-safe cache, optional resource cap, and non-binding repetition signals."""

    def __init__(
        self,
        *,
        company_id: str,
        data_version: str,
        document_version_ids: list[str] | tuple[str, ...],
        search_limit: int | None = DEFAULT_SEARCH_LIMIT,
        saturation_limit: int = DEFAULT_SATURATION_LIMIT,
        denial_limit: int = DEFAULT_DENIAL_LIMIT,
    ) -> None:
        if (min(saturation_limit, denial_limit) < 1
                or (search_limit is not None and (type(search_limit) is not int or search_limit < 1))):
            raise ValueError("retrieval policy limits must be positive")
        self._scope = {
            "company_id": company_id,
            "data_version": data_version,
            "document_version_ids": list(document_version_ids),
        }
        self.search_limit = search_limit
        self.saturation_limit = saturation_limit
        self.denial_limit = denial_limit
        self._lock = threading.RLock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._result_ids: set[str] = set()
        self._passage_ids: dict[str, None] = {}
        self._passage_candidates: dict[str, dict[str, Any]] = {}
        self._source_ids: set[str] = set()
        self._accepted = 0
        self._cached = 0
        self._denied = 0
        self._attempts = 0
        self._consecutive_no_new = 0
        self._closed_reason: str | None = None

    def configuration(self) -> dict[str, Any]:
        return {
            "version": POLICY_VERSION,
            "search_tools": sorted(SEARCH_TOOLS),
            "search_limit": self.search_limit,
            "saturation_limit": self.saturation_limit,
            "denial_limit": self.denial_limit,
            "scope": deepcopy(self._scope),
        }

    def add_document_version(self, document_version_id: str) -> None:
        """Extend only this run's version scope after an authoritative acquisition grant."""
        with self._lock:
            versions = self._scope["document_version_ids"]
            if document_version_id not in versions:
                versions.append(document_version_id)
                # A result cached under the narrower scope must not mask the new document.
                self._cache.clear()

    def progress(self) -> dict[str, Any]:
        with self._lock:
            return self._progress_locked()

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        operation: Callable[[], dict[str, Any]],
    ) -> RetrievalOutcome:
        """Apply policy and run an original search at most once per exact scoped key.

        The policy lock intentionally covers original execution. Search handlers may be
        invoked concurrently by a provider, and duplicate calls must never race past the
        cache or consume two original retrievals.
        """
        if name not in SEARCH_TOOLS:
            raise ValueError(f"{name} is not a retrieval-policy search tool")
        key = self._cache_key(name, arguments)
        with self._lock:
            self._attempts += 1
            cached = self._cache.get(key)
            if cached is not None:
                self._cached += 1
                result = deepcopy(cached)
                result["_retrieval_policy"] = {
                    **self._progress_locked(),
                    "decision": "cached",
                    "cache_marker": "exact_scoped_arguments",
                }
                return RetrievalOutcome("cached", result)

            if self._closed_reason is not None or (self.search_limit is not None and self._accepted >= self.search_limit):
                if self._closed_reason is None:
                    self._closed_reason = "search_cap"
                self._denied += 1
                terminate = self._denied >= self.denial_limit
                return RetrievalOutcome("denied", self._closed_result_locked(terminate), terminate)

            self._accepted += 1
            # Exceptions are still actual accepted search executions, but are neither
            # cached nor treated as an empty successful result for saturation.
            result = operation()
            if not isinstance(result, dict):
                raise TypeError("search tool result must be an object")
            original = deepcopy(result)
            self._cache[key] = original
            found, passages, sources = self._identifiers(result)
            candidates = self._candidate_passages(result)
            new_ids = found - self._result_ids
            self._result_ids.update(found)
            for identifier in candidates:
                self._passage_ids.setdefault(identifier, None)
            for identifier in sorted(passages):
                self._passage_ids.setdefault(identifier, None)
            for identifier, candidate in candidates.items():
                self._passage_candidates.setdefault(identifier, candidate)
            self._source_ids.update(sources)
            if new_ids:
                self._consecutive_no_new = 0
            else:
                self._consecutive_no_new += 1
            if self.search_limit is not None and self._accepted >= self.search_limit and self._closed_reason is None:
                self._closed_reason = "search_cap"
            result = deepcopy(original)
            result["_retrieval_policy"] = {
                **self._progress_locked(),
                "decision": "accepted",
                "new_result_ids": len(new_ids),
            }
            return RetrievalOutcome("accepted", result)

    def metadata_for_tool(self) -> dict[str, Any]:
        """Current progress for non-search tool results, including reads after closure."""
        with self._lock:
            return {**self._progress_locked(), "decision": "non_search_allowed"}

    def _cache_key(self, name: str, arguments: dict[str, Any]) -> str:
        value = {"scope": self._scope, "tool": name, "arguments": arguments}
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _progress_locked(self) -> dict[str, Any]:
        return {
            "version": POLICY_VERSION,
            "attempts": self._attempts,
            "accepted": self._accepted,
            "cached": self._cached,
            "denied": self._denied,
            "search_limit": self.search_limit,
            "consecutive_no_new_result_ids": self._consecutive_no_new,
            "saturation_limit": self.saturation_limit,
            "repetition_warning": self._consecutive_no_new >= self.saturation_limit,
            "coverage_status": "not_assessed",
            "instruction": (
                "Returned IDs and repetition counts are not semantic coverage. Assess remaining research "
                "questions and choose whether another search, another method, clarification, or a report is justified. "
                "All attempts still consume the shared execution budget."
            ),
            "closed": self._closed_reason is not None,
            "closed_reason": self._closed_reason,
            "known_refs": {
                "passage_ids": list(self._passage_ids)[:MAX_KNOWN_PASSAGE_IDS],
                "passage_candidates": list(self._passage_candidates.values())[
                    :MAX_KNOWN_PASSAGE_CANDIDATES
                ],
                "source_ids": sorted(self._source_ids)[:20],
            },
        }

    def _closed_result_locked(self, terminate: bool) -> dict[str, Any]:
        progress = self._progress_locked()
        return {
            "status": "retrieval_closed",
            "error": {
                "code": "retrieval_closed",
                "message": "Search retrieval is closed by the deterministic per-run policy.",
            },
            "known_candidate_passage_ids": progress["known_refs"]["passage_ids"],
            "known_candidate_passages": progress["known_refs"]["passage_candidates"],
            "explicit_uncertainty": (
                "The requested missing information was not established by the accepted searches; "
                "absence from results is not evidence of non-disclosure."
            ),
            "instruction": (
                "Do not request more searches. Read selected known passages or use non-search fact "
                "and calculation tools, then answer the supported part and explicitly leave unresolved "
                "items unresolved rather than guessing."
            ),
            "_retrieval_policy": {
                **progress,
                "decision": "denied",
                "termination_required": terminate,
            },
        }

    @staticmethod
    def _identifiers(value: Any) -> tuple[set[str], set[str], set[str]]:
        found: set[str] = set()
        passages: set[str] = set()
        sources: set[str] = set()

        def visit(item: Any) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if isinstance(child, str):
                        if key == "passage_id":
                            passages.add(child)
                            found.add(f"passage:{child}")
                        elif key == "source_id":
                            sources.add(child)
                            found.add(f"source:{child}")
                        elif key == "evidence_id":
                            found.add(f"evidence:{child}")
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)

        visit(value)
        return found, passages, sources

    @staticmethod
    def _candidate_passages(value: Any) -> dict[str, dict[str, Any]]:
        """Keep first-seen search ranking plus enough text to choose a useful read."""
        candidates: dict[str, dict[str, Any]] = {}

        def visit(item: Any) -> None:
            if isinstance(item, dict):
                identifier = item.get("passage_id")
                if isinstance(identifier, str) and identifier not in candidates:
                    candidate = {"passage_id": identifier}
                    for key in ("document_version_id", "title", "locator"):
                        if key in item:
                            candidate[key] = deepcopy(item[key])
                    quote = item.get("quote")
                    if isinstance(quote, str):
                        candidate["quote"] = quote[:MAX_KNOWN_QUOTE_CHARS]
                        candidate["quote_truncated"] = len(quote) > MAX_KNOWN_QUOTE_CHARS
                    candidates[identifier] = candidate
                for child in item.values():
                    visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)

        visit(value)
        return candidates
