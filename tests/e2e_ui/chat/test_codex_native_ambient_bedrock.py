"""E2E: ``omnigent codex`` honors an ambient Codex-native built-in Bedrock config.

With ``~/.codex/config.toml`` selecting ``model_provider = "amazon-bedrock"``, no
Omnigent provider and no Codex login, plain ``codex`` runs (Codex resolves the
AWS credential chain itself). ``omnigent codex`` must route the same way instead
of failing the first turn with "Codex is not signed in and no Omnigent provider
routes the codex harness". The rig mirrors
``test_codex_native_headless_login_timeout.py``: a dedicated server + runner
under a redirected ``HOME`` so nothing leaks into other tests.
The real Codex CLI runs with empty AWS credential files and metadata discovery
disabled; this verifies native launch without authenticating or running inference.
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

_HEALTH_TIMEOUT_S = 60.0
# Allow time for the turn to either fail terminally or reach Codex thread start.
_TURN_OUTCOME_TIMEOUT_S = 150.0
_ERROR_PILL = '[data-testid="error-pill"]'
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_USER = '[data-testid="message-bubble"][data-role="user"]'

# Ambient config selecting the self-sufficient built-in Bedrock provider.
_AMBIENT_BEDROCK_CONFIG = """\
model = "openai.gpt-5.6-terra"
model_provider = "amazon-bedrock"

[model_providers.amazon-bedrock.aws]
region = "us-east-1"
"""

# Turn executor failures carry this prefix; pre-turn rig notices do not.
_TURN_EXECUTOR_ERROR = "inner executor error"
# Both claim nothing routes the codex harness, which the ambient config refutes.
_NO_ROUTE_MARKER = "no Omnigent provider routes the codex harness"
_NO_PROVIDER_SUMMARY_MARKER = "no provider configured for the codex harness"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# CI forces an egress proxy; loopback requests to the spawned server must bypass it.
_client = httpx.Client(trust_env=False)


def _clean_env() -> dict[str, str]:
    """Ambient env with loopback proxy-excluded and launch-routing inputs stripped.

    Leaked ``OMNIGENT_RUNNER_*`` / ``OMNIGENT_HOST_*`` vars would send the child
    runner down the zygote-fork path; vendor keys and ``CODEX_HOME`` would mask
    the no-provider state under test.
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


def _write_isolated_codex_shim(work: Path, codex_path: str) -> Path:
    """Disable AWS credential discovery after Omnigent filters subprocess env."""
    aws_config = work / "empty-aws-config"
    aws_config.write_text("", encoding="utf-8")
    shim = work / "isolated-codex"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import sys\n"
        "for key in tuple(os.environ):\n"
        "    if key.startswith('AWS_'):\n"
        "        del os.environ[key]\n"
        "os.environ.update({\n"
        f"    'AWS_CONFIG_FILE': {str(aws_config)!r},\n"
        f"    'AWS_SHARED_CREDENTIALS_FILE': {str(aws_config)!r},\n"
        "    'AWS_EC2_METADATA_DISABLED': 'true',\n"
        "})\n"
        f"os.execv({codex_path!r}, [{codex_path!r}, *sys.argv[1:]])\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return shim


def _codex_thread_started(bridge_root: Path, session_id: str) -> bool:
    """Whether the runner's bridge ``state.json`` records a started Codex thread."""
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
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[str, str, Path]]:
    """A codex-native wrapper session on a rig carrying the ambient Bedrock config.

    The dedicated server + runner see a ``HOME`` with the Bedrock ``config.toml``
    and no ``auth.json``, plus an empty ``OMNIGENT_CONFIG_HOME``.

    :returns: ``(base_url, session_id, home_dir)``; *home_dir* holds the
        ``.omnigent/codex-native`` bridge state that records thread start.
    """
    # Shared conftest helpers use env-trusting httpx calls; keep loopback off any proxy.
    for var in ("NO_PROXY", "no_proxy"):
        loopback = ",".join(filter(None, [os.environ.get(var, ""), "127.0.0.1,localhost"]))
        monkeypatch.setenv(var, loopback)
    work = tmp_path_factory.mktemp("codex_ambient_bedrock")
    config_home = work / "config-home"
    home_dir = work / "home"
    codex_dir = home_dir / ".codex"
    state_dir = work / "codex-native-state"
    artifacts = work / "artifacts"
    for path in (config_home, codex_dir, state_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)

    (codex_dir / "config.toml").write_text(_AMBIENT_BEDROCK_CONFIG, encoding="utf-8")
    codex_path = shutil.which("codex")
    assert codex_path is not None
    codex_shim = _write_isolated_codex_shim(work, codex_path)

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
        "OMNIGENT_CODEX_PATH": str(codex_shim),
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
            with contextlib.suppress(httpx.HTTPError):
                if _client.get(f"{base_url}/health", timeout=2).status_code == 200:
                    status = _client.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json().get("online"):
                        online = True
                        break
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

    While the bug is live the launch router ignores the ambient config, marks the
    launch ``login_required``, and the turn fails within seconds with an error
    pill; after the fix the launch routes through Bedrock and a thread starts.
    """
    base_url, session_id, home_dir = ambient_bedrock_codex_session
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)

    # Pre-turn rig notices may already show error pills; count them to isolate the turn.
    pre_error_pills = page.locator(_ERROR_PILL).count()

    _send(page, "Reply with just the word OK.")
    sent_at = time.monotonic()
    expect(page.locator(_USER).first).to_be_visible(timeout=30_000)

    # A terminal turn outcome settles the wait; so does the thread starting, since a
    # rig without live AWS credentials may keep the routed Bedrock turn in flight.
    bridge_root = home_dir / ".omnigent" / "codex-native"
    deadline = time.monotonic() + _TURN_OUTCOME_TIMEOUT_S
    settled = False
    thread_started = False
    data: list[dict[str, object]] = []
    error_messages: list[str] = []
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx.HTTPError):
            items = _client.get(
                f"{base_url}/v1/sessions/{session_id}/items?limit=50", timeout=10.0
            )
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

    # Best-effort render wait so a recorded run films the user-visible outcome.
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

    # The ambient config selects a self-sufficient built-in provider, so no turn
    # error may claim that nothing routes the codex harness.
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

    # Settling on a different startup error is still a broken journey.
    thread_started = thread_started or _codex_thread_started(bridge_root, session_id)
    assert thread_started or any(item.get("role") == "assistant" for item in data), (
        "the codex-native launch never started a thread despite the ambient "
        f"Bedrock config (after {elapsed:.0f}s); transcript errors: {error_messages}"
    )
