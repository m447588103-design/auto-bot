"""Small pure helpers shared across features (easily unit-tested)."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from datetime import datetime, timezone
from typing import Iterable, Iterator, Sequence, TypeVar

T = TypeVar("T")

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS_RE = re.compile(r"[ \t\f\v]+")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def url_hash(*parts: object, length: int = 16) -> str:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return digest[:length]


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    text = (text or "").strip()
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def clean_text(text: str | None, *, limit: int | None = None) -> str:
    """Strip HTML, control characters and collapse whitespace."""
    if not text:
        return ""
    value = html.unescape(text)
    value = re.sub(r"<[^>]+>", " ", value)
    value = unicodedata.normalize("NFKC", value)
    value = _CONTROL_RE.sub(" ", value)
    value = _WS_RE.sub(" ", value).replace("\r", "").strip()
    if limit is not None:
        value = truncate(value, limit)
    return value


def chunked(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    if size <= 0:
        raise ValueError("size must be positive")
    for start in range(0, len(items), size):
        yield items[start : start + size]


def split_message(text: str, limit: int = 1900) -> list[str]:
    """Split long text on paragraph/sentence boundaries for Discord."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n"):
        paragraph = paragraph.rstrip()
        while len(paragraph) > limit:
            cut = paragraph.rfind(" ", 0, limit)
            if cut < limit // 2:
                cut = limit
            piece, paragraph = paragraph[:cut], paragraph[cut:].lstrip()
            if current:
                chunks.append(current)
                current = ""
            chunks.append(piece)
        candidate = f"{current}\n{paragraph}" if current else paragraph
        if len(candidate) > limit:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]


def human_duration(seconds: float | int | None) -> str:
    if seconds is None:
        return "unknown"
    seconds = int(max(0, seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def human_number(value: float | int | None, prefix: str = "$") -> str:
    if value is None:
        return "n/a"
    value = float(value)
    sign = "-" if value < 0 else ""
    value = abs(value)
    for cut, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if value >= cut:
            body = f"{value / cut:,.2f}".rstrip("0").rstrip(".")
            return f"{sign}{prefix}{body}{suffix}"
    if value < 0.01:
        return f"{sign}{prefix}{value:.6f}"
    if value < 1:
        return f"{sign}{prefix}{value:.4f}"
    return f"{sign}{prefix}{value:,.2f}"


def pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.{digits}f}%"


def progress_bar(percent: float, width: int = 12) -> str:
    percent = clamp(percent, 0, 100)
    filled = int(round(width * percent / 100))
    return "█" * filled + "░" * (width - filled)


def format_ts(dt: datetime | None, style: str = "R") -> str:
    """Discord relative/absolute timestamp markdown."""
    if dt is None:
        return "n/a"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"<t:{int(dt.timestamp())}:{style}>"


def dedupe(items: Iterable[T]) -> list[T]:
    seen: set[T] = set()
    out: list[T] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw[:19], fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def safe_filename(name: str, fallback: str = "file") -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (name or "").strip()).strip("-.")
    return truncate(slug, 60, suffix="") or fallback


def mask_secret(value: str, keep: int = 2) -> str:
    """Partially mask an email/username/phone for public alerts."""
    if not value:
        return ""
    if "@" in value:
        local, _, domain = value.partition("@")
        head = local[:keep] if len(local) > keep else local[:1]
        masked = head + "*" * max(2, len(local) - keep)
        d_head = domain[:1]
        return f"{masked}@{d_head}{'*' * max(2, len(domain) - 1)}"
    if len(value) <= keep * 2:
        return value[:1] + "*" * max(1, len(value) - 1)
    return value[:keep] + "*" * (len(value) - keep * 2) + value[-keep:]
