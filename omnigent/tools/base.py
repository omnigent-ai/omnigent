"""Abstract base class for agent tools."""

from __future__ import annotations

import abc
import re
from dataclasses import dataclass
from pathlib import Path  # used by ToolContext.workspace type hint
from typing import Any

# Tool name constraint: alphanumeric plus ``_`` and ``-``, up to
# 256 characters. OpenAI enforces 1–64 but other providers allow
# longer names, and client-side tools come from the user.
TOOL_NAME_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_-]{1,256}$")


def is_valid_tool_name(name: str) -> bool:
    """
    Check whether a tool name is valid: 1–256 characters,
    alphanumeric plus ``_`` and ``-``.

    :param name: The tool name to validate, e.g. ``"get_weather"``.
    :returns: ``True`` if the name is valid, ``False`` otherwise.
    """
    return TOOL_NAME_RE.match(name) is not None


@dataclass(frozen=True)
class ToolContext:
    """
    Execution context passed to every tool invocation.

    Provides server-side metadata that tools may need but
    which the LLM does not supply (task identity, agent
    identity, workspace path). Individual tools read the
    fields they need and ignore the rest.

    :param task_id: The current task/workflow ID,
        e.g. ``"task_abc123"``.
    :param agent_id: The registered agent ID,
        e.g. ``"ag_xyz789"``.
    :param workspace: Per-conversation persistent working
        directory. ``upload_file`` resolves paths against it;
        ``sys_terminal_launch`` uses it as the cwd default
        (per ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §4.6 cwd-resolution
        precedence). ``None`` when no workspace is available (e.g.
        tests).
    :param conversation_id: The current conversation's ID,
        e.g. ``"conv_abc123"``. Used by conversation-scoped
        tools (the ``sys_terminal_*`` family looks up its
        :class:`TerminalRegistry` entries by this id; the
        ``sys_session_*`` family uses it for sub-agent
        addressing). ``None`` when not available (older workflow
        paths, unit tests) — tools that require it should fail
        loud.
    :param user_id: The *acting* collaborator who triggered the current
        turn — the authenticated sender of the message being processed,
        not the session owner (#5). In a shared session this lets a tool
        act under the actor's own identity, e.g. resolve that user's
        per-user vault secret via
        :func:`omnigent.runtime.credentials.resolve_user_secret`. ``None``
        in single-user mode, for owner-initiated turns with no distinct
        actor, or when the identity wasn't threaded (older paths, tests).
    """

    task_id: str
    agent_id: str
    workspace: Path | None = None
    conversation_id: str | None = None
    user_id: str | None = None


class Tool(abc.ABC):
    """
    Abstract base class for all tools available to the agent.

    Each tool has a unique name, an OpenAI-format schema for the
    LLM, and an ``invoke`` method that executes the tool and
    returns a string result.

    Subclasses must implement ``name()`` as a ``@classmethod``
    (for tools with a fixed name, e.g. ``SpawnTool.name()``)
    or as a regular method (for tools whose name depends on
    instance state, e.g. ``McpTool``).
    """

    @classmethod
    @abc.abstractmethod
    def name(cls) -> str:
        """
        Unique tool name used for dispatch and schema registration.

        :returns: The tool name, e.g. ``"load_skill"``.
        """

    @classmethod
    @abc.abstractmethod
    def description(cls) -> str:
        """
        Human-readable description of what the tool does.

        Must be readable without instantiation — used for tool
        discovery (e.g. the onboarding assistant's
        ``list_builtin_tools``) and should match the description
        in :meth:`get_schema`.

        :returns: The tool's description string.
        """

    @abc.abstractmethod
    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI Chat Completions tool schema.

        :returns: A dict with ``"type": "function"`` and a
            ``"function"`` sub-dict describing the tool's name,
            description, and parameters.
        """

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Execute the tool with the given arguments.

        Optional override. Tools that ship as schema-only
        (because the runner dispatches them out-of-band — e.g.
        ``sys_cancel_task``, ``sys_session_send``, async-inbox
        family) inherit this default, which fails loud if the
        AP-side path ever reaches them.

        :param arguments: JSON-encoded arguments string from the
            LLM, e.g. ``'{"name": "summarize"}'``.
        :param ctx: Server-side execution context with task and
            agent identity.
        :returns: The tool's string result.
        :raises NotImplementedError: When the subclass is
            runner-dispatched and Omnigent misroutes here.
        """
        del arguments, ctx
        raise NotImplementedError(
            f"{type(self).__name__}.invoke is runner-dispatched; the "
            f"AP-side path should not reach this method. The runner "
            f"handles dispatch via omnigent/runner/tool_dispatch.py."
        )

    def cancel(self) -> None:  # noqa: B027 — optional override hook; default is a no-op
        """
        Cancel an in-progress invocation.

        Called by ``call_tool_with_timeout`` when the deadline
        expires. Subprocess-based tools override this to kill
        the child process. Default is a no-op.
        """

    def shutdown(self) -> None:  # noqa: B027 — optional override hook; default is a no-op
        """
        Release resources held by this tool instance.

        Called by :meth:`ToolManager.shutdown` during teardown.
        Tools that hold subprocesses, file handles, or other
        long-lived resources override this to clean up.
        Default is a no-op.
        """

    def is_async(self, arguments: str | None = None) -> bool:
        """
        Return ``True`` if this invocation runs in a background workflow.

        Tools that NEVER run async (the common case) leave the
        default. ``arguments`` is passed in case a future tool's
        async-ness depends on call-time choice — current tools either
        ignore the parameter (fixed at the class level, e.g.
        ``SysCallAsyncTool``) or never go async at all.

        Async invocations bypass the inline ``invoke()`` path. Instead,
        the runtime calls :meth:`dispatch_async` which starts a child
        workflow and returns a handle immediately; the real result
        arrives later via the async-work drain.

        :param arguments: JSON-encoded argument string from the LLM,
            same shape as what ``invoke`` would receive. ``None``
            means the caller only wants the tool-level default (used
            by tool manifest generation that doesn't have call-time
            arguments). Tools that ignore per-call semantics can
            safely ignore this parameter.
        :returns: ``True`` iff this invocation should dispatch as a
            background workflow.
        """
        return False

    async def dispatch_async(
        self,
        *,
        parent_task_id: str,
        parent_conversation_id: str,
        agent_id: str,
        agent_name: str,
        arguments: str,
        workspace_path: str | None,
    ) -> Any:
        """
        Run an async invocation — either by spawning a child workflow
        or by producing the result inline.

        Called by the runtime (from an async workflow body) when
        :meth:`is_async` returned True. Two return shapes are
        supported, and the runtime's ``_execute_tools`` dispatcher
        accepts both:

        - **Background dispatch (the common case).** The tool creates
          a child ``task_store`` row, starts the runner-side async task pinned to
          that task_id, and returns an ``_AsyncToolHandle`` (defined
          in ``omnigent.runtime.workflow``). The handle is
          serialized via ``to_handle_json`` and surfaced to the LLM
          as the tool-call output; the real result arrives later
          through the async-work drain.
        - **Inline async work.** The tool returns a ``str`` directly.
          The runtime treats it as a normal sync tool result without
          spawning any child. Used by tools whose work needs the
          parent's async loop (e.g., draining a runner inbox via
          ``the async-work drain``) but produces a result immediately —
          ``sys_read_inbox`` is the canonical example. Also used by
          tools like ``sys_call_async`` to surface argument-validation
          errors that don't justify a child workflow.

        The default raises ``NotImplementedError`` — subclasses that
        override :meth:`is_async` to return True MUST override this
        as well. The split lets synchronous tools ignore the async
        path entirely without import-cycle headaches around
        ``_AsyncToolHandle``.

        :param parent_task_id: The currently-executing parent
            workflow's task_id. The new child task points at it via
            ``root_task_id``; the background workflow signals it via
            the ``async_work_complete`` topic. Inline-result tools
            may ignore this.
        :param parent_conversation_id: The owning conversation's id.
            Recorded on the child task row for conversation-scoped
            queries. Inline-result tools may ignore this.
        :param agent_id: The owning agent's id. Recorded on the
            child task row. Inline-result tools may ignore this.
        :param agent_name: The tool's name (same as ``self.name()``
            in most cases) — recorded as ``agent_name`` on the task
            so ``list_tasks`` results show what produced the work.
            Inline-result tools may ignore this.
        :param arguments: JSON-encoded argument string from the LLM.
        :param workspace_path: Per-conversation workspace directory
            (or ``None`` if this tool doesn't need it).
        :returns: Either an ``_AsyncToolHandle`` instance (the
            runtime's shared handle shape, for the background path)
            or a ``str`` (for inline-result tools). Typed as ``Any``
            to avoid importing the runtime module from the tool
            base.
        :raises NotImplementedError: When called on a tool that
            didn't override this method.
        """
        raise NotImplementedError(
            f"{type(self).__name__} declared is_async() true but did not override dispatch_async()"
        )
