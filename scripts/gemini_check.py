#!/usr/bin/env python3
"""Small, secret-safe Gemini health check.

Run from the repository root with the same virtualenv as the bot::

    python scripts/gemini_check.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import settings  # noqa: E402
from core.gemini import (  # noqa: E402
    get_gemini,
    thinking_budget_for_model,
    thinking_level_for_model,
)
from features.companion.service import CompanionService  # noqa: E402


async def main(use_gf_prompt: bool = False) -> int:
    client = get_gemini()
    print(f"GEMINI_API_KEY set : {bool(settings.gemini.api_key)}")
    print(f"GEMINI_MODEL       : {settings.gemini.model}")
    print(f"SDK backend        : {client.backend or 'none'}")
    print(f"Client available   : {client.available}")
    level = thinking_level_for_model(client.model)
    budget = thinking_budget_for_model(client.model)
    if level:
        thinking = f"low ({level})"
    elif budget == 0:
        thinking = "off"
    else:
        thinking = "default"
    print(f"Thinking mode      : {thinking}")
    if not client.available:
        print("\nNo usable Gemini client. Add GEMINI_API_KEY to .env and restart.")
        return 1

    if use_gf_prompt:
        # Reproduce the real companion request, including its long system prompt.
        service = object.__new__(CompanionService)
        service.settings = settings.companion
        prompt = service._gf_prompt("ProbeUser", [], "Hello, kemon acho?")
        system = service._gf_system("ProbeUser")
        max_tokens = 800
        temperature = 0.95
        print("\nCalling Gemini with the real girlfriend prompt…")
    else:
        prompt = "Reply with exactly: OK"
        system = None
        max_tokens = 512
        temperature = 0.0
        print("\nCalling Gemini with a tiny prompt…")

    try:
        reply = await client.generate(
            prompt,
            system_instruction=system,
            max_output_tokens=max_tokens,
            temperature=temperature,
        )
    except Exception as exc:  # noqa: BLE001 - this script is a diagnostic
        print(f"API test FAILED: {type(exc).__name__}: {exc}")
        print("\nCheck the key, model name, quota and network connection.")
        return 1

    print(f"API test OK: {reply.text[:200]}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check Gemini configuration and a live call.")
    parser.add_argument(
        "--gf",
        action="store_true",
        help="use the same long system prompt and budget as !gf",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.gf)))
