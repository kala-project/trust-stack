"""Strict one-shot text edits for an already prepared structured publication."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import re
import threading
from typing import Any


MAX_EDITS = 16
MAX_RESPONSE_CHARS = 16_000
MAX_REQUEST_CHARS = 160_000
MAX_ARGUMENT_CHARS = 100_000
MAX_REPLACEMENT_CHARS = 4_000
SCHEMA_VERSION = "publication-text-repair/1"
_NUMERIC_TOKEN = re.compile(r"[+-]?(?:\d+(?:[.,]\d+)*|\.\d+)(?:[eE][+-]?\d+)?")
_COVERAGE_RANK = {"unanswered": 0, "partial": 1, "answered": 2}


class PublicationRepairError(ValueError):
    """The proposed edit packet is unsafe or outside the bounded contract."""


class TransportBudgetExceeded(RuntimeError):
    """A post-generation provider call exceeded its shared transport allowance."""


class TransportBudget:
    """Shared provider-output limit plus a separate cumulative request reservation.

    max_payload_chars counts emitted events and tool submissions, not prompts.
    Request reservation additionally bounds application-controlled outbound messages,
    system instructions and tool schemas; it does not claim to count vendor internals.
    """

    def __init__(self, max_events: int, max_payload_chars: int, *,
                 max_request_chars: int = 4 * MAX_REQUEST_CHARS):
        if type(max_events) is not int or max_events < 0:
            raise ValueError("transport event limit must be a non-negative integer")
        if type(max_payload_chars) is not int or max_payload_chars < 1:
            raise ValueError("transport payload limit must be a positive integer")
        if type(max_request_chars) is not int or max_request_chars < 1:
            raise ValueError("request character limit must be a positive integer")
        self.max_events = max_events
        self.max_payload_chars = max_payload_chars
        self.max_request_chars = max_request_chars
        self._request_chars_reserved = 0
        self._events_used = 0
        self._payload_chars_used = 0
        self._lock = threading.Lock()

    @staticmethod
    def _serialized_chars(payload: Any) -> int:
        return len(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))

    def _consume(self, *, events: int, payload_chars: int) -> None:
        with self._lock:
            self._events_used += events
            self._payload_chars_used += payload_chars
            if (self._events_used > self.max_events
                    or self._payload_chars_used > self.max_payload_chars):
                raise TransportBudgetExceeded("shared post-generation transport budget exhausted")

    def consume_request(self, request: Any) -> None:
        """Reserve the complete application request before starting any provider stream."""
        count = self._serialized_chars(request)
        with self._lock:
            if self._request_chars_reserved + count > self.max_request_chars:
                raise TransportBudgetExceeded("shared post-generation request budget exhausted")
            self._request_chars_reserved += count

    def consume_event(self, event: Any) -> None:
        """Debit one yielded event and its complete serialized payload."""
        self._consume(events=1, payload_chars=self._serialized_chars(event))

    def consume_submission(self, arguments: Any) -> None:
        """Debit tool submission arguments that may not be represented by a yielded event."""
        self._consume(events=0, payload_chars=self._serialized_chars(arguments))

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "max_request_chars": self.max_request_chars,
                "request_chars_reserved": self._request_chars_reserved,
                "remaining_request_chars": self.max_request_chars - self._request_chars_reserved,
                "max_events": self.max_events,
                "events_used": self._events_used,
                "remaining_events": max(0, self.max_events - self._events_used),
                "max_payload_chars": self.max_payload_chars,
                "payload_chars_used": self._payload_chars_used,
                "remaining_payload_chars": max(0, self.max_payload_chars - self._payload_chars_used),
            }


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise PublicationRepairError(f"repair response contains duplicate JSON key: {key}")
        value[key] = item
    return value


def has_conflicting_claims(review: Any) -> bool:
    return bool(
        isinstance(review, dict)
        and review.get("status") == "assessed"
        and any(
            isinstance(claim, dict)
            and claim.get("support_status") in {"unsupported", "conflicting"}
            for claim in review.get("claims", [])
        )
    )


def review_is_acceptable(original: Any, candidate: Any) -> bool:
    """Require an assessed clean candidate without reduced question coverage."""
    if not isinstance(candidate, dict) or candidate.get("status") != "assessed":
        return False
    if any(
        isinstance(claim, dict)
        and claim.get("support_status") in {"unsupported", "conflicting"}
        for claim in candidate.get("claims", [])
    ):
        return False
    old_status = (original.get("coverage") or {}).get("status") if isinstance(original, dict) else None
    new_status = (candidate.get("coverage") or {}).get("status")
    return old_status in _COVERAGE_RANK and new_status in _COVERAGE_RANK and (
        _COVERAGE_RANK[new_status] >= _COVERAGE_RANK[old_status]
    )


def build_request(arguments: dict[str, Any], rendered_draft: str, review: dict[str, Any],
                  budgets: dict[str, Any]) -> dict[str, Any]:
    """Build bounded untrusted data; the provider supplies the trusted system contract."""
    if not isinstance(arguments, dict) or not isinstance(rendered_draft, str) or not isinstance(review, dict):
        raise PublicationRepairError("repair context is invalid")
    encoded_arguments = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded_arguments) > MAX_ARGUMENT_CHARS:
        raise PublicationRepairError("original publication arguments exceed the repair input limit")
    request = {
        "schema_version": SCHEMA_VERSION,
        "instructions": {
            "response": "Return exactly one JSON object with an edits array; no Markdown or prose.",
            "allowed_targets": ["part_text", "reasoning", "limitation"],
            "rules": [
                "Use zero-based explicit indices and exact old_text.",
                "Edit existing prose only; do not add fields, blocks, parts, limitations, records, or datasets.",
                "Do not change typed bindings, support, classification, value_style, or dataset definitions.",
                "Replacement numeric tokens must already occur in that same original field.",
                "Withdraw or narrow unsupported wording instead of inventing support.",
            ],
            "max_edits": MAX_EDITS,
            "edit_shapes": {
                "part_text": ["target", "block_index", "part_index", "old_text", "new_text"],
                "reasoning": ["target", "block_index", "old_text", "new_text"],
                "limitation": ["target", "block_index", "item_index", "old_text", "new_text"],
            },
        },
        "original_publish_arguments": deepcopy(arguments),
        "rendered_draft": rendered_draft,
        "semantic_critique": deepcopy(review),
        "budgets": deepcopy(budgets),
    }
    if len(json.dumps(request, ensure_ascii=False, separators=(",", ":"))) > MAX_REQUEST_CHARS:
        raise PublicationRepairError("publication repair request exceeds the input limit")
    return request


def _field(edit: dict[str, Any], arguments: dict[str, Any]) -> tuple[dict | list, str | int, str]:
    target = edit.get("target")
    expected = {"target", "block_index", "old_text", "new_text"}
    if target == "part_text":
        expected.add("part_index")
    elif target == "limitation":
        expected.add("item_index")
    elif target != "reasoning":
        raise PublicationRepairError("unknown edit target")
    if set(edit) != expected:
        raise PublicationRepairError("edit keys do not match the target schema")
    block_index = edit["block_index"]
    blocks = arguments.get("blocks")
    if type(block_index) is not int or not isinstance(blocks, list) or not 0 <= block_index < len(blocks):
        raise PublicationRepairError("block index is out of range")
    block = blocks[block_index]
    if not isinstance(block, dict):
        raise PublicationRepairError("target block is invalid")
    if target == "reasoning":
        if "reasoning" not in block:
            raise PublicationRepairError("reasoning field is absent")
        return block, "reasoning", block["reasoning"]
    if target == "part_text":
        index = edit["part_index"]
        parts = block.get("parts")
        if type(index) is not int or not isinstance(parts, list) or not 0 <= index < len(parts):
            raise PublicationRepairError("part index is out of range")
        part = parts[index]
        if not isinstance(part, dict) or set(part) != {"kind", "text"} or part.get("kind") != "text":
            raise PublicationRepairError("only an existing text part may be edited")
        return part, "text", part["text"]
    index = edit["item_index"]
    limitations = block.get("limitations")
    if type(index) is not int or not isinstance(limitations, list) or not 0 <= index < len(limitations):
        raise PublicationRepairError("limitation index is out of range")
    return limitations, index, limitations[index]


def apply_response(arguments: dict[str, Any], response: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Parse strict JSON and apply only exact, unique, numeric-token-safe text replacements."""
    if not isinstance(response, str) or not response.strip() or len(response) > MAX_RESPONSE_CHARS:
        raise PublicationRepairError("repair response is empty or oversized")
    try:
        value = json.loads(response, object_pairs_hook=_strict_object)
    except PublicationRepairError:
        raise
    except (json.JSONDecodeError, TypeError) as exc:
        raise PublicationRepairError("repair response is not exact JSON") from exc
    if not isinstance(value, dict) or set(value) != {"edits"}:
        raise PublicationRepairError("repair response must contain only edits")
    edits = value["edits"]
    if not isinstance(edits, list) or not 1 <= len(edits) <= MAX_EDITS:
        raise PublicationRepairError("repair edit count is outside the limit")
    candidate = deepcopy(arguments)
    touched: set[tuple[Any, ...]] = set()
    for edit in edits:
        if not isinstance(edit, dict):
            raise PublicationRepairError("each edit must be an object")
        container, key, current = _field(edit, candidate)
        old_text, new_text = edit["old_text"], edit["new_text"]
        if not isinstance(current, str) or not isinstance(old_text, str) or not isinstance(new_text, str):
            raise PublicationRepairError("edit text values must be strings")
        if current != old_text:
            raise PublicationRepairError("old_text does not exactly match the indexed field")
        if old_text == new_text or len(new_text) > MAX_REPLACEMENT_CHARS:
            raise PublicationRepairError("replacement is unchanged or oversized")
        identity = (edit["target"], edit["block_index"], edit.get("part_index"), edit.get("item_index"))
        if identity in touched:
            raise PublicationRepairError("multiple edits to one field are ambiguous")
        touched.add(identity)
        if Counter(_NUMERIC_TOKEN.findall(new_text)) - Counter(_NUMERIC_TOKEN.findall(old_text)):
            raise PublicationRepairError("replacement adds a numeric token")
        container[key] = new_text
    return candidate, deepcopy(edits)
