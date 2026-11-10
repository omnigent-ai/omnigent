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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server_version", "expected_status"),
    [("0.21.0", "waiting"), (None, "idle"), ("0.2.0", "idle"), ("source", "idle")],
)
async def test_background_agent_waiting_edge_names_its_agents(
    monkeypatch: pytest.MonkeyPatch, server_version: str | None, expected_status: str
) -> None:
    """A background-agent ``waiting`` edge names the agents; old or unknown servers get idle.

    ``running`` would hold follow-ups behind Steer although the turn ended, and
    a pre-0.3.0 server 500s its session reads on ``waiting``.
    """
    import asyncio

    import httpx

    from omnigent.runner.app import _session_event_queues_ref, create_runner_app

    conv_id = "conv_waiting_on_agent"
    agents: list[dict[str, object]] = [{"id": "a1", "type": "subagent", "description": "Audit"}]
    monkeypatch.setattr("omnigent.runner.app._server_version", server_version)
    async with httpx.AsyncClient(base_url="http://server.invalid") as server_client:
        app = create_runner_app(server_client=server_client)
    registry = app.state.session_resource_registry
    monkeypatch.setattr(registry, "session_background_agents", lambda _sid: agents)
    _session_event_queues_ref.pop(conv_id, None)

    registry._session_status_publisher(conv_id, "waiting", None)
    registry._session_status_publisher(conv_id, "idle", None)

    queue = _session_event_queues_ref.pop(conv_id)
    events: list[object] = []
    while True:
        try:
            events.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    expected_waiting: dict[str, object] = {"type": "session.status", "status": expected_status}
    if expected_status == "waiting":
        expected_waiting["background_agents"] = agents
    assert events == [expected_waiting, {"type": "session.status", "status": "idle"}]
