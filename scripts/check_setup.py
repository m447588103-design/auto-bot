#!/usr/bin/env python3
"""Pre-flight doctor: verifies dependencies, keys and directories before launch.

Usage::

    python scripts/check_setup.py            # offline checks
    python scripts/check_setup.py --network  # also test public APIs
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.config import settings  # noqa: E402


def line(ok: bool | None, label: str, detail: str = "") -> None:
    icon = "✅" if ok else ("⚠️ " if ok is None else "❌")
    print(f"{icon} {label:<28} {detail}")


def check_python() -> bool:
    ok = sys.version_info >= (3, 11)
    line(ok, "Python >= 3.11", sys.version.split()[0])
    return ok


def check_imports() -> bool:
    modules = {
        "discord": "discord.py",
        "aiohttp": "aiohttp",
        "dotenv": "python-dotenv",
        "google.genai": "google-genai",
        "edge_tts": "edge-tts",
        "yt_dlp": "yt-dlp",
    }
    all_ok = True
    for module, package in modules.items():
        try:
            importlib.import_module(module)
            line(True, module)
        except ImportError:
            line(False, module, f"pip install {package}")
            all_ok = False
    for optional in ("nacl", "gtts", "playwright"):
        try:
            importlib.import_module(optional)
            line(True, f"{optional} (optional)")
        except ImportError:
            line(None, f"{optional} (optional)", "not installed")
    return all_ok


def check_binaries() -> bool:
    ffmpeg = shutil.which(settings.cinematic.ffmpeg_bin)
    ffprobe = shutil.which(settings.cinematic.ffprobe_bin)
    line(bool(ffmpeg), "ffmpeg", ffmpeg or "install ffmpeg")
    line(bool(ffprobe), "ffprobe", ffprobe or "install ffmpeg")
    return bool(ffmpeg and ffprobe)


def check_config() -> bool:
    """Only the Discord token is required; other keys are reported as optional."""
    line(bool(settings.discord.token), "DISCORD_BOT_TOKEN", "required" if not settings.discord.token else "set")
    optional = (
        ("GEMINI_API_KEY", settings.gemini.configured, "AI features use fallbacks"),
        ("HIBP_API_KEY", settings.leak.hibp_configured, "HIBP scans disabled"),
        ("PEXELS_API_KEY", settings.cinematic.pexels_configured, "using picsum fallback"),
        ("Spotify", settings.dj.spotify_configured, "YouTube-only curation"),
        ("FINNHUB_API_KEY", bool(settings.sniper.finnhub_api_key), "/sniper stock disabled"),
        ("X_BEARER_TOKEN", bool(settings.sniper.x_bearer_token), "X sentiment disabled"),
    )
    for label, configured, hint in optional:
        line(True if configured else None, label, "set" if configured else hint)
    return bool(settings.discord.token)


def check_paths() -> None:
    for label, path in (
        ("data dir", settings.data_dir),
        ("cinematic workdir", settings.cinematic.workdir),
        ("music library", settings.cinematic.music_dir),
    ):
        exists = path.exists()
        if not exists and label != "music library":
            path.mkdir(parents=True, exist_ok=True)
            exists = True
        line(exists, label, str(path))


async def network_checks() -> None:
    import aiohttp

    endpoints = {
        "CoinGecko": "https://api.coingecko.com/api/v3/ping",
        "Pexels": "https://api.pexels.com/videos/search?query=sunset&per_page=1",
        "Dexscreener": "https://api.dexscreener.com/latest/dex/search?q=SOL",
        "HIBP": "https://haveibeenpwned.com/api/v3/breaches",
        "ransomwatch": "https://raw.githubusercontent.com/joshhighet/ransomwatch/main/posts.json",
        "Reddit": "https://www.reddit.com/r/CryptoCurrency/hot.json?limit=1",
        "YouTube": "https://www.youtube.com",
        "Google": "https://www.google.com",
    }
    headers = {"User-Agent": "AutoBot/2.0 setup-checker"}
    if settings.cinematic.pexels_api_key:
        headers["Authorization"] = settings.cinematic.pexels_api_key
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), headers=headers) as session:
        for name, url in endpoints.items():
            try:
                async with session.get(url) as response:
                    line(response.status < 500, name, f"HTTP {response.status}")
            except Exception as exc:  # noqa: BLE001
                line(False, name, str(exc)[:80])


def main() -> int:
    parser = argparse.ArgumentParser(description="Check AutoBot prerequisites")
    parser.add_argument("--network", action="store_true", help="also test public APIs")
    args = parser.parse_args()

    print("=" * 62)
    print(" AutoBot setup check")
    print("=" * 62)
    print("\n[Python & packages]")
    deps_ok = check_imports()
    print("\n[Binaries]")
    bins_ok = check_binaries()
    print("\n[Configuration]")
    token_ok = check_config()
    print("\n[Paths]")
    check_paths()

    if args.network:
        print("\n[Network]")
        asyncio.run(network_checks())

    print("\n" + "=" * 62)
    if not (deps_ok and bins_ok and token_ok):
        print(" Fix the ❌ items above, then run this script again.")
        return 1
    print(" Everything looks good. Start the bot with: python main.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
