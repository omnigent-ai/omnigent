"""Types for the per-harness native pane turn probes.

A turn probe asks a native harness's own state whether its agent is still
working, so the pane reaper can confirm or refute a recorded ``running`` before
it tears a silent pane down. Each built-in harness that has such a source
declares it as ``NativeHarnessProvider.pane_turn_probe``: an
``async (NativeProbeContext) -> TurnProbe | None`` callable. ``None`` means the
probe does not answer in this mode (a deep probe asked for a cheap answer).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from omnigent.runner.resource_registry import SessionResourceRegistry
    from omnigent.runner.session_status import SessionStatusBook


class TurnState(StrEnum):
    """What a probe says the agent is doing."""

    ACTIVE = "active"
    PARKED = "parked"
    INACTIVE = "inactive"
    UNKNOWN = "unknown"


ProbeAuthority = Literal["vendor", "inferred"]


@dataclass(frozen=True)
class TurnProbe:
    """One probe answer.

    :param state: The agent's state.
    :param authority: ``"vendor"`` when read from the vendor's own state,
        ``"inferred"`` when derived from Omnigent-side bookkeeping.
    :param blocked_on: What a PARKED agent waits for, e.g. ``"approval"``.
    :param detail: Short diagnostic, e.g. ``"thread/read: idle"``.
    :param started_wall: Wall-clock time the ACTIVE turn began, when the
        source dates it (a hook log line's record time). Orders an inferred
        ACTIVE against an accepted interrupt; ``None`` when unknown.
    """

    state: TurnState
    authority: ProbeAuthority
    blocked_on: str | None = None
    detail: str = ""
    started_wall: float | None = None


@dataclass(frozen=True)
class NativeProbeContext:
    """What a probe may use to answer.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param harness_key: Native agent key, e.g. ``"codex"``.
    :param resource_registry: The runner's session resource registry.
    :param status_book: The runner's session status book.
    :param session_labels: Async getter for the session's labels (bridge ids).
    :param forwarder_alive: Whether the session's forwarder task is running.
    :param timeout_s: Budget for any I/O the probe performs.
    :param cheap_only: Answer only from local files; deep probes return ``None``.
    """

    session_id: str
    harness_key: str
    resource_registry: SessionResourceRegistry
    status_book: SessionStatusBook
    session_labels: Callable[[], Awaitable[Mapping[str, str]]]
    forwarder_alive: bool
    timeout_s: float = 5.0
    cheap_only: bool = False

    async def bridge_id(self, label_key: str | None) -> str:
        """The bridge id from *label_key*'s session label, else the session id."""
        if label_key is None:
            return self.session_id
        labels = await self.session_labels()
        return labels.get(label_key) or self.session_id


PaneTurnProbe = Callable[[NativeProbeContext], Awaitable[TurnProbe | None]]
