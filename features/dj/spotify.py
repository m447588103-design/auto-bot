"""Spotify Web API curation.

Spotify audio cannot be streamed into a Discord voice channel (DRM), so Spotify
is used as a **curator**: it supplies genre-accurate tracks, and each pick is
resolved on YouTube for playback. This keeps the "real Spotify DJ" feeling
without violating Spotify's terms or breaking Discord.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any

import aiohttp

from core.config import DJSettings
from core.http import HTTPError, request

log = logging.getLogger(__name__)

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"


@dataclass
class SpotifySeed:
    title: str
    artist: str
    popularity: int = 0

    @property
    def query(self) -> str:
        return f"{self.artist} {self.title}".strip()


class SpotifyClient:
    def __init__(self, settings: DJSettings):
        self.settings = settings
        self._token: str | None = None
        self._expires_at: float = 0.0

    @property
    def configured(self) -> bool:
        return self.settings.spotify_configured

    async def _access_token(self, session: aiohttp.ClientSession) -> str | None:
        if not self.configured:
            return None
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        client_id = self.settings.spotify_client_id or ""
        secret = self.settings.spotify_client_secret or ""
        auth = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
        try:
            data = await request(
                session,
                "POST",
                TOKEN_URL,
                expect="json",
                data={"grant_type": "client_credentials"},
                headers={"Authorization": f"Basic {auth}"},
                retries=1,
                timeout=20,
            )
        except HTTPError as exc:
            log.warning("Spotify token request failed: %s", exc)
            return None
        token = (data or {}).get("access_token")
        expires_in = int((data or {}).get("expires_in") or 3600)
        if not token:
            return None
        self._token = token
        self._expires_at = time.time() + expires_in
        return token

    async def search_tracks(
        self, session: aiohttp.ClientSession, query: str, *, limit: int = 8
    ) -> list[SpotifySeed]:
        token = await self._access_token(session)
        if not token:
            return []
        try:
            data = await request(
                session,
                "GET",
                SEARCH_URL,
                expect="json",
                params={"q": query, "type": "track", "limit": max(1, min(limit, 20)), "market": "US"},
                headers={"Authorization": f"Bearer {token}"},
                retries=1,
                timeout=20,
            )
        except HTTPError as exc:
            log.warning("Spotify search failed: %s", exc)
            return []
        items = ((data or {}).get("tracks") or {}).get("items") or []
        seeds: list[SpotifySeed] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            artists = ", ".join(
                artist.get("name", "") for artist in item.get("artists", []) if artist.get("name")
            )
            if not artists:
                continue
            seeds.append(
                SpotifySeed(
                    title=str(item["name"])[:150],
                    artist=artists[:120],
                    popularity=int(item.get("popularity") or 0),
                )
            )
        return seeds

    async def seed_queries(
        self, session: aiohttp.ClientSession, plan: Any, *, limit: int = 3
    ) -> list[SpotifySeed]:
        """Use genres + mood to get concrete Spotify tracks to look up on YouTube."""
        if not self.configured:
            return []
        queries: list[str] = []
        for genre in list(getattr(plan, "genres", []))[:3]:
            queries.append(f"genre:{genre.replace(' ', '-')}")
        if not queries:
            queries.append(str(getattr(plan, "mood", "chill")))
        seen: set[str] = set()
        seeds: list[SpotifySeed] = []
        for query in queries:
            for seed in await self.search_tracks(session, query, limit=3):
                key = seed.query.lower()
                if key in seen:
                    continue
                seen.add(key)
                seeds.append(seed)
                if len(seeds) >= limit:
                    return seeds
        return seeds
