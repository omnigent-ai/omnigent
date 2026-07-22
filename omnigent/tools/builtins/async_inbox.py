"""
LLM-callable async-dispatch builtins.

Step 11 of the harness contract introduces a small set of tools
the LLM uses to manage long-running work via the inbox pattern
instead of polling. This module ships:

- :class:`SysCallAsyncTool` (11a.i) — dispatch a local Python tool
  as a background workflow.
- :class:`SysReadInboxTool` (11a.ii) — pull-mode drain of completed
  async-work payloads.
- :class:`SysCancelAsyncTool` (11a.iii) — cancel a dispatched
  task by its handle id; thin alias over the always-registered
  ``sys_cancel_task``.

All tools in this module are gated on the agent's top-level
``async:`` flag (see :attr:`AgentSpec.async_enabled`). The flag
**defaults to ``True``** to match the legacy inner stack's
default (``omnigent/inner/datamodel.py::AgentDef.async_enabled``),
so agents that don't mention it still see the async surface and
the same YAML produces the same tool list under Omnigent mode and the
legacy path. Agents that explicitly want a minimal-tools surface
declare ``async: false`` to suppress all three.

See ``designs/SERVER_HARNESS_CONTRACT.md`` §Async work + inbox
for the protocol design and the rationale for the inbox-vs-poll
flip.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysCancelTaskTool(Tool):
    """
    Cancel a background task by ``task_id``.

    Non-blocking: marks the task cancelled and returns immediately.
    The child workflow observes the cancel on its next runner-managed
    iteration and emits the ``async_work_complete`` signal so the
    parent's drain wakes.

    This class defines the schema and name for ``sys_cancel_task``.
    :class:`SysCancelAsyncTool` extends it with the ``handle_id``
    alias schema.
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_cancel_task"``."""
        return "sys_cancel_task"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description."""
        return (
            "Cancel a running background task. Non-blocking — the task "
            "will transition to cancelled status; you'll see a "
            "[System: task ... cancelled] message before your next "
            "iteration. Already-terminal tasks are unchanged (no error). "
            "NOTE: this tool only cancels tasks that Omnigent created via "
            "sys_session_send / sys_call_async. Background shell commands "
            "launched by Bash (`&`) are tracked by the Claude SDK — kill "
            "those with the KillBash tool instead. Calling sys_cancel_task "
            "on a Bash background id returns task_not_found."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: OpenAI function-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": (
                                "The task identifier to cancel — must be "
                                "an AP-created background task (from "
                                "sys_session_send / sys_call_async). NOT a "
                                "Bash background task id (those go to "
                                "KillBash)."
                            ),
                        },
                    },
                    "required": ["task_id"],
                },
            },
        }

    def invoke(self, arguments: str, ctx: Any) -> str:
        """
        Cancel the requested task.

        The tasks table has been removed. This tool returns a
        ``task_not_found`` response for all inputs since no tasks are
        persisted server-side.

        :param arguments: JSON-encoded ``{"task_id": "..."}`` string.
        :param ctx: Server-side execution context (unused).
        :returns: JSON ``{"error": "task_not_found", ...}``.
        """
        import json as _json

        try:
            args = _json.loads(arguments) if arguments else {}
        except (_json.JSONDecodeError, ValueError):
            args = {}
        task_id = args.get("task_id", "")
        return _json.dumps(
            {
                "error": "task_not_found",
                "task_id": task_id,
                "hint": (
                    "The tasks table has been removed. sys_cancel_task is "
                    "only effective for tasks created via sys_call_async "
                    "or sys_session_send; no server-persisted tasks exist."
                ),
            }
        )


class SysCallAsyncTool(Tool):
    """
    Dispatch any local Python tool as a background task.

    The LLM passes the target tool's name and a JSON-encoded
    arguments string; this meta-tool spawns the target via the
    runner-side async-task machinery that powers the
    legacy ``@tool(synchronous=False)`` decoration. The handle
    returned to the LLM describes the TARGET tool's task — the
    LLM never sees a handle for ``sys_call_async`` itself.

    Limitations (intentional v1):

    - Only **local Python tools** can be dispatched. MCP tools,
      builtins, and client-side tools fall through with an
      ``unknown_tool`` or ``unsupported_tool`` error. The
      restriction matches the inner Session's pre-existing async
      surface and keeps the dispatch path narrow.
    - ``sys_call_async`` itself is NOT a valid target. Recursive
      meta-dispatch would let the LLM build infinite handle chains
      with no useful semantic; reject explicitly.

    The handle round-trip uses ``handle_id`` as the canonical
    identifier (cancel via ``sys_cancel_async`` with that same
    field). Runner dispatch also echoes ``task_id`` with an
    identical value as a compatibility alias (remove in 0.8.0) for
    clients that still read the older field name — prefer
    ``handle_id``; do not confuse it with
    :class:`SysCancelTaskTool`'s distinct ``task_id`` contract.

    Handle fields:

    - ``handle_id`` — the freshly created async-work handle id
      (canonical; pass to ``sys_cancel_async``).
    - ``task_id`` — compatibility alias, identical to ``handle_id``;
      remove in 0.8.0.
    - ``tool_name`` — the TARGET tool's name (not
      ``"sys_call_async"``).
    - ``status`` — ``"in_progress"``.
    - ``message`` — the canonical async-handle instruction
      string the LLM keys off of (G12).
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_call_async"``."""
        return "sys_call_async"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description for the LLM."""
        return (
            "Dispatch a local Python tool as a background task. "
            "Returns a handle immediately (canonical field: "
            "handle_id); the result auto-delivers as a system "
            "message when ready (or call sys_read_inbox to drain "
            "proactively). To abort, pass that handle_id to "
            "sys_cancel_async. Use this when you want to run a "
            "normally-synchronous tool concurrently with other "
            "work — e.g., kicking off several long calls in "
            "parallel."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict.
        """
        return {
            "type": "function",
            "function": {
                "name": SysCallAsyncTool.name(),
                "description": SysCallAsyncTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool": {
                            "type": "string",
                            "description": (
                                "Name of the local Python tool to "
                                "dispatch. Must match a tool the "
                                "agent has registered (see your "
                                "available tools)."
                            ),
                        },
                        "args": {
                            "type": "string",
                            "description": (
                                "JSON-encoded arguments for the "
                                "target tool, e.g. "
                                '\'{"q": "weather"}\'. Same shape '
                                "the target tool would accept if "
                                "called directly."
                            ),
                        },
                    },
                    "required": ["tool", "args"],
                    "additionalProperties": False,
                },
            },
        }

    def is_async(self, arguments: str | None = None) -> bool:
        """
        Always returns ``True`` — the whole point of this tool is
        async dispatch.

        Telling the runtime the tool is async-dispatching makes
        AP's ``_call_tool`` route to :meth:`dispatch_async` instead
        of :meth:`invoke`. The LLM-facing function_call_output
        carries the resulting :class:`_AsyncToolHandle` JSON.

        :param arguments: Ignored — async-ness here is intrinsic
            to the tool, not derived from arguments. Kept for
            interface parity with :class:`Tool.is_async`.
        :returns: Always ``True``.
        """
        return True


class SysReadInboxTool(Tool):
    """
    Drain the parent task's inbox of completed async-work payloads.

    The LLM calls this proactively to pull every
    ``async_work_complete`` payload that has piled up on the
    parent workflow's inbox since the last drain. The
    returned text is a concatenation of ``[System: task ...]``
    blocks — the same format AP's between-iteration auto-collect
    persists as user messages — but delivered inline as a
    ``function_call_output`` so the LLM doesn't have to wait for
    the next iteration boundary.

    Why an explicit tool: AP's auto-collect drain runs at the top
    of every loop iteration (see ``_drain_async_completions``).
    That's enough on its own when the LLM finishes a turn and the
    framework gets another shot. But mid-turn, a long chain of
    function_calls could complete several async tasks before the
    LLM yields, and the LLM may want to inspect those completions
    in the SAME turn — e.g., to fan out a second wave of work
    based on the first wave's results. ``sys_read_inbox`` is the
    pull-mode counterpart to the auto-collect push.

    Consumes payloads off the topic — the next iteration's
    auto-collect won't re-deliver them, so the LLM never sees the
    same completion twice. (Inner had the same semantics; this
    matches.)

    Gated on ``async: true`` at the spec level (see
    :class:`SysCallAsyncTool` docstring).
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_read_inbox"``."""
        return "sys_read_inbox"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description for the LLM."""
        return (
            "Drain the inbox of completed async-work payloads "
            "(from sys_call_async dispatches and sub-agent runs) "
            "and return them inline. Use this mid-turn when you "
            "want to inspect completions before yielding — e.g., "
            "to plan follow-up work based on the results. Returns "
            "a textual summary; an empty inbox returns a sentinel "
            "string."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict. The tool takes no arguments.
        """
        return {
            "type": "function",
            "function": {
                "name": SysReadInboxTool.name(),
                "description": SysReadInboxTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }

    def is_async(self, arguments: str | None = None) -> bool:
        """
        Always returns ``True``.

        The drain reads from the parent workflow's async-completion
        topic — an async-only API. AP's sync ``_call_tool`` path runs
        in ``run_in_executor`` (a thread without an event loop), so
        we can't call the drain from there. Returning ``True`` here
        routes through :meth:`dispatch_async`, which Omnigent awaits
        directly in the workflow's async body.

        Despite the ``True`` return, this tool does NOT spawn a
        child workflow — :meth:`dispatch_async` returns the result
        as a string instead of an :class:`_AsyncToolHandle`. AP's
        ``_execute_tools`` accepts both shapes (see the
        ``isinstance(dispatched, _AsyncToolHandle)`` branch).

        :param arguments: Ignored — the drain has no per-call
            knobs.
        :returns: Always ``True``.
        """
        return True


class SysCancelAsyncTool(SysCancelTaskTool):
    """
    Cancel an async-dispatched task by its handle id.

    LLM-facing alias for :class:`SysCancelTaskTool` scoped to the
    async-handle namespace. The schema takes ``handle_id`` instead
    of ``task_id`` so the LLM's mental model — "I have a handle
    from ``sys_call_async``; cancel it via ``sys_cancel_async``" —
    maps cleanly to the tool list. Behaviour is identical to the
    parent class: the parent's per-kind cancel primitives
    (terminal SIGINT, ``client_tool`` SSE cancel) are inherited
    unchanged.

    Why a subclass and not a re-registration of
    :class:`SysCancelTaskTool` under a second name: the schema's
    parameter name differs (``handle_id`` vs ``task_id``), so the
    JSON shape sent by the LLM is keyed differently. A subclass
    cleanly inherits the cancel logic while overriding only the
    schema-and-arg-translation surface.

    Gated on ``async: true`` (see :class:`SysCallAsyncTool`'s
    docstring). The generic :class:`SysCancelTaskTool` is always
    registered via ``_register_task_lifecycle_tools``; this alias
    is the async-namespace counterpart.
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_cancel_async"``."""
        return "sys_cancel_async"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description for the LLM."""
        return (
            "Cancel a task you previously dispatched via "
            "sys_call_async, using the handle_id from the handle "
            "JSON. Non-blocking — the task transitions to "
            "cancelled status and a [System: task ... cancelled] "
            "block arrives in the inbox or auto-deliver. "
            "Already-terminal tasks return without changing "
            "state. Distinct from sys_cancel_task, which takes "
            "task_id for non-async-handle cancellations."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        Differs from :meth:`SysCancelTaskTool.get_schema` only in
        the parameter name (``handle_id`` vs ``task_id``) and
        ``additionalProperties: false`` to match the rest of the
        async-namespace schemas.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict.
        """
        return {
            "type": "function",
            "function": {
                "name": SysCancelAsyncTool.name(),
                "description": SysCancelAsyncTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "handle_id": {
                            "type": "string",
                            "description": (
                                "The ``handle_id`` from the JSON "
                                "handle returned by "
                                "sys_call_async (canonical "
                                "async-dispatch identifier)."
                            ),
                        },
                    },
                    "required": ["handle_id"],
                    "additionalProperties": False,
                },
            },
        }
