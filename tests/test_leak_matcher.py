from __future__ import annotations

import pytest

from features.leak_monitor.matcher import (
    InvalidTarget,
    find_match,
    infer_kind,
    match_many,
    normalize,
    validate_target,
)
from features.leak_monitor.models import TargetKind, WatchTarget


def target(value: str, kind: TargetKind = TargetKind.KEYWORD, target_id: int = 1) -> WatchTarget:
    return WatchTarget(id=target_id, guild_id=1, value=value, kind=kind.value)


def test_infer_kind():
    assert infer_kind("analyst@example.com") is TargetKind.EMAIL
    assert infer_kind("Example.COM") is TargetKind.DOMAIN
    assert infer_kind("@some_user") is TargetKind.USERNAME
    assert infer_kind("+880 1712-345678") is TargetKind.PHONE
    assert infer_kind("some company name") is TargetKind.KEYWORD


def test_validate_target_normalises_domains_and_emails():
    assert validate_target("  ACHE.com/path ", TargetKind.DOMAIN) == "ache.com"
    assert validate_target("https://www.Acme.com/login", TargetKind.DOMAIN) == "www.acme.com"
    assert validate_target("User@Example.com", TargetKind.EMAIL) == "user@example.com"
    assert validate_target("user", TargetKind.USERNAME) == "@user"


@pytest.mark.parametrize(
    "value,kind",
    [
        ("not-an-email", TargetKind.EMAIL),
        ("nodot", TargetKind.DOMAIN),
        ("ab", TargetKind.KEYWORD),
        ("a!b@c", TargetKind.EMAIL),
        ("", TargetKind.KEYWORD),
    ],
)
def test_validate_target_rejects_bad_input(value, kind):
    with pytest.raises(InvalidTarget):
        validate_target(value, kind)


def test_domain_matches_subdomains_but_not_lookalikes():
    t = target("acme.com", TargetKind.DOMAIN)
    assert find_match(t, "login.acme.com is down")
    assert find_match(t, "mail from ACME.COM")
    assert find_match(t, "https://acme.com/leak")
    assert not find_match(t, "notacme.com")
    assert not find_match(t, "acme.com.evil.net")
    assert not find_match(t, "acme.co")


def test_email_and_keyword_matching():
    email = target("ceo@acme.com", TargetKind.EMAIL)
    assert find_match(email, "dump contains CEO@acme.com:password")
    assert not find_match(email, "dump contains ceo@acme.com.br")

    keyword = target("Project Falcon", TargetKind.KEYWORD)
    assert find_match(keyword, "leaked docs for project   falcon internal")
    assert not find_match(keyword, "project falconry")

    short = target("ACME-2024", TargetKind.KEYWORD)
    assert find_match(short, "archive: acme-2024.zip")
    assert not find_match(short, "acme-20241")


def test_phone_matching_ignores_formatting():
    phone = target("+8801712345678", TargetKind.PHONE)
    assert find_match(phone, "call +880 1712-345678 now")
    assert not find_match(phone, "call +880 9999-999999")


def test_username_matching():
    user = target("@dark_trader", TargetKind.USERNAME)
    assert find_match(user, "contact dark_trader on telegram")
    assert not find_match(user, "dark_traders")


def test_match_many_returns_all_hits():
    targets = [
        target("acme.com", TargetKind.DOMAIN, 1),
        target("ceo@acme.com", TargetKind.EMAIL, 2),
        target("globex", TargetKind.KEYWORD, 3),
    ]
    matches = match_many(targets, "ceo@acme.com works at acme.com, not globex")
    assert {match[0].id for match in matches} == {1, 2, 3}


def test_normalize_bangla_and_whitespace():
    assert normalize("  প্রজেক্ট   ফ্যালকন ") == "প্রজেক্ট ফ্যালকন"
