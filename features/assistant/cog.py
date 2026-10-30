"""Legacy ``!agent`` assistant plus /help and /doctor."""

from __future__ import annotations

import logging
import platform
import time

import discord
from discord import app_commands
from discord.ext import commands

from agent import process_agent_command
from core.config import settings
from core.discord_ui import BRAND, GUILD_ONLY_NOTE, embed, error_embed
from core.utils import split_message

log = logging.getLogger(__name__)

FEATURE_GUIDE = [
    (
        "🕵️ Leak monitor",
        "`/leak add` · `/leak list` · `/leak scan` · `/leak hits` · `/leak channel` · `/leak status`",
    ),
    (
        "🎬 Cinematic director",
        "`/movie create` · `/movie preview` · `/movie status` · `/movie cancel`",
    ),
    (
        "🎧 AI DJ",
        "`/dj play` · `/dj add` · `/dj queue` · `/dj skip` · `/dj stop` · `/dj volume` · `/dj loop` · `/dj autoplay` · `/dj leave`",
    ),
    (
        "🚀 Market sniper",
        "`/sniper hype` · `/sniper coin` · `/sniper analyze` · `/sniper movers` · `/sniper newpairs` · `/sniper stock` · `/sniper watch` · `/sniper alerts`",
    ),
    (
        "🎮 AI games",
        "`/rpg` · `/game rpg` · `/game choose` · `/detective` · `/game interrogate` · `/game accuse` · `/bossfight` · `/game attack` · `/game spell` · `/game heal` · `/game join`",
    ),
]


class AssistantCog(commands.Cog, name="Assistant"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # -- prefix commands ---------------------------------------------------
    @commands.command(name="agent", help="Ask the general AI assistant anything")
    async def agent_cmd(self, ctx: commands.Context, *, prompt: str = "") -> None:
        if not prompt:
            await ctx.reply("Usage: `!agent <question>`. Example: `!agent explain Rust ownership`.")
            return
        async with ctx.typing():
            response = await process_agent_command(prompt)
        for chunk in split_message(response, 1900):
            await ctx.send(chunk)

    @commands.command(name="doctor", help="Check bot configuration and dependencies")
    async def doctor_prefix(self, ctx: commands.Context) -> None:
        await ctx.reply(embed=self._doctor_embed())

    @commands.command(name="ping", help="Show gateway latency")
    async def ping(self, ctx: commands.Context) -> None:
        started = time.perf_counter()
        message = await ctx.reply("Pinging…")
        latency = (time.perf_counter() - started) * 1000
        await message.edit(content=f"🏓 Gateway: **{self.bot.latency * 1000:.0f} ms** · round-trip: **{latency:.0f} ms**")

    # -- slash commands ----------------------------------------------------
    @app_commands.command(name="doctor", description="Check API keys, ffmpeg, database and services")
    async def doctor(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        await interaction.followup.send(embed=self._doctor_embed(), ephemeral=True)

    @app_commands.command(name="help", description="Show every AutoBot command")
    async def help_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        emb = embed(
            "🤖 AutoBot — AI automation suite",
            "Four production-grade automations plus a Gemini-powered games suite.\n"
            f"Prefix commands also work with `{settings.discord.command_prefix}` "
            f"(e.g. `{settings.discord.command_prefix}agent`, "
            f"`{settings.discord.command_prefix}rpg`).",
            color=BRAND,
            fields=[(name, value, False) for name, value in FEATURE_GUIDE],
            footer="AutoBot • use cautiously: leak monitoring and market tools are informational only",
        )
        await interaction.followup.send(embed=emb, ephemeral=True)

    @app_commands.command(name="features", description="What can this bot do?")
    async def features(self, interaction: discord.Interaction) -> None:
        emb = embed(
            "✨ Feature overview",
            "\n\n".join(f"**{name}**\n{value}" for name, value in FEATURE_GUIDE),
            color=BRAND,
            footer=GUILD_ONLY_NOTE,
        )
        await interaction.response.send_message(embed=emb)

    # -- shared ------------------------------------------------------------
    def _doctor_embed(self) -> discord.Embed:
        summary = settings.summary()
        pipeline = getattr(self.bot, "movie_pipeline", None)
        preflight = pipeline.preflight() if pipeline else None

        lines = [f"`{key:<14}` {value}" for key, value in summary.items()]
        checks = []
        checks.append(("Database", "✅ connected" if getattr(self.bot, "db", None) and self.bot.db.connected else "❌ not connected"))
        checks.append(("Gateway latency", f"{self.bot.latency * 1000:.0f} ms" if self.bot.latency else "n/a"))
        checks.append(("Guilds", str(len(self.bot.guilds))))
        checks.append(("Python", platform.python_version()))
        if preflight:
            checks.append(("ffmpeg", "✅" if preflight.ffmpeg else "❌ missing"))
            checks.append(("ffprobe", "✅" if preflight.ffprobe else "❌ missing"))
            checks.append(("TTS", preflight.tts or "❌ none"))
            checks.append(("Free disk", f"{preflight.disk_free_mb:.0f} MB"))
            if preflight.problems:
                checks.append(("Blocking", "; ".join(preflight.problems)))

        emb = embed(
            "🩺 AutoBot doctor",
            "\n".join(lines),
            color=BRAND,
            fields=[
                ("Health", "\n".join(f"**{name}:** {value}" for name, value in checks), False),
                (
                    "Environment variables",
                    "Copy `.env.example` → `.env` and restart the bot after changing keys.",
                    False,
                ),
            ],
            footer="Secrets are never displayed",
        )
        return emb

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.exception("Assistant command error", exc_info=error)
        message = "Unexpected error — please try again later."
        if interaction.response.is_done():
            await interaction.followup.send(embed=error_embed(message), ephemeral=True)
        else:
            await interaction.response.send_message(embed=error_embed(message), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AssistantCog(bot))
