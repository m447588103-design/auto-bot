from __future__ import annotations

import pytest

from features.dj.mood import MoodPlan, plan_mood_rules, shuffle_queries


@pytest.mark.parametrize(
    "text,expected",
    [
        ("bhai amar khub sad mood, ektu lofi gan bajao", "sad & healing"),
        ("mon bhalo na, kichu kosto lage", "sad & healing"),
        ("মন খারাপ, একটু গান বাজাও", "sad & healing"),
        ("need hyperpop for coding", "focus / coding"),
        ("porasona korbo, focus music dao", "focus / coding"),
        ("gym time, phonk chai", "workout"),
        ("bristi porche, chill vibe dao", "chill"),
        ("party mood, dhamaka chai", "hype & party"),
        ("ghoom asche na, raat holo", "sleep & calm"),
        ("purano gaan shunte mon chay", "nostalgic"),
    ],
)
def test_rule_engine_banglish_and_bangla(text, expected):
    plan = plan_mood_rules(text)
    assert plan.mood == expected
    assert plan.queries
    assert 0.0 <= plan.energy <= 1.0


def test_explicit_genre_pins_query():
    plan = plan_mood_rules("bhai hyperpop bajao")
    assert "hyperpop" in plan.genres
    assert any("hyperpop" in query for query in plan.queries)


def test_freeform_message_becomes_search_query():
    plan = plan_mood_rules("play something like tame impala but bengali")
    assert plan.mood == "custom vibe"
    assert "tame impala" in plan.queries[0]
    assert plan.source == "rules"


def test_empty_message_returns_default_mix():
    plan = plan_mood_rules("")
    assert plan.mood
    assert len(plan.queries) >= 2


def test_energy_clamped_and_plan_serialisable():
    plan = plan_mood_rules("party hard gym hype")
    assert 0.0 <= plan.energy <= 1.0
    copy = MoodPlan(**plan.__dict__)
    assert copy.all_queries() == plan.all_queries()


def test_shuffle_queries_returns_subset():
    plan = plan_mood_rules("sad lofi")
    queries = shuffle_queries(plan, k=2)
    assert len(queries) <= 2
    assert all(query in plan.all_queries() for query in queries)


async def test_ai_plan_uses_fallback_without_gemini():
    from core.gemini import GeminiClient
    from features.dj.mood import plan_mood

    client = GeminiClient(api_key=None)
    plan = await plan_mood("mon bhalo na", client)
    assert plan.source == "rules"
    assert plan.mood == "sad & healing"
