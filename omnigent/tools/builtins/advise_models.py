"""Built-in tool: sys_advise_models — fan-out model sizing advisor.

This class exists solely to provide the tool **schema** to
:class:`~omnigent.tools.manager.ToolManager` so the LLM can see and
call ``sys_advise_models``.  Execution is handled server-side: the
Omnigent server intercepts the ``tools/call`` in
:func:`~omnigent.server.routes.sessions._handle_advise_models_mcp`
before the MCP proxy ever reaches the runner.

The tool is registered by ``ToolManager`` when:
- ``tools.agents`` is declared in the spec, AND
- ``RuntimeCaps.routing_client`` is configured
  (``OMNIGENT_SMART_ROUTING=1`` + ``llm:`` config block).
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool, ToolContext


class SysAdviseModelsTool(Tool):
    """
    Recommend a model for each sub-agent task before fan-out.

    Accepts a list of tasks the orchestrator is about to dispatch and
    returns a per-task model recommendation based on the task
    description's difficulty.  The caller should pass the recommended
    ``model`` as ``args.model`` when invoking ``sys_session_send`` for
    each worker.

    Returns ``{"recommendations": [...], "router_on": true/false}``.
    Each recommendation has ``{title, agent, model, tier, rationale}``;
    ``model`` is ``null`` when the router is unavailable or the harness
    is unrecognised.
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_advise_models"``."""
        return "sys_advise_models"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Recommend the best model per worker per task before fan-out. "
            "Each task specifies one or more agents to get recommendations "
            "for, plus an optional model list to constrain the pick. "
            "Returns one {agent, model, rationale} entry per agent entry. "
            "Use the returned model as args.model in sys_session_send. "
            "Advisory only. Available when OMNIGENT_SMART_ROUTING=1."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict describing the ``tasks`` parameter.
        """
        return {
            "type": "function",
            "function": {
                "name": SysAdviseModelsTool.name(),
                "description": SysAdviseModelsTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tasks": {
                            "type": "array",
                            "description": (
                                "The tasks to size. Each element describes "
                                "one planned sys_session_send dispatch."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {
                                        "type": "string",
                                        "description": "Short human label, e.g. 'auth-refactor'.",
                                    },
                                    "agents": {
                                        "type": "array",
                                        "description": (
                                            "The workers to get recommendations for. "
                                            "One recommendation is returned per entry."
                                        ),
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "agent": {
                                                    "type": "string",
                                                    "description": (
                                                        "Sub-agent name as declared in the spec, "
                                                        "e.g. 'claude_code'."
                                                    ),
                                                },
                                                "models": {
                                                    "description": (
                                                        "Model ids to pick from; "
                                                        "null = server defaults."
                                                    ),
                                                    "oneOf": [
                                                        {
                                                            "type": "array",
                                                            "items": {"type": "string"},
                                                        },
                                                        {"type": "null"},
                                                    ],
                                                },
                                            },
                                            "required": ["agent"],
                                            "additionalProperties": False,
                                        },
                                    },
                                    "task": {
                                        "type": "string",
                                        "description": (
                                            "Full task description — the text you will "
                                            "send to the workers as args.input."
                                        ),
                                    },
                                },
                                "required": ["title", "agents", "task"],
                                "additionalProperties": False,
                            },
                        }
                    },
                    "required": ["tasks"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Unreachable — execution is intercepted server-side.

        The server's MCP handler intercepts ``sys_advise_models`` in
        :func:`~omnigent.server.routes.sessions._handle_advise_models_mcp`
        and returns the result directly.  ``invoke`` is never called
        in practice; it exists only to satisfy the :class:`Tool`
        abstract interface.

        :param arguments: JSON-encoded arguments (unused).
        :param ctx: Tool execution context (unused).
        :raises RuntimeError: Always, if somehow reached.
        """
        del arguments, ctx
        raise RuntimeError(
            "sys_advise_models is handled server-side via the MCP "
            "intercept; this invoke() path should never be reached."
        )
