"""AutoBot entry point.

Boots the Discord bot, attaches the feature services and starts the background
workers (leak scanner + price alerts).

Run with::

    python main.py
"""

from __future__ import annotations

import asyncio
import logging
import sys

import discord
from discord.ext import commands

from core.config import Settings, settings
from core.db import Database
from core.gemini import get_gemini, thinking_budget_for_model, thinking_level_for_model
from core.logging_setup import setup_logging
from features.cinematic.pipeline import MoviePipeline
from features.companion.service import CompanionService
from features.dj.curator import DJCurator
from features.games.service import GameService
from features.leak_monitor.monitor import LeakMonitor
from features.leak_monitor.store import LeakStore
from features.sniper.alerts import PriceAlertService
from features.sniper.analyst import MarketAnalyst
from features.sniper.market import MarketData
from features.sniper.sentiment import HypeEngine

log = logging.getLogger("autobot")

EXTENSIONS = (
    "features.assistant.cog",
    "features.leak_monitor.cog",
    "features.cinematic.cog",
    "features.dj.cog",
    "features.sniper.cog",
    "features.games.cog",
    "features.companion.cog",
)


class AutoBot(commands.Bot):
    def __init__(self, config: Settings):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        if config.discord.enable_members_intent:
            intents.members = True

        super().__init__(
            command_prefix=config.discord.command_prefix,
            intents=intents,
            help_command=None,
            case_insensitive=True,
        )
        self.config = config

        # shared storage
        self.db = Database(config.db_path)

        # feature services (attached before extensions load)
        self.leak_monitor = LeakMonitor(
            self,
            LeakStore(self.db),
            config.leak,
            default_channel_id=config.discord.default_alert_channel_id,
        )
        self.movie_pipeline = MoviePipeline(config.cinematic)
        self.dj_curator = DJCurator(config.dj)
        self.games = GameService(self, self.db, config.games)
        self.companion = CompanionService(self, self.db, config.companion)
        self.market_data = MarketData(config.sniper)
        self.hype_engine = HypeEngine(config.sniper)
        self.market_analyst = MarketAnalyst()
        self.price_alerts = PriceAlertService(self, self.db, config.sniper, self.market_data)

    # -- lifecycle ---------------------------------------------------------
    async def setup_hook(self) -> None:
        self.config.ensure_dirs()
        await self.db.connect()
        await self.leak_monitor.setup()
        await self.price_alerts.setup()

        for extension in EXTENSIONS:
            try:
                await self.load_extension(extension)
                log.info("Loaded extension %s", extension)
            except Exception:
                log.exception("Failed to load extension %s", extension)

        await self._sync_commands()

        if self.config.leak.enabled:
            await self.leak_monitor.start()
        if self.config.sniper.enabled:
            await self.price_alerts.start()

    async def _sync_commands(self) -> None:
        # Global commands are the set Discord uses in DMs. Always sync them,
        # even while developing against a guild, otherwise /gf cannot work in
        # a DM. Discord may take a little while to publish globally.
        try:
            synced = await self.tree.sync()
            log.info("Synced %s global slash commands (guilds + DMs)", len(synced))
        except discord.HTTPException:
            log.exception(
                "Global slash command sync failed (DM commands may be stale; "
                "check the bot's applications.commands scope or try again later)"
            )
        if not self.config.discord.dev_guild_ids:
            return
        try:
            for guild_id in self.config.discord.dev_guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %s commands to guild %s", len(synced), guild_id)
        except discord.HTTPException:
            log.exception("Guild slash command sync failed for %s", self.config.discord.dev_guild_ids)

    async def on_ready(self) -> None:
        log.info("Logged in as %s (id=%s)", self.user, getattr(self.user, "id", "?"))
        log.info("Serving %s guild(s)", len(self.guilds))
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="/gf /ask · /game /rpg /detective /bossfight",
            ),
            status=discord.Status.online,
        )

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply(
                f"Missing argument: `{error.param.name}`. Try `{self.config.discord.command_prefix}help`."
            )
            return
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.reply(f"Cooldown: try again in {error.retry_after:.0f}s.")
            return
        log.exception("Prefix command error", exc_info=error)
        await ctx.reply("Something went wrong. Check the bot logs for details.")

    async def close(self) -> None:
        log.info("Shutting down AutoBot…")
        try:
            await self.leak_monitor.stop()
            await self.price_alerts.stop()
        finally:
            await self.db.close()
        await super().close()


async def amain() -> None:
    bot = AutoBot(settings)
    async with bot:
        await bot.start(settings.discord.token or "")


def main() -> None:
    settings.ensure_dirs()
    setup_logging(settings.log_level, settings.data_dir / "logs")

    if not settings.discord.token:
        print(
            "ERROR: DISCORD_BOT_TOKEN is not set.\n"
            "Copy .env.example to .env and fill in your bot token, then run again.",
            file=sys.stderr,
        )
        raise SystemExit(1)

    log.info("Starting AutoBot (environment=%s)", settings.environment)
    for name, status in settings.summary().items():
        log.info("  %-14s : %s", name, status)

    gemini = get_gemini()
    level = thinking_level_for_model(gemini.model)
    budget = thinking_budget_for_model(gemini.model)
    thinking = f"level={level}" if level else ("budget=0" if budget == 0 else "default")
    log.info(
        "Gemini model=%s backend=%s available=%s thinking=%s",
        gemini.model,
        gemini.backend or "none",
        gemini.available,
        thinking,
    )

    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        log.info("Interrupted by user")
        sys.exit(0)


if __name__ == "__main__":
    main()
