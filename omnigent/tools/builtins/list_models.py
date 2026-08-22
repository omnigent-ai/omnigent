"""Built-in tool: sys_list_models — per-worker model availability.

Registers alongside the sub-agent dispatch surface so an orchestrator
can deterministically learn which models each worker (and it itself)
can run before passing an ``args.model`` to ``sys_session_send``. The
enumeration logic lives in :mod:`omnigent.model_catalog`; the runner
dispatches this tool locally (see ``omnigent/runner/tool_dispatch.py``)
and the in-process path runs the same enumerator via :meth:`invoke`.
"""

from __future__ import annotations

import json

# Any: tool schemas are heterogeneous dicts.
from typing import Any

from omnigent.spec.types import AgentSpec
from omnigent.tools.base import Tool, ToolContext


class SysListModelsTool(Tool):
    """
    List the models each sub-agent worker (and the caller) can run.

    Returns a JSON object mapping each declared sub-agent name — plus
    ``"self"`` for the calling agent's own harness — to
    ``{source, verified, models: [{id, family, context_window?}], note}``.
    Lists are resolved from each worker's actual model provider and
    filtered to the model family its harness can run, so every id in a
    worker's list is dispatchable to that worker via
    ``sys_session_send`` ``args.model``.

    :param spec: The calling agent's parsed :class:`AgentSpec`.
    """

    def __init__(self, spec: AgentSpec) -> None:
        """
        Create a list-models tool bound to the calling agent's spec.

        :param spec: The calling agent's spec (sub-agents enumerated
            from ``spec.sub_agents``).
        """
        self._spec = spec

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_list_models"``."""
        return "sys_list_models"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "List the models each sub-agent worker — and this agent "
            "itself, under 'self' — can actually run here, resolved from "
            "each worker's real model provider and filtered to its "
            "harness's model family. Call this BEFORE passing an "
            "unfamiliar 'args.model' to sys_session_send, or when the user "
            "asks which models are available. Each entry reports {source, "
            "verified, models: [{id, family, context_window?}], note}; pick "
            "'args.model' values verbatim from the target worker's list. An "
            "EMPTY list is not by itself a dead worker — read 'source': "
            "'self-managed' means the harness authenticates itself and "
            "Omnigent cannot enumerate its models, so dispatch normally and "
            "omit 'args.model'; 'unconfigured' means no provider is set up "
            "for that worker; 'unavailable' means its listing could not be "
            "fetched. Only a note saying dispatches 'cannot run here' is a "
            "launch-blocking verdict."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema (no parameters).

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict.
        """
        return {
            "type": "function",
            "function": {
                "name": SysListModelsTool.name(),
                "description": SysListModelsTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Enumerate per-worker model availability (in-process path).

        :param arguments: Ignored — the tool takes no parameters.
        :param ctx: Tool execution context (unused).
        :returns: JSON mapping of worker name (plus ``"self"``) to its
            ``{source, verified, models, note}`` row.
        """
        del arguments, ctx
        from omnigent.model_catalog import catalog_for_spec

        return json.dumps(catalog_for_spec(self._spec))
