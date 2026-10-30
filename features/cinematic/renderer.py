"""ffmpeg based video assembly for vertical 1080x1920 reels."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Sequence

from core.config import CinematicSettings

log = logging.getLogger(__name__)

WIDTH, HEIGHT, FPS = 1080, 1920, 30


class RenderError(RuntimeError):
    pass


def ffmpeg_installed(binary: str) -> bool:
    return shutil.which(binary) is not None


def ffprobe_installed(binary: str) -> bool:
    return shutil.which(binary) is not None


async def run(cmd: Sequence[str], *, cwd: Path, timeout: float = 1800.0) -> None:
    """Run a command inside ``cwd``; raise :class:`RenderError` on failure."""
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RenderError(f"Command timed out after {timeout:.0f}s: {cmd[0]}") from exc
    if process.returncode != 0:
        tail = (stderr or b"").decode("utf-8", "ignore").strip().splitlines()[-8:]
        raise RenderError("ffmpeg failed: " + " | ".join(tail)[:900])


async def probe_duration(
    path: Path, *, cwd: Path, ffprobe_bin: str = "ffprobe"
) -> float | None:
    try:
        process = await asyncio.create_subprocess_exec(
            ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    except (FileNotFoundError, asyncio.TimeoutError, OSError):
        return None
    try:
        return float(stdout.decode().strip())
    except (ValueError, AttributeError):
        return None


async def make_silence(duration: float, out_name: str, *, cwd: Path, settings: CinematicSettings) -> None:
    await run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t",
            f"{duration:.2f}",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            out_name,
        ],
        cwd=cwd,
        timeout=120,
    )


async def make_color_visual(
    duration: float, out_name: str, *, cwd: Path, settings: CinematicSettings, color: str = "0x101820"
) -> None:
    await run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={WIDTH}x{HEIGHT}:r={FPS}",
            "-t",
            f"{duration:.2f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
            out_name,
        ],
        cwd=cwd,
        timeout=300,
    )


async def make_visual(
    asset_name: str,
    *,
    is_video: bool,
    duration: float,
    out_name: str,
    cwd: Path,
    settings: CinematicSettings,
) -> None:
    frames = max(int(duration * FPS), FPS)
    if is_video:
        video_filter = (
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},fps={FPS},setsar=1"
        )
        cmd = [
            settings.ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-stream_loop",
            "-1",
            "-i",
            asset_name,
            "-t",
            f"{duration:.2f}",
            "-an",
            "-vf",
            video_filter,
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            out_name,
        ]
    else:
        # Ken Burns effect: zoom a portrait still up to 1.18x over the scene.
        video_filter = (
            f"scale={WIDTH * 2}:{HEIGHT * 2}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH * 2}:{HEIGHT * 2},"
            f"zoompan=z='min(zoom+0.0012,1.18)'"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS},setsar=1"
        )
        cmd = [
            settings.ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-framerate",
            str(FPS),
            "-i",
            asset_name,
            "-vf",
            video_filter,
            "-t",
            f"{duration:.2f}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            out_name,
        ]
    await run(cmd, cwd=cwd, timeout=600)


async def mux_narration(
    visual_name: str,
    audio_name: str,
    *,
    duration: float,
    out_name: str,
    cwd: Path,
    settings: CinematicSettings,
) -> None:
    await run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            visual_name,
            "-i",
            audio_name,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-af",
            "apad",
            "-t",
            f"{duration:.2f}",
            "-movflags",
            "+faststart",
            out_name,
        ],
        cwd=cwd,
        timeout=300,
    )


async def concat_scenes(
    scene_names: Sequence[str],
    *,
    out_name: str,
    cwd: Path,
    settings: CinematicSettings,
) -> None:
    if not scene_names:
        raise RenderError("No scene clips to concatenate")
    list_file = cwd / "concat.txt"
    list_file.write_text(
        "".join(f"file '{name}'\n" for name in scene_names), encoding="utf-8"
    )
    try:
        await run(
            [
                settings.ffmpeg_bin,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                "concat.txt",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                out_name,
            ],
            cwd=cwd,
            timeout=600,
        )
        if (cwd / out_name).stat().st_size > 0:
            return
    except (RenderError, FileNotFoundError) as exc:
        log.warning("Stream-copy concat failed, re-encoding: %s", exc)

    # Fallback: re-encode through the concat filter (handles odd timestamps).
    inputs: list[str] = []
    for name in scene_names:
        inputs.extend(["-i", name])
    filter_parts = "".join(f"[{i}:v][{i}:a]" for i in range(len(scene_names)))
    filter_complex = f"{filter_parts}concat=n={len(scene_names)}:v=1:a=1[v][a]"
    await run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            *inputs,
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "21",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            out_name,
        ],
        cwd=cwd,
        timeout=1200,
    )


async def burn_captions(
    input_name: str,
    *,
    captions_name: str = "captions.ass",
    out_name: str,
    cwd: Path,
    settings: CinematicSettings,
) -> None:
    await run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            input_name,
            "-vf",
            f"ass={captions_name}",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "21",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            out_name,
        ],
        cwd=cwd,
        timeout=1200,
    )


async def mix_music(
    input_name: str,
    *,
    music_name: str,
    out_name: str,
    cwd: Path,
    settings: CinematicSettings,
    volume: float = 0.10,
) -> None:
    await run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            input_name,
            "-stream_loop",
            "-1",
            "-i",
            music_name,
            "-filter_complex",
            f"[1:a]volume={volume:.3f},afade=t=in:st=0:d=2[m];"
            f"[0:a][m]amix=inputs=2:duration=first:normalize=0[a]",
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-shortest",
            "-movflags",
            "+faststart",
            out_name,
        ],
        cwd=cwd,
        timeout=900,
    )


async def compress(
    input_name: str,
    *,
    out_name: str,
    width: int,
    height: int,
    crf: int,
    audio_kbps: int,
    cwd: Path,
    settings: CinematicSettings,
) -> None:
    await run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            input_name,
            "-vf",
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(crf),
            "-maxrate",
            "2500k",
            "-bufsize",
            "5000k",
            "-r",
            str(FPS),
            "-c:a",
            "aac",
            "-b:a",
            f"{audio_kbps}k",
            "-movflags",
            "+faststart",
            out_name,
        ],
        cwd=cwd,
        timeout=1200,
    )


async def make_thumbnail(
    input_name: str, *, out_name: str, cwd: Path, settings: CinematicSettings
) -> None:
    await run(
        [
            settings.ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            "1.0",
            "-i",
            input_name,
            "-frames:v",
            "1",
            "-q:v",
            "3",
            out_name,
        ],
        cwd=cwd,
        timeout=120,
    )


LADDER = (
    (720, 1280, 28, 96),
    (540, 960, 30, 80),
    (480, 854, 33, 64),
)


async def encode_ladder(
    path: Path,
    *,
    target_mb: float,
    cwd: Path,
    settings: CinematicSettings,
) -> tuple[Path, list[str]]:
    """Re-encode until the file fits Discord's upload limit."""
    warnings: list[str] = []
    target_bytes = int(target_mb * 1024 * 1024)
    if path.stat().st_size <= target_bytes:
        return path, warnings

    for width, height, crf, audio in LADDER:
        out_name = f"final_{width}.mp4"
        try:
            await compress(
                path.name,
                out_name=out_name,
                width=width,
                height=height,
                crf=crf,
                audio_kbps=audio,
                cwd=cwd,
                settings=settings,
            )
        except RenderError as exc:
            warnings.append(f"Compression to {width}px failed: {exc}")
            continue
        candidate = cwd / out_name
        if not candidate.exists():
            continue
        if candidate.stat().st_size <= target_bytes:
            warnings.append(f"Re-encoded to {width}x{height} to fit the upload limit.")
            return candidate, warnings
        warnings.append(f"{width}px still too large ({candidate.stat().st_size / 1048576:.1f} MB).")
    # Nothing fit: return the smallest attempt (or original)
    candidates = [cwd / f"final_{w}.mp4" for w, *_ in LADDER]
    existing = [c for c in candidates if c.exists()]
    if existing:
        smallest = min(existing, key=lambda c: c.stat().st_size)
        warnings.append("Could not fit the upload limit; sending the smallest encode.")
        return smallest, warnings
    return path, warnings


def pick_music(music_dir: Path) -> Path | None:
    if not music_dir or not music_dir.exists():
        return None
    files = [
        path
        for path in music_dir.iterdir()
        if path.is_file() and path.suffix.lower() in {".mp3", ".m4a", ".aac", ".ogg", ".wav", ".flac"}
    ]
    if not files:
        return None
    import random

    return random.choice(files)
