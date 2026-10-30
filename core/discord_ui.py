"""Discord presentation helpers: brand embeds, throttled progress reporter."""

from __future__ import annotations

import logging
import time
from typing import Iterable, Sequence

import discord

from .utils import progress_bar, truncate

log = logging.getLogger(__name__)

BRAND = discord.Color.from_str("#5865F2")
LEAK = discord.Color.from_str("#ED4245")
CINEMA = discord.Color.from_str("#F1C40F")
MUSIC = discord.Color.from_str("#1DB954")
CRYPTO = discord.Color.from_str("#F7931A")
SUCCESS = discord.Color.from_str("#57F287")
WARNING = discord.Color.from_str("#FEE75C")
NEUTRAL = discord.Color.from_str("#95A5A6")

FOOTER = "AutoBot • AI automation suite"
GUILD_ONLY_NOTE = "AutoBot • drop-in AI automation for Discord servers"

DISCLAIMER = (
    "⚠️ **Not financial advice.** Crypto is volatile; always do your own research "
    "and never risk money you cannot afford to lose."
)


def embed(
    title: str,
    description: str | None = None,
    *,
    color: discord.Color = BRAND,
    fields: Sequence[tuple[str, str, bool]] | None = None,
    footer: str | None = FOOTER,
    thumbnail: str | None = None,
    image: str | None = None,
    url: str | None = None,
) -> discord.Embed:
    emb = discord.Embed(title=truncate(title, 256, suffix="…"), description=description, color=color, url=url)
    for name, value, inline in fields or ():
        if not value:
            continue
        emb.add_field(name=truncate(name, 256), value=truncate(str(value), 1024), inline=inline)
    if footer:
        emb.set_footer(text=footer)
    if thumbnail:
        emb.set_thumbnail(url=thumbnail)
    if image:
        emb.set_image(url=image)
    return emb


def error_embed(message: str, *, title: str = "Something went wrong") -> discord.Embed:
    return embed(title, truncate(message, 3500), color=LEAK, footer=FOOTER)


def success_embed(message: str, *, title: str = "Done") -> discord.Embed:
    return embed(title, truncate(message, 3500), color=SUCCESS, footer=FOOTER)


def info_embed(message: str, *, title: str) -> discord.Embed:
    return embed(title, truncate(message, 3500), color=BRAND, footer=FOOTER)


def list_embed(
    title: str,
    rows: Iterable[str],
    *,
    empty: str = "Nothing here yet.",
    color: discord.Color = BRAND,
    per_field: int = 15,
    description: str | None = None,
) -> discord.Embed:
    items = [row for row in rows if row]
    if not items:
        return embed(title, description or empty, color=color)
    pages: list[list[str]] = [items[i : i + per_field] for i in range(0, len(items), per_field)]
    emb = embed(title, description, color=color)
    for index, page in enumerate(pages[:5], start=1):
        field_name = "Entries" if len(pages) == 1 else f"Entries ({index}/{len(pages)})"
        emb.add_field(name=field_name, value=truncate("\n".join(page), 1024), inline=False)
    return emb


class ProgressReporter:
    """Edits a single Discord message while a long job progresses.

    ``target`` can be an :class:`discord.Interaction` (already deferred) or a
    :class:`discord.abc.Messageable` channel.
    """

    def __init__(self, target, *, title: str, min_interval: float = 2.5):
        self.target = target
        self.title = title
        self.min_interval = min_interval
        self._last_update = 0.0
        self._message: discord.Message | None = None
        self._last_text = ""
        self._closed = False

    async def start(self) -> None:
        await self._render(0, "Starting…")

    async def update(self, percent: float, stage: str, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_update < self.min_interval:
            return
        await self._render(percent, stage)

    async def _render(self, percent: float, stage: str) -> None:
        text = f"`{progress_bar(percent)}` {percent:>3.0f}%\n{truncate(stage, 250)}"
        if text == self._last_text:
            return
        emb = embed(self.title, text, color=CINEMA)
        self._last_text = text
        self._last_update = time.monotonic()
        try:
            if isinstance(self.target, discord.Interaction):
                if self._message is None:
                    self._message = await self.target.edit_original_response(embed=emb)
                else:
                    await self._message.edit(embed=emb)
            else:
                if self._message is None:
                    self._message = await self.target.send(embed=emb)
                else:
                    await self._message.edit(embed=emb)
        except discord.HTTPException as exc:
            log.debug("Progress update failed: %s", exc)

    async def finish(self, emb: discord.Embed) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if isinstance(self.target, discord.Interaction):
                if self._message is None:
                    await self.target.edit_original_response(embed=emb)
                else:
                    await self._message.edit(embed=emb)
            else:
                if self._message is None:
                    await self.target.send(embed=emb)
                else:
                    await self._message.edit(embed=emb)
        except discord.HTTPException as exc:
            log.debug("Progress finish failed: %s", exc)

    async def fail(self, message: str) -> None:
        await self.finish(error_embed(message, title=f"{self.title} failed"))
