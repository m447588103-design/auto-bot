"""Social hype detection from Reddit, X (optional) and Google News RSS."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import xml.etree.ElementTree as ET
from typing import Iterable
from urllib.parse import quote_plus

import aiohttp

from core.config import SniperSettings
from core.http import HTTPError, get_json, get_text, http_session, request

from .models import (
    CoinQuote,
    HypeItem,
    HypeSnapshot,
    NewsItem,
    RedditPost,
    TrendingCoin,
)

log = logging.getLogger(__name__)

REDDIT_URL = "https://www.reddit.com/r/{subreddit}/hot.json?limit=100&raw_json=1"
GOOGLE_NEWS = "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
X_SEARCH = "https://api.x.com/2/tweets/search/recent"

# Symbols that are also common English words need an explicit cashtag/uppercase mention.
AMBIGUOUS_SYMBOLS = {
    "NEAR", "LINK", "RENDER", "SAND", "FLOW", "CAKE", "GRASS", "MOVE", "ONDO",
    "USUAL", "AERO", "VIRTUAL", "ONE", "MANA", "MASK", "DOT", "OP", "AR", "AI",
    "W", "TON", "SUI", "SEI", "TIA", "ENA", "PYTH", "JUP", "RAY", "GMT", "APE",
}

_CASHTAG_RE = re.compile(r"\$([A-Za-z]{2,10})\b")
_UPPER_TOKEN_RE = re.compile(r"\b([A-Z]{2,10})\b")


class HypeEngine:
    def __init__(self, settings: SniperSettings):
        self.settings = settings

    # -- reddit ------------------------------------------------------------
    async def collect_posts(
        self, subreddits: Iterable[str] | None = None
    ) -> tuple[list[RedditPost], list[str]]:
        subs = list(subreddits or self.settings.subreddits)[:6]
        errors: list[str] = []
        if not subs:
            return [], ["No subreddits configured"]
        headers = {"User-Agent": "AutoBot-Sniper/2.0 (Discord market bot)"}
        async with http_session(timeout=self.settings.request_timeout, headers=headers) as session:
            results = await asyncio.gather(
                *[self._fetch_subreddit(session, sub) for sub in subs],
                return_exceptions=True,
            )
        posts: list[RedditPost] = []
        for sub, outcome in zip(subs, results):
            if isinstance(outcome, Exception):
                errors.append(f"r/{sub}: {outcome}")
                continue
            posts.extend(outcome)
        return posts, errors

    async def _fetch_subreddit(
        self, session: aiohttp.ClientSession, subreddit: str
    ) -> list[RedditPost]:
        try:
            data = await get_json(
                session, REDDIT_URL.format(subreddit=quote_plus(subreddit)), retries=1
            )
        except HTTPError as exc:
            if exc.status in (403, 429):
                raise RuntimeError("Reddit rate-limited this server (403/429)") from exc
            raise
        children = ((data or {}).get("data") or {}).get("children") or []
        posts: list[RedditPost] = []
        for child in children:
            payload = (child or {}).get("data") or {}
            title = str(payload.get("title") or "").strip()
            if not title:
                continue
            body = str(payload.get("selftext") or "")[:600]
            posts.append(
                RedditPost(
                    title=f"{title} {body}".strip(),
                    subreddit=str(payload.get("subreddit") or subreddit),
                    score=int(payload.get("score") or 0),
                    comments=int(payload.get("num_comments") or 0),
                    url=f"https://reddit.com{payload.get('permalink', '')}",
                    created_utc=float(payload.get("created_utc") or 0),
                )
            )
        return posts

    # -- X / Twitter -------------------------------------------------------
    async def x_mentions(self, symbols: list[str]) -> tuple[dict[str, int], list[str]]:
        counts: dict[str, int] = {}
        errors: list[str] = []
        if not self.settings.x_bearer_token:
            return counts, errors
        headers = {"Authorization": f"Bearer {self.settings.x_bearer_token}"}
        async with http_session(timeout=self.settings.request_timeout, headers=headers) as session:
            for symbol in symbols[:3]:
                query = f"(${symbol} OR #{symbol}) -is:retweet"
                try:
                    data = await request(
                        session,
                        "GET",
                        X_SEARCH,
                        expect="json",
                        params={"query": query, "max_results": 10, "tweet.fields": "public_metrics"},
                        retries=0,
                    )
                except HTTPError as exc:
                    errors.append(f"X API: {exc.status or exc}")
                    break
                meta = (data or {}).get("meta") or {}
                counts[symbol] = int(meta.get("result_count") or len((data or {}).get("data") or []))
                await asyncio.sleep(1.0)
        return counts, errors

    # -- news --------------------------------------------------------------
    async def news(self, query: str, limit: int = 5) -> list[NewsItem]:
        url = GOOGLE_NEWS.format(query=quote_plus(query))
        try:
            async with http_session(timeout=self.settings.request_timeout) as session:
                raw = await get_text(session, url, retries=1)
        except (HTTPError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.debug("News fetch failed: %s", exc)
            return []
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            return []
        items: list[NewsItem] = []
        for node in root.iter("item"):
            title = (node.findtext("title") or "").strip()
            link = (node.findtext("link") or "").strip()
            source_node = node.find("source")
            source = (source_node.text if source_node is not None else "") or "Google News"
            if title and link:
                items.append(NewsItem(title=title, source=source, url=link))
            if len(items) >= limit:
                break
        return items

    # -- hype --------------------------------------------------------------
    async def snapshot(
        self,
        universe: list[CoinQuote],
        trending: list[TrendingCoin] | None = None,
        *,
        subreddits: Iterable[str] | None = None,
    ) -> HypeSnapshot:
        posts, errors = await self.collect_posts(subreddits)
        trending = trending or []
        trending_symbols = {coin.symbol.upper() for coin in trending}
        items = rank_hype(universe, posts, trending_symbols)
        x_counts, x_errors = await self.x_mentions([item.symbol for item in items[:3]])
        errors.extend(x_errors)
        if x_counts:
            for item in items:
                if item.symbol in x_counts:
                    item.mentions += x_counts[item.symbol]
                    item.score += math.log10(1 + x_counts[item.symbol]) * 2
                    if "X" not in item.sources:
                        item.sources.append("X")
            items.sort(key=lambda item: item.score, reverse=True)

        news: list[NewsItem] = []
        if items:
            try:
                news = await self.news(f"{items[0].name} crypto", limit=4)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"news: {exc}")

        return HypeSnapshot(
            items=items,
            posts_scanned=len(posts),
            subreddits=list(subreddits or self.settings.subreddits),
            news=news,
            x_mentions=x_counts,
            errors=errors,
        )


def _post_index(text: str) -> tuple[set[str], str]:
    cashtags = {match.group(1).upper() for match in _CASHTAG_RE.finditer(text)}
    upper_tokens = {match.group(1) for match in _UPPER_TOKEN_RE.finditer(text)}
    return cashtags | upper_tokens, text.lower()


def rank_hype(
    universe: list[CoinQuote],
    posts: list[RedditPost],
    trending_symbols: set[str] | None = None,
    *,
    pool: int = 250,
) -> list[HypeItem]:
    """Score coins by how much Reddit is talking about them."""
    trending_symbols = trending_symbols or set()
    indexed = [_post_index(post.title) for post in posts]
    items: list[HypeItem] = []
    for coin in universe[:pool]:
        symbol = coin.symbol.upper()
        if not symbol:
            continue
        name_lower = coin.name.lower()
        mentions = 0
        upvotes = 0
        samples: list[str] = []
        for post, (symbols, lowered) in zip(posts, indexed):
            hit = symbol in symbols
            if not hit and len(name_lower) >= 4 and symbol not in AMBIGUOUS_SYMBOLS:
                hit = name_lower in lowered
            if not hit:
                continue
            mentions += 1
            upvotes += max(0, post.score)
            if len(samples) < 2:
                samples.append(post.title[:140])
        if mentions == 0 and symbol not in trending_symbols:
            continue
        score = mentions * 2.0 + math.log10(1 + upvotes) * 1.5
        if symbol in trending_symbols:
            score += 4.0
        if coin.change_24h:
            score += max(-2.0, min(2.0, coin.change_24h / 10))
        sources = ["Reddit"] if mentions else []
        if symbol in trending_symbols:
            sources.append("CoinGecko trending")
        items.append(
            HypeItem(
                symbol=symbol,
                name=coin.name,
                mentions=mentions,
                upvotes=upvotes,
                score=round(score, 2),
                trending=symbol in trending_symbols,
                sources=sources,
                sample_titles=samples,
            )
        )
    items.sort(key=lambda item: item.score, reverse=True)
    return items

