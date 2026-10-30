"""Pure matching logic for leak-monitor watch targets.

Kept dependency-free so it can be unit-tested without Discord or network.
"""

from __future__ import annotations

import re
import unicodedata

from .models import TargetKind, WatchTarget

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
DOMAIN_RE = re.compile(
    r"^(?=.{4,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$"
)
PHONE_RE = re.compile(r"^\+?[0-9][0-9\s\-()]{6,18}$")
USERNAME_RE = re.compile(r"^@?[A-Za-z0-9._\-]{3,40}$")

_WS_RE = re.compile(r"\s+")
_MIN_KEYWORD_LEN = 3


class InvalidTarget(ValueError):
    """Raised when a user submits an unusable watch value."""


def normalize(text: str) -> str:
    """Casefold / NFKC-normalise and collapse whitespace."""
    if not text:
        return ""
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def infer_kind(value: str) -> TargetKind:
    value = (value or "").strip()
    if EMAIL_RE.match(value):
        return TargetKind.EMAIL
    if PHONE_RE.match(value):
        return TargetKind.PHONE
    if DOMAIN_RE.match(value):
        return TargetKind.DOMAIN
    if value.startswith("@"):
        return TargetKind.USERNAME
    return TargetKind.KEYWORD


def validate_target(value: str, kind: TargetKind) -> str:
    """Normalise + validate user input, returning the canonical value."""
    cleaned = (value or "").strip()
    if not cleaned:
        raise InvalidTarget("The watch value cannot be empty.")
    if len(cleaned) > 120:
        raise InvalidTarget("The watch value is too long (max 120 characters).")

    if kind is TargetKind.EMAIL:
        if not EMAIL_RE.match(cleaned):
            raise InvalidTarget("That does not look like a valid email address.")
        return cleaned.lower()
    if kind is TargetKind.DOMAIN:
        domain = cleaned.lower().lstrip("@").strip(".")
        if domain.startswith("http://") or domain.startswith("https://"):
            domain = domain.split("://", 1)[1]
        domain = domain.split("/", 1)[0].split(":", 1)[0]
        if not DOMAIN_RE.match(domain):
            raise InvalidTarget("That does not look like a valid domain (example: acme.com).")
        return domain
    if kind is TargetKind.PHONE:
        if not PHONE_RE.match(cleaned):
            raise InvalidTarget("Phone numbers should contain 7-19 digits (spaces, - and + allowed).")
        return cleaned
    if kind is TargetKind.USERNAME:
        username = cleaned if cleaned.startswith("@") else f"@{cleaned}"
        if not USERNAME_RE.match(username.lstrip("@")):
            raise InvalidTarget("Usernames must be 3-40 characters (letters, digits, . _ -).")
        return username.lower()
    # keyword
    if len(cleaned) < _MIN_KEYWORD_LEN:
        raise InvalidTarget(f"Keywords need at least {_MIN_KEYWORD_LEN} characters.")
    return cleaned


def _domain_pattern(domain: str) -> re.Pattern[str]:
    label = re.escape(normalize(domain))
    return re.compile(
        rf"(?<![a-z0-9.@_\-])(?:[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?\.)*{label}(?![a-z0-9\-]|\.[a-z0-9])"
    )


def _keyword_pattern(keyword: str) -> re.Pattern[str]:
    normalized = normalize(keyword)
    escaped = re.escape(normalized).replace(r"\ ", r"\s+")
    if re.fullmatch(r"[\w\s.@+\-]{3,}", normalized):
        return re.compile(rf"(?<!\w){escaped}(?!\w)")
    return re.compile(escaped)


_PATTERN_CACHE: dict[tuple[str, str], re.Pattern[str]] = {}


def _pattern_for(target: WatchTarget) -> re.Pattern[str] | None:
    key = (target.kind, normalize(target.value))
    if key in _PATTERN_CACHE:
        return _PATTERN_CACHE[key]
    if target.kind_enum is TargetKind.EMAIL:
        pattern = re.compile(rf"(?<![a-z0-9._%+\-]){re.escape(normalize(target.value))}(?![a-z0-9._%+\-])")
    elif target.kind_enum is TargetKind.DOMAIN:
        pattern = _domain_pattern(target.value)
    elif target.kind_enum is TargetKind.PHONE:
        pattern = re.compile(re.escape(target.value.replace(" ", "")))
    elif target.kind_enum is TargetKind.USERNAME:
        handle = normalize(target.value).lstrip("@")
        pattern = re.compile(rf"(?<![\w.])@?{re.escape(handle)}(?![\w.])")
    else:
        pattern = _keyword_pattern(target.value)
    _PATTERN_CACHE[key] = pattern
    return pattern


def find_match(target: WatchTarget, haystack: str) -> str | None:
    """Return the matched fragment from ``haystack`` or ``None``."""
    if not haystack:
        return None
    if target.kind_enum is TargetKind.PHONE:
        digits = re.sub(r"[^0-9+]", "", target.value)
        if digits and digits in re.sub(r"[^0-9+]", "", haystack):
            return target.value
        return None
    pattern = _pattern_for(target)
    if pattern is None:
        return None
    match = pattern.search(normalize(haystack))
    if not match:
        return None
    return match.group(0) or target.value


def match_many(targets: list[WatchTarget], haystack: str) -> list[tuple[WatchTarget, str]]:
    matches: list[tuple[WatchTarget, str]] = []
    for target in targets:
        found = find_match(target, haystack)
        if found:
            matches.append((target, found))
    return matches
