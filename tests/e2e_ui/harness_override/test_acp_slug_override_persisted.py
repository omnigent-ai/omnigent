"""E2E: a per-session ``acp:<slug>`` override must PERSIST its slug.

The persistence half of the wrong-ACP-agent launch: when a session is created
for a non-ACP bundle with a namespaced ``acp:<slug>`` override, canonicalizing
it to bare ``acp`` at the create route drops the slug, the runner never learns
which ACP agent was selected, and spawn falls back to the first configured
agent. This is the root cause behind the wrong-agent launch that the sibling
``test_acp_slug_override_launches_named_agent.py`` drives end-to-end.

This test isolates the persistence contract at the API layer (no runner or LLM
needed): with two ACP agents configured (the reported precondition — the
create route validates the slug against the configured agents), create a
``hello_world`` (openai-agents, i.e. non-ACP) session with the Goose agent's
``acp:<slug>`` via the same JSON ``POST /v1/sessions`` the web UI uses, then
read the session snapshot back. The ``harness`` field reflects the persisted
per-session override verbatim (see ``_resolve_harness`` /
``conv.harness_override``). The assertion pins the *expected* behavior — the
snapshot preserves the namespaced value (identity consumers canonicalize on
their own, so preserving it is safe). It fails when the create route stores
bare ``acp``.
"""

from __future__ import annotations

import httpx

from tests.e2e_ui.harness_override.conftest import GOOSE_OVERRIDE


def test_acp_slug_override_is_persisted(
    live_server: str,
    two_acp_agents_config: None,
) -> None:
    """Creating a session with an ``acp:<slug>`` must keep the slug persisted.

    Journey (API surface): configure the two ACP agents, find the built-in
    ``hello_world`` (non-ACP, openai-agents) agent, create a session with the
    Goose ``acp:<slug>`` override, and read the session snapshot. The
    snapshot's ``harness`` field carries the persisted per-session override
    verbatim, so it must remain the namespaced value. When the create route
    canonicalizes it to bare ``acp``, the slug is lost and the runner cannot
    select the named agent.
    """
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
    # The JSON create path returns a SessionResponse (keyed ``id``); only the
    # multipart bundle-upload path returns ``session_id``.
    session_id = create_resp.json()["id"]

    try:
        snapshot = httpx.get(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        snapshot.raise_for_status()
        harness = snapshot.json()["harness"]

        # The runner selects the concrete ACP agent from this value, so the
        # slug must survive. Bare ``acp`` sends the runner to the first
        # configured agent instead of the named one.
        assert harness == GOOSE_OVERRIDE, (
            f"expected the session to preserve {GOOSE_OVERRIDE!r} for spawn selection, "
            f"but the snapshot harness is {harness!r} (the slug was dropped)"
        )
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
