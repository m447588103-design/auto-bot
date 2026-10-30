"""Per-guild voice player with queue, volume, loop and autoplay."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Deque

import discord

from core.config import DJSettings
from core.discord_ui import MUSIC, embed
from core.utils import human_duration, truncate

from .curator import DJCurator
from .models import Track
from .youtube import YouTubeError, YouTubeResolver

log = logging.getLogger(__name__)


class GuildPlayer:
    def __init__(
        self,
        bot: discord.Client,
        guild_id: int,
        settings: DJSettings,
        curator: DJCurator,
        resolver: YouTubeResolver,
    ):
        self.bot = bot
        self.guild_id = guild_id
        self.settings = settings
        self.curator = curator
        self.resolver = resolver
        self.queue: Deque[Track] = deque()
        self.current: Track | None = None
        self.plan = None
        self.volume = settings.default_volume
        self.autoplay = settings.autoplay
        self.loop_mode = "off"
        self._voice: discord.VoiceClient | None = None
        self._advancing = asyncio.Lock()
        self._idle_task: asyncio.Task | None = None
        self._played: Deque[str] = deque(maxlen=100)
        self._last_channel_id: int | None = None

    # -- voice -------------------------------------------------------------
    @property
    def voice(self) -> discord.VoiceClient | None:
        if self._voice and self._voice.is_connected():
            return self._voice
        self._voice = discord.utils.get(self.bot.voice_clients, guild__id=self.guild_id)
        return self._voice if self._voice and self._voice.is_connected() else None

    async def connect(self, channel: discord.VoiceChannel) -> discord.VoiceClient:
        self._cancel_idle()
        existing = self.voice
        if existing:
            if existing.channel and existing.channel.id != channel.id:
                await existing.move_to(channel)
            self._last_channel_id = channel.id
            return existing
        timeout = 30.0
        try:
            self._voice = await channel.connect(timeout=timeout, reconnect=True, self_deaf=True)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Timed out connecting to the voice channel.") from exc
        self._last_channel_id = channel.id
        return self._voice

    async def disconnect(self) -> None:
        self.queue.clear()
        self.current = None
        self._cancel_idle()
        voice = self.voice
        if voice:
            try:
                await voice.disconnect(force=True)
            except Exception:  # noqa: BLE001
                pass
        self._voice = None

    @property
    def connected(self) -> bool:
        return self.voice is not None

    @property
    def playing(self) -> bool:
        voice = self.voice
        return bool(voice and (voice.is_playing() or voice.is_paused()))

    # -- queue -------------------------------------------------------------
    def enqueue(self, tracks: list[Track]) -> int:
        added = 0
        for track in tracks:
            if len(self.queue) >= self.settings.max_queue:
                break
            self.queue.append(track)
            added += 1
        return added

    @property
    def queue_size(self) -> int:
        return len(self.queue)

    async def skip(self) -> Track | None:
        current = self.current
        voice = self.voice
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()  # triggers the after-callback -> next track
        else:
            await self._start_next()
        return current

    async def stop(self) -> None:
        self.queue.clear()
        self.current = None
        voice = self.voice
        if voice and (voice.is_playing() or voice.is_paused()):
            voice.stop()
        self._schedule_idle()

    def pause(self) -> bool:
        voice = self.voice
        if voice and voice.is_playing():
            voice.pause()
            return True
        return False

    def resume(self) -> bool:
        voice = self.voice
        if voice and voice.is_paused():
            voice.resume()
            return True
        return False

    def set_volume(self, volume: float) -> None:
        self.volume = max(0.0, min(2.0, volume))
        voice = self.voice
        if voice and isinstance(voice.source, discord.PCMVolumeTransformer):
            voice.source.volume = self.volume

    # -- playback ----------------------------------------------------------
    async def start(self) -> None:
        if self.connected and not self.playing and self.current is None:
            await self._start_next()

    async def _start_next(self) -> None:
        async with self._advancing:
            voice = self.voice
            if voice is None:
                return
            if voice.is_playing() or voice.is_paused():
                return

            track = await self._next_track()
            if track is None:
                self.current = None
                self._schedule_idle()
                return

            stream_url = None
            for attempt in range(3):
                try:
                    stream_url = await self.resolver.stream_url(track)
                    break
                except YouTubeError as exc:
                    log.warning("Could not resolve %s (attempt %s): %s", track.display, attempt + 1, exc)
                    track = await self._next_track() if attempt < 2 else None
                    if track is None:
                        break
            if not stream_url or track is None:
                self.current = None
                self._schedule_idle()
                return

            self.current = track
            self._played.append(track.key)
            self._cancel_idle()

            source = discord.FFmpegPCMAudio(
                stream_url,
                before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin",
                options="-vn",
                executable=self.settings.ffmpeg_bin or "ffmpeg",
            )
            transformer = discord.PCMVolumeTransformer(source, volume=self.volume)
            try:
                voice.play(transformer, after=self._after_playback)
            except discord.ClientException as exc:
                log.warning("Voice playback error: %s", exc)
                self.current = None
                return
            log.info("Guild %s now playing: %s", self.guild_id, track.display)

    async def _next_track(self) -> Track | None:
        if self.queue:
            return self.queue.popleft()
        if self.autoplay and self.plan is not None:
            try:
                exclude = set(self._played) | {track.key for track in self.queue}
                more = await self.curator.refill(self.plan, exclude=exclude, limit=3)
            except Exception as exc:  # noqa: BLE001
                log.warning("Autoplay refill failed: %s", exc)
                return None
            if more:
                self.enqueue(more)
                return self.queue.popleft() if self.queue else None
        return None

    def _after_playback(self, error: Exception | None) -> None:
        if error:
            log.warning("Playback error in guild %s: %s", self.guild_id, error)
        try:
            asyncio.run_coroutine_threadsafe(self._on_finished(), self.bot.loop)
        except RuntimeError:  # pragma: no cover - loop closed during shutdown
            pass

    async def _on_finished(self) -> None:
        finished = self.current
        self.current = None
        if finished and self.loop_mode == "one":
            self.queue.appendleft(finished)
        elif finished and self.loop_mode == "all":
            self.queue.append(finished)
        await self._start_next()

    # -- idle --------------------------------------------------------------
    def _schedule_idle(self) -> None:
        self._cancel_idle()
        if self.settings.idle_disconnect_seconds <= 0:
            return
        self._idle_task = asyncio.create_task(self._idle_disconnect())

    async def _idle_disconnect(self) -> None:
        try:
            await asyncio.sleep(self.settings.idle_disconnect_seconds)
        except asyncio.CancelledError:
            return
        if self.playing or self.queue:
            return
        log.info("Idle timeout in guild %s - leaving voice", self.guild_id)
        await self.disconnect()

    def _cancel_idle(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    # -- presentation ------------------------------------------------------
    def now_playing_embed(self, plan_reason: str | None = None) -> discord.Embed:
        track = self.current
        if track is None:
            return embed(
                "🎵 Nothing playing",
                "Queue is empty. Try `/dj play bhai mon bhalo na, lofi bajao`.",
                color=MUSIC,
            )
        total_remaining = sum(t.duration or 0 for t in self.queue)
        emb = embed(
            "🎶 Now playing",
            f"**{truncate(track.display, 200)}**",
            color=MUSIC,
            thumbnail=track.thumbnail,
            url=track.watch_url,
            fields=[
                ("Duration", track.duration_text, True),
                ("Volume", f"{int(self.volume * 100)}%", True),
                ("Up next", str(self.queue_size), True),
                (
                    "Queue length",
                    human_duration(total_remaining) if total_remaining else "—",
                    True,
                ),
                ("Loop", self.loop_mode, True),
                ("Autoplay", "on" if self.autoplay else "off", True),
            ],
            footer=(
                f"AutoBot DJ · mood plan {getattr(self.plan, 'mood', 'n/a')}"
                if self.plan
                else "AutoBot DJ"
            ),
        )
        if plan_reason:
            emb.add_field(name="Why", value=truncate(plan_reason, 300), inline=False)
        return emb

