"""Price alert rules with a background checker."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import discord

from core.config import SniperSettings
from core.db import Database
from core.discord_ui import CRYPTO, DISCLAIMER, embed
from core.utils import format_ts, human_number, pct, utcnow

from .market import MarketData, MarketError
from .models import CoinQuote, PriceRule

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sniper_alerts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    channel_id   INTEGER NOT NULL,
    symbol       TEXT NOT NULL,
    direction    TEXT NOT NULL CHECK(direction IN ('above','below')),
    target_price REAL NOT NULL,
    created_by   INTEGER,
    created_at   TEXT NOT NULL,
    triggered_at TEXT,
    last_price   REAL
);
CREATE INDEX IF NOT EXISTS idx_sniper_alerts_guild ON sniper_alerts(guild_id, triggered_at);

CREATE TABLE IF NOT EXISTS sniper_channels (
    guild_id   INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PriceAlertService:
    def __init__(
        self,
        bot: discord.Client,
        db: Database,
        settings: SniperSettings,
        market: MarketData,
    ):
        self.bot = bot
        self.db = db
        self.settings = settings
        self.market = market
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._warned_channels: set[int] = set()

    # -- lifecycle ---------------------------------------------------------
    async def setup(self) -> None:
        await self.db.executescript(SCHEMA)

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="sniper-alerts")
        log.info("Sniper alert loop started (interval=%ss)", self.settings.alert_interval_seconds)

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        await self.bot.wait_until_ready()
        await asyncio.sleep(10)
        while not self._stop.is_set():
            try:
                await self.check_once()
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception:  # noqa: BLE001
                log.exception("Sniper alert cycle failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.alert_interval_seconds)
            except asyncio.TimeoutError:
                continue

    # -- CRUD --------------------------------------------------------------
    async def add_rule(
        self,
        guild_id: int,
        channel_id: int,
        symbol: str,
        direction: str,
        target_price: float,
        created_by: int | None = None,
    ) -> int:
        direction = direction.lower()
        if direction not in {"above", "below"}:
            raise ValueError("direction must be 'above' or 'below'")
        if target_price <= 0:
            raise ValueError("target price must be positive")
        return await self.db.execute(
            """
            INSERT INTO sniper_alerts
                (guild_id, channel_id, symbol, direction, target_price, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                symbol.upper().lstrip("$"),
                direction,
                target_price,
                created_by,
                _now_iso(),
            ),
        )

    async def remove_rule(self, guild_id: int, rule_id: int) -> bool:
        deleted = await self.db.execute_rowcount(
            "DELETE FROM sniper_alerts WHERE guild_id = ? AND id = ?", (guild_id, rule_id)
        )
        return deleted > 0

    async def list_rules(self, guild_id: int, *, include_triggered: bool = False) -> list[PriceRule]:
        query = "SELECT * FROM sniper_alerts WHERE guild_id = ?"
        if not include_triggered:
            query += " AND triggered_at IS NULL"
        query += " ORDER BY id DESC"
        rows = await self.db.fetchall(query, (guild_id,))
        return [
            PriceRule(
                id=int(row["id"]),
                guild_id=int(row["guild_id"]),
                channel_id=int(row["channel_id"]),
                symbol=row["symbol"],
                direction=row["direction"],
                target_price=float(row["target_price"]),
                created_by=row.get("created_by"),
                created_at=row.get("created_at"),
                triggered_at=row.get("triggered_at"),
                last_price=row.get("last_price"),
            )
            for row in rows
        ]

    async def set_channel(self, guild_id: int, channel_id: int) -> None:
        await self.db.execute(
            """
            INSERT INTO sniper_channels (guild_id, channel_id, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET channel_id = excluded.channel_id, updated_at = excluded.updated_at
            """,
            (guild_id, channel_id, _now_iso()),
        )

    async def get_channel(self, guild_id: int) -> int | None:
        value = await self.db.scalar(
            "SELECT channel_id FROM sniper_channels WHERE guild_id = ?", (guild_id,)
        )
        return int(value) if value else None

    # -- checking ----------------------------------------------------------
    @staticmethod
    def is_triggered(rule: PriceRule, price: float) -> bool:
        if price is None:
            return False
        if rule.direction == "above":
            return price >= rule.target_price
        return price <= rule.target_price

    async def check_once(self) -> int:
        rows = await self.db.fetchall(
            "SELECT * FROM sniper_alerts WHERE triggered_at IS NULL ORDER BY symbol"
        )
        if not rows:
            return 0
        rules = [
            PriceRule(
                id=int(row["id"]),
                guild_id=int(row["guild_id"]),
                channel_id=int(row["channel_id"]),
                symbol=row["symbol"],
                direction=row["direction"],
                target_price=float(row["target_price"]),
                created_by=row.get("created_by"),
                created_at=row.get("created_at"),
                last_price=row.get("last_price"),
            )
            for row in rows
        ]
        symbols = sorted({rule.symbol for rule in rules})
        try:
            quotes = await self.market.quotes(symbols)
        except MarketError as exc:
            log.warning("Price alert quote fetch failed: %s", exc)
            return 0
        except Exception as exc:  # noqa: BLE001
            log.warning("Price alert quote fetch crashed: %s", exc)
            return 0

        triggered = 0
        for rule in rules:
            quote = quotes.get(rule.symbol)
            if quote is None or quote.price is None:
                continue
            await self.db.execute(
                "UPDATE sniper_alerts SET last_price = ? WHERE id = ?", (quote.price, rule.id)
            )
            if not self.is_triggered(rule, quote.price):
                continue
            await self.db.execute(
                "UPDATE sniper_alerts SET triggered_at = ? WHERE id = ?", (_now_iso(), rule.id)
            )
            triggered += 1
            await self._notify(rule, quote)
        log.info("Checked %s alert rules, %s triggered", len(rules), triggered)
        return triggered

    async def _notify(self, rule: PriceRule, quote: CoinQuote) -> None:
        channel = await self._resolve_channel(rule)
        if channel is None:
            return
        try:
            await channel.send(embed=self.build_alert_embed(rule, quote))
        except discord.HTTPException as exc:
            log.warning("Failed to send price alert: %s", exc)

    async def _resolve_channel(self, rule: PriceRule) -> discord.abc.Messageable | None:
        channel_id = (
            rule.channel_id
            or await self.get_channel(rule.guild_id)
            or self.settings.alert_channel_id
        )
        if not channel_id:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                if rule.guild_id not in self._warned_channels:
                    log.warning("Sniper alert channel %s is not accessible", channel_id)
                    self._warned_channels.add(rule.guild_id)
                return None
        return channel

    @staticmethod
    def build_alert_embed(rule: PriceRule, quote: CoinQuote) -> discord.Embed:
        arrow = "📈" if rule.direction == "above" else "📉"
        emb = embed(
            f"{arrow} {rule.symbol} moved {rule.direction} ${rule.target_price:,.8g}",
            (
                f"**Current price:** {human_number(quote.price)}\n"
                f"**24h change:** {pct(quote.change_24h)}\n"
                f"**Target:** {rule.direction} ${rule.target_price:,.8g}\n"
                f"**Rule:** #{rule.id}"
            ),
            color=CRYPTO,
            thumbnail=quote.image,
            url=f"https://www.coingecko.com/en/coins/{quote.id}" if quote.id else None,
            fields=[
                ("1h", pct(quote.change_1h), True),
                ("24h", pct(quote.change_24h), True),
                ("7d", pct(quote.change_7d), True),
                ("Triggered", format_ts(utcnow(), "R"), True),
            ],
            footer=DISCLAIMER,
        )
        return emb
