"""Data structures for the market sniper."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class CoinQuote:
    id: str
    symbol: str
    name: str
    price: float | None = None
    change_1h: float | None = None
    change_24h: float | None = None
    change_7d: float | None = None
    market_cap: float | None = None
    volume_24h: float | None = None
    rank: int | None = None
    image: str | None = None
    high_24h: float | None = None
    low_24h: float | None = None

    @property
    def ticker(self) -> str:
        return self.symbol.upper()

    @property
    def momentum(self) -> float:
        """Weighted momentum score used by the heuristic fallback."""
        return (
            (self.change_1h or 0.0) * 0.35
            + (self.change_24h or 0.0) * 0.45
            + (self.change_7d or 0.0) * 0.20
        )


@dataclass
class TrendingCoin:
    id: str
    name: str
    symbol: str
    rank: int | None = None
    thumb: str | None = None
    price_btc: float | None = None
    change_24h: float | None = None


@dataclass
class HypeItem:
    symbol: str
    name: str
    mentions: int = 0
    score: float = 0.0
    upvotes: int = 0
    trending: bool = False
    sources: list[str] = field(default_factory=list)
    sample_titles: list[str] = field(default_factory=list)

    @property
    def ticker(self) -> str:
        return self.symbol.upper()


@dataclass
class RedditPost:
    title: str
    subreddit: str
    score: int
    comments: int
    url: str
    created_utc: float


@dataclass
class NewsItem:
    title: str
    source: str
    url: str
    published: datetime | None = None


@dataclass
class HypeSnapshot:
    items: list[HypeItem]
    posts_scanned: int
    subreddits: list[str]
    news: list[NewsItem] = field(default_factory=list)
    x_mentions: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Verdict:
    symbol: str
    verdict: str
    confidence: int
    horizon: str
    key_signals: list[str]
    risks: list[str]
    summary_en: str
    summary_bn: str
    source: str = "gemini"

    @property
    def emoji(self) -> str:
        return {"bullish": "🟢", "bearish": "🔴", "neutral": "🟡"}.get(self.verdict, "⚪")


@dataclass
class NewPair:
    name: str
    symbol: str
    chain: str
    address: str
    url: str | None = None
    price_usd: float | None = None
    liquidity_usd: float | None = None
    fdv: float | None = None
    created_at: datetime | None = None
    description: str | None = None


@dataclass
class PriceRule:
    id: int
    guild_id: int
    channel_id: int
    symbol: str
    direction: str  # above | below
    target_price: float
    created_by: int | None = None
    created_at: str | None = None
    triggered_at: str | None = None
    last_price: float | None = None

    @property
    def is_active(self) -> bool:
        return self.triggered_at is None


@dataclass
class StockQuote:
    symbol: str
    price: float | None
    change: float | None
    change_pct: float | None
    high: float | None
    low: float | None
    open: float | None
    previous_close: float | None
    timestamp: datetime | None = None
    currency: str = "USD"
