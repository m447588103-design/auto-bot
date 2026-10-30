"""Pure ASS subtitle generation (unit-testable, no ffmpeg needed)."""

from __future__ import annotations

from dataclasses import dataclass, field

from core.config import CinematicSettings

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,{font},{title_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,5,3,8,70,70,170,1
Style: Caption,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,4,2,2,70,70,250,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


@dataclass
class Word:
    start: float
    end: float
    text: str


@dataclass
class CaptionScene:
    offset: float
    duration: float
    text: str
    words: list[Word] = field(default_factory=list)


@dataclass
class CaptionCue:
    start: float
    end: float
    text: str


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours}:{minutes:02d}:{secs:05.2f}"


def _escape(text: str) -> str:
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace("{", "(")
        .replace("}", ")")
        .replace("\n", "\\N")
        .strip()
    )


def _derive_words(scene: CaptionScene) -> list[Word]:
    """Fallback timing: distribute words proportionally across the scene."""
    tokens = [token for token in scene.text.split() if token]
    if not tokens:
        return []
    weights = [max(1, len(token)) for token in tokens]
    total_weight = sum(weights)
    usable = max(0.5, scene.duration - 0.15)
    words: list[Word] = []
    cursor = scene.offset
    for token, weight in zip(tokens, weights):
        span = usable * weight / total_weight
        words.append(Word(start=cursor, end=cursor + span, text=token))
        cursor += span
    return words


def _wrap_lines(words: list[str], max_chars: int = 30) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > 2:
        # merge the overflow into a second line, dropping the rest keeps timing sane
        lines = [lines[0], " ".join(lines[1:])]
    return lines[:2]


def group_words(
    scene: CaptionScene,
    words: list[Word] | None,
    *,
    max_words: int = 5,
    max_chars: int = 30,
    max_duration: float = 3.6,
) -> list[CaptionCue]:
    """Group word timings into short, readable on-screen cues."""
    words = words or _derive_words(scene)
    if not words:
        return [CaptionCue(scene.offset, scene.offset + scene.duration, _escape(scene.text))]

    cues: list[CaptionCue] = []
    bucket: list[Word] = []

    def flush() -> None:
        nonlocal bucket
        if not bucket:
            return
        lines = _wrap_lines([word.text for word in bucket], max_chars=max_chars)
        text = "\\N".join(_escape(line) for line in lines)
        start = bucket[0].start
        end = max(bucket[-1].end, start + 0.45)
        end = min(end, scene.offset + scene.duration + 0.35)
        cues.append(CaptionCue(start, end, text))
        bucket = []

    for word in words:
        if bucket:
            combined = " ".join([w.text for w in bucket] + [word.text])
            too_long = len(combined) > max_chars * 2
            too_many = len(bucket) >= max_words
            too_slow = word.end - bucket[0].start > max_duration
            gap = word.start - bucket[-1].end > 0.45
            if too_long or too_many or too_slow or gap:
                flush()
        bucket.append(word)
    flush()
    return cues


def build_ass(
    scenes: list[CaptionScene],
    *,
    title: str,
    settings: CinematicSettings,
    total_duration: float | None = None,
) -> str:
    """Render a full ASS file for a movie."""
    font = settings.caption_font or "DejaVu Sans"
    size = max(24, settings.caption_size)
    title_size = int(size * 1.5)
    out = [ASS_HEADER.format(font=font, size=size, title_size=title_size)]

    clean_title = _escape(title)
    if clean_title:
        title_end = min(3.2, max(1.8, total_duration / 6 if total_duration else 3.0))
        out.append(
            f"Dialogue: 0,{_ass_time(0.15)},{_ass_time(0.15 + title_end)},Title,,0,0,0,,"
            f"{clean_title}\n"
        )

    for scene in scenes:
        for cue in group_words(scene, scene.words):
            if cue.end <= cue.start:
                continue
            text = cue.text
            out.append(
                f"Dialogue: 0,{_ass_time(cue.start)},{_ass_time(cue.end)},Caption,,0,0,0,,{text}\n"
            )
    return "".join(out)


def shift_words(words: list[Word], offset: float) -> list[Word]:
    return [Word(start=word.start + offset, end=word.end + offset, text=word.text) for word in words]
