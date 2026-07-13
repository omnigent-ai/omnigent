"""LLM-visible schemas for the runner-local workflow DAG tools."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class _WorkflowTool(Tool):
    tool_name: str
    tool_description: str
    parameters: dict[str, Any]

    @classmethod
    def name(cls) -> str:
        return cls.tool_name

    @classmethod
    def description(cls) -> str:
        return cls.tool_description

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": self.parameters,
            },
        }


# Node schema shared by submit (nodes) and amend (add_nodes / replace_nodes),
# kept in lockstep with omnigent.dag_workflows.models.WorkflowNode. The runtime
# rejects unknown fields (pydantic ``extra="forbid"``), so the schema is closed
# to steer the model onto the exact field names rather than plausible guesses
# (``input``/``purpose``/``depends_on`` all fail validation).
_WORKFLOW_NODE_SCHEMA = {
    "type": "object",
    "required": ["id", "title", "contract", "agent"],
    "additionalProperties": False,
    "properties": {
        "id": {
            "type": "string",
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
            "description": "Unique node id within the DAG, e.g. 'explore_1070'.",
        },
        "title": {
            "type": "string",
            "description": "Short human-readable label shown in the DAG UI.",
        },
        "contract": {
            "type": "string",
            "description": (
                "The full task instructions sent to the child agent. Must require a "
                "final <workflow_result>{...}</workflow_result> block matching output_schema."
            ),
        },
        "agent": {
            "type": "string",
            "description": "Sub-agent name as declared in the spec, e.g. 'claude_code'.",
        },
        "deps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "IDs of nodes that must succeed first. Omit or [] for a root node.",
        },
        "role": {
            "type": "string",
            "enum": ["investigate", "implement", "review", "generic"],
            "description": "Node role. Defaults to 'generic'.",
        },
        "model": {
            "type": ["string", "null"],
            "description": "Optional model id override; null uses the worker default.",
        },
        "output_schema": {
            "type": "object",
            "description": (
                "JSON Schema the child's <workflow_result> must satisfy. "
                "Defaults to {'type': 'object'}."
            ),
            "additionalProperties": True,
        },
        "max_attempts": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "description": "Retry budget for this node. Defaults to 2.",
        },
        "cost_budget": {
            "type": "object",
            "required": ["max_cost_usd"],
            "additionalProperties": False,
            "properties": {"max_cost_usd": {"type": "number", "exclusiveMinimum": 0}},
            "description": (
                "Per-node cost cap. REQUIRED on every node when the workflow budget "
                "sets max_cost_usd."
            ),
        },
        "worktree_path": {
            "type": ["string", "null"],
            "description": "Optional git worktree path for the child agent.",
        },
    },
}

# Kept in lockstep with omnigent.dag_workflows.models.WorkflowBudget. Shared by
# the definition schema (submit) and the amend delta.
_WORKFLOW_BUDGET_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "description": "Workflow-wide execution limits.",
    "properties": {
        "max_concurrency": {
            "type": "integer",
            "minimum": 1,
            "description": "Max nodes running at once. Defaults to 4.",
        },
        "max_dispatches": {
            "type": "integer",
            "minimum": 1,
            "description": "Max total child dispatches (attempts). Defaults to 100.",
        },
        "max_cost_usd": {
            "type": ["number", "null"],
            "exclusiveMinimum": 0,
            "description": (
                "Optional total cost cap. When set, EVERY node must declare "
                "its own cost_budget.max_cost_usd."
            ),
        },
    },
}

_WORKFLOW_DEFINITION_SCHEMA = {
    "type": "object",
    "required": ["id", "name", "nodes"],
    "additionalProperties": False,
    "description": "Static workflow DAG. Nodes run once all their deps succeed.",
    "properties": {
        "id": {
            "type": "string",
            "pattern": r"^[A-Za-z0-9][A-Za-z0-9_.-]*$",
            "description": "Workflow id, unique per parent session.",
        },
        "name": {"type": "string", "description": "Human-readable workflow name."},
        "version": {
            "type": "integer",
            "minimum": 1,
            "description": "Definition version. Defaults to 1.",
        },
        "budget": _WORKFLOW_BUDGET_SCHEMA,
        "nodes": {
            "type": "array",
            "minItems": 1,
            "items": _WORKFLOW_NODE_SCHEMA,
            "description": "The DAG nodes. Each is one child-agent execution.",
        },
    },
}


class SysWorkflowSubmitTool(_WorkflowTool):
    tool_name = "sys_workflow_submit"
    tool_description = (
        "Validate and save a static DAG workflow as a draft. This does not dispatch agents; "
        "call sys_workflow_start with the returned version and definition_hash after approval. "
        "Nodes use fields id/title/contract/agent/deps (NOT input/purpose/depends_on)."
    )
    parameters = {
        "type": "object",
        "required": ["definition"],
        "properties": {"definition": _WORKFLOW_DEFINITION_SCHEMA},
        "additionalProperties": False,
    }


class SysWorkflowAmendTool(_WorkflowTool):
    tool_name = "sys_workflow_amend"
    tool_description = (
        "Create a new workflow version by adding, replacing, or removing nodes that have not "
        "started. The amended workflow must be approved and started again."
    )
    parameters = {
        "type": "object",
        "required": ["workflow_id", "expected_version", "delta"],
        "properties": {
            "workflow_id": {"type": "string"},
            "expected_version": {"type": "integer", "minimum": 1},
            "delta": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "budget": _WORKFLOW_BUDGET_SCHEMA,
                    "add_nodes": {"type": "array", "items": _WORKFLOW_NODE_SCHEMA},
                    "replace_nodes": {"type": "array", "items": _WORKFLOW_NODE_SCHEMA},
                    "remove_node_ids": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
                "description": "Only nodes that have not started can be added/replaced/removed.",
            },
        },
        "additionalProperties": False,
    }


class SysWorkflowStartTool(_WorkflowTool):
    tool_name = "sys_workflow_start"
    tool_description = (
        "Start or resume an approved workflow definition. Version and definition_hash must match "
        "the current draft exactly."
    )
    parameters = {
        "type": "object",
        "required": ["workflow_id", "version", "definition_hash"],
        "properties": {
            "workflow_id": {"type": "string"},
            "version": {"type": "integer", "minimum": 1},
            "definition_hash": {"type": "string", "minLength": 64, "maxLength": 64},
        },
        "additionalProperties": False,
    }


class SysWorkflowGetTool(_WorkflowTool):
    tool_name = "sys_workflow_get"
    tool_description = "Return one workflow's current state, node results, attempts, and errors."
    parameters = {
        "type": "object",
        "required": ["workflow_id"],
        "properties": {"workflow_id": {"type": "string"}},
        "additionalProperties": False,
    }


class SysWorkflowCancelTool(_WorkflowTool):
    tool_name = "sys_workflow_cancel"
    tool_description = "Cancel a workflow and interrupt its running child-agent sessions."
    parameters = {
        "type": "object",
        "required": ["workflow_id"],
        "properties": {"workflow_id": {"type": "string"}},
        "additionalProperties": False,
    }


WORKFLOW_TOOLS = (
    SysWorkflowSubmitTool,
    SysWorkflowAmendTool,
    SysWorkflowStartTool,
    SysWorkflowGetTool,
    SysWorkflowCancelTool,
)
