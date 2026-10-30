"""Discord commands for the mood-based AI DJ."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from core.config import settings
from core.discord_ui import MUSIC, embed, error_embed, success_embed
from core.utils import truncate

from .curator import DJCurator, DJError
from .player import GuildPlayer
from .youtube import YouTubeError

log = logging.getLogger(__name__)

LOOP_CHOICES = [
    app_commands.Choice(name="off", value="off"),
    app_commands.Choice(name="one (repeat track)", value="one"),
    app_commands.Choice(name="all (repeat queue)", value="all"),
]


class DJCog(commands.Cog, name="AI DJ"):
    """Mood-aware music curation and voice playback."""

    dj = app_commands.Group(name="dj", description="Mood-based AI DJ for voice channels")

    def __init__(self, bot: commands.Bot, curator: DJCurator):
        self.bot = bot
        self.curator = curator
        self.players: dict[int, GuildPlayer] = {}

    async def cog_unload(self) -> None:
        for player in list(self.players.values()):
            await player.disconnect()
        self.players.clear()

    # -- helpers -----------------------------------------------------------
    def _player(self, guild_id: int) -> GuildPlayer:
        player = self.players.get(guild_id)
        if player is None:
            player = GuildPlayer(
                self.bot,
                guild_id,
                settings.dj,
                self.curator,
                self.curator.resolver,
            )
            self.players[guild_id] = player
        return player

    async def _require_voice(
        self, interaction: discord.Interaction
    ) -> tuple[discord.VoiceChannel | None, bool]:
        """Return the user's voice channel, replying with an error if unusable."""
        if interaction.guild is None:
            await interaction.response.send_message(
                embed=error_embed("The DJ only works inside a server."), ephemeral=True
            )
            return None, False
        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            await interaction.response.send_message(
                embed=error_embed("Join a voice channel first, then ask me to play."),
                ephemeral=True,
            )
            return None, False
        channel = member.voice.channel
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message(
                embed=error_embed("I can only join regular voice channels (not stage channels)."),
                ephemeral=True,
            )
            return None, False
        perms = channel.permissions_for(interaction.guild.me)
        if not perms.connect or not perms.speak:
            await interaction.response.send_message(
                embed=error_embed(
                    f"I need **Connect** and **Speak** permissions in {channel.mention}."
                ),
                ephemeral=True,
            )
            return None, False
        return channel, True

    def _existing_player(self, interaction: discord.Interaction) -> GuildPlayer | None:
        return self.players.get(interaction.guild_id or 0)

    # -- commands ----------------------------------------------------------
    @dj.command(name="play", description="Tell me your mood, I'll build and play a playlist")
    @app_commands.describe(
        mood="e.g. 'bhai mon bhalo na, lofi bajao' or 'hyperpop for coding'"
    )
    @app_commands.checks.cooldown(2, 20.0, key=lambda i: i.user.id)
    async def play(self, interaction: discord.Interaction, mood: str) -> None:
        channel, ok = await self._require_voice(interaction)
        if not ok:
            return
        await interaction.response.defer(thinking=True)
        player = self._player(interaction.guild_id)  # type: ignore[arg-type]
        try:
            await player.connect(channel)  # type: ignore[arg-type]
            playlist = await self.curator.build(
                mood, limit=6, requester_id=interaction.user.id
            )
        except (DJError, YouTubeError, RuntimeError) as exc:
            if player.current is None and not player.queue:
                await player.disconnect()
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("DJ play failed")
            if player.current is None and not player.queue:
                await player.disconnect()
            await interaction.followup.send(
                embed=error_embed(f"Could not start playback: {exc}"), ephemeral=True
            )
            return

        player.plan = playlist.plan
        added = player.enqueue(playlist.tracks)
        await player.start()

        plan = playlist.plan
        track_lines = [
            f"`{index}.` {truncate(track.display, 80)} · {track.duration_text}"
            for index, track in enumerate(playlist.tracks[:6], start=1)
        ]
        emb = embed(
            f"🎧 {plan.mood.title()}",
            truncate(plan.reason, 500),
            color=MUSIC,
            fields=[
                ("Mood", f"energy {int(plan.energy * 100)}%", True),
                ("Genres", ", ".join(plan.genres[:4]) or "mixed", True),
                ("Tracks queued", str(added), True),
                ("Playlist", "\n".join(track_lines) or "—", False),
                ("Source", playlist.note or "YouTube search", False),
            ],
            thumbnail=playlist.tracks[0].thumbnail if playlist.tracks else None,
            footer=f"AutoBot DJ · {channel} · /dj skip to change track",  # type: ignore[str-format]
        )
        await interaction.followup.send(embed=emb)

    @dj.command(name="add", description="Add a specific song or search query to the queue")
    @app_commands.describe(query="Song name, artist or YouTube link")
    async def add(self, interaction: discord.Interaction, query: str) -> None:
        player = self._existing_player(interaction) or self._player(interaction.guild_id or 0)
        if not player.connected:
            channel, ok = await self._require_voice(interaction)
            if not ok:
                return
            await player.connect(channel)  # type: ignore[arg-type]
        await interaction.response.defer(thinking=True)

        try:
            tracks = await self.curator.resolver.search(query, limit=2)
        except YouTubeError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return
        if not tracks:
            await interaction.followup.send(
                embed=error_embed(f"No results for `{truncate(query, 100)}`."), ephemeral=True
            )
            return
        tracks[0].requester_id = interaction.user.id
        player.enqueue(tracks[:1])
        await player.start()
        await interaction.followup.send(
            embed=success_embed(
                f"Added **{truncate(tracks[0].display, 150)}** ({tracks[0].duration_text}) "
                f"to the queue. Position: {player.queue_size}.",
                title="🎵 Queued",
            )
        )

    @dj.command(name="queue", description="Show the current track and what's next")
    async def queue(self, interaction: discord.Interaction) -> None:
        player = self._existing_player(interaction)
        if player is None or (player.current is None and not player.queue):
            await interaction.response.send_message(
                embed=embed("Queue is empty", "Use `/dj play <mood>` to start.", color=MUSIC),
                ephemeral=True,
            )
            return
        lines = []
        if player.current:
            lines.append(f"**▶ {truncate(player.current.display, 90)}** · {player.current.duration_text}")
        for index, track in enumerate(list(player.queue)[:10], start=1):
            lines.append(f"`{index}.` {truncate(track.display, 80)} · {track.duration_text}")
        if player.queue_size > 10:
            lines.append(f"…and {player.queue_size - 10} more")
        await interaction.response.send_message(
            embed=embed(
                "🎶 Playback queue",
                "\n".join(lines),
                color=MUSIC,
                footer=f"AutoBot DJ · {player.queue_size} waiting · loop {player.loop_mode}",
            )
        )

    @dj.command(name="skip", description="Skip the current track")
    async def skip(self, interaction: discord.Interaction) -> None:
        player = self._existing_player(interaction)
        if player is None or not player.playing:
            await interaction.response.send_message(
                embed=error_embed("Nothing is playing right now."), ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        previous = await player.skip()
        await asyncio.sleep(1.0)
        emb = embed(
            "⏭️ Skipped",
            f"Skipped **{truncate(previous.display, 150) if previous else 'current track'}**.",
            color=MUSIC,
        )
        if player.current:
            emb.add_field(name="Now playing", value=truncate(player.current.display, 200), inline=False)
        else:
            emb.add_field(name="Queue", value="Empty — add more with `/dj add`.", inline=False)
        await interaction.followup.send(embed=emb)

    @dj.command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, interaction: discord.Interaction) -> None:
        player = self._existing_player(interaction)
        if player is None or not player.connected:
            await interaction.response.send_message(
                embed=error_embed("I'm not in a voice channel."), ephemeral=True
            )
            return
        await player.stop()
        await interaction.response.send_message(
            embed=success_embed("Playback stopped and the queue was cleared.", title="⏹️ Stopped")
        )

    @dj.command(name="pause", description="Pause playback")
    async def pause(self, interaction: discord.Interaction) -> None:
        player = self._existing_player(interaction)
        if player and player.pause():
            await interaction.response.send_message(
                embed=success_embed("Paused. Use `/dj resume` to continue."), ephemeral=True
            )
        else:
            await interaction.response.send_message(
                embed=error_embed("Nothing is playing."), ephemeral=True
            )

    @dj.command(name="resume", description="Resume playback")
    async def resume(self, interaction: discord.Interaction) -> None:
        player = self._existing_player(interaction)
        if player and player.resume():
            await interaction.response.send_message(embed=success_embed("Resumed."), ephemeral=True)
        else:
            await interaction.response.send_message(
                embed=error_embed("Playback is not paused."), ephemeral=True
            )

    @dj.command(name="volume", description="Set the playback volume (0-200%)")
    @app_commands.describe(percent="0 to 200")
    async def volume(self, interaction: discord.Interaction, percent: int) -> None:
        player = self._existing_player(interaction)
        if player is None or not player.connected:
            await interaction.response.send_message(
                embed=error_embed("I'm not playing anything right now."), ephemeral=True
            )
            return
        percent = max(0, min(200, percent))
        player.set_volume(percent / 100)
        await interaction.response.send_message(
            embed=success_embed(f"Volume set to **{percent}%**.", title="🔊 Volume"), ephemeral=True
        )

    @dj.command(name="nowplaying", description="Show the current track")
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        player = self._existing_player(interaction)
        if player is None:
            await interaction.response.send_message(
                embed=error_embed("I'm not in a voice channel."), ephemeral=True
            )
            return
        await interaction.response.send_message(embed=player.now_playing_embed())

    @dj.command(name="loop", description="Loop the current track or the whole queue")
    @app_commands.choices(mode=LOOP_CHOICES)
    async def loop(self, interaction: discord.Interaction, mode: app_commands.Choice[str]) -> None:
        player = self._existing_player(interaction)
        if player is None:
            await interaction.response.send_message(
                embed=error_embed("Start some music first."), ephemeral=True
            )
            return
        player.loop_mode = mode.value
        await interaction.response.send_message(
            embed=success_embed(f"Loop mode set to **{mode.value}**.", title="🔁 Loop"), ephemeral=True
        )

    @dj.command(name="autoplay", description="Keep the music going when the queue ends")
    async def autoplay(self, interaction: discord.Interaction, enabled: bool) -> None:
        player = self._existing_player(interaction)
        if player is None:
            await interaction.response.send_message(
                embed=error_embed("Start some music first."), ephemeral=True
            )
            return
        player.autoplay = enabled
        await interaction.response.send_message(
            embed=success_embed(f"Autoplay is now **{'on' if enabled else 'off'}**.", title="♾️ Autoplay"),
            ephemeral=True,
        )

    @dj.command(name="leave", description="Disconnect the DJ from voice and clear everything")
    async def leave(self, interaction: discord.Interaction) -> None:
        player = self._existing_player(interaction)
        if player is None or not player.connected:
            await interaction.response.send_message(
                embed=error_embed("I'm not in a voice channel."), ephemeral=True
            )
            return
        await player.disconnect()
        self.players.pop(interaction.guild_id or 0, None)
        await interaction.response.send_message(
            embed=success_embed("Left the voice channel. See you next time!", title="👋 Bye")
        )

    # -- errors ------------------------------------------------------------
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"Chill for **{error.retry_after:.0f}s** before the next request."
        else:
            log.exception("DJ command error", exc_info=error)
            message = "Unexpected DJ error. Check the bot logs."
        if interaction.response.is_done():
            await interaction.followup.send(embed=error_embed(message), ephemeral=True)
        else:
            await interaction.response.send_message(embed=error_embed(message), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    curator: DJCurator | None = getattr(bot, "dj_curator", None)
    if curator is None:
        raise RuntimeError("DJCurator was not attached to the bot")
    await bot.add_cog(DJCog(bot, curator))
