"""End-to-end reel production: script -> voice -> footage -> edit -> mp4."""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from core.config import CinematicSettings
from core.gemini import GeminiClient, get_gemini
from core.http import http_session
from core.utils import safe_filename, utcnow

from . import renderer
from .captions import CaptionScene, build_ass, shift_words
from .models import MovieJob, MovieResult, StockAsset
from .scriptwriter import MovieScript, write_script
from .stock import StockLibrary, attribution_line
from .tts import SpeechResult, TTSError, synthesize

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], Awaitable[None]]


class JobCancelled(RuntimeError):
    pass


async def _noop(percent: float, stage: str) -> None:  # pragma: no cover
    return None


@dataclass
class Preflight:
    ok: bool
    ffmpeg: bool
    ffprobe: bool
    tts: str | None
    pexels: bool
    gemini: bool
    disk_free_mb: float
    problems: list[str]

    def summary(self) -> str:
        def mark(value: bool) -> str:
            return "✅" if value else "❌"

        lines = [
            f"{mark(self.ffmpeg)} ffmpeg",
            f"{mark(self.ffprobe)} ffprobe",
            f"{mark(bool(self.tts))} TTS engine" + (f" ({self.tts})" if self.tts else ""),
            f"{mark(self.pexels)} Pexels API" + ("" if self.pexels else " (using picsum fallback)"),
            f"{mark(self.gemini)} Gemini script" + ("" if self.gemini else " (demo templates only)"),
            f"💾 {self.disk_free_mb:.0f} MB free disk",
        ]
        if self.problems:
            lines.append("\n**Blocking issues:** " + "; ".join(self.problems))
        return "\n".join(lines)


def check_tts() -> str | None:
    try:
        import edge_tts  # noqa: F401

        return "edge-tts"
    except ImportError:
        pass
    try:
        import gtts  # noqa: F401

        return "gTTS"
    except ImportError:
        return None


class MoviePipeline:
    def __init__(self, settings: CinematicSettings, gemini: GeminiClient | None = None):
        self.settings = settings
        self.gemini = gemini or get_gemini()
        self.stock = StockLibrary(settings)

    # -- checks ------------------------------------------------------------
    def preflight(self) -> Preflight:
        ffmpeg_ok = renderer.ffmpeg_installed(self.settings.ffmpeg_bin)
        ffprobe_ok = renderer.ffprobe_installed(self.settings.ffprobe_bin)
        tts = check_tts()
        problems: list[str] = []
        if not ffmpeg_ok:
            problems.append("ffmpeg is not installed or not on PATH")
        if not ffprobe_ok:
            problems.append("ffprobe is not installed or not on PATH")
        if not tts:
            problems.append("no TTS engine (pip install edge-tts)")
        self.settings.workdir.mkdir(parents=True, exist_ok=True)
        try:
            usage = shutil.disk_usage(self.settings.workdir)
            free_mb = usage.free / 1048576
        except OSError:  # pragma: no cover
            free_mb = 0.0
        if free_mb < 300:
            problems.append("less than 300 MB free disk space")
        return Preflight(
            ok=not problems,
            ffmpeg=ffmpeg_ok,
            ffprobe=ffprobe_ok,
            tts=tts,
            pexels=self.settings.pexels_configured,
            gemini=self.gemini.available,
            disk_free_mb=free_mb,
            problems=problems,
        )

    # -- main --------------------------------------------------------------
    async def run(
        self, job: MovieJob, progress: ProgressFn | None = None, *, target_upload_mb: float | None = None
    ) -> MovieResult:
        progress = progress or _noop
        preflight = self.preflight()
        if not preflight.ok:
            raise renderer.RenderError("; ".join(preflight.problems))

        job.status = "running"
        job.started_at = utcnow()
        workdir = self.settings.workdir / job.id
        assets_dir = workdir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        job.workdir = workdir
        warnings: list[str] = []

        self._check(job)
        await progress(3, "🧠 Writing the script with Gemini…")
        script = await write_script(job.theme, settings=self.settings, gemini=self.gemini)
        if script.demo:
            warnings.append(
                "Gemini was unavailable, so a demo template script was used. "
                "Add `GEMINI_API_KEY` for fully original stories."
            )
        self._check(job)
        await progress(10, f"📜 Script ready: **{script.title}** · {len(script.scenes)} scenes")

        # -- voiceover -----------------------------------------------------
        speech: list[SpeechResult | None] = []
        durations: list[float] = []
        scene_count = len(script.scenes)
        for index, scene in enumerate(script.scenes, start=1):
            self._check(job)
            audio_name = f"narration_{index:02d}.mp3"
            try:
                result = await synthesize(
                    scene.narration, workdir / audio_name, settings=self.settings
                )
            except TTSError as exc:
                if index == 1:
                    raise renderer.RenderError(str(exc)) from exc
                duration = max(2.5, scene.duration_hint)
                await renderer.make_silence(
                    duration, audio_name, cwd=workdir, settings=self.settings
                )
                warnings.append(f"TTS failed for scene {index}; inserted silence instead.")
                result = SpeechResult(
                    path=workdir / audio_name,
                    duration=duration,
                    words=[],
                    voice="silence",
                    engine="silence",
                )
            speech.append(result)
            durations.append(
                min(12.0, max(2.5, result.duration + 0.6, scene.duration_hint))
            )
            await progress(
                12 + 28 * index / scene_count,
                f"🎙️ Voiceover {index}/{scene_count} · {result.engine} · {result.voice}",
            )

        # -- stock footage -------------------------------------------------
        assets: list[StockAsset | None] = []
        await progress(42, "🎥 Hunting vertical stock footage…")
        async with http_session(timeout=120, headers={"Accept": "*/*"}) as session:
            for index, scene in enumerate(script.scenes, start=1):
                self._check(job)
                asset, stock_warnings = await self.stock.locate(
                    session, scene.visual_query, assets_dir, index
                )
                if stock_warnings:
                    warnings.extend(w for w in stock_warnings if w not in warnings)
                assets.append(asset)
                await progress(
                    42 + 14 * index / scene_count,
                    f"🎥 Footage {index}/{scene_count} · `{scene.visual_query}`",
                )

        # -- per-scene visuals + narration --------------------------------
        clips: list[str] = []
        for index, (asset, audio, duration) in enumerate(
            zip(assets, speech, durations), start=1
        ):
            self._check(job)
            visual_name = f"visual_{index:02d}.mp4"
            clip_name = f"scene_{index:02d}.mp4"
            if asset is None:
                warnings.append(f"No footage for scene {index}; using a dark gradient card.")
                await renderer.make_color_visual(
                    duration, visual_name, cwd=workdir, settings=self.settings
                )
            else:
                await renderer.make_visual(
                    f"assets/{asset.path.name}",
                    is_video=asset.kind == "video",
                    duration=duration,
                    out_name=visual_name,
                    cwd=workdir,
                    settings=self.settings,
                )
            assert audio is not None
            await renderer.mux_narration(
                visual_name,
                audio.path.name,
                duration=duration,
                out_name=clip_name,
                cwd=workdir,
                settings=self.settings,
            )
            clips.append(clip_name)
            await progress(
                60 + 16 * index / scene_count,
                f"✂️ Editing scene {index}/{scene_count}",
            )

        # -- concat --------------------------------------------------------
        self._check(job)
        await progress(78, "🧩 Assembling the timeline…")
        await renderer.concat_scenes(clips, out_name="timeline.mp4", cwd=workdir, settings=self.settings)

        # -- captions ------------------------------------------------------
        caption_scenes: list[CaptionScene] = []
        offset = 0.0
        for scene, audio, duration in zip(script.scenes, speech, durations):
            words = shift_words(audio.words, offset) if audio and audio.words else []
            caption_scenes.append(
                CaptionScene(offset=offset, duration=duration, text=scene.narration, words=words)
            )
            offset += duration

        ass = build_ass(
            caption_scenes,
            title=script.title,
            settings=self.settings,
            total_duration=offset,
        )
        (workdir / "captions.ass").write_text(ass, encoding="utf-8")

        current = "timeline.mp4"
        await progress(84, "💬 Burning in captions…")
        try:
            await renderer.burn_captions(
                current,
                out_name="captioned.mp4",
                cwd=workdir,
                settings=self.settings,
            )
            current = "captioned.mp4"
        except renderer.RenderError as exc:
            warnings.append(f"Caption burn-in failed: {exc}")
            log.warning("Caption burn-in failed: %s", exc)

        # -- music ---------------------------------------------------------
        music = renderer.pick_music(self.settings.music_dir)
        if music:
            self._check(job)
            await progress(89, "🎵 Mixing background music…")
            music_name = f"music{music.suffix.lower()}"
            try:
                shutil.copyfile(music, workdir / music_name)
                await renderer.mix_music(
                    current,
                    music_name=music_name,
                    out_name="mixed.mp4",
                    cwd=workdir,
                    settings=self.settings,
                )
                current = "mixed.mp4"
            except (renderer.RenderError, OSError) as exc:
                warnings.append(f"Music mix failed: {exc}")

        # -- final encode / size ladder ------------------------------------
        self._check(job)
        await progress(93, "📦 Encoding the final reel…")
        if current != "final.mp4" and (workdir / current) != (workdir / "final.mp4"):
            shutil.copyfile(workdir / current, workdir / "final.mp4")
        final_path, ladder_warnings = await renderer.encode_ladder(
            workdir / "final.mp4",
            target_mb=float(target_upload_mb or self.settings.max_upload_mb),
            cwd=workdir,
            settings=self.settings,
        )
        warnings.extend(ladder_warnings)

        thumbnail: Path | None = None
        try:
            await renderer.make_thumbnail(
                final_path.name, out_name="thumbnail.jpg", cwd=workdir, settings=self.settings
            )
            thumbnail = workdir / "thumbnail.jpg"
        except renderer.RenderError:
            thumbnail = None

        duration = await renderer.probe_duration(
            final_path, cwd=workdir, ffprobe_bin=self.settings.ffprobe_bin
        ) or offset
        size_mb = final_path.stat().st_size / 1048576

        credits = attribution_line([a for a in assets if a])
        if credits:
            warnings.append(f"Footage credits: {credits}")

        await progress(99, "✨ Finishing touches…")
        self._cleanup_old_jobs(keep=job.id)
        job.status = "done"
        job.finished_at = utcnow()
        return MovieResult(
            script=script,
            video=final_path,
            thumbnail=thumbnail,
            duration=duration,
            size_mb=size_mb,
            workdir=workdir,
            assets=[a for a in assets if a],
            warnings=warnings,
            captioned=(workdir / "captioned.mp4").exists(),
        )

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _check(job: MovieJob) -> None:
        if job.cancelled:
            raise JobCancelled("Job cancelled")

    def _cleanup_old_jobs(self, *, keep: str | None = None) -> None:
        try:
            directories = [
                path for path in self.settings.workdir.iterdir() if path.is_dir()
            ]
        except OSError:
            return
        directories.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        kept = 0
        for path in directories:
            if keep and path.name == keep:
                kept += 1
                continue
            if kept < self.settings.keep_jobs:
                kept += 1
                continue
            shutil.rmtree(path, ignore_errors=True)


def output_filename(script: MovieScript) -> str:
    slug = safe_filename(script.title.lower().replace(" ", "-"), fallback="reel")
    return f"{slug}.mp4"
