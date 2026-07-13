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


class SysWorkflowSubmitTool(_WorkflowTool):
    tool_name = "sys_workflow_submit"
    tool_description = (
        "Validate and save a static DAG workflow as a draft. This does not dispatch agents; "
        "call sys_workflow_start with the returned version and definition_hash after approval."
    )
    parameters = {
        "type": "object",
        "required": ["definition"],
        "properties": {
            "definition": {
                "type": "object",
                "required": ["id", "name", "nodes"],
                "description": "Static workflow definition with budget, nodes, and dependencies.",
                "additionalProperties": True,
            }
        },
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
                    "budget": {"type": "object"},
                    "add_nodes": {"type": "array", "items": {"type": "object"}},
                    "replace_nodes": {"type": "array", "items": {"type": "object"}},
                    "remove_node_ids": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
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
