"""End-to-end test for the ``detect_loop`` builtin policy.

Exercises the full YAML → parser → PolicyEngine → evaluate path
with real session-state persistence, verifying that repeated
identical tool calls trigger ASK and that diverse calls pass
through.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies import build_policy_engine
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.parser import parse
from omnigent.spec.types import Phase, PolicyAction
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

_YAML = """\
spec_version: 1
name: loop-detect-agent
guardrails:
  policies:
    loop_guard:
      type: function
      function:
        path: omnigent.policies.builtins.safety.detect_loop
        arguments:
          window: 5
          threshold: 3
"""


def _build(tmp_path: Path, store: SqlAlchemyConversationStore) -> PolicyEngine:
    (tmp_path / "config.yaml").write_text(_YAML)
    spec = parse(tmp_path)
    conv = store.create_conversation()
    return build_policy_engine(
        spec=spec, conversation_id=conv.id, conversation_store=store
    )


def _tool_ctx(name: str, args: dict | None = None) -> EvaluationContext:
    return EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": name, "arguments": args or {}},
        tool_name=name,
    )


@pytest.mark.asyncio
async def test_detect_loop_asks_on_repeated_tool_calls(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Three identical tool calls within the window trigger ASK."""
    engine = _build(tmp_path, conversation_store)
    ctx = _tool_ctx("Bash", {"command": "cat /nonexistent"})

    r1 = await engine.evaluate(ctx)
    assert r1.action == PolicyAction.ALLOW

    r2 = await engine.evaluate(ctx)
    assert r2.action == PolicyAction.ALLOW

    r3 = await engine.evaluate(ctx)
    assert r3.action == PolicyAction.ASK
    assert "retry loop" in (r3.reason or "")


@pytest.mark.asyncio
async def test_detect_loop_allows_diverse_tool_calls(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Distinct tool calls never trigger, even with many calls."""
    engine = _build(tmp_path, conversation_store)

    for i in range(10):
        r = await engine.evaluate(_tool_ctx("Bash", {"command": f"echo {i}"}))
        assert r.action == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_detect_loop_window_evicts_old_entries(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Old entries outside the window stop counting.

    With window=5, threshold=3: two identical calls, then three
    different calls push the first two out of the window, so a
    third identical call does not trigger.
    """
    engine = _build(tmp_path, conversation_store)
    repeated = _tool_ctx("Bash", {"command": "fail"})

    r1 = await engine.evaluate(repeated)
    assert r1.action == PolicyAction.ALLOW
    r2 = await engine.evaluate(repeated)
    assert r2.action == PolicyAction.ALLOW

    for i in range(3):
        r = await engine.evaluate(_tool_ctx("Read", {"path": f"/file{i}"}))
        assert r.action == PolicyAction.ALLOW

    # The two early "fail" calls have been evicted from the window.
    r3 = await engine.evaluate(repeated)
    assert r3.action == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_detect_loop_non_tool_call_phases_pass_through(
    tmp_path: Path,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Request and response phases are not affected."""
    engine = _build(tmp_path, conversation_store)

    for _ in range(5):
        r = await engine.evaluate(
            EvaluationContext(
                phase=Phase.REQUEST, content="same message", tool_name=None
            )
        )
        assert r.action == PolicyAction.ALLOW
