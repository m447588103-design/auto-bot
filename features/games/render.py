"""Discord embeds for the game features."""

from __future__ import annotations

import discord

from core.discord_ui import CINEMA, LEAK, SUCCESS, WARNING, embed
from core.utils import progress_bar, truncate
from features.games.models import AdventureState, BossState, DetectiveState
from features.games.service import BossTurn

RPG_COLOR = discord.Color.from_str("#9B59B6")
DETECTIVE_COLOR = discord.Color.from_str("#E67E22")
BOSS_COLOR = discord.Color.from_str("#ED4245")

SOURCE_LABEL = {"gemini": "✨ Gemini narrative", "local": "📜 offline script"}


def _bar(current: int, maximum: int, width: int = 12) -> str:
    maximum = max(1, maximum)
    percent = max(0, min(100, current / maximum * 100))
    return progress_bar(percent, width)


def _source(source: str) -> str:
    return SOURCE_LABEL.get(source, SOURCE_LABEL["local"])


def adventure_embed(state: AdventureState) -> discord.Embed:
    if state.status == "dead":
        color, header = LEAK, "☠️ Your adventure ends"
    elif state.status == "finished":
        color, header = SUCCESS, "🏁 Adventure complete"
    else:
        color, header = RPG_COLOR, "🎲 Choose your path"

    inventory = ", ".join(state.inventory) or "empty"
    description = truncate(state.scene, 3800)
    fields = [
        ("❤️ HP", f"`{_bar(state.hp, state.max_hp)}` {state.hp}/{state.max_hp}", True),
        ("💰 Gold", f"**{state.gold}**", True),
        ("🎒 Inventory", truncate(inventory, 400), True),
    ]
    if state.status == "active":
        hint = "\n".join(
            f"**{i + 1}.** {choice.hint or '—'}" for i, choice in enumerate(state.choices)
        )
        fields.append(("What next?", hint or "Choose a button below.", False))
    footer = (
        f"{header} · scene {state.scene_no + 1} · {state.owner_name} · "
        f"{_source(state.source)}"
    )
    return embed(
        title=truncate(f"{header} — {state.title}", 240),
        description=description,
        color=color,
        fields=fields,
        footer=footer,
    )


def detective_embed(state: DetectiveState) -> discord.Embed:
    if state.status == "solved":
        color, label = SUCCESS, "✅ Case solved"
    elif state.status == "failed":
        color, label = LEAK, "❌ Case closed"
    else:
        color, label = DETECTIVE_COLOR, "🕵️ The case"

    fields = [
        ("Victim", state.victim, True),
        ("Scene", f"{state.location}\n{state.time}", True),
        ("Weapon", state.weapon, True),
    ]
    for index, suspect in enumerate(state.suspects):
        fields.append(
            (
                f"Suspect {index + 1} · {truncate(suspect.name, 120)}",
                f"*{truncate(suspect.role, 100)}*\n"
                f"{truncate(suspect.profile, 300)}\n"
                f"🕗 {truncate(suspect.alibi, 200)}",
                False,
            )
        )
    fields.append(
        (
            "How to play",
            "Use `/game interrogate` (or `!interrogate 2 <question>`) to question a suspect. "
            "When you are sure, `/game accuse` (or `!accuse 2`).",
            False,
        )
    )
    footer = (
        f"{label} · questions {state.interrogations} · accusations left "
        f"{state.attempts_left} · {_source(state.source)}"
    )
    return embed(
        title=truncate(f"🔎 {state.title}", 240),
        description=truncate(state.summary, 3000),
        color=color,
        fields=fields,
        footer=footer,
    )


def interrogation_embed(
    state: DetectiveState,
    suspect_name: str,
    suspect_role: str,
    question: str,
    answer: str,
    asked_by: str,
    source: str,
    index: int,
) -> discord.Embed:
    return embed(
        title=truncate(f"🎙️ {suspect_name} · suspect {index + 1}", 240),
        description=f"**{asked_by} asks:** {truncate(question, 500)}\n\n"
        f"**{suspect_name}:** {truncate(answer, 2800)}",
        color=DETECTIVE_COLOR,
        fields=[
            ("Role", truncate(suspect_role, 200), True),
            ("Questions so far", f"{state.interrogations}", True),
            ("Case", truncate(state.title, 200), True),
        ],
        footer=f"🎙️ Interrogation · {_source(source)}",
    )


def verdict_embed(state: DetectiveState, correct: bool) -> discord.Embed:
    if correct:
        title = "✅ You named the killer"
        description = (
            f"**{state.killer or 'The killer'}** — caught.\n\n"
            f"{truncate(_reveal(state), 2500)}"
        )
        color = SUCCESS
    elif state.status == "failed":
        title = "❌ Wrong — and out of accusations"
        description = (
            "The killer smiles, walks out of the station, and disappears into the rain.\n\n"
            f"{truncate(_reveal(state), 2500)}"
        )
        color = LEAK
    else:
        title = "❌ Not them"
        description = (
            f"That suspect has a solid alibi. **{state.attempts_left}** accusation(s) left — "
            "read the transcripts again."
        )
        color = WARNING
    return embed(
        title=title,
        description=description,
        color=color,
        fields=[("Case", state.title, True), ("Victim", state.victim, True)],
        footer=f"🕵️ {state.owner_name} · {_source(state.source)}",
    )


def _reveal(state: DetectiveState) -> str:
    killer = next((s for s in state.suspects if s.is_killer), None)
    if killer is None:
        return "The file is empty."
    return (
        f"**{killer.name}** ({killer.role}) killed {state.victim} with {state.weapon} "
        f"at {state.location}. Motive: {killer.secret}"
    )


def boss_embed(state: BossState) -> discord.Embed:
    if state.status == "victory":
        color, label = SUCCESS, "🏆 Raid cleared"
    elif state.status == "defeat":
        color, label = LEAK, "💀 Raid wiped"
    else:
        color, label = BOSS_COLOR, "⚔️ Boss raid live"

    fighters = sorted(state.players.values(), key=lambda p: p.damage_dealt, reverse=True)
    if fighters:
        lines = []
        for player in fighters[:8]:
            status = "💀 down" if not player.alive else f"❤️ {player.hp}/{player.max_hp}"
            lines.append(
                f"**{player.name}** — {status} · ⚔️ {player.damage_dealt} dmg · "
                f"🔵 {player.mana} mana · 💰 {player.gold}"
            )
        if len(fighters) > 8:
            lines.append(f"…and {len(fighters) - 8} more")
        fighter_text = "\n".join(lines)
    else:
        fighter_text = "No one has joined yet."

    fields = [
        (
            "Boss HP",
            f"`{_bar(state.hp, state.max_hp, 16)}` **{state.hp:,}/{state.max_hp:,}**",
            False,
        ),
        ("Phase", f"**{state.phase}** · round {state.round}", True),
        ("Fighters", f"**{len(state.players)}** · alive {len(state.alive_players)}", True),
        ("Party", fighter_text, False),
    ]
    if state.status == "victory":
        mvp = max(state.players.values(), key=lambda p: p.damage_dealt, default=None)
        if mvp:
            fields.append(("MVP", f"**{mvp.name}** — {mvp.damage_dealt} damage", False))
        fields.append(
            ("Rewards", "Everyone who fought earned gold and a story worth retelling.", False)
        )
    elif state.status == "defeat":
        fields.append(
            ("Aftermath", "The raid fell. The boss remembers your names now.", False)
        )
    else:
        fields.append(
            (
                "Commands",
                "`!attack` · `!spell` · `!heal` — or use the buttons. "
                "Boss retaliates every second action.",
                False,
            )
        )
    footer = (
        f"{label} · {len(state.players)} fighters · actions {state.actions} · "
        f"{_source(state.source)}"
    )
    return embed(
        title=truncate(f"⚔️ {state.name}", 240),
        description=truncate(state.lore, 3000),
        color=color,
        fields=fields,
        footer=footer,
    )


def boss_turn_embed(turn: BossTurn) -> discord.Embed:
    state = turn.state
    if state.status == "victory":
        color = SUCCESS
    elif state.status == "defeat":
        color = LEAK
    elif turn.crit:
        color = CINEMA
    else:
        color = BOSS_COLOR

    fields = [
        ("Boss HP", f"`{_bar(state.hp, state.max_hp, 16)}` **{state.hp:,}/{state.max_hp:,}**", False),
        ("Hit", f"**{turn.player_damage or 0}** damage" + (" · 💥 CRIT" if turn.crit else ""), True),
    ]
    if turn.healed:
        fields.append(("Healing", f"**{turn.healed}** HP", True))
    if turn.boss_damage:
        fields.append(
            (
                "Boss hit back",
                f"**{turn.boss_damage}** damage"
                + (" to everyone" if turn.special else ""),
                True,
            )
        )
    fields.append(("Round", f"{state.round} · phase {state.phase}", True))
    if turn.mvp_name:
        fields.append(("MVP", turn.mvp_name, False))
    return embed(
        title=truncate(f"⚔️ {state.name} · round {state.round}", 240),
        description=truncate(turn.narration, 3200),
        color=color,
        fields=fields,
        footer=f"⚔️ {len(state.players)} fighters · {_source(turn.source)}",
    )


def game_status_embed(
    counts: dict[str, int],
    boss: BossState | None,
    *,
    enabled: bool,
    ai_available: bool,
) -> discord.Embed:
    fields = [
        ("Adventures", f"{counts.get('rpg', 0)} active", True),
        ("Mysteries", f"{counts.get('detective', 0)} open", True),
        ("Boss raids", f"{counts.get('boss', 0)} active", True),
    ]
    if boss is not None:
        fields.append(
            (
                "Current boss",
                f"**{boss.name}** — {boss.hp:,}/{boss.max_hp:,} HP · round {boss.round}",
                False,
            )
        )
    return embed(
        title="🎮 AI Games",
        description=(
            "Choose-your-own-adventure RPG, AI murder mysteries, and server-wide boss raids."
            if enabled
            else "Games are currently disabled (`GAMES_ENABLED=false`)."
        ),
        color=RPG_COLOR,
        fields=fields,
        footer=f"🎮 AutoBot games · {'Gemini storytelling on' if ai_available else 'offline mode'}",
    )


def help_embed() -> discord.Embed:
    return embed(
        title="🎮 AI Games — commands",
        description=(
            "Three Gemini-powered games with buttons. Everything also works with prefix commands."
        ),
        color=RPG_COLOR,
        fields=[
            (
                "🎲 Adventure RPG",
                "`/game rpg theme:<...>` · `!rpg <theme>`\n"
                "`/game choose number:<1-3>` · `!choose 2` · `/game quit`",
                False,
            ),
            (
                "🔎 Detective",
                "`/game detective theme:<...>` · `!detective <theme>`\n"
                "`/game interrogate suspect:<1-3> question:<...>` · `!interrogate 2 Where were you?`\n"
                "`/game accuse suspect:<1-3>` · `!accuse 2`",
                False,
            ),
            (
                "⚔️ Boss raid",
                "`/game bossfight theme:<...>` · `!bossfight <theme>`\n"
                "`/game join` · `!join`\n"
                "`/game attack` · `!attack` · `/game spell` · `!spell` · `/game heal` · `!heal`\n"
                "`/game status` · `!gamestatus`",
                False,
            ),
            (
                "Good to know",
                "• Shortcuts: `/rpg`, `/detective`, `/bossfight` (same as `/game ...`).\n"
                "• `/game quit` ends your RPG/case; a raid owner ends the whole raid.\n"
                "• Games persist across bot restarts (SQLite).\n"
                "• Sessions expire after inactivity.\n"
                "• Without `GEMINI_API_KEY` the games still run on built-in scripts.",
                False,
            ),
        ],
        footer="🎮 AutoBot games",
    )
