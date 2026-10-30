"""Offline tests for the AI girlfriend and smart Q&A companion."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.config import CompanionSettings
from core.db import Database
from core.gemini import GeminiError
from features.companion.render import (
    ask_embeds,
    gf_help_embed,
    gf_reset_embed,
    split_markdown,
    why_no_answer_embed,
)
from features.companion.service import CompanionError, CompanionService
from features.companion.store import CompanionStore


def make_settings(**overrides) -> CompanionSettings:
    base = CompanionSettings(
        enabled=True,
        gf_enabled=True,
        ask_enabled=True,
        gf_name="Maya",
        gf_persona="",
        gf_cooldown_seconds=0.0,
        ask_cooldown_seconds=0.0,
        gf_history_turns=8,
        gf_max_message_chars=700,
        ask_max_question_chars=2000,
        ask_max_output_tokens=1600,
        memory_ttl_days=30,
    )
    return dataclasses.replace(base, **overrides)


class FakeGemini:
    available = True

    def __init__(self, *texts: str, fail: bool = False):
        self.texts = list(texts) or ["A sweet little reply 💕"]
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    async def generate(self, prompt: str, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self.fail:
            raise GeminiError("boom")
        text = self.texts.pop(0) if len(self.texts) > 1 else self.texts[0]
        return SimpleNamespace(text=text, model="fake-model")


class OfflineGemini:
    available = False


async def make_service(tmp_path: Path, *, settings=None, gemini=None):
    db = Database(tmp_path / "companion.db")
    await db.connect()
    service = CompanionService(
        bot=SimpleNamespace(),
        db=db,
        settings=settings or make_settings(),
        gemini=gemini or FakeGemini(),
    )
    await service.setup()
    return service, db


# -- storage ---------------------------------------------------------------
async def test_store_round_trip_and_turn_cap(tmp_path: Path):
    db = Database(tmp_path / "memory.db")
    await db.connect()
    store = CompanionStore(db)
    await store.setup()
    try:
        await store.append(1, "one", "reply one", max_turns=2, max_chars=500)
        await store.append(1, "two", "reply two", max_turns=2, max_chars=500)
        history = await store.append(1, "three", "reply three", max_turns=2, max_chars=500)
        assert [item["content"] for item in history] == [
            "two",
            "reply two",
            "three",
            "reply three",
        ]
        assert await store.load(1) == history
        assert await store.count() == 1
    finally:
        await db.close()


async def test_store_clear_and_malformed_history(tmp_path: Path):
    db = Database(tmp_path / "memory.db")
    await db.connect()
    store = CompanionStore(db)
    await store.setup()
    try:
        assert await store.clear(99) is False
        await store.append(7, "hi", "hello", max_turns=4, max_chars=500)
        assert await store.clear(7) is True
        assert await store.load(7) == []

        await db.execute(
            "INSERT INTO companion_memory (user_id, history, updated_at) VALUES (?, ?, ?)",
            (8, "{not-json", "2026-01-01T00:00:00+00:00"),
        )
        assert await store.load(8) == []
    finally:
        await db.close()


async def test_store_purges_old_memories(tmp_path: Path):
    db = Database(tmp_path / "memory.db")
    await db.connect()
    store = CompanionStore(db)
    await store.setup()
    try:
        await store.append(1, "old", "gone", max_turns=2, max_chars=500)
        await store.append(2, "fresh", "here", max_turns=2, max_chars=500)
        await db.execute(
            "UPDATE companion_memory SET updated_at = ? WHERE user_id = 1",
            ("2020-01-01T00:00:00+00:00",),
        )
        assert await store.purge(30) == 1
        assert await store.load(1) == []
        assert len(await store.load(2)) == 2
    finally:
        await db.close()


# -- girlfriend -----------------------------------------------------------
async def test_girlfriend_reply_uses_persona_and_memory(tmp_path: Path):
    fake = FakeGemini("Ami bhalo achi! Tumi kemon acho? 🌸", "Tomake mone porechhe 💕")
    service, db = await make_service(tmp_path, gemini=fake)
    try:
        first = await service.gf_chat(11, "Rafi", "Sokal sola ki korcho?")
        assert first.source == "gemini"
        assert first.model == "fake-model"
        assert "bhalo achi" in first.text
        assert "Maya" in fake.calls[0]["system_instruction"]
        assert "PG-13" in fake.calls[0]["system_instruction"]

        second = await service.gf_chat(11, "Rafi", "Amar kotha mone ache?")
        assert "Sokal sola ki korcho?" in fake.calls[1]["prompt"]
        assert "Ami bhalo achi" in fake.calls[1]["prompt"]
        history = await service.store.load(11)
        assert [row["role"] for row in history] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]
        assert "Amar kotha mone ache?" in history[-2]["content"]
        assert "Tomake mone porechhe" in second.text
    finally:
        await db.close()


async def test_girlfriend_survives_missing_gemini_without_polluting_memory(tmp_path: Path):
    service, db = await make_service(tmp_path, gemini=OfflineGemini())
    try:
        reply = await service.gf_chat(12, "Nabila", "Kemon acho?")
        assert reply.source == "local"
        assert "offline" in reply.text.lower()
        assert "GEMINI_API_KEY" in reply.text
        assert await service.store.load(12) == []
    finally:
        await db.close()


async def test_girlfriend_errors_and_cooldown(tmp_path: Path):
    service, db = await make_service(
        tmp_path,
        settings=make_settings(gf_max_message_chars=100),
    )
    try:
        with pytest.raises(CompanionError, match="Write a message"):
            await service.gf_chat(13, "Rafi", '  ""  ')
        with pytest.raises(CompanionError, match="too long"):
            await service.gf_chat(13, "Rafi", "x" * 101)
    finally:
        await db.close()

    service, db = await make_service(
        tmp_path,
        settings=make_settings(gf_cooldown_seconds=60.0),
    )
    try:
        await service.gf_chat(13, "Rafi", "hello")
        with pytest.raises(CompanionError, match="One moment"):
            await service.gf_chat(13, "Rafi", "again")
    finally:
        await db.close()


async def test_girlfriend_reset_and_disabled(tmp_path: Path):
    service, db = await make_service(tmp_path)
    try:
        await service.gf_chat(14, "Rafi", "hi")
        assert await service.reset_memory(14) is True
        assert await service.reset_memory(14) is False
    finally:
        await db.close()

    service, db = await make_service(tmp_path, settings=make_settings(enabled=False))
    try:
        with pytest.raises(CompanionError, match="disabled"):
            await service.gf_chat(15, "Rafi", "hi")
        with pytest.raises(CompanionError, match="disabled"):
            await service.ask(15, "Rafi", "hi")
    finally:
        await db.close()


# -- smart Q&A ------------------------------------------------------------
async def test_ask_returns_detailed_answer_without_memory(tmp_path: Path):
    answer = "Quantum computing uses qubits.\n\n- Superposition\n- Entanglement"
    fake = FakeGemini(answer)
    service, db = await make_service(tmp_path, gemini=fake)
    try:
        reply = await service.ask(21, "Rafi", '  "Quantum computing ki?"  ')
        assert reply.text == answer
        assert reply.model == "fake-model"
        assert "expert Q&A assistant" in fake.calls[0]["system_instruction"]
        assert "not professional advice" in fake.calls[0]["system_instruction"]
        assert fake.calls[0]["max_output_tokens"] == 1600
        assert "Quantum computing ki?" in fake.calls[0]["prompt"]
        assert await service.store.count() == 0
    finally:
        await db.close()


async def test_ask_reports_missing_key_and_api_failure(tmp_path: Path):
    service, db = await make_service(tmp_path, gemini=OfflineGemini())
    try:
        with pytest.raises(CompanionError, match="GEMINI_API_KEY"):
            await service.ask(22, "Rafi", "What is TCP?")
    finally:
        await db.close()

    service, db = await make_service(tmp_path, gemini=FakeGemini(fail=True))
    try:
        with pytest.raises(CompanionError, match="request failed"):
            await service.ask(22, "Rafi", "What is TCP?")
    finally:
        await db.close()


async def test_ask_rejects_disabled_flag_and_empty_output(tmp_path: Path):
    service, db = await make_service(tmp_path, settings=make_settings(ask_enabled=False))
    try:
        with pytest.raises(CompanionError, match="disabled"):
            await service.ask(23, "Rafi", "hello")
    finally:
        await db.close()

    service, db = await make_service(tmp_path, gemini=FakeGemini("   "))
    try:
        with pytest.raises(CompanionError, match="empty answer"):
            await service.ask(23, "Rafi", "hello")
    finally:
        await db.close()


# -- presentation ---------------------------------------------------------
def test_split_markdown_keeps_code_fences_balanced():
    code = "\n".join(f"print('line {i}')" for i in range(160))
    text = f"Here is a long example:\n```python\n{code}\n```\nAnd a final note."
    chunks = split_markdown(text, 420)
    assert len(chunks) >= 3
    for chunk in chunks:
        assert len(chunk) <= 450
        assert chunk.count("```") % 2 == 0
    assert "final note" in chunks[-1]


def test_ask_embeds_chunk_long_answers():
    question = "Explain " + "very " * 80 + "long question"
    answer = "\n\n".join(f"Paragraph {i}: " + "detail " * 200 for i in range(6))
    embeds = ask_embeds(question, answer, "gemini-test")
    assert len(embeds) >= 2
    assert all(len(embed.description or "") <= 4096 for embed in embeds)
    assert "part 1/" in (embeds[0].footer.text or "")
    assert "gemini-test" in (embeds[0].footer.text or "")


def test_help_and_reset_embeds():
    help_embed = gf_help_embed("Maya")
    assert "Maya" in (help_embed.title or "")
    assert any("!gf" in (field.value or "") for field in help_embed.fields)
    assert len(help_embed.fields) >= 4
    assert "forgotten" in (gf_reset_embed("Maya", True).description or "")
    assert "Nothing" in gf_reset_embed("Maya", False).title
    assert "GEMINI_API_KEY" in (why_no_answer_embed().description or "")


# -- command helpers -------------------------------------------------------
class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeCtx:
    def __init__(self):
        self.author = SimpleNamespace(id=41, display_name="Rafi")
        self.replies: list[dict] = []
        self.sent: list[dict] = []

    def typing(self):
        return FakeTyping()

    async def reply(self, *args, **kwargs):
        self.replies.append({"args": args, **kwargs})

    async def send(self, *args, **kwargs):
        self.sent.append({"args": args, **kwargs})


class FakeResponse:
    def __init__(self):
        self.done = False
        self.defer_kwargs: dict = {}
        self.messages: list[dict] = []

    def is_done(self):
        return self.done

    async def defer(self, **kwargs):
        self.done = True
        self.defer_kwargs = kwargs

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)


class FakeFollowup:
    def __init__(self):
        self.messages: list[dict] = []

    async def send(self, *args, **kwargs):
        self.messages.append({"args": args, **kwargs})


class FakeInteraction:
    def __init__(self):
        self.user = SimpleNamespace(id=42, display_name="Nabila")
        self.response = FakeResponse()
        self.followup = FakeFollowup()


async def test_prefix_commands_reply_with_embeds_and_text(tmp_path: Path):
    from features.companion.cog import CompanionCog

    service, db = await make_service(tmp_path, gemini=FakeGemini("Hello there 💕"))
    try:
        cog = CompanionCog(bot=SimpleNamespace(), service=service)
        ctx = FakeCtx()
        await cog.gf_prefix.callback(cog, ctx, message="")
        assert "Maya" in (ctx.replies[0]["embed"].title or "")

        ctx = FakeCtx()
        await cog.gf_prefix.callback(cog, ctx, message='"Sokal sola ki korcho?"')
        assert ctx.replies[0]["args"][0] == "Hello there 💕"
        assert ctx.replies[0]["mention_author"] is False

        ctx = FakeCtx()
        await cog.ask_prefix.callback(cog, ctx, question="What is TCP?")
        assert "TCP" in (ctx.replies[0]["embed"].title or "")

        ctx = FakeCtx()
        await cog.gfreset_prefix.callback(cog, ctx)
        assert "Memory cleared" in (ctx.replies[0]["embed"].title or "")
    finally:
        await db.close()


async def test_slash_commands_defer_and_send_private(tmp_path: Path):
    from features.companion.cog import CompanionCog

    service, db = await make_service(tmp_path, gemini=FakeGemini("Hi slash 💕", "Slash answer"))
    try:
        cog = CompanionCog(bot=SimpleNamespace(), service=service)
        interaction = FakeInteraction()
        await cog.gf_slash.callback(cog, interaction, message="hello", private=True)
        assert interaction.response.defer_kwargs == {"thinking": True, "ephemeral": True}
        assert interaction.followup.messages[0]["ephemeral"] is True
        assert interaction.followup.messages[0]["args"][0] == "Hi slash 💕"

        interaction = FakeInteraction()
        await cog.ask_slash.callback(cog, interaction, question="Explain UDP", private=False)
        assert interaction.response.defer_kwargs == {"thinking": True, "ephemeral": False}
        assert "UDP" in (interaction.followup.messages[0]["embed"].title or "")

        interaction = FakeInteraction()
        await cog.gfreset_slash.callback(cog, interaction)
        assert interaction.followup.messages[0]["ephemeral"] is True
    finally:
        await db.close()
