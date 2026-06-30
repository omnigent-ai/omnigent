"""Built-in tool: sys_advise_models — fan-out model sizing advisor.

Recommends a model tier for each sub-agent task the orchestrator is
about to launch, using the server's :class:`~omnigent.server.smart_routing.RoutingClient`.
The tool is available when ``RuntimeCaps.routing_client`` is configured
(i.e. ``OMNIGENT_SMART_ROUTING=1`` with an ``llm:`` config block) and
is advisory-only — it does not enforce the recommended model on the
resulting ``sys_session_send`` calls.

Real dispatch logic lives in
:func:`~omnigent.runner.tool_dispatch._execute_advise_models_tool`; the
:meth:`invoke` below is the in-process fallback path (raises a clear
error rather than silently returning wrong data).
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
            "Recommend the best model for each sub-agent task before "
            "fan-out. Pass the list of tasks you are about to dispatch "
            "and receive a per-task {model, tier, rationale}. Use the "
            "returned model as args.model in the matching sys_session_send "
            "call. Advisory only — the recommendation is not enforced. "
            "Available when the server routing client is configured "
            "(OMNIGENT_SMART_ROUTING=1 + llm: config)."
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
                                    "agent": {
                                        "type": "string",
                                        "description": (
                                            "Sub-agent name as declared in the spec, "
                                            "e.g. 'claude_code'."
                                        ),
                                    },
                                    "task": {
                                        "type": "string",
                                        "description": (
                                            "Full task description — the text you will "
                                            "send to the worker as args.input."
                                        ),
                                    },
                                },
                                "required": ["title", "agent", "task"],
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
        In-process fallback (raises — real dispatch is in the runner).

        :param arguments: JSON-encoded arguments (unused).
        :param ctx: Tool execution context (unused).
        :raises RuntimeError: Always — this path is not supported; the
            runner intercepts ``sys_advise_models`` before it reaches
            ``ToolManager.call_tool``.
        """
        del arguments, ctx
        raise RuntimeError(
            "sys_advise_models must be dispatched via the runner "
            "(_execute_advise_models_tool); in-process invocation is "
            "not supported."
        )
