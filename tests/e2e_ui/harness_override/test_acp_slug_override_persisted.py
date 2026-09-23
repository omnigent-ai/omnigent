"""The API preserves a namespaced ACP override on the session."""

from __future__ import annotations

import httpx

from tests.e2e_ui.harness_override.conftest import GOOSE_OVERRIDE


def test_acp_slug_override_is_persisted(
    live_server: str,
    two_acp_agents_config: None,
) -> None:
    agents_resp = httpx.get(f"{live_server}/v1/agents", timeout=10.0)
    agents_resp.raise_for_status()
    agent_id = next(
        (a["id"] for a in agents_resp.json()["data"] if a["name"] == "hello_world"),
        None,
    )
    assert agent_id is not None, "hello_world agent is not registered on the server"

    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        json={"agent_id": agent_id, "harness_override": GOOSE_OVERRIDE},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["id"]

    try:
        snapshot = httpx.get(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        snapshot.raise_for_status()
        harness = snapshot.json()["harness"]

        assert harness == GOOSE_OVERRIDE, (
            f"expected the session to preserve {GOOSE_OVERRIDE!r} for spawn selection, "
            f"but the snapshot harness is {harness!r} (the slug was dropped)"
        )
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
