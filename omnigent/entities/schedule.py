"""Schedule entity — persisted in the ``schedules`` table.

A schedule drives recurring agent work within a conversation:

- a **loop** fires a prompt on a cron cadence (e.g. a Friday-night weekly
  report);
- a **monitor** streams a shell command and fires a prompt per output line.

The row holds the definition + lifecycle bookkeeping; the scheduler service
executes it (firing a turn in the owning conversation).
"""

from __future__ import annotations

from dataclasses import dataclass

# Allowed schedule kinds. Single source of truth for the store, tools, and API.
SCHEDULE_KINDS: frozenset[str] = frozenset({"loop", "monitor"})


@dataclass
class Schedule:
    """
    A schedule persisted in the ``schedules`` table.

    :param id: Opaque primary key, e.g. ``"sch_a1b2c3..."``.
    :param conversation_id: The conversation this schedule fires into.
    :param name: Human-readable name, unique within the conversation.
    :param kind: ``"loop"`` (cron-driven) or ``"monitor"`` (stream-driven).
    :param prompt: The prompt fired on each tick. For monitors this is a
        template that may reference the triggering ``{line}``.
    :param enabled: Whether the scheduler runs this schedule.
    :param status: Lifecycle/health state, e.g. ``"idle"``, ``"running"``,
        ``"errored"`` (chiefly for monitors).
    :param created_at: Unix epoch seconds at row creation.
    :param cron: Cron expression for ``kind="loop"`` (else ``None``).
    :param command: Shell command to stream for ``kind="monitor"`` (else
        ``None``).
    :param created_by_user_id: User id that created the schedule, or ``None``.
    :param last_fired_at: Unix epoch seconds of the last fire, or ``None``.
    :param last_run_id: Id of the run produced by the last fire, or ``None``.
    :param updated_at: Unix epoch seconds of the last write, or ``None``.
    """

    id: str
    conversation_id: str
    name: str
    kind: str
    prompt: str
    enabled: bool
    status: str
    created_at: int
    cron: str | None = None
    command: str | None = None
    created_by_user_id: str | None = None
    last_fired_at: int | None = None
    last_run_id: str | None = None
    updated_at: int | None = None
