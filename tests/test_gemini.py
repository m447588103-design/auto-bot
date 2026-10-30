"""Gemini client configuration tests (no network calls)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.gemini import (
    GeminiClient,
    thinking_budget_for_model,
    thinking_level_for_model,
)


def test_thinking_controls_are_model_specific():
    assert thinking_budget_for_model("gemini-2.5-flash") == 0
    assert thinking_level_for_model("gemini-2.5-flash") is None
    assert thinking_budget_for_model("gemini-3.8-flash") is None
    assert thinking_level_for_model("gemini-3.8-flash") == "low"
    assert thinking_level_for_model("gemini-3.8-flash-preview") == "low"
    assert thinking_level_for_model("gemini-3.8-flash-lite") is None
    assert thinking_level_for_model("gemini-3.8-pro") is None
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
    assert config.temperature == 0.2
    assert config.max_output_tokens == 64


async def test_38_flash_uses_low_thinking_and_omits_temperature():
    client = GeminiClient(api_key="test-key", model="gemini-3.8-flash")
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
        temperature=0.35,
        max_output_tokens=400,
        json_mode=False,
    )

    assert text == "OK"
    config = captured["config"]
    assert config.thinking_config is not None
    level = getattr(config.thinking_config.thinking_level, "value", config.thinking_config.thinking_level)
    assert level == "LOW"
    assert config.thinking_config.thinking_budget is None
    assert config.temperature is None
    assert config.max_output_tokens == 400
