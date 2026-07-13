from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from omnigent.dag_workflows import runtime
from omnigent.runner import app as runner_app


@pytest.fixture(autouse=True)
def _clean_subagent_state() -> Iterator[None]:
    runner_app._subagent_work_by_child.clear()
    runner_app._subagent_work_by_parent.clear()
    runner_app._session_inboxes_ref.clear()
    runner_app._drained_delivered_subagent_children.clear()
    yield
    runner_app._subagent_work_by_child.clear()
    runner_app._subagent_work_by_parent.clear()
    runner_app._session_inboxes_ref.clear()
    runner_app._drained_delivered_subagent_children.clear()


def test_workflow_completion_routes_without_parent_inbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivered: list[dict[str, Any]] = []

    def capture(
        parent_session_id: str,
        workflow_ref: dict[str, Any],
        payload: dict[str, Any],
    ) -> bool:
        delivered.append({"parent": parent_session_id, "ref": workflow_ref, "payload": payload})
        return True

    monkeypatch.setattr(runtime, "deliver_workflow_completion", capture)
    entry = runner_app.register_subagent_work(
        parent_session_id="conv_parent",
        child_session_id="conv_child",
        agent="codex",
        title="node-a",
        workflow_ref={"workflow_id": "wf", "node_id": "a", "attempt": 1},
    )
    ack = runner_app.mark_subagent_work_terminal(
        "conv_child",
        status="completed",
        output='<workflow_result>{"ok": true}</workflow_result>',
    )

    assert ack.delivered is True
    assert ack.delivered_now is True
    assert entry.workflow_delivered is True
    assert delivered[0]["payload"]["work_id"] == entry.work_id
    assert "conv_parent" not in runner_app._session_inboxes_ref
