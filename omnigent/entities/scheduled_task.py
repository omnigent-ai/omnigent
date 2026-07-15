"""Scheduled-task entities — persisted in the ``scheduled_tasks`` and
``scheduled_task_runs`` tables.

A :class:`ScheduledTask` is a saved, scheduled instruction that fires an agent
session on a recurring cron schedule (``cron_expression``). A
:class:`ScheduledTaskRun` records one firing of a task (its run history). This
module holds the plain dataclasses the store converts ORM rows into; the store
owns the JSON (de)serialization of the Text-backed columns.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ScheduledTask:
    """
    A scheduled task persisted in the ``scheduled_tasks`` table.

    A task's trigger is a required recurring ``cron_expression``.

    :param id: UUID primary key (bare 32-char hex string, no dashes).
    :param name: Human-readable task name, e.g. ``"nightly triage"``.
    :param prompt: The instruction dispatched to the agent on each firing.
    :param cron_expression: The required cron string for the recurring trigger,
        e.g. ``"0 9 * * *"``.
    :param owner_user_id: User the spawned session's ``LEVEL_OWNER`` grant is
        written for, e.g. ``"alice@example.com"``. ``None`` in single-user mode.
    :param agent_id: The agent bound to this task, e.g. ``"ag_..."``.
    :param timezone: IANA timezone the trigger is evaluated in,
        e.g. ``"America/Los_Angeles"``.
    :param created_at: Unix epoch seconds at row creation.
    :param model_override: Per-task LLM model override, e.g.
        ``"claude-opus-4-7"``. ``None`` means use the agent default.
    :param reasoning_effort: Per-task reasoning-effort hint, e.g. ``"high"``.
        ``None`` means use the agent default.
    :param workspace: Absolute path where a fired session's runner should
        start (the source repo / working dir). ``None`` when unset.
    :param base_branch: Git base ref a firing branches from when it creates a
        worktree at fire time. Pairs with ``workspace``. ``None`` when unset.
    :param execution_target: Where a firing runs — one of ``"connected_host"``,
        ``"managed_sandbox"``. Defaults to ``"connected_host"``.
    :param host_id: For ``connected_host``, the specific host to run on;
        ``None`` means the owner's freshest online host. Always ``None`` for
        ``managed_sandbox``.
    :param state: Lifecycle state — one of ``"active"``, ``"paused"``,
        ``"deleted"``. Defaults to ``"active"``.
    :param last_run_at: Unix epoch seconds of the most recent firing, or
        ``None`` if it has never fired.
    :param last_run_conversation_id: Conversation created by the most recent
        firing, or ``None``.
    :param updated_at: Unix epoch seconds of the last write, or ``None`` if the
        row has never been updated.
    """

    id: str
    name: str
    prompt: str
    cron_expression: str
    owner_user_id: str | None
    agent_id: str
    timezone: str
    created_at: int
    model_override: str | None = None
    reasoning_effort: str | None = None
    workspace: str | None = None
    base_branch: str | None = None
    execution_target: str = "connected_host"
    host_id: str | None = None
    state: str = "active"
    last_run_at: int | None = None
    last_run_conversation_id: str | None = None
    updated_at: int | None = None


@dataclass
class ScheduledTaskRun:
    """
    A single firing of a scheduled task, persisted in the ``scheduled_task_runs``
    table.

    :param id: UUID primary key (bare 32-char hex string, no dashes).
    :param scheduled_task_id: The task this run belongs to (a bare 32-char hex
        UUID string).
    :param status: Lifecycle state — one of ``"scheduled"``, ``"running"``,
        ``"succeeded"``, ``"failed"``, ``"skipped"``.
    :param scheduled_at: Unix epoch seconds the firing was scheduled for.
    :param conversation_id: Conversation created by this firing, or ``None``
        before dispatch / after the conversation is deleted.
    :param fired_at: Unix epoch seconds dispatch began, or ``None``.
    :param finished_at: Unix epoch seconds the run reached a terminal state,
        or ``None``.
    :param error: Failure detail when ``status == "failed"``; ``None`` otherwise.
    :param error_code: Short failure classification (e.g. ``"timeout"``,
        ``"rate_limited"``) for future retry logic; ``None`` unless
        ``status == "failed"``.
    """

    id: str
    scheduled_task_id: str
    status: str
    scheduled_at: int
    conversation_id: str | None = None
    fired_at: int | None = None
    finished_at: int | None = None
    error: str | None = None
    error_code: str | None = None
