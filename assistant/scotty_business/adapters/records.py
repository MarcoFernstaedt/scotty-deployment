from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class ProviderRecord:
    provider: str
    source_id: str
    retrieved_at: datetime
    source_revision: str
    fields: Mapping[str, object]
    missing_attributes: tuple[str, ...]


def utc_now() -> datetime:
    return datetime.now(UTC)
