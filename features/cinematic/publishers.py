"""Optional publishing integrations (YouTube Shorts).

Instagram Reels publishing is intentionally **not** implemented: the Instagram
Graph API only accepts videos from a public HTTPS URL on a Business/Creator
account, which a self-hosted Discord bot does not have. Export the generated
mp4 from Discord and upload it from the Instagram app instead.

YouTube upload requires the optional packages::

    pip install google-api-python-client google-auth google-auth-oauthlib

and a one-time OAuth consent (see ``scripts/youtube_auth.py``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from core.config import CinematicSettings

from .models import MovieScript

log = logging.getLogger(__name__)

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"


class PublishError(RuntimeError):
    pass


@dataclass
class PublishResult:
    platform: str
    url: str
    video_id: str | None = None


class YouTubePublisher:
    def __init__(self, settings: CinematicSettings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(
            self.settings.youtube_client_secret
            and self.settings.youtube_token_file
            and self.settings.youtube_client_secret.exists()
            and self.settings.youtube_token_file.exists()
        )

    def _service(self):
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover
            raise PublishError(
                "YouTube upload needs: pip install google-api-python-client google-auth google-auth-oauthlib"
            ) from exc

        token_file = self.settings.youtube_token_file
        assert token_file is not None
        creds = Credentials.from_authorized_user_file(str(token_file), [YOUTUBE_UPLOAD_SCOPE])
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_file.write_text(creds.to_json(), encoding="utf-8")
        return build("youtube", "v3", credentials=creds, cache_discovery=False)

    async def upload(self, video: Path, script: MovieScript) -> PublishResult:
        if not self.configured:
            raise PublishError(
                "YouTube upload is not configured. Set YOUTUBE_CLIENT_SECRET and "
                "YOUTUBE_TOKEN_FILE in .env, then run `python scripts/youtube_auth.py` once."
            )

        def _upload() -> PublishResult:
            from googleapiclient.http import MediaFileUpload

            service = self._service()
            description = script.logline + "\n\n" + " ".join(script.clean_hashtags())
            body = {
                "snippet": {
                    "title": f"{script.title} #Shorts"[:100],
                    "description": description[:4900],
                    "tags": [tag.lstrip("#") for tag in script.clean_hashtags()][:15],
                    "categoryId": "22",
                },
                "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False},
            }
            media = MediaFileUpload(str(video), mimetype="video/mp4", resumable=True)
            request = service.videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    log.info("YouTube upload progress: %s%%", int(status.progress() * 100))
            video_id = response.get("id")
            if not video_id:
                raise PublishError("YouTube did not return a video id")
            return PublishResult(
                platform="youtube",
                url=f"https://youtube.com/shorts/{video_id}",
                video_id=video_id,
            )

        return await asyncio.to_thread(_upload)
