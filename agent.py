"""General-purpose assistant used by the ``!agent`` prefix command."""

from __future__ import annotations

import logging

from core.gemini import GeminiError, GeminiUnavailable, get_gemini

log = logging.getLogger(__name__)

BROWSER_TRIGGERS = ("browse", "scrape", "search the web", "open website", "visit ")

SYSTEM = (
    "You are AutoBot, a concise Discord assistant. Answer in the user's language "
    "(Bangla, Banglish or English). Use short paragraphs and Discord markdown. "
    "Never claim to have run code or browsed the web unless the tool result is included."
)


async def process_agent_command(prompt: str) -> str:
    """Handle a free-form assistant request.

    Browser automation (Playwright) is imported lazily so the bot can run without
    the optional Playwright dependency installed.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        return "Give me a question or task, e.g. `!agent explain quantum computing simply`."

    lowered = prompt.lower()
    if any(trigger in lowered for trigger in BROWSER_TRIGGERS):
        try:
            from automation import run_browser_task

            result = await run_browser_task(prompt)
            return f"🌐 **Browser automation**\n{result}"
        except ImportError:
            return (
                "Browser automation is optional. Install it with:\n"
                "```\npip install playwright && playwright install chromium\n```"
            )
        except Exception as exc:  # noqa: BLE001
            log.exception("Browser task failed")
            return f"❌ Browser task failed: {exc}"

    gemini = get_gemini()
    try:
        reply = await gemini.generate(prompt, system_instruction=SYSTEM, max_output_tokens=1200)
        return reply.text
    except GeminiUnavailable:
        return "⚠️ `GEMINI_API_KEY` is not configured, so I cannot answer that right now."
    except GeminiError as exc:
        log.warning("Gemini assistant error: %s", exc)
        return f"❌ The AI request failed: {exc}"
