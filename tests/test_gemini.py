"""Gemini client configuration tests (no network calls)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.gemini import GeminiClient, thinking_budget_for_model


def test_thinking_is_disabled_only_for_flash_25():
    assert thinking_budget_for_model("gemini-2.5-flash") == 0
    assert thinking_budget_for_model("gemini-2.5-flash-lite") == 0
    assert thinking_budget_for_model("gemini-2.5-pro") is None
    assert thinking_budget_for_model("gemini-2.0-flash") is None
    assert thinking_budget_for_model(None) is None


async def test_25_flash_request_sets_zero_thinking_budget():
    client = GeminiClient(api_key="test-key", model="gemini-2.5-flash")
    if client.backend != "google-genai":
        pytest.skip("modern google-genai SDK not available")

    captured: dict = {}

    class FakeModels:
        async def generate_content(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(text="OK")

    client._client = SimpleNamespace(aio=SimpleNamespace(models=FakeModels()))
    text = await client._call(
        "hello",
        system_instruction=None,
        temperature=0.2,
        max_output_tokens=64,
        json_mode=False,
    )

    assert text == "OK"
    assert captured["model"] == "gemini-2.5-flash"
    config = captured["config"]
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_budget == 0
    assert captured["config"].max_output_tokens == 64
