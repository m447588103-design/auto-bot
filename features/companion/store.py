"""SQLite-backed conversation memory for the AI companion."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from core.db import Database

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS companion_memory (
    user_id    INTEGER PRIMARY KEY,
    history    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_companion_memory_updated
    ON companion_memory (updated_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CompanionStore:
    def __init__(self, db: Database):
        self.db = db

    async def setup(self) -> None:
        await self.db.executescript(SCHEMA)

    async def load(self, user_id: int) -> list[dict[str, str]]:
        row = await self.db.fetchone(
            "SELECT history FROM companion_memory WHERE user_id = ?", (user_id,)
        )
        if row is None:
            return []
        try:
            raw = json.loads(row["history"])
        except (TypeError, ValueError):
            log.warning("Discarding unreadable companion memory for user %s", user_id)
            return []
        if not isinstance(raw, list):
            return []
        history: list[dict[str, str]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            role = entry.get("role")
            content = entry.get("content")
            if role in {"user", "assistant"} and isinstance(content, str) and content:
                history.append({"role": role, "content": content})
        return history

    async def append(
        self,
        user_id: int,
        user_text: str,
        assistant_text: str,
        *,
        max_turns: int,
        max_chars: int,
    ) -> list[dict[str, str]]:
        history = await self.load(user_id) if max_turns > 0 else []
        if max_turns <= 0:
            await self.clear(user_id)
            return []
        user_text = _clip(user_text, max_chars)
        assistant_text = _clip(assistant_text, max_chars)
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": assistant_text})
        history = history[-max_turns * 2 :]
        await self.db.execute(
            """
            INSERT INTO companion_memory (user_id, history, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                history = excluded.history,
                updated_at = excluded.updated_at
            """,
            (user_id, json.dumps(history, ensure_ascii=False), _now()),
        )
        return history

    async def clear(self, user_id: int) -> bool:
        removed = await self.db.execute_rowcount(
            "DELETE FROM companion_memory WHERE user_id = ?", (user_id,)
        )
        return removed > 0

    async def purge(self, max_age_days: int) -> int:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, max_age_days))
        ).isoformat(timespec="seconds")
        removed = await self.db.execute_rowcount(
            "DELETE FROM companion_memory WHERE updated_at < ?", (cutoff,)
        )
        if removed:
            log.info("Purged %s stale companion memories", removed)
        return removed

    async def count(self) -> int:
        row = await self.db.fetchone("SELECT COUNT(*) AS total FROM companion_memory")
        return int(row["total"]) if row else 0


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"
