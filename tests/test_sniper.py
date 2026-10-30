from __future__ import annotations

import pytest

from core.config import settings
from core.db import Database
from core.gemini import GeminiError
from features.sniper.alerts import PriceAlertService
from features.sniper.analyst import heuristic_verdict, validate_verdict
from features.sniper.market import MarketData
from features.sniper.models import CoinQuote, HypeItem, PriceRule, RedditPost
from features.sniper.sentiment import HypeEngine, rank_hype


def coin(symbol: str, name: str, change_24h: float = 0.0, **kwargs) -> CoinQuote:
    return CoinQuote(
        id=symbol.lower(),
        symbol=symbol,
        name=name,
        price=kwargs.get("price", 1.0),
        change_24h=change_24h,
        change_1h=kwargs.get("change_1h", 0.0),
        change_7d=kwargs.get("change_7d", 0.0),
        market_cap=kwargs.get("market_cap", 1_000_000.0),
        volume_24h=kwargs.get("volume_24h", 100_000.0),
    )


def test_rank_hype_counts_symbols_cashtags_and_names():
    universe = [
        coin("BTC", "Bitcoin", 2.0),
        coin("SOL", "Solana", 5.0),
        coin("DOGE", "Dogecoin", -1.0),
    ]
    posts = [
        RedditPost("Bitcoin to the moon $BTC", "CryptoCurrency", 500, 20, "u1", 0),
        RedditPost("SOL looking strong", "SatoshiStreetBets", 50, 3, "u2", 0),
        RedditPost("solana ecosystem growing", "CryptoCurrency", 100, 5, "u3", 0),
        RedditPost("dog pictures", "memecoins", 10, 0, "u4", 0),
    ]
    items = rank_hype(universe, posts, {"SOL"})
    assert items
    top = items[0]
    assert top.symbol == "SOL"
    assert top.mentions == 2
    assert top.trending is True
    symbols = {item.symbol for item in items}
    assert "BTC" in symbols
    assert "DOGE" not in symbols  # no real mention


def test_rank_hype_ignores_ambiguous_word_matches():
    universe = [coin("NEAR", "NEAR Protocol"), coin("LINK", "Chainlink")]
    posts = [RedditPost("the exit is near the door", "CryptoCurrency", 10, 0, "u", 0)]
    assert rank_hype(universe, posts, set()) == []


def test_rank_hype_includes_trending_without_posts():
    universe = [coin("PEPE", "Pepe")]
    items = rank_hype(universe, [], {"PEPE"})
    assert len(items) == 1
    assert items[0].trending is True
    assert items[0].score >= 4


def test_price_rule_trigger_directions():
    above = PriceRule(1, 1, 2, "BTC", "above", 100.0)
    below = PriceRule(2, 1, 2, "BTC", "below", 50.0)
    assert PriceAlertService.is_triggered(above, 100.0) is True
    assert PriceAlertService.is_triggered(above, 99.99) is False
    assert PriceAlertService.is_triggered(below, 49.0) is True
    assert PriceAlertService.is_triggered(below, 50.01) is False


def test_heuristic_verdict_bullish_and_bearish():
    bullish = heuristic_verdict(coin("X", "X", change_24h=25, change_1h=4, change_7d=40))
    assert bullish.verdict == "bullish"
    assert 0 <= bullish.confidence <= 100
    assert "বাংলা" not in bullish.summary_bn
    assert bullish.summary_bn

    bearish = heuristic_verdict(coin("Y", "Y", change_24h=-30, change_1h=-8, change_7d=-45))
    assert bearish.verdict == "bearish"

    flat = heuristic_verdict(coin("Z", "Z", change_24h=0.1))
    assert flat.verdict == "neutral"


def test_heuristic_includes_hype():
    quote = coin("WIF", "dogwifhat", change_24h=5)
    hype = HypeItem(symbol="WIF", name="dogwifhat", mentions=40, score=20, upvotes=900)
    verdict = heuristic_verdict(quote, hype, trending=True)
    assert verdict.verdict == "bullish"
    assert any("mentions" in signal for signal in verdict.key_signals)


def test_validate_verdict_clamps_and_rejects():
    quote = coin("BTC", "Bitcoin")
    verdict = validate_verdict(
        {
            "verdict": "BULLISH",
            "confidence": 150,
            "horizon": "1 week",
            "key_signals": ["a", "b"],
            "risks": ["r"],
            "summary_en": "summary",
            "summary_bn": "সারসংক্ষেপ",
        },
        quote,
    )
    assert verdict.verdict == "bullish"
    assert verdict.confidence == 100
    with pytest.raises(GeminiError):
        validate_verdict({"verdict": "moon"}, quote)
    with pytest.raises(GeminiError):
        validate_verdict("nope", quote)


def test_market_quote_parsing():
    market = MarketData(settings.sniper)
    quote = market._quote_from_market(
        {
            "id": "bitcoin",
            "symbol": "btc",
            "name": "Bitcoin",
            "current_price": 67000.5,
            "price_change_percentage_1h_in_currency": 0.5,
            "price_change_percentage_24h_in_currency": -2.25,
            "price_change_percentage_7d_in_currency": 8.0,
            "market_cap": 1_300_000_000_000,
            "total_volume": 30_000_000_000,
            "market_cap_rank": 1,
        }
    )
    assert quote.symbol == "BTC"
    assert quote.price == 67000.5
    assert quote.change_24h == -2.25
    assert quote.momentum == pytest.approx(0.5 * 0.35 - 2.25 * 0.45 + 8.0 * 0.2)


async def test_alert_service_crud_and_check():
    db = Database(":memory:")
    await db.connect()
    market = MarketData(settings.sniper)
    service = PriceAlertService(bot=None, db=db, settings=settings.sniper, market=market)
    try:
        await service.setup()
        rule_id = await service.add_rule(1, 2, "btc", "above", 100.0, created_by=5)
        rules = await service.list_rules(1)
        assert len(rules) == 1
        assert rules[0].symbol == "BTC"
        with pytest.raises(ValueError):
            await service.add_rule(1, 2, "BTC", "sideways", 1.0)
        with pytest.raises(ValueError):
            await service.add_rule(1, 2, "BTC", "above", 0)
        assert await service.set_channel(1, 99) is None
        assert await service.get_channel(1) == 99
        assert await service.remove_rule(2, rule_id) is False
        assert await service.remove_rule(1, rule_id) is True
    finally:
        await db.close()


async def test_alert_check_triggers_without_network(monkeypatch):
    db = Database(":memory:")
    await db.connect()
    market = MarketData(settings.sniper)
    service = PriceAlertService(bot=None, db=db, settings=settings.sniper, market=market)
    sent: list[tuple] = []

    async def fake_quotes(symbols):
        return {"BTC": coin("BTC", "Bitcoin", 3.0, price=101.0)}

    async def fake_notify(rule, quote):
        sent.append((rule.symbol, quote.price))

    monkeypatch.setattr(service.market, "quotes", fake_quotes)
    monkeypatch.setattr(service, "_notify", fake_notify)
    try:
        await service.setup()
        await service.add_rule(1, 2, "BTC", "above", 100.0)
        await service.add_rule(1, 2, "BTC", "below", 50.0)
        triggered = await service.check_once()
        assert triggered == 1
        assert sent == [("BTC", 101.0)]
        # already-triggered rules are not re-fired
        assert await service.check_once() == 0
        remaining = await service.list_rules(1)
        assert len(remaining) == 1
        assert remaining[0].direction == "below"
    finally:
        await db.close()


async def test_hype_engine_snapshot_handles_no_posts(monkeypatch):
    engine = HypeEngine(settings.sniper)

    async def fake_posts(subreddits=None):
        return [], ["Reddit blocked"]

    async def fake_x(symbols):
        return {}, []

    async def fake_news(query, limit=5):
        return []

    monkeypatch.setattr(engine, "collect_posts", fake_posts)
    monkeypatch.setattr(engine, "x_mentions", fake_x)
    monkeypatch.setattr(engine, "news", fake_news)
    snapshot = await engine.snapshot([coin("BTC", "Bitcoin")], [])
    assert snapshot.posts_scanned == 0
    assert snapshot.errors == ["Reddit blocked"]
