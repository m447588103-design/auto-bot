"""Google Gemini client used by all four features.

Supports the modern ``google-genai`` SDK and falls back to the legacy
``google-generativeai`` package when only that one is installed. Every feature
treats Gemini as optional: when no key is configured
:class:`GeminiUnavailable` is raised and callers use their local fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from .config import settings

log = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)
_RETRYABLE_MARKERS = ("429", "500", "503", "deadline", "timeout", "overloaded", "unavailable", "rate limit")


class GeminiError(RuntimeError):
    """Gemini call failed."""


class GeminiUnavailable(GeminiError):
    """No API key / SDK available."""


@dataclass(frozen=True)
class GeminiReply:
    text: str
    model: str


def extract_json(text: str) -> Any:
    """Best-effort extraction of a JSON object/array from a model reply."""
    if not text or not text.strip():
        raise GeminiError("Gemini returned an empty response")
    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    starts = [i for i in (cleaned.find("{"), cleaned.find("[")) if i >= 0]
    if not starts:
        raise GeminiError("Gemini response did not contain JSON")
    start = min(starts)
    end_obj = cleaned.rfind("}")
    end_arr = cleaned.rfind("]")
    end = max(end_obj, end_arr)
    if end <= start:
        raise GeminiError("Gemini response contained truncated JSON")
    candidate = cleaned[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        # tolerate trailing commas, a common model slip
        repaired = re.sub(r",\s*([}\]])", r"\1", candidate)
        try:
            return json.loads(repaired)
        except json.JSONDecodeError as exc:
            raise GeminiError(f"Could not parse Gemini JSON: {exc}") from exc


def _is_retryable(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(marker in message for marker in _RETRYABLE_MARKERS)


class GeminiClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_retries: int = 2,
    ):
        self.api_key = api_key if api_key is not None else settings.gemini.api_key
        self.model = model or settings.gemini.model
        self.temperature = settings.gemini.temperature if temperature is None else temperature
        self.max_retries = max_retries
        self._backend: str | None = None
        self._client: Any = None
        self._legacy_model: Any = None
        if self.api_key:
            self._init_backend()

    # -- setup -------------------------------------------------------------
    def _init_backend(self) -> None:
        try:  # modern SDK
            from google import genai  # type: ignore

            self._client = genai.Client(api_key=self.api_key)
            self._backend = "google-genai"
            return
        except Exception as exc:  # pragma: no cover - depends on env
            log.debug("google-genai unavailable: %s", exc)

        try:  # legacy SDK
            import google.generativeai as legacy  # type: ignore

            legacy.configure(api_key=self.api_key)
            self._legacy_model = legacy.GenerativeModel(self.model)
            self._backend = "google-generativeai"
        except Exception as exc:  # pragma: no cover
            log.warning("No Gemini SDK available: %s", exc)
            self._backend = None

    @property
    def available(self) -> bool:
        return bool(self.api_key and self._backend)

    @property
    def backend(self) -> str | None:
        return self._backend

    # -- generation --------------------------------------------------------
    async def generate(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        json_mode: bool = False,
    ) -> GeminiReply:
        if not self.available:
            raise GeminiUnavailable(
                "Gemini is not configured. Add GEMINI_API_KEY to your .env file."
            )
        temp = self.temperature if temperature is None else temperature
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                text = await self._call(
                    prompt,
                    system_instruction=system_instruction,
                    temperature=temp,
                    max_output_tokens=max_output_tokens,
                    json_mode=json_mode,
                )
                if not text or not text.strip():
                    raise GeminiError("Gemini returned an empty response (possibly safety-filtered)")
                return GeminiReply(text=text.strip(), model=self.model)
            except GeminiUnavailable:
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= self.max_retries or not _is_retryable(exc):
                    break
                delay = 1.5 * (2**attempt)
                log.warning("Gemini retry %s/%s in %.1fs: %s", attempt + 1, self.max_retries, delay, exc)
                await asyncio.sleep(delay)
        raise GeminiError(f"Gemini request failed: {last_error}")

    async def _call(
        self,
        prompt: str,
        *,
        system_instruction: str | None,
        temperature: float,
        max_output_tokens: int | None,
        json_mode: bool,
    ) -> str:
        if self._backend == "google-genai":
            from google.genai import types  # type: ignore

            config_kwargs: dict[str, Any] = {"temperature": temperature}
            if system_instruction:
                config_kwargs["system_instruction"] = system_instruction
            if max_output_tokens:
                config_kwargs["max_output_tokens"] = max_output_tokens
            if json_mode:
                config_kwargs["response_mime_type"] = "application/json"
            response = await self._client.aio.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(**config_kwargs),
            )
            return getattr(response, "text", None) or ""

        # legacy SDK is synchronous -> run in a worker thread
        def _legacy_call() -> str:
            generation_config: dict[str, Any] = {"temperature": temperature}
            if max_output_tokens:
                generation_config["max_output_tokens"] = max_output_tokens
            if json_mode:
                generation_config["response_mime_type"] = "application/json"
            full_prompt = (
                f"{system_instruction}\n\n{prompt}" if system_instruction else prompt
            )
            result = self._legacy_model.generate_content(
                full_prompt, generation_config=generation_config
            )
            return getattr(result, "text", "") or ""

        return await asyncio.to_thread(_legacy_call)

    async def generate_json(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
    ) -> Any:
        reply = await self.generate(
            prompt,
            system_instruction=system_instruction,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            json_mode=True,
        )
        return extract_json(reply.text)


_client: GeminiClient | None = None


def get_gemini() -> GeminiClient:
    global _client
    if _client is None:
        _client = GeminiClient()
    return _client
