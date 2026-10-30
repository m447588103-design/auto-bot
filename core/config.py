"""Environment driven configuration for AutoBot.

Every setting is read from the environment (``.env`` is loaded automatically)
and has a safe default so that the bot can boot even when optional API keys are
missing. Missing keys disable the individual integration instead of crashing
the whole process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


# ---------------------------------------------------------------------------
# primitive readers
# ---------------------------------------------------------------------------
def _str(name: str, default: str | None = None) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw or default


def _int(name: str, default: int, minimum: int | None = None) -> int:
    raw = _str(name)
    if raw is None:
        return default
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return minimum
    return value


def _float(name: str, default: float, minimum: float | None = None) -> float:
    raw = _str(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return minimum
    return value


def _bool(name: str, default: bool = False) -> bool:
    raw = _str(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}


def _id(name: str) -> int | None:
    raw = _str(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _ids(name: str) -> tuple[int, ...]:
    raw = _str(name)
    if raw is None:
        return ()
    out: list[int] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.isdigit():
            out.append(int(chunk))
    return tuple(out)


def _csv(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = _str(name)
    if raw is None:
        return default
    items = tuple(part.strip() for part in raw.split(",") if part.strip())
    return items or default


def _path(name: str, default: str, base: Path | None = None) -> Path:
    raw = _str(name, default) or default
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (base or PROJECT_ROOT) / path
    return path


# ---------------------------------------------------------------------------
# feature configs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DiscordSettings:
    token: str | None
    command_prefix: str
    dev_guild_ids: tuple[int, ...]
    default_alert_channel_id: int | None
    enable_members_intent: bool
    max_upload_mb: int


@dataclass(frozen=True)
class GeminiSettings:
    api_key: str | None
    model: str
    temperature: float

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class LeakSettings:
    enabled: bool
    interval_seconds: int
    min_source_interval: int
    alert_channel_id: int | None
    hibp_api_key: str | None
    github_token: str | None
    enable_ransomwatch: bool
    enable_psbdmp: bool
    enable_hudsonrock: bool
    max_alerts_per_cycle: int
    request_timeout: float
    snippet_chars: int

    @property
    def hibp_configured(self) -> bool:
        return bool(self.hibp_api_key)


@dataclass(frozen=True)
class CinematicSettings:
    enabled: bool
    pexels_api_key: str | None
    workdir: Path
    music_dir: Path
    ffmpeg_bin: str
    ffprobe_bin: str
    tts_voices: tuple[str, ...]
    max_duration_seconds: int
    max_upload_mb: int
    max_scenes: int
    keep_jobs: int
    allow_demo_scripts: bool
    caption_font: str
    caption_size: int
    youtube_client_secret: Path | None
    youtube_token_file: Path | None

    @property
    def pexels_configured(self) -> bool:
        return bool(self.pexels_api_key)

    @property
    def youtube_upload_configured(self) -> bool:
        return bool(self.youtube_client_secret and self.youtube_token_file)


@dataclass(frozen=True)
class DJSettings:
    enabled: bool
    default_volume: float
    max_queue: int
    autoplay: bool
    spotify_client_id: str | None
    spotify_client_secret: str | None
    ytdlp_cookies_file: Path | None
    ffmpeg_bin: str
    idle_disconnect_seconds: int

    @property
    def spotify_configured(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)


@dataclass(frozen=True)
class SniperSettings:
    enabled: bool
    alert_interval_seconds: int
    alert_channel_id: int | None
    coingecko_api_key: str | None
    x_bearer_token: str | None
    finnhub_api_key: str | None
    subreddits: tuple[str, ...]
    cache_ttl_seconds: int
    request_timeout: float


@dataclass(frozen=True)
class GameSettings:
    enabled: bool
    session_ttl_minutes: int
    max_active_sessions: int
    rpg_cooldown_seconds: float
    interrogation_cooldown_seconds: float
    boss_cooldown_seconds: float
    boss_base_hp: int
    max_interrogations: int
    ai_narration: bool


@dataclass(frozen=True)
class CompanionSettings:
    enabled: bool
    gf_enabled: bool
    ask_enabled: bool
    gf_name: str
    gf_persona: str
    gf_cooldown_seconds: float
    ask_cooldown_seconds: float
    gf_history_turns: int
    gf_max_message_chars: int
    ask_max_question_chars: int
    ask_max_output_tokens: int
    memory_ttl_days: int


@dataclass(frozen=True)
class Settings:
    discord: DiscordSettings
    gemini: GeminiSettings
    leak: LeakSettings
    cinematic: CinematicSettings
    dj: DJSettings
    sniper: SniperSettings
    games: GameSettings
    companion: CompanionSettings
    data_dir: Path
    db_path: Path
    log_level: str
    environment: str

    # -- helpers -----------------------------------------------------------
    def ensure_dirs(self) -> None:
        """Create writable directories used by the bot."""
        for path in (self.data_dir, self.cinematic.workdir, self.data_dir / "logs"):
            path.mkdir(parents=True, exist_ok=True)

    def summary(self) -> dict[str, object]:
        """Non-secret overview used by the /doctor command."""
        return {
            "environment": self.environment,
            "data_dir": str(self.data_dir),
            "gemini": "configured" if self.gemini.configured else "missing GEMINI_API_KEY",
            "leak_monitor": (
                "enabled" if self.leak.enabled else "disabled"
            ),
            "hibp": "configured" if self.leak.hibp_configured else "missing HIBP_API_KEY (HIBP scans skipped)",
            "pexels": (
                "configured" if self.cinematic.pexels_configured else "missing PEXELS_API_KEY (using picsum fallback)"
            ),
            "spotify": "configured" if self.dj.spotify_configured else "not configured (YouTube only)",
            "reddit": "enabled" if self.sniper.subreddits else "disabled",
            "x_twitter": "configured" if self.sniper.x_bearer_token else "not configured (X sentiment skipped)",
            "stocks": "configured" if self.sniper.finnhub_api_key else "not configured (add FINNHUB_API_KEY)",
            "youtube_upload": (
                "configured" if self.cinematic.youtube_upload_configured else "not configured"
            ),
            "games": "enabled" if self.games.enabled else "disabled",
            "companion": "enabled" if self.companion.enabled else "disabled",
            "girlfriend": "enabled" if self.companion.gf_enabled else "disabled",
            "smart_qa": "enabled" if self.companion.ask_enabled else "disabled",
        }

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = _path("DATA_DIR", "data")
        return cls(
            discord=DiscordSettings(
                token=_str("DISCORD_BOT_TOKEN"),
                command_prefix=_str("COMMAND_PREFIX", "!") or "!",
                dev_guild_ids=_ids("DEV_GUILD_IDS"),
                default_alert_channel_id=_id("DEFAULT_ALERT_CHANNEL_ID"),
                enable_members_intent=_bool("ENABLE_MEMBERS_INTENT", False),
                max_upload_mb=_int("MAX_UPLOAD_MB", 24, minimum=1),
            ),
            gemini=GeminiSettings(
                api_key=_str("GEMINI_API_KEY"),
                model=_str("GEMINI_MODEL", "gemini-2.5-flash") or "gemini-2.5-flash",
                temperature=_float("GEMINI_TEMPERATURE", 0.9, minimum=0.0),
            ),
            leak=LeakSettings(
                enabled=_bool("LEAK_MONITOR_ENABLED", True),
                interval_seconds=_int("LEAK_SCAN_INTERVAL", 900, minimum=120),
                min_source_interval=_int("LEAK_MIN_SOURCE_INTERVAL", 1800, minimum=120),
                alert_channel_id=_id("LEAK_ALERT_CHANNEL_ID"),
                hibp_api_key=_str("HIBP_API_KEY"),
                github_token=_str("GITHUB_TOKEN"),
                enable_ransomwatch=_bool("LEAK_SOURCE_RANSOMWATCH", True),
                enable_psbdmp=_bool("LEAK_SOURCE_PSBDMP", True),
                enable_hudsonrock=_bool("LEAK_SOURCE_HUDSONROCK", True),
                max_alerts_per_cycle=_int("LEAK_MAX_ALERTS_PER_CYCLE", 10, minimum=1),
                request_timeout=_float("LEAK_REQUEST_TIMEOUT", 20.0, minimum=3.0),
                snippet_chars=_int("LEAK_SNIPPET_CHARS", 400, minimum=80),
            ),
            cinematic=CinematicSettings(
                enabled=_bool("CINEMATIC_ENABLED", True),
                pexels_api_key=_str("PEXELS_API_KEY"),
                workdir=_path("CINEMATIC_WORKDIR", str(data_dir / "cinematic")),
                music_dir=_path("CINEMATIC_MUSIC_DIR", "assets/music"),
                ffmpeg_bin=_str("FFMPEG_BIN", "ffmpeg") or "ffmpeg",
                ffprobe_bin=_str("FFPROBE_BIN", "ffprobe") or "ffprobe",
                tts_voices=_csv(
                    "CINEMATIC_VOICES",
                    (
                        "en-US-AriaNeural",
                        "en-US-GuyNeural",
                        "en-GB-RyanNeural",
                        "bn-BD-NabanitaNeural",
                        "bn-IN-BashkarNeural",
                    ),
                ),
                max_duration_seconds=_int("CINEMATIC_MAX_SECONDS", 60, minimum=10),
                max_upload_mb=_int("CINEMATIC_MAX_UPLOAD_MB", _int("MAX_UPLOAD_MB", 24), minimum=2),
                max_scenes=_int("CINEMATIC_MAX_SCENES", 8, minimum=2),
                keep_jobs=_int("CINEMATIC_KEEP_JOBS", 5, minimum=1),
                allow_demo_scripts=_bool("CINEMATIC_DEMO_MODE", True),
                caption_font=_str("CAPTION_FONT", "DejaVu Sans") or "DejaVu Sans",
                caption_size=_int("CAPTION_SIZE", 62, minimum=24),
                youtube_client_secret=(
                    _path("YOUTUBE_CLIENT_SECRET", "credentials/client_secret.json")
                    if _str("YOUTUBE_CLIENT_SECRET")
                    else None
                ),
                youtube_token_file=(
                    _path("YOUTUBE_TOKEN_FILE", "credentials/youtube_token.json")
                    if _str("YOUTUBE_TOKEN_FILE")
                    else None
                ),
            ),
            dj=DJSettings(
                enabled=_bool("DJ_ENABLED", True),
                default_volume=_float("DJ_VOLUME", 0.5, minimum=0.0),
                max_queue=_int("DJ_MAX_QUEUE", 50, minimum=1),
                autoplay=_bool("DJ_AUTOPLAY", True),
                spotify_client_id=_str("SPOTIFY_CLIENT_ID"),
                spotify_client_secret=_str("SPOTIFY_CLIENT_SECRET"),
                ytdlp_cookies_file=(
                    _path("YTDLP_COOKIES_FILE", "credentials/ytdlp-cookies.txt")
                    if _str("YTDLP_COOKIES_FILE")
                    else None
                ),
                ffmpeg_bin=_str("FFMPEG_BIN", "ffmpeg") or "ffmpeg",
                idle_disconnect_seconds=_int("DJ_IDLE_DISCONNECT", 60, minimum=0),
            ),
            sniper=SniperSettings(
                enabled=_bool("SNIPER_ENABLED", True),
                alert_interval_seconds=_int("SNIPER_ALERT_INTERVAL", 120, minimum=30),
                alert_channel_id=_id("SNIPER_ALERT_CHANNEL_ID"),
                coingecko_api_key=_str("COINGECKO_API_KEY"),
                x_bearer_token=_str("X_BEARER_TOKEN"),
                finnhub_api_key=_str("FINNHUB_API_KEY"),
                subreddits=_csv(
                    "REDDIT_SUBREDDITS",
                    ("CryptoCurrency", "CryptoMoonShots", "SatoshiStreetBets", "memecoins"),
                ),
                cache_ttl_seconds=_int("SNIPER_CACHE_TTL", 60, minimum=10),
                request_timeout=_float("SNIPER_REQUEST_TIMEOUT", 20.0, minimum=3.0),
            ),
            games=GameSettings(
                enabled=_bool("GAMES_ENABLED", True),
                session_ttl_minutes=_int("GAMES_SESSION_TTL", 180, minimum=10),
                max_active_sessions=_int("GAMES_MAX_SESSIONS", 200, minimum=1),
                rpg_cooldown_seconds=_float("GAMES_RPG_COOLDOWN", 2.0, minimum=0.5),
                interrogation_cooldown_seconds=_float(
                    "GAMES_INTERROGATION_COOLDOWN", 5.0, minimum=1.0
                ),
                boss_cooldown_seconds=_float("GAMES_BOSS_COOLDOWN", 8.0, minimum=2.0),
                boss_base_hp=_int("GAMES_BOSS_BASE_HP", 280, minimum=100),
                max_interrogations=_int("GAMES_MAX_INTERROGATIONS", 24, minimum=3),
                ai_narration=_bool("GAMES_AI_NARRATION", True),
            ),
            companion=CompanionSettings(
                enabled=_bool("COMPANION_ENABLED", True),
                gf_enabled=_bool("GF_ENABLED", True),
                ask_enabled=_bool("ASK_ENABLED", True),
                gf_name=_str("GF_NAME", "Maya") or "Maya",
                gf_persona=_str("GF_PERSONA", "") or "",
                gf_cooldown_seconds=_float("GF_COOLDOWN", 3.0, minimum=1.0),
                ask_cooldown_seconds=_float("ASK_COOLDOWN", 5.0, minimum=1.0),
                gf_history_turns=_int("GF_HISTORY_TURNS", 8, minimum=0),
                gf_max_message_chars=_int("GF_MAX_MESSAGE_CHARS", 700, minimum=100),
                ask_max_question_chars=_int("ASK_MAX_QUESTION_CHARS", 2000, minimum=100),
                ask_max_output_tokens=_int("ASK_MAX_OUTPUT_TOKENS", 1600, minimum=200),
                memory_ttl_days=_int("COMPANION_MEMORY_TTL_DAYS", 30, minimum=1),
            ),
            data_dir=data_dir,
            db_path=_path("DATABASE_PATH", str(data_dir / "autobot.db")),
            log_level=(_str("LOG_LEVEL", "INFO") or "INFO").upper(),
            environment=_str("ENVIRONMENT", "production") or "production",
        )


settings = Settings.from_env()
