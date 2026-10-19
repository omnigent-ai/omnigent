"""Scheduled-task mutations require approval even without configured guardrails.

``build_policy_engine`` injects the mandatory approval guards into every engine
and ``any_policies_apply`` never fast-paths their tools; reads and deletes stay
allowed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.entities import Conversation
from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.builder import any_policies_apply, build_policy_engine
from omnigent.spec.parser import parse
from omnigent.spec.types import AgentSpec, Phase, PolicyAction
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

_TASK_ID = "0123456789abcdef0123456789abcdef"
_CREATE_ARGS = {
    "name": "nightly triage",
    "prompt": "Triage new issues.",
    "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
    "agent_id": "ag_1",
}


def _no_guardrails_spec(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
) -> tuple[AgentSpec, Conversation]:
    (tmp_path / "config.yaml").write_text("spec_version: 1\nname: scheduler-probe\n")
    return parse(tmp_path), conversation_store.create_conversation()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments", "expected"),
    [
        ("sys_add_policy", {"name": "shell_gate", "handler": "x.y"}, PolicyAction.ASK),
        ("sys_scheduled_task_create", _CREATE_ARGS, PolicyAction.ASK),
        (
            "sys_scheduled_task_update",
            {"scheduled_task_id": _TASK_ID, "state": "paused"},
            PolicyAction.ASK,
        ),
    ],
    ids=["add_policy_control", "create", "update"],
)
async def test_scheduled_task_mutation_asks_without_guardrails(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
    tool_name: str,
    arguments: dict[str, object],
    expected: PolicyAction,
) -> None:
    spec, conv = _no_guardrails_spec(tmp_path, conversation_store)
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    result = await engine.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": tool_name, "arguments": arguments},
            tool_name=tool_name,
        )
    )
    assert result.action == expected, result.reason


@pytest.mark.parametrize(
    "tool_name",
    ["sys_add_policy", "sys_scheduled_task_create", "sys_scheduled_task_update"],
)
def test_no_policy_fast_path_keeps_mutation_gates(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
    tool_name: str,
) -> None:
    spec, conv = _no_guardrails_spec(tmp_path, conversation_store)
    assert any_policies_apply(
        spec=spec,
        conversation_id=conv.id,
        default_policies=None,
        policy_store=None,
        phase=Phase.TOOL_CALL,
        tool_name=tool_name,
        conversation=conv,
    )


@pytest.mark.parametrize("tool_name", ["sys_scheduled_task_list", "sys_scheduled_task_delete"])
def test_no_policy_fast_path_skips_ungated_scheduled_task_tools(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
    tool_name: str,
) -> None:
    spec, conv = _no_guardrails_spec(tmp_path, conversation_store)
    assert not any_policies_apply(
        spec=spec,
        conversation_id=conv.id,
        default_policies=None,
        policy_store=None,
        phase=Phase.TOOL_CALL,
        tool_name=tool_name,
        conversation=conv,
    )
