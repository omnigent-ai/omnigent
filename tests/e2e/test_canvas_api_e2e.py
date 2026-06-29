"""E2E: the canvas REST round-trips on the live server (#2).

Creates a runner-bound session, upserts a canvas via ``PUT /v1/canvas/{id}``,
and reads it back via ``GET`` — proving the canvas store + routes are wired end
to end. (The agent-driven ``set_canvas`` tool path is unit-covered in
``tests/runner/test_work_management_dispatch`` / the native relay; like the
other proxied builtins it needs a real LLM to exercise via the bridge.)
"""

from __future__ import annotations

import uuid

import httpx

from tests.e2e.conftest import create_runner_bound_session, register_inline_agent


def test_canvas_put_then_get_roundtrips(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    agent_name = register_inline_agent(
        http_client,
        name=f"canvas-agent-{uuid.uuid4().hex[:6]}",
        harness="claude-sdk",
        model=f"mock-canvas-{uuid.uuid4().hex[:6]}",
        profile="",
        prompt="You are a test assistant.",
        mock_llm_base_url=mock_llm_server_url,
    )
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )

    marker = f"<h1>canvas-{uuid.uuid4().hex[:8]}</h1>"
    put = http_client.put(
        f"/v1/canvas/{session_id}",
        json={"title": "Report", "content": marker, "content_type": "html"},
    )
    put.raise_for_status()

    got = http_client.get(f"/v1/canvas/{session_id}").json()
    assert got["content"] == marker
    assert got["content_type"] == "html"
