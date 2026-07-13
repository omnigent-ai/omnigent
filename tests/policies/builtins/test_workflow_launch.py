from omnigent.policies.builtins.orchestration import workflow_launch_approval


def _event(definition_hash: str, approved_hash: str | None = None) -> dict[str, object]:
    labels = {} if approved_hash is None else {"workflow.approved_hash": approved_hash}
    return {
        "type": "tool_call",
        "data": {
            "name": "sys_workflow_start",
            "arguments": {"definition_hash": definition_hash},
        },
        "context": {"labels": labels},
    }


def test_workflow_launch_asks_once_per_definition_hash() -> None:
    policy = workflow_launch_approval()
    first = policy(_event("abc"))
    assert first == {
        "result": "ASK",
        "reason": "Approve this static workflow DAG and its declared execution budget?",
        "set_labels": {"workflow.approved_hash": "abc"},
    }
    assert policy(_event("abc", "abc")) == {"result": "ALLOW"}
    assert policy(_event("def", "abc"))["result"] == "ASK"


def test_workflow_launch_ignores_other_tools() -> None:
    policy = workflow_launch_approval()
    assert policy({"type": "tool_call", "data": {"name": "sys_workflow_get"}}) == {
        "result": "ALLOW"
    }
