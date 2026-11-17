"""A CLI query recognized by ``data_queries:`` stays an ordinary, policy-gated shell call."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.data_queries import classify_tool_call
from omnigent.policies.schema import PolicyEvent
from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies import _enforce_policy, build_policy_engine
from omnigent.spec import load
from omnigent.spec.types import Phase, PolicyAction
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def deny_destructive_psql(event: PolicyEvent) -> dict[str, str]:
    """Deny a shell command that drops a table; allow everything else."""
    arguments = event["data"].get("arguments")
    command = arguments.get("command", "") if isinstance(arguments, dict) else ""
    if "DROP TABLE" in str(command).upper():
        return {"result": "deny", "reason": "No dropping tables."}
    return {"result": "allow"}


def _spec_path(tmp_path: Path, *, data_queries: dict[str, object] | None) -> Path:
    config: dict[str, object] = {
        "name": "analyst",
        "prompt": "You are a test agent.",
        "executor": {"model": "openai/gpt-5", "harness": "claude-sdk"},
        "policies": {
            "deny_destructive_psql": {
                "type": "function",
                "on": ["tool_call:sys_os_shell"],
                "function": {"path": f"{__name__}.deny_destructive_psql"},
            }
        },
    }
    if data_queries is not None:
        config["data_queries"] = data_queries
    path = tmp_path / ("with.yaml" if data_queries is not None else "without.yaml")
    path.write_text(yaml.dump(config))
    return path


@pytest.mark.asyncio
async def test_shell_policy_still_gates_a_classified_cli_query(
    tmp_path: Path, conversation_store: SqlAlchemyConversationStore
) -> None:
    """Declaring psql as a data-query tool changes no shell policy decision."""
    data_queries = {"rules": [{"name": "team_psql", "shell": "psql", "sql": {"flag": "-c"}}]}
    with_block = load(_spec_path(tmp_path, data_queries=data_queries))
    without_block = load(_spec_path(tmp_path, data_queries=None))
    assert with_block.data_queries is not None

    for command, expected in (
        ("psql -d shop -c 'DROP TABLE orders'", (PolicyAction.DENY, "No dropping tables.")),
        ("psql -d shop -c 'SELECT 1'", (PolicyAction.ALLOW, None)),
    ):
        arguments = {"command": command}
        query = classify_tool_call("sys_os_shell", arguments, spec=with_block.data_queries)
        assert query is not None and query.rule == "team_psql"
        decisions = []
        for spec in (with_block, without_block):
            engine = build_policy_engine(
                spec=spec,
                conversation_id=conversation_store.create_conversation().id,
                conversation_store=conversation_store,
            )
            result = await _enforce_policy(
                engine,
                EvaluationContext(
                    phase=Phase.TOOL_CALL,
                    content={"name": "sys_os_shell", "arguments": arguments},
                    tool_name="sys_os_shell",
                ),
            )
            decisions.append((result.action, result.reason))
        assert decisions == [expected, expected], command
