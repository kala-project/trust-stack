"""Narrow dependency capabilities consumed by L3 research assembly."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class EvidenceReader(Protocol):
    """Read one immutable company evidence snapshot; owns no research policy."""

    def report(self, company_id: str) -> dict: ...


@runtime_checkable
class EvidenceReaderFactory(Protocol):
    def __call__(self, source) -> EvidenceReader: ...
