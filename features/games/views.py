"""Persistent Discord components for the games."""

from __future__ import annotations

import functools
import logging
from typing import Any

import discord

from core.utils import truncate
from features.games.models import AdventureState, BossState, DetectiveState
from features.games.render import interrogation_embed, verdict_embed
from features.games.service import GameError

log = logging.getLogger(__name__)


class BaseGameView(discord.ui.View):
    def __init__(self, cog: Any, session_id: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.session_id = session_id


class RPGView(BaseGameView):
    """One button per choice; custom IDs embed the scene to reject stale clicks."""

    def __init__(self, cog: Any, state: AdventureState):
        super().__init__(cog, state.id)
        self.scene_no = state.scene_no
        emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
        styles = [discord.ButtonStyle.primary, discord.ButtonStyle.secondary, discord.ButtonStyle.success]
        for index, choice in enumerate(state.choices[:4]):
            button = discord.ui.Button(
                label=truncate(choice.label, 80),
                emoji=emojis[index % len(emojis)],
                style=styles[index % len(styles)],
                custom_id=f"ab:rpg:{state.id}:{state.scene_no}:{index}",
            )
            button.callback = functools.partial(self._choose, index=index)
            self.add_item(button)

    async def _choose(self, interaction: discord.Interaction, index: int) -> None:
        await self.cog.handle_rpg_choice(
            interaction, self.session_id, self.scene_no, index
        )


class InterrogateModal(discord.ui.Modal):
    def __init__(self, cog: Any, state: DetectiveState, index: int):
        suspect = state.suspects[index]
        super().__init__(title=truncate(f"Question {suspect.name}", 45))
        self.cog = cog
        self.session_id = state.id
        self.index = index
        self.question = discord.ui.TextInput(
            label="Your question",
            placeholder="Where were you when it happened?",
            min_length=3,
            max_length=300,
            style=discord.TextStyle.paragraph,
            required=True,
        )
        self.add_item(self.question)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        try:
            state, suspect, reply = await self.cog.service.interrogate(
                self.session_id,
                interaction.user.id,
                self.index,
                str(self.question.value),
                interaction.user.display_name,
            )
        except GameError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Interrogation failed: %s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.followup.send(
                "🎲 The suspect clammed up. Please try again.", ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=interrogation_embed(
                state=state,
                suspect_name=suspect.name,
                suspect_role=suspect.role,
                question=str(self.question.value),
                answer=reply.answer,
                asked_by=interaction.user.display_name,
                source=reply.source,
                index=self.index,
            )
        )


class AccuseSelect(discord.ui.Select):
    def __init__(self, view: "AccuseView", state: DetectiveState):
        options = [
            discord.SelectOption(
                label=truncate(suspect.name, 95),
                value=str(index),
                description=truncate(f"{suspect.role} · {suspect.alibi}", 95) or None,
            )
            for index, suspect in enumerate(state.suspects)
        ]
        super().__init__(
            placeholder="Who killed the victim?",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"ab:accuse:{state.id}",
        )
        self.view_ref = view

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view_ref.accuse(interaction, int(self.values[0]))


class AccuseView(discord.ui.View):
    def __init__(self, cog: Any, state: DetectiveState):
        super().__init__(timeout=120)
        self.cog = cog
        self.session_id = state.id
        self.add_item(AccuseSelect(self, state))

    async def accuse(self, interaction: discord.Interaction, index: int) -> None:
        await interaction.response.defer()
        try:
            state, correct, _ = await self.cog.service.accuse(
                self.session_id, interaction.user.id, index
            )
        except GameError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Accusation failed: %s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
            )
            await interaction.followup.send(
                "🎲 The case file slipped away. Please try again.", ephemeral=True
            )
            return
        verdict = verdict_embed(state, correct)
        await interaction.followup.send(embed=verdict)
        try:
            await interaction.edit_original_response(
                content="Verdict posted below.", embed=None, view=None
            )
        except discord.HTTPException:
            pass


class DetectiveView(BaseGameView):
    def __init__(self, cog: Any, state: DetectiveState):
        super().__init__(cog, state.id)
        for index, suspect in enumerate(state.suspects[:3]):
            button = discord.ui.Button(
                label=truncate(f"{index + 1}. {suspect.name}", 80),
                emoji="🎙️",
                style=discord.ButtonStyle.secondary,
                custom_id=f"ab:det:{state.id}:{index}",
            )
            button.callback = functools.partial(self._question, index=index)
            self.add_item(button)
        accuse = discord.ui.Button(
            label="Accuse",
            emoji="⚖️",
            style=discord.ButtonStyle.danger,
            custom_id=f"ab:det-accuse:{state.id}",
            row=1,
        )
        accuse.callback = self._accuse
        self.add_item(accuse)

    async def _question(self, interaction: discord.Interaction, index: int) -> None:
        if interaction.user.bot:
            return
        try:
            state = await self.cog.service._load(self.session_id)
        except GameError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if not isinstance(state, DetectiveState) or state.over:
            await interaction.response.send_message(
                "This case is closed — start a new one with `/game detective`.", ephemeral=True
            )
            return
        await interaction.response.send_modal(InterrogateModal(self.cog, state, index))

    async def _accuse(self, interaction: discord.Interaction) -> None:
        try:
            state = await self.cog.service._load(self.session_id)
        except GameError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if not isinstance(state, DetectiveState) or state.over:
            await interaction.response.send_message(
                "This case is already closed.", ephemeral=True
            )
            return
        if interaction.user.id != state.owner_id:
            await interaction.response.send_message(
                f"Only <@{state.owner_id}> can make the final accusation.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Who do you accuse? This costs one attempt.",
            view=AccuseView(self.cog, state),
            ephemeral=True,
        )


class BossView(BaseGameView):
    def __init__(self, cog: Any, state: BossState):
        super().__init__(cog, state.id)
        specs = [
            ("attack", "Attack", "⚔️", discord.ButtonStyle.danger),
            ("spell", "Spell", "✨", discord.ButtonStyle.primary),
            ("heal", "Heal", "💚", discord.ButtonStyle.success),
            ("join", "Join", "🛡️", discord.ButtonStyle.secondary),
        ]
        for action, label, emoji, style in specs:
            button = discord.ui.Button(
                label=label,
                emoji=emoji,
                style=style,
                custom_id=f"ab:boss:{state.id}:{action}",
            )
            button.callback = functools.partial(self._act, action=action)
            self.add_item(button)

    async def _act(self, interaction: discord.Interaction, action: str) -> None:
        await self.cog.handle_boss_action(interaction, self.session_id, action)


def build_view(cog: Any, state: Any) -> discord.ui.View | None:
    if isinstance(state, AdventureState) and not state.over:
        return RPGView(cog, state)
    if isinstance(state, DetectiveState) and not state.over:
        return DetectiveView(cog, state)
    if isinstance(state, BossState) and not state.over:
        return BossView(cog, state)
    return None
