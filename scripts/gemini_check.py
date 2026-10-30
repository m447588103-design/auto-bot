#!/usr/bin/env python3
"""Small, secret-safe Gemini health check.

Run from the repository root with the same virtualenv as the bot::

    python scripts/gemini_check.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import settings  # noqa: E402
from core.gemini import get_gemini, thinking_budget_for_model  # noqa: E402


async def main() -> int:
    client = get_gemini()
    print(f"GEMINI_API_KEY set : {bool(settings.gemini.api_key)}")
    print(f"GEMINI_MODEL       : {settings.gemini.model}")
    print(f"SDK backend        : {client.backend or 'none'}")
    print(f"Client available   : {client.available}")
    print(
        "Thinking mode      : "
        f"{'off' if thinking_budget_for_model(client.model) == 0 else 'default'}"
    )
    if not client.available:
        print("\nNo usable Gemini client. Add GEMINI_API_KEY to .env and restart.")
        return 1

    print("\nCalling Gemini with a tiny prompt…")
    try:
        reply = await client.generate(
            "Reply with exactly: OK",
            max_output_tokens=64,
            temperature=0.0,
        )
    except Exception as exc:  # noqa: BLE001 - this script is a diagnostic
        print(f"API test FAILED: {type(exc).__name__}: {exc}")
        print("\nCheck the key, model name, quota and network connection.")
        return 1

    print(f"API test OK: {reply.text[:200]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
