"""Text-to-speech with word-level timings for burned-in captions.

Primary engine: **edge-tts** (free Microsoft neural voices, no API key).
Fallback: **gTTS** (if installed). Both are optional dependencies.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from core.config import CinematicSettings

from .captions import Word

log = logging.getLogger(__name__)

_BENGALI_RE = re.compile(r"[\u0980-\u09FF]")


class TTSError(RuntimeError):
    pass


@dataclass
class SpeechResult:
    path: Path
    duration: float
    words: list[Word] = field(default_factory=list)
    voice: str = "unknown"
    engine: str = "unknown"


def has_bengali(text: str) -> bool:
    return bool(_BENGALI_RE.search(text or ""))


def pick_voice(text: str, voices: tuple[str, ...] | list[str]) -> str:
    voices = list(voices) or ["en-US-AriaNeural"]
    if has_bengali(text):
        for voice in voices:
            if voice.lower().startswith(("bn-", "bn_")) or "-bn-" in voice.lower():
                return voice
    for voice in voices:
        if voice.lower().startswith("en-"):
            return voice
    return voices[0]


def estimate_duration(text: str) -> float:
    """Rough TTS duration used when ffprobe is unavailable."""
    words = max(1, len(text.split()))
    return max(1.5, words / 2.6)


async def probe_duration(path: Path, ffprobe_bin: str = "ffprobe") -> float | None:
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
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    except (FileNotFoundError, asyncio.TimeoutError, OSError):
        return None
    try:
        value = float(stdout.decode().strip())
    except (ValueError, AttributeError):
        return None
    return value if value > 0 else None


async def synthesize(
    text: str,
    out_path: Path,
    *,
    settings: CinematicSettings,
    voice: str | None = None,
) -> SpeechResult:
    """Generate narration audio for ``text`` and return timings."""
    text = (text or "").strip()
    if not text:
        raise TTSError("Cannot synthesize empty narration")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    chosen_voice = voice or pick_voice(text, settings.tts_voices)

    errors: list[str] = []
    words: list[Word] = []
    engine = "edge-tts"
    try:
        words = await _edge_tts(text, out_path, chosen_voice)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"edge-tts: {exc}")
        log.warning("edge-tts failed (%s), trying gTTS fallback", exc)
        engine = "gTTS"
        try:
            await _gtts(text, out_path, "bn" if has_bengali(text) else "en")
            words = []
            chosen_voice = engine
        except Exception as fallback_exc:  # noqa: BLE001
            errors.append(f"gTTS: {fallback_exc}")
            raise TTSError(
                "No text-to-speech engine available. Install `edge-tts` (recommended) "
                "or `gTTS`, and make sure ffmpeg is installed. Details: " + " | ".join(errors)
            ) from fallback_exc

    duration = await probe_duration(out_path, settings.ffprobe_bin)
    if duration is None:
        duration = words[-1].end if words else estimate_duration(text)
    if duration <= 0:
        raise TTSError("Generated audio has zero duration")
    return SpeechResult(
        path=out_path, duration=duration, words=words, voice=chosen_voice, engine=engine
    )


async def _edge_tts(text: str, out_dir: Path, voice: str) -> list[Word]:
    import edge_tts  # imported lazily so the bot boots without the package

    communicate = edge_tts.Communicate(text, voice)
    words: list[Word] = []
    buffer = bytearray()
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio" and chunk.get("data"):
            buffer.extend(chunk["data"])
        elif chunk.get("type") == "WordBoundary":
            start = float(chunk.get("offset", 0)) / 10_000_000
            span = float(chunk.get("duration", 0)) / 10_000_000
            token = str(chunk.get("text", "")).strip()
            if token:
                words.append(Word(start=start, end=start + max(0.12, span), text=token))
    if not buffer:
        raise TTSError("edge-tts returned no audio")
    out_dir.write_bytes(bytes(buffer))
    return words


async def _gtts(text: str, out_path: Path, lang: str) -> None:
    from gtts import gTTS  # optional fallback

    def _save() -> None:
        gTTS(text=text, lang=lang, slow=False).save(str(out_path))

    await asyncio.to_thread(_save)
