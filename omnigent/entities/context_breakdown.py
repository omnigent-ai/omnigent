"""Per-turn context breakdown a runner reports for the request it sent the model."""

from __future__ import annotations

from pydantic import BaseModel, Field, NonNegativeInt


class TurnContextBreakdown(BaseModel):
    """
    What one turn's request carried, split by source.

    Figures are estimated from the content the runner handed the harness
    (``method``); the provider-reported ``context_tokens`` stays the measured
    total. Content a harness adds itself (its own system prompt, project
    instructions) is not included.

    :param system_tokens: Composed instructions, e.g. ``1200``.
    :param tool_tokens: Tool schemas listed for the turn, e.g. ``5400``.
    :param tool_count: Tools listed, e.g. ``18``.
    :param history_tokens: Earlier messages and tool calls/results, without
        their attachments.
    :param history_item_count: Earlier messages and tool calls/results.
    :param attachment_tokens: Attachments sent inline, in every message.
    :param attachment_count: Attachments sent inline, e.g. ``2``.
    :param unestimated_attachment_count: Inline attachments with no reliable
        estimate (e.g. a PDF); their size is not in ``attachment_tokens``.
    :param message_tokens: The current message's text.
    :param method: How the figures were derived, e.g. ``"content_size"``
        (UTF-8 bytes per token, with a fixed estimate per image).
    """

    system_tokens: NonNegativeInt = 0
    tool_tokens: NonNegativeInt = 0
    tool_count: NonNegativeInt = 0
    history_tokens: NonNegativeInt = 0
    history_item_count: NonNegativeInt = 0
    attachment_tokens: NonNegativeInt = 0
    attachment_count: NonNegativeInt = 0
    unestimated_attachment_count: NonNegativeInt = 0
    message_tokens: NonNegativeInt = 0
    method: str = Field(default="content_size", max_length=32)

    @property
    def total_tokens(self) -> int:
        """Sum of the itemized estimates."""
        return (
            self.system_tokens
            + self.tool_tokens
            + self.history_tokens
            + self.attachment_tokens
            + self.message_tokens
        )
