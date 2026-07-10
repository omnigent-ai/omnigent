"""Scheduled-task entities — persisted in the ``scheduled_tasks`` and
``scheduled_task_runs`` tables.

A :class:`ScheduledTask` is a saved, scheduled instruction that fires an agent
session — recurring (``cron_expression``) or one-shot (``run_at_ms``). A
:class:`ScheduledTaskRun` records one firing of a task (its run history). This
module holds the plain dataclasses the store converts ORM rows into; the store
owns the JSON (de)serialization of the Text-backed columns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ScheduledTask:
    """
    A scheduled task persisted in the ``scheduled_tasks`` table.

    A task's trigger is a ``cron_expression | run_at_ms`` oneof (matching
    Harry's ``ScheduleTrigger``): exactly one is set — ``cron_expression`` for a
    recurring task, ``run_at_ms`` for a one-shot.

    :param id: Opaque primary key, e.g. ``"st_a1b2c3..."``. On Isaac→Omni
        migration a fresh id is minted; Isaac's schedule_id is preserved in
        metadata.
    :param name: Human-readable task name, e.g. ``"nightly triage"``.
    :param prompt: The instruction dispatched to the agent on each firing.
    :param owner_user_id: User the spawned session's ``LEVEL_OWNER`` grant is
        written for, e.g. ``"alice@example.com"``. ``None`` in single-user mode.
    :param agent_id: The agent bound to this task, e.g. ``"ag_..."``.
    :param timezone: IANA timezone the trigger is evaluated in,
        e.g. ``"America/Los_Angeles"``.
    :param created_at: Unix epoch seconds at row creation.
    :param cron_expression: A single cron string for a recurring task, or
        ``None`` for a one-shot. Mutually exclusive with ``run_at_ms``.
    :param run_at_ms: One-shot fire time as Unix epoch milliseconds, or ``None``
        for a recurring task. Mutually exclusive with ``cron_expression``.
    :param harness_override: Per-task brain-harness override, e.g. ``"pi"``.
        ``None`` means use the agent default.
    :param model_override: Per-task LLM model override, e.g.
        ``"claude-opus-4-7"``. ``None`` means use the agent default.
    :param reasoning_effort: Per-task reasoning-effort hint, e.g. ``"high"``.
        ``None`` means use the agent default.
    :param workspace: Absolute path where a fired session's runner should
        start (the source repo / working dir). ``None`` when unset.
    :param base_branch: Git base ref a firing branches from when it creates a
        worktree at fire time. Pairs with ``workspace``. ``None`` when unset.
    :param sandbox_target: Nullable compute-target hint (a provider name such
        as ``"local"`` or ``"isaac"``). Persisted only; no resolution logic yet.
    :param state: Lifecycle state — one of ``"active"``, ``"paused"``,
        ``"deleted"``, ``"completed"``. Defaults to ``"active"``.
    :param last_run_at: Unix epoch seconds of the most recent firing, or
        ``None`` if it has never fired.
    :param last_run_conversation_id: Conversation created by the most recent
        firing, or ``None``.
    :param metadata: Free-form metadata dict. Defaults to an empty dict.
    :param updated_at: Unix epoch seconds of the last write, or ``None`` if the
        row has never been updated.
    """

    id: str
    name: str
    prompt: str
    owner_user_id: str | None
    agent_id: str
    timezone: str
    created_at: int
    cron_expression: str | None = None
    run_at_ms: int | None = None
    harness_override: str | None = None
    model_override: str | None = None
    reasoning_effort: str | None = None
    workspace: str | None = None
    base_branch: str | None = None
    sandbox_target: str | None = None
    state: str = "active"
    last_run_at: int | None = None
    last_run_conversation_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    updated_at: int | None = None


@dataclass
class ScheduledTaskRun:
    """
    A single firing of a scheduled task, persisted in the ``scheduled_task_runs``
    table.

    :param id: Opaque primary key, e.g. ``"sr_a1b2c3..."``.
    :param scheduled_task_id: The task this run belongs to, e.g. ``"st_..."``.
    :param status: Lifecycle state — one of ``"scheduled"``, ``"running"``,
        ``"succeeded"``, ``"failed"``, ``"skipped"``.
    :param scheduled_at: Unix epoch seconds the firing was scheduled for.
    :param conversation_id: Conversation created by this firing, or ``None``
        before dispatch / after the conversation is deleted.
    :param fired_at: Unix epoch seconds dispatch began, or ``None``.
    :param finished_at: Unix epoch seconds the run reached a terminal state,
        or ``None``.
    :param error: Failure detail when ``status == "failed"``; ``None`` otherwise.
    """

    id: str
    scheduled_task_id: str
    status: str
    scheduled_at: int
    conversation_id: str | None = None
    fired_at: int | None = None
    finished_at: int | None = None
    error: str | None = None
