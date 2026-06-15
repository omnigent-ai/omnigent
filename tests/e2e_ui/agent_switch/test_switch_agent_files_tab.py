"""E2E: the Files tab follows os_env availability across an in-place agent switch.

The web Files tab is gated on the session's environment resource (the
runner 404s it when the bound agent's spec has no ``os_env``). An
in-place agent switch can cross that boundary in either direction, and
nothing about the page navigates — the tab only updates because the
switch route's post-reset ``session.changed_files.invalidated`` SSE
event drives a ``workspace-environment`` refetch in the chat store.
This test exercises that full server → SSE → react-query → DOM chain in
a real browser; no LLM turn is involved (the switch runs while idle).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _FILES_PROBE_ENV_AGENT_NAME,
    _FILES_PROBE_NO_ENV_AGENT_NAME,
    _ensure_runner_online,
    _server_state,
)

# The header's "Switch agent" button was removed from the web UI; the
# SwitchAgentDialog component and the /switch-agent route still exist but
# have no UI entry point, so this browser-driven flow can't run. The env
# var is the opt-in to re-run once the affordance returns (and to drop
# this gate again).
pytestmark = pytest.mark.skipif(
    not os.environ.get("OMNIGENT_E2E_SWITCH_AGENT_UI"),
    reason="Switch-agent has no UI entry point (header button removed); "
    "set OMNIGENT_E2E_SWITCH_AGENT_UI=1 once the affordance returns.",
)


def _builtin_agent_id(base_url: str, name: str) -> str:
    """Resolve a built-in agent's id by name from ``GET /v1/agents``.

    :param base_url: Live server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param name: Built-in agent name, e.g. ``"files_probe_env"``.
    :returns: The agent id, e.g. ``"ag_abc123"``.
    """
    resp = httpx.get(f"{base_url}/v1/agents?limit=100", timeout=10.0)
    resp.raise_for_status()
    for agent in resp.json()["data"]:
        if agent["name"] == name:
            return str(agent["id"])
    pytest.fail(
        f"Built-in agent {name!r} not registered on {base_url}. The spawned "
        f"live_server seeds it via OMNIGENT_BUILTIN_AGENT_DIRS; an external "
        f"--ui-base-url server won't have it."
    )


@pytest.fixture
def os_env_switch_session(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, str, str]]:
    """Create a runner-bound session on the no-os_env probe built-in.

    Binds the session DIRECTLY to the built-in by id (the same JSON
    create the web new-chat flow uses) — safe to switch away from, since
    ``switch_conversation_agent`` only deletes session-scoped agents.
    Respawns the shared runner first if a prior test in the shard killed
    it; otherwise the runner-bind ``PATCH`` would 400.

    :param live_server: Spawned server fixture (seeds the probe built-ins).
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id, with_env_agent_id, no_env_agent_id)``.
    """
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    no_env_id = _builtin_agent_id(live_server, _FILES_PROBE_NO_ENV_AGENT_NAME)
    with_env_id = _builtin_agent_id(live_server, _FILES_PROBE_ENV_AGENT_NAME)

    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        json={"agent_id": no_env_id},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = str(create_resp.json()["id"])
    patch_resp = httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()

    try:
        yield (live_server, session_id, with_env_id, no_env_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        # Same reap pattern as the conftest runner teardowns: escalate to
        # kill only on timeout, and always wait so no zombie is left.
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


def _switch_agent(page: Page, target_agent_id: str) -> None:
    """Drive the header switch-agent dialog to the given built-in.

    :param page: Playwright page on an open ``/c/<id>`` chat surface.
    :param target_agent_id: Built-in agent id to switch the session to,
        e.g. ``"ag_abc123"``.
    """
    page.get_by_test_id("switch-agent-header").click()
    page.get_by_test_id("switch-agent-select").click()
    # Radix portals the option list to the body, so look it up on the
    # page, not inside the dialog locator.
    page.get_by_test_id(f"switch-agent-option-{target_agent_id}").click()
    page.get_by_test_id("switch-agent-submit").click()
    # The dialog closes only on a 2xx switch — a still-open dialog here
    # means the route rejected (e.g. 409 busy) and the rest of the test
    # would assert against an unswitched session.
    expect(page.get_by_test_id("switch-agent-dialog")).to_have_count(0)


def test_files_tab_follows_os_env_across_agent_switch(
    page: Page,
    os_env_switch_session: tuple[str, str, str, str],
) -> None:
    """Switching across an os_env boundary shows/hides the Files tab live.

    Covers both directions with no reload in between:

    1. Session starts on the no-os_env agent → the environment resource
       404s → the Files tab is hidden.
    2. Switch to the os_env agent → the post-switch runner reset closes
       the old env and publishes ``session.changed_files.invalidated`` →
       the store refetches ``workspace-environment`` → the tab appears.
       Before this fix the tab stayed hidden until an unrelated refetch
       (60 s staleTime + window refocus / reload).
    3. Switch back to the no-os_env built-in → same chain, env 404s
       again → the tab disappears.
    """
    base_url, session_id, with_env_id, no_env_id = os_env_switch_session

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Ask the agent anything…")).to_be_visible()

    # Scope to the desktop "Workspace" rail so the locator can't match
    # the hidden mobile drawer that mirrors the same tab markup.
    rail = page.get_by_role("complementary", name="Workspace")
    files_tab = rail.get_by_role("tab", name=re.compile("Files"))

    # No os_env → the runner 404s the environment resource → tab hidden.
    # (The tab may flash while the availability query is in flight —
    # to_have_count(0) retries until the 404 resolves and it unmounts.)
    expect(files_tab).to_have_count(0)

    # Switch onto the os_env agent. The tab appearing WITHOUT any
    # navigation proves the post-reset invalidation event reached the
    # store and refetched availability — there is no other refetch
    # trigger inside this window (staleTime is 60 s and the page never
    # reloads or refocuses). 30 s budget: the reset is a background
    # task on the server plus a debounced (750 ms) client flush.
    _switch_agent(page, with_env_id)
    expect(files_tab).to_be_visible(timeout=30_000)

    # And back across the boundary: the new agent has no os_env, so the
    # refetched availability flips false and the tab unmounts again.
    _switch_agent(page, no_env_id)
    expect(files_tab).to_have_count(0, timeout=30_000)
