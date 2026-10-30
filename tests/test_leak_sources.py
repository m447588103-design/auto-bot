from __future__ import annotations

from core.config import LeakSettings
from features.leak_monitor.models import Severity, TargetKind, WatchTarget
from features.leak_monitor.sources import (
    GitHubCodeSource,
    HibpSource,
    HudsonRockSource,
    PsbdmpSource,
    RansomwatchSource,
)


def leak_settings(**overrides) -> LeakSettings:
    base = dict(
        enabled=True,
        interval_seconds=900,
        min_source_interval=1800,
        alert_channel_id=None,
        hibp_api_key=None,
        github_token=None,
        enable_ransomwatch=True,
        enable_psbdmp=True,
        enable_hudsonrock=True,
        max_alerts_per_cycle=10,
        request_timeout=20.0,
        snippet_chars=400,
    )
    base.update(overrides)
    return LeakSettings(**base)


def target(value: str, kind: TargetKind) -> WatchTarget:
    return WatchTarget(id=1, guild_id=1, value=value, kind=kind.value)


def test_hibp_parsing():
    source = HibpSource(leak_settings(hibp_api_key="k"))
    payload = [
        {
            "Name": "AcmeLeak",
            "Domain": "acme.com",
            "BreachDate": "2023-05-01",
            "AddedDate": "2023-05-02T10:00:00Z",
            "Description": "Company database leaked",
            "DataClasses": ["Email addresses", "Passwords"],
            "PwnCount": 120000,
        }
    ]
    hits = source._parse(target("ceo@acme.com", TargetKind.EMAIL), payload)
    assert len(hits) == 1
    hit = hits[0]
    assert hit.source == "hibp"
    assert hit.severity is Severity.HIGH
    assert "AcmeLeak" in hit.title
    assert "ceo@acme.com" in hit.haystack
    assert hit.external_id
    assert "Passwords" in hit.snippet


def test_hibp_parsing_handles_empty_payload():
    source = HibpSource(leak_settings())
    assert source._parse(target("a@b.com", TargetKind.EMAIL), None) == []
    assert source._parse(target("a@b.com", TargetKind.EMAIL), {}) == []


def test_hudsonrock_parsing_never_exposes_credentials():
    source = HudsonRockSource(leak_settings())
    payload = {
        "message": "ok",
        "stealers": [
            {
                "computer_name": "DESKTOP-ACME",
                "operating_system": "Windows 10",
                "date_compromised": "2024-01-01",
                "malware_path": "C:/Users/ceo/AppData/redline.exe",
                "top_logins": "ceo@acme.com",
                "top_passwords": ["SuperSecret123"],
            }
        ],
    }
    hits = source._parse(target("acme.com", TargetKind.DOMAIN), payload)
    assert len(hits) == 1
    assert hits[0].severity is Severity.CRITICAL
    assert "DESKTOP-ACME" in hits[0].snippet
    assert "SuperSecret123" not in hits[0].snippet
    assert "credentials are intentionally withheld" in hits[0].snippet.lower()


def test_hudsonrock_parsing_domain_shape():
    source = HudsonRockSource(leak_settings())
    payload = {"employees": [{"computer_name": "LAPTOP-1", "operating_system": "macOS"}]}
    assert len(source._parse(target("acme.com", TargetKind.DOMAIN), payload)) == 1
    assert source._parse(target("acme.com", TargetKind.DOMAIN), {"message": "not found"}) == []


def test_source_availability():
    assert HibpSource(leak_settings()).available is False
    assert HibpSource(leak_settings(hibp_api_key="x")).available is True
    assert GitHubCodeSource(leak_settings()).available is False
    assert GitHubCodeSource(leak_settings(github_token="x")).available is True
    assert RansomwatchSource(leak_settings()).available is True
    assert PsbdmpSource(leak_settings()).available is True
