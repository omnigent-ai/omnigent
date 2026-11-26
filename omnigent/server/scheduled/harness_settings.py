"""Permission modes for saved automations and the sessions that sessions start."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any

from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness, is_native_harness
from omnigent.native.permission_modes import (
    AGENT_DEFAULT_PERMISSION_MODE_HINT,
    APPROVAL_SKIPPING_PERMISSION_MODES,
    PERMISSION_LAUNCH_ARGS,
)
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.routes._sessions.common import (
    _CLAUDE_NATIVE_PERMISSION_MODE_LABEL_KEY,
    _CODEX_NATIVE_APPROVAL_MODE_LABEL_KEY,
)
from omnigent.stores.conversation_store import CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY

_logger = logging.getLogger(__name__)

# Labels where a running session reports the mode it is in, including a switch made
# inside the CLI, in its harness's vocabulary. Anyone who may edit the session can
# write them too, so they never grant a mode that skips approval.
_REPORTED_PERMISSION_MODE_LABEL_KEYS: dict[str, str] = {
    "claude-native": _CLAUDE_NATIVE_PERMISSION_MODE_LABEL_KEY,
    "codex-native": _CODEX_NATIVE_APPROVAL_MODE_LABEL_KEY,
}

INHERITED_APPROVAL_SKIPPING_MESSAGE = (
    "The agent's spec sets permission mode {mode!r}, which skips approval, and an automation "
    "cannot inherit it; set a mode in the agent spec that asks for approval or uses the auto "
    "classifier"
)


def _permission_mode_choices(harness: str | None) -> str:
    """Name the values a task may send for ``harness``, or for every harness when unknown."""
    if harness is None:
        vocabularies = "; ".join(
            f"{name}: {', '.join(map(repr, modes))}"
            for name, modes in PERMISSION_LAUNCH_ARGS.items()
        )
        # The null hint leads because the runner truncates the server errors it relays.
        return (
            f"Use {AGENT_DEFAULT_PERMISSION_MODE_HINT}; other harnesses, such as claude-sdk, "
            f"have no automation permission modes. Native harness modes: {vocabularies}"
        )
    modes = PERMISSION_LAUNCH_ARGS.get(harness)
    if not modes:
        return (
            f"{harness!r} has no automation permission modes; "
            f"use {AGENT_DEFAULT_PERMISSION_MODE_HINT}"
        )
    return f"Use one of {', '.join(map(repr, modes))}, or {AGENT_DEFAULT_PERMISSION_MODE_HINT}"


def validate_scheduled_permission_mode(mode: str | None) -> str | None:
    """Validate the vocabulary before resolving the bound agent's harness."""
    if mode is None:
        return None
    if not any(mode in modes for modes in PERMISSION_LAUNCH_ARGS.values()):
        raise OmnigentError(
            f"Unsupported permission_mode: {mode!r}. {_permission_mode_choices(None)}",
            code=ErrorCode.INVALID_INPUT,
        )
    return mode


async def _scheduled_agent_executor(agent: Any, agent_cache: Any) -> tuple[str | None, Any]:
    """Load the saved agent's executor; ``(None, None)`` when it cannot be loaded."""
    if agent is None or agent_cache is None or getattr(agent, "bundle_location", None) is None:
        return None, None
    try:
        loaded = await asyncio.to_thread(
            agent_cache.load,
            agent.id,
            agent.bundle_location,
            expand_env=agent.operator_authored,
        )
        executor = getattr(loaded.spec, "executor", None)
        raw = (executor.config.get("harness") or executor.type) if executor else None
        return canonicalize_harness(raw) or raw, executor
    except Exception:
        _logger.exception("Could not resolve harness for scheduled agent %s", agent.id)
        return None, None


async def scheduled_agent_harness(agent: Any, agent_cache: Any) -> str | None:
    """Resolve the saved agent; return None when its harness cannot be determined."""
    harness, _ = await _scheduled_agent_executor(agent, agent_cache)
    return harness


def permission_mode_launch_options(
    harness: str, permission_mode: str
) -> tuple[list[str] | None, dict[str, str]] | None:
    """Launch args and labels that start ``harness`` in ``permission_mode``.

    ``None`` when the harness has no such mode; ``None`` args keep the CLI's own default.
    """
    args = PERMISSION_LAUNCH_ARGS.get(harness, {}).get(permission_mode)
    if args is None:
        return None
    labels = {}
    if harness == "codex-native" and permission_mode == "bypass":
        labels[CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY] = "1"
    return list(args) or None, labels


def launch_args_contain(args: Sequence[str], needle: Sequence[str]) -> bool:
    """Whether *needle* appears in *args* as consecutive items."""
    width = len(needle)
    return any(tuple(args[i : i + width]) == tuple(needle) for i in range(len(args) - width + 1))


# Harnesses whose launch settings are read with the CLI's own parsing, so a session
# they start can take their creator's mode.
_INHERITED_MODE_HARNESSES = frozenset({"claude-native", "codex-native"})


# Codex's built-in permission profiles, as the sandbox each one selects.
_CODEX_PROFILE_SANDBOXES = {":danger-full-access": "danger-full-access", ":read-only": "read-only"}


def _codex_permission_settings(launch_args: Sequence[str]) -> dict[str, Any] | None:
    """The approval and sandbox settings Codex resumes with for *launch_args*.

    :param launch_args: Codex launch args, e.g. ``["-a", "never", "-s", "danger-full-access"]``.
    :returns: The settings, e.g. ``{"approvalPolicy": "never", "sandbox": "danger-full-access"}``,
        or ``None`` when a custom permission profile or config value takes part.
    """
    from omnigent.harnesses.codex_native.app_server import _codex_resume_permission_params

    try:
        settings = dict(_codex_resume_permission_params(launch_args))
    except ValueError:
        return None
    if "config" in settings:
        return None
    profile = settings.pop("permissions", None)
    if profile is not None:
        if profile not in _CODEX_PROFILE_SANDBOXES:
            return None
        settings.setdefault("sandbox", _CODEX_PROFILE_SANDBOXES[profile])
    if settings.get("approvalPolicy") == "never":
        # Nothing asks for approval, so no reviewer takes part.
        settings.pop("approvalsReviewer", None)
    return settings


def _launch_permission_mode(session: Conversation, harness: str) -> str | None:
    """The mode *session*'s launch settings select, which only its owner or runner set.

    :param session: A session running *harness*.
    :param harness: Its canonical harness, e.g. ``"codex-native"``.
    :returns: A mode of :data:`PERMISSION_LAUNCH_ARGS` for *harness*, or ``None``
        when the settings select none of them exactly. Harnesses other than
        Claude Code and Codex match a preset's args among the options.
    """
    launch_args = list(session.terminal_launch_args or [])
    # Everything after ``--`` is prompt text, not an option.
    options = launch_args[: launch_args.index("--")] if "--" in launch_args else launch_args
    modes = PERMISSION_LAUNCH_ARGS.get(harness, {})
    if harness == "claude-native":
        from omnigent.harnesses.claude_native.bridge import claude_launch_permission_mode

        mode = claude_launch_permission_mode(options)
        return mode if mode in modes else None
    if harness == "codex-native":
        from omnigent.harnesses.codex_native.launch_args import codex_config_profile

        if session.labels.get(CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY) == "1":
            return "bypass"
        # A file profile can set more than any preset, so none stands in for it.
        try:
            if codex_config_profile(options) is not None:
                return None
        except ValueError:
            return None
        effective = _codex_permission_settings(options)
        if effective is None:
            return None
        return next(
            (
                mode
                for mode, args in modes.items()
                if args and _codex_permission_settings(args) == effective
            ),
            None,
        )
    return next(
        (mode for mode, args in modes.items() if args and launch_args_contain(options, args)),
        None,
    )


def session_permission_mode(session: Conversation, harness: str) -> str | None:
    """The permission mode *session* runs in, in *harness*'s vocabulary, or ``None``.

    A reported mode that keeps approval wins, since launch args can lag a switch made
    while the session runs; any session can already ask for such a mode for a session
    it starts. A mode that skips approval comes only from the launch settings.

    :param session: A session running *harness*.
    :param harness: Its canonical harness, e.g. ``"codex-native"``.
    :returns: A mode of :data:`PERMISSION_LAUNCH_ARGS` for *harness*, e.g.
        ``"full-access"``, or ``None`` when it cannot be told.
    """
    reported = session.labels.get(_REPORTED_PERMISSION_MODE_LABEL_KEYS.get(harness, ""))
    if (
        reported in PERMISSION_LAUNCH_ARGS.get(harness, {})
        and reported not in APPROVAL_SKIPPING_PERMISSION_MODES
    ):
        return reported
    return _launch_permission_mode(session, harness)


def creator_session_launch_options(
    creator: Conversation,
    *,
    harness: str | None,
    creator_harness: str | None,
    on_creator_host: bool,
) -> tuple[list[str] | None, dict[str, str]] | None:
    """The creator's permission mode for a session that *creator* starts.

    A session another session starts can do without asking what its creator can.
    ``None`` keeps the new session's agent default: the harness isn't Claude Code or
    Codex, the two run different harnesses, the creator's mode cannot be told, or it
    skips approval and the new session runs off the creator's host, where the user
    never chose that mode.

    :param creator: The session that starts the new one.
    :param harness: The new session's harness, e.g. ``"codex-native"``.
    :param creator_harness: The creator's harness.
    :param on_creator_host: Whether the new session runs on the creator's host.
    :returns: Launch args and labels, as :func:`permission_mode_launch_options` returns.
    """
    harness = canonicalize_harness(harness) or harness
    if (
        harness is None
        or harness not in _INHERITED_MODE_HARNESSES
        or harness != (canonicalize_harness(creator_harness) or creator_harness)
    ):
        return None
    mode = session_permission_mode(creator, harness)
    if mode is None or (mode in APPROVAL_SKIPPING_PERMISSION_MODES and not on_creator_host):
        return None
    return permission_mode_launch_options(harness, mode)


# Legacy fire session -> task id until its run row is written. In-process only, like
# legacy timers themselves (see ``_IN_FLIGHT_TASKS`` in ``fire.py``).
_UNRECORDED_LEGACY_RUN_SESSIONS: WorkspaceScopedCache[str, str] = WorkspaceScopedCache()


def note_unrecorded_legacy_run_session(conversation_id: str, scheduled_task_id: str) -> None:
    """Count *conversation_id* as *scheduled_task_id*'s run until its run row exists."""
    _UNRECORDED_LEGACY_RUN_SESSIONS[conversation_id] = scheduled_task_id


def forget_unrecorded_legacy_run_session(conversation_id: str) -> None:
    """Stop counting a legacy run session once its run row is written."""
    _UNRECORDED_LEGACY_RUN_SESSIONS.pop(conversation_id, None)


def _is_automation_run_session(
    scheduled_task_store: Any, task: Any, source_session_id: str | None
) -> bool:
    """Whether *source_session_id* is where *task* runs now, not a fork or a finished run.

    A continue task runs in its kept session. A new-session run's session counts
    from before its prompt is sent until its run ends: a claimed run is already
    ``starting`` then, and a legacy fire is noted until its run row exists.
    """
    if not source_session_id:
        return False
    if task.continues_session and task.session_id == source_session_id:
        return True
    if _UNRECORDED_LEGACY_RUN_SESSIONS.get(source_session_id) == task.id:
        return True
    return bool(scheduled_task_store.has_active_run(task.id, source_session_id))


async def automation_session_launch_options(
    scheduled_task_store: Any,
    scheduled_task_id: str | None,
    *,
    source_session_id: str | None,
    user_id: str | None,
    harness: str | None,
) -> tuple[list[str] | None, dict[str, str]] | None:
    """The automation's permission mode for a session that one of its runs creates.

    ``None`` applies no automation mode: the creator is not one of the
    automation's runs (a fork keeps the origin label but not the run), the automation
    is deleted, has no mode or belongs to another user, or the new session's harness
    has no such mode.
    """
    if scheduled_task_store is None or not scheduled_task_id or harness is None:
        return None
    task = await asyncio.to_thread(scheduled_task_store.get, scheduled_task_id)
    # ``get`` still returns soft-deleted rows, and deleting a task doesn't stop its run.
    if task is None or task.state == "deleted" or task.permission_mode is None:
        return None
    if (task.user_id or RESERVED_USER_LOCAL) != (user_id or RESERVED_USER_LOCAL):
        return None
    if not await asyncio.to_thread(
        _is_automation_run_session, scheduled_task_store, task, source_session_id
    ):
        return None
    harness = canonicalize_harness(harness) or harness
    options = permission_mode_launch_options(harness, task.permission_mode)
    if options is None:
        _logger.info(
            "Automation %s permission mode %r does not apply to harness %r; "
            "the new session uses its agent default",
            task.id,
            task.permission_mode,
            harness,
        )
    return options


async def scheduled_agent_default_permission_mode(agent: Any, agent_cache: Any) -> str | None:
    """The mode a run with no task override launches with, or ``None`` for the harness default.

    Native harnesses start in their CLI's own default. SDK harnesses take
    ``executor.config.permission_mode`` from the spec; without one they run in
    ``auto``. ``None`` also when the agent cannot be loaded.
    """
    harness, executor = await _scheduled_agent_executor(agent, agent_cache)
    if harness is None or executor is None or is_native_harness(harness):
        return None
    configured = executor.config.get("permission_mode")
    return None if configured is None else str(configured)


async def inherited_approval_skipping_problem(agent: Any, agent_cache: Any) -> str | None:
    """Why a task without a permission mode may not run in its agent's default, or ``None``.

    An explicit task mode is shown in the automation's settings and its approval
    prompt; a mode inherited from the agent spec is not, so it may not skip approval.
    """
    mode = await scheduled_agent_default_permission_mode(agent, agent_cache)
    if mode not in APPROVAL_SKIPPING_PERMISSION_MODES:
        return None
    return INHERITED_APPROVAL_SKIPPING_MESSAGE.format(mode=mode)


async def validate_scheduled_permission_support(
    *,
    permission_mode: str | None,
    agent: Any,
    agent_cache: Any,
    continues_session: bool = False,
) -> None:
    """Reject modes belonging to a different harness, or an inherited approval-skipping mode.

    :param continues_session: Whether the task continues a session. Without a mode of its
        own, the task wakes that session in the session's mode, so nothing is inherited.
    """
    if permission_mode is None:
        if continues_session:
            return
        problem = await inherited_approval_skipping_problem(agent, agent_cache)
        if problem is not None:
            raise OmnigentError(problem, code=ErrorCode.INVALID_INPUT)
        return
    harness = await scheduled_agent_harness(agent, agent_cache)
    if harness is None:
        raise OmnigentError(
            "Cannot resolve the agent's harness to validate permission_mode; try again later",
            code=ErrorCode.INVALID_INPUT,
        )
    if permission_mode not in PERMISSION_LAUNCH_ARGS.get(harness, {}):
        raise OmnigentError(
            f"permission_mode {permission_mode!r} is not supported for {harness!r}. "
            f"{_permission_mode_choices(harness)}",
            code=ErrorCode.INVALID_INPUT,
        )
