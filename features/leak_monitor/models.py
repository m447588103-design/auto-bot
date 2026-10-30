"""Data structures for the leak monitor."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class TargetKind(str, Enum):
    EMAIL = "email"
    DOMAIN = "domain"
    KEYWORD = "keyword"
    PHONE = "phone"
    USERNAME = "username"

    @classmethod
    def values(cls) -> list[str]:
        return [item.value for item in cls]


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"low": 1, "medium": 2, "high": 3, "critical": 4}[self.value]

    @property
    def emoji(self) -> str:
        return {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}[self.value]

    @classmethod
    def parse(cls, value: str | None, default: "Severity" = None) -> "Severity":  # type: ignore[assignment]
        try:
            return cls(str(value).lower())
        except (ValueError, TypeError):
            return default or cls.MEDIUM


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class WatchTarget:
    id: int
    guild_id: int
    value: str
    kind: str
    label: str | None = None
    created_by: int | None = None
    created_at: str = field(default_factory=_utcnow_iso)

    @property
    def display(self) -> str:
        return self.label or self.value

    @property
    def kind_enum(self) -> TargetKind:
        try:
            return TargetKind(self.kind)
        except ValueError:
            return TargetKind.KEYWORD

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "WatchTarget":
        return cls(
            id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            value=row["value"],
            kind=row["kind"],
            label=row.get("label"),
            created_by=row.get("created_by"),
            created_at=row.get("created_at") or _utcnow_iso(),
        )


@dataclass(frozen=True)
class RawHit:
    """A document returned by a leak source, before watchlist matching."""

    source: str
    external_id: str
    title: str
    haystack: str
    url: str | None = None
    snippet: str = ""
    severity: Severity = Severity.MEDIUM
    published_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LeakHit:
    """A :class:`RawHit` that matched a watchlist target."""

    source: str
    external_id: str
    title: str
    url: str | None
    snippet: str
    severity: Severity
    published_at: str | None
    target: WatchTarget
    matched_value: str
    fingerprint: str


@dataclass
class SourceState:
    source: str
    cursor: str | None = None
    last_run: float = 0.0
    last_error: str | None = None
    hits_total: int = 0

    @classmethod
    def from_row(cls, row: dict[str, Any] | None, source: str) -> "SourceState":
        if not row:
            return cls(source=source)
        return cls(
            source=source,
            cursor=row.get("cursor"),
            last_run=float(row.get("last_run") or 0.0),
            last_error=row.get("last_error"),
            hits_total=int(row.get("hits_total") or 0),
        )


@dataclass
class ScanReport:
    scanned_sources: int = 0
    skipped_sources: list[str] = field(default_factory=list)
    raw_documents: int = 0
    new_hits: int = 0
    duplicates: int = 0
    errors: list[str] = field(default_factory=list)
    alerts_sent: int = 0

    def merge(self, other: "ScanReport") -> None:
        self.scanned_sources += other.scanned_sources
        self.raw_documents += other.raw_documents
        self.new_hits += other.new_hits
        self.duplicates += other.duplicates
        self.alerts_sent += other.alerts_sent
        self.skipped_sources.extend(other.skipped_sources)
        self.errors.extend(other.errors)
