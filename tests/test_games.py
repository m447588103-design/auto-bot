"""Offline tests for the AI games: engine maths, local scripts, service flows."""

from __future__ import annotations

import dataclasses
import random
from types import SimpleNamespace

import pytest

from core.config import settings
from core.db import Database
from features.games import engine
from features.games.models import (
    AdventureState,
    BossPlayer,
    BossState,
    Choice,
    DetectiveState,
    Suspect,
)
from features.games.service import BossTurn, GameError, GameService
from features.games.storyteller import Storyteller, _validate_case, _validate_choices
from features.games.views import BossView, DetectiveView, RPGView, build_view


class OfflineGemini:
    """Mimics a Gemini client without a key so all local fallbacks run."""

    available = False
    backend = None


class ScriptedGemini:
    """Returns canned JSON/text so the AI code path can be tested offline."""

    available = True
    backend = "fake"

    def __init__(self, payload=None, text="A shadow moves behind the door."):
        self.payload = payload
        self.text = text
        self.calls = 0

    async def generate_json(self, *args, **kwargs):
        self.calls += 1
        return self.payload

    async def generate(self, *args, **kwargs):
        self.calls += 1
        return SimpleNamespace(text=self.text)


@pytest.fixture
def games_settings():
    return dataclasses.replace(
        settings.games,
        boss_base_hp=100,
        boss_cooldown_seconds=0.0,
        rpg_cooldown_seconds=0.0,
        interrogation_cooldown_seconds=0.0,
        max_interrogations=12,
        ai_narration=False,
    )


@pytest.fixture
async def service(tmp_path, games_settings):
    db = Database(tmp_path / "games.db")
    await db.connect()
    svc = GameService(
        bot=SimpleNamespace(),
        db=db,
        settings=games_settings,
        storyteller=Storyteller(gemini=OfflineGemini(), rng=random.Random(7)),
        rng=random.Random(7),
    )
    await svc.setup()
    try:
        yield svc
    finally:
        await db.close()


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
def test_boss_hp_scales_with_party_and_floors_at_200():
    assert engine.boss_hp_for_players(600, 1) == 600
    assert engine.boss_hp_for_players(100, 1) == 200
    assert engine.boss_hp_for_players(280, 4) == 520


def test_phase_thresholds():
    assert engine.phase_for(100, 100) == 1
    assert engine.phase_for(65, 100) == 2
    assert engine.phase_for(65, 100) == 2
    assert engine.phase_for(30, 100) == 3
    assert engine.phase_for(0, 100) == 3


def test_player_actions_respect_mana_and_health_costs():
    rng = random.Random(1)
    attack = engine.player_action("attack", 1, rng)
    assert attack.damage >= 1 and attack.mana_delta == engine.MANA_REGEN
    spell = engine.player_action("spell", 1, rng)
    assert spell.damage >= 1 and spell.mana_delta == -engine.SPELL_COST
    heal = engine.player_action("heal", 1, rng)
    assert heal.heal >= 1 and heal.mana_delta == -engine.HEAL_COST
    with pytest.raises(ValueError):
        engine.player_action("dance", 1, rng)


def test_apply_action_clamps_hp_and_tracks_damage():
    boss = BossState(
        id="b", guild_id=1, channel_id=2, owner_id=3, theme="t", name="Boss",
        lore="lore", hp=50, max_hp=100,
    )
    player = BossPlayer(user_id=3, name="Hero", hp=90, mana=50)
    boss.add_player(player)
    effect = engine.ActionEffect(damage=70, crit=True, mana_delta=-25)
    damage, healed = engine.apply_action(player, effect, boss)
    assert damage == 70 and boss.hp == 0 and player.mana == 25
    heal_effect = engine.ActionEffect(heal=40, mana_delta=-20)
    _, healed = engine.apply_action(player, heal_effect, boss)
    assert player.hp == 100 and healed == 10


def test_revive_downed_only_after_timer():
    players = [
        BossPlayer(user_id=1, name="A", hp=0, downed_until=3),
        BossPlayer(user_id=2, name="B", hp=0, downed_until=5),
    ]
    assert engine.revive_downed(players, 2) == []
    assert engine.revive_downed(players, 4) == ["A"]
    assert players[0].hp == engine.DOWNED_RECOVERY_HP


def test_crit_rolls_stay_positive():
    rng = random.Random(3)
    for _ in range(200):
        roll = engine.roll_damage(2, 5, rng)
        assert roll.damage >= 1


# ---------------------------------------------------------------------------
# Storyteller validation / local content
# ---------------------------------------------------------------------------
def test_choice_validation_rejects_bad_payloads():
    assert _validate_choices("nope") == []
    assert _validate_choices([{"label": ""}]) == []
    assert len(_validate_choices([{"label": "a"}, {"label": "b"}])) == 2
    four = _validate_choices(
        [{"label": "a"}, {"label": "b"}, {"label": "c"}, {"label": "d"}]
    )
    assert len(four) == 4


def test_case_validation_requires_exactly_one_killer():
    base = {
        "title": "T",
        "victim": "V",
        "summary": "S",
        "suspects": [
            {"name": "A", "is_killer": False},
            {"name": "B", "is_killer": False},
            {"name": "C", "is_killer": False},
        ],
    }
    assert _validate_case(base) is None
    base["suspects"][1]["is_killer"] = True
    case = _validate_case(base)
    assert case is not None and case.killer().name == "B"


def test_local_case_has_one_killer_and_red_herrings():
    story = Storyteller(gemini=OfflineGemini(), rng=random.Random(2))
    case = story.local_case("a wedding in Old Dhaka")
    assert len(case.suspects) == 3
    assert sum(s.is_killer for s in case.suspects) == 1
    assert all(s.secret for s in case.suspects)
    assert "Old Dhaka" in case.summary


def test_local_interrogation_stays_in_character():
    story = Storyteller(gemini=OfflineGemini(), rng=random.Random(2))
    case = story.local_case("x")
    killer = next(s for s in case.suspects if s.is_killer)
    innocent = next(s for s in case.suspects if not s.is_killer)
    killer_answer = story.local_interrogation(killer, "Where were you at midnight?")
    innocent_answer = story.local_interrogation(innocent, "Where were you at midnight?")
    assert killer_answer and innocent_answer
    assert killer_answer != innocent_answer
    assert "AI" not in killer_answer


# ---------------------------------------------------------------------------
# Service: adventure
# ---------------------------------------------------------------------------
async def test_rpg_flow_and_stale_scene_guard(service: GameService):
    state = await service.start_rpg(1, 2, 99, "Rafi", "a haunted tea garden")
    assert isinstance(state, AdventureState)
    assert state.status == "active" and len(state.choices) >= 2
    assert state.source == "local"

    chosen = await service.choose(state.id, 99, 0, expected_scene=0)
    assert chosen.scene_no == 1
    assert chosen.last_choice == state.choices[0].label

    with pytest.raises(GameError, match="older scene"):
        await service.choose(state.id, 99, 0, expected_scene=0)

    with pytest.raises(GameError, match="belongs to"):
        await service.choose(state.id, 1234, 0)


async def test_rpg_rejects_duplicate_and_foreign_sessions(service: GameService):
    await service.start_rpg(1, 2, 99, "Rafi", "a sunken library")
    with pytest.raises(GameError, match="already have an active adventure"):
        await service.start_rpg(1, 2, 99, "Rafi", "another")
    other = await service.start_rpg(1, 2, 100, "Mita", "a second story")
    assert other.owner_id == 100


async def test_rpg_can_finish_or_die(service: GameService):
    state = await service.start_rpg(1, 2, 99, "Rafi", "a broken clock tower")
    for _ in range(12):
        if state.over:
            break
        state = await service.choose(state.id, 99, 0)
    assert state.over
    assert state.status in {"finished", "dead"}


# ---------------------------------------------------------------------------
# Service: detective
# ---------------------------------------------------------------------------
async def test_detective_flow_interrogate_and_accuse(service: GameService):
    case = await service.start_detective(1, 2, 99, "Rafi", "a wedding in Old Dhaka")
    assert isinstance(case, DetectiveState)
    assert len(case.suspects) == 3
    assert case.attempts_left == 2

    state, suspect, reply = await service.interrogate(
        case.id, 100, 0, "Where were you at midnight?", "Mita"
    )
    assert state.transcript[suspect.name][0]["q"] == "Where were you at midnight?"
    assert reply.answer

    with pytest.raises(GameError, match="Only"):
        await service.accuse(case.id, 100, 0)

    killer_index = next(i for i, s in enumerate(state.suspects) if s.is_killer)
    wrong_index = next(i for i in range(3) if i != killer_index)
    state, correct, _ = await service.accuse(case.id, 99, wrong_index)
    assert not correct and state.attempts_left == 1 and state.status == "active"

    state, correct, reveal = await service.accuse(case.id, 99, killer_index)
    assert correct and state.status == "solved"
    assert state.killer == state.suspects[killer_index].name
    assert state.suspects[killer_index].name in reveal


async def test_detective_second_wrong_accusation_closes_case(service: GameService):
    case = await service.start_detective(5, 6, 7, "Rafi", "a midnight train")
    killer_index = next(i for i, s in enumerate(case.suspects) if s.is_killer)
    wrong = [i for i in range(3) if i != killer_index]
    state, correct, _ = await service.accuse(case.id, 7, wrong[0])
    assert not correct
    state, correct, reveal = await service.accuse(case.id, 7, wrong[1])
    assert not correct and state.status == "failed"
    assert "killed" in reveal


async def test_interrogation_limits(service: GameService):
    case = await service.start_detective(1, 2, 99, "Rafi", "a rooftop party")
    for i in range(service.settings.max_interrogations):
        await service.interrogate(case.id, 100, 0, f"Question {i}?", "Mita")
    with pytest.raises(GameError, match="lawyered up"):
        await service.interrogate(case.id, 100, 0, "One more?", "Mita")


# ---------------------------------------------------------------------------
# Service: boss raid
# ---------------------------------------------------------------------------
async def test_boss_raid_reaches_a_resolution(service: GameService):
    boss = await service.start_boss(1, 2, 99, "Rafi", "a drowned temple")
    assert isinstance(boss, BossState)
    assert boss.players["99"].name == "Rafi"

    boss, joined = await service.join_boss(boss.id, 100, "Mita")
    assert joined
    assert boss.player(100) is not None
    assert boss.max_hp > engine.boss_hp_for_players(service.settings.boss_base_hp, 1)

    boss, joined = await service.join_boss(boss.id, 100, "Mita")
    assert not joined

    turns = 0
    while not boss.over and turns < 80:
        turn = await service.boss_action(boss.id, 99, "Rafi", "attack")
        boss = turn.state
        turns += 1
        assert turn.narration
        if not boss.over:
            boss = (await service.boss_action(boss.id, 100, "Mita", "attack")).state
    assert boss.over
    assert boss.status in {"victory", "defeat"}


async def test_boss_cooldown_blocks_double_actions(service: GameService):
    service.settings = dataclasses.replace(service.settings, boss_cooldown_seconds=60.0)
    boss = await service.start_boss(1, 2, 99, "Rafi", "a frozen palace")
    await service.boss_action(boss.id, 99, "Rafi", "attack")
    with pytest.raises(GameError, match="Catch your breath"):
        await service.boss_action(boss.id, 99, "Rafi", "attack")


async def test_boss_spell_requires_mana(service: GameService):
    boss = await service.start_boss(1, 2, 99, "Rafi", "a burning archive")
    boss.player(99).mana = 0
    await service.store.save(boss)
    with pytest.raises(GameError, match="Not enough mana"):
        await service.boss_action(boss.id, 99, "Rafi", "spell")


async def test_boss_sessions_are_per_guild(service: GameService):
    await service.start_boss(1, 2, 99, "Rafi", "a ruined keep")
    with pytest.raises(GameError, match="already attacking"):
        await service.start_boss(1, 3, 100, "Mita", "a frozen lake")
    other_guild = await service.start_boss(2, 3, 100, "Mita", "a frozen lake")
    assert other_guild.guild_id == 2


async def test_sessions_persist_across_reload(service: GameService):
    state = await service.start_rpg(1, 2, 99, "Rafi", "a clockwork forest")
    loaded = await service.store.get(state.id)
    assert isinstance(loaded, AdventureState)
    assert loaded.title == state.title
    assert loaded.choices[0].label == state.choices[0].label


async def test_end_session_requires_owner(service: GameService):
    state = await service.start_rpg(1, 2, 99, "Rafi", "a silent monastery")
    with pytest.raises(GameError):
        await service.end_session(state.id, 1000, 1)
    await service.end_session(state.id, 99, 1)
    assert (await service.store.get(state.id)).status == "abandoned"


# ---------------------------------------------------------------------------
# Rendering / components
# ---------------------------------------------------------------------------
def test_render_and_components_are_constructible():
    from features.games.render import (
        adventure_embed,
        boss_embed,
        boss_turn_embed,
        detective_embed,
        game_status_embed,
        help_embed,
        interrogation_embed,
        verdict_embed,
    )

    class FakeCog:
        pass

    story = Storyteller(gemini=OfflineGemini(), rng=random.Random(11))
    adventure = AdventureState(
        id="adv1",
        guild_id=1,
        channel_id=2,
        owner_id=3,
        owner_name="Rafi",
        theme="a ruined city",
        title="The Ruined City",
        scene_no=0,
        scene="You stand at a gate.",
        choices=[Choice("Enter", "risky"), Choice("Wait", "safe")],
        hp=80,
        gold=12,
        inventory=["lantern"],
    )
    view = RPGView(FakeCog(), adventure)
    assert len(view.children) == 2
    assert all(child.custom_id.startswith("ab:rpg:") for child in view.children)
    assert adventure_embed(adventure).fields

    case = story.local_case("a quiet village")
    detective = DetectiveState(
        id="det1",
        guild_id=1,
        channel_id=2,
        owner_id=3,
        owner_name="Rafi",
        theme="a quiet village",
        title=case.title,
        victim=case.victim,
        location=case.location,
        time=case.time,
        weapon=case.weapon,
        summary=case.summary,
        suspects=case.suspects,
        transcript={s.name: [] for s in case.suspects},
    )
    det_view = DetectiveView(FakeCog(), detective)
    assert len(det_view.children) == 4
    assert detective_embed(detective).fields
    assert interrogation_embed(
        detective, case.suspects[0].name, case.suspects[0].role,
        "Where?", "I was home.", "Mita", "local", 0,
    ).description
    assert verdict_embed(detective, False).title

    boss = BossState(
        id="boss1", guild_id=1, channel_id=2, owner_id=3, theme="t",
        name="The Hollow King", lore="A king of dust.", hp=120, max_hp=400,
    )
    boss.add_player(BossPlayer(user_id=3, name="Rafi", damage_dealt=40, gold=20))
    boss_view = BossView(FakeCog(), boss)
    assert len(boss_view.children) == 4
    assert boss_embed(boss).fields
    assert boss_turn_embed(
        BossTurn(state=boss, narration="A hit lands.", player_damage=14, boss_damage=9)
    ).description

    status = game_status_embed({"rpg": 1, "detective": 0, "boss": 1}, boss,
                               enabled=True, ai_available=False)
    assert "AI Games" in status.title
    assert help_embed().fields
    assert build_view(FakeCog(), adventure) is not None
    adventure.status = "finished"
    assert build_view(FakeCog(), adventure) is None


def test_resolve_suspect_accepts_numbers_and_names():
    state = DetectiveState(
        id="d", guild_id=1, channel_id=2, owner_id=3, owner_name="R", theme="t",
        title="T", victim="V", location="L", time="T", weapon="W", summary="S",
        suspects=[
            Suspect(name="Nusrat Jahan", role="assistant", profile="p", alibi="a", secret="s"),
            Suspect(name="Imran Chowdhury", role="librarian", profile="p", alibi="a", secret="s"),
            Suspect(name="Farhana Rahman", role="head", profile="p", alibi="a", secret="s"),
        ],
    )
    assert GameService.resolve_suspect(state, "2 Where were you?") == (1, "Where were you?")
    assert GameService.resolve_suspect(state, "suspect 3") == (2, "")
    assert GameService.resolve_suspect(state, "Nusrat What did you see?") == (0, "What did you see?")
    assert GameService.resolve_suspect(state, "Farhana Rahman") == (2, "")
    assert GameService.resolve_suspect(state, "9 nowhere") == (None, "")
    assert GameService.resolve_suspect(state, "Nobody") == (None, "")


async def test_storyteller_uses_gemini_when_it_returns_valid_json():
    gemini = ScriptedGemini(
        payload={
            "title": "The Glass Orchard",
            "scene": "Glass fruit hangs from black branches.",
            "choices": [
                {"label": "Pick one", "hint": "shine"},
                {"label": "Leave", "hint": "safety"},
            ],
        },
        text="The blade sings. Something answers.",
    )
    story = Storyteller(gemini=gemini, rng=random.Random(4))
    turn = await story.start_adventure("a glass orchard", "Rafi")
    assert turn.source == "gemini"
    assert turn.title == "The Glass Orchard"
    assert len(turn.choices) == 2
    assert gemini.calls == 1

    state = AdventureState(
        id="a", guild_id=1, channel_id=2, owner_id=3, owner_name="Rafi",
        theme="a glass orchard", title=turn.title, scene_no=1, scene=turn.scene,
        choices=[Choice(**c) for c in turn.choices],
    )
    next_turn = await story.next_adventure(state, {"label": "Pick one"})
    assert next_turn.source == "gemini"
    assert next_turn.title == state.title

    gemini.payload = {
        "title": "The Glass Orchard Murder",
        "victim": "Orchard keeper",
        "location": "the glass orchard",
        "time": "midnight",
        "weapon": "a shard of glass",
        "summary": "Someone broke the keeper along with the glass.",
        "suspects": [
            {"name": "A", "role": "gardener", "profile": "quiet", "alibi": "home", "secret": "debt", "is_killer": False},
            {"name": "B", "role": "trader", "profile": "loud", "alibi": "market", "secret": "theft", "is_killer": True},
            {"name": "C", "role": "cousin", "profile": "kind", "alibi": "inn", "secret": "letter", "is_killer": False},
        ],
    }
    case = await story.generate_case("a glass orchard")
    assert case.source == "gemini"
    assert len(case.suspects) == 3
    assert sum(s.is_killer for s in case.suspects) == 1

    detective = DetectiveState(
        id="d", guild_id=1, channel_id=2, owner_id=3, owner_name="Rafi",
        theme="a glass orchard", title=case.title, victim=case.victim,
        location=case.location, time=case.time, weapon=case.weapon,
        summary=case.summary, suspects=case.suspects,
        transcript={s.name: [] for s in case.suspects},
    )
    reply = await story.interrogate(detective, case.suspects[0], "Where were you?", "Mita")
    assert reply.source == "gemini"
    assert reply.answer == gemini.text


async def test_storyteller_falls_back_when_gemini_returns_garbage():
    gemini = ScriptedGemini(payload={"scene": "", "choices": []})
    story = Storyteller(gemini=gemini, rng=random.Random(5))
    turn = await story.start_adventure("a silent sea", "Rafi")
    assert turn.source == "local"
    assert turn.choices

    case = await story.generate_case("a silent sea")
    assert case.source == "local"


async def test_disabled_games_refuse_to_start(service: GameService):
    service.settings = dataclasses.replace(service.settings, enabled=False)
    with pytest.raises(GameError, match="disabled"):
        await service.start_rpg(1, 2, 99, "Rafi", "a quiet forest")
    with pytest.raises(GameError, match="disabled"):
        await service.start_detective(1, 2, 99, "Rafi", "a quiet forest")
    with pytest.raises(GameError, match="disabled"):
        await service.start_boss(1, 2, 99, "Rafi", "a quiet forest")
