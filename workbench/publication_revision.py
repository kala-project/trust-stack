"""Pure preparation of bounded canonical publication revisions.

Patch mode deliberately treats the current canonical publication as authoritative.  It
re-prepares only explicitly replaced natural blocks and translates the saved canonical
spans of every other block; it never reverse-parses rendered text into draft input.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from .report_publications import prepare
from .tool_errors import ToolError


_DATASET_PATCH_MESSAGE = (
    "Dataset-generated blocks cannot be patched independently; use explicit replace mode "
    "with the complete blocks and datasets."
)


def _block_id(block: dict[str, Any], index: int) -> str:
    value = block.get("block_id", block.get("id", f"report-block-{index + 1}"))
    if not isinstance(value, str) or not value:
        raise ToolError("invalid_report", "Canonical publication has an invalid block ID")
    return value


def _canonical_ids(publication: dict[str, Any]) -> tuple[list[str], list[str]]:
    blocks = publication.get("blocks")
    datasets = publication.get("datasets", [])
    if not isinstance(blocks, list) or not isinstance(datasets, list) or len(datasets) > len(blocks):
        raise ToolError("invalid_report", "Canonical publication block and dataset shape is invalid")
    block_ids = [_block_id(block, index) for index, block in enumerate(blocks)]
    if len(set(block_ids)) != len(block_ids):
        raise ToolError("invalid_report", "Canonical publication block IDs must be unique")
    return block_ids, block_ids[len(block_ids) - len(datasets):] if datasets else []


def _translate_block(block: dict[str, Any], *, identifier: str, start: int) -> dict[str, Any]:
    copied = deepcopy(block)
    old_start, old_end = copied.get("start"), copied.get("end")
    text = copied.get("text")
    spans = copied.get("spans")
    if (type(old_start) is not int or type(old_end) is not int or old_end < old_start
            or not isinstance(text, str) or old_end - old_start != len(text)
            or not isinstance(spans, list)):
        raise ToolError("invalid_report", "Canonical publication block spans are invalid")
    delta = start - old_start
    for span in spans:
        if (not isinstance(span, dict) or type(span.get("start")) is not int
                or type(span.get("end")) is not int
                or not old_start <= span["start"] <= span["end"] <= old_end
                or span.get("text") != text[span["start"] - old_start:span["end"] - old_start]):
            raise ToolError("invalid_report", "Canonical publication span does not match its block text")
        span["start"] += delta
        span["end"] += delta
    copied["id"] = identifier
    copied["start"] = start
    copied["end"] = start + len(text)
    return copied


def patch_publication(*, publication: dict[str, Any], replacements: list[dict[str, Any]],
                      refs: dict[str, Any], facts: dict[str, Any],
                      calculations: dict[str, Any], max_chars: int,
                      title: str | None = None) -> dict[str, Any]:
    """Return a canonical publication with only named narrative blocks replaced."""
    current = deepcopy(publication)
    block_ids, dataset_block_ids = _canonical_ids(current)
    answer = current.get("answer")
    if not isinstance(answer, str):
        raise ToolError("invalid_report", "Canonical publication answer is invalid")
    for block in current["blocks"]:
        start, end = block.get("start"), block.get("end")
        if (type(start) is not int or type(end) is not int
                or answer[start:end] != block.get("text")):
            raise ToolError("invalid_report", "Canonical publication block text does not match its answer span")
    if not isinstance(replacements, list) or len(replacements) > 80:
        raise ToolError("invalid_report", "replacements must be a bounded list")
    by_id: dict[str, dict[str, Any]] = {}
    for replacement in deepcopy(replacements):
        if (not isinstance(replacement, dict) or set(replacement) != {"block_id", "block"}
                or not isinstance(replacement["block_id"], str)
                or not isinstance(replacement["block"], dict)):
            raise ToolError("invalid_report", "Each replacement needs block_id and one natural draft block")
        identifier = replacement["block_id"]
        if identifier in by_id:
            raise ToolError("duplicate_block_id", f"Duplicate replacement block ID: {identifier}")
        if identifier not in block_ids:
            raise ToolError("unknown_block_id", f"Unknown replacement block ID: {identifier}")
        if identifier in dataset_block_ids:
            raise ToolError("dataset_block_patch_forbidden", _DATASET_PATCH_MESSAGE)
        by_id[identifier] = replacement["block"]

    new_title = current.get("title") if title is None else title
    if not isinstance(new_title, str):
        raise ToolError("invalid_report", "Canonical publication title is invalid")
    prepared_replacements: dict[str, dict[str, Any]] = {}
    for identifier, draft in by_id.items():
        one = prepare(title=new_title, blocks=[draft], refs=refs, facts=facts,
                      calculations=calculations, max_chars=max_chars,
                      datasets=[], value_style=current.get("value_style", "recorded"))
        prepared_replacements[identifier] = one["blocks"][0]

    answer = f"# {new_title}\n\n"
    blocks = []
    for index, (identifier, existing) in enumerate(zip(block_ids, current["blocks"])):
        source = prepared_replacements.get(identifier, existing)
        if index:
            answer += "\n\n"
        translated = _translate_block(source, identifier=identifier, start=len(answer))
        answer += translated["text"]
        blocks.append(translated)
    if len(answer) > max_chars:
        raise ToolError("report_size_limit", "Report exceeds the publication text limit")
    result = {"schema_version": current.get("schema_version"), "title": new_title,
              "answer": answer, "blocks": blocks,
              "datasets": deepcopy(current.get("datasets", []))}
    if current.get("value_style", "recorded") != "recorded":
        result["value_style"] = current["value_style"]
    return result


def replace_publication(*, title: str, blocks: list[dict[str, Any]],
                        datasets: list[dict[str, Any]] | None, value_style: str,
                        refs: dict[str, Any], facts: dict[str, Any],
                        calculations: dict[str, Any], max_chars: int) -> dict[str, Any]:
    """Prepare an explicit complete replacement through the existing canonical builder."""
    return prepare(title=title, blocks=deepcopy(blocks), datasets=deepcopy(datasets or []),
                   value_style=value_style, refs=refs, facts=facts,
                   calculations=calculations, max_chars=max_chars)


def revision_diff(existing: dict[str, Any], revised: dict[str, Any], *, limit: int = 50) -> dict[str, Any]:
    """Return a bounded structural diff; content remains in the server-owned preview."""
    def nodes(publication):
        block_ids, _ = _canonical_ids(publication)
        ordered = ["report-title", *block_ids]
        values = {"report-title": publication.get("title", "")}
        values.update({_block_id(block, index): block
                       for index, block in enumerate(publication["blocks"])})
        for index, dataset in enumerate(publication.get("datasets", []), 1):
            identifier = f"report-dataset-{index}"
            ordered.append(identifier)
            values[identifier] = dataset
        return ordered, values

    old_ids, old = nodes(existing)
    new_ids, new = nodes(revised)
    changed = [identifier for identifier in new_ids
               if identifier not in old or old[identifier] != new[identifier]]
    removed = [identifier for identifier in old_ids if identifier not in new]
    added = [identifier for identifier in new_ids if identifier not in old]
    return {
        "changed_node_ids": changed[:limit], "removed_node_ids": removed[:limit],
        "added_node_ids": added[:limit],
        "truncated": any(len(items) > limit for items in (changed, removed, added)),
        "answer_chars_before": len(existing.get("answer", "")),
        "answer_chars_after": len(revised.get("answer", "")),
    }
