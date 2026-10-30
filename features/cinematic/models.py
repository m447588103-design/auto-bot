"""Core data structures for the cinematic director."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Scene:
    index: int
    narration: str
    visual_query: str
    duration_hint: float = 5.0
    on_screen_text: str | None = None

    @property
    def estimated_seconds(self) -> float:
        # ~15 characters per second of narration is a good TTS average
        return max(2.5, min(12.0, len(self.narration) / 15.0))


@dataclass
class MovieScript:
    theme: str
    title: str
    logline: str
    scenes: list[Scene]
    music_mood: str = "cinematic ambient"
    hashtags: list[str] = field(default_factory=list)
    cta: str = "Follow for more AI stories."
    demo: bool = False

    @property
    def estimated_seconds(self) -> float:
        return sum(scene.estimated_seconds for scene in self.scenes)

    def narration_text(self) -> str:
        return " ".join(scene.narration for scene in self.scenes)

    def clean_hashtags(self) -> list[str]:
        out: list[str] = []
        for tag in self.hashtags:
            tag = str(tag).strip().lstrip("#")
            tag = "".join(ch for ch in tag if ch.isalnum() or ch == "_")
            if tag:
                out.append(f"#{tag}")
        # de-duplicate, keep order
        seen: set[str] = set()
        return [tag for tag in out if not (tag.lower() in seen or seen.add(tag.lower()))]


@dataclass
class StockAsset:
    path: Path
    kind: str  # "video" | "image"
    source: str
    query: str
    attribution: str | None = None
    duration: float | None = None


@dataclass
class SceneRender:
    scene: Scene
    asset: StockAsset | None
    audio_path: Path
    audio_duration: float
    visual_path: Path | None = None
    clip_path: Path | None = None
    words: list = field(default_factory=list)


@dataclass
class MovieResult:
    script: MovieScript
    video: Path
    thumbnail: Path | None
    duration: float
    size_mb: float
    workdir: Path
    assets: list[StockAsset] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    captioned: bool = False


@dataclass
class MovieJob:
    id: str
    theme: str
    guild_id: int
    channel_id: int
    requester_id: int
    requester_name: str
    publish_youtube: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    workdir: Path | None = None
    status: str = "queued"
    cancelled: bool = False
    result: MovieResult | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def cancel(self) -> None:
        self.cancelled = True
        if self.status in {"queued", "running"}:
            self.status = "cancelled"

    @property
    def short_theme(self) -> str:
        return self.theme if len(self.theme) <= 70 else self.theme[:67] + "…"
