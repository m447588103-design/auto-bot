"""Prefix and slash commands for the AI companion and smart Q&A."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.discord_ui import error_embed
from core.utils import split_message
from features.companion.render import (
    ask_embeds,
    ask_help_embed,
    gf_help_embed,
    gf_reset_embed,
    why_no_answer_embed,
)
from features.companion.service import CompanionError, CompanionService

log = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
RESET_WORDS = {"reset", "clear", "forget"}
HELP_WORDS = {"help", "?", "commands"}


def _private_ephemeral(interaction: discord.Interaction, private: bool) -> bool:
    """Ephemeral replies only exist in guilds; a DM is already private."""
    return bool(private and getattr(interaction, "guild", None) is not None)


def _dm_contexts(func):
    """Allow a companion command in guilds and one-to-one DMs."""
    func = app_commands.allowed_contexts(guilds=True, dms=True, private_channels=False)(func)
    return app_commands.allowed_installs(guilds=True, users=True)(func)


class CompanionCog(commands.Cog, name="Companion"):
    """A wholesome AI girlfriend and a Gemini-powered Q&A assistant."""

    def __init__(self, bot: commands.Bot, service: CompanionService):
        self.bot = bot
        self.service = service

    async def cog_load(self) -> None:
        await self.service.setup()
        if not self._memory_cleanup.is_running():
            self._memory_cleanup.start()

    def cog_unload(self) -> None:
        if self._memory_cleanup.is_running():
            self._memory_cleanup.cancel()

    @tasks.loop(hours=12)
    async def _memory_cleanup(self) -> None:
        try:
            await self.service.purge()
        except Exception:
            log.warning("Companion memory cleanup failed", exc_info=True)

    @_memory_cleanup.before_loop
    async def _before_memory_cleanup(self) -> None:
        try:
            await self.bot.wait_until_ready()
        except RuntimeError:
            # Offline command-registration tests never log in; still run once.
            return

    # -- prefix commands ---------------------------------------------------
    @commands.command(name="gf", help="Chat with your AI girlfriend")
    async def gf_prefix(self, ctx: commands.Context, *, message: str = "") -> None:
        await self._prefix_gf(ctx, message)

    @commands.command(name="gfreset", help="Clear your AI companion chat memory")
    async def gfreset_prefix(self, ctx: commands.Context) -> None:
        cleared = await self.service.reset_memory(ctx.author.id)
        await ctx.reply(
            embed=gf_reset_embed(self.service.gf_name, cleared),
            mention_author=False,
            allowed_mentions=NO_MENTIONS,
        )

    @commands.command(name="ask", help="Ask Gemini for a detailed answer")
    async def ask_prefix(self, ctx: commands.Context, *, question: str = "") -> None:
        if not (question or "").strip():
            await ctx.reply(embed=ask_help_embed(), mention_author=False)
            return
        async with ctx.typing():
            reply = await self.service.ask(
                ctx.author.id, getattr(ctx.author, "display_name", "friend"), question
            )
        embeds = ask_embeds(question, reply.text, reply.model)
        await ctx.reply(
            embed=embeds[0], mention_author=False, allowed_mentions=NO_MENTIONS
        )
        for emb in embeds[1:]:
            await ctx.send(embed=emb, allowed_mentions=NO_MENTIONS)

    async def _prefix_gf(self, ctx: commands.Context, message: str) -> None:
        text = (message or "").strip()
        lowered = text.lower()
        if not text or lowered in HELP_WORDS:
            await ctx.reply(embed=gf_help_embed(self.service.gf_name), mention_author=False)
            return
        if lowered in RESET_WORDS:
            cleared = await self.service.reset_memory(ctx.author.id)
            await ctx.reply(
                embed=gf_reset_embed(self.service.gf_name, cleared),
                mention_author=False,
                allowed_mentions=NO_MENTIONS,
            )
            return
        async with ctx.typing():
            reply = await self.service.gf_chat(
                ctx.author.id, getattr(ctx.author, "display_name", "friend"), text
            )
        chunks = split_message(reply.text, 1900)
        if not chunks:
            raise CompanionError("💖 She had nothing to say — try again.")
        await ctx.reply(chunks[0], mention_author=False, allowed_mentions=NO_MENTIONS)
        for chunk in chunks[1:]:
            await ctx.send(chunk, allowed_mentions=NO_MENTIONS)

    # -- slash commands ----------------------------------------------------
    @app_commands.command(name="gf", description="Chat with your AI girlfriend")
    @_dm_contexts
    @app_commands.describe(
        message="What you want to say to her",
        private="Only you can see the reply (default: everyone in the channel)",
    )
    async def gf_slash(
        self,
        interaction: discord.Interaction,
        message: str = "",
        private: bool = False,
    ) -> None:
        text = (message or "").strip()
        lowered = text.lower()
        if not text or lowered in HELP_WORDS:
            await interaction.response.send_message(
                embed=gf_help_embed(self.service.gf_name),
                ephemeral=_private_ephemeral(interaction, True),
            )
            return
        if lowered in RESET_WORDS:
            cleared = await self.service.reset_memory(interaction.user.id)
            await interaction.response.send_message(
                embed=gf_reset_embed(self.service.gf_name, cleared),
                ephemeral=_private_ephemeral(interaction, True),
            )
            return

        ephemeral = _private_ephemeral(interaction, private)
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)
        reply = await self.service.gf_chat(
            interaction.user.id,
            getattr(interaction.user, "display_name", "friend"),
            text,
        )
        chunks = split_message(reply.text, 1900)
        if not chunks:
            raise CompanionError("💖 She had nothing to say — try again.")
        await interaction.followup.send(
            chunks[0], ephemeral=ephemeral, allowed_mentions=NO_MENTIONS
        )
        for chunk in chunks[1:]:
            await interaction.followup.send(
                chunk, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS
            )

    @app_commands.command(name="gfreset", description="Clear your AI companion chat memory")
    @_dm_contexts
    async def gfreset_slash(self, interaction: discord.Interaction) -> None:
        ephemeral = _private_ephemeral(interaction, True)
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)
        cleared = await self.service.reset_memory(interaction.user.id)
        await interaction.followup.send(
            embed=gf_reset_embed(self.service.gf_name, cleared), ephemeral=ephemeral
        )

    @app_commands.command(name="ask", description="Ask a question and get a detailed answer")
    @_dm_contexts
    @app_commands.describe(
        question="Any question — coding, science, study help, everyday life",
        private="Only you can see the answer (default: everyone in the channel)",
    )
    async def ask_slash(
        self,
        interaction: discord.Interaction,
        question: str,
        private: bool = False,
    ) -> None:
        ephemeral = _private_ephemeral(interaction, private)
        await interaction.response.defer(thinking=True, ephemeral=ephemeral)
        reply = await self.service.ask(
            interaction.user.id,
            getattr(interaction.user, "display_name", "friend"),
            question,
        )
        embeds = ask_embeds(question, reply.text, reply.model)
        await interaction.followup.send(
            embed=embeds[0], ephemeral=ephemeral, allowed_mentions=NO_MENTIONS
        )
        for emb in embeds[1:]:
            await interaction.followup.send(
                emb, ephemeral=ephemeral, allowed_mentions=NO_MENTIONS
            )

    # -- errors ------------------------------------------------------------
    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, CompanionError):
            message = str(original)
            emb = (
                why_no_answer_embed()
                if "GEMINI_API_KEY" in message
                else error_embed(message, title="💖 Companion")
            )
            await ctx.reply(embed=emb, mention_author=False)
            return
        if isinstance(original, commands.MissingRequiredArgument):
            return
        log.exception("Companion prefix command failed", exc_info=original)
        await ctx.reply(
            embed=error_embed("Something unexpected happened — please try again."),
            mention_author=False,
        )

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, CompanionError):
            message = str(original)
            emb = (
                why_no_answer_embed()
                if "GEMINI_API_KEY" in message
                else error_embed(message, title="💖 Companion")
            )
        else:
            log.exception("Companion app command failed", exc_info=original)
            emb = error_embed("Something unexpected happened — please try again.")
        try:
            ephemeral = _private_ephemeral(interaction, True)
            if interaction.response.is_done():
                await interaction.followup.send(embed=emb, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(embed=emb, ephemeral=ephemeral)
        except discord.HTTPException:
            log.warning("Could not send companion error response", exc_info=True)


async def setup(bot: commands.Bot):
    service: CompanionService | None = getattr(bot, "companion", None)
    if service is None:
        raise RuntimeError(
            "CompanionService was not attached to the bot before loading features.companion.cog"
        )
    await bot.add_cog(CompanionCog(bot, service))
