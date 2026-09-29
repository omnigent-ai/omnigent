"""
Unit tests for the runner's session.status "waiting" backwards-compat gate.

The runner emits ``session.status: "waiting"`` (PR #930) only to servers new
enough to serialize it; older servers (< 0.3.0) 500 on ``GET /v1/sessions``, so
the runner downgrades "waiting"→"running" for them. Here we test the pure
version-comparison; the probe + downgrade are exercised end-to-end by
tests/e2e/test_waiting_status_compat_e2e.py (old server + new runner -> no 500).
"""

from __future__ import annotations

import pytest

from omnigent.runner.app import _version_supports_waiting_status


@pytest.mark.parametrize(
    ("server_version", "expected"),
    [
        ("0.2.0", False),  # the released version that 500s on "waiting"
        ("0.2.5", False),
        ("0.1.1", False),
        ("0.3.0", True),  # first release that models "waiting"
        ("0.3.0.dev0", True),  # main: dev of the supporting release still supports it
        ("0.3.1", True),
        ("0.4.0", True),  # later minor: still supports "waiting"
        ("1.0.0", True),
        ("source", False),  # source installs can report non-PEP-440 metadata
        ("not-a-version", False),  # malformed probes fail closed
    ],
)
def test_version_supports_waiting_status(server_version: str, expected: bool) -> None:
    assert _version_supports_waiting_status(server_version) is expected


async def test_waiting_downgraded_on_the_wire_is_recorded_as_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old server hears ``running``; the runner's own record keeps ``waiting``.

    A codex-native parent whose turn ends with a child still running publishes
    ``waiting``. The book must not turn that into a running claim that would
    keep the parent's pane alive after its children finish.
    """
    import asyncio
    import uuid

    from omnigent.runner import app as runner_app
    from omnigent.spec.types import AgentSpec, ExecutorSpec
    from tests.runner.conftest import (
        _FakeProcessManager,
        _runner_client,
        _ScriptedHarnessClient,
        _sse,
    )
    from tests.runner.helpers import NullServerClient

    monkeypatch.setattr(runner_app, "_server_version", None)
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    harness = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_1"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
        ]
    )
    pm = _FakeProcessManager(harness)
    app = runner_app.create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(runner_app, "_launch_native_terminal", _no_terminal)
    conv_id = uuid.uuid4().hex
    child_id = f"{conv_id}_child"
    runner_app.register_subagent_work(
        parent_session_id=conv_id, child_session_id=child_id, agent="w", title="t"
    ).status = "running"
    try:
        async with _runner_client(app) as client:
            created = await client.post(
                "/v1/sessions", json={"session_id": conv_id, "agent_id": "agent_1"}
            )
            assert created.status_code == 201, created.text
            started = await client.post(
                f"/v1/sessions/{conv_id}/events",
                json={
                    "type": "message",
                    "agent_id": "agent_1",
                    "content": [{"type": "input_text", "text": "delegate"}],
                },
            )
            assert started.status_code == 202, started.text
            for _ in range(300):
                if conv_id not in app.state.active_turns:
                    break
                await asyncio.sleep(0.01)
        queue = runner_app._session_event_queues_ref[conv_id]
        wire = [
            event["status"]
            for event in (queue.get_nowait() for _ in range(queue.qsize()))
            if isinstance(event, dict) and event.get("type") == "session.status"
        ]
        record = app.state.session_status_book.current(conv_id)
        assert wire == ["running", "running"]
        assert record is not None
        assert record.status == "waiting"
        assert app.state.session_status_book.claim(conv_id) is None
    finally:
        runner_app._subagent_work_by_child.pop(child_id, None)
        runner_app._subagent_work_by_parent.pop(conv_id, None)
        runner_app._session_event_queues_ref.pop(conv_id, None)


async def _no_terminal(harness_name: str, ctx: object, **_kw: object) -> bool:
    del harness_name, ctx
    return True
