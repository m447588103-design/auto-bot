from __future__ import annotations

from datetime import datetime, timezone

from core.utils import (
    chunked,
    clean_text,
    format_ts,
    human_duration,
    human_number,
    mask_secret,
    pct,
    progress_bar,
    split_message,
    truncate,
)


def test_truncate_and_clean_text():
    assert truncate("hello world", 8) == "hello w…"
    assert clean_text("<b>Hello &amp;   bye</b>") == "Hello & bye"
    assert clean_text("line\x00one\n\ntwo") == "line one\n\ntwo"


def test_human_number():
    assert human_number(1234) == "$1.23K"
    assert human_number(2_500_000) == "$2.5M"
    assert human_number(0.000123) == "$0.000123"
    assert human_number(None) == "n/a"
    assert human_number(-1500) == "-$1.5K"


def test_human_duration_and_pct():
    assert human_duration(65) == "1m 05s"
    assert human_duration(3661) == "1h 01m"
    assert human_duration(None) == "unknown"
    assert pct(2.345) == "+2.35%"
    assert pct(-1.2) == "-1.20%"
    assert pct(None) == "n/a"


def test_mask_secret():
    masked = mask_secret("john.doe@example.com")
    assert masked.startswith("jo") and "@e" in masked
    assert "john" not in masked and "example" not in masked
    assert mask_secret("+8801712345678").startswith("+8")
    assert mask_secret("") == ""


def test_split_message_respects_limit_and_content():
    text = ("word " * 600).strip()
    chunks = split_message(text, limit=500)
    assert len(chunks) > 1
    assert all(len(chunk) <= 500 for chunk in chunks)
    assert " ".join(chunks).split() == text.split()


def test_split_message_short_and_empty():
    assert split_message("hi") == ["hi"]
    assert split_message("") == []


def test_chunked_and_progress_bar():
    assert list(chunked([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert progress_bar(0, width=4) == "░░░░"
    assert progress_bar(100, width=4) == "████"
    assert progress_bar(50, width=4) == "██░░"


def test_format_ts():
    dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert format_ts(dt) == "<t:1704067200:R>"
    assert format_ts(None) == "n/a"
