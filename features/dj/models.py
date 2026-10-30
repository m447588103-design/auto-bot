"""Shared DJ data structures."""

from __future__ import annotations

from dataclasses import dataclass, field

from core.utils import human_duration


@dataclass
class Track:
    title: str
    artist: str | None = None
    video_id: str | None = None
    url: str | None = None
    duration: float | None = None
    thumbnail: str | None = None
    query: str | None = None
    source: str = "youtube"
    requester_id: int | None = None
    resolved_stream: str | None = field(default=None, repr=False)

    @property
    def key(self) -> str:
        return self.video_id or (self.url or self.query or self.title).lower()

    @property
    def display(self) -> str:
        if self.artist:
            return f"{self.artist} — {self.title}"
        return self.title

    @property
    def duration_text(self) -> str:
        return human_duration(self.duration) if self.duration else "live/unknown"

    @property
    def watch_url(self) -> str | None:
        if self.url:
            return self.url
        if self.video_id:
            return f"https://www.youtube.com/watch?v={self.video_id}"
        return None


@dataclass
class Playlist:
    plan: object
    tracks: list[Track]
    note: str = ""

    def __len__(self) -> int:
        return len(self.tracks)
