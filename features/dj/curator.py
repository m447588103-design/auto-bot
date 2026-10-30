"""Combine mood planning, Spotify curation and YouTube search into a playlist."""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from core.config import DJSettings
from core.gemini import GeminiClient, get_gemini
from core.http import http_session

from .models import Playlist, Track
from .mood import MoodPlan, plan_mood, shuffle_queries
from .spotify import SpotifyClient
from .youtube import YouTubeError, YouTubeResolver

log = logging.getLogger(__name__)


class DJError(RuntimeError):
    pass


class DJCurator:
    def __init__(
        self,
        settings: DJSettings,
        *,
        gemini: GeminiClient | None = None,
        resolver: YouTubeResolver | None = None,
        spotify: SpotifyClient | None = None,
    ):
        self.settings = settings
        self.gemini = gemini or get_gemini()
        self.resolver = resolver or YouTubeResolver(settings)
        self.spotify = spotify or SpotifyClient(settings)

    @property
    def spotify_available(self) -> bool:
        return self.spotify.configured

    async def plan(self, text: str) -> MoodPlan:
        return await plan_mood(text, self.gemini)

    async def build(
        self, text: str, *, limit: int = 6, requester_id: int | None = None
    ) -> Playlist:
        plan = await self.plan(text)
        seeds = []
        session_errors: list[str] = []

        async with http_session(timeout=45) as session:
            if self.spotify_available:
                try:
                    seeds = await self.spotify.seed_queries(session, plan, limit=3)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Spotify seed lookup failed: %s", exc)
                    session_errors.append(str(exc))

        search_tasks: list[asyncio.Future] = []
        labels: list[str] = []
        for seed in seeds[:3]:
            search_tasks.append(asyncio.create_task(self.resolver.search(seed.query, 1)))
            labels.append(f"spotify:{seed.query}")
        for query in plan.all_queries()[:3]:
            search_tasks.append(asyncio.create_task(self.resolver.search(query, 4)))
            labels.append(f"query:{query}")

        results = await asyncio.gather(*search_tasks, return_exceptions=True)
        groups: list[list[Track]] = []
        for label, outcome in zip(labels, results):
            if isinstance(outcome, Exception):
                log.warning("DJ search failed for %s: %s", label, outcome)
                session_errors.append(str(outcome))
                continue
            if outcome:
                groups.append(outcome)

        tracks = _interleave(groups, limit=limit)
        if not tracks:
            detail = f" ({session_errors[0]})" if session_errors else ""
            raise DJError(f"I could not find any playable music for that mood{detail}.")

        for track in tracks:
            track.requester_id = requester_id

        note = ""
        if seeds:
            note = f"Curated with Spotify ({len(seeds)} seed tracks) · playback via YouTube."
        if session_errors and not seeds:
            note = "Spotify was unavailable, so YouTube search was used directly."

        return Playlist(plan=plan, tracks=tracks, note=note)

    async def refill(
        self, plan: MoodPlan, *, exclude: Iterable[str] = (), limit: int = 3
    ) -> list[Track]:
        exclude_set = set(exclude)
        queries = shuffle_queries(plan, k=2) or ["chill music mix"]
        tasks = [asyncio.create_task(self.resolver.search(query, 4)) for query in queries]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        pool: list[Track] = []
        for outcome in results:
            if isinstance(outcome, Exception):
                continue
            pool.extend(outcome or [])
        fresh: list[Track] = []
        seen: set[str] = set()
        for track in pool:
            if track.key in exclude_set or track.key in seen:
                continue
            seen.add(track.key)
            fresh.append(track)
            if len(fresh) >= limit:
                break
        return fresh


def _interleave(groups: list[list[Track]], *, limit: int) -> list[Track]:
    """Round-robin across result groups so the queue is not one long search."""
    picked: list[Track] = []
    seen: set[str] = set()
    index = 0
    while len(picked) < limit and any(index < len(group) for group in groups):
        for group in groups:
            if index >= len(group):
                continue
            track = group[index]
            if track.key in seen:
                continue
            seen.add(track.key)
            picked.append(track)
            if len(picked) >= limit:
                break
        index += 1
    return picked


__all__ = ["DJCurator", "DJError", "YouTubeError"]
