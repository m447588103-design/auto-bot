"""Discord slash commands for the leak monitor."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from core.discord_ui import BRAND, LEAK, embed, error_embed, list_embed, success_embed
from core.utils import format_ts, mask_secret, truncate

from .matcher import InvalidTarget, infer_kind, validate_target
from .models import TargetKind, WatchTarget
from .monitor import LeakMonitor, build_status_embed, no_targets_embed, summarize_scan

log = logging.getLogger(__name__)

def _kind_label(kind: TargetKind) -> str:
    return {
        TargetKind.EMAIL: "📧 email",
        TargetKind.DOMAIN: "🌐 domain",
        TargetKind.KEYWORD: "🔎 keyword",
        TargetKind.PHONE: "📱 phone",
        TargetKind.USERNAME: "👤 username",
    }[kind]


class LeakMonitorCog(commands.Cog, name="Leak Monitor"):
    """Dark-web / breach monitoring with Discord alerts."""

    leak = app_commands.Group(
        name="leak",
        description="Monitor emails, domains and keywords for breaches and leaks",
    )

    def __init__(self, bot: commands.Bot, monitor: LeakMonitor):
        self.bot = bot
        self.monitor = monitor

    # -- helpers -----------------------------------------------------------
    @staticmethod
    async def _guild_guard(interaction: discord.Interaction) -> bool:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                embed=error_embed("This command only works inside a server."), ephemeral=True
            )
            return False
        return True

    @staticmethod
    async def _require_manage_guild(interaction: discord.Interaction) -> bool:
        member = interaction.user
        if (
            interaction.guild is not None
            and isinstance(member, discord.Member)
            and member.guild_permissions.manage_guild
        ):
            return True
        message = "You need the **Manage Server** permission to do that."
        if interaction.response.is_done():
            await interaction.followup.send(embed=error_embed(message), ephemeral=True)
        else:
            await interaction.response.send_message(embed=error_embed(message), ephemeral=True)
        return False

    # -- commands ----------------------------------------------------------
    @leak.command(name="add", description="Add an email, domain or keyword to the watchlist")
    @app_commands.describe(
        value="Email, domain (acme.com), keyword, phone or @username to monitor",
        kind="Auto-detected when omitted",
        label="Optional human friendly name, e.g. 'Main company domain'",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        value: str,
        kind: TargetKind | None = None,
        label: str | None = None,
    ) -> None:
        if not await self._guild_guard(interaction):
            return
        if not await self._require_manage_guild(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            resolved_kind = kind or infer_kind(value)
            cleaned = validate_target(value, resolved_kind)
        except InvalidTarget as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return

        target, created = await self.monitor.store.add_target(
            interaction.guild_id,  # type: ignore[arg-type]
            cleaned,
            resolved_kind,
            label=truncate(label, 80) if label else None,
            created_by=interaction.user.id,
        )
        sources = [
            source.name
            for source in self.monitor.sources
            if source.available and source.name in {"hibp", "hudsonrock", "ransomwatch", "psbdmp", "github"}
        ]
        if not created:
            await interaction.followup.send(
                embed=embed(
                    "Already monitored",
                    f"`{_masked(target)}` (`#{target.id}`) is already on the watchlist.",
                    color=BRAND,
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=success_embed(
                f"Now monitoring `{_masked(target)}` as **{resolved_kind.value}**.\n"
                f"Target id: `#{target.id}`\n"
                f"Active sources: {', '.join(sources) or 'none configured'}\n\n"
                "Alerts will be posted as soon as a match is found. "
                "Run `/leak scan` to check immediately.",
                title="Watch target added",
            ),
            ephemeral=True,
        )
        log.info(
            "Guild %s added leak target %s (%s) by %s",
            interaction.guild_id,
            target.value,
            target.kind,
            interaction.user,
        )

    @leak.command(name="remove", description="Remove a target from the watchlist")
    @app_commands.describe(target_id="The id shown by /leak list")
    async def remove(self, interaction: discord.Interaction, target_id: int) -> None:
        if not await self._guild_guard(interaction):
            return
        if not await self._require_manage_guild(interaction):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        removed = await self.monitor.store.remove_target(interaction.guild_id, target_id)  # type: ignore[arg-type]
        if removed:
            await interaction.followup.send(
                embed=success_embed(f"Target `#{target_id}` removed.", title="Watchlist updated"),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=error_embed(
                    f"I could not find target `#{target_id}` in this server.", title="Not found"
                ),
                ephemeral=True,
            )

    @leak.command(name="list", description="Show every monitored target in this server")
    async def list_targets(self, interaction: discord.Interaction) -> None:
        if not await self._guild_guard(interaction):
            return
        await interaction.response.defer(thinking=True)
        targets = await self.monitor.store.list_targets(interaction.guild_id)  # type: ignore[arg-type]
        if not targets:
            await interaction.followup.send(embed=no_targets_embed())
            return
        rows = [
            f"`#{target.id}` {_kind_label(target.kind_enum)} "
            f"`{_masked(target)}`"
            + (f" — {truncate(target.label, 40)}" if target.label else "")
            + f" · added {format_ts(_parse(target.created_at), 'd')}"
            for target in targets
        ]
        await interaction.followup.send(
            embed=list_embed(
                f"🔍 Watchlist ({len(targets)})",
                rows,
                color=LEAK,
                description="Alerts are posted to the configured alert channel.",
            )
        )

    @leak.command(name="scan", description="Run an immediate leak scan for this server")
    @app_commands.checks.cooldown(1, 120.0, key=lambda i: i.guild_id)
    async def scan(self, interaction: discord.Interaction) -> None:
        if not await self._guild_guard(interaction):
            return
        if not await self._require_manage_guild(interaction):
            return
        await interaction.response.defer(thinking=True)
        targets = await self.monitor.store.list_targets(interaction.guild_id)  # type: ignore[arg-type]
        if not targets:
            await interaction.followup.send(embed=no_targets_embed())
            return
        report = await self.monitor.scan(guild_id=interaction.guild_id, force=True)
        await interaction.followup.send(
            embed=embed(
                "🔎 Manual leak scan finished",
                summarize_scan(report),
                color=LEAK if report.new_hits == 0 else discord.Color.from_str("#ED4245"),
            )
        )

    @leak.command(name="hits", description="Show the most recent leak hits")
    @app_commands.describe(limit="How many hits to show (1-25)")
    async def hits(self, interaction: discord.Interaction, limit: int = 10) -> None:
        if not await self._guild_guard(interaction):
            return
        await interaction.response.defer(thinking=True)
        rows = await self.monitor.store.recent_hits(interaction.guild_id, limit)  # type: ignore[arg-type]
        if not rows:
            await interaction.followup.send(
                embed=embed(
                    "No hits yet",
                    "Nothing has matched your watchlist so far. That is good news!",
                    color=BRAND,
                )
            )
            return
        lines = []
        for row in rows:
            target = row.get("target_value") or "unknown"
            lines.append(
                f"`{row['severity'].upper():<8}` **{truncate(row['title'], 70)}**\n"
                f"↳ `{truncate(target, 50)}` · {row['source']} · {format_ts(_parse(row['found_at']), 'R')}"
                + (f" · [source]({row['url']})" if row.get("url") else "")
            )
        await interaction.followup.send(
            embed=list_embed(
                f"🚨 Recent leak hits ({len(rows)})",
                lines,
                color=LEAK,
                per_field=5,
            )
        )

    @leak.command(name="channel", description="Set the channel that receives leak alerts")
    @app_commands.describe(channel="Text channel for alerts (defaults to this channel)")
    async def channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        if not await self._guild_guard(interaction):
            return
        if not await self._require_manage_guild(interaction):
            return
        target_channel = channel or interaction.channel
        if not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message(
                embed=error_embed("Please pick a regular text channel."), ephemeral=True
            )
            return
        me = getattr(interaction.guild, "me", None)
        if me is not None and not target_channel.permissions_for(me).send_messages:
            await interaction.response.send_message(
                embed=error_embed(f"I cannot send messages in {target_channel.mention}."),
                ephemeral=True,
            )
            return
        await self.monitor.store.set_channel(interaction.guild_id, target_channel.id)  # type: ignore[arg-type]
        await interaction.response.send_message(
            embed=success_embed(
                f"Leak alerts will be posted in {target_channel.mention}.",
                title="Alert channel updated",
            ),
            ephemeral=True,
        )

    @leak.command(name="status", description="Show monitor health, sources and counters")
    async def status(self, interaction: discord.Interaction) -> None:
        if not await self._guild_guard(interaction):
            return
        await interaction.response.defer(thinking=True)
        data = await self.monitor.guild_status(interaction.guild_id)  # type: ignore[arg-type]
        await interaction.followup.send(embed=build_status_embed(data))

    # -- error handling ----------------------------------------------------
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"Slow down — try again in **{error.retry_after:.0f}s**."
        elif isinstance(error, app_commands.MissingPermissions):
            message = "You need the **Manage Server** permission for that."
        elif isinstance(error, app_commands.CheckFailure):
            message = "You cannot use this command."
        else:  # pragma: no cover
            log.exception("Leak command error", exc_info=error)
            message = "Unexpected error, check the bot logs."
        if interaction.response.is_done():
            await interaction.followup.send(embed=error_embed(message), ephemeral=True)
        else:
            await interaction.response.send_message(embed=error_embed(message), ephemeral=True)


def _masked(target: WatchTarget) -> str:
    if target.kind_enum in (TargetKind.EMAIL, TargetKind.PHONE, TargetKind.USERNAME):
        return mask_secret(target.value)
    return truncate(target.value, 80)


def _parse(value: str | None):
    from core.utils import parse_iso

    return parse_iso(value)


async def setup(bot: commands.Bot) -> None:
    monitor: LeakMonitor | None = getattr(bot, "leak_monitor", None)
    if monitor is None:
        raise RuntimeError("LeakMonitor service was not attached to the bot")
    await bot.add_cog(LeakMonitorCog(bot, monitor))
