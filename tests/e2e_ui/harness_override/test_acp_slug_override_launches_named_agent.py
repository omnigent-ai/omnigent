"""E2E: a per-session ``acp:<slug>`` override must launch the NAMED ACP agent.

The user journey:

1. Configure two generic ACP agents in the global ``acp:`` block of
   ``~/.omnigent/config.yaml``, with *Gemini listed before Goose*.
2. Create a session for a bundle whose harness is **not** generic ACP (here the
   built-in ``hello_world`` openai-agents agent) and set ``harness_override`` to
   the Goose agent's ``acp:<slug>`` (the web new-chat harness picker's
   per-session override).
3. Send the first message in the web chat.

When the override loses its slug anywhere between session create and spawn,
the runner never learns which ACP agent was picked and
``_build_acp_spawn_env`` falls back to the *first* configured agent
(Fake Gemini) — so the reply comes from Gemini, not Goose.

Expected: the session preserves the ``acp:<slug>`` selection for spawn, so the
runner launches Goose and the reply comes from Fake Goose.

This test drives that journey for real against a live server + runner: it
registers two hermetic fake ACP agents (each a Python script speaking the Agent
Client Protocol over stdio, replying with text that names *which* agent
answered), creates the ``hello_world`` session with the Goose override via the
same JSON ``POST /v1/sessions`` the web UI uses, sends a chat message, and
asserts the reply is Fake Goose's.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online
from tests.e2e_ui.harness_override.conftest import GEMINI_REPLY, GOOSE_OVERRIDE, GOOSE_REPLY


@pytest.fixture
def acp_override_session(
    live_server: str,
    runner_id: str,
    two_acp_agents_config: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """Create a ``hello_world`` session overridden to the Goose ACP agent.

    Uses the same JSON ``POST /v1/sessions`` (agent_id + harness_override) the
    web new-chat harness picker uses, then binds the session to the spawned
    runner. The bound agent's own harness is openai-agents (a non-ACP bundle),
    exactly the report's precondition.

    :param live_server: Spawned server base URL.
    :param runner_id: Token-bound runner id to bind the session to.
    :param two_acp_agents_config: Ensures the two ACP agents are configured
        before the session (and thus the harness) is created.
    :param tmp_path_factory: Temp directories for a replacement runner's logs.
    :returns: ``(base_url, session_id)``.
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

    respawned_runner: subprocess.Popen[bytes] | None = None
    try:
        respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
        patch_resp = httpx.patch(
            f"{live_server}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=10.0,
        )
        patch_resp.raise_for_status()
        yield (live_server, session_id)
    finally:
        try:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        finally:
            if respawned_runner is not None:
                respawned_runner.terminate()
                try:
                    respawned_runner.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    respawned_runner.kill()
                    respawned_runner.wait(timeout=5)


def test_acp_slug_override_launches_named_agent(
    page: Page,
    acp_override_session: tuple[str, str],
) -> None:
    """The ``acp:<slug>`` override must launch Goose, not the first ACP agent.

    Journey: open the Goose-overridden session, send a message, and read the
    reply. The assertion pins the *correct* behavior — the reply comes from
    Fake Goose (the named agent) — so it fails whenever the override loses its
    slug and the runner launches the first configured agent (Fake Gemini)
    instead.
    """
    base_url, session_id = acp_override_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    composer.fill("Say hello")
    composer.press("Enter")

    # The launched ACP agent streams a deterministic reply naming itself. A
    # slug-losing regression launches Fake Gemini (first configured), so the
    # GOOSE reply never appears and this expectation times out.
    expect(page.get_by_text(GOOSE_REPLY)).to_be_visible(timeout=90_000)

    # And the WRONG agent (the first configured one) must not have answered.
    expect(page.get_by_text(GEMINI_REPLY)).to_have_count(0)
