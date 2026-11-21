"""
LLM-callable timer builtins.

Two tools:

- :class:`SysTimerSetTool` (``sys_timer_set``) — schedules a timer
  that fires a notification after a delay.
- :class:`SysTimerCancelTool` (``sys_timer_cancel``) — cancels a
  previously scheduled timer by ``timer_id``.

Both tools are gated on the agent spec's top-level ``timers:`` flag
(see :attr:`AgentSpec.timers`, defaulting to ``False``).

These classes own the LLM-facing schema and argument validation.
The runner's ``execute_tool`` intercepts ``sys_timer_set`` /
``sys_timer_cancel`` and dispatches to
:func:`omnigent.runner.tool_dispatch._execute_timer_set` /
``_execute_timer_cancel``, which save or delete the timer on the server.
There each timer is one scheduled task (``kind="timer"``) that the
database scheduler fires, so it survives runner exits and restarts. The
shared :func:`validate_timer_set_args` helper keeps the tools, the runner
and the server route rejecting the same inputs.

The tools are **synchronous** (``is_async() == False``): the LLM
gets the ``timer_id`` back immediately so it can later cancel by ID.
A firing arrives as a hidden ``[System: timer X fired]`` meta message
that wakes the session on the normal ingest path.

Invoked in-process (off the runner dispatch path) these tools have no
server to schedule or cancel against, so ``invoke`` validates its
arguments and then reports that no timer was scheduled or found rather
than raising.

See ``designs/SERVER_HARNESS_CONTRACT.md`` §Timers.
"""

from __future__ import annotations

import json
import logging
import math
from typing import Any

from omnigent.tools.base import Tool, ToolContext

_logger = logging.getLogger(__name__)

# Maximum ``seconds`` value the LLM can pass to ``sys_timer_set``.
# A pragmatic cap (~12 days) that's long enough for any realistic
# scheduling use case and short enough that an obvious typo (e.g.
# the LLM hallucinating ``seconds=99999999``) can't park a timer
# indefinitely. If a real use case exceeds this, the cap gets
# revisited together with the design tradeoffs of long-lived
# timer workflows.
MAX_TIMER_SECONDS = 1_000_000


def validate_timer_set_args(
    args: dict[str, Any],
) -> tuple[float, bool, str | None] | str:
    """
    Validate parsed ``sys_timer_set`` arguments.

    Shared by :meth:`SysTimerSetTool.invoke` and the runner's
    ``_execute_timer_set`` so both surfaces reject the same inputs with
    identical messages and honor one delay ceiling.

    :param args: JSON-decoded argument mapping, e.g.
        ``{"seconds": 5, "repeat": False, "note": "x"}``.
    :returns: ``(seconds, repeat, note)`` when valid, otherwise an error
        message naming the first invalid field, e.g.
        ``"seconds must be a number"``.
    """
    seconds_raw = args.get("seconds")
    # Reject bool explicitly: ``isinstance(True, int)`` is True, so a bare
    # int/float check would silently coerce ``True`` to ``1.0``.
    if not isinstance(seconds_raw, (int, float)) or isinstance(seconds_raw, bool):
        return "seconds must be a number"
    seconds = float(seconds_raw)
    # NaN/Inf pass isinstance(float) but fail every comparison, so they
    # would bypass the non-negative, cap, and repeat>0 guards.
    if not math.isfinite(seconds):
        return "seconds must be a finite number"
    if seconds < 0:
        return "seconds must be non-negative"
    if seconds > MAX_TIMER_SECONDS:
        return f"seconds must be <= {MAX_TIMER_SECONDS}"
    repeat = args.get("repeat", False)
    if not isinstance(repeat, bool):
        return "repeat must be a boolean"
    # repeat=true with seconds=0 would busy-loop sleep(0) + POST forever.
    # One-shot seconds=0 remains valid (immediate single firing).
    if repeat and seconds == 0:
        return "seconds must be > 0 when repeat is true"
    note = args.get("note")
    if note is not None and not isinstance(note, str):
        return "note must be a string"
    return seconds, repeat, note


class SysTimerSetTool(Tool):
    """
    Schedule a timer that wakes this session after a delay.

    The LLM passes ``seconds`` (delay), optional ``repeat`` (default
    ``False``), and optional ``note`` (echoed back in each firing). On
    the runner dispatch path the server saves the timer as a scheduled
    task whose ID is returned immediately as ``timer_id``; each firing
    arrives later as a hidden ``[System: timer X fired]`` meta message
    that wakes the session. Repeating timers continue until
    ``sys_timer_cancel`` is called.
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_timer_set"``."""
        return "sys_timer_set"

    @classmethod
    def description(cls) -> str:
        """
        :returns: Description visible to the LLM in tool listings.
        """
        return (
            "Schedule a timer that wakes this session after a delay, even if "
            "the session has gone idle or its runner restarted. The firing "
            "appears as a [System: timer X fired] message in the conversation; "
            "you can include an optional note that's echoed back in the firing. "
            "Set repeat=true for a recurring timer (cancel via sys_timer_cancel); "
            "a repeating timer skips a tick while this session is busy, and ticks "
            "missed while the server was down arrive as one late firing. Times are "
            "rounded up to whole seconds and are best effort: a firing can arrive "
            "a little late, never early. The tool returns immediately with the "
            "timer_id and the first fire_at."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        :returns: OpenAI tool schema with ``seconds`` (number,
            required), ``repeat`` (boolean, optional, default
            ``False``), and ``note`` (string, optional).
        """
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "seconds": {
                            "type": "number",
                            "description": (
                                "Delay before the timer fires, in "
                                "seconds. Must be a finite "
                                "non-negative number; the first "
                                "firing happens after this delay, "
                                "rounded up to a whole second. "
                                "For repeat=true, must be > 0 and "
                                "is also the interval between "
                                "firings, rounded up to a whole "
                                "second."
                            ),
                        },
                        "repeat": {
                            "type": "boolean",
                            "description": (
                                "When true, the timer fires every "
                                "`seconds` until cancelled. When "
                                "false (default), fires once."
                            ),
                            "default": False,
                        },
                        "note": {
                            "type": "string",
                            "description": (
                                "Optional string echoed in each "
                                "firing's [System: timer X fired] "
                                "message. Useful to disambiguate "
                                "multiple timers."
                            ),
                        },
                    },
                    "required": ["seconds"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Validate arguments; report that the in-process path scheduled
        no timer.

        The runner intercepts ``sys_timer_set`` before this builtin is
        reached and saves the timer on the server. When ``invoke`` does
        run (off the runner dispatch path) there is no server to schedule
        against, so it validates its input for a consistent error surface
        and then returns a structured error instead of falsely reporting
        success.

        :param arguments: JSON-encoded args, e.g.
            ``'{"seconds": 5, "repeat": false, "note": "x"}'``.
        :param ctx: Provides ``ctx.conversation_id`` — required so the
            argument contract matches the runner path.
        :returns: JSON string ``{"error": "..."}`` — either a validation
            failure or a note that no timer was scheduled.
        """
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"invalid arguments: {exc}"})

        validated = validate_timer_set_args(args)
        if isinstance(validated, str):
            return json.dumps({"error": validated})

        if ctx.conversation_id is None:
            # Match the runner contract: a timer needs a destination
            # conversation to fire into.
            return json.dumps({"error": "sys_timer_set requires a conversation context"})

        return json.dumps(
            {
                "error": (
                    "sys_timer_set is executed by the runner dispatch path; this "
                    "in-process call cannot schedule a timer, so none was started."
                )
            }
        )


class SysTimerCancelTool(Tool):
    """
    Cancel a scheduled timer by ``timer_id``.

    The runner intercepts ``sys_timer_cancel`` and deletes the timer's
    scheduled task on the server, which suppresses a firing that has not
    gone out yet. When this builtin runs in-process (off the runner
    dispatch path) there is no server to consult, so a valid
    ``timer_id`` reports ``status="not_found"`` — a timer that already
    fired and cleaned up is indistinguishable from one that never
    existed.
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_timer_cancel"``."""
        return "sys_timer_cancel"

    @classmethod
    def description(cls) -> str:
        """
        :returns: Description visible to the LLM in tool listings.
        """
        return (
            "Cancel a previously scheduled timer by timer_id. "
            "Returns status='cancelled' if the timer was active, "
            "or status='not_found' if no such timer exists, it was "
            "already cancelled, or a one-shot timer has already fired. "
            "A firing already on its way when you cancel may still arrive."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        :returns: OpenAI tool schema with ``timer_id`` (string,
            required) — the value the LLM received from
            ``sys_timer_set``.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "timer_id": {
                            "type": "string",
                            "description": "The timer_id returned by sys_timer_set.",
                        },
                    },
                    "required": ["timer_id"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Report cancellation for a ``timer_id`` (in-process fallback).

        The runner intercepts this tool before the builtin is reached and
        cancels on the server; this in-process path has no server, so a
        valid id reports ``not_found``.

        :param arguments: JSON-encoded args, e.g.
            ``'{"timer_id": "timer_..."}'``.
        :param ctx: Tool context (unused; cancellation is keyed on
            ``timer_id`` alone).
        :returns: JSON string ``{"timer_id", "status": "not_found"}``,
            or ``{"error": "..."}`` for invalid input.
        """
        del ctx  # The tool doesn't need any per-invocation context.
        try:
            args = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"invalid arguments: {exc}"})

        timer_id = args.get("timer_id")
        if not isinstance(timer_id, str) or not timer_id:
            return json.dumps({"error": "timer_id is required"})

        # No server is reachable on the in-process path.
        return json.dumps({"timer_id": timer_id, "status": "not_found"})
