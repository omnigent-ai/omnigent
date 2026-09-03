"""E2E: an ACP session's composer must display the harness-reported active model.

An agent pinned to an executor model (``claude-opus-5``) is launched through
the generic ACP harness with a self-authenticated agent — a hermetic stand-in
for Grok Build that owns its model selection and reports the model it actually
runs (``grok-4.6``) via the standard ACP ``config_option_update`` (the
``model`` option's ``currentValue``, the same payload
``AcpExecutor._note_config_options`` records). The session composer must not
keep claiming the pinned spec model:

- before the agent's first report, only the selected harness identity should
  show — not the spec model the ACP process never runs;
- after the first turn, the ACP-reported active model must persist and
  display.

Both fail on the current build: the ACP executor records the reported model
(``_active_model``) but nothing forwards it to the server (no
``external_model_change`` / ``reported_model`` write), so the session
snapshot's ``llm_model`` falls back to the agent-spec model forever and the
composer renders ``claude-opus-5`` while the agent runs ``grok-4.6``.
"""

from __future__ import annotations

import gzip
import io
import json
import shlex
import subprocess
import sys
import tarfile
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online, _server_state

_AGENT_NAME = "grok_acp_probe"
#: The model the agent spec pins — what the composer wrongly keeps showing.
_SPEC_MODEL = "claude-opus-5"
#: The model the ACP agent actually selects and reports.
_REPORTED_MODEL = "grok-4.6"

_COMPOSER = 'textarea[aria-label="Message the agent"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_MODEL_LABEL = "composer-model-effort-label"

# A minimal self-authenticated ACP agent (JSON-RPC 2.0 over newline-delimited
# stdio), the same shape as the hermetic agent in tests/inner/test_acp_executor
# .py. It answers the handshake, and on session/prompt first reports the model
# it is actually running via the standard ``config_option_update`` (Grok Build
# reports ``grok-4.6`` this way regardless of any pinned spec model), then
# streams a reply and completes the turn.
_FAKE_GROK_ACP = r"""
import sys, json

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def update(sid, upd):
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": sid, "update": upd}})

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
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "grok-session-1"}})
    elif method == "session/prompt":
        sid = msg["params"]["sessionId"]
        update(sid, {"sessionUpdate": "config_option_update", "configOptions": [{
            "id": "model",
            "currentValue": "grok-4.6",
            "options": [{"value": "grok-4.6"}, {"value": "grok-4.6-mini"}],
        }]})
        update(sid, {"sessionUpdate": "agent_message_chunk",
                     "content": {"type": "text", "text": "Hello from Grok."}})
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 12, "outputTokens": 4, "totalTokens": 16},
        }})
"""

# omnigent shorthand YAML (non-config.yaml arcname routes it through the
# compat adapter, which threads ``executor.acp_agent`` into
# ``executor.config["acp_agent"]`` — the same embedded one-shot agent shape
# ``omnigent run --harness acp:<slug> --server <remote>`` produces). The
# harness owns its auth and model; ``omnigent_mcp: false`` keeps the hermetic
# agent free of the MCP relay.
_AGENT_YAML_TEMPLATE = f"""\
name: {_AGENT_NAME}
prompt: You are a probe agent.

executor:
  model: {_SPEC_MODEL}
  harness: acp
  acp_agent:
    name: Grok Build
    command: {{command}}
    omnigent_mcp: false
"""


@pytest.fixture
def acp_probe_session(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A runner-bound session for an ACP agent with a pinned spec model.

    Writes the hermetic fake-Grok ACP agent to disk, registers an agent whose
    spec pins ``claude-opus-5`` but whose harness is the generic ACP wrap
    driving that fake agent, and binds a session to the shared runner.

    :returns: ``(base_url, session_id)``.
    """
    script_dir = tmp_path_factory.mktemp("fake_grok_acp")
    script_path = script_dir / "fake_grok_acp.py"
    script_path.write_text(_FAKE_GROK_ACP)
    yaml_text = _AGENT_YAML_TEMPLATE.format(command=shlex.join([sys.executable, str(script_path)]))

    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        data = yaml_text.encode()
        info = tarfile.TarInfo(name=f"{_AGENT_NAME}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    patch_resp = httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()

    try:
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


def test_pre_report_composer_shows_harness_identity_not_spec_model(
    page: Page,
    acp_probe_session: tuple[str, str],
) -> None:
    """Before the ACP agent reports, the composer must not claim the spec model.

    A self-authenticated ACP harness ignores the spec's pinned model — Grok
    runs whatever its own login/config selects — so displaying
    ``claude-opus-5`` before any report is a claim the client cannot know.
    Only the selected harness identity should show until the first report.
    """
    base_url, session_id = acp_probe_session

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.locator(_COMPOSER)).to_be_visible(timeout=30_000)
    # Let the session snapshot land in the store so the label reflects it.
    page.wait_for_timeout(3_000)

    expect(page.get_by_test_id(_MODEL_LABEL)).not_to_contain_text(_SPEC_MODEL, timeout=5_000)


def test_composer_shows_acp_reported_model_after_first_turn(
    page: Page,
    acp_probe_session: tuple[str, str],
) -> None:
    """After the first turn, the ACP-reported active model must display.

    The fake Grok agent reports ``grok-4.6`` via ``config_option_update``
    during the turn. That report is the display authority (the same
    ``reported_model`` semantics every native harness follows), and it must
    persist — the label shows it even after a reload, not the pinned
    ``claude-opus-5`` the ACP process never ran.
    """
    base_url, session_id = acp_probe_session

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.locator(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)

    composer.fill("Say hello.")
    page.get_by_role("button", name="Send", exact=True).click()

    # The turn completed: the ACP agent streamed its reply, and with it the
    # config_option_update reporting grok-4.6 as the live model.
    expect(page.locator(_ASSISTANT, has_text="Hello from Grok").first).to_be_visible(
        timeout=60_000
    )

    # Reload so the label renders from the persisted session snapshot — the
    # report must survive, not just flash from a transient event.
    page.reload()
    expect(page.locator(_COMPOSER)).to_be_visible(timeout=30_000)

    expect(page.get_by_test_id(_MODEL_LABEL)).to_contain_text(_REPORTED_MODEL, timeout=15_000)
