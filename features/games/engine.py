"""Pure combat / progression rules for the games (no Discord or AI dependencies)."""

from __future__ import annotations

import random
from dataclasses import dataclass

from features.games.models import BossPlayer, BossState

ACTIONS = ("attack", "spell", "heal")
ACTION_LABELS = {
    "attack": ("⚔️ Attack", "Reliable strike · +5 mana"),
    "spell": ("✨ Spell", "Heavy arcane damage · costs 25 mana"),
    "heal": ("💚 Heal", "Restore HP · costs 20 mana"),
}
SPELL_COST = 25
HEAL_COST = 20
MANA_REGEN = 5
BASE_PLAYER_HP = 100
DOWNED_RECOVERY_HP = 30


@dataclass(frozen=True)
class Roll:
    damage: int
    crit: bool = False


@dataclass(frozen=True)
class ActionEffect:
    damage: int = 0
    heal: int = 0
    mana_delta: int = 0
    crit: bool = False


def boss_hp_for_players(base_hp: int, player_count: int) -> int:
    """Boss health scales gently so solo fights finish and raids stay tense."""
    return max(200, int(base_hp) + 80 * max(0, player_count - 1))


def phase_for(hp: int, max_hp: int) -> int:
    if max_hp <= 0:
        return 3
    ratio = hp / max_hp
    if ratio <= 0.3:
        return 3
    if ratio <= 0.65:
        return 2
    return 1


def roll_damage(
    base: int,
    spread: int,
    rng: random.Random,
    crit_chance: float = 0.12,
    crit_multiplier: float = 1.6,
) -> Roll:
    raw = max(1, int(round(rng.uniform(base - spread, base + spread))))
    crit = rng.random() < crit_chance
    if crit:
        raw = max(raw + 1, int(raw * crit_multiplier))
    return Roll(damage=raw, crit=crit)


def player_action(action: str, phase_context: int, rng: random.Random) -> ActionEffect:
    """Resolve a player action. ``phase_context`` is the boss phase (2/3 hit harder)."""
    action = action.lower().strip()
    bonus = max(0, phase_context - 1) * 2
    if action == "attack":
        roll = roll_damage(14 + bonus, 5, rng)
        return ActionEffect(damage=roll.damage, crit=roll.crit, mana_delta=MANA_REGEN)
    if action == "spell":
        roll = roll_damage(24 + bonus, 7, rng, crit_chance=0.18)
        return ActionEffect(damage=roll.damage, crit=roll.crit, mana_delta=-SPELL_COST)
    if action == "heal":
        amount = max(1, int(round(rng.uniform(16, 26))))
        return ActionEffect(heal=amount, mana_delta=-HEAL_COST)
    raise ValueError(f"Unknown action: {action}")


def boss_attack_damage(
    phase: int,
    player_count: int,
    rng: random.Random,
    special: bool = False,
) -> int:
    base = 7 + phase * 4 + max(0, player_count - 1)
    spread = 5
    if special:
        base += 6
        spread = 6
    return max(3, int(round(rng.uniform(base - spread, base + spread))))


def pick_target(players: list[BossPlayer], rng: random.Random) -> BossPlayer | None:
    alive = [player for player in players if player.alive]
    if not alive:
        return None
    return rng.choice(alive)


def revive_downed(players: list[BossPlayer], current_round: int) -> list[str]:
    """Bring back players whose downed timer has elapsed."""
    revived: list[str] = []
    for player in players:
        if player.hp <= 0 and player.downed_until <= current_round:
            player.hp = min(player.max_hp, DOWNED_RECOVERY_HP)
            player.downed_until = 0
            revived.append(player.name)
    return revived


def boss_turn_due(state: BossState) -> bool:
    """The boss retaliates every second player action."""
    return state.actions % 2 == 0


def is_special_boss_turn(state: BossState) -> bool:
    return state.boss_turns > 0 and state.boss_turns % 4 == 0


def reward_for(player: BossPlayer) -> int:
    return 40 + player.damage_dealt // 12 + player.healing_done // 20


def join_bonus_hp() -> int:
    return 60


def apply_action(player: BossPlayer, effect: ActionEffect, boss: BossState) -> tuple[int, int]:
    """Apply an action to the boss/player. Returns (damage_done, healed)."""
    healed_time = 0
    if effect.mana_delta:
        player.mana = max(0, min(player.max_mana, player.mana + effect.mana_delta))
    if effect.heal:
        before = player.hp
        player.hp = min(player.max_hp, player.hp + effect.heal)
        healed_time = player.hp - before
        player.healing_done += healed_time
    if effect.damage:
        boss.hp = max(0, boss.hp - effect.damage)
        player.damage_dealt += effect.damage
    return effect.damage, healed_time


def clamp_mana(player: BossPlayer) -> None:
    player.mana = max(0, min(player.max_mana, player.mana))
