#!/usr/bin/env python3
"""One-time OAuth consent for YouTube Shorts uploads.

Usage::

    pip install google-api-python-client google-auth google-auth-oauthlib
    python scripts/youtube_auth.py

It opens a browser, asks for the ``youtube.upload`` scope and writes the token
to ``credentials/youtube_token.json``. Point ``YOUTUBE_TOKEN_FILE`` at that file
and ``YOUTUBE_CLIENT_SECRET`` at the OAuth client JSON downloaded from Google
Cloud Console.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

SCOPE = ["https://www.googleapis.com/auth/youtube.upload"]


def main() -> int:
    load_dotenv(ROOT / ".env")
    client_secret = Path(
        os.getenv("YOUTUBE_CLIENT_SECRET", "credentials/client_secret.json")
    ).expanduser()
    token_file = Path(os.getenv("YOUTUBE_TOKEN_FILE", "credentials/youtube_token.json")).expanduser()
    if not client_secret.is_absolute():
        client_secret = ROOT / client_secret
    if not token_file.is_absolute():
        token_file = ROOT / token_file

    if not client_secret.exists():
        print(f"❌ OAuth client secret not found: {client_secret}")
        print("   Download it from Google Cloud Console → APIs & Services → Credentials")
        print("   (OAuth client type: Desktop app) and set YOUTUBE_CLIENT_SECRET in .env.")
        return 1

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("❌ Install dependencies first:")
        print("   pip install google-api-python-client google-auth google-auth-oauthlib")
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPE)
    print("Opening the browser for Google consent…")
    credentials = flow.run_local_server(port=0, prompt="consent")
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(credentials.to_json(), encoding="utf-8")
    print(f"✅ Token saved to {token_file}")
    print("   Add this to .env: YOUTUBE_TOKEN_FILE=" + str(token_file.relative_to(ROOT)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
