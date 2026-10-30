"""YouTube search + audio stream resolution via yt-dlp."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from core.config import DJSettings

from .models import Track

log = logging.getLogger(__name__)

MAX_TRACK_SECONDS = 1800  # skip 30+ minute mixes
MIN_TRACK_SECONDS = 30


class YouTubeError(RuntimeError):
    pass


class YouTubeResolver:
    def __init__(self, settings: DJSettings):
        self.settings = settings
        self._stream_cache: dict[str, tuple[str, float]] = {}
        self._cache_ttl = 3600.0

    # -- options -----------------------------------------------------------
    def _base_opts(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "skip_download": True,
            "noplaylist": True,
            "cachedir": False,
            "socket_timeout": 20,
            "retries": 2,
            "extract_flat": "in_playlist",
        }
        if self.settings.ytdlp_cookies_file and self.settings.ytdlp_cookies_file.exists():
            opts["cookiefile"] = str(self.settings.ytdlp_cookies_file)
        return opts

    @staticmethod
    def _require_ytdlp():
        try:
            import yt_dlp  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise YouTubeError("yt-dlp is not installed. Run `pip install yt-dlp`.") from exc

    # -- search ------------------------------------------------------------
    async def search(self, query: str, limit: int = 5) -> list[Track]:
        return await asyncio.to_thread(self._search_sync, query, limit)

    def _search_sync(self, query: str, limit: int) -> list[Track]:
        self._require_ytdlp()
        import yt_dlp

        query = (query or "").strip()
        if not query:
            return []
        opts = self._base_opts()
        search_term = query if query.startswith(("http://", "https://")) else f"ytsearch{limit}:{query}"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(search_term, download=False)
        except Exception as exc:  # noqa: BLE001 - yt-dlp raises many error types
            raise YouTubeError(self._friendly_error(exc)) from exc

        entries = (info or {}).get("entries") or []
        tracks: list[Track] = []
        for entry in entries:
            track = self._entry_to_track(entry)
            if track:
                tracks.append(track)
        return tracks

    async def first(self, query: str) -> Track | None:
        tracks = await self.search(query, limit=2)
        return tracks[0] if tracks else None

    @staticmethod
    def _entry_to_track(entry: dict[str, Any] | None) -> Track | None:
        if not isinstance(entry, dict):
            return None
        video_id = entry.get("id")
        if not video_id or not re.fullmatch(r"[\w-]{6,20}", str(video_id)):
            return None
        duration = entry.get("duration")
        try:
            duration_value = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_value = None
        if duration_value is not None and not (MIN_TRACK_SECONDS <= duration_value <= MAX_TRACK_SECONDS):
            return None
        title = str(entry.get("title") or "Unknown title").strip()
        artist = (
            entry.get("artist")
            or entry.get("creator")
            or entry.get("uploader")
            or entry.get("channel")
        )
        thumbnails = entry.get("thumbnails") or []
        thumbnail = entry.get("thumbnail")
        if not thumbnail and thumbnails:
            thumbnail = thumbnails[-1].get("url") if isinstance(thumbnails[-1], dict) else None
        return Track(
            title=title[:200],
            artist=str(artist)[:120] if artist else None,
            video_id=str(video_id),
            url=f"https://www.youtube.com/watch?v={video_id}",
            duration=duration_value,
            thumbnail=thumbnail,
            source="youtube",
        )

    # -- stream url --------------------------------------------------------
    async def stream_url(self, track: Track) -> str:
        cached = self._stream_cache.get(track.key)
        if cached and time.time() - cached[1] < self._cache_ttl:
            return cached[0]
        url = await asyncio.to_thread(self._resolve_sync, track)
        self._stream_cache[track.key] = (url, time.time())
        return url

    def _resolve_sync(self, track: Track) -> str:
        self._require_ytdlp()
        import yt_dlp

        target = track.watch_url or track.query
        if not target:
            raise YouTubeError("Track has no video id or search query")

        opts = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "skip_download": True,
            "noplaylist": True,
            "cachedir": False,
            "socket_timeout": 20,
            "retries": 2,
            "format": "bestaudio[abr<=192]/bestaudio/best",
            "extract_flat": False,
        }
        if self.settings.ytdlp_cookies_file and self.settings.ytdlp_cookies_file.exists():
            opts["cookiefile"] = str(self.settings.ytdlp_cookies_file)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(target, download=False)
        except Exception as exc:  # noqa: BLE001
            raise YouTubeError(self._friendly_error(exc)) from exc

        if not isinstance(info, dict):
            raise YouTubeError("yt-dlp returned no stream information")
        if info.get("entries"):
            entries = [e for e in info["entries"] if e]
            if not entries:
                raise YouTubeError("No playable result for that track")
            info = entries[0]
        stream = info.get("url")
        if not stream:
            formats = info.get("requested_formats") or []
            for fmt in formats:
                if fmt.get("acodec") != "none" and fmt.get("url"):
                    stream = fmt["url"]
                    break
        if not stream:
            raise YouTubeError("Could not extract an audio stream (video may be region-locked)")
        if not track.title or track.title == "Unknown title":
            track.title = str(info.get("title") or track.title)[:200]
        if not track.duration and info.get("duration"):
            track.duration = float(info["duration"])
        if not track.thumbnail and info.get("thumbnail"):
            track.thumbnail = info["thumbnail"]
        return str(stream)

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        message = str(exc)
        lowered = message.lower()
        if "sign in" in lowered or "bot" in lowered and "confirm" in lowered:
            return (
                "YouTube is asking this server to sign in. Export cookies with "
                "`YTDLP_COOKIES_FILE=credentials/ytdlp-cookies.txt`."
            )
        if "unable to download" in lowered or "403" in lowered:
            return "YouTube refused the stream request (403). Try again or use cookies."
        return f"YouTube error: {message[:400]}"


def parse_video_id(value: str) -> str | None:
    value = (value or "").strip()
    match = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{6,20})", value)
    return match.group(1) if match else None
