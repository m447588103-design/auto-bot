"""Turn a one-line theme into a structured, renderable movie script."""

from __future__ import annotations

import logging
import re
from typing import Any

from core.config import CinematicSettings
from core.gemini import GeminiClient, GeminiError, GeminiUnavailable
from core.utils import clean_text, truncate

from .models import MovieScript, Scene

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a viral short-form video director (YouTube Shorts / Instagram Reels). "
    "You write tight, cinematic 30-60 second stories with a strong hook, emotional beats "
    "and a memorable ending. You always answer with pure JSON only."
)

USER_PROMPT = """Create a vertical short-film script about: "{theme}"

Requirements:
- 4 to {max_scenes} scenes, each 3-9 seconds of narration.
- Total narration must stay under {max_seconds} seconds when spoken (~15 characters/second).
- Write narration in the SAME language as the theme: if the theme is Bangla or Banglish,
  write natural conversational Bangla; otherwise write English.
- visual_query must be 2-5 ENGLISH words describing a concrete, filmable shot
  (good stock-footage search terms), e.g. "astronaut walking red desert".
- The hook is the first sentence and must grab attention instantly.
- No scene should mention camera directions, watermarks, or "AI".
- Hashtags: 5-8 short, relevant, no spaces.

Return JSON exactly like:
{{
  "title": "short cinematic title (max 60 chars)",
  "logline": "one sentence summary",
  "hook": "first spoken sentence",
  "music_mood": "e.g. emotional cinematic piano",
  "cta": "one short closing call to action",
  "hashtags": ["shorts", "cinematic"],
  "scenes": [
    {{"narration": "spoken words for scene 1", "visual_query": "english stock search", "on_screen_text": "2-5 word caption or null"}}
  ]
}}
"""


class ScriptError(RuntimeError):
    pass


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def script_from_dict(data: Any, *, theme: str, settings: CinematicSettings) -> MovieScript:
    """Validate and normalise a raw model payload into a :class:`MovieScript`."""
    if not isinstance(data, dict):
        raise ScriptError("Script payload was not a JSON object")

    scenes_raw = data.get("scenes")
    if not isinstance(scenes_raw, list) or not scenes_raw:
        raise ScriptError("Script payload is missing scenes")

    scenes: list[Scene] = []
    for raw in scenes_raw:
        if not isinstance(raw, dict):
            continue
        narration = clean_text(raw.get("narration") or raw.get("text"), limit=600)
        if len(narration) < 6:
            continue
        visual = clean_text(raw.get("visual_query") or raw.get("visual"), limit=80)
        if not visual:
            visual = _fallback_query(theme, narration)
        on_screen = clean_text(raw.get("on_screen_text"), limit=60) or None
        hint = _as_float(raw.get("duration"), max(3.0, min(9.0, len(narration) / 15.0)))
        scenes.append(
            Scene(
                index=len(scenes) + 1,
                narration=narration,
                visual_query=visual,
                duration_hint=max(2.5, min(12.0, hint)),
                on_screen_text=on_screen,
            )
        )

    if len(scenes) < 2:
        raise ScriptError("Gemini returned fewer than two usable scenes")

    max_scenes = max(2, settings.max_scenes)
    scenes = scenes[:max_scenes]

    # Trim trailing scenes until the estimated duration fits the budget.
    budget = float(settings.max_duration_seconds)
    while len(scenes) > 2 and sum(s.estimated_seconds for s in scenes) > budget:
        scenes.pop()
    for index, scene in enumerate(scenes, start=1):
        scene.index = index

    title = clean_text(data.get("title"), limit=70) or truncate(theme, 60)
    logline = clean_text(data.get("logline") or data.get("summary"), limit=300)
    music_mood = clean_text(data.get("music_mood"), limit=80) or "emotional cinematic"
    cta = clean_text(data.get("cta"), limit=120) or "Follow for more."
    hashtags = data.get("hashtags") or []
    if isinstance(hashtags, str):
        hashtags = hashtags.split()
    hashtags = [str(tag) for tag in hashtags if tag][:10]

    hook = clean_text(data.get("hook"), limit=200)
    if hook and not scenes[0].narration.lower().startswith(hook[:20].lower()):
        scenes[0].narration = f"{hook} {scenes[0].narration}"
        scenes[0].narration = clean_text(scenes[0].narration, limit=600)

    return MovieScript(
        theme=theme,
        title=title,
        logline=logline,
        scenes=scenes,
        music_mood=music_mood,
        hashtags=hashtags,
        cta=cta,
        demo=False,
    )


def _fallback_query(theme: str, narration: str) -> str:
    words = re.sub(r"[^A-Za-z0-9 ]+", " ", theme).split()
    base = " ".join(words[:4]) or "cinematic landscape"
    return truncate(f"{base} cinematic", 60)


def fallback_script(theme: str, settings: CinematicSettings) -> MovieScript:
    """Deterministic demo script used when Gemini is unavailable.

    It still produces a valid reel so the rendering pipeline can be tested
    without an API key.
    """
    subject = truncate(clean_text(theme, limit=80) or "an untold story", 80)
    subject_lower = subject[0].lower() + subject[1:] if subject else subject
    beats = [
        f"Somewhere beyond the noise, {subject_lower} is waiting to be seen.",
        "Every journey begins with a single, quiet decision.",
        "The world pushes back, and still the heart keeps moving forward.",
        "In the end, the smallest light is the one that changes everything.",
    ]
    queries = [
        f"{_fallback_query(theme, '')} wide shot",
        "cinematic slow motion light",
        "person walking golden hour",
        "stars night sky timelapse",
    ]
    scenes = [
        Scene(
            index=index + 1,
            narration=beat,
            visual_query=queries[index % len(queries)],
            duration_hint=max(3.0, len(beat) / 15.0),
        )
        for index, beat in enumerate(beats)
    ]
    while len(scenes) > 2 and sum(s.estimated_seconds for s in scenes) > settings.max_duration_seconds:
        scenes.pop()
    return MovieScript(
        theme=theme,
        title=truncate(subject, 60).title(),
        logline=f"A cinematic short about {subject_lower}.",
        scenes=scenes,
        music_mood="emotional cinematic piano",
        hashtags=["shorts", "cinematic", "story", "aivideo", "reels"],
        cta="Follow for more stories.",
        demo=True,
    )


async def write_script(
    theme: str,
    *,
    settings: CinematicSettings,
    gemini: GeminiClient | None = None,
    temperature: float = 0.95,
) -> MovieScript:
    """Ask Gemini for a script, falling back to a local template if allowed."""
    theme = clean_text(theme, limit=400)
    if len(theme) < 4:
        raise ScriptError("Please describe your idea with at least a few words.")

    if gemini is None or not gemini.available:
        if settings.allow_demo_scripts:
            log.warning("Gemini unavailable - using demo script template")
            return fallback_script(theme, settings)
        raise GeminiUnavailable("GEMINI_API_KEY is not configured")

    prompt = USER_PROMPT.format(
        theme=theme, max_scenes=max(2, settings.max_scenes), max_seconds=settings.max_duration_seconds
    )
    try:
        data = await gemini.generate_json(
            prompt, system_instruction=SYSTEM_PROMPT, temperature=temperature, max_output_tokens=4096
        )
        return script_from_dict(data, theme=theme, settings=settings)
    except (GeminiError, ScriptError) as exc:
        log.warning("Gemini script generation failed: %s", exc)
        if settings.allow_demo_scripts:
            return fallback_script(theme, settings)
        raise ScriptError(f"Could not generate a script: {exc}") from exc
