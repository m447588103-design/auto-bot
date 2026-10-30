"""Thin async wrapper around the stdlib sqlite3 module.

A single connection is shared between threads (``check_same_thread=False``) and
guarded by a lock; every call is dispatched with :func:`asyncio.to_thread` so the
Discord event loop never blocks on disk I/O.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------
    async def connect(self) -> None:
        if self._conn is not None:
            return
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        def _open() -> sqlite3.Connection:
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            return conn

        self._conn = await asyncio.to_thread(_open)
        log.info("SQLite connected at %s", self.path)

    async def close(self) -> None:
        if self._conn is None:
            return
        conn, self._conn = self._conn, None
        await asyncio.to_thread(conn.close)
        log.info("SQLite connection closed")

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def _require(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() must be awaited before use")
        return self._conn

    # -- query helpers -----------------------------------------------------
    async def executescript(self, script: str) -> None:
        conn = self._require()

        def _run() -> None:
            with self._lock:
                conn.executescript(script)
                conn.commit()

        await asyncio.to_thread(_run)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Execute a statement and return ``lastrowid`` (or rowcount)."""
        conn = self._require()

        def _run() -> int:
            with self._lock:
                cur = conn.execute(sql, params)
                conn.commit()
                return int(cur.lastrowid or cur.rowcount or 0)

        return await asyncio.to_thread(_run)

    async def execute_rowcount(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Execute a statement and return the number of affected rows.

        Useful for ``INSERT OR IGNORE`` de-duplication (1 = inserted, 0 = seen).
        """
        conn = self._require()

        def _run() -> int:
            with self._lock:
                cur = conn.execute(sql, params)
                conn.commit()
                return int(cur.rowcount if cur.rowcount is not None else 0)

        return await asyncio.to_thread(_run)

    async def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        conn = self._require()
        rows = list(seq)

        def _run() -> None:
            with self._lock:
                conn.executemany(sql, rows)
                conn.commit()

        await asyncio.to_thread(_run)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        conn = self._require()

        def _run() -> list[dict[str, Any]]:
            with self._lock:
                cur = conn.execute(sql, params)
                return [dict(row) for row in cur.fetchall()]

        return await asyncio.to_thread(_run)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = await self.fetchall(sql, params)
        return rows[0] if rows else None

    async def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        conn = self._require()

        def _run() -> Any:
            with self._lock:
                cur = conn.execute(sql, params)
                row = cur.fetchone()
                return row[0] if row else None

        return await asyncio.to_thread(_run)
