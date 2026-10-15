"""Discord component wiring for elicitation cards.

The decision shapes, card copy, and answer mapping live in ``approvals`` (pure,
unit-testable). This module is the thin adapter that turns them into
``discord.ui`` components and feeds the resulting :class:`Verdict` back to the
waiting turn worker.

Views here are deliberately **non-persistent**: they hold the owner/session/
elicitation ids as attributes rather than packing them into a component
``custom_id`` (Discord caps that at 100 characters, which a session id plus an
elicitation id can exceed). A non-persistent view lives exactly as long as the
in-memory coordinator waiter it answers, so a restart drops both together.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord
from omnigent_bot_core.events import ElicitationRequest

from omnigent_discord.approvals import (
    NON_OWNER_NOTICE,
    Card,
    ClickTarget,
    Selections,
    Verdict,
    question_options,
)

_logger = logging.getLogger(__name__)

# Called with the user's answer; returns whether a live waiter received it.
VerdictSink = Callable[[Verdict], Awaitable[bool]]


def to_embed(card: Card) -> discord.Embed:
    """Render a :class:`Card` as a Discord embed."""
    embed = discord.Embed(
        title=card.title, description=card.description, color=discord.Color(card.color)
    )
    for name, value in card.fields:
        embed.add_field(name=name, value=value, inline=False)
    return embed


class _AnswerSelect(discord.ui.Select["ElicitationView"]):
    """One question's select menu; records the choice on the shared state."""

    def __init__(
        self,
        *,
        question_key: str,
        prompt: str,
        options: list[tuple[str, str, str | None]],
        multi: bool,
        selections: Selections,
        row: int,
    ) -> None:
        super().__init__(
            placeholder=prompt,
            min_values=0 if multi else 1,
            max_values=len(options) if multi else 1,
            options=[
                discord.SelectOption(label=label, value=value, description=description)
                for label, value, description in options
            ],
            row=row,
        )
        self._question_key = question_key
        self._multi = multi
        self._selections = selections

    async def callback(self, interaction: discord.Interaction) -> None:
        self._selections.record(self._question_key, list(self.values), multi=self._multi)
        # Acknowledge without changing the message: the selection is already
        # reflected in the menu, and the answer is only submitted by the button.
        await interaction.response.defer()


class ElicitationView(discord.ui.View):
    """Buttons (binary approval) or selects + Submit/Cancel (an ask).

    Enforces the per-session owner boundary in :meth:`interaction_check`, so a
    click from anyone else is refused before any verdict is delivered — the card
    is visible to the whole channel but only the owner can act on it.
    """

    def __init__(
        self,
        request: ElicitationRequest,
        target: ClickTarget,
        deliver: VerdictSink,
        *,
        timeout_seconds: float,
    ) -> None:
        super().__init__(timeout=timeout_seconds)
        self._request = request
        self._target = target
        self._deliver = deliver
        self._selections = Selections()
        if request.is_form:
            self._build_form()
        else:
            self._build_binary()

    # ── construction ─────────────────────────────────────────────────────
    def _build_binary(self) -> None:
        approve = discord.ui.Button(label="Approve", style=discord.ButtonStyle.success, row=0)
        approve.callback = self._on_approve  # type: ignore[method-assign]
        deny = discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger, row=0)
        deny.callback = self._on_deny  # type: ignore[method-assign]
        self.add_item(approve)
        self.add_item(deny)

    def _build_form(self) -> None:
        # ``is_renderable`` has already guaranteed the question/option counts fit
        # Discord's row and option caps, so each question gets its own row and
        # the buttons take the last one.
        for row, (question, options) in enumerate(
            zip(self._request.questions, question_options(self._request), strict=True)
        ):
            self.add_item(
                _AnswerSelect(
                    question_key=question.key,
                    prompt=question.question,
                    options=options,
                    multi=question.multi_select,
                    selections=self._selections,
                    row=row,
                )
            )
        row = len(self._request.questions)
        submit = discord.ui.Button(label="Submit", style=discord.ButtonStyle.success, row=row)
        submit.callback = self._on_submit  # type: ignore[method-assign]
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=row)
        cancel.callback = self._on_cancel  # type: ignore[method-assign]
        self.add_item(submit)
        self.add_item(cancel)

    # ── authorization ────────────────────────────────────────────────────
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) == self._target.owner_user_id:
            return True
        _logger.info(
            "Rejecting non-owner elicitation interaction elicitation_id=%s owner=%s clicker=%s",
            self._target.elicitation_id,
            self._target.owner_user_id,
            interaction.user.id,
        )
        await interaction.response.send_message(NON_OWNER_NOTICE, ephemeral=True)
        return False

    # ── callbacks ────────────────────────────────────────────────────────
    async def _on_approve(self, interaction: discord.Interaction) -> None:
        await self._settle(interaction, Verdict(accepted=True))

    async def _on_deny(self, interaction: discord.Interaction) -> None:
        await self._settle(interaction, Verdict(accepted=False))

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await self._settle(interaction, Verdict(accepted=False))

    async def _on_submit(self, interaction: discord.Interaction) -> None:
        missing = self._selections.missing(self._request)
        if missing:
            # Nudge privately and leave the card live, rather than sending the
            # agent a partial answer.
            await interaction.response.send_message(
                "Choose an answer for: " + ", ".join(missing), ephemeral=True
            )
            return
        await self._settle(
            interaction, Verdict(accepted=True, content=dict(self._selections.values))
        )

    async def _settle(self, interaction: discord.Interaction, verdict: Verdict) -> None:
        """Disable the controls, then hand the verdict to the turn worker.

        The controls are disabled first so a double-click can't deliver twice
        and the user sees the click land immediately; the card's final embed is
        written later by the controller, once the server confirms the
        resolution.
        """
        self._disable_all()
        try:
            await interaction.response.edit_message(view=self)
        except discord.HTTPException:
            _logger.warning(
                "Could not disable elicitation controls elicitation_id=%s; continuing",
                self._target.elicitation_id,
            )
        self.stop()
        delivered = await self._deliver(verdict)
        if not delivered:
            _logger.info(
                "Elicitation answer had no waiter elicitation_id=%s", self._target.elicitation_id
            )
            await interaction.followup.send(
                "That request already closed — send your message again to retry.",
                ephemeral=True,
            )

    def _disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button | discord.ui.Select):
                item.disabled = True
