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
from omnigent.tools.builtins._arguments import parse_json_object_arguments


def filter_model_catalog(
    catalog: dict[str, Any],
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """
    Apply optional worker and model-id filters to a model catalog.

    Filtering is deliberately shared by the in-process tool and the
    runner-local dispatch path so both expose the same contract.

    :param catalog: Full ``sys_list_models`` payload.
    :param arguments: Parsed tool arguments.
    :returns: A filtered copy of the catalog.
    """
    workers = arguments.get("workers")
    worker_names = set(workers) if isinstance(workers, list) else None
    model_ids = arguments.get("model_ids")
    selected_models = set(model_ids) if isinstance(model_ids, list) else None

    filtered: dict[str, Any] = {}
    for worker, raw_row in catalog.items():
        if worker_names is not None and worker not in worker_names:
            continue
        if not isinstance(raw_row, dict):
            filtered[worker] = raw_row
            continue

        row = dict(raw_row)
        if selected_models is not None and isinstance(row.get("models"), list):
            row["models"] = [
                model
                for model in row["models"]
                if isinstance(model, dict) and model.get("id") in selected_models
            ]
        filtered[worker] = row
    return filtered


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
            "unfamiliar 'args.model' to sys_session_send, when the user "
            "asks which models are available, or to preflight a worker "
            "(source 'none' means dispatches to that worker cannot run "
            "in this deployment). Each entry reports {source, verified, "
            "models: [{id, family, context_window?}], note}; pick "
            "'args.model' values verbatim from the target worker's list."
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
                "name": SysListModelsTool.name(),
                "description": SysListModelsTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workers": {
                            "type": "array",
                            "items": {"type": "string"},
                            "uniqueItems": True,
                            "description": (
                                "Optional worker names to include, such as "
                                "'self' or a declared sub-agent name."
                            ),
                        },
                        "model_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "uniqueItems": True,
                            "description": (
                                "Optional exact model ids to retain in each "
                                "selected worker's models list."
                            ),
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Enumerate per-worker model availability (in-process path).

        :param arguments: JSON with optional ``workers`` and
            ``model_ids`` arrays.
        :param ctx: Tool execution context (unused).
        :returns: JSON mapping of worker name (plus ``"self"``) to its
            ``{source, verified, models, note}`` row, optionally filtered.
        """
        del ctx
        from omnigent.model_catalog import catalog_for_spec

        args, error = parse_json_object_arguments(arguments)
        if error is not None:
            return json.dumps({"error": error})
        assert args is not None
        return json.dumps(filter_model_catalog(catalog_for_spec(self._spec), args))
