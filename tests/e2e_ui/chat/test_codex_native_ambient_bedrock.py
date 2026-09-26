"""E2E: ``omnigent codex`` must honor an ambient Codex-native Bedrock config.

Reported journey: Codex CLI itself is configured for its **built-in** Amazon
Bedrock provider via ``~/.codex/config.toml``::

    model = "openai.gpt-5.6-terra"
    model_provider = "amazon-bedrock"

    [model_providers.amazon-bedrock.aws]
    region = "us-east-1"

No Omnigent provider is configured and Codex holds no ChatGPT login. Plain
``codex`` runs fine against this config (Codex resolves the AWS credential
chain itself), so ``omnigent codex`` must route through it too: no turn error
may claim that no provider routes the codex harness. A launch router that
ignores the ambient built-in-provider config instead marks the launch
``login_required`` and the first chat message dies with::

    inner executor error: Codex native thread never started: Codex is not
    signed in and no Omnigent provider routes the codex harness, so the
    Codex TUI is parked on its sign-in screen and cannot run this turn. ...

The rig mirrors ``test_codex_native_headless_login_timeout.py`` (own server +
runner so the redirected ``HOME`` / ``OMNIGENT_CONFIG_HOME`` cannot leak into
other tests), with the reporter's Bedrock ``config.toml`` written into the
rig's ``~/.codex`` and no ``auth.json`` (Codex not signed in).
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _create_native_codex_session
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view, _send

_REPO_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None or shutil.which("tmux") is None,
    reason="codex-native e2e needs the `codex` CLI and `tmux` on PATH.",
)

# Boot budget for the spawned server + runner pair.
_HEALTH_TIMEOUT_S = 60.0
# The buggy path fails fast (pre-recorded bridge startup error); a fixed
# routing starts the Codex thread within the TUI boot budget, so leave room
# for either outcome plus rig jitter.
_TURN_OUTCOME_TIMEOUT_S = 150.0
_ERROR_PILL = '[data-testid="error-pill"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_USER = '[data-testid="message-bubble"][data-role="user"]'

# The reporter's exact ambient Codex config: the built-in Bedrock provider,
# selected as the effective model_provider, with its supported aws overrides.
_AMBIENT_BEDROCK_CONFIG = """\
model = "openai.gpt-5.6-terra"
model_provider = "amazon-bedrock"

[model_providers.amazon-bedrock.aws]
region = "us-east-1"
"""

# A chat turn's executor failure is surfaced into the transcript with this
# prefix; pre-turn rig notices (e.g. the policy-hook error item) lack it.
_TURN_EXECUTOR_ERROR = "inner executor error"

# Markers of the reported failure in the turn's executor error text: the
# login fail-fast body and the launch-routing summary it embeds. Both claim
# nothing routes the codex harness, which is false with the ambient config.
_NO_ROUTE_MARKER = "no Omnigent provider routes the codex harness"
_NO_PROVIDER_SUMMARY_MARKER = "no provider configured for the codex harness"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars
# that must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False)

# Shared fixtures/helpers (e.g. the conftest session factory) use ambient
# ``httpx`` calls that DO trust env, so also exclude loopback from any forced
# proxy at import time.
for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = ",".join(filter(None, [os.environ.get(_var, ""), "127.0.0.1,localhost"]))


def _clean_env() -> dict[str, str]:
    """Ambient env with loopback proxy-excluded and routing inputs stripped.

    Stripping ``OMNIGENT_RUNNER_*`` / ``OMNIGENT_HOST_*`` matters when the
    test itself runs inside a server-spawned runner: leaked zygote/tunnel
    vars make the spawned child runner take the zygote-fork path and hang.
    Vendor API keys and ``CODEX_HOME`` are stripped because they are launch-
    routing inputs: a leaked ``OPENAI_API_KEY`` would give the codex harness
    an ambient provider and mask the no-provider state under test, and a
    leaked ``CODEX_HOME`` would bypass the rig's ``~/.codex``.
    """
    env = os.environ.copy()
    for var in ("NO_PROXY", "no_proxy"):
        existing = env.get(var, "")
        env[var] = ",".join(filter(None, [existing, "127.0.0.1,localhost"]))
    for key in list(env):
        if key.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_HOST_")):
            del env[key]
    for key in (
        "RUNNER_SERVER_URL",
        "OMNIGENT_PROCESS_LOG_FILE",
        "OMNIGENT_DATA_DIR",
        "CODEX_HOME",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
        "GEMINI_API_KEY",
        "DATABRICKS_CONFIG_PROFILE",
    ):
        env.pop(key, None)
    return env


def _codex_thread_started(bridge_root: Path, session_id: str) -> bool:
    """Whether the runner recorded a started Codex thread for *session_id*.

    The runner writes the bridge ``state.json`` (with a ``thread_id``) only
    after the Codex TUI actually started a thread — the exact thing a launch
    parked on the sign-in screen never does.
    """
    for state_file in bridge_root.glob("*/state.json"):
        try:
            payload = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if payload.get("session_id") == session_id and payload.get("thread_id"):
            return True
    return False


@pytest.fixture
def ambient_bedrock_codex_session(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """A codex-native wrapper session on a rig with an ambient Bedrock config.

    Spawns a dedicated server + runner whose ``HOME`` carries the reporter's
    ``~/.codex/config.toml`` (built-in ``amazon-bedrock`` selected as the
    effective provider) and **no** ``auth.json`` (Codex not signed in), with
    an empty ``OMNIGENT_CONFIG_HOME`` (no Omnigent provider configured), then
    creates and binds the same codex-native wrapper session ``omnigent
    codex`` ships. This is the reported launch-routing state.

    :returns: ``(base_url, session_id, home_dir)`` — *home_dir* is the
        rig's redirected ``HOME``, whose ``.omnigent/codex-native`` bridge
        state records whether the Codex thread started.
    """
    work = tmp_path_factory.mktemp("codex_ambient_bedrock")
    config_home = work / "config-home"
    home_dir = work / "home"
    codex_dir = home_dir / ".codex"
    state_dir = work / "codex-native-state"
    artifacts = work / "artifacts"
    for path in (config_home, codex_dir, state_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)

    (codex_dir / "config.toml").write_text(_AMBIENT_BEDROCK_CONFIG, encoding="utf-8")

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)

    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    shared_env = {
        **_clean_env(),
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
        "HOME": str(home_dir),
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }

    server_log = work / "server.log"
    runner_log = work / "runner.log"
    server_handle = server_log.open("w")
    runner_handle = runner_log.open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    try:
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{work}/test.db",
                "--artifact-location",
                str(artifacts),
            ],
            env=server_env,
            stdout=server_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )

        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            if server_proc.poll() is not None or runner_proc.poll() is not None:
                break
            try:
                if _client.get(f"{base_url}/health", timeout=2).status_code == 200:
                    status = _client.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json().get("online"):
                        online = True
                        break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        if not online:
            raise RuntimeError(
                "ambient-bedrock codex rig did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )

        session_id = _create_native_codex_session(base_url, runner_id)
        yield (base_url, session_id, home_dir)
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                _client.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        for proc in (runner_proc, server_proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in (runner_proc, server_proc):
            if proc is not None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        server_handle.close()
        runner_handle.close()


@pytest.mark.timeout(400)
def test_ambient_bedrock_codex_config_routes_the_native_launch(
    page: Page,
    ambient_bedrock_codex_session: tuple[str, str, Path],
) -> None:
    """The first chat turn must not die on the "nothing routes codex" fail-fast.

    Journey (the reported one): with Codex's own ``~/.codex/config.toml``
    selecting the built-in ``amazon-bedrock`` provider and no Omnigent
    provider configured, open the codex-native session ``omnigent codex``
    creates and send the first message. While the bug is live the launch
    router ignores the ambient config, marks the launch ``login_required``,
    and the turn fails immediately with "Codex is not signed in and no
    Omnigent provider routes the codex harness ... Launch routing: Codex CLI
    login (no provider configured for the codex harness, no Databricks
    profile)" — rendered by the SPA as an error pill. After a fix the launch
    routes through the ambient Bedrock config, so no turn error may claim
    that nothing routes the codex harness.
    """
    base_url, session_id, home_dir = ambient_bedrock_codex_session
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)

    # The rig can surface pre-turn error items (e.g. the policy-hook notice
    # for the bridged config), so count pills before the send to tell the
    # turn's own outcome apart from pre-existing noise.
    pre_error_pills = page.locator(_ERROR_PILL).count()

    _send(page, "Reply with just the word OK.")
    sent_at = time.monotonic()
    expect(page.locator(_USER).first).to_be_visible(timeout=30_000)

    # Wait for evidence of the launch routing. A terminal turn outcome (an
    # assistant reply, or an executor turn error — the buggy fail-fast lands
    # here within seconds) settles it; so does the Codex thread starting,
    # because a machine without live AWS credentials may keep the routed
    # Bedrock turn in flight longer than any CI budget. Pre-existing rig
    # notices are not turn outcomes and must not satisfy this wait.
    bridge_root = home_dir / ".omnigent" / "codex-native"
    deadline = time.monotonic() + _TURN_OUTCOME_TIMEOUT_S
    settled = False
    thread_started = False
    error_messages: list[str] = []
    while time.monotonic() < deadline:
        items = _client.get(f"{base_url}/v1/sessions/{session_id}/items?limit=50", timeout=10.0)
        items.raise_for_status()
        data = items.json()["data"]
        error_messages = [
            str(item.get("message", "")) for item in data if item.get("type") == "error"
        ]
        if any(item.get("role") == "assistant" for item in data) or any(
            _TURN_EXECUTOR_ERROR in message for message in error_messages
        ):
            settled = True
            break
        if _codex_thread_started(bridge_root, session_id):
            thread_started = True
            break
        time.sleep(1.0)
    elapsed = time.monotonic() - sent_at

    # Give the SPA a moment to render the outcome (error pill / assistant
    # bubble) so a recorded run films the user-visible failure. Best-effort:
    # the durable assertions below run against the canonical transcript.
    render_deadline = time.monotonic() + 30.0
    while time.monotonic() < render_deadline:
        if (
            page.locator(_ASSISTANT).count() > 0
            or page.locator(_ERROR_PILL).count() > pre_error_pills
        ):
            break
        time.sleep(0.5)

    assert settled or thread_started, (
        "the first codex-native turn neither reached a terminal outcome "
        "(assistant reply or executor error) nor started a Codex thread "
        f"within {_TURN_OUTCOME_TIMEOUT_S:.0f}s; "
        f"transcript errors so far: {error_messages}"
    )

    # THE BUG: no turn error may claim that no provider routes the codex
    # harness — the ambient config.toml selects Codex's own self-sufficient
    # built-in Bedrock provider, exactly like the plain `codex` CLI it drives.
    unrouted = [
        message
        for message in error_messages
        if _NO_ROUTE_MARKER in message or _NO_PROVIDER_SUMMARY_MARKER in message
    ]
    assert not unrouted, (
        "the codex-native launch router ignored the ambient Codex-native "
        f"Bedrock config and fail-fasted the turn (after {elapsed:.0f}s) as "
        f"if nothing routes the codex harness: {unrouted[0][:500]}"
    )

    # The routed launch must actually get past any sign-in screen: settling
    # on a different startup error is still a broken journey.
    thread_started = thread_started or _codex_thread_started(bridge_root, session_id)
    assert thread_started or any(item.get("role") == "assistant" for item in data), (
        "the codex-native launch never started a thread despite the ambient "
        f"Bedrock config (after {elapsed:.0f}s); transcript errors: {error_messages}"
    )
