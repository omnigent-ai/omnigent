"""Scheduled-task entities — persisted in the ``scheduled_tasks`` and
``scheduled_task_runs`` tables.

A :class:`ScheduledTask` is a saved, scheduled instruction that fires an agent
session on a recurring schedule (``rrule``). A
:class:`ScheduledTaskRun` records one firing of a task (its run history). This
module holds the plain dataclasses the store converts ORM rows into; the store
owns the JSON (de)serialization of the Text-backed columns.

Naming: the user-facing product name for this feature is "Automations"
(web UI display copy only). The domain model, DB tables
(``scheduled_tasks`` / ``scheduled_task_runs``), stores, REST paths
(``/v1/scheduled-tasks``), agent tools (``sys_scheduled_task_*``), and
these types deliberately keep the name "scheduled task". Keep code,
comments, and schema on "scheduled task"; only presentation strings say
"Automations".
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

# Bounds on the outcome an agent records for a run. An emoji can span several
# code points (skin tones, ZWJ sequences, flags), so its bound is in code points.
RUN_OUTCOME_EMOJI_MAX_CHARS = 16
RUN_OUTCOME_TEXT_MAX_CHARS = 80

# Unicode's Extended_Pictographic ranges (emoji-data.txt): the code points that
# can start an emoji. General symbol categories also hold math, box-drawing and
# currency signs, which are not emoji.
_EXTENDED_PICTOGRAPHIC: tuple[tuple[int, int], ...] = (
    (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x203C), (0x2049, 0x2049),
    (0x2122, 0x2122), (0x2139, 0x2139), (0x2194, 0x2199), (0x21A9, 0x21AA),
    (0x231A, 0x231B), (0x2328, 0x2328), (0x2388, 0x2388), (0x23CF, 0x23CF),
    (0x23E9, 0x23F3), (0x23F8, 0x23FA), (0x24C2, 0x24C2), (0x25AA, 0x25AB),
    (0x25B6, 0x25B6), (0x25C0, 0x25C0), (0x25FB, 0x25FE), (0x2600, 0x2605),
    (0x2607, 0x2612), (0x2614, 0x2685), (0x2690, 0x2705), (0x2708, 0x2712),
    (0x2714, 0x2714), (0x2716, 0x2716), (0x271D, 0x271D), (0x2721, 0x2721),
    (0x2728, 0x2728), (0x2733, 0x2734), (0x2744, 0x2744), (0x2747, 0x2747),
    (0x274C, 0x274C), (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757),
    (0x2763, 0x2767), (0x2795, 0x2797), (0x27A1, 0x27A1), (0x27B0, 0x27B0),
    (0x27BF, 0x27BF), (0x2934, 0x2935), (0x2B05, 0x2B07), (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x3030, 0x3030), (0x303D, 0x303D),
    (0x3297, 0x3297), (0x3299, 0x3299), (0x1F000, 0x1F0FF), (0x1F10D, 0x1F10F),
    (0x1F12F, 0x1F12F), (0x1F16C, 0x1F171), (0x1F17E, 0x1F17F), (0x1F18E, 0x1F18E),
    (0x1F191, 0x1F19A), (0x1F1AD, 0x1F1E5), (0x1F201, 0x1F20F), (0x1F21A, 0x1F21A),
    (0x1F22F, 0x1F22F), (0x1F232, 0x1F23A), (0x1F23C, 0x1F23F), (0x1F249, 0x1F3FA),
    (0x1F400, 0x1F53D), (0x1F546, 0x1F64F), (0x1F680, 0x1F6FF), (0x1F774, 0x1F77F),
    (0x1F7D5, 0x1F7FF), (0x1F80C, 0x1F80F), (0x1F848, 0x1F84F), (0x1F85A, 0x1F85F),
    (0x1F888, 0x1F88F), (0x1F8AE, 0x1F8FF), (0x1F90C, 0x1F93A), (0x1F93C, 0x1F945),
    (0x1F947, 0x1FAFF), (0x1FC00, 0x1FFFD),
)  # fmt: skip
_EMOJI_ZWJ = "\u200d"
_EMOJI_KEYCAP = "\u20e3"
_EMOJI_VARIATION_SELECTORS = frozenset("\ufe0e\ufe0f")
_EMOJI_KEYCAP_BASES = frozenset("0123456789#*")


def _is_extended_pictographic(cp: int) -> bool:
    return any(start <= cp <= end for start, end in _EXTENDED_PICTOGRAPHIC)


def _is_emoji_modifier(cp: int) -> bool:
    """Skin tones and the tag characters of subdivision flags extend the emoji before them."""
    return 0x1F3FB <= cp <= 0x1F3FF or 0xE0020 <= cp <= 0xE007F


def is_single_emoji(value: str) -> bool:
    """Whether *value* is one emoji, counting a ZWJ or keycap sequence or a flag as one.

    Approximates one emoji grapheme without a Unicode segmentation dependency:
    every code point must be an Extended_Pictographic character, a keycap base
    followed by its keycap mark, a regional indicator of a flag pair, or a
    joiner, variation selector or modifier attached to one of those.
    """
    if not value or len(value) > RUN_OUTCOME_EMOJI_MAX_CHARS:
        return False
    bases = 0
    joined = False
    regional = 0
    for index, ch in enumerate(value):
        cp = ord(ch)
        if ch == _EMOJI_ZWJ:
            if bases == 0 or joined:
                return False
            joined = True
            continue
        if ch in _EMOJI_VARIATION_SELECTORS or ch == _EMOJI_KEYCAP or _is_emoji_modifier(cp):
            if bases == 0:
                return False
            continue
        if 0x1F1E6 <= cp <= 0x1F1FF:  # regional indicators pair into one flag
            regional += 1
            if regional % 2 == 0:
                continue
        elif ch in _EMOJI_KEYCAP_BASES:
            if _EMOJI_KEYCAP not in value[index + 1 : index + 3]:
                return False
        elif not _is_extended_pictographic(cp):
            return False
        if not joined:
            bases += 1
        joined = False
    return bases == 1 and not joined and regional % 2 == 0


def normalize_run_outcome(emoji: str, text: str) -> tuple[str, str]:
    """Trim and check an outcome's emoji and text, returning the values to store.

    :param emoji: Exactly one emoji, e.g. ``"🚀"``; surrounding spaces are dropped.
    :param text: A few words; runs of whitespace collapse to single spaces.
    :returns: The trimmed emoji and the collapsed text.
    :raises ValueError: If the emoji is not exactly one emoji, or the text is
        empty or longer than :data:`RUN_OUTCOME_TEXT_MAX_CHARS`.
    """
    emoji = emoji.strip()
    if not is_single_emoji(emoji):
        raise ValueError("emoji must be exactly one emoji, e.g. '🚀'")
    text = " ".join(text.split())
    if not text:
        raise ValueError("text must not be empty")
    if len(text) > RUN_OUTCOME_TEXT_MAX_CHARS:
        raise ValueError(f"text must be at most {RUN_OUTCOME_TEXT_MAX_CHARS} characters")
    return emoji, text


class ScheduledRunLogContext(BaseModel):
    """Occurrence identity retained with a saved prompt across replica changes."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    run_id: str
    task_workspace_id: int
    scheduled_at: int
    trigger_key: str | None = None
    target: str
    session_mode: str


@dataclass
class ScheduledTask:
    """
    A scheduled task persisted in the ``scheduled_tasks`` table.

    A task's trigger is a required recurring ``rrule``.

    :param id: UUID primary key (bare 32-char hex string, no dashes).
    :param workspace_id: Tenant partition key that owns this row.
    :param name: Human-readable task name, e.g. ``"nightly triage"``.
    :param prompt: The instruction dispatched to the agent on each firing.
    :param rrule: The required RFC 5545 recurrence rule for the recurring
        trigger, e.g. ``"FREQ=DAILY;BYHOUR=9;BYMINUTE=0"``. Evaluated in
        ``timezone``.
    :param user_id: User the spawned session's ``LEVEL_OWNER`` grant is
        written for, e.g. ``"alice@example.com"``. ``None`` in single-user mode.
    :param agent_id: The agent bound to this task, e.g. ``"ag_..."``.
    :param timezone: IANA timezone the trigger is evaluated in,
        e.g. ``"America/Los_Angeles"``.
    :param created_at: Unix epoch seconds at row creation.
    :param model_override: Per-task LLM model override, e.g.
        ``"claude-opus-4-7"``. ``None`` means use the agent default.
    :param reasoning_effort: Per-task reasoning-effort hint, e.g. ``"high"``.
        ``None`` means use the agent default.
    :param permission_mode: Harness-specific permission or execution mode.
        The fire path translates it into that harness's launch options.
        ``None`` means use the agent's configured default.
    :param max_cost_usd: Optional per-firing cost budget in USD. When set, the
        fire path attaches a ``cost_budget`` policy to each spawned session that
        blocks all models once cumulative spend reaches this limit. ``None``
        means no per-firing cost cap (the session runs unconstrained unless the
        agent spec or server-wide defaults impose one).
    :param session_mode: ``"new"`` (default) starts a new session per firing;
        ``"continue"`` sends each firing's prompt into ``session_id``.
    :param session_id: The session a ``"continue"`` task sends its prompt to.
        ``None`` until its first firing creates one, unless the owner picked an
        existing session.
    :param workspace: Connected-host directory or managed-sandbox repository URL.
        ``None`` uses the execution target's default workspace.
    :param base_branch: Create a random worktree from this ref for each new
        session. ``None`` runs in the selected workspace; ``"HEAD"`` opts in.
    :param execution_target: ``"connected_host"`` for a pinned host, or
        ``"managed_sandbox"`` for a fresh sandbox per occurrence.
    :param host_id: Specific connected host to run on. ``None`` only for legacy
        or invalid rows.
    :param state: Lifecycle state — one of ``"active"``, ``"paused"``,
        ``"deleted"``, or ``"completed"`` once a schedule has no remaining
        occurrences. Defaults to ``"active"``.
    :param last_run_at: Unix epoch seconds of the most recent firing, or
        ``None`` if it has never fired.
    :param last_run_conversation_id: Conversation used by the most recent
        firing, or ``None``.
    :param updated_at: Unix epoch seconds of the last write, or ``None`` if the
        row has never been updated.
    """

    id: str
    name: str
    prompt: str
    rrule: str
    user_id: str | None
    agent_id: str
    timezone: str
    created_at: int
    workspace_id: int = 0
    model_override: str | None = None
    reasoning_effort: str | None = None
    permission_mode: str | None = None
    max_cost_usd: float | None = None
    session_mode: str = "new"
    session_id: str | None = None
    workspace: str | None = None
    base_branch: str | None = None
    execution_target: str = "connected_host"
    host_id: str | None = None
    state: str = "active"
    last_run_at: int | None = None
    last_run_conversation_id: str | None = None
    updated_at: int | None = None
    account_generation: str | None = None
    next_run_at: int | None = None
    project_id: str | None = None
    delegation_ref: str | None = None

    @property
    def continues_session(self) -> bool:
        """Whether the next firing sends its prompt into an existing session."""
        return self.session_mode == "continue" and self.session_id is not None


@dataclass
class ScheduledTaskRun:
    """
    A single firing of a scheduled task, persisted in the ``scheduled_task_runs``
    table.

    :param id: UUID primary key (bare 32-char hex string, no dashes).
    :param scheduled_task_id: The task this run belongs to (a bare 32-char hex
        UUID string).
    :param status: Lifecycle state — one of ``"scheduled"``, ``"starting"``, ``"running"``,
        ``"succeeded"``, ``"failed"``, ``"skipped"``.
    :param scheduled_at: Unix epoch seconds. Persistent dispatch records the
        occurrence's scheduled time; legacy timers record when the fire worker
        began handling the firing, before dispatch.
    :param conversation_id: Conversation created by this firing, or ``None``
        before dispatch / after the conversation is deleted.
    :param fired_at: Unix epoch seconds dispatch began, or ``None``.
    :param finished_at: Unix epoch seconds the run reached a terminal state,
        or ``None``.
    :param error: Failure detail when ``status == "failed"``; ``None`` otherwise.
    :param error_code: Short failure classification (e.g. ``"timeout"``,
        ``"rate_limited"``) for future retry logic; ``None`` unless
        ``status == "failed"``.
    :param outcome_success: Whether the agent reached the run's goal, as it
        recorded with ``sys_scheduled_task_record_outcome``; ``None`` when it recorded
        nothing. Independent of ``status``, which only says whether the run
        finished: a ``succeeded`` run can still have missed its goal.
    :param outcome_emoji: The emoji the agent chose for the outcome, e.g.
        ``"🚀"``; ``None`` when no outcome was recorded.
    :param outcome_text: A few words on the outcome, e.g. ``"Opened 2 PRs"``;
        ``None`` when no outcome was recorded.
    :param outcome_recorded_at: Unix epoch seconds the outcome was last
        recorded, or ``None``.
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
    workspace_id: int = 0
    trigger_key: str | None = None
    host_id: str | None = None
    lease_expires_at: int | None = None
    outcome_success: bool | None = None
    outcome_emoji: str | None = None
    outcome_text: str | None = None
    outcome_recorded_at: int | None = None
