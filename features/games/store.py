"""SQLite persistence for game sessions."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from core.db import Database
from features.games.models import AdventureState, BossState, DetectiveState, GameState

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS game_sessions (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    owner_id    INTEGER NOT NULL,
    status      TEXT NOT NULL,
    state       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_game_sessions_guild
    ON game_sessions (guild_id, kind, status);
CREATE INDEX IF NOT EXISTS idx_game_sessions_owner
    ON game_sessions (guild_id, owner_id, kind);
CREATE INDEX IF NOT EXISTS idx_game_sessions_channel
    ON game_sessions (guild_id, kind, channel_id, status);
"""

def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def kind_of(state: GameState) -> str:
    if isinstance(state, AdventureState):
        return "rpg"
    if isinstance(state, DetectiveState):
        return "detective"
    if isinstance(state, BossState):
        return "boss"
    raise TypeError(f"Unsupported game state: {type(state)!r}")


def decode(kind: str, raw: str) -> GameState | None:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("Discarding unreadable game session (%s)", kind)
        return None
    try:
        if kind == "rpg":
            return AdventureState.from_dict(data)
        if kind == "detective":
            return DetectiveState.from_dict(data)
        if kind == "boss":
            return BossState.from_dict(data)
    except (TypeError, ValueError, KeyError, AttributeError):
        log.warning("Discarding malformed game session (%s)", kind, exc_info=True)
    return None


class GameStore:
    def __init__(self, db: Database):
        self.db = db

    async def setup(self) -> None:
        await self.db.executescript(SCHEMA)

    async def save(self, state: GameState) -> None:
        now = _iso_now()
        state.updated_at = now
        await self.db.execute(
            """
            INSERT INTO game_sessions
                (id, kind, guild_id, channel_id, owner_id, status, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                state = excluded.state,
                updated_at = excluded.updated_at
            """,
            (
                state.id,
                kind_of(state),
                state.guild_id,
                state.channel_id,
                state.owner_id,
                state.status,
                json.dumps(state.to_dict(), ensure_ascii=False),
                state.created_at,
                now,
            ),
        )

    async def get(self, session_id: str) -> GameState | None:
        row = await self.db.fetchone(
            "SELECT kind, state FROM game_sessions WHERE id = ?", (session_id,)
        )
        if row is None:
            return None
        return decode(row["kind"], row["state"])

    async def list_active(
        self,
        guild_id: int | None = None,
        kind: str | None = None,
        limit: int = 200,
    ) -> list[GameState]:
        query = "SELECT kind, state FROM game_sessions WHERE status = 'active'"
        params: list[object] = []
        if guild_id is not None:
            query += " AND guild_id = ?"
            params.append(guild_id)
        if kind is not None:
            query += " AND kind = ?"
            params.append(kind)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, limit))
        rows = await self.db.fetchall(query, tuple(params))
        states: list[GameState] = []
        for row in rows:
            state = decode(row["kind"], row["state"])
            if state is not None:
                states.append(state)
        return states

    async def for_guild(
        self,
        guild_id: int,
        kind: str,
        status: str | None = "active",
    ) -> list[GameState]:
        query = "SELECT kind, state FROM game_sessions WHERE guild_id = ? AND kind = ?"
        params: list[object] = [guild_id, kind]
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC LIMIT 100"
        rows = await self.db.fetchall(query, tuple(params))
        return [s for s in (decode(r["kind"], r["state"]) for r in rows) if s is not None]

    async def find_active(
        self,
        guild_id: int,
        kind: str,
        *,
        owner_id: int | None = None,
        channel_id: int | None = None,
        limit: int = 20,
    ) -> list[GameState]:
        query = (
            "SELECT kind, state FROM game_sessions "
            "WHERE status = 'active' AND guild_id = ? AND kind = ?"
        )
        params: list[object] = [guild_id, kind]
        if owner_id is not None:
            query += " AND owner_id = ?"
            params.append(owner_id)
        if channel_id is not None:
            query += " AND channel_id = ?"
            params.append(channel_id)
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(max(1, limit))
        rows = await self.db.fetchall(query, tuple(params))
        return [s for s in (decode(r["kind"], r["state"]) for r in rows) if s is not None]

    async def count_active(self) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS total FROM game_sessions WHERE status = 'active'"
        )
        return int(row["total"]) if row else 0

    async def set_status(self, session_id: str, status: str) -> None:
        await self.db.execute(
            "UPDATE game_sessions SET status = ?, updated_at = ? WHERE id = ?",
            (status, _iso_now(), session_id),
        )

    async def purge(self, active_ttl_minutes: int, finished_ttl_hours: int = 48) -> int:
        now = datetime.now(timezone.utc)
        active_cutoff = (now - timedelta(minutes=max(10, active_ttl_minutes))).isoformat()
        finished_cutoff = (now - timedelta(hours=max(1, finished_ttl_hours))).isoformat()
        removed = await self.db.execute_rowcount(
            """
            DELETE FROM game_sessions
            WHERE (status = 'active' AND updated_at < ?)
               OR (status != 'active' AND updated_at < ?)
            """,
            (active_cutoff, finished_cutoff),
        )
        if removed:
            log.info("Purged %s stale game sessions", removed)
        return removed

    async def expire(self, session_id: str) -> None:
        await self.set_status(session_id, "expired")

