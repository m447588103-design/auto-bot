"""Gemini market analysis with a transparent rule-based fallback."""

from __future__ import annotations

import json
import logging

from core.gemini import GeminiClient, GeminiError, get_gemini
from core.utils import clamp, truncate

from .models import CoinQuote, HypeItem, NewsItem, Verdict

log = logging.getLogger(__name__)

SYSTEM = (
    "You are a cautious crypto market analyst for a Discord community. "
    "You explain data, you do not give financial advice, and you never promise returns. "
    "You flag manipulation, thin liquidity and hype-driven risk. Answer with pure JSON only."
)

PROMPT = """Analyse this market snapshot. Be balanced: mention both opportunity and risk.

DATA:
{data}

Return JSON:
{{
  "verdict": "bullish" | "neutral" | "bearish",
  "confidence": 0-100,
  "horizon": "e.g. 1-3 days",
  "key_signals": ["3-5 concrete data points from the snapshot"],
  "risks": ["2-4 concrete risks"],
  "summary_en": "2-3 sentence English summary with numbers",
  "summary_bn": "2-3 sentence informal Bangla summary (Banglish readers welcome)"
}}
"""

VERDICT_WORDS = {"bullish", "neutral", "bearish"}


class MarketAnalyst:
    def __init__(self, gemini: GeminiClient | None = None):
        self.gemini = gemini or get_gemini()

    async def analyze(
        self,
        quote: CoinQuote,
        *,
        hype: HypeItem | None = None,
        news: list[NewsItem] | None = None,
        trending: bool = False,
        global_stats: dict | None = None,
    ) -> Verdict:
        payload = {
            "coin": {
                "name": quote.name,
                "symbol": quote.ticker,
                "price_usd": quote.price,
                "change_1h_pct": quote.change_1h,
                "change_24h_pct": quote.change_24h,
                "change_7d_pct": quote.change_7d,
                "market_cap_usd": quote.market_cap,
                "volume_24h_usd": quote.volume_24h,
                "rank": quote.rank,
                "high_24h": quote.high_24h,
                "low_24h": quote.low_24h,
            },
            "social": {
                "reddit_mentions": hype.mentions if hype else 0,
                "reddit_upvotes": hype.upvotes if hype else 0,
                "hype_score": hype.score if hype else 0,
                "coingecko_trending": trending,
                "sample_posts": hype.sample_titles if hype else [],
            },
            "news": [{"title": item.title, "source": item.source} for item in (news or [])[:4]],
            "market": global_stats or {},
        }

        if self.gemini.available:
            try:
                data = await self.gemini.generate_json(
                    PROMPT.format(data=json.dumps(payload, ensure_ascii=False, default=str)),
                    system_instruction=SYSTEM,
                    temperature=0.55,
                    max_output_tokens=1200,
                )
                return validate_verdict(data, quote)
            except GeminiError as exc:
                log.warning("Gemini analysis failed, using heuristic: %s", exc)
        return heuristic_verdict(quote, hype, trending)


def validate_verdict(data: dict, quote: CoinQuote) -> Verdict:
    if not isinstance(data, dict):
        raise GeminiError("Analysis payload was not an object")
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in VERDICT_WORDS:
        raise GeminiError("Invalid verdict value")
    try:
        confidence = int(float(data.get("confidence", 50)))
    except (TypeError, ValueError):
        confidence = 50
    signals = [truncate(str(item), 160) for item in data.get("key_signals") or [] if str(item).strip()]
    risks = [truncate(str(item), 160) for item in data.get("risks") or [] if str(item).strip()]
    if not signals:
        signals = ["Insufficient structured signals returned by the model."]
    return Verdict(
        symbol=quote.ticker,
        verdict=verdict,
        confidence=int(clamp(confidence, 0, 100)),
        horizon=str(data.get("horizon") or "short term")[:40],
        key_signals=signals[:5],
        risks=risks[:4] or ["Markets can move against any thesis; manage risk."],
        summary_en=truncate(str(data.get("summary_en") or ""), 900)
        or "No summary provided.",
        summary_bn=truncate(str(data.get("summary_bn") or ""), 900)
        or "সংক্ষিপ্ত বিশ্লেষণ পাওয়া যায়নি।",
        source="gemini",
    )


def heuristic_verdict(
    quote: CoinQuote, hype: HypeItem | None = None, trending: bool = False
) -> Verdict:
    """Deterministic fallback used when Gemini is unavailable."""
    momentum = quote.momentum
    hype_score = (hype.score if hype else 0.0) + (4.0 if trending else 0.0)
    combined = momentum + min(8.0, hype_score) * 0.5

    if combined >= 6:
        verdict = "bullish"
    elif combined <= -6:
        verdict = "bearish"
    else:
        verdict = "neutral"

    confidence = int(clamp(48 + abs(combined) * 1.8 + min(10.0, hype_score), 35, 88))

    signals = [
        f"24h change: {quote.change_24h:+.2f}%" if quote.change_24h is not None else "24h change: n/a",
        f"1h change: {quote.change_1h:+.2f}%" if quote.change_1h is not None else "1h change: n/a",
        f"7d change: {quote.change_7d:+.2f}%" if quote.change_7d is not None else "7d change: n/a",
        f"Reddit mentions: {hype.mentions}" if hype else "No social data collected",
    ]
    if quote.rank:
        signals.append(f"Market cap rank: #{quote.rank}")
    risks = [
        "Short-term momentum can reverse quickly; this is not financial advice.",
        "Hype-driven moves often retrace once social attention fades.",
    ]
    if quote.volume_24h and quote.market_cap and quote.volume_24h < quote.market_cap * 0.01:
        risks.append("Low volume relative to market cap — thin liquidity risk.")

    direction = {"bullish": "upward", "bearish": "downward", "neutral": "sideways"}[verdict]
    summary_en = (
        f"{quote.name} ({quote.ticker}) trades at ${quote.price:,.6f} "
        if quote.price is not None
        else f"{quote.name} ({quote.ticker}) "
    ) + (
        f"with {quote.change_24h:+.2f}% over 24h. Momentum and social activity point "
        f"{direction}, but this rule-based view cannot see fundamentals."
        if quote.change_24h is not None
        else f"Momentum points {direction}, but data is incomplete."
    )
    summary_bn = (
        f"{quote.name} ({quote.ticker}) এখন "
        + (f"${quote.price:,.6f} " if quote.price is not None else "")
        + (
            f"এবং গত ২৪ ঘণ্টায় {quote.change_24h:+.2f}% পরিবর্তন হয়েছে। "
            if quote.change_24h is not None
            else "এর দামের পূর্ণ তথ্য পাওয়া যায়নি। "
        )
        + f"সোশ্যাল সিগন্যাল ও মোমেন্টাম মিলে দিকটা **{verdict}** মনে হচ্ছে, "
        "তবে এটা কোনো বিনিয়োগ পরামর্শ নয়।"
    )
    return Verdict(
        symbol=quote.ticker,
        verdict=verdict,
        confidence=confidence,
        horizon="1-3 days",
        key_signals=signals,
        risks=risks,
        summary_en=summary_en,
        summary_bn=summary_bn,
        source="rules",
    )
