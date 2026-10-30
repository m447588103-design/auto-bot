"""Discord surface for the AI games: slash group, prefix commands, buttons."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from core.discord_ui import error_embed
from features.games.render import (
    adventure_embed,
    boss_embed,
    boss_turn_embed,
    detective_embed,
    game_status_embed,
    help_embed,
    interrogation_embed,
    verdict_embed,
)
from features.games.service import GameError, GameService
from features.games.views import build_view

log = logging.getLogger(__name__)

def _display_name(user: Any) -> str:
    return getattr(user, "display_name", None) or getattr(user, "name", "player")


class GamesCog(commands.Cog, name="AI Games"):
    """Interactive AI adventures, detective mysteries and boss raids."""

    game = app_commands.Group(
        name="game",
        description="Gemini-powered interactive games",
        guild_only=True,
    )

    def __init__(self, bot: commands.Bot, service: GameService):
        self.bot = bot
        self.service = service

    async def cog_load(self) -> None:
        await self.service.setup()
        await self._restore_views()
        self.cleanup.start()

    async def cog_unload(self) -> None:
        self.cleanup.cancel()

    async def _restore_views(self) -> None:
        """Re-register buttons for sessions that survived a restart."""
        try:
            states = await self.service.store.list_active(limit=200)
        except Exception:  # pragma: no cover - startup resilience
            log.warning("Could not restore game views", exc_info=True)
            return
        restored = 0
        for state in states:
            view = build_view(self, state)
            if view is None:
                continue
            try:
                self.bot.add_view(view)
                restored += 1
            except Exception:  # pragma: no cover - defensive
                log.warning("Failed to restore view for %s", state.id, exc_info=True)
        if restored:
            log.info("Restored %s persistent game view(s)", restored)

    @tasks.loop(hours=6)
    async def cleanup(self) -> None:
        try:
            removed = await self.service.purge()
            if removed:
                log.info("Game cleanup removed %s sessions", removed)
        except Exception:  # pragma: no cover - background resilience
            log.warning("Game cleanup failed", exc_info=True)

    @cleanup.before_loop
    async def _before_cleanup(self) -> None:
        await self.bot.wait_until_ready()

    # -- helpers -----------------------------------------------------------
    def _register(self, state: Any) -> discord.ui.View | None:
        view = build_view(self, state)
        if view is not None:
            try:
                self.bot.add_view(view)
            except Exception:  # pragma: no cover - defensive
                log.debug("Could not register view for %s", state.id, exc_info=True)
        return view

    async def _fail(self, interaction: discord.Interaction, exc: Exception) -> None:
        if isinstance(exc, GameError):
            message = str(exc)
        else:
            log.error(
                "Games command failed: %s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            message = "🎲 The game hit a snag. Please try again in a moment."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass

    # -- button handlers ---------------------------------------------------
    async def handle_rpg_choice(
        self,
        interaction: discord.Interaction,
        session_id: str,
        scene_no: int,
        index: int,
    ) -> None:
        await interaction.response.defer()
        try:
            state = await self.service.choose(
                session_id, interaction.user.id, index, expected_scene=scene_no
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail(interaction, exc)
            return
        view = self._register(state)
        try:
            await interaction.edit_original_response(embed=adventure_embed(state), view=view)
        except discord.HTTPException:
            pass

    async def handle_boss_action(
        self,
        interaction: discord.Interaction,
        session_id: str,
        action: str,
    ) -> None:
        await interaction.response.defer()
        try:
            if action == "join":
                state, joined = await self.service.join_boss(
                    session_id, interaction.user.id, _display_name(interaction.user)
                )
                if not joined:
                    await interaction.followup.send(
                        "🛡️ You are already in this raid — hit **Attack**!", ephemeral=True
                    )
                    return
                await interaction.edit_original_response(
                    embed=boss_embed(state), view=self._register(state)
                )
                return
            turn = await self.service.boss_action(
                session_id, interaction.user.id, _display_name(interaction.user), action
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail(interaction, exc)
            return
        try:
            await interaction.edit_original_response(
                embed=boss_turn_embed(turn), view=self._register(turn.state)
            )
        except discord.HTTPException:
            pass

    # -- slash: utility ----------------------------------------------------
    @game.command(name="help", description="Show how to play the AI games")
    async def game_help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=help_embed(), ephemeral=True)

    @game.command(name="status", description="Show active games in this server")
    async def game_status(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        counts = await self.service.session_counts(interaction.guild.id)
        boss = await self.service.active_boss(interaction.guild.id)
        await interaction.response.send_message(
            embed=game_status_embed(
                counts,
                boss,
                enabled=self.service.settings.enabled,
                ai_available=self.service.ai_available,
            )
        )

    # -- slash: adventure --------------------------------------------------
    async def _start_rpg_slash(self, interaction: discord.Interaction, theme: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            state = await self.service.start_rpg(
                interaction.guild.id,
                interaction.channel_id or 0,
                interaction.user.id,
                _display_name(interaction.user),
                theme,
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail(interaction, exc)
            return
        await interaction.followup.send(
            embed=adventure_embed(state), view=self._register(state)
        )

    @app_commands.command(name="rpg", description="Start a choose-your-own-adventure story")
    @app_commands.describe(theme="Setting for your adventure (e.g. 'a haunted Sylhet tea garden')")
    async def rpg_slash(self, interaction: discord.Interaction, theme: str = "") -> None:
        await self._start_rpg_slash(interaction, theme)

    @game.command(name="rpg", description="Start a choose-your-own-adventure story")
    @app_commands.describe(theme="Setting for your adventure (e.g. 'a haunted Sylhet tea garden')")
    async def game_rpg(self, interaction: discord.Interaction, theme: str = "") -> None:
        await self._start_rpg_slash(interaction, theme)

    @game.command(name="choose", description="Pick a choice in your adventure")
    @app_commands.describe(number="Choice number (1-4)")
    async def game_choose(self, interaction: discord.Interaction, number: app_commands.Range[int, 1, 4]) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = await self.service.active_rpg(interaction.guild.id, interaction.user.id)
        if state is None:
            await interaction.response.send_message(
                "You have no active adventure. Start one with `/game rpg`.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        try:
            state = await self.service.choose(state.id, interaction.user.id, number - 1)
        except Exception as exc:  # noqa: BLE001
            await self._fail(interaction, exc)
            return
        await interaction.followup.send(
            embed=adventure_embed(state), view=self._register(state)
        )

    # -- slash: detective --------------------------------------------------
    async def _start_detective_slash(self, interaction: discord.Interaction, theme: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            state = await self.service.start_detective(
                interaction.guild.id,
                interaction.channel_id or 0,
                interaction.user.id,
                _display_name(interaction.user),
                theme,
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail(interaction, exc)
            return
        await interaction.followup.send(
            embed=detective_embed(state), view=self._register(state)
        )

    @app_commands.command(name="detective", description="Open an AI murder mystery for this channel")
    @app_commands.describe(theme="Setting for the case (e.g. 'a wedding in Old Dhaka')")
    async def detective_slash(self, interaction: discord.Interaction, theme: str = "") -> None:
        await self._start_detective_slash(interaction, theme)

    @game.command(name="detective", description="Open an AI murder mystery for this channel")
    @app_commands.describe(theme="Setting for the case (e.g. 'a wedding in Old Dhaka')")
    async def game_detective(self, interaction: discord.Interaction, theme: str = "") -> None:
        await self._start_detective_slash(interaction, theme)

    @game.command(name="interrogate", description="Question a suspect")
    @app_commands.describe(suspect="Suspect number (1-3)", question="What do you ask them?")
    async def game_interrogate(
        self,
        interaction: discord.Interaction,
        suspect: app_commands.Range[int, 1, 3],
        question: str,
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = await self.service.active_detective(
            interaction.guild.id, interaction.channel_id or 0
        )
        if state is None:
            await interaction.response.send_message(
                "No open case in this channel. Start one with `/game detective`.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        try:
            state, suspect_obj, reply = await self.service.interrogate(
                state.id,
                interaction.user.id,
                suspect - 1,
                question,
                _display_name(interaction.user),
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail(interaction, exc)
            return
        await interaction.followup.send(
            embed=interrogation_embed(
                state=state,
                suspect_name=suspect_obj.name,
                suspect_role=suspect_obj.role,
                question=question,
                answer=reply.answer,
                asked_by=_display_name(interaction.user),
                source=reply.source,
                index=suspect - 1,
            )
        )

    @game.command(name="accuse", description="Make your final accusation")
    @app_commands.describe(suspect="Suspect number (1-3)")
    async def game_accuse(
        self, interaction: discord.Interaction, suspect: app_commands.Range[int, 1, 3]
    ) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = await self.service.active_detective(
            interaction.guild.id, interaction.channel_id or 0
        )
        if state is None:
            await interaction.response.send_message(
                "No open case in this channel.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        try:
            state, correct, _ = await self.service.accuse(
                state.id, interaction.user.id, suspect - 1
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail(interaction, exc)
            return
        await interaction.followup.send(embed=verdict_embed(state, correct))

    # -- slash: boss -------------------------------------------------------
    async def _start_boss_slash(self, interaction: discord.Interaction, theme: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            state = await self.service.start_boss(
                interaction.guild.id,
                interaction.channel_id or 0,
                interaction.user.id,
                _display_name(interaction.user),
                theme,
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail(interaction, exc)
            return
        await interaction.followup.send(embed=boss_embed(state), view=self._register(state))

    @app_commands.command(name="bossfight", description="Summon a server-wide boss raid")
    @app_commands.describe(theme="Where the boss comes from (e.g. 'a drowned Sundarbans temple')")
    async def bossfight_slash(self, interaction: discord.Interaction, theme: str = "") -> None:
        await self._start_boss_slash(interaction, theme)

    @game.command(name="bossfight", description="Summon a server-wide boss raid")
    @app_commands.describe(theme="Where the boss comes from (e.g. 'a drowned Sundarbans temple')")
    async def game_bossfight(self, interaction: discord.Interaction, theme: str = "") -> None:
        await self._start_boss_slash(interaction, theme)

    @game.command(name="join", description="Join the active boss raid")
    async def game_join(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = await self.service.active_boss(interaction.guild.id)
        if state is None:
            await interaction.response.send_message(
                "No boss is attacking right now. Start one with `/game bossfight`.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)
        try:
            state, joined = await self.service.join_boss(
                state.id, interaction.user.id, _display_name(interaction.user)
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail(interaction, exc)
            return
        if not joined:
            await interaction.followup.send("🛡️ You are already in this raid.", ephemeral=True)
            return
        await interaction.followup.send(
            embed=boss_embed(state), view=self._register(state)
        )

    @game.command(name="attack", description="Strike the boss")
    async def game_attack(self, interaction: discord.Interaction) -> None:
        await self._slash_boss_action(interaction, "attack")

    @game.command(name="spell", description="Cast a heavy spell at the boss")
    async def game_spell(self, interaction: discord.Interaction) -> None:
        await self._slash_boss_action(interaction, "spell")

    @game.command(name="heal", description="Heal yourself during the raid")
    async def game_heal(self, interaction: discord.Interaction) -> None:
        await self._slash_boss_action(interaction, "heal")

    async def _slash_boss_action(self, interaction: discord.Interaction, action: str) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        state = await self.service.active_boss(interaction.guild.id)
        if state is None:
            await interaction.response.send_message(
                "No boss is attacking right now. Start one with `/game bossfight`.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(thinking=True)
        try:
            turn = await self.service.boss_action(
                state.id, interaction.user.id, _display_name(interaction.user), action
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail(interaction, exc)
            return
        await interaction.followup.send(
            embed=boss_turn_embed(turn), view=self._register(turn.state)
        )

    # -- slash: quit -------------------------------------------------------
    @game.command(name="quit", description="End your active adventure, case or raid")
    async def game_quit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return
        session = await self.service.active_rpg(interaction.guild.id, interaction.user.id)
        if session is None:
            session = await self.service.active_detective(
                interaction.guild.id, interaction.channel_id or 0
            )
            if session is not None and session.owner_id != interaction.user.id:
                session = None
        if session is None:
            boss = await self.service.active_boss(interaction.guild.id)
            if boss is not None and boss.owner_id == interaction.user.id:
                session = boss
        if session is None:
            await interaction.response.send_message(
                "You have no active game to end.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await self.service.end_session(session.id, interaction.user.id, interaction.guild.id)
        except Exception as exc:  # noqa: BLE001
            await self._fail(interaction, exc)
            return
        await interaction.followup.send("🛑 Game ended.", ephemeral=True)

    # -- prefix commands ---------------------------------------------------
    @commands.command(name="rpg", help="Start a choose-your-own-adventure story")
    @commands.guild_only()
    async def rpg_prefix(self, ctx: commands.Context, *, theme: str = "") -> None:
        message = await ctx.reply("🎲 *The dice are rolling…*")
        try:
            state = await self.service.start_rpg(
                ctx.guild.id,
                ctx.channel.id,
                ctx.author.id,
                _display_name(ctx.author),
                theme,
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail_prefix(message, exc)
            return
        await message.edit(embed=adventure_embed(state), view=self._register(state))

    @commands.command(name="choose", help="Choose an option in your adventure")
    @commands.guild_only()
    async def choose_prefix(self, ctx: commands.Context, number: int = 1) -> None:
        state = await self.service.active_rpg(ctx.guild.id, ctx.author.id)
        if state is None:
            await ctx.reply("You have no active adventure. Start one with `!rpg`.")
            return
        try:
            state = await self.service.choose(state.id, ctx.author.id, number - 1)
        except Exception as exc:  # noqa: BLE001
            await self._fail_prefix(ctx.message, exc)
            return
        await ctx.reply(embed=adventure_embed(state), view=self._register(state))

    @commands.command(name="detective", help="Open an AI murder mystery in this channel")
    @commands.guild_only()
    async def detective_prefix(self, ctx: commands.Context, *, theme: str = "") -> None:
        message = await ctx.reply("🕵️ *Opening the case file…*")
        try:
            state = await self.service.start_detective(
                ctx.guild.id,
                ctx.channel.id,
                ctx.author.id,
                _display_name(ctx.author),
                theme,
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail_prefix(message, exc)
            return
        await message.edit(embed=detective_embed(state), view=self._register(state))

    @commands.command(name="interrogate", help="Question a suspect: !interrogate 2 Where were you?")
    @commands.guild_only()
    async def interrogate_prefix(self, ctx: commands.Context, *, arguments: str = "") -> None:
        state = await self.service.active_detective(ctx.guild.id, ctx.channel.id)
        if state is None:
            await ctx.reply("No open case in this channel. Start one with `!detective`.")
            return
        index, question = GameService.resolve_suspect(state, arguments)
        if index is None:
            await ctx.reply(
                "Usage: `!interrogate <suspect number or name> <question>` "
                "— e.g. `!interrogate 2 Where were you?`"
            )
            return
        if not question:
            await ctx.reply("Add a question after the suspect.")
            return
        message = await ctx.reply("🎙️ *The suspect leans forward…*")
        try:
            state, suspect, reply = await self.service.interrogate(
                state.id, ctx.author.id, index, question, _display_name(ctx.author)
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail_prefix(message, exc)
            return
        await message.edit(
            embed=interrogation_embed(
                state=state,
                suspect_name=suspect.name,
                suspect_role=suspect.role,
                question=question,
                answer=reply.answer,
                asked_by=_display_name(ctx.author),
                source=reply.source,
                index=index,
            )
        )

    @commands.command(name="accuse", help="Accuse a suspect: !accuse 2")
    @commands.guild_only()
    async def accuse_prefix(self, ctx: commands.Context, *, arguments: str = "") -> None:
        state = await self.service.active_detective(ctx.guild.id, ctx.channel.id)
        if state is None:
            await ctx.reply("No open case in this channel.")
            return
        index, _ = GameService.resolve_suspect(state, arguments)
        if index is None:
            await ctx.reply("Usage: `!accuse <suspect number or name>`")
            return
        try:
            state, correct, _ = await self.service.accuse(state.id, ctx.author.id, index)
        except Exception as exc:  # noqa: BLE001
            await self._fail_prefix(ctx.message, exc)
            return
        await ctx.reply(embed=verdict_embed(state, correct))

    @commands.command(name="bossfight", help="Summon a server-wide boss raid")
    @commands.guild_only()
    async def bossfight_prefix(self, ctx: commands.Context, *, theme: str = "") -> None:
        message = await ctx.reply("⚔️ *Something enormous stirs…*")
        try:
            state = await self.service.start_boss(
                ctx.guild.id,
                ctx.channel.id,
                ctx.author.id,
                _display_name(ctx.author),
                theme,
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail_prefix(message, exc)
            return
        await message.edit(embed=boss_embed(state), view=self._register(state))

    @commands.command(name="join", help="Join the active boss raid")
    @commands.guild_only()
    async def join_prefix(self, ctx: commands.Context) -> None:
        state = await self.service.active_boss(ctx.guild.id)
        if state is None:
            await ctx.reply("No boss is attacking right now. Start one with `!bossfight`.")
            return
        try:
            state, joined = await self.service.join_boss(
                state.id, ctx.author.id, _display_name(ctx.author)
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail_prefix(ctx.message, exc)
            return
        if not joined:
            await ctx.reply("🛡️ You are already in this raid.")
            return
        await ctx.reply(embed=boss_embed(state), view=self._register(state))

    @commands.command(name="attack", help="Attack the boss")
    @commands.guild_only()
    async def attack_prefix(self, ctx: commands.Context) -> None:
        await self._prefix_boss_action(ctx, "attack")

    @commands.command(name="spell", help="Cast a spell at the boss")
    @commands.guild_only()
    async def spell_prefix(self, ctx: commands.Context) -> None:
        await self._prefix_boss_action(ctx, "spell")

    @commands.command(name="heal", help="Heal yourself during a boss raid")
    @commands.guild_only()
    async def heal_prefix(self, ctx: commands.Context) -> None:
        await self._prefix_boss_action(ctx, "heal")

    async def _prefix_boss_action(self, ctx: commands.Context, action: str) -> None:
        state = await self.service.active_boss(ctx.guild.id)
        if state is None:
            await ctx.reply("No boss is attacking right now. Start one with `!bossfight`.")
            return
        message = await ctx.reply(f"⚔️ *{_display_name(ctx.author)} moves…*")
        try:
            turn = await self.service.boss_action(
                state.id, ctx.author.id, _display_name(ctx.author), action
            )
        except Exception as exc:  # noqa: BLE001
            await self._fail_prefix(message, exc)
            return
        await message.edit(embed=boss_turn_embed(turn), view=self._register(turn.state))

    @commands.command(name="gamestatus", aliases=["games"], help="Show active AI games")
    @commands.guild_only()
    async def gamestatus_prefix(self, ctx: commands.Context) -> None:
        counts = await self.service.session_counts(ctx.guild.id)
        boss = await self.service.active_boss(ctx.guild.id)
        await ctx.reply(
            embed=game_status_embed(
                counts,
                boss,
                enabled=self.service.settings.enabled,
                ai_available=self.service.ai_available,
            )
        )

    @commands.command(name="quitgame", help="End your active game")
    @commands.guild_only()
    async def quitgame_prefix(self, ctx: commands.Context) -> None:
        session = await self.service.active_rpg(ctx.guild.id, ctx.author.id)
        if session is None:
            session = await self.service.active_detective(ctx.guild.id, ctx.channel.id)
            if session is not None and session.owner_id != ctx.author.id:
                session = None
        if session is None:
            boss = await self.service.active_boss(ctx.guild.id)
            if boss is not None and boss.owner_id == ctx.author.id:
                session = boss
        if session is None:
            await ctx.reply("You have no active game to end.")
            return
        try:
            await self.service.end_session(session.id, ctx.author.id, ctx.guild.id)
        except Exception as exc:  # noqa: BLE001
            await self._fail_prefix(ctx.message, exc)
            return
        await ctx.reply("🛑 Game ended.")

    async def _fail_prefix(self, message: discord.Message, exc: Exception) -> None:
        if isinstance(exc, GameError):
            text = str(exc)
        else:
            log.error(
                "Games prefix command failed: %s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            text = "🎲 The game hit a snag. Please try again."
        embed = error_embed(text)
        if message.author.id == getattr(self.bot.user, "id", None):
            try:
                await message.edit(embed=embed)
                return
            except discord.HTTPException:
                pass
        try:
            await message.reply(embed=embed)
        except discord.HTTPException:
            pass

    # -- error handling ----------------------------------------------------
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.error(
            "Unhandled games slash error: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        await self._fail(interaction, error)

    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.NoPrivateMessage):
            await ctx.reply("🎮 Games only run inside a server.")
            return
        if isinstance(error, (commands.MissingRequiredArgument, commands.BadArgument)):
            await ctx.reply(f"Usage: `{ctx.prefix}{ctx.command} {ctx.command.signature}`")
            return
        log.error(
            "Unhandled games prefix error: %s",
            error,
            exc_info=(type(error), error, error.__traceback__),
        )
        await ctx.reply("🎲 The game hit a snag. Please try again.")


async def setup(bot: commands.Bot):
    service: GameService | None = getattr(bot, "games", None)
    if service is None:
        raise RuntimeError("GameService was not attached to the bot before loading features.games.cog")
    await bot.add_cog(GamesCog(bot, service))
