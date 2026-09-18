"""A damaged server disk-cache entry must not break an agent whose stored bundle is intact.

Journey: a user chats with a custom agent (the server extracts its bundle to
the disk cache), the extracted ``config.yaml`` is later lost or corrupted on
disk, and the server restarts with a fresh ``AgentCache``. The user returns to
the same session and sends another message. The intact bundle still lives in
the artifact store, so the turn must succeed and the agent must stay usable.
A regression treats the damaged directory as a cache hit and fails every spec
load, which surfaces as one of two user-visible failures: the session no
longer hydrates ("Conversation not found"), or the input-policy phase denies
the message with "Denied by policy (policy evaluation error)".
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path
from typing import cast

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _create_bundled_session,
    _ensure_runner_online,
    _server_state,
    set_fallback_mock_llm,
)

_AGENT_YAML = """\
spec_version: 1
name: cache_damage_probe
prompt: |
  You are a friendly assistant. Reply with one short sentence.

executor:
  model: {model}
  config:
    harness: openai-agents
"""

_REPLY = "Cache probe reply."
_ASSISTANT_BUBBLE = '[data-testid="message-bubble"][data-role="assistant"]'
_DENY_SENTINEL = "policy evaluation error"


def _server_cache_config(agent_id: str) -> Path:
    """Path of the server-side extracted ``config.yaml`` for *agent_id*."""
    database_uri = str(_server_state["database_uri"])
    server_tmp = Path(database_uri.removeprefix("sqlite:///")).parent
    return server_tmp / "artifacts" / ".cache" / agent_id / "config.yaml"


@pytest.mark.timeout(300)
@pytest.mark.parametrize("damage", ["removed", "malformed"])
def test_damaged_cache_entry_rebuilds_from_intact_bundle(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
    damage: str,
) -> None:
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    restart = _server_state.get("restart_server")
    if not callable(restart):
        pytest.skip("requires the locally spawned restartable server")
    restart_server = cast("Callable[[], None]", restart)

    runner_id = str(_server_state["runner_id"])
    model = f"cache-damage-{uuid.uuid4().hex[:8]}"
    set_fallback_mock_llm(mock_llm_server_url, model, _REPLY)

    session_id = _create_bundled_session(live_server, runner_id, _AGENT_YAML.format(model=model))
    try:
        agent_resp = httpx.get(f"{live_server}/v1/sessions/{session_id}/agent", timeout=10.0)
        agent_resp.raise_for_status()
        agent_id = str(agent_resp.json()["id"])

        # First turn populates the server's disk cache with the extracted bundle.
        page.goto(f"{live_server}/c/{session_id}")
        composer = page.get_by_placeholder("Send a message…")
        expect(composer).to_be_visible(timeout=30_000)
        composer.fill("Hello!")
        page.get_by_role("button", name="Send", exact=True).click()
        first_reply = page.locator(_ASSISTANT_BUBBLE).first
        expect(first_reply).to_be_visible(timeout=60_000)
        expect(first_reply).to_contain_text(_REPLY, timeout=60_000)

        # Damage the on-disk cache entry; the artifact-store bundle stays intact.
        config_path = _server_cache_config(agent_id)
        assert config_path.is_file(), f"expected the server disk cache entry at {config_path}"
        if damage == "removed":
            config_path.unlink()
        else:
            config_path.write_text("{unclosed: [malformed yaml")

        # Fresh AgentCache on restart: the next spec load hits the damaged disk entry.
        restart_server()

        # The session must still hydrate from the intact stored bundle.
        page.reload()
        load_error = page.get_by_role("heading", name="Conversation not found")
        composer = page.get_by_placeholder("Send a message…")
        expect(composer.or_(load_error).first).to_be_visible(timeout=30_000)
        assert load_error.count() == 0, (
            "session failed to hydrate after the damaged cache entry was left "
            "in place; the intact stored bundle should have been re-extracted"
        )

        composer.fill("Hello again after restart!")
        page.get_by_role("button", name="Send", exact=True).click()

        # The turn must produce the normal reply, not a policy-evaluation-error
        # denial from the input-policy phase failing to load the damaged spec.
        second_reply = page.locator(_ASSISTANT_BUBBLE).nth(1)
        deny = page.get_by_text(_DENY_SENTINEL)
        expect(second_reply.or_(deny).first).to_be_visible(timeout=60_000)
        assert deny.count() == 0, (
            "the message was denied with a policy-evaluation error because the "
            "damaged cache entry blocked loading the intact stored bundle"
        )
        expect(second_reply).to_contain_text(_REPLY, timeout=30_000)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except Exception:
                respawned_runner.kill()
