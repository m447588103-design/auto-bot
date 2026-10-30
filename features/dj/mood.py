"""Mood understanding for the AI DJ.

The rule engine is intentionally bilingual: it understands English, Bangla and
Banglish (romanised Bangla), so messages like *"bhai mon bhalo na, lofi bajao"*
work even without Gemini.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from core.gemini import GeminiClient, GeminiError
from core.utils import clamp

BENGALI_RE = re.compile(r"[\u0980-\u09FF]")


@dataclass
class MoodPlan:
    mood: str
    energy: float
    genres: list[str]
    queries: list[str]
    avoid: list[str] = field(default_factory=list)
    reason: str = ""
    source: str = "rules"

    def all_queries(self) -> list[str]:
        out: list[str] = []
        for query in self.queries:
            if query and query not in out:
                out.append(query)
        return out


@dataclass(frozen=True)
class Rule:
    name: str
    keywords: tuple[str, ...]
    energy: float
    genres: tuple[str, ...]
    queries: tuple[str, ...]
    avoid: tuple[str, ...] = ()


# Order matters: earlier rules win ties.
RULES: tuple[Rule, ...] = (
    Rule(
        "sad & healing",
        (
            "sad", "depressed", "depressing", "heartbroken", "heartbreak", "broken heart",
            "cry", "crying", "lonely", "alone", "emo", "mon bhalo na", "mon kharap",
            "kharap lagche", "kosto", "koshto", "ekla", "vishon", "dukhi", "mon chhilo",
            "কষ্ট", "মন খারাপ", "একা", "কাঁদ", "দুঃখ", "ভীষণ",
        ),
        0.22,
        ("lofi", "sad acoustic", "soft indie", "slowed reverb"),
        (
            "lofi sad chill mix",
            "sad acoustic songs playlist",
            "soft indie heartbreak songs",
            "slowed reverb sad songs",
        ),
        ("edm", "party", "hardstyle"),
    ),
    Rule(
        "heartbreak romantic",
        (
            "breakup", "prem", "premi", "valobasha", "bhalobasha", "love", "crush",
            "romantic", "romance", "miss kori", "miss karci", "ভালোবাসা", "প্রেম", "বিরহ",
        ),
        0.35,
        ("romantic", "bangla romantic", "hindi romantic", "acoustic love"),
        (
            "romantic love songs playlist",
            "bangla romantic songs",
            "acoustic love songs",
        ),
    ),
    Rule(
        "hype & party",
        (
            "party", "hype", "turn up", "dance", "club", "rave", "celebrate", "birthday",
            "mast", "moja", "dhamaka", "পার্টি", "মজা", "নাচ",
        ),
        0.95,
        ("edm", "dance pop", "hip hop", "afrobeats"),
        (
            "party hype playlist",
            "edm festival bangers",
            "dance pop hits",
            "hip hop party mix",
        ),
        ("sad", "lofi"),
    ),
    Rule(
        "focus / coding",
        (
            "coding", "code", "programming", "focus", "study", "pora", "porasona", "exam",
            "work", "deadline", "concentrate", "deep work", "পড়া", "পড়াশোনা", "মনোযোগ",
        ),
        0.45,
        ("lofi beats", "instrumental", "synthwave", "ambient"),
        (
            "lofi coding beats",
            "instrumental focus music",
            "synthwave programming mix",
            "deep work ambient playlist",
        ),
        ("vocals", "party"),
    ),
    Rule(
        "workout",
        (
            "gym", "workout", "training", "lift", "run", "running", "cardio", "energy",
            "motivation", "জিম", "ব্যায়াম", "দৌড়",
        ),
        0.92,
        ("phonk", "trap", "hard rock", "drum and bass"),
        (
            "gym phonk workout mix",
            "trap workout motivation",
            "hard rock training playlist",
            "drum and bass running mix",
        ),
    ),
    Rule(
        "sleep & calm",
        (
            "sleep", "insomnia", "ghoom", "ghum", "raat", "tired", "relax", "calm",
            "peace", "meditate", "ambient", "ঘুম", "রাত", "ক্লান্ত", "শান্ত",
        ),
        0.12,
        ("ambient", "sleep piano", "rain sounds", "soft lofi"),
        (
            "sleep ambient music",
            "calm piano for sleep",
            "rain sounds for sleeping",
            "soft lofi sleep mix",
        ),
        ("hype", "edm"),
    ),
    Rule(
        "rainy day",
        (
            "rain", "rainy", "bristi", "brshti", "monsoon", "cloudy", "বৃষ্টি", "মেঘ",
        ),
        0.3,
        ("lofi", "chillhop", "acoustic", "jazzhop"),
        (
            "rainy day lofi mix",
            "chillhop rainy playlist",
            "acoustic rainy day songs",
            "jazzhop chill mix",
        ),
    ),
    Rule(
        "nostalgic",
        (
            "nostalgia", "nostalgic", "old songs", "purano", "childhood", "memory",
            "memories", "purano gaan", "পুরানো", "স্মৃতি", "নস্টালজিয়া",
        ),
        0.4,
        ("retro", "90s", "bangla classics", "old hindi"),
        (
            "nostalgic 90s hits",
            "purano bangla gaan",
            "old hindi classics playlist",
            "retro throwback songs",
        ),
    ),
    Rule(
        "angry / release",
        (
            "angry", "rage", "rag", "frustrated", "hatred", "raag", "রাগ", "বিরক্ত",
        ),
        0.85,
        ("metal", "punk", "hard rock", "rap"),
        (
            "metal rage playlist",
            "punk rock angry songs",
            "hard rap aggressive",
            "hard rock release mix",
        ),
    ),
    Rule(
        "hyperpop",
        (
            "hyperpop", "glitchcore", "hyper pop", "100 gecs", "পপ",
        ),
        0.9,
        ("hyperpop", "glitchcore", "digicore"),
        (
            "hyperpop playlist",
            "glitchcore mix",
            "digicore hyperpop songs",
        ),
    ),
    Rule(
        "chill",
        (
            "chill", "vibe", "relaxed", "aaram", "aram", "chill vibe", "chill korbo",
            "আরাম", "চিল",
        ),
        0.35,
        ("chill pop", "r&b", "indie", "lofi"),
        (
            "chill vibes playlist",
            "r&b chill mix",
            "indie chill songs",
            "lofi chill beats",
        ),
    ),
)

GENRE_HINTS: dict[str, tuple[str, str]] = {
    "lofi": ("lofi", "lofi hip hop mix"),
    "lo-fi": ("lofi", "lofi hip hop mix"),
    "hyperpop": ("hyperpop", "hyperpop playlist"),
    "phonk": ("phonk", "phonk mix"),
    "rap": ("rap", "rap hits playlist"),
    "hip hop": ("hip hop", "hip hop hits"),
    "hiphop": ("hip hop", "hip hop hits"),
    "rock": ("rock", "rock anthems"),
    "metal": ("metal", "metal playlist"),
    "classical": ("classical", "classical music"),
    "jazz": ("jazz", "jazz playlist"),
    "edm": ("edm", "edm mix"),
    "k-pop": ("k-pop", "kpop hits"),
    "kpop": ("k-pop", "kpop hits"),
    "punjabi": ("punjabi", "punjabi songs"),
    "bangla": ("bangla", "bangla songs"),
    "bangladeshi": ("bangla", "bangla songs"),
    "hindi": ("hindi", "hindi songs"),
    "bollywood": ("bollywood", "bollywood hits"),
    "english": ("english", "english hits"),
    "instrumental": ("instrumental", "instrumental music"),
    "piano": ("piano", "piano music"),
    "synthwave": ("synthwave", "synthwave mix"),
    "afrobeats": ("afrobeats", "afrobeats hits"),
    "trap": ("trap", "trap music"),
    "indie": ("indie", "indie playlist"),
    "আধুনিক": ("bangla", "bangla modern songs"),
    "বাংলা": ("bangla", "bangla songs"),
    "হিন্দি": ("hindi", "hindi songs"),
}

DEFAULT_PLAN = MoodPlan(
    mood="surprise mix",
    energy=0.5,
    genres=["mixed hits"],
    queries=["trending music mix", "feel good songs playlist", "best hits playlist"],
    reason="No strong mood detected, so here is a varied mix.",
)

BANGLA_HINT_WORDS = ("gan", "gaan", "song", "music", "bajao", "baja", "play", "chalao", "গান", "বাজাও", "চালাও")


def _contains(text: str, keyword: str) -> bool:
    keyword = keyword.lower()
    if BENGALI_RE.search(keyword):
        return keyword in text
    if " " in keyword:
        return keyword in text
    return re.search(rf"(?<![a-z]){re.escape(keyword)}(?![a-z])", text) is not None


def _detect_genres(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for hint, (genre, query) in GENRE_HINTS.items():
        if hint in text and (genre, query) not in found:
            found.append((genre, query))
    return found


def plan_mood_rules(text: str) -> MoodPlan:
    """Keyword based mood detection (works offline, no API key)."""
    normalized = (text or "").lower().strip()
    if not normalized:
        return MoodPlan(**{**DEFAULT_PLAN.__dict__, "queries": list(DEFAULT_PLAN.queries)})

    best: tuple[int, Rule] | None = None
    for rule in RULES:
        score = sum(1 for keyword in rule.keywords if _contains(normalized, keyword))
        if score and (best is None or score > best[0]):
            best = (score, rule)

    genre_hits = _detect_genres(normalized)

    if best is None and not genre_hits:
        # Free-form request: search the user's own words
        query = re.sub(r"\s+", " ", text).strip()[:120]
        return MoodPlan(
            mood="custom vibe",
            energy=0.5,
            genres=["custom"],
            queries=[f"{query} music", f"{query} playlist", "chill music mix"],
            reason=f"Playing something based on: “{query}”",
        )

    rule = best[1] if best else None
    mood = rule.name if rule else "custom vibe"
    energy = rule.energy if rule else 0.5
    genres = list(rule.genres) if rule else []
    queries = list(rule.queries) if rule else []
    avoid = list(rule.avoid) if rule else []

    for genre, query in genre_hits:
        if genre not in genres:
            genres.insert(0, genre)
        if query not in queries:
            queries.insert(0, query)
        if genre in ("hyperpop", "phonk", "rap", "metal", "rock", "edm", "k-pop", "bangla", "hindi"):
            energy = max(energy, 0.7)

    plan = MoodPlan(
        mood=mood,
        energy=clamp(energy, 0.0, 1.0),
        genres=genres[:4],
        queries=queries[:5],
        avoid=avoid,
        reason=(
            f"Detected **{mood}** vibes from your message."
            + (f" Pinning genre: **{', '.join(g for g, _ in genre_hits)}**." if genre_hits else "")
        ),
    )
    return plan


AI_SYSTEM = (
    "You are a world-class music supervisor for a Discord DJ bot. "
    "You read free-form messages (English, Bangla or Banglish) and choose music. "
    "Answer with pure JSON only."
)

AI_PROMPT = """The user says: "{text}"

Pick music that genuinely fits their mood. Prefer concrete, searchable English
queries that work on YouTube. If they mention Bangla/Hindi/Punjabi, include some
songs in that language.

Return JSON:
{{
  "mood": "two or three words",
  "energy": 0.0-1.0,
  "genres": ["3-4 genres"],
  "queries": ["3-5 youtube search queries, no quotes"],
  "avoid": ["genres to avoid"],
  "reason": "one short sentence in the user's language"
}}
Do not include artist copyright issues, just search queries.
"""


def _validate_ai_plan(data: dict, fallback: MoodPlan) -> MoodPlan:
    if not isinstance(data, dict):
        raise ValueError("not a dict")
    queries = [str(q).strip() for q in data.get("queries") or [] if str(q).strip()]
    if not queries:
        raise ValueError("no queries")
    genres = [str(g).strip() for g in data.get("genres") or [] if str(g).strip()][:4]
    try:
        energy = float(data.get("energy", 0.5))
    except (TypeError, ValueError):
        energy = 0.5
    return MoodPlan(
        mood=str(data.get("mood") or fallback.mood)[:60],
        energy=clamp(energy, 0.0, 1.0),
        genres=genres or fallback.genres,
        queries=queries[:5],
        avoid=[str(a) for a in data.get("avoid") or []][:4],
        reason=str(data.get("reason") or fallback.reason)[:200],
        source="gemini",
    )


async def plan_mood(
    text: str, gemini: GeminiClient | None = None, *, use_ai: bool = True
) -> MoodPlan:
    """AI mood plan with a deterministic rule-based fallback."""
    fallback = plan_mood_rules(text)
    if not use_ai or gemini is None or not gemini.available:
        return fallback
    try:
        data = await gemini.generate_json(
            AI_PROMPT.format(text=text[:400]),
            system_instruction=AI_SYSTEM,
            temperature=0.8,
            max_output_tokens=700,
        )
        plan = _validate_ai_plan(data, fallback)
        # Keep explicit genre pins from the raw text (e.g. "hyperpop").
        for genre, query in _detect_genres(text.lower()):
            if genre not in plan.genres:
                plan.genres.insert(0, genre)
            if query not in plan.queries:
                plan.queries.insert(0, query)
        return plan
    except (GeminiError, ValueError) as exc:
        fallback.reason += f"\n(AI was unavailable: {exc})"
        return fallback


def shuffle_queries(plan: MoodPlan, k: int = 2) -> list[str]:
    queries = plan.all_queries()
    if not queries:
        return []
    random.shuffle(queries)
    return queries[:k]
