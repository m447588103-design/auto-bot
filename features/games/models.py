"""Serialisable state objects for the interactive games."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any


def new_id() -> str:
    return uuid.uuid4().hex[:10]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


@dataclass
class Choice:
    label: str
    hint: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "hint": self.hint}


@dataclass
class AdventureState:
    id: str
    guild_id: int
    channel_id: int
    owner_id: int
    owner_name: str
    theme: str
    title: str
    scene_no: int
    scene: str
    choices: list[Choice] = field(default_factory=list)
    hp: int = 100
    max_hp: int = 100
    gold: int = 0
    inventory: list[str] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    status: str = "active"  # active | finished | dead
    source: str = "local"
    last_choice: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @property
    def over(self) -> bool:
        return self.status != "active"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["choices"] = [choice.to_dict() for choice in self.choices]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AdventureState":
        payload = _known(cls, data)
        payload["choices"] = [
            Choice(**{"label": c.get("label", "Continue"), "hint": c.get("hint", "")})
            for c in data.get("choices", [])
            if isinstance(c, dict)
        ]
        return cls(**payload)

    def touch(self) -> None:
        self.updated_at = utc_now()


@dataclass
class Suspect:
    name: str
    role: str
    profile: str
    alibi: str
    secret: str
    is_killer: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DetectiveState:
    id: str
    guild_id: int
    channel_id: int
    owner_id: int
    owner_name: str
    theme: str
    title: str
    victim: str
    location: str
    time: str
    weapon: str
    summary: str
    suspects: list[Suspect] = field(default_factory=list)
    transcript: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    interrogations: int = 0
    attempts_left: int = 2
    status: str = "active"  # active | solved | failed
    killer: str = ""
    source: str = "local"
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @property
    def over(self) -> bool:
        return self.status != "active"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["suspects"] = [suspect.to_dict() for suspect in self.suspects]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DetectiveState":
        payload = _known(cls, data)
        payload["suspects"] = [
            Suspect(
                name=_text(s.get("name"), 40) or "Suspect",
                role=_text(s.get("role"), 60),
                profile=_text(s.get("profile"), 400),
                alibi=_text(s.get("alibi"), 300),
                secret=_text(s.get("secret"), 300),
                is_killer=bool(s.get("is_killer")),
            )
            for s in data.get("suspects", [])
            if isinstance(s, dict)
        ]
        transcript = data.get("transcript") or {}
        payload["transcript"] = {
            str(name): [entry for entry in entries if isinstance(entry, dict)]
            for name, entries in transcript.items()
            if isinstance(entries, list)
        }
        return cls(**payload)

    def touch(self) -> None:
        self.updated_at = utc_now()


@dataclass
class BossPlayer:
    user_id: int
    name: str
    hp: int = 100
    max_hp: int = 100
    mana: int = 100
    max_mana: int = 100
    damage_dealt: int = 0
    healing_done: int = 0
    gold: int = 0
    downed_until: int = 0
    last_action_at: float = 0.0

    @property
    def alive(self) -> bool:
        return self.hp > 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BossPlayer":
        return cls(**_known(cls, data))


@dataclass
class BossState:
    id: str
    guild_id: int
    channel_id: int
    owner_id: int
    theme: str
    name: str
    lore: str
    hp: int
    max_hp: int
    players: dict[str, BossPlayer] = field(default_factory=dict)
    round: int = 1
    actions: int = 0
    boss_turns: int = 0
    phase: int = 1
    status: str = "active"  # active | victory | defeat
    source: str = "local"
    log: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    @property
    def over(self) -> bool:
        return self.status != "active"

    @property
    def alive_players(self) -> list[BossPlayer]:
        return [p for p in self.players.values() if p.alive]

    @property
    def total_damage(self) -> int:
        return sum(p.damage_dealt for p in self.players.values())

    def player(self, user_id: int) -> BossPlayer | None:
        return self.players.get(str(user_id))

    def add_player(self, player: BossPlayer) -> None:
        self.players[str(player.user_id)] = player

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["players"] = {uid: p.to_dict() for uid, p in self.players.items()}
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BossState":
        payload = _known(cls, data)
        payload["players"] = {
            str(uid): BossPlayer.from_dict(p)
            for uid, p in (data.get("players") or {}).items()
            if isinstance(p, dict)
        }
        return cls(**payload)

    def touch(self) -> None:
        self.updated_at = utc_now()


GameState = AdventureState | DetectiveState | BossState


def _known(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    names = {f.name for f in fields(cls)}
    return {key: value for key, value in data.items() if key in names}


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]
