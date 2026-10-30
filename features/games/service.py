"""Game orchestration: validation, persistence, cooldowns and combat turns."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from core.config import GameSettings
from core.db import Database
from features.games import engine
from features.games.models import (
    AdventureState,
    BossPlayer,
    BossState,
    Choice,
    DetectiveState,
    GameState,
    Suspect,
    new_id,
)
from features.games.store import GameStore
from features.games.storyteller import (
    BossIntro,
    CaseData,
    InterrogationReply,
    Storyteller,
    clean_text,
)

log = logging.getLogger(__name__)

MAX_THEME = 200
MIN_THEME = 3
MAX_QUESTION = 300
MAX_HISTORY = 14
MAX_INVENTORY = 12


class GameError(RuntimeError):
    """A user-facing problem with a game command."""


@dataclass
class BossTurn:
    state: BossState
    narration: str
    source: str = "local"
    player_damage: int = 0
    boss_damage: int = 0
    healed: int = 0
    crit: bool = False
    special: bool = False
    events: list[str] = field(default_factory=list)
    mvp_name: str | None = None


class GameService:
    def __init__(
        self,
        bot: Any,
        db: Database,
        settings: GameSettings,
        storyteller: Storyteller | None = None,
        rng: random.Random | None = None,
    ):
        self.bot = bot
        self.settings = settings
        self.store = GameStore(db)
        self.story = storyteller or Storyteller(rng=rng)
        self.rng = rng or random.Random()
        self._locks: dict[str, asyncio.Lock] = {}
        self._cooldowns: dict[tuple[str, int], float] = {}

    # -- lifecycle ---------------------------------------------------------
    async def setup(self) -> None:
        await self.store.setup()
        try:
            await self.store.purge(self.settings.session_ttl_minutes)
        except Exception:  # pragma: no cover - purge is best effort
            log.warning("Game session purge failed", exc_info=True)

    async def purge(self) -> int:
        return await self.store.purge(self.settings.session_ttl_minutes)

    @property
    def ai_available(self) -> bool:
        return self.story.ai_available

    def _require_enabled(self) -> None:
        if not self.settings.enabled:
            raise GameError("🎮 Games are disabled on this bot (GAMES_ENABLED=false).")

    def _lock(self, session_id: str) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
            if len(self._locks) > 500:
                self._locks = {
                    key: value for key, value in self._locks.items() if value.locked()
                } or {session_id: lock}
        return lock

    async def _save(self, state: GameState) -> None:
        await self.store.save(state)

    async def _load(self, session_id: str) -> GameState:
        state = await self.store.get(session_id)
        if state is None:
            raise GameError("This game no longer exists. Start a new one.")
        if state.status == "active" and self._is_stale(state.updated_at):
            state.status = "expired"
            await self._save(state)
            raise GameError("This game has expired. Start a new one.")
        return state

    def _is_stale(self, updated_at: str) -> bool:
        try:
            stamp = datetime.fromisoformat(updated_at)
        except (TypeError, ValueError):
            return False
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - stamp).total_seconds() / 60
        return age > self.settings.session_ttl_minutes

    def _check_cooldown(self, kind: str, user_id: int, seconds: float) -> None:
        now = time.monotonic()
        if len(self._cooldowns) > 5000:
            cutoff = now - 3600
            self._cooldowns = {
                key: stamp for key, stamp in self._cooldowns.items() if stamp >= cutoff
            }
        last = self._cooldowns.get((kind, user_id), 0.0)
        remaining = seconds - (now - last)
        if remaining > 0:
            raise GameError(f"⏳ Easy — wait **{remaining:.1f}s** before acting again.")
        self._cooldowns[(kind, user_id)] = now

    async def _reserve_session(self) -> None:
        sessions = await self.store.list_active(limit=1000)
        active = sum(1 for state in sessions if not self._is_stale(state.updated_at))
        if active >= self.settings.max_active_sessions:
            raise GameError(
                "🎲 Too many games are running right now. Try again once a few finish."
            )

    def _clean_theme(self, theme: str, *, empty: str = "a dark fantasy adventure") -> str:
        text = clean_text(theme, MAX_THEME)
        if not text:
            return empty
        if len(text) < MIN_THEME:
            raise GameError("Give me a slightly longer theme (at least 3 characters).")
        return text

    # -- lookups -----------------------------------------------------------
    async def active_rpg(self, guild_id: int, owner_id: int) -> AdventureState | None:
        sessions = await self.store.find_active(guild_id, "rpg", owner_id=owner_id)
        for state in sessions:
            if isinstance(state, AdventureState) and not self._is_stale(state.updated_at):
                return state
        return None

    async def active_detective(
        self, guild_id: int, channel_id: int | None = None
    ) -> DetectiveState | None:
        sessions = await self.store.find_active(
            guild_id, "detective", channel_id=channel_id
        )
        for state in sessions:
            if isinstance(state, DetectiveState) and not self._is_stale(state.updated_at):
                return state
        return None

    async def active_boss(self, guild_id: int) -> BossState | None:
        sessions = await self.store.find_active(guild_id, "boss")
        for state in sessions:
            if isinstance(state, BossState) and not self._is_stale(state.updated_at):
                return state
        return None

    async def session_counts(self, guild_id: int) -> dict[str, int]:
        sessions = await self.store.list_active(guild_id=guild_id, limit=500)
        counts = {"rpg": 0, "detective": 0, "boss": 0}
        for state in sessions:
            if self._is_stale(state.updated_at):
                continue
            if isinstance(state, AdventureState):
                counts["rpg"] += 1
            elif isinstance(state, DetectiveState):
                counts["detective"] += 1
            elif isinstance(state, BossState):
                counts["boss"] += 1
        return counts

    # -- adventure ---------------------------------------------------------
    async def start_rpg(
        self,
        guild_id: int,
        channel_id: int,
        owner_id: int,
        owner_name: str,
        theme: str,
    ) -> AdventureState:
        self._require_enabled()
        theme_text = self._clean_theme(theme)
        existing = await self.active_rpg(guild_id, owner_id)
        if existing:
            raise GameError(
                f"You already have an active adventure: **{existing.title}**. "
                "Finish it or use `/game quit` first."
            )
        await self._reserve_session()
        turn = await self.story.start_adventure(theme_text, owner_name)
        state = AdventureState(
            id=new_id(),
            guild_id=guild_id,
            channel_id=channel_id,
            owner_id=owner_id,
            owner_name=owner_name,
            theme=theme_text,
            title=turn.title,
            scene_no=0,
            scene=turn.scene,
            choices=[Choice(**choice) for choice in turn.choices],
            source=turn.source,
        )
        await self._save(state)
        log.info("RPG %s started by %s (%s)", state.id, owner_id, turn.source)
        return state

    async def choose(
        self,
        session_id: str,
        user_id: int,
        index: int,
        expected_scene: int | None = None,
    ) -> AdventureState:
        self._require_enabled()
        async with self._lock(session_id):
            state = await self._load(session_id)
            if not isinstance(state, AdventureState):
                raise GameError("That is not an adventure session.")
            if state.owner_id != user_id:
                raise GameError(f"🚫 This adventure belongs to <@{state.owner_id}>.")
            if state.over:
                raise GameError("This adventure is already over. Start a new one with `/game rpg`.")
            if expected_scene is not None and expected_scene != state.scene_no:
                raise GameError("⌛ That button belongs to an older scene — use the newest message.")
            if not 0 <= index < len(state.choices):
                raise GameError("That choice does not exist.")
            self._check_cooldown("rpg", user_id, self.settings.rpg_cooldown_seconds)
            choice = state.choices[index]
            turn = await self.story.next_adventure(state, {"label": choice.label, "hint": choice.hint})
            self._apply_effects(state, turn.effects)
            state.scene = turn.scene
            state.choices = [Choice(**item) for item in turn.choices]
            state.scene_no += 1
            state.last_choice = choice.label
            state.source = turn.source
            state.history.append(f"S{state.scene_no}: {choice.label}")
            state.history = state.history[-MAX_HISTORY:]
            if state.hp <= 0:
                state.status = "dead"
                state.choices = []
                state.scene = (
                    f"The {state.theme} takes its price. Your story ends here, "
                    f"{state.owner_name}, with **{state.gold} gold** in your pack and "
                    f"{len(state.inventory)} relic(s) to show for it."
                )
            elif turn.ending:
                state.status = "finished"
                state.choices = []
            state.touch()
            await self._save(state)
            return state

    def _apply_effects(self, state: AdventureState, effects: dict[str, Any]) -> None:
        if not effects:
            return
        state.hp = max(0, min(state.max_hp, state.hp + int(effects.get("hp", 0))))
        state.gold = max(0, state.gold + int(effects.get("gold", 0)))
        item = clean_text(effects.get("item"), 40)
        if item and item not in state.inventory and len(state.inventory) < MAX_INVENTORY:
            state.inventory.append(item)

    async def end_session(self, session_id: str, user_id: int, guild_id: int) -> str:
        async with self._lock(session_id):
            state = await self._load(session_id)
            if state.owner_id != user_id:
                raise GameError("🚫 Only the player who started this game can end it.")
            if state.status == "active":
                state.status = "abandoned"
                state.touch()
                await self._save(state)
            return state.id

    # -- detective ---------------------------------------------------------
    async def start_detective(
        self,
        guild_id: int,
        channel_id: int,
        owner_id: int,
        owner_name: str,
        theme: str,
    ) -> DetectiveState:
        self._require_enabled()
        theme_text = self._clean_theme(theme, empty="a rain-soaked city noir")
        existing = await self.active_detective(guild_id, channel_id)
        if existing:
            raise GameError(
                f"🔎 A case is already open in this channel: **{existing.title}**. "
                "Solve it or close it with `/game quit` first."
            )
        await self._reserve_session()
        case: CaseData = await self.story.generate_case(theme_text)
        state = DetectiveState(
            id=new_id(),
            guild_id=guild_id,
            channel_id=channel_id,
            owner_id=owner_id,
            owner_name=owner_name,
            theme=theme_text,
            title=case.title,
            victim=case.victim,
            location=case.location,
            time=case.time,
            weapon=case.weapon,
            summary=case.summary,
            suspects=case.suspects,
            transcript={suspect.name: [] for suspect in case.suspects},
            attempts_left=2,
            source=case.source,
        )
        await self._save(state)
        log.info("Detective %s started by %s (%s)", state.id, owner_id, case.source)
        return state

    async def interrogate(
        self,
        session_id: str,
        user_id: int,
        suspect_index: int,
        question: str,
        questioner_name: str,
    ) -> tuple[DetectiveState, Suspect, InterrogationReply]:
        self._require_enabled()
        async with self._lock(session_id):
            state = await self._load(session_id)
            if not isinstance(state, DetectiveState):
                raise GameError("That is not a detective case.")
            if state.over:
                raise GameError("This case is closed. Start another with `/game detective`.")
            if state.interrogations >= self.settings.max_interrogations:
                raise GameError(
                    "🕯️ The suspects have lawyered up — no more questions. Make your accusation!"
                )
            if not 0 <= suspect_index < len(state.suspects):
                raise GameError("Pick one of the listed suspects.")
            text = clean_text(question, MAX_QUESTION)
            if len(text) < 3:
                raise GameError("Ask a real question (at least 3 characters).")
            self._check_cooldown(
                "detective", user_id, self.settings.interrogation_cooldown_seconds
            )
            suspect = state.suspects[suspect_index]
            reply = await self.story.interrogate(state, suspect, text, questioner_name)
            entries = state.transcript.setdefault(suspect.name, [])
            entries.append({"q": text, "a": reply.answer, "by": questioner_name})
            state.transcript[suspect.name] = entries[-8:]
            state.interrogations += 1
            if reply.source == "gemini":
                state.source = "gemini"
            state.touch()
            await self._save(state)
            return state, suspect, reply

    async def accuse(
        self, session_id: str, user_id: int, suspect_index: int
    ) -> tuple[DetectiveState, bool, str]:
        self._require_enabled()
        async with self._lock(session_id):
            state = await self._load(session_id)
            if not isinstance(state, DetectiveState):
                raise GameError("That is not a detective case.")
            if state.status == "solved":
                raise GameError("✅ This case is already solved.")
            if state.status == "failed":
                raise GameError("❌ This case is closed — the killer walked free.")
            if state.over:
                raise GameError("This case has expired or been abandoned. Start another one.")
            if state.owner_id != user_id:
                raise GameError(f"🕵️ Only <@{state.owner_id}> can make the final accusation.")
            if state.attempts_left <= 0:
                raise GameError("No accusations left.")
            if not 0 <= suspect_index < len(state.suspects):
                raise GameError("Pick one of the listed suspects.")
            suspect = state.suspects[suspect_index]
            correct = suspect.is_killer
            state.attempts_left -= 1
            if correct:
                state.status = "solved"
                state.killer = suspect.name
            elif state.attempts_left <= 0:
                state.status = "failed"
                state.killer = next((s.name for s in state.suspects if s.is_killer), "")
            state.touch()
            await self._save(state)
            return state, correct, self.story.reveal(state) if state.over else ""

    # -- boss raid ---------------------------------------------------------
    async def start_boss(
        self,
        guild_id: int,
        channel_id: int,
        owner_id: int,
        owner_name: str,
        theme: str,
    ) -> BossState:
        self._require_enabled()
        theme_text = self._clean_theme(theme, empty="a shattered kingdom at the edge of night")
        existing = await self.active_boss(guild_id)
        if existing:
            raise GameError(
                f"⚔️ **{existing.name}** is already attacking! Join with `/game join` "
                "or use `/game status`."
            )
        await self._reserve_session()
        intro: BossIntro = await self.story.boss_intro(theme_text)
        max_hp = engine.boss_hp_for_players(self.settings.boss_base_hp, 1)
        state = BossState(
            id=new_id(),
            guild_id=guild_id,
            channel_id=channel_id,
            owner_id=owner_id,
            theme=theme_text,
            name=intro.name,
            lore=intro.lore,
            hp=max_hp,
            max_hp=max_hp,
            source=intro.source,
        )
        owner = BossPlayer(user_id=owner_id, name=clean_text(owner_name, 40) or "Hero")
        state.add_player(owner)
        state.log.append(f"{owner.name} sounded the horn and raised the raid.")
        await self._save(state)
        log.info("Boss %s (%s) started by %s", state.name, state.id, owner_id)
        return state

    async def join_boss(self, session_id: str, user_id: int, name: str) -> tuple[BossState, bool]:
        self._require_enabled()
        async with self._lock(session_id):
            state = await self._load(session_id)
            if not isinstance(state, BossState):
                raise GameError("That is not a boss raid.")
            if state.over:
                raise GameError("This raid is already over. Start a new one with `/game bossfight`.")
            if state.player(user_id):
                return state, False
            self._add_player(state, user_id, name)
            await self._save(state)
            return state, True

    def _add_player(self, state: BossState, user_id: int, name: str) -> BossPlayer:
        player = BossPlayer(user_id=user_id, name=clean_text(name, 40) or f"Hero {user_id % 1000}")
        state.add_player(player)
        bonus = engine.join_bonus_hp()
        state.max_hp += bonus
        state.hp = min(state.max_hp, state.hp + bonus)
        state.log.append(f"{player.name} joined the fight.")
        return player

    async def boss_action(
        self,
        session_id: str,
        user_id: int,
        name: str,
        action: str,
    ) -> BossTurn:
        self._require_enabled()
        action = action.lower().strip()
        if action not in engine.ACTIONS:
            raise GameError("Choose `attack`, `spell` or `heal`.")
        async with self._lock(session_id):
            state = await self._load(session_id)
            if not isinstance(state, BossState):
                raise GameError("That is not a boss raid.")
            if state.over:
                raise GameError("This raid is already finished. Start a fresh one with `/game bossfight`.")
            player = state.player(user_id)
            joined = False
            if player is None:
                player = self._add_player(state, user_id, name)
                joined = True
            now = time.time()
            remaining = self.settings.boss_cooldown_seconds - (now - player.last_action_at)
            if player.last_action_at and remaining > 0:
                raise GameError(f"⏳ Catch your breath — **{remaining:.1f}s** until your next action.")
            if player.hp <= 0:
                if state.round < player.downed_until:
                    raise GameError(
                        f"💀 You are downed until round **{player.downed_until}** — "
                        "you will rejoin automatically. Cheer the others on!"
                    )
                player.hp = engine.DOWNED_RECOVERY_HP
            cost = 0
            if action == "spell":
                cost = engine.SPELL_COST
            elif action == "heal":
                cost = engine.HEAL_COST
            if player.mana < cost:
                raise GameError(
                    f"🔵 Not enough mana (need {cost}, have {player.mana}). "
                    "Use `!attack` to recover mana."
                )
            if action == "heal" and player.hp >= player.max_hp:
                raise GameError("Your HP is already full — save the mana for a spell.")

            effect = engine.player_action(action, state.phase, self.rng)
            damage, healed = engine.apply_action(player, effect, state)
            player.last_action_at = now
            state.actions += 1
            state.log.append(
                f"{player.name} used {action} for {damage or healed} "
                f"{'damage' if damage else 'healing'}."
            )

            events: list[str] = []
            if healed:
                events.append(f"healed {healed}")
            if effect.crit:
                events.append("critical hit")

            boss_damage = 0
            target_name: str | None = None
            special = False
            if state.hp > 0 and engine.boss_turn_due(state):
                state.boss_turns += 1
                for revived in engine.revive_downed(list(state.players.values()), state.round):
                    events.append(f"revived:{revived}")
                special = engine.is_special_boss_turn(state)
                if special:
                    boss_damage = engine.boss_attack_damage(
                        state.phase, len(state.players), self.rng, special=True
                    )
                    for other in state.alive_players:
                        other.hp = max(0, other.hp - boss_damage)
                        if not other.alive:
                            other.downed_until = state.round + 2
                            events.append(f"downed:{other.name}")
                    target_name = "everyone"
                else:
                    target = engine.pick_target(list(state.players.values()), self.rng)
                    if target is not None:
                        boss_damage = engine.boss_attack_damage(
                            state.phase, len(state.players), self.rng
                        )
                        target.hp = max(0, target.hp - boss_damage)
                        target_name = target.name
                        if not target.alive:
                            target.downed_until = state.round + 2
                            events.append(f"downed:{target.name}")
                state.round += 1

            state.phase = engine.phase_for(state.hp, state.max_hp)
            mvp_name = None
            if state.hp <= 0:
                state.status = "victory"
                for fighter in state.players.values():
                    fighter.gold += engine.reward_for(fighter)
                mvp = max(state.players.values(), key=lambda p: p.damage_dealt)
                mvp_name = mvp.name
                events.append("victory")
                state.log.append(f"{state.name} was defeated! MVP: {mvp_name}.")
            elif not state.alive_players:
                state.status = "defeat"
                events.append("defeat")
                state.log.append("The raid was wiped out.")

            context = {
                "actor": player.name,
                "action": action,
                "damage": damage,
                "healed": healed,
                "crit": effect.crit,
                "boss_damage": boss_damage,
                "target": target_name,
                "special": special,
                "events": events,
                "joined": joined,
            }

            state.log = state.log[-12:]
            state.touch()
            await self._save(state)
            turn = BossTurn(
                state=state,
                narration="",
                player_damage=damage,
                boss_damage=boss_damage,
                healed=healed,
                crit=effect.crit,
                special=special,
                events=events,
                mvp_name=mvp_name,
            )

        # Narration is generated outside the lock so a raid's other players can
        # keep acting while Gemini is thinking.
        if self.settings.ai_narration:
            narration = await self.story.boss_narration(state, context)
            text, source = narration.text, narration.source
        else:
            text, source = self.story.local_boss_narration(state, context), "local"
        if joined:
            text = f"**{player.name}** charges into the fight! {text}"
        turn.narration = text
        turn.source = source
        return turn

    # -- rendering helpers -------------------------------------------------
    @staticmethod
    def resolve_suspect(state: DetectiveState, query: str) -> tuple[int | None, str]:
        """Resolve ``2`` / ``suspect 2`` / ``Nusrat`` plus the remaining question."""
        text = (query or "").strip()
        if not text:
            return None, ""
        indexed = re.match(r"^(?:suspect\s*)?(\d+)\s*[.:)-]?\s*(.*)$", text, re.IGNORECASE)
        if indexed:
            index = int(indexed.group(1)) - 1
            if 0 <= index < len(state.suspects):
                return index, indexed.group(2).strip()
            return None, ""
        lowered = text.lower()
        for index, suspect in enumerate(state.suspects):
            name = suspect.name.lower()
            if lowered.startswith(name):
                return index, text[len(suspect.name) :].lstrip(" :-—")
        for index, suspect in enumerate(state.suspects):
            first_name = suspect.name.split()[0].lower()
            if lowered.startswith(first_name):
                rest = text[len(first_name) :]
                if not rest or rest[0] in " :-—":
                    return index, rest.lstrip(" :-—")
        return None, ""
