"""Leak intelligence sources.

Every source is optional and degrades gracefully:

===========================  ==========================  =====================
Source                       What it queries             Credentials
===========================  ==========================  =====================
Have I Been Pwned            account/domain breaches      HIBP_API_KEY (paid)
Hudson Rock (Cavalier)       infostealer infections       none (free tier)
ransomwatch                  ransomware victim posts      none
psbdmp.ws                    paste-site dumps             none
GitHub code search           leaked configs in repos      GITHUB_TOKEN
===========================  ==========================  =====================

Sources never receive raw secrets: alerts only expose metadata, never the
recovered passwords that some feeds return.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Sequence
from urllib.parse import quote

import aiohttp

from core.config import LeakSettings
from core.http import HTTPError, get_json
from core.utils import clean_text, parse_iso, truncate, url_hash

from .models import RawHit, Severity, SourceState, TargetKind, WatchTarget

log = logging.getLogger(__name__)

HIBP_BASE = "https://haveibeenpwned.com/api/v3"
HUDSON_BASE = "https://cavalier.hudsonrock.com/api/json/v2/osint-tools"
RANSOMWATCH_URL = "https://raw.githubusercontent.com/joshhighet/ransomwatch/main/posts.json"
PSBDMP_SEARCH = "https://psbdmp.ws/api/search"
GITHUB_SEARCH = "https://api.github.com/search/code"


@dataclass
class SourceResult:
    hits: list[RawHit] = field(default_factory=list)
    cursor: str | None = None
    warnings: list[str] = field(default_factory=list)


class LeakSource:
    name: str = "base"
    min_interval: int = 3600
    requires: str | None = None

    @property
    def available(self) -> bool:
        return True

    @property
    def reason_unavailable(self) -> str | None:
        return None

    async def scan(
        self,
        session: aiohttp.ClientSession,
        targets: Sequence[WatchTarget],
        state: SourceState,
    ) -> SourceResult:  # pragma: no cover - abstract
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Have I Been Pwned
# ---------------------------------------------------------------------------
class HibpSource(LeakSource):
    name = "hibp"
    min_interval = 3600

    def __init__(self, settings: LeakSettings, max_targets: int = 8):
        self.api_key = settings.hibp_api_key
        self.timeout = settings.request_timeout
        self.max_targets = max_targets

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def reason_unavailable(self) -> str | None:
        return None if self.api_key else "HIBP_API_KEY is not set"

    async def scan(self, session, targets, state) -> SourceResult:
        result = SourceResult()
        if not self.api_key:
            return result

        interesting = [t for t in targets if t.kind_enum in (TargetKind.EMAIL, TargetKind.DOMAIN)]
        headers = {"hibp-api-key": self.api_key, "User-Agent": "AutoBot-LeakMonitor"}
        for target in interesting[: self.max_targets]:
            # HIBP allows ~1 request every 1.5s
            await asyncio.sleep(1.6)
            try:
                if target.kind_enum is TargetKind.EMAIL:
                    url = f"{HIBP_BASE}/breachedaccount/{quote(target.value, safe='@')}?truncateResponse=false"
                else:
                    url = f"{HIBP_BASE}/breacheddomain/{quote(target.value)}"
                data = await get_json(
                    session, url, headers=headers, timeout=self.timeout, retries=1
                )
                result.hits.extend(self._parse(target, data))
            except HTTPError as exc:
                if exc.status == 404:
                    continue  # clean: no breach for this account/domain
                if exc.status == 403:
                    result.warnings.append(
                        "HIBP domain search requires the domain to be verified in your HIBP dashboard."
                    )
                    continue
                if exc.status == 401:
                    result.warnings.append("HIBP API key rejected (401).")
                    break
                result.warnings.append(f"HIBP error for {target.display}: {exc}")
        return result

    @staticmethod
    def _parse(target: WatchTarget, data: Any) -> list[RawHit]:
        if not data:
            return []
        if isinstance(data, dict) and "breach" in data:  # unusual shape
            data = [data]
        if not isinstance(data, list):
            return []
        hits: list[RawHit] = []
        for breach in data:
            if not isinstance(breach, dict):
                continue
            name = clean_text(breach.get("Name") or breach.get("Title"), limit=120) or "Unknown breach"
            domain = clean_text(breach.get("Domain"), limit=120)
            description = clean_text(breach.get("Description"), limit=500)
            classes = ", ".join(breach.get("DataClasses") or [])[:200]
            external = f"{name}:{target.value}:{breach.get('BreachDate', '')}"
            hits.append(
                RawHit(
                    source="hibp",
                    external_id=url_hash(external, length=24),
                    title=f"HIBP breach: {name}",
                    haystack=" ".join(filter(None, [name, domain, description, target.value])),
                    url=f"https://haveibeenpwned.com/PwnedWebsites#{quote(name)}",
                    snippet=truncate(
                        f"{description}\n**Exposed data:** {classes or 'unknown'}",
                        480,
                    ),
                    severity=Severity.HIGH,
                    published_at=parse_iso(breach.get("AddedDate") or breach.get("BreachDate")).isoformat()
                    if parse_iso(breach.get("AddedDate") or breach.get("BreachDate"))
                    else None,
                    metadata={"breach": name, "domain": domain, "accounts": breach.get("PwnCount")},
                )
            )
        return hits


# ---------------------------------------------------------------------------
# Hudson Rock infostealer intelligence
# ---------------------------------------------------------------------------
class HudsonRockSource(LeakSource):
    name = "hudsonrock"
    min_interval = 3600

    def __init__(self, settings: LeakSettings, max_targets: int = 6):
        self.timeout = settings.request_timeout
        self.max_targets = max_targets

    async def scan(self, session, targets, state) -> SourceResult:
        result = SourceResult()
        interesting = [t for t in targets if t.kind_enum in (TargetKind.EMAIL, TargetKind.DOMAIN)]
        for target in interesting[: self.max_targets]:
            endpoint = "search-by-email" if target.kind_enum is TargetKind.EMAIL else "search-by-domain"
            param = "email" if target.kind_enum is TargetKind.EMAIL else "domain"
            url = f"{HUDSON_BASE}/{endpoint}?{param}={quote(target.value)}"
            try:
                data = await get_json(session, url, timeout=self.timeout, retries=1)
            except HTTPError as exc:
                if exc.status in (400, 404):
                    continue
                result.warnings.append(f"Hudson Rock error for {target.display}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(f"Hudson Rock error for {target.display}: {exc}")
                continue
            result.hits.extend(self._parse(target, data))
            await asyncio.sleep(1.0)
        return result

    @staticmethod
    def _records(data: Any) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not isinstance(data, dict):
            return records
        for key in ("stealers", "employees", "clients", "users", "data"):
            value = data.get(key)
            if isinstance(value, list):
                records.extend([item for item in value if isinstance(item, dict)])
            elif isinstance(value, dict):
                records.append(value)
        return records

    def _parse(self, target: WatchTarget, data: Any) -> list[RawHit]:
        hits: list[RawHit] = []
        for record in self._records(data):
            computer = clean_text(record.get("computer_name") or record.get("computerName"), limit=80)
            os_name = clean_text(record.get("operating_system") or record.get("os"), limit=60)
            compromised = clean_text(
                record.get("date_compromised") or record.get("dateCompromised"), limit=40
            )
            malware = clean_text(record.get("malware_path"), limit=120)
            login = clean_text(record.get("top_logins") or record.get("login"), limit=80)
            identity = url_hash(target.value, computer, compromised, login, length=24)
            details = " • ".join(filter(None, [computer, os_name, compromised, malware]))
            hits.append(
                RawHit(
                    source="hudsonrock",
                    external_id=identity,
                    title="Infostealer infection detected",
                    haystack=" ".join(
                        filter(None, [target.value, computer, os_name, login, malware])
                    ),
                    url="https://www.hudsonrock.com/",
                    snippet=truncate(
                        f"Malware log referencing this target was found.\n"
                        f"**Device:** {details or 'unknown'}\n"
                        f"_Credentials are intentionally withheld._",
                        480,
                    ),
                    severity=Severity.CRITICAL,
                    metadata={"device": computer, "os": os_name},
                )
            )
        return hits


# ---------------------------------------------------------------------------
# ransomwatch (ransomware leak-site victims)
# ---------------------------------------------------------------------------
class RansomwatchSource(LeakSource):
    name = "ransomwatch"
    min_interval = 900
    MAX_POSTS = 4000

    def __init__(self, settings: LeakSettings):
        self.timeout = settings.request_timeout

    async def scan(self, session, targets, state) -> SourceResult:
        result = SourceResult()
        try:
            data = await get_json(session, RANSOMWATCH_URL, timeout=self.timeout, retries=1)
        except HTTPError as exc:
            result.warnings.append(f"ransomwatch feed unavailable: {exc}")
            return result
        if not isinstance(data, list):
            result.warnings.append("ransomwatch returned an unexpected payload")
            return result

        posts = data[-self.MAX_POSTS :]
        for post in posts:
            if not isinstance(post, dict):
                continue
            title = clean_text(post.get("post_title") or post.get("title"), limit=200)
            description = clean_text(post.get("description"), limit=500)
            group = clean_text(post.get("group_name") or post.get("group"), limit=60)
            link = post.get("link") or post.get("post_url") or ""
            discovered = clean_text(post.get("discovered"), limit=40)
            external = url_hash(link or title, discovered, length=24)
            result.hits.append(
                RawHit(
                    source="ransomwatch",
                    external_id=external,
                    title=f"Ransomware victim listed: {title or 'unknown'}",
                    haystack=" ".join(filter(None, [title, description, group, link])),
                    url=link or "https://www.ransomwatch.io",
                    snippet=truncate(
                        f"**Group:** {group or 'unknown'}\n{description or 'No description provided.'}",
                        460,
                    ),
                    severity=Severity.HIGH,
                    published_at=parse_iso(discovered).isoformat()
                    if parse_iso(discovered)
                    else None,
                    metadata={"group": group},
                )
            )
        result.cursor = str(len(posts))
        return result


# ---------------------------------------------------------------------------
# psbdmp.ws paste search
# ---------------------------------------------------------------------------
class PsbdmpSource(LeakSource):
    name = "psbdmp"
    min_interval = 1800
    MAX_TARGETS = 5

    def __init__(self, settings: LeakSettings):
        self.timeout = settings.request_timeout

    async def scan(self, session, targets, state) -> SourceResult:
        result = SourceResult()
        # Searching every keyword is expensive; prefer concrete identifiers.
        interesting = [
            t for t in targets if t.kind_enum in (TargetKind.EMAIL, TargetKind.DOMAIN, TargetKind.KEYWORD)
        ][: self.MAX_TARGETS]
        for target in interesting:
            query = target.value.lstrip("@")
            url = f"{PSBDMP_SEARCH}/{quote(query)}"
            try:
                data = await get_json(session, url, timeout=self.timeout, retries=1)
            except HTTPError as exc:
                result.warnings.append(f"psbdmp error for {target.display}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001
                result.warnings.append(f"psbdmp error for {target.display}: {exc}")
                continue
            entries = data
            if isinstance(data, dict):
                entries = data.get("data") or data.get("results") or data.get("children") or []
            if not isinstance(entries, list):
                continue
            for entry in entries[:10]:
                if not isinstance(entry, dict):
                    continue
                paste_id = str(entry.get("id") or entry.get("key") or "")[:64]
                if not paste_id:
                    continue
                tags = entry.get("tags") or entry.get("tag") or []
                if isinstance(tags, str):
                    tags = [tags]
                tags_text = ", ".join(str(tag) for tag in tags if tag)[:160]
                when = clean_text(entry.get("time") or entry.get("date"), limit=40)
                result.hits.append(
                    RawHit(
                        source="psbdmp",
                        external_id=url_hash("psbdmp", paste_id, target.value, length=24),
                        title="Possible paste dump mention",
                        haystack=" ".join(filter(None, [query, tags_text])),
                        url=f"https://psbdmp.ws/dump/{paste_id}",
                        snippet=truncate(
                            f"Paste **{paste_id}** matched this watch target.\n"
                            f"**Tags:** {tags_text or 'none'}\n**Pasted:** {when or 'unknown'}",
                            400,
                        ),
                        severity=Severity.LOW,
                        published_at=parse_iso(when).isoformat() if parse_iso(when) else None,
                        metadata={"paste_id": paste_id},
                    )
                )
            await asyncio.sleep(1.0)
        return result


# ---------------------------------------------------------------------------
# GitHub code search (leaked configs / repos)
# ---------------------------------------------------------------------------
class GitHubCodeSource(LeakSource):
    name = "github"
    min_interval = 3600
    MAX_TARGETS = 3

    def __init__(self, settings: LeakSettings):
        self.token = settings.github_token
        self.timeout = settings.request_timeout

    @property
    def available(self) -> bool:
        return bool(self.token)

    @property
    def reason_unavailable(self) -> str | None:
        return None if self.token else "GITHUB_TOKEN is not set"

    async def scan(self, session, targets, state) -> SourceResult:
        result = SourceResult()
        if not self.token:
            return result
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        interesting = [t for t in targets if t.kind_enum in (TargetKind.EMAIL, TargetKind.DOMAIN)]
        for target in interesting[: self.MAX_TARGETS]:
            query = f'"{target.value}"'
            url = f"{GITHUB_SEARCH}?q={quote(query)}&per_page=5&sort=indexed&order=desc"
            try:
                data = await get_json(session, url, headers=headers, timeout=self.timeout, retries=1)
            except HTTPError as exc:
                if exc.status in (403, 422):
                    result.warnings.append("GitHub code search rate-limited or unavailable (403/422).")
                    break
                result.warnings.append(f"GitHub search error: {exc}")
                continue
            for item in (data or {}).get("items", [])[:5]:
                repo = (item.get("repository") or {}).get("full_name", "unknown/repo")
                path = item.get("path", "")
                html_url = item.get("html_url")
                result.hits.append(
                    RawHit(
                        source="github",
                        external_id=url_hash("github", html_url or repo + path, target.value, length=24),
                        title=f"Code hit in {repo}",
                        haystack=" ".join(filter(None, [repo, path, target.value])),
                        url=html_url,
                        snippet=truncate(
                            f"Repository `{repo}` contains a file referencing this target.\n"
                            f"**File:** `{path}`\n"
                            f"_Content withheld to avoid amplifying the leak._",
                            400,
                        ),
                        severity=Severity.MEDIUM,
                        metadata={"repo": repo, "path": path},
                    )
                )
            await asyncio.sleep(1.2)
        return result


def build_sources(settings: LeakSettings) -> list[LeakSource]:
    sources: list[LeakSource] = [HibpSource(settings), HudsonRockSource(settings)]
    if settings.enable_ransomwatch:
        sources.append(RansomwatchSource(settings))
    if settings.enable_psbdmp:
        sources.append(PsbdmpSource(settings))
    sources.append(GitHubCodeSource(settings))
    return sources


__all__ = [
    "LeakSource",
    "SourceResult",
    "HibpSource",
    "HudsonRockSource",
    "RansomwatchSource",
    "PsbdmpSource",
    "GitHubCodeSource",
    "build_sources",
]
