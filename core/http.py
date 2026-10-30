"""Async HTTP helpers with retries, sane headers and JSON decoding."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import aiohttp

log = logging.getLogger(__name__)

USER_AGENT = "AutoBot/2.0 (+https://github.com/m447588103-design/auto-bot)"
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class HTTPError(RuntimeError):
    """Raised when a request fails after retries."""

    def __init__(self, message: str, *, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


def default_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
    }
    if extra:
        headers.update({k: v for k, v in extra.items() if v})
    return headers


@asynccontextmanager
async def http_session(
    *,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
) -> AsyncIterator[aiohttp.ClientSession]:
    connector = aiohttp.TCPConnector(limit=20, ttl_dns_cache=300)
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout, connect=min(10.0, timeout)),
        headers=default_headers(headers),
        connector=connector,
    ) as session:
        yield session


async def request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    *,
    retries: int = 2,
    backoff: float = 1.5,
    expect: str = "json",
    timeout: float | None = None,
    **kwargs: Any,
) -> Any:
    """Perform a request, retrying transient failures.

    ``expect`` is one of ``"json"``, ``"text"``, ``"bytes"`` or ``"none"``.
    """
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            request_kwargs = dict(kwargs)
            if timeout is not None:
                request_kwargs["timeout"] = aiohttp.ClientTimeout(total=timeout)
            async with session.request(method, url, **request_kwargs) as response:
                if response.status in RETRYABLE_STATUS:
                    retry_after = response.headers.get("Retry-After")
                    delay = None
                    if retry_after:
                        try:
                            delay = min(60.0, float(retry_after))
                        except ValueError:
                            delay = None
                    body = (await response.text())[:300]
                    last_error = HTTPError(
                        f"HTTP {response.status} from {url}: {body}",
                        status=response.status,
                        url=url,
                    )
                    if attempt < retries:
                        wait = delay if delay is not None else backoff * (2**attempt)
                        log.warning("Retrying %s in %.1fs (status %s)", url, wait, response.status)
                        await asyncio.sleep(wait + random.uniform(0, 0.4))
                        continue
                    raise last_error

                if response.status >= 400:
                    body = (await response.text())[:300]
                    raise HTTPError(
                        f"HTTP {response.status} from {url}: {body}",
                        status=response.status,
                        url=url,
                    )

                if expect == "json":
                    raw = await response.text()
                    if not raw.strip():
                        return None
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError as exc:
                        raise HTTPError(f"Invalid JSON from {url}: {exc}", status=response.status, url=url)
                if expect == "text":
                    return await response.text()
                if expect == "bytes":
                    return await response.read()
                return None
        except (aiohttp.ClientError, asyncio.TimeoutError, HTTPError) as exc:
            last_error = exc
            retryable = not isinstance(exc, HTTPError) or exc.status in RETRYABLE_STATUS or exc.status is None
            if attempt >= retries or not retryable:
                break
            wait = backoff * (2**attempt)
            log.warning("Retrying %s in %.1fs (%s)", url, wait, exc)
            await asyncio.sleep(wait + random.uniform(0, 0.4))
    if isinstance(last_error, HTTPError):
        raise last_error
    raise HTTPError(f"Request to {url} failed: {last_error}", url=url)


async def get_json(session: aiohttp.ClientSession, url: str, **kwargs: Any) -> Any:
    return await request(session, "GET", url, expect="json", **kwargs)


async def get_text(session: aiohttp.ClientSession, url: str, **kwargs: Any) -> str:
    return await request(session, "GET", url, expect="text", **kwargs)
