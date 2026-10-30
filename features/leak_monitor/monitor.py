"""Background leak scanner and Discord alert dispatcher."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Sequence

import discord

from core.config import LeakSettings
from core.discord_ui import DISCLAIMER, LEAK, embed, error_embed
from core.http import http_session
from core.utils import format_ts, mask_secret, truncate, url_hash, utcnow

from .matcher import find_match
from .models import (
    LeakHit,
    RawHit,
    ScanReport,
    Severity,
    SourceState,
    TargetKind,
    WatchTarget,
)
from .sources import LeakSource, build_sources
from .store import LeakStore

log = logging.getLogger(__name__)

SEVERITY_COLORS = {
    Severity.CRITICAL: discord.Color.from_str("#8B0000"),
    Severity.HIGH: discord.Color.from_str("#ED4245"),
    Severity.MEDIUM: discord.Color.from_str("#E67E22"),
    Severity.LOW: discord.Color.from_str("#95A5A6"),
}


class LeakMonitor:
    """Runs the watchlist scan loop and posts alerts."""

    def __init__(
        self,
        bot: discord.Client,
        store: LeakStore,
        settings: LeakSettings,
        *,
        default_channel_id: int | None = None,
        sources: Sequence[LeakSource] | None = None,
    ):
        self.bot = bot
        self.store = store
        self.settings = settings
        self.default_channel_id = default_channel_id or settings.alert_channel_id
        self.sources = list(sources) if sources is not None else build_sources(settings)
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self.last_scan_at: float = 0.0
        self.last_report: ScanReport | None = None
        self._missing_channel_warned: set[int] = set()

    # -- lifecycle ---------------------------------------------------------
    async def setup(self) -> None:
        await self.store.ensure_schema()
        log.info(
            "Leak monitor ready with %s source(s): %s",
            len(self.sources),
            ", ".join(s.name for s in self.sources),
        )

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="leak-monitor")
        log.info("Leak monitor loop started (interval=%ss)", self.settings.interval_seconds)

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
        # small delay so startup traffic settles
        await asyncio.sleep(15)
        while not self._stop.is_set():
            try:
                await self.scan()
            except asyncio.CancelledError:  # pragma: no cover
                raise
            except Exception:  # noqa: BLE001
                log.exception("Leak scan cycle crashed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.settings.interval_seconds)
            except asyncio.TimeoutError:
                continue

    # -- scanning ----------------------------------------------------------
    async def scan(self, *, guild_id: int | None = None, force: bool = False) -> ScanReport:
        report = ScanReport()
        if self._lock.locked():
            log.info("Leak scan already running, skipping overlapping request")
            report.errors.append("A scan is already in progress.")
            return report

        async with self._lock:
            grouped = await self.store.all_targets_by_guild()
            if guild_id is not None:
                grouped = {guild_id: grouped.get(guild_id, [])}
            targets = [target for group in grouped.values() for target in group]
            if not targets:
                self.last_scan_at = time.time()
                self.last_report = report
                return report

            states = await self.store.get_all_source_states()
            async with http_session(timeout=self.settings.request_timeout) as session:
                for source in self.sources:
                    if not source.available:
                        report.skipped_sources.append(f"{source.name} ({source.reason_unavailable})")
                        continue
                    state = states.get(source.name) or SourceState(source=source.name)
                    if not force and time.time() - state.last_run < min(
                        source.min_interval, self.settings.min_source_interval
                    ):
                        report.skipped_sources.append(f"{source.name} (cooling down)")
                        continue
                    await self._run_source(session, source, state, targets, grouped, report)

            self.last_scan_at = time.time()
            self.last_report = report
            log.info(
                "Leak scan complete: sources=%s hits=%s duplicates=%s alerts=%s errors=%s",
                report.scanned_sources,
                report.new_hits,
                report.duplicates,
                report.alerts_sent,
                len(report.errors),
            )
        return report

    async def _run_source(
        self,
        session,
        source: LeakSource,
        state: SourceState,
        targets: list[WatchTarget],
        grouped: dict[int, list[WatchTarget]],
        report: ScanReport,
    ) -> None:
        started = time.monotonic()
        new_hits = 0
        try:
            result = await asyncio.wait_for(
                source.scan(session, targets, state), timeout=180
            )
        except asyncio.TimeoutError:
            await self.store.save_source_state(
                source.name, cursor=None, last_error="scan timed out after 180s"
            )
            report.errors.append(f"{source.name}: timeout")
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("Leak source %s failed: %s", source.name, exc)
            await self.store.save_source_state(source.name, cursor=None, last_error=str(exc)[:300])
            report.errors.append(f"{source.name}: {exc}")
            return

        report.scanned_sources += 1
        report.raw_documents += len(result.hits)
        alerts_by_guild: dict[int, list[LeakHit]] = {}

        for raw in result.hits:
            for guild, guild_targets in grouped.items():
                for target in guild_targets:
                    matched = self._match_target(target, raw)
                    if not matched:
                        continue
                    severity = self.store.severity_for(source.name, target.kind_enum)
                    if raw.severity.rank > severity.rank:
                        severity = raw.severity
                    hit = LeakHit(
                        source=source.name,
                        external_id=raw.external_id,
                        title=raw.title,
                        url=raw.url,
                        snippet=raw.snippet,
                        severity=severity,
                        published_at=raw.published_at,
                        target=target,
                        matched_value=matched,
                        fingerprint=url_hash(source.name, raw.external_id, target.id, length=32),
                    )
                    if await self.store.record_hit(hit):
                        new_hits += 1
                        report.new_hits += 1
                        alerts_by_guild.setdefault(guild, []).append(hit)
                    else:
                        report.duplicates += 1

        await self.store.save_source_state(
            source.name,
            cursor=result.cursor,
            last_error="; ".join(result.warnings)[:300] or None,
            new_hits=new_hits,
        )
        log.info(
            "Source %s: %s documents, %s new hits in %.1fs",
            source.name,
            len(result.hits),
            new_hits,
            time.monotonic() - started,
        )
        if alerts_by_guild:
            report.alerts_sent += await self._dispatch(alerts_by_guild, source.name)

    @staticmethod
    def _match_target(target: WatchTarget, raw: RawHit) -> str | None:
        if target.kind_enum is TargetKind.KEYWORD:
            return find_match(target, raw.haystack)
        if raw.source in {"hibp", "hudsonrock"}:
            # those sources are queried per target already
            return target.value if target.value.casefold() in raw.haystack.casefold() else None
        return find_match(target, raw.haystack)

    # -- dispatch ----------------------------------------------------------
    async def _dispatch(self, alerts_by_guild: dict[int, list[LeakHit]], source: str) -> int:
        sent = 0
        budget = self.settings.max_alerts_per_cycle
        for guild_id, hits in alerts_by_guild.items():
            if budget <= 0:
                break
            channel = await self._resolve_channel(guild_id)
            if channel is None:
                continue
            hits.sort(key=lambda h: h.severity.rank, reverse=True)
            batch = hits[:budget]
            budget -= len(batch)
            for hit in batch:
                try:
                    await channel.send(embed=self.build_alert_embed(hit))
                    sent += 1
                except discord.HTTPException as exc:
                    log.warning("Failed to send leak alert to guild %s: %s", guild_id, exc)
                    break
                await asyncio.sleep(0.4)
        return sent

    async def _resolve_channel(self, guild_id: int) -> discord.abc.Messageable | None:
        channel_id = await self.store.get_channel(guild_id) or self.default_channel_id
        if not channel_id:
            if guild_id not in self._missing_channel_warned:
                log.warning(
                    "Guild %s has no leak alert channel configured (use /leak channel)", guild_id
                )
                self._missing_channel_warned.add(guild_id)
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                log.warning("Leak alert channel %s not accessible", channel_id)
                return None
        if not isinstance(channel, discord.abc.Messageable):  # pragma: no cover
            return None
        return channel

    def build_alert_embed(self, hit: LeakHit) -> discord.Embed:
        kind = hit.target.kind_enum
        display = (
            mask_secret(hit.target.value)
            if kind in (TargetKind.EMAIL, TargetKind.PHONE, TargetKind.USERNAME)
            else hit.target.value
        )
        found = format_ts(utcnow(), "R")
        emb = embed(
            f"{hit.severity.emoji} New leak signal · {hit.source.upper()}",
            truncate(hit.snippet or hit.title, max(280, self.settings.snippet_chars)),
            color=SEVERITY_COLORS[hit.severity],
            fields=[
                ("Monitored target", f"`{display}` ({kind.value})", True),
                ("Severity", hit.severity.value.upper(), True),
                ("Source", hit.source, True),
                ("Matched", f"`{truncate(hit.matched_value, 120)}`", True),
                ("Detected", found, True),
                ("Reference", truncate(hit.title, 200), False),
            ],
            url=hit.url or None,
            footer=f"{DISCLAIMER}",
        )
        if hit.url:
            emb.add_field(name="Link", value=f"[Open source]({hit.url})", inline=False)
        return emb

    # -- reporting ---------------------------------------------------------
    async def guild_status(self, guild_id: int) -> dict[str, object]:
        states = await self.store.get_all_source_states()
        return {
            "targets": await self.store.count_targets(guild_id),
            "hits": await self.store.count_hits(guild_id),
            "sources": {name: state.__dict__ for name, state in states.items()},
            "channel_id": await self.store.get_channel(guild_id),
            "last_scan": self.last_scan_at,
            "source_health": [
                {
                    "name": source.name,
                    "available": source.available,
                    "reason": source.reason_unavailable,
                    "cooldown": source.min_interval,
                }
                for source in self.sources
            ],
        }


def summarize_scan(report: ScanReport) -> str:
    lines = [
        f"**Sources scanned:** {report.scanned_sources}",
        f"**Documents checked:** {report.raw_documents}",
        f"**New hits:** {report.new_hits}",
        f"**Duplicates skipped:** {report.duplicates}",
        f"**Alerts sent:** {report.alerts_sent}",
    ]
    if report.skipped_sources:
        lines.append("**Skipped:** " + ", ".join(report.skipped_sources[:6]))
    if report.errors:
        lines.append("**Errors:** " + "; ".join(report.errors[:4]))
    return "\n".join(lines)


def build_status_embed(status: dict[str, object]) -> discord.Embed:
    sources = status.get("source_health") or []
    rows = []
    for source in sources:  # type: ignore[assignment]
        mark = "🟢" if source["available"] else "⚪"
        detail = "" if source["available"] else f" — {source['reason']}"
        rows.append(f"{mark} `{source['name']}`{detail}")
    states = status.get("sources") or {}
    state_rows = []
    for name, state in states.items():  # type: ignore[union-attr]
        last = state.get("last_run") or 0
        if last:
            from datetime import datetime, timezone

            state_rows.append(
                f"`{name}` last run {format_ts(datetime.fromtimestamp(last, timezone.utc), 'R')}"
            )
        if state.get("last_error"):
            state_rows.append(f"`{name}` warning: {truncate(state['last_error'], 120)}")
    channel = status.get("channel_id")
    channel_line = (
        f"**Alert channel:** <#{channel}>" if channel else "**Alert channel:** not set (use `/leak channel`)"
    )
    description = (
        f"**Targets:** {status.get('targets', 0)}\n"
        f"**Total hits:** {status.get('hits', 0)}\n"
        f"{channel_line}"
    )
    return embed(
        "🔍 Leak monitor status",
        description,
        color=LEAK,
        fields=[
            ("Sources", "\n".join(rows) or "none", False),
            ("Last activity", "\n".join(state_rows[:8]) or "No scan yet", False),
        ],
    )


def no_targets_embed() -> discord.Embed:
    return error_embed(
        "No watch targets yet. Add one with `/leak add value:acme.com kind:domain`.",
        title="Watchlist is empty",
    )
