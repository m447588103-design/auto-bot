"""Live market data: CoinGecko (crypto), Dexscreener (DEX) and Finnhub (stocks)."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

import aiohttp

from core.config import SniperSettings
from core.http import HTTPError, get_json, http_session, request

from .models import CoinQuote, NewPair, StockQuote, TrendingCoin

log = logging.getLogger(__name__)

COINGECKO = "https://api.coingecko.com/api/v3"
DEXSCREENER = "https://api.dexscreener.com"
FINNHUB = "https://finnhub.io/api/v1"


class MarketError(RuntimeError):
    pass


def _float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


class MarketData:
    def __init__(self, settings: SniperSettings):
        self.settings = settings
        self._cache: dict[str, tuple[float, Any]] = {}
        self._last_call = 0.0
        self._lock = asyncio.Lock()
        self._coin_ids: dict[str, list[str]] | None = None

    # -- plumbing ----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        if self.settings.coingecko_api_key:
            return {"x-cg-demo-api-key": self.settings.coingecko_api_key}
        return {}

    async def _throttle(self) -> None:
        async with self._lock:
            delta = time.monotonic() - self._last_call
            if delta < 1.1:
                await asyncio.sleep(1.1 - delta)
            self._last_call = time.monotonic()

    def _cached(self, key: str, ttl: float) -> Any | None:
        entry = self._cache.get(key)
        if not entry:
            return None
        expires, value = entry
        if time.monotonic() > expires:
            self._cache.pop(key, None)
            return None
        return value

    def _store(self, key: str, value: Any, ttl: float) -> Any:
        self._cache[key] = (time.monotonic() + ttl, value)
        return value

    async def _cg(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await self._throttle()
        async with http_session(timeout=self.settings.request_timeout, headers=self._headers()) as session:
            try:
                return await get_json(session, f"{COINGECKO}{path}", params=params, retries=1)
            except HTTPError as exc:
                if exc.status == 429:
                    raise MarketError(
                        "CoinGecko rate limit reached. Add a free `COINGECKO_API_KEY` or retry shortly."
                    ) from exc
                raise MarketError(f"CoinGecko request failed: {exc}") from exc

    # -- crypto ------------------------------------------------------------
    async def top_coins(self, limit: int = 100) -> list[CoinQuote]:
        cache_key = f"top:{limit}"
        cached = self._cached(cache_key, self.settings.cache_ttl_seconds)
        if cached is not None:
            return cached
        data = await self._cg(
            "/coins/markets",
            {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": max(1, min(limit, 250)),
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "1h,24h,7d",
            },
        )
        quotes = [self._quote_from_market(row) for row in data or [] if isinstance(row, dict)]
        return self._store(cache_key, quotes, self.settings.cache_ttl_seconds)

    @staticmethod
    def _quote_from_market(row: dict[str, Any]) -> CoinQuote:
        return CoinQuote(
            id=str(row.get("id") or ""),
            symbol=str(row.get("symbol") or "").upper(),
            name=str(row.get("name") or ""),
            price=_float(row.get("current_price")),
            change_1h=_float(row.get("price_change_percentage_1h_in_currency")),
            change_24h=_float(row.get("price_change_percentage_24h_in_currency"))
            or _float(row.get("price_change_percentage_24h")),
            change_7d=_float(row.get("price_change_percentage_7d_in_currency")),
            market_cap=_float(row.get("market_cap")),
            volume_24h=_float(row.get("total_volume")),
            rank=_int(row.get("market_cap_rank")),
            image=row.get("image"),
            high_24h=_float(row.get("high_24h")),
            low_24h=_float(row.get("low_24h")),
        )

    async def coin_ids(self) -> dict[str, list[str]]:
        if self._coin_ids is not None:
            return self._coin_ids
        cached = self._cached("coin_ids", 86400)
        if cached is not None:
            self._coin_ids = cached
            return cached
        data = await self._cg("/coins/list", {"include_platform": "false"})
        mapping: dict[str, list[str]] = {}
        for row in data or []:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            coin_id = row.get("id")
            if symbol and coin_id:
                mapping.setdefault(symbol, []).append(str(coin_id))
        self._coin_ids = mapping
        return self._store("coin_ids", mapping, 86400)

    async def resolve_id(self, symbol: str) -> str | None:
        symbol = symbol.strip().upper().lstrip("$")
        top = await self.top_coins(250)
        for coin in top:
            if coin.symbol.upper() == symbol:
                return coin.id
        mapping = await self.coin_ids()
        candidates = mapping.get(symbol) or []
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        # Prefer the best ranked candidate among the top 250 by market cap
        ranked = {coin.id for coin in top}
        for candidate in candidates:
            if candidate in ranked:
                return candidate
        return candidates[0]

    async def quote(self, symbol: str) -> CoinQuote:
        symbol = symbol.strip().upper().lstrip("$")
        cache_key = f"quote:{symbol}"
        cached = self._cached(cache_key, self.settings.cache_ttl_seconds)
        if cached is not None:
            return cached
        top = await self.top_coins(250)
        for coin in top:
            if coin.symbol.upper() == symbol:
                return self._store(cache_key, coin, self.settings.cache_ttl_seconds)
        coin_id = await self.resolve_id(symbol)
        if not coin_id:
            # allow full CoinGecko ids such as "bitcoin" or "solana"
            coin_id = symbol.lower()
        if not coin_id:
            raise MarketError(f"Unknown symbol `{symbol}`.")
        data = await self._cg(
            "/coins/markets",
            {
                "vs_currency": "usd",
                "ids": coin_id,
                "sparkline": "false",
                "price_change_percentage": "1h,24h,7d",
            },
        )
        rows = [row for row in data or [] if isinstance(row, dict)]
        if not rows:
            raise MarketError(f"No market data for `{symbol}`")
        return self._store(cache_key, self._quote_from_market(rows[0]), self.settings.cache_ttl_seconds)

    async def quotes(self, symbols: list[str]) -> dict[str, CoinQuote]:
        results: dict[str, CoinQuote] = {}
        unique = list(dict.fromkeys(s.strip().upper().lstrip("$") for s in symbols if s.strip()))
        if not unique:
            return results
        try:
            top = await self.top_coins(250)
        except MarketError:
            top = []
        top_by_symbol = {coin.symbol.upper(): coin for coin in top}
        missing: list[str] = []
        for symbol in unique:
            if symbol in top_by_symbol:
                results[symbol] = top_by_symbol[symbol]
            else:
                missing.append(symbol)
        if missing:
            try:
                mapping = await self.coin_ids()
            except MarketError:
                mapping = {}
            ids = []
            for symbol in missing:
                try:
                    candidate = (await self.resolve_id(symbol)) if mapping.get(symbol) else None
                except MarketError:
                    candidate = None
                if candidate:
                    ids.append(candidate)
            if ids:
                data = await self._cg(
                    "/coins/markets",
                    {
                        "vs_currency": "usd",
                        "ids": ",".join(ids),
                        "sparkline": "false",
                        "price_change_percentage": "1h,24h,7d",
                    },
                )
                for row in data or []:
                    quote = self._quote_from_market(row)
                    results[quote.symbol] = quote
        return results

    async def trending(self) -> list[TrendingCoin]:
        cached = self._cached("trending", 300)
        if cached is not None:
            return cached
        data = await self._cg("/search/trending")
        coins: list[TrendingCoin] = []
        for entry in (data or {}).get("coins", []):
            item = entry.get("item") or {}
            if not item.get("symbol"):
                continue
            price_change = None
            price_data = item.get("data") or {}
            change = price_data.get("price_change_percentage_24h") or {}
            if isinstance(change, dict):
                price_change = _float(change.get("usd"))
            coins.append(
                TrendingCoin(
                    id=str(item.get("id") or ""),
                    name=str(item.get("name") or ""),
                    symbol=str(item.get("symbol") or "").upper(),
                    rank=_int(item.get("market_cap_rank")),
                    thumb=item.get("thumb") or item.get("large"),
                    price_btc=_float(item.get("price_btc")),
                    change_24h=price_change,
                )
            )
        return self._store("trending", coins, 300)

    async def global_stats(self) -> dict[str, Any]:
        cached = self._cached("global", 300)
        if cached is not None:
            return cached
        data = await self._cg("/global")
        payload = (data or {}).get("data") or {}
        stats = {
            "market_cap_usd": _float((payload.get("total_market_cap") or {}).get("usd")),
            "volume_usd": _float((payload.get("total_volume") or {}).get("usd")),
            "btc_dominance": _float((payload.get("market_cap_percentage") or {}).get("btc")),
            "eth_dominance": _float((payload.get("market_cap_percentage") or {}).get("eth")),
            "market_cap_change_24h": _float(payload.get("market_cap_change_percentage_24h_usd")),
        }
        return self._store("global", stats, 300)

    async def new_pairs(self, limit: int = 5) -> list[NewPair]:
        cached = self._cached(f"newpairs:{limit}", 120)
        if cached is not None:
            return cached
        async with http_session(timeout=self.settings.request_timeout) as session:
            try:
                profiles = await get_json(
                    session, f"{DEXSCREENER}/token-profiles/latest/v1", retries=1
                )
            except HTTPError as exc:
                raise MarketError(f"Dexscreener unavailable: {exc}") from exc
            if not isinstance(profiles, list):
                return []
            selected = [p for p in profiles if isinstance(p, dict) and p.get("tokenAddress")][:limit]
            results = await asyncio.gather(
                *[self._pair_details(session, profile) for profile in selected],
                return_exceptions=True,
            )
        pairs: list[NewPair] = []
        for result in results:
            if isinstance(result, NewPair):
                pairs.append(result)
        return self._store(f"newpairs:{limit}", pairs, 120)

    async def _pair_details(self, session: aiohttp.ClientSession, profile: dict[str, Any]) -> NewPair | None:
        address = str(profile.get("tokenAddress") or "")
        chain = str(profile.get("chainId") or "unknown")
        try:
            data = await get_json(
                session, f"{DEXSCREENER}/latest/dex/tokens/{address}", retries=1, timeout=20
            )
        except HTTPError:
            return None
        pairs = [pair for pair in (data or {}).get("pairs") or [] if isinstance(pair, dict)]
        if not pairs:
            return None
        pairs.sort(key=lambda pair: _float((pair.get("liquidity") or {}).get("usd")) or 0, reverse=True)
        pair = pairs[0]
        base = pair.get("baseToken") or {}
        created = pair.get("pairCreatedAt")
        created_at = (
            datetime.fromtimestamp(created / 1000, tz=timezone.utc) if isinstance(created, (int, float)) else None
        )
        return NewPair(
            name=str(base.get("name") or "Unknown"),
            symbol=str(base.get("symbol") or "?").upper(),
            chain=chain,
            address=address,
            url=pair.get("url") or profile.get("url"),
            price_usd=_float(pair.get("priceUsd")),
            liquidity_usd=_float((pair.get("liquidity") or {}).get("usd")),
            fdv=_float(pair.get("fdv")),
            created_at=created_at,
            description=(profile.get("description") or "")[:300] or None,
        )

    # -- stocks ------------------------------------------------------------
    @property
    def stocks_configured(self) -> bool:
        return bool(self.settings.finnhub_api_key)

    async def stock_quote(self, symbol: str) -> StockQuote:
        if not self.settings.finnhub_api_key:
            raise MarketError(
                "Stock quotes need a free Finnhub key: set `FINNHUB_API_KEY` in .env "
                "(get one at finnhub.io)."
            )
        symbol = symbol.strip().upper()
        if not symbol.isalpha() or len(symbol) > 6:
            raise MarketError("Use a valid US ticker, e.g. `AAPL` or `TSLA`.")
        cache_key = f"stock:{symbol}"
        cached = self._cached(cache_key, 60)
        if cached is not None:
            return cached
        async with http_session(timeout=self.settings.request_timeout) as session:
            try:
                data = await request(
                    session,
                    "GET",
                    f"{FINNHUB}/quote",
                    expect="json",
                    params={"symbol": symbol, "token": self.settings.finnhub_api_key},
                    retries=1,
                )
            except HTTPError as exc:
                raise MarketError(f"Finnhub request failed: {exc}") from exc
        if not data or data.get("c") in (None, 0):
            raise MarketError(f"No quote for `{symbol}` (invalid ticker or market closed).")
        quote = StockQuote(
            symbol=symbol,
            price=_float(data.get("c")),
            change=_float(data.get("d")),
            change_pct=_float(data.get("dp")),
            high=_float(data.get("h")),
            low=_float(data.get("l")),
            open=_float(data.get("o")),
            previous_close=_float(data.get("pc")),
            timestamp=datetime.fromtimestamp(data["t"], tz=timezone.utc) if data.get("t") else None,
        )
        return self._store(cache_key, quote, 60)

    # -- misc --------------------------------------------------------------
    async def top_movers(self, limit: int = 5) -> tuple[list[CoinQuote], list[CoinQuote]]:
        coins = await self.top_coins(100)
        ranked = [coin for coin in coins if coin.change_24h is not None and coin.market_cap]
        gainers = sorted(ranked, key=lambda coin: coin.change_24h or 0, reverse=True)[:limit]
        losers = sorted(ranked, key=lambda coin: coin.change_24h or 0)[:limit]
        return gainers, losers
