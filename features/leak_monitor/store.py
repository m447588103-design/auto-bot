"""SQLite persistence for watch targets, hits and source state."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from core.db import Database

from .models import LeakHit, Severity, SourceState, TargetKind, WatchTarget

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS leak_targets (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    value       TEXT    NOT NULL,
    kind        TEXT    NOT NULL,
    label       TEXT,
    created_by  INTEGER,
    created_at  TEXT    NOT NULL,
    UNIQUE(guild_id, kind, value)
);
CREATE INDEX IF NOT EXISTS idx_leak_targets_guild ON leak_targets(guild_id);

CREATE TABLE IF NOT EXISTS leak_hits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    target_id    INTEGER,
    source       TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    title        TEXT NOT NULL,
    matched      TEXT NOT NULL,
    snippet      TEXT,
    url          TEXT,
    severity     TEXT NOT NULL,
    published_at TEXT,
    found_at     TEXT NOT NULL,
    fingerprint  TEXT NOT NULL UNIQUE,
    FOREIGN KEY(target_id) REFERENCES leak_targets(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_leak_hits_guild ON leak_hits(guild_id, found_at DESC);

CREATE TABLE IF NOT EXISTS leak_channels (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leak_source_state (
    source      TEXT PRIMARY KEY,
    cursor      TEXT,
    last_run    REAL DEFAULT 0,
    last_error  TEXT,
    hits_total  INTEGER DEFAULT 0
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LeakStore:
    def __init__(self, db: Database):
        self.db = db

    async def ensure_schema(self) -> None:
        await self.db.executescript(SCHEMA)

    # -- targets -----------------------------------------------------------
    async def add_target(
        self,
        guild_id: int,
        value: str,
        kind: TargetKind,
        *,
        label: str | None = None,
        created_by: int | None = None,
    ) -> tuple[WatchTarget, bool]:
        created_at = _now_iso()
        inserted = await self.db.execute_rowcount(
            """
            INSERT OR IGNORE INTO leak_targets (guild_id, value, kind, label, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (guild_id, value, kind.value, label, created_by, created_at),
        )
        row = await self.db.fetchone(
            "SELECT * FROM leak_targets WHERE guild_id = ? AND kind = ? AND value = ?",
            (guild_id, kind.value, value),
        )
        if row is None:  # pragma: no cover - should not happen
            raise RuntimeError("Failed to persist watch target")
        return WatchTarget.from_row(row), bool(inserted)

    async def list_targets(self, guild_id: int) -> list[WatchTarget]:
        rows = await self.db.fetchall(
            "SELECT * FROM leak_targets WHERE guild_id = ? ORDER BY id DESC", (guild_id,)
        )
        return [WatchTarget.from_row(row) for row in rows]

    async def all_targets_by_guild(self) -> dict[int, list[WatchTarget]]:
        rows = await self.db.fetchall("SELECT * FROM leak_targets ORDER BY guild_id, id")
        grouped: dict[int, list[WatchTarget]] = {}
        for row in rows:
            target = WatchTarget.from_row(row)
            grouped.setdefault(target.guild_id, []).append(target)
        return grouped

    async def remove_target(self, guild_id: int, target_id: int) -> bool:
        deleted = await self.db.execute_rowcount(
            "DELETE FROM leak_targets WHERE guild_id = ? AND id = ?", (guild_id, target_id)
        )
        return deleted > 0

    async def count_targets(self, guild_id: int) -> int:
        value = await self.db.scalar(
            "SELECT COUNT(*) FROM leak_targets WHERE guild_id = ?", (guild_id,)
        )
        return int(value or 0)

    # -- channels ----------------------------------------------------------
    async def set_channel(self, guild_id: int, channel_id: int) -> None:
        await self.db.execute(
            """
            INSERT INTO leak_channels (guild_id, channel_id, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id, updated_at = excluded.updated_at
            """,
            (guild_id, channel_id, _now_iso()),
        )

    async def get_channel(self, guild_id: int) -> int | None:
        value = await self.db.scalar(
            "SELECT channel_id FROM leak_channels WHERE guild_id = ?", (guild_id,)
        )
        return int(value) if value else None

    # -- hits --------------------------------------------------------------
    async def record_hit(self, hit: LeakHit) -> bool:
        inserted = await self.db.execute_rowcount(
            """
            INSERT OR IGNORE INTO leak_hits
                (guild_id, target_id, source, external_id, title, matched, snippet, url,
                 severity, published_at, found_at, fingerprint)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hit.target.guild_id,
                hit.target.id,
                hit.source,
                hit.external_id,
                hit.title,
                hit.matched_value,
                hit.snippet,
                hit.url,
                hit.severity.value,
                hit.published_at,
                _now_iso(),
                hit.fingerprint,
            ),
        )
        return bool(inserted)

    async def recent_hits(self, guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            """
            SELECT h.*, t.value AS target_value, t.kind AS target_kind, t.label AS target_label
            FROM leak_hits h
            LEFT JOIN leak_targets t ON t.id = h.target_id
            WHERE h.guild_id = ?
            ORDER BY h.id DESC
            LIMIT ?
            """,
            (guild_id, max(1, min(limit, 25))),
        )
        return rows

    async def count_hits(self, guild_id: int) -> int:
        value = await self.db.scalar(
            "SELECT COUNT(*) FROM leak_hits WHERE guild_id = ?", (guild_id,)
        )
        return int(value or 0)

    async def stats_by_source(self, guild_id: int) -> list[dict[str, Any]]:
        return await self.db.fetchall(
            """
            SELECT source, COUNT(*) AS hits, MAX(found_at) AS last_hit
            FROM leak_hits WHERE guild_id = ? GROUP BY source ORDER BY hits DESC
            """,
            (guild_id,),
        )

    # -- source state ------------------------------------------------------
    async def get_source_state(self, source: str) -> SourceState:
        row = await self.db.fetchone("SELECT * FROM leak_source_state WHERE source = ?", (source,))
        return SourceState.from_row(row, source)

    async def get_all_source_states(self) -> dict[str, SourceState]:
        rows = await self.db.fetchall("SELECT * FROM leak_source_state")
        return {row["source"]: SourceState.from_row(row, row["source"]) for row in rows}

    async def save_source_state(
        self, source: str, *, cursor: str | None, last_error: str | None, new_hits: int = 0
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO leak_source_state (source, cursor, last_run, last_error, hits_total)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                cursor = COALESCE(excluded.cursor, leak_source_state.cursor),
                last_run = excluded.last_run,
                last_error = excluded.last_error,
                hits_total = leak_source_state.hits_total + ?
            """,
            (source, cursor, datetime.now(timezone.utc).timestamp(), last_error, new_hits, new_hits),
        )

    @staticmethod
    def severity_for(source: str, kind: TargetKind) -> Severity:
        base = {
            "hibp": Severity.HIGH,
            "hudsonrock": Severity.CRITICAL,
            "ransomwatch": Severity.HIGH,
            "psbdmp": Severity.LOW,
        }.get(source, Severity.MEDIUM)
        if kind in (TargetKind.EMAIL, TargetKind.DOMAIN) and base.rank < Severity.HIGH.rank:
            return Severity.HIGH
        return base
