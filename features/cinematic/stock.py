"""Stock footage retrieval.

Priority:
1. **Pexels** (vertical HD videos, requires ``PEXELS_API_KEY``; free tier).
2. **Pexels photos** (same key) with a Ken Burns pan.
3. **picsum.photos** keyless fallback so the pipeline always renders something.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import aiohttp

from core.config import CinematicSettings
from core.http import HTTPError, get_json
from core.utils import safe_filename, url_hash

from .models import StockAsset

log = logging.getLogger(__name__)

PEXELS_VIDEO_SEARCH = "https://api.pexels.com/videos/search"
PEXELS_PHOTO_SEARCH = "https://api.pexels.com/v1/search"
PICSUM = "https://picsum.photos/seed/{seed}/1080/1920"
MAX_DOWNLOAD_BYTES = 60 * 1024 * 1024


class StockError(RuntimeError):
    pass


async def download(
    session: aiohttp.ClientSession, url: str, destination: Path, *, max_bytes: int = MAX_DOWNLOAD_BYTES
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as response:
        if response.status >= 400:
            raise StockError(f"Download failed ({response.status}) for {url}")
        with destination.open("wb") as handle:
            async for chunk in response.content.iter_chunked(256 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    handle.close()
                    destination.unlink(missing_ok=True)
                    raise StockError("Stock asset exceeded the download size limit")
                handle.write(chunk)
    if written == 0:
        destination.unlink(missing_ok=True)
        raise StockError("Downloaded asset was empty")
    return destination


@dataclass
class PexelsVideo:
    id: int
    duration: float
    url: str
    author: str
    link: str


class PexelsProvider:
    name = "pexels"

    def __init__(self, api_key: str, timeout: float = 30.0):
        self.api_key = api_key
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self.api_key}

    async def search_videos(
        self, session: aiohttp.ClientSession, query: str, *, per_page: int = 5
    ) -> list[dict]:
        data = await get_json(
            session,
            PEXELS_VIDEO_SEARCH,
            params={"query": query, "orientation": "portrait", "size": "medium", "per_page": per_page},
            headers=self._headers(),
            timeout=self.timeout,
            retries=1,
        )
        return (data or {}).get("videos", []) or []

    async def search_photos(
        self, session: aiohttp.ClientSession, query: str, *, per_page: int = 5
    ) -> list[dict]:
        data = await get_json(
            session,
            PEXELS_PHOTO_SEARCH,
            params={"query": query, "orientation": "portrait", "per_page": per_page},
            headers=self._headers(),
            timeout=self.timeout,
            retries=1,
        )
        return (data or {}).get("photos", []) or []

    @staticmethod
    def _best_video_file(video: dict) -> dict | None:
        files = [f for f in video.get("video_files", []) if f.get("file_type") == "video/mp4"]
        if not files:
            return None
        portrait = [f for f in files if (f.get("height") or 0) > (f.get("width") or 0)]
        pool = portrait or files
        # smallest portrait file with height >= 1280, else the largest available
        hd = [f for f in pool if (f.get("height") or 0) >= 1280]
        if hd:
            return min(hd, key=lambda f: (f.get("height") or 0) * (f.get("width") or 0))
        return max(pool, key=lambda f: (f.get("height") or 0) * (f.get("width") or 0))


class PicsumProvider:
    name = "picsum"
    available = True

    async def fetch_image(self, session: aiohttp.ClientSession, query: str, destination: Path) -> StockAsset:
        seed = safe_filename(url_hash(query, random.random(), length=12), fallback="scene")
        url = PICSUM.format(seed=seed)
        await download(session, url, destination)
        return StockAsset(
            path=destination,
            kind="image",
            source="picsum",
            query=query,
            attribution="Photo: picsum.photos",
        )


class StockLibrary:
    """Chooses the best available provider per scene."""

    def __init__(self, settings: CinematicSettings):
        self.settings = settings
        self.pexels = PexelsProvider(settings.pexels_api_key or "")
        self.picsum = PicsumProvider()
        self._pexels_failures = 0

    @property
    def primary(self) -> str:
        return "pexels" if self.pexels.available else "picsum"

    async def locate(
        self,
        session: aiohttp.ClientSession,
        query: str,
        destination_dir: Path,
        index: int,
    ) -> tuple[StockAsset | None, list[str]]:
        warnings: list[str] = []
        destination_dir.mkdir(parents=True, exist_ok=True)

        if self.pexels.available and self._pexels_failures < 2:
            try:
                asset = await self._from_pexels_video(session, query, destination_dir, index)
                if asset:
                    return asset, warnings
            except (HTTPError, StockError, asyncio.TimeoutError) as exc:
                self._pexels_failures += 1
                warnings.append(f"Pexels video failed for '{query}': {exc}")
                log.warning("Pexels video search failed: %s", exc)
            try:
                asset = await self._from_pexels_photo(session, query, destination_dir, index)
                if asset:
                    return asset, warnings
            except (HTTPError, StockError, asyncio.TimeoutError) as exc:
                self._pexels_failures += 1
                warnings.append(f"Pexels photo failed for '{query}': {exc}")

        try:
            destination = destination_dir / f"scene_{index:02d}.jpg"
            asset = await self.picsum.fetch_image(session, query, destination)
            warnings.append("Used picsum.photos fallback image (no Pexels key or no results).")
            return asset, warnings
        except (aiohttp.ClientError, StockError, asyncio.TimeoutError, OSError) as exc:
            warnings.append(f"Image fallback failed: {exc}")
            return None, warnings

    async def _from_pexels_video(
        self, session: aiohttp.ClientSession, query: str, destination_dir: Path, index: int
    ) -> StockAsset | None:
        videos = await self.pexels.search_videos(session, query)
        for video in videos:
            file_info = self.pexels._best_video_file(video)
            if not file_info or not file_info.get("link"):
                continue
            destination = destination_dir / f"scene_{index:02d}.mp4"
            try:
                await download(session, file_info["link"], destination)
            except StockError as exc:
                log.debug("Skipping video file: %s", exc)
                continue
            author = (video.get("user") or {}).get("name") or "Pexels creator"
            return StockAsset(
                path=destination,
                kind="video",
                source="pexels",
                query=query,
                attribution=f"Video: {author} / Pexels",
                duration=float(video.get("duration") or 0) or None,
            )
        return None

    async def _from_pexels_photo(
        self, session: aiohttp.ClientSession, query: str, destination_dir: Path, index: int
    ) -> StockAsset | None:
        photos = await self.pexels.search_photos(session, query)
        for photo in photos:
            src = (photo.get("src") or {}).get("portrait") or (photo.get("src") or {}).get("large2x")
            if not src:
                continue
            destination = destination_dir / f"scene_{index:02d}.jpg"
            try:
                await download(session, src, destination)
            except StockError:
                continue
            author = photo.get("photographer") or "Pexels creator"
            return StockAsset(
                path=destination,
                kind="image",
                source="pexels",
                query=query,
                attribution=f"Photo: {author} / Pexels",
            )
        return None


def attribution_line(assets: Sequence[StockAsset]) -> str | None:
    credits = []
    for asset in assets:
        if asset.attribution and asset.attribution not in credits:
            credits.append(asset.attribution)
    if not credits:
        return None
    return " · ".join(credits[:5])
