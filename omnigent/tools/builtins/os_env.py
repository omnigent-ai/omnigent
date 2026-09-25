"""
``sys_os_*`` tool builtins for the AP-side ToolManager.

The legacy non-AP path registers ``sys_os_read``,
``sys_os_write``, ``sys_os_edit``, and ``sys_os_shell`` via the
inner :mod:`omnigent.inner.session` when the agent's
``os_env`` is set. They wrap a shared
:class:`omnigent.inner.os_env.OSEnvironment` instance so the
same shell + cwd + sandbox is used across calls.

The Omnigent path can't reuse those Session tool registrations
directly: its
the harness HTTP executor
talks to the harness subprocess via the new HTTP contract,
and tool dispatch goes through AP's
:class:`omnigent.tools.manager.ToolManager` (so policies,
retries, history all apply uniformly).

This module re-exposes the same four tools as AP-side
:class:`Tool` subclasses, backed by the same
:class:`OSEnvironment` machinery. They get registered alongside
other builtins by :meth:`ToolManager._register_local_tools`
when the spec carries an ``os_env`` config.

Why a separate module from ``builtins/spawn.py`` etc.: these
tools depend on the inner os_env machinery
(:func:`omnigent.inner.os_env.create_os_environment`,
:class:`OSEnvSpec`) which won't ship in the AP-only deployment
once :mod:`omnigent.inner` retires. Keeping the dependency
in one file makes that future deletion clean.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from omnigent.inner.os_env import _DEFAULT_READ_LIMIT, OSEnvironment
from omnigent.tools.base import Tool, ToolContext

_logger = logging.getLogger(__name__)


# ── JSON Schemas ──────────────────────────────────────────────
# Mirror the inner :mod:`omnigent.inner.session` schemas
# verbatim so the LLM sees the same parameter shapes regardless
# of which path serves the request. Duplicated literally rather
# than imported because session.py won't ship with AP-only
# deployments — copying these here is the cost of layering.


_OS_READ_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the file to read.",
        },
        "offset": {
            "type": "integer",
            "description": "1-indexed line offset to start reading from.",
        },
        "limit": {
            "type": "integer",
            "description": "Maximum number of lines to return.",
        },
    },
    "required": ["path"],
}


_OS_WRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the file to write.",
        },
        "content": {
            "type": "string",
            "description": "Full file contents to write.",
        },
    },
    "required": ["path", "content"],
}


_OS_EDIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the file to edit.",
        },
        "oldText": {
            "type": "string",
            "description": "Exact text to replace.",
        },
        "newText": {
            "type": "string",
            "description": "Replacement text.",
        },
        "edits": {
            "type": "array",
            "description": "Optional batch of exact edits.",
            "items": {
                "type": "object",
                "properties": {
                    "oldText": {"type": "string"},
                    "newText": {"type": "string"},
                },
                "required": ["oldText", "newText"],
            },
        },
    },
    "required": ["path"],
}


_OS_SHELL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "Shell command to execute.",
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds.",
        },
    },
    "required": ["command"],
}


class _OSEnvBackedTool(Tool):
    """
    Base class shared by the four ``sys_os_*`` tools.

    Holds the :class:`OSEnvironment` instance the
    :class:`omnigent.tools.manager.ToolManager` constructs
    once per agent. Subclasses override
    :meth:`_invoke_async` with their concrete OSEnvironment
    method.

    :param os_env: The agent's :class:`OSEnvironment`.
    """

    def __init__(self, os_env: OSEnvironment) -> None:
        self._os_env = os_env

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Run the tool synchronously by driving the async backend.

        :class:`Tool.invoke` is sync; the OSEnvironment's
        operations are async. We bridge with
        :func:`asyncio.run` per call rather than holding a
        long-lived loop because each invocation is short and
        independent — tool dispatches don't share async state
        across calls.

        :param arguments: JSON-encoded LLM arguments, e.g.
            ``'{"path": "/tmp/x", "offset": 1}'``.
        :param ctx: Server-side execution context (unused).
        :returns: JSON-encoded result string.
        """
        del ctx
        try:
            kwargs = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"malformed arguments JSON: {exc}"})
        if not isinstance(kwargs, dict):
            return json.dumps({"error": "arguments must be a JSON object"})
        import asyncio

        # Surface every failure to the LLM as a structured error
        # payload — bubbling out of the tool call would make the
        # whole turn fail with no diagnostic the model can act on.
        try:
            result = asyncio.run(self._invoke_async(kwargs))
        except Exception as exc:
            _logger.exception("%s failed", self.name())
            return json.dumps({"error": str(exc)})
        return json.dumps(result)

    async def _invoke_async(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """
        Subclass hook: dispatch to the right OSEnvironment method.

        :param kwargs: Parsed argument dict from the LLM.
        :returns: The OSEnvironment's :class:`OpResult`.
        :raises NotImplementedError: Always; subclasses override.
        """
        raise NotImplementedError


class SysOsReadTool(_OSEnvBackedTool):
    """``sys_os_read`` — read text from a file in the OS env."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_os_read"``."""
        return "sys_os_read"

    @classmethod
    def description(cls) -> str:
        """:returns: Description shown to the LLM."""
        return "Read a text file from the OS environment."

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        """
        :returns: OpenAI Chat-Completions tool schema for
            ``sys_os_read``.
        """
        return {
            "type": "function",
            "function": {
                "name": cls.name(),
                "description": cls.description(),
                "parameters": _OS_READ_SCHEMA,
            },
        }

    async def _invoke_async(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """
        Forward to :meth:`OSEnvironment.read`.

        :param kwargs: Parsed args; ``path`` is required.
        :returns: OpResult from the OSEnvironment.
        """
        return dict(
            await self._os_env.read(
                path=kwargs["path"],
                offset=kwargs.get("offset", 1),
                # Unspecified limit → agent-tool default (2 000 lines).
                # None is now "unlimited" in _read_impl, so we must be explicit.
                # Use is-None check (not `or`) so that invalid values like 0 are
                # forwarded to os_env.read for validation rather than silently
                # replaced with the default.
                limit=(lv if (lv := kwargs.get("limit")) is not None else _DEFAULT_READ_LIMIT),
            )
        )


class SysOsWriteTool(_OSEnvBackedTool):
    """``sys_os_write`` — write a full file in the OS env."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_os_write"``."""
        return "sys_os_write"

    @classmethod
    def description(cls) -> str:
        """:returns: Description shown to the LLM."""
        return "Write full contents of a text file in the OS environment."

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        """
        :returns: OpenAI Chat-Completions tool schema for
            ``sys_os_write``.
        """
        return {
            "type": "function",
            "function": {
                "name": cls.name(),
                "description": cls.description(),
                "parameters": _OS_WRITE_SCHEMA,
            },
        }

    async def _invoke_async(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """
        Forward to :meth:`OSEnvironment.write`.

        :param kwargs: Parsed args; ``path`` and ``content`` required.
        :returns: OpResult from the OSEnvironment.
        """
        return dict(
            await self._os_env.write(
                path=kwargs["path"],
                content=kwargs["content"],
            )
        )


class SysOsEditTool(_OSEnvBackedTool):
    """``sys_os_edit`` — exact-text edit a file in the OS env."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_os_edit"``."""
        return "sys_os_edit"

    @classmethod
    def description(cls) -> str:
        """:returns: Description shown to the LLM."""
        return "Perform exact text replacements in a file in the OS environment."

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        """
        :returns: OpenAI Chat-Completions tool schema for
            ``sys_os_edit``.
        """
        return {
            "type": "function",
            "function": {
                "name": cls.name(),
                "description": cls.description(),
                "parameters": _OS_EDIT_SCHEMA,
            },
        }

    async def _invoke_async(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """
        Forward to :meth:`OSEnvironment.edit`.

        :param kwargs: Parsed args; ``path`` is required, plus
            either ``oldText`` + ``newText`` OR ``edits``.
        :returns: OpResult from the OSEnvironment.
        """
        return dict(
            await self._os_env.edit(
                path=kwargs["path"],
                old_text=kwargs.get("oldText"),
                new_text=kwargs.get("newText"),
                edits=kwargs.get("edits"),
            )
        )


class SysOsShellTool(_OSEnvBackedTool):
    """``sys_os_shell`` — run a shell command in the OS env."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_os_shell"``."""
        return "sys_os_shell"

    @classmethod
    def description(cls) -> str:
        """:returns: Description shown to the LLM."""
        return (
            "Run a shell command in the OS environment and return stdout, stderr, and exit_code."
        )

    @classmethod
    def get_schema(cls) -> dict[str, Any]:
        """
        :returns: OpenAI Chat-Completions tool schema for
            ``sys_os_shell``.
        """
        return {
            "type": "function",
            "function": {
                "name": cls.name(),
                "description": cls.description(),
                "parameters": _OS_SHELL_SCHEMA,
            },
        }

    async def _invoke_async(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """
        Forward to :meth:`OSEnvironment.shell`.

        :param kwargs: Parsed args; ``command`` is required.
        :returns: OpResult from the OSEnvironment.
        """
        return dict(
            await self._os_env.shell(
                command=kwargs["command"],
                timeout=kwargs.get("timeout"),
            )
        )


OS_ENV_TOOL_TYPES = (SysOsReadTool, SysOsWriteTool, SysOsEditTool, SysOsShellTool)


def build_os_env_tools(os_env: OSEnvironment) -> list[Tool]:
    """
    Construct one of each ``sys_os_*`` tool against *os_env*.

    Uses the same tool types as metadata-only registration.

    :param os_env: The agent's :class:`OSEnvironment`. All four
        returned tools share this instance — they MUST share
        so cwd / sandbox state stays consistent across calls.
    :returns: A list of four :class:`Tool` instances ready to
        register.
    """
    return [tool_cls(os_env) for tool_cls in OS_ENV_TOOL_TYPES]
