"""Loads every cog into a real (offline) bot to catch registration regressions."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import discord

from core.config import settings
from main import EXTENSIONS, AutoBot


async def test_all_extensions_load_and_register_commands(tmp_path: Path):
    config = dataclasses.replace(
        settings,
        db_path=tmp_path / "test.db",
        data_dir=tmp_path,
    )
    bot = AutoBot(config)
    await bot.db.connect()
    await bot.leak_monitor.setup()
    await bot.price_alerts.setup()
    try:
        for extension in EXTENSIONS:
            await bot.load_extension(extension)

        cog_names = {cog.qualified_name for cog in bot.cogs.values()}
        assert {"Assistant", "Leak Monitor", "Cinematic Director", "AI DJ", "Market Sniper"} <= cog_names

        tree = bot.tree
        for group in ("leak", "movie", "dj", "sniper"):
            command = tree.get_command(group)
            assert command is not None, f"/{group} group missing"
        for root in ("doctor", "help", "features"):
            assert tree.get_command(root) is not None, f"/{root} missing"

        leak = tree.get_command("leak")
        assert {"add", "remove", "list", "scan", "hits", "channel", "status"} <= {
            cmd.name for cmd in leak.commands
        }
        movie = tree.get_command("movie")
        assert {"create", "preview", "status", "cancel"} <= {cmd.name for cmd in movie.commands}
        dj = tree.get_command("dj")
        assert {"play", "add", "queue", "skip", "stop", "pause", "resume", "volume", "loop", "leave"} <= {
            cmd.name for cmd in dj.commands
        }
        sniper = tree.get_command("sniper")
        assert {"hype", "coin", "analyze", "movers", "newpairs", "stock", "watch", "alerts"} <= {
            cmd.name for cmd in sniper.commands
        }

        # prefix commands still exist for backwards compatibility
        assert bot.get_command("agent") is not None
        assert bot.get_command("doctor") is not None
    finally:
        for extension in reversed(EXTENSIONS):
            try:
                await bot.unload_extension(extension)
            except Exception:
                pass
        await bot.db.close()


def test_bot_uses_expected_intents():
    bot = AutoBot(settings)
    assert bot.intents.message_content is True
    assert bot.intents.voice_states is True
    assert isinstance(bot, discord.Client)
    assert bot.command_prefix == settings.discord.command_prefix
