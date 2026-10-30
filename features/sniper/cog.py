"""Discord commands for the market sniper."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from core.discord_ui import (
    CRYPTO,
    DISCLAIMER,
    LEAK,
    MUSIC,
    SUCCESS,
    WARNING,
    embed,
    error_embed,
    list_embed,
    success_embed,
)
from core.utils import (
    format_ts,
    human_duration,
    human_number,
    pct,
    progress_bar,
    truncate,
    utcnow,
)

from .alerts import PriceAlertService
from .analyst import MarketAnalyst
from .market import MarketData, MarketError
from .models import CoinQuote, NewPair, PriceRule, StockQuote
from .sentiment import HypeEngine

log = logging.getLogger(__name__)

DIRECTIONS = [
    app_commands.Choice(name="above", value="above"),
    app_commands.Choice(name="below", value="below"),
]


class SniperCog(commands.Cog, name="Market Sniper"):
    """Real-time crypto data, social hype and AI market reads."""

    sniper = app_commands.Group(name="sniper", description="Crypto signals, hype radar and price alerts")

    def __init__(
        self,
        bot: commands.Bot,
        market: MarketData,
        hype: HypeEngine,
        analyst: MarketAnalyst,
        alerts: PriceAlertService,
    ):
        self.bot = bot
        self.market = market
        self.hype = hype
        self.analyst = analyst
        self.alerts = alerts

    # -- formatting helpers ------------------------------------------------
    @staticmethod
    def _coin_embed(quote: CoinQuote, *, trending: bool = False) -> discord.Embed:
        fields = [
            ("Price", human_number(quote.price), True),
            ("1h", pct(quote.change_1h), True),
            ("24h", pct(quote.change_24h), True),
            ("7d", pct(quote.change_7d), True),
            ("Market cap", human_number(quote.market_cap), True),
            ("24h volume", human_number(quote.volume_24h), True),
            ("Rank", f"#{quote.rank}" if quote.rank else "—", True),
            ("24h high", human_number(quote.high_24h), True),
            ("24h low", human_number(quote.low_24h), True),
        ]
        title = f"{quote.name} · ${quote.ticker}"
        if trending:
            title += " 🔥"
        return embed(
            title,
            f"[View on CoinGecko](https://www.coingecko.com/en/coins/{quote.id})"
            + ("\n🔥 Currently trending on CoinGecko" if trending else ""),
            color=CRYPTO,
            thumbnail=quote.image,
            fields=fields,
            footer=DISCLAIMER,
        )

    @staticmethod
    def _new_pair_embed(pair: NewPair) -> discord.Embed:
        age = ""
        if pair.created_at:
            age = human_duration((utcnow() - pair.created_at).total_seconds())
        return embed(
            f"🆕 {pair.name} · ${pair.symbol}",
            truncate(pair.description or "Freshly listed token — extreme risk.", 300),
            color=WARNING,
            url=pair.url,
            fields=[
                ("Chain", pair.chain, True),
                ("Price", human_number(pair.price_usd), True),
                ("Liquidity", human_number(pair.liquidity_usd), True),
                ("FDV", human_number(pair.fdv), True),
                ("Age", age or "unknown", True),
                ("Contract", f"`{truncate(pair.address, 60)}`", False),
            ],
            footer="⚠️ New pairs are extremely risky: check contract, liquidity locks and holders.",
        )

    # -- commands ----------------------------------------------------------
    @sniper.command(name="hype", description="What crypto is Reddit + trending radar talking about?")
    async def hype(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        try:
            universe = await self.market.top_coins(100)
            trending = await self.market.trending()
        except MarketError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return

        snapshot = await self.hype.snapshot(universe, trending)
        top = snapshot.items[:10]
        if not top:
            await interaction.followup.send(
                embed=embed(
                    "😴 No clear hype detected",
                    f"Scanned {snapshot.posts_scanned} posts across "
                    f"{', '.join('r/' + sub for sub in snapshot.subreddits)} and found no standout coin.",
                    color=MUSIC,
                )
            )
            return

        prices = {coin.symbol.upper(): coin for coin in universe}
        lines = []
        for index, item in enumerate(top, start=1):
            coin = prices.get(item.symbol)
            change = pct(coin.change_24h) if coin and coin.change_24h is not None else "—"
            badges = "🔥" if item.trending else ""
            lines.append(
                f"`{index:>2}.` **${item.symbol}** {badges} · {item.mentions} mentions · "
                f"score {item.score:.1f} · {change}"
            )

        emb = embed(
            "🔥 Hype radar",
            f"Scanned **{snapshot.posts_scanned}** hot posts across "
            + ", ".join(f"r/{sub}" for sub in snapshot.subreddits[:5]),
            color=CRYPTO,
            fields=[("Top hype", "\n".join(lines), False)],
            footer=f"{DISCLAIMER} • Sources: {', '.join(sorted({s for item in top for s in item.sources})) or 'Reddit'}",
        )
        trending_names = ", ".join(f"${coin.symbol}" for coin in trending[:8]) or "none"
        emb.add_field(name="CoinGecko trending", value=trending_names, inline=False)
        if snapshot.news:
            emb.add_field(
                name="📰 Headlines",
                value="\n".join(
                    f"• [{truncate(item.title, 90)}]({item.url})" for item in snapshot.news[:3]
                ),
                inline=False,
            )
        if snapshot.errors:
            emb.add_field(
                name="Data notes",
                value=truncate("; ".join(snapshot.errors), 500),
                inline=False,
            )
        await interaction.followup.send(embed=emb)

    @sniper.command(name="coin", description="Live price and stats for a coin")
    @app_commands.describe(symbol="Ticker or CoinGecko id, e.g. BTC, SOL, bitcoin")
    async def coin(self, interaction: discord.Interaction, symbol: str) -> None:
        await interaction.response.defer(thinking=True)
        try:
            quote = await self.market.quote(symbol)
            trending = await self.market.trending()
        except MarketError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return
        is_trending = any(item.symbol.upper() == quote.ticker for item in trending)
        emb = self._coin_embed(quote, trending=is_trending)
        news = await self.hype.news(f"{quote.name} crypto", limit=3)
        if news:
            emb.add_field(
                name="📰 Latest",
                value="\n".join(f"• [{truncate(item.title, 100)}]({item.url})" for item in news),
                inline=False,
            )
        await interaction.followup.send(embed=emb)

    @sniper.command(name="analyze", description="AI read on a coin using price, hype and news")
    @app_commands.describe(symbol="Ticker, e.g. BTC, ETH, SOL")
    @app_commands.checks.cooldown(3, 60.0, key=lambda i: i.user.id)
    async def analyze(self, interaction: discord.Interaction, symbol: str) -> None:
        await interaction.response.defer(thinking=True)
        try:
            quote = await self.market.quote(symbol)
            universe = await self.market.top_coins(100)
            trending = await self.market.trending()
        except MarketError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return

        snapshot = await self.hype.snapshot(universe, trending)
        hype_item = next((item for item in snapshot.items if item.symbol == quote.ticker), None)
        is_trending = any(item.symbol.upper() == quote.ticker for item in trending)
        global_stats = {}
        try:
            global_stats = await self.market.global_stats()
        except MarketError:
            pass

        verdict = await self.analyst.analyze(
            quote, hype=hype_item, news=snapshot.news, trending=is_trending, global_stats=global_stats
        )
        color = {"bullish": SUCCESS, "bearish": LEAK}.get(verdict.verdict, WARNING)
        emb = embed(
            f"{verdict.emoji} {quote.name} (${quote.ticker}) · {verdict.verdict.upper()}",
            f"`{progress_bar(verdict.confidence)}` **{verdict.confidence}% confidence** · "
            f"horizon {verdict.horizon}",
            color=color,
            thumbnail=quote.image,
            fields=[
                ("Price", human_number(quote.price), True),
                ("24h", pct(quote.change_24h), True),
                ("7d", pct(quote.change_7d), True),
                (
                    "Social",
                    (
                        f"{hype_item.mentions} Reddit mentions · score {hype_item.score:.1f}"
                        if hype_item
                        else "No notable social activity"
                    ),
                    True,
                ),
                ("Key signals", "\n".join(f"• {s}" for s in verdict.key_signals), False),
                ("Risks", "\n".join(f"• {r}" for r in verdict.risks), False),
                ("English read", verdict.summary_en, False),
                ("বাংলায়", verdict.summary_bn, False),
            ],
            footer=f"{'Gemini analysis' if verdict.source == 'gemini' else 'Rule-based fallback (no Gemini key)'} · {DISCLAIMER}",
        )
        await interaction.followup.send(embed=emb)

    @sniper.command(name="movers", description="Top gainers and losers in the last 24h")
    async def movers(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        try:
            gainers, losers = await self.market.top_movers(5)
            stats = await self.market.global_stats()
        except MarketError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return

        def rows(coins: list[CoinQuote]) -> str:
            return "\n".join(
                f"**${coin.symbol}** {pct(coin.change_24h)} · {human_number(coin.price)}"
                for coin in coins
            ) or "n/a"

        emb = embed(
            "📊 Market movers (24h)",
            f"Total market cap: **{human_number(stats.get('market_cap_usd'))}** · "
            f"BTC dominance: **{stats.get('btc_dominance') or 0:.1f}%**",
            color=CRYPTO,
            fields=[
                ("🟢 Gainers", rows(gainers), True),
                ("🔴 Losers", rows(losers), True),
            ],
            footer=DISCLAIMER,
        )
        await interaction.followup.send(embed=emb)

    @sniper.command(name="newpairs", description="Fresh DEX listings from Dexscreener")
    @app_commands.describe(count="How many pairs to show (1-10)")
    async def newpairs(self, interaction: discord.Interaction, count: int = 5) -> None:
        await interaction.response.defer(thinking=True)
        count = max(1, min(10, count))
        try:
            pairs = await self.market.new_pairs(count)
        except MarketError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return
        if not pairs:
            await interaction.followup.send(
                embed=embed("No fresh pairs", "Dexscreener returned no new token profiles.", color=WARNING)
            )
            return
        embeds = [self._new_pair_embed(pair) for pair in pairs[:4]]
        await interaction.followup.send(embeds=embeds)

    @sniper.command(name="stock", description="Live US stock quote (needs FINNHUB_API_KEY)")
    @app_commands.describe(ticker="US ticker, e.g. AAPL, TSLA, NVDA")
    async def stock(self, interaction: discord.Interaction, ticker: str) -> None:
        await interaction.response.defer(thinking=True)
        try:
            quote: StockQuote = await self.market.stock_quote(ticker)
        except MarketError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return
        color = SUCCESS if (quote.change or 0) >= 0 else LEAK
        emb = embed(
            f"📈 {quote.symbol} · {human_number(quote.price)}",
            f"**{quote.change:+.2f} ({quote.change_pct:+.2f}%)** today"
            if quote.change is not None
            else "Quote data unavailable",
            color=color,
            fields=[
                ("Open", human_number(quote.open), True),
                ("Previous close", human_number(quote.previous_close), True),
                ("Day high", human_number(quote.high), True),
                ("Day low", human_number(quote.low), True),
                ("As of", format_ts(quote.timestamp, "T") if quote.timestamp else "—", True),
            ],
            footer=f"Quote via Finnhub · {DISCLAIMER}",
        )
        await interaction.followup.send(embed=emb)

    # -- price alerts ------------------------------------------------------
    @sniper.command(name="watch", description="Get pinged when a coin crosses a price")
    @app_commands.describe(
        symbol="Coin ticker, e.g. BTC",
        direction="Trigger when price goes above or below the target",
        price="Target price in USD",
    )
    @app_commands.choices(direction=DIRECTIONS)
    async def watch(
        self,
        interaction: discord.Interaction,
        symbol: str,
        direction: app_commands.Choice[str],
        price: float,
    ) -> None:
        if interaction.guild_id is None or interaction.channel_id is None:
            await interaction.response.send_message(
                embed=error_embed("Alerts only work inside a server."), ephemeral=True
            )
            return
        if price <= 0:
            await interaction.response.send_message(
                embed=error_embed("Price must be greater than zero."), ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            quote = await self.market.quote(symbol)
        except MarketError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return
        rule_id = await self.alerts.add_rule(
            interaction.guild_id,
            interaction.channel_id,
            quote.ticker,
            direction.value,
            price,
            created_by=interaction.user.id,
        )
        already = self.alerts.is_triggered(
            PriceRule(
                id=rule_id,
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                symbol=quote.ticker,
                direction=direction.value,
                target_price=price,
            ),
            quote.price or 0.0,
        )
        await interaction.followup.send(
            embed=success_embed(
                f"Alert `#{rule_id}` created.\n"
                f"**{quote.ticker}** {direction.value} **${price:,.8g}**\n"
                f"Current price: {human_number(quote.price)}\n\n"
                + (
                    "⚠️ This condition is already true, so it will fire on the next check."
                    if already
                    else f"Checked every {self.alerts.settings.alert_interval_seconds}s."
                ),
                title="🚨 Price alert armed",
            ),
            ephemeral=True,
        )

    @sniper.command(name="alerts", description="List your active price alerts")
    async def alerts_list(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                embed=error_embed("Alerts only work inside a server."), ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        rules = await self.alerts.list_rules(interaction.guild_id)
        if not rules:
            await interaction.followup.send(
                embed=embed(
                    "No active alerts",
                    "Create one with `/sniper watch symbol:BTC direction:above price:100000`.",
                    color=CRYPTO,
                )
            )
            return
        lines = [
            f"`#{rule.id}` **${rule.symbol}** {rule.direction} "
            f"**${rule.target_price:,.8g}**"
            + (f" · last {human_number(rule.last_price)}" if rule.last_price else "")
            + f" · <#{rule.channel_id}>"
            for rule in rules
        ]
        await interaction.followup.send(
            embed=list_embed(f"🚨 Active price alerts ({len(rules)})", lines, color=CRYPTO)
        )

    @sniper.command(name="unwatch", description="Remove a price alert")
    @app_commands.describe(alert_id="The id shown by /sniper alerts")
    async def unwatch(self, interaction: discord.Interaction, alert_id: int) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                embed=error_embed("Alerts only work inside a server."), ephemeral=True
            )
            return
        rules = await self.alerts.list_rules(interaction.guild_id, include_triggered=True)
        rule = next((rule for rule in rules if rule.id == alert_id), None)
        if rule is None:
            await interaction.response.send_message(
                embed=error_embed(f"No alert `#{alert_id}` in this server."), ephemeral=True
            )
            return
        is_mod = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild
        if rule.created_by not in (None, interaction.user.id) and not is_mod:
            await interaction.response.send_message(
                embed=error_embed("Only the creator or a server manager can remove this alert."),
                ephemeral=True,
            )
            return
        await self.alerts.remove_rule(interaction.guild_id, alert_id)
        await interaction.response.send_message(
            embed=success_embed(f"Alert `#{alert_id}` removed.", title="Alert deleted"),
            ephemeral=True,
        )

    @sniper.command(name="channel", description="Set the default channel for price alerts")
    @app_commands.describe(channel="Text channel (defaults to this channel)")
    async def channel(
        self, interaction: discord.Interaction, channel: discord.TextChannel | None = None
    ) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                embed=error_embed("Alerts only work inside a server."), ephemeral=True
            )
            return
        member = interaction.user
        if not (isinstance(member, discord.Member) and member.guild_permissions.manage_guild):
            await interaction.response.send_message(
                embed=error_embed("You need the **Manage Server** permission to do that."),
                ephemeral=True,
            )
            return
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                embed=error_embed("Pick a regular text channel."), ephemeral=True
            )
            return
        await self.alerts.set_channel(interaction.guild_id, target.id)
        await interaction.response.send_message(
            embed=success_embed(f"Price alerts will post in {target.mention}.", title="Channel set"),
            ephemeral=True,
        )

    # -- errors ------------------------------------------------------------
    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"Analysis is rate-limited — retry in **{error.retry_after:.0f}s**."
        elif isinstance(error, app_commands.MissingPermissions):
            message = "You need the **Manage Server** permission for that."
        else:
            log.exception("Sniper command error", exc_info=error)
            message = "Unexpected market data error. Check the bot logs."
        if interaction.response.is_done():
            await interaction.followup.send(embed=error_embed(message), ephemeral=True)
        else:
            await interaction.response.send_message(embed=error_embed(message), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    market = getattr(bot, "market_data", None)
    hype = getattr(bot, "hype_engine", None)
    analyst = getattr(bot, "market_analyst", None)
    alerts = getattr(bot, "price_alerts", None)
    if not all((market, hype, analyst, alerts)):
        raise RuntimeError("Sniper services were not attached to the bot")
    await bot.add_cog(SniperCog(bot, market, hype, analyst, alerts))
