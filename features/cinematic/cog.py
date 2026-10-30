"""Discord commands + job queue for the AI cinematic director."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

import discord
from discord import app_commands
from discord.ext import commands

from core.config import settings
from core.discord_ui import (
    CINEMA,
    ProgressReporter,
    embed,
    error_embed,
    success_embed,
)
from core.utils import human_duration, truncate, utcnow

from .models import MovieJob, MovieResult
from .pipeline import JobCancelled, MoviePipeline, output_filename
from .publishers import PublishError, YouTubePublisher

log = logging.getLogger(__name__)

MAX_QUEUE = 10


class CinematicCog(commands.Cog, name="Cinematic Director"):
    """Turns a one-line idea into a rendered vertical reel."""

    movie = app_commands.Group(name="movie", description="AI script → voiceover → stock footage → reel")

    def __init__(self, bot: commands.Bot, pipeline: MoviePipeline):
        self.bot = bot
        self.pipeline = pipeline
        self.youtube = YouTubePublisher(settings.cinematic)
        self.queue: asyncio.Queue[MovieJob] = asyncio.Queue()
        self._jobs: dict[str, MovieJob] = {}
        self._current: MovieJob | None = None
        self._worker: asyncio.Task[None] | None = None

    async def cog_load(self) -> None:
        self._worker = asyncio.create_task(self._worker_loop(), name="cinematic-worker")

    async def cog_unload(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._worker = None

    # -- worker ------------------------------------------------------------
    async def _worker_loop(self) -> None:
        await self.bot.wait_until_ready()
        while True:
            job = await self.queue.get()
            self._current = job
            try:
                if job.cancelled:
                    continue
                await self._process(job)
            except JobCancelled:
                await self._notify(job, error_embed("The job was cancelled.", title="🎬 Cancelled"))
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                job.status = "cancelled"
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("Movie job %s failed", job.id)
                job.status = "failed"
                job.error = str(exc)[:500]
                job.finished_at = utcnow()
                await self._notify(
                    job,
                    error_embed(
                        f"The render failed.\n```{truncate(str(exc), 900)}```",
                        title="🎬 Production failed",
                    ),
                )
            finally:
                self._current = None
                self.queue.task_done()
                self._prune_jobs()

    async def _process(self, job: MovieJob) -> None:
        channel = await self._channel(job)
        if channel is None:
            log.warning("Movie job %s: channel %s is gone", job.id, job.channel_id)
            return

        reporter = ProgressReporter(
            channel, title=f"🎬 {truncate(job.short_theme, 200)}", min_interval=2.5
        )
        await reporter.start()

        target_mb = settings.cinematic.max_upload_mb
        guild = getattr(channel, "guild", None)
        if guild is not None:
            limit_mb = getattr(guild, "filesize_limit", 10 * 1024 * 1024) / 1048576
            target_mb = max(1.0, min(target_mb, limit_mb - 1.0))

        result = await self.pipeline.run(job, reporter.update, target_upload_mb=target_mb)
        job.result = result
        await self._deliver(job, result, reporter)

    async def _deliver(
        self, job: MovieJob, result: MovieResult, reporter: ProgressReporter
    ) -> None:
        channel = await self._channel(job)
        script = result.script
        publish_line = ""
        if job.publish_youtube:
            if not self.youtube.configured:
                publish_line = "\n⚠️ YouTube publishing is not configured; video was delivered to Discord only."
            else:
                await reporter.update(99, "📤 Uploading to YouTube Shorts…", force=True)
                try:
                    published = await self.youtube.upload(result.video, script)
                    publish_line = f"\n✅ Published to YouTube: {published.url}"
                except PublishError as exc:
                    publish_line = f"\n⚠️ YouTube upload failed: {exc}"
                except Exception as exc:  # noqa: BLE001
                    log.exception("YouTube publish failed")
                    publish_line = f"\n⚠️ YouTube upload failed: {exc}"

        warn_text = ""
        if result.warnings:
            warn_text = "\n\n**Production notes**\n" + "\n".join(
                f"• {truncate(warning, 180)}" for warning in result.warnings[:6]
            )

        emb = embed(
            f"🎬 {script.title}",
            truncate(script.logline or job.theme, 500),
            color=CINEMA,
            fields=[
                ("Duration", human_duration(result.duration), True),
                ("Size", f"{result.size_mb:.1f} MB", True),
                ("Scenes", str(len(script.scenes)), True),
                ("Hashtags", " ".join(script.clean_hashtags()) or "—", False),
                ("Idea", truncate(job.theme, 300), False),
            ],
            footer=f"Requested by {job.requester_name} • AutoBot cinematic director",
        )
        if warn_text:
            emb.add_field(name="Notes", value=truncate(warn_text, 1024), inline=False)
        if publish_line:
            emb.add_field(name="Publishing", value=publish_line.strip(), inline=False)

        if channel is None:
            await reporter.finish(emb)
            return

        limit_bytes = settings.cinematic.max_upload_mb * 1048576
        guild = getattr(channel, "guild", None)
        if guild is not None:
            limit_bytes = min(limit_bytes, getattr(guild, "filesize_limit", 10 * 1024 * 1024) - 65536)

        await reporter.finish(emb)

        try:
            size = result.video.stat().st_size
            if size <= limit_bytes:
                await channel.send(
                    file=discord.File(str(result.video), filename=output_filename(script)),
                )
            else:
                note = (
                    f"The reel is {size / 1048576:.1f} MB which exceeds this server's "
                    f"{limit_bytes / 1048576:.0f} MB upload limit. Here is the cover frame instead."
                )
                await channel.send(
                    embed=error_embed(note, title="Video too large to upload"),
                    file=discord.File(str(result.thumbnail), filename="reel-cover.jpg")
                    if result.thumbnail and result.thumbnail.exists()
                    else None,
                )
        except discord.HTTPException as exc:
            log.warning("Failed to upload movie: %s", exc)
            await channel.send(embed=error_embed(f"Upload failed: {exc}"))

    async def _notify(self, job: MovieJob, emb: discord.Embed) -> None:
        channel = await self._channel(job)
        if channel is None:
            return
        try:
            await channel.send(content=f"<@{job.requester_id}>", embed=emb)
        except discord.HTTPException:
            pass

    async def _channel(self, job: MovieJob) -> discord.abc.Messageable | None:
        channel = self.bot.get_channel(job.channel_id)
        if channel is not None:
            return channel
        try:
            channel = await self.bot.fetch_channel(job.channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None
        return channel

    def _prune_jobs(self) -> None:
        if len(self._jobs) <= 25:
            return
        done = [job for job in self._jobs.values() if job.status in {"done", "cancelled", "failed"}]
        done.sort(key=lambda job: job.created_at)
        for job in done[: max(0, len(self._jobs) - 25)]:
            self._jobs.pop(job.id, None)

    # -- commands ----------------------------------------------------------
    @movie.command(name="create", description="Generate a complete vertical reel from an idea")
    @app_commands.describe(
        theme="Your story idea, e.g. 'A lonely astronaut on Mars finds a glowing flower'",
        publish="Also upload to YouTube Shorts (needs YouTube credentials)",
    )
    @app_commands.checks.cooldown(1, 300.0, key=lambda i: i.user.id)
    async def create(
        self, interaction: discord.Interaction, theme: str, publish: bool = False
    ) -> None:
        if interaction.guild_id is None or interaction.channel is None:
            await interaction.response.send_message(
                embed=error_embed("This command only works in a server."), ephemeral=True
            )
            return

        preflight = self.pipeline.preflight()
        if not preflight.ffmpeg:
            await interaction.response.send_message(
                embed=error_embed(
                    "**ffmpeg is missing** — install it (`apt install ffmpeg` / `brew install ffmpeg`) "
                    "and restart the bot.",
                    title="Renderer not ready",
                ),
                ephemeral=True,
            )
            return
        if self.queue.qsize() >= MAX_QUEUE:
            await interaction.response.send_message(
                embed=error_embed(
                    f"The render queue is full ({MAX_QUEUE} jobs). Try again in a few minutes.",
                    title="Queue full",
                ),
                ephemeral=True,
            )
            return

        job = MovieJob(
            id=f"{int(time.time())}-{uuid.uuid4().hex[:6]}",
            theme=theme.strip(),
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            requester_id=interaction.user.id,
            requester_name=interaction.user.display_name,
            publish_youtube=publish,
        )
        self._jobs[job.id] = job
        await self.queue.put(job)
        position = self.queue.qsize()
        eta = position * 3

        await interaction.response.send_message(
            embed=embed(
                "🎬 Production queued",
                f"**Idea:** {truncate(job.theme, 400)}\n"
                f"**Job:** `{job.id}`\n"
                f"**Queue position:** {position}\n"
                f"**Est. start:** ~{eta} min\n\n"
                "I'll post progress here and ping you when the reel is ready.",
                color=CINEMA,
            )
        )

    @movie.command(name="preview", description="See the AI script before rendering")
    @app_commands.describe(theme="Story idea to preview")
    async def preview(self, interaction: discord.Interaction, theme: str) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        from .scriptwriter import write_script

        try:
            script = await write_script(
                theme, settings=settings.cinematic, gemini=self.pipeline.gemini
            )
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return

        lines = [
            f"**{scene.index}. {truncate(scene.narration, 240)}**\n"
            f"↳ footage: `{scene.visual_query}`"
            for scene in script.scenes
        ]
        emb = embed(
            f"📜 {script.title}",
            truncate(script.logline, 400),
            color=CINEMA,
            fields=[
                ("Scenes", "\n".join(lines)[:1024], False),
                ("Music mood", script.music_mood, True),
                ("Est. length", human_duration(script.estimated_seconds), True),
                ("Hashtags", " ".join(script.clean_hashtags()), False),
            ],
            footer="Preview only — use /movie create to render",
        )
        if script.demo:
            emb.add_field(
                name="Note",
                value="Gemini is not configured, so this preview uses the local demo template.",
                inline=False,
            )
        await interaction.followup.send(embed=emb, ephemeral=True)

    @movie.command(name="status", description="Show renderer health, queue and recent jobs")
    async def status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        preflight = self.pipeline.preflight()
        recent = sorted(self._jobs.values(), key=lambda job: job.created_at, reverse=True)[:5]
        job_lines = []
        for job in recent:
            icon = {"queued": "⏳", "running": "🎬", "done": "✅", "cancelled": "🚫", "failed": "❌"}.get(
                job.status, "•"
            )
            job_lines.append(f"{icon} `{job.id}` {truncate(job.short_theme, 50)} — {job.status}")
        current = f"🎬 rendering `{self._current.id}`" if self._current else "idle"
        emb = embed(
            "🎥 Cinematic director status",
            preflight.summary(),
            color=CINEMA,
            fields=[
                ("Worker", f"{current}\nQueue: {self.queue.qsize()}", False),
                ("Recent jobs", "\n".join(job_lines) or "No jobs yet", False),
                ("Music library", str(settings.cinematic.music_dir), False),
            ],
        )
        await interaction.followup.send(embed=emb)

    @movie.command(name="cancel", description="Cancel a queued or running render")
    @app_commands.describe(job_id="Job id shown when you created it")
    async def cancel(self, interaction: discord.Interaction, job_id: str) -> None:
        job = self._jobs.get(job_id.strip())
        if job is None:
            await interaction.response.send_message(
                embed=error_embed(f"No job with id `{job_id}`.", title="Unknown job"), ephemeral=True
            )
            return
        is_owner = job.requester_id == interaction.user.id
        is_mod = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild
        if not (is_owner or is_mod):
            await interaction.response.send_message(
                embed=error_embed("Only the requester or a server manager can cancel this job."),
                ephemeral=True,
            )
            return
        if job.status in {"done", "cancelled"}:
            await interaction.response.send_message(
                embed=error_embed(f"Job `{job.id}` is already {job.status}."), ephemeral=True
            )
            return
        job.cancel()
        await interaction.response.send_message(
            embed=success_embed(
                f"Job `{job.id}` cancelled. The current ffmpeg step may take a moment to stop.",
                title="Cancelled",
            ),
            ephemeral=True,
        )

    # -- errors ------------------------------------------------------------
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"One reel at a time — try again in **{error.retry_after:.0f}s**."
        else:
            log.exception("Cinematic command error", exc_info=error)
            message = "Unexpected error while preparing your reel. Check the bot logs."
        if interaction.response.is_done():
            await interaction.followup.send(embed=error_embed(message), ephemeral=True)
        else:
            await interaction.response.send_message(embed=error_embed(message), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    pipeline: MoviePipeline | None = getattr(bot, "movie_pipeline", None)
    if pipeline is None:
        raise RuntimeError("MoviePipeline was not attached to the bot")
    await bot.add_cog(CinematicCog(bot, pipeline))
