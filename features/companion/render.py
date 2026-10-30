"""Presentation helpers for the companion and Q&A commands."""

from __future__ import annotations

import discord

from core.discord_ui import BRAND, SUCCESS, WARNING, embed
from core.utils import split_message, truncate

COMPANION_COLOR = discord.Color.from_str("#FF7AB6")
MAX_EMBED_CHARS = 3400
MAX_ANSWER_PARTS = 6


def split_markdown(text: str, limit: int = MAX_EMBED_CHARS) -> list[str]:
    """Split prose near line boundaries, closing and reopening code fences."""
    text = (text or "").strip()
    if not text:
        return []
    limit = max(200, limit)
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buffer: list[str] = []
    in_code = False
    fence = "```"

    def flush() -> None:
        nonlocal buffer
        if not buffer:
            return
        block = list(buffer)
        if in_code:
            block.append("```")
        rendered = "\n".join(block).strip("\n")
        if rendered.strip():
            chunks.append(rendered)
        buffer = [fence] if in_code else []

    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if len(line) > limit - 20:
            flush()
            for piece in split_message(line, max(100, limit - 20)):
                if in_code:
                    chunks.append(f"{fence}\n{piece}\n```")
                else:
                    chunks.append(piece)
            buffer = [fence] if in_code else []
            continue

        projected = len("\n".join(buffer + [line])) if buffer else len(line)
        reserve = 5 if in_code else 0
        if buffer and projected + reserve > limit:
            flush()
        buffer.append(line)

        stripped = line.strip()
        if stripped.startswith("```"):
            if in_code:
                in_code = False
                fence = "```"
            else:
                in_code = True
                fence = stripped[:20]

    if buffer:
        if in_code:
            buffer.append("```")
        rendered = "\n".join(buffer).strip("\n")
        if rendered.strip():
            chunks.append(rendered)
    return [chunk for chunk in chunks if chunk.strip()]


def ask_embeds(question: str, answer: str, model: str | None) -> list[discord.Embed]:
    all_chunks = split_markdown(answer, MAX_EMBED_CHARS)
    chunks = all_chunks[:MAX_ANSWER_PARTS]
    if len(all_chunks) > MAX_ANSWER_PARTS:
        chunks[-1] += "\n\n… *(answer truncated — ask a narrower question for the rest)*"
    if not chunks:
        chunks = ["*(empty answer)*"]
    total = len(chunks)
    flat_question = " ".join(question.split())
    embeds: list[discord.Embed] = []
    for index, chunk in enumerate(chunks):
        if index == 0:
            title = f"🧠 {truncate(flat_question, 210)}"
        else:
            title = f"🧠 Answer · part {index + 1}/{total}"
        part = f" · part {index + 1}/{total}" if total > 1 else ""
        embeds.append(
            embed(
                title=title,
                description=chunk,
                color=BRAND,
                footer=f"AutoBot Q&A · {model or 'Gemini'}{part}",
            )
        )
    return embeds


def why_no_answer_embed() -> discord.Embed:
    return embed(
        title="🧠 AI brain is not configured",
        description=(
            "I need a Gemini API key for smart answers.\n\n"
            "1. Get a free key: https://aistudio.google.com/apikey\n"
            "2. Open the **`.env`** file in VS Code and add:\n"
            "```\nGEMINI_API_KEY=your_key_here\n```\n"
            "3. Restart the bot with `python main.py`.\n\n"
            "The key goes in the file — not directly in the terminal."
        ),
        color=WARNING,
    )


def gf_help_embed(gf_name: str) -> discord.Embed:
    return embed(
        title=f"💖 {gf_name} — your AI companion",
        description=(
            f"Chat with {gf_name}: a warm, caring AI companion with a memory for your "
            "conversation. She replies in Bangla, Banglish or English, whichever you use."
        ),
        color=COMPANION_COLOR,
        fields=[
            (
                "Start chatting",
                "`!gf Sokal sola ki korcho?`\n"
                "`/gf message:Sokal sola ki korcho? private:true`",
                False,
            ),
            (
                "Memory",
                f"Your last few chats are remembered so {gf_name} can follow up naturally. "
                "They are stored per user in the bot database and expire automatically.",
                False,
            ),
            (
                "Reset",
                "`!gfreset` or `/gfreset` clears your saved chat memory.",
                False,
            ),
            (
                "Ask anything",
                "`!ask Quantum computing ki r eta kivabe kaj kore?`\n"
                "`/ask question:Explain photosynthesis like I'm 15`",
                False,
            ),
            (
                "Good to know",
                "• AI companion, not a human — she will say so if asked.\n"
                "• Wholesome and PG-13; no manipulation or pressure.\n"
                "• `!gf`, `/gf`, `!ask` and `/ask` all work in the bot's DMs.\n"
                "• In a server, `private:true` makes the reply ephemeral; in a DM it is already private.",
                False,
            ),
        ],
        footer=f"💖 {gf_name} · AI companion · Gemini-powered",
    )


def gf_reset_embed(gf_name: str, cleared: bool) -> discord.Embed:
    return embed(
        title="💫 Memory cleared" if cleared else "💫 Nothing to clear",
        description=(
            "Okay, I've forgotten our saved chat history. Let's start fresh 🌸"
            if cleared
            else "I don't have any saved chat history for you yet."
        ),
        color=SUCCESS if cleared else COMPANION_COLOR,
        footer=f"💖 {gf_name}",
    )


def ask_help_embed() -> discord.Embed:
    return embed(
        title="🧠 Smart Q&A",
        description=(
            "Ask a question and get a detailed, structured answer — no tab-switching."
        ),
        color=BRAND,
        fields=[
            (
                "Examples",
                "`!ask Quantum computing ki r eta kivabe kaj kore?`\n"
                "`/ask question:Explain the difference between TCP and UDP`\n"
                "`!ask Write a Python function to reverse a linked list`",
                False,
            ),
            (
                "Language",
                "Bangla, Banglish or English — the answer follows your question.",
                False,
            ),
            (
                "Privacy",
                "Questions and answers are not saved by the Q&A command.",
                False,
            ),
        ],
        footer="🧠 AutoBot Q&A · Gemini-powered",
    )
