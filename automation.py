"""Optional Playwright browser automation (lazy import, headless Chromium)."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 20_000


async def run_browser_task(query: str) -> str:
    """Open a page relevant to the query and return a short summary."""
    from playwright.async_api import async_playwright  # lazy optional dependency

    query = (query or "").strip()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(user_agent="AutoBot/2.0")
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)
        try:
            if "github" in query.lower():
                target = "https://github.com/trending"
            else:
                target = "https://duckduckgo.com/?q=" + _quote(query)
            await page.goto(target, wait_until="domcontentloaded")
            title = await page.title()
            text = await page.inner_text("body")
            excerpt = " ".join(text.split())[:600]
            return (
                f"Opened <{target}>\n"
                f"**Title:** {title}\n"
                f"**Excerpt:** {excerpt}\n"
                f"(handled with Playwright Chromium)"
            )
        finally:
            await browser.close()


def _quote(value: str) -> str:
    from urllib.parse import quote_plus

    return quote_plus(value[:200])


if __name__ == "__main__":  # manual smoke test
    logging.basicConfig(level=logging.INFO)
    print(asyncio.run(run_browser_task("github trending")))
