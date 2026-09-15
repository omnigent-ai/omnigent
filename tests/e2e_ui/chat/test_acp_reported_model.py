"""E2E: ACP sessions must display the active harness model, not the spec pin.

An agent whose spec pins an executor model (the builtin ``hello_world``
fixture agent pins ``gpt-4o-mini``) is launched with a self-authenticated
ACP harness override (Grok Build). The ACP process owns its own model and
reports the one it actually runs (``grok-4.6``); the spec's pinned model
never executes.

Expected behavior, split into the two facets asserted here:

1. **Pre-report** (``test_fresh_acp_session_shows_harness_identity_not_spec_model``):
   before the ACP process has reported anything, the composer identifies the
   session by the selected harness identity (Grok Build) — it must NOT claim
   the pinned spec model the ACP process will never run.
2. **Post-report** (``test_acp_reported_model_persists_and_displays``): after
   the first turn, the ACP-reported active model is displayed live and
   persisted, so a reload still shows it instead of the spec pin.

The journey is driven for real: the builtin ``grok`` ACP CLI harness row is
pointed at a hermetic fake Grok Build agent (an executable Python script
speaking the Agent Client Protocol on stdio, mirroring
``tests/e2e_ui/files/test_files_tab_survives_acp_reply.py``) via the
``harness.grok.command`` config override, which the runner re-reads from
``config.yaml`` at dispatch time. The session is created with
``harness_override: "grok"`` — the same JSON create the web new-chat harness
picker issues — and the SPA is driven through the session page.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online, _server_state

# The builtin fixture agent registered by the spawned live_server. Its spec
# pins ``executor.model: gpt-4o-mini`` — an agent with a pinned executor
# model the ACP process never runs.
_PINNED_AGENT_NAME = "hello_world"
_SPEC_PINNED_MODEL = "gpt-4o-mini"

# What the fake Grok Build ACP agent reports as its active model, and the
# harness identity label the composer should show before any report.
_ACP_ACTIVE_MODEL = "grok-4.6"
_HARNESS_IDENTITY = "Grok Build"

_ACP_REPLY_TEXT = "Grok Build reply: hello from fake-grok"

# A minimal Grok Build stand-in speaking the Agent Client Protocol over
# stdio. It reports its active model the way real ACP agents do — a ``model``
# config option with a ``currentValue`` in the ``session/new`` result — then
# streams one deterministic reply chunk and completes the turn with token
# usage (no ``model`` key: the executor stamps the reported active model).
# Stdlib only; launched as ``<this script> agent stdio`` (args ignored).
_FAKE_GROK_AGENT = r"""#!/usr/bin/env python3
import sys, json

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"promptCapabilities": {"image": False}},
        }})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "sessionId": "fake-grok-session-1",
            "configOptions": [
                {"id": "model", "name": "Model", "currentValue": "grok-4.6"},
            ],
        }})
    elif method == "session/prompt":
        sid = msg["params"]["sessionId"]
        send({"jsonrpc": "2.0", "method": "session/update",
              "params": {"sessionId": sid, "update": {
                  "sessionUpdate": "agent_message_chunk",
                  "content": {"type": "text",
                              "text": "Grok Build reply: hello from fake-grok"}}}})
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 3, "outputTokens": 5, "totalTokens": 8},
        }})
"""


def _config_yaml_path() -> Path:
    """The global omnigent config file the server/runner processes read.

    Mirrors ``omnigent.onboarding.provider_config._config_path``: the spawned
    server and runner inherit this process's environment, so computing the
    path from the same ``$OMNIGENT_CONFIG_HOME`` fallback chain targets the
    file the runner's dispatch-time ``load_config()`` re-reads.
    """
    config_home = os.environ.get("OMNIGENT_CONFIG_HOME")
    config_dir = Path(config_home) if config_home else Path.home() / ".omnigent"
    return config_dir / "config.yaml"


def _builtin_agent_id(base_url: str, name: str) -> str:
    """Resolve a builtin agent's id by name from ``GET /v1/agents``."""
    resp = httpx.get(f"{base_url}/v1/agents?limit=100", timeout=10.0)
    resp.raise_for_status()
    for agent in resp.json()["data"]:
        if agent["name"] == name:
            return str(agent["id"])
    pytest.fail(
        f"Builtin agent {name!r} not registered on {base_url}. The spawned "
        f"live_server seeds it via OMNIGENT_BUILTIN_AGENT_DIRS; an external "
        f"--ui-base-url server won't have it."
    )


@pytest.fixture
def grok_override_session(
    live_server: str,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A session on the pinned-model agent with the Grok Build ACP override.

    Points the builtin ``grok`` harness row at the hermetic fake agent via
    the ``harness.grok.command`` config override (backed up and restored),
    creates the session with ``harness_override: "grok"`` — the same JSON
    create the web new-chat harness picker issues — and binds it to the
    spawned runner.

    :returns: ``(base_url, session_id)``.
    """
    # The fake Grok Build binary: an executable ACP-speaking script, spawned
    # by the runner as ``<script> agent stdio`` (the row's argv).
    fake_grok = tmp_path / "fake-grok"
    fake_grok.write_text(_FAKE_GROK_AGENT)
    fake_grok.chmod(0o755)

    # Point the grok row at it through config — re-read by the runner at
    # dispatch time, so no runner restart is needed. Back up whatever the
    # machine already had and restore it on teardown.
    config_path = _config_yaml_path()
    original = config_path.read_bytes() if config_path.exists() else None
    config_path.parent.mkdir(parents=True, exist_ok=True)
    cfg: dict[str, object] = {}
    if original is not None:
        loaded = yaml.safe_load(original.decode()) or {}
        if isinstance(loaded, dict):
            cfg = loaded
    harness_block = cfg.get("harness")
    if isinstance(harness_block, str):
        harness_block = {"default": harness_block}
    if not isinstance(harness_block, dict):
        harness_block = {}
    harness_block["grok"] = {"command": str(fake_grok)}
    cfg["harness"] = harness_block
    config_path.write_text(yaml.safe_dump(cfg))

    respawned_runner = None
    session_id: str | None = None
    try:
        # Earlier tests may deliberately stop the session-scoped runner.
        respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
        runner_id = str(_server_state["runner_id"])

        agent_id = _builtin_agent_id(live_server, _PINNED_AGENT_NAME)
        create_resp = httpx.post(
            f"{live_server}/v1/sessions",
            json={"agent_id": agent_id, "harness_override": "grok"},
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

        yield (live_server, session_id)
    finally:
        try:
            if session_id is not None:
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        finally:
            try:
                if original is None:
                    config_path.unlink(missing_ok=True)
                else:
                    config_path.write_bytes(original)
            finally:
                if respawned_runner is not None:
                    respawned_runner.terminate()
                    try:
                        respawned_runner.wait(timeout=5)
                    except Exception:  # best-effort teardown
                        respawned_runner.kill()
                        respawned_runner.wait(timeout=5)


def test_fresh_acp_session_shows_harness_identity_not_spec_model(
    page: Page,
    grok_override_session: tuple[str, str],
) -> None:
    """Pre-report: the composer shows the harness identity, not the spec pin.

    The reported journey's first observable failure: open a fresh session
    created with the Grok Build ACP harness override, before any turn. The
    ACP process owns its model and has not reported one yet, so the composer
    must identify the session by the selected harness identity — it must NOT
    display the agent spec's pinned executor model, which this session never
    runs. Under the bug the label reads the spec pin (``gpt-4o-mini`` here)
    and this test fails on the first expect.
    """
    base_url, session_id = grok_override_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    # The composer's session-config trigger (model/harness label). Once the
    # snapshot hydrates, correct behavior renders the harness identity; the
    # positive expectation doubles as the hydration wait.
    config_value = page.get_by_test_id("composer-agent-config-value")
    expect(config_value).to_be_visible(timeout=30_000)
    expect(config_value).to_contain_text(_HARNESS_IDENTITY, timeout=30_000)
    expect(config_value).not_to_contain_text(_SPEC_PINNED_MODEL)


def test_acp_reported_model_persists_and_displays(
    page: Page,
    grok_override_session: tuple[str, str],
) -> None:
    """Post-report: the ACP-reported active model displays and persists.

    Drive one real turn through the fake Grok Build ACP agent, which reports
    ``grok-4.6`` as its active model. The composer must flip to the reported
    model once the turn completes (the ``session.model`` push), and a reload
    must still show it (the persisted ``reported_model`` on the snapshot)
    rather than the spec's pinned model.
    """
    base_url, session_id = grok_override_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    # The reported user action: a message to the ACP-override session.
    composer.fill("Say hello")
    composer.press("Enter")

    # The fake agent streams a deterministic reply; once it renders, the turn
    # has completed and the agent's model report has reached the server.
    expect(page.get_by_text(_ACP_REPLY_TEXT)).to_be_visible(timeout=90_000)

    # Live display: the composer model label must show the ACP-reported
    # active model, not the spec pin.
    model_value = page.get_by_test_id("composer-agent-model-value")
    expect(model_value).to_contain_text(_ACP_ACTIVE_MODEL, timeout=30_000)
    expect(model_value).not_to_contain_text(_SPEC_PINNED_MODEL)

    # Persistence: a reload re-renders from the session snapshot, which must
    # carry the reported model instead of falling back to the spec pin.
    page.reload()
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
    model_value = page.get_by_test_id("composer-agent-model-value")
    expect(model_value).to_contain_text(_ACP_ACTIVE_MODEL, timeout=30_000)
    expect(model_value).not_to_contain_text(_SPEC_PINNED_MODEL)
