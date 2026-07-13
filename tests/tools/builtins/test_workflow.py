"""Unit tests for the LLM-visible workflow DAG tool schemas.

The submit/amend tools advertise the node shape to the model. If the
advertised schema drifts from :class:`~omnigent.dag_workflows.models.WorkflowNode`
(``extra="forbid"``), the model guesses field names and hits a wall of
validation errors on first submit. These tests pin the schema to the model.
"""

from __future__ import annotations

import jsonschema

from omnigent.dag_workflows.models import WorkflowDefinition, WorkflowNode
from omnigent.tools.builtins.workflow import (
    SysWorkflowAmendTool,
    SysWorkflowSubmitTool,
)


def _definition_schema() -> dict:
    params = SysWorkflowSubmitTool().get_schema()["function"]["parameters"]
    return params["properties"]["definition"]


def test_submit_and_amend_schemas_are_valid_json_schema() -> None:
    for tool in (SysWorkflowSubmitTool(), SysWorkflowAmendTool()):
        params = tool.get_schema()["function"]["parameters"]
        jsonschema.Draft202012Validator.check_schema(params)


def test_node_schema_fields_match_model() -> None:
    """Advertised node fields must exactly match WorkflowNode — no more, no less.

    Guards against the model guessing ``input``/``purpose``/``depends_on`` because
    the real ``contract``/``role``/``deps`` were never advertised.
    """
    node_schema = _definition_schema()["properties"]["nodes"]["items"]
    assert set(node_schema["properties"]) == set(WorkflowNode.model_fields)
    # Fields that are required on the model (no default) must be required here.
    assert set(node_schema["required"]) == {"id", "title", "contract", "agent"}


def test_schema_forbids_extra_node_fields() -> None:
    """The advertised node schema is closed, mirroring ``extra='forbid'``."""
    node_schema = _definition_schema()["properties"]["nodes"]["items"]
    assert node_schema["additionalProperties"] is False


def test_schema_shaped_definition_validates_against_model() -> None:
    """A definition built to the advertised schema passes model validation."""
    definition = {
        "id": "investigate-prs",
        "name": "Investigate PRs",
        "budget": {"max_concurrency": 3, "max_dispatches": 20, "max_cost_usd": 20},
        "nodes": [
            {
                "id": "explore_a",
                "title": "explore-a",
                "contract": "Investigate PR A and emit <workflow_result>{...}</workflow_result>.",
                "agent": "claude_code",
                "role": "investigate",
                "cost_budget": {"max_cost_usd": 5},
            },
            {
                "id": "synthesis",
                "title": "synthesis",
                "contract": "Combine findings and emit <workflow_result>{...}</workflow_result>.",
                "agent": "claude_code",
                "deps": ["explore_a"],
                "cost_budget": {"max_cost_usd": 5},
            },
        ],
    }
    jsonschema.validate(definition, _definition_schema())
    # And the runtime model accepts the same payload.
    WorkflowDefinition.model_validate(definition)
