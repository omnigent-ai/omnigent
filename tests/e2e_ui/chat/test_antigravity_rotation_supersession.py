r"""UI journey: an agy TUI ``/clear`` must notify the superseded web conversation.

Antigravity-native ``/clear`` mints a fresh cascade on the live agy process;
the RPC reader detects the newer-active sibling and rotates the Omnigent
binding onto a replacement conversation, transferring the terminal. A user
watching the OLD conversation in the web UI must be told, exactly as
claude-native's ``_post_clear_supersession`` does: the client auto-redirects
to the replacement conversation (the transient ``session.superseded`` event)
and the old transcript gains a durable assistant notice linking to it.

The real ``agy`` CLI is OAuth-only (interactive Google sign-in), which CI
cannot provision, so this drives the REAL runner launch, cold-start, reader,
and rotation paths against a scripted agy stand-in
(``tests/fixtures/antigravity/fake_agy.py``): a fake ``agy`` binary on the
runner's PATH that renders agy's composer chrome in the tmux pane and serves
agy's loopback connect-RPC surface. Typing ``/clear`` into its TUI mints a new
cascade just like the vendor CLI, and everything downstream of that signal —
detection, rotation, terminal transfer, and the missing supersession notice —
is the genuine omnigent code under test.

Journey: open the conversation, exchange one turn typed in the Terminal view
(mirrored to the web transcript by the RPC reader), run ``/clear`` in the TUI,
and wait for the rotation to land (the bridge state's ``session_id`` moves to
the replacement). Then assert the old conversation's viewer is redirected and
the old transcript links to the new conversation. While the bug is live the
rotation completes silently, so the redirect assertion times out on the dead
conversation's URL.

Marked ``nightly``: boots a dedicated server + runner and drives a TUI, like
``tests/e2e_ui/messages/test_native_codex_render_parity.py``.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.antigravity_native.bridge import (
    bridge_dir_for_bridge_id,
    is_placeholder_conversation_id,
    read_bridge_state,
)
from tests.e2e_ui.chat.test_codex_rotation_supersession import (
    _wait_for_supersession_notice,
)
from tests.e2e_ui.conftest import (
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _bind_session_runner,
    _find_free_port,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _ensure_chat_view,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _open_terminal_view,
    _type_into_tui,
    _wait_terminal_connected,
)

_FAKE_AGY_SOURCE = Path(__file__).resolve().parents[2] / "fixtures" / "antigravity" / "fake_agy.py"

_COLD_START_TIMEOUT_S = 60.0
_ROTATION_TIMEOUT_S = 60.0
_REDIRECT_TIMEOUT_MS = 30_000
_MIRROR_TIMEOUT_MS = 60_000


def _create_native_antigravity_session(base_url: str, runner_id: str) -> str:
    """Register the ``antigravity-native`` wrapper agent and bind its session.

    Mirrors ``_create_native_codex_session``: reuses the exact terminal-first
    spec ``omnigent antigravity`` ships and stamps the same wrapper /
    terminal-first labels, so binding triggers the runner's antigravity-native
    auto-bootstrap (agy terminal launch, cold-start, RPC reader).

    :param base_url: Spawned server base URL.
    :param runner_id: The token-bound runner id to bind.
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        ANTIGRAVITY_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.antigravity_native.main import _materialize_antigravity_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        spec_path = _materialize_antigravity_agent_spec(Path(tmp))
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo("antigravity-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: ANTIGRAVITY_NATIVE_WRAPPER_VALUE,
    }
    metadata = {"labels": labels, "workspace": str(_REPO_ROOT)}
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("antigravity-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


@pytest.fixture
def fake_agy_antigravity_session(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[tuple[str, str]]:
    """Spawn a dedicated server + runner whose ``agy`` is the scripted stand-in.

    A dedicated server keeps the fake-agy PATH shim and the fake
    ``GEMINI_API_KEY`` (which satisfies the antigravity-native credential
    readiness probe) away from the shared ``live_server`` used by unrelated
    tests.
    """
    if request.config.getoption("--ui-base-url"):
        pytest.skip("fake-agy antigravity e2e requires an isolated spawned server")
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the antigravity-native terminal")
    if shutil.which("openssl") is None:
        pytest.skip("openssl is required for the fake agy TLS endpoint")

    server_tmp = tmp_path_factory.mktemp("e2e_ui_fake_agy_server")
    shim_bin = server_tmp / "agy-shim" / "bin"
    shim_bin.mkdir(parents=True)
    fake_agy = shim_bin / "agy"
    fake_agy.write_bytes(_FAKE_AGY_SOURCE.read_bytes())
    fake_agy.chmod(fake_agy.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = server_tmp / "server.log"
    runner_log_path = server_tmp / "runner.log"
    db_path = server_tmp / "test.db"
    artifact_dir = server_tmp / "artifacts"
    artifact_dir.mkdir()
    agent_yaml_path = server_tmp / "hello_world.yaml"
    agent_yaml_path.write_text(_TEST_AGENT_YAML, encoding="utf-8")

    import secrets as _secrets

    from omnigent.runner.identity import token_bound_runner_id

    binding_token = _secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    shared_env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "PATH": f"{shim_bin}{os.pathsep}{os.environ.get('PATH', '')}",
        # A non-empty key satisfies the antigravity-native credential probe;
        # the fake agy never contacts Google.
        "GEMINI_API_KEY": "fake-e2e-agy-key",
    }
    server_env = {
        **shared_env,
        "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
    }
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }

    server_command = [
        sys.executable,
        "-m",
        "omnigent.cli",
        "server",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--database-uri",
        f"sqlite:///{db_path}",
        "--artifact-location",
        str(artifact_dir),
        "--agent",
        str(agent_yaml_path),
    ]

    log_handle = open(log_path, "w")  # noqa: SIM115
    runner_log_handle = open(runner_log_path, "w")  # noqa: SIM115
    proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    try:
        proc = subprocess.Popen(
            server_command, env=server_env, stdout=log_handle, stderr=subprocess.STDOUT
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_log_handle,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        last_error = "not polled yet"
        while True:
            if proc.poll() is not None:
                last_error = f"server exited early with code {proc.returncode}"
                break
            if runner_proc.poll() is not None:
                last_error = f"runner exited early with code {runner_proc.returncode}"
                break
            try:
                health = httpx.get(f"{base_url}/health", timeout=2)
                if health.status_code == 200:
                    status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json()["online"] is True:
                        break
                    last_error = f"runner status HTTP {status.status_code}"
                else:
                    last_error = f"health HTTP {health.status_code}"
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"fake-agy e2e server did not become healthy within "
                    f"{_HEALTH_TIMEOUT_S:.0f}s on {base_url} (last_error={last_error}).\n"
                    f"Server log:\n{log_path.read_text()[-3000:] if log_path.exists() else ''}\n"
                    f"Runner log:\n"
                    f"{runner_log_path.read_text()[-3000:] if runner_log_path.exists() else ''}"
                )
            time.sleep(_HEALTH_POLL_INTERVAL_S)
        if proc.poll() is not None or runner_proc.poll() is not None:
            raise RuntimeError(f"fake-agy e2e stack died during boot ({last_error})")

        session_id = _create_native_antigravity_session(base_url, runner_id)
        yield (base_url, session_id)
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        for child in (runner_proc, proc):
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        subprocess.run(
            ["pkill", "-f", str(shim_bin)],
            check=False,
            capture_output=True,
        )
        runner_log_handle.close()
        log_handle.close()


def _wait_for_cold_start(session_id: str, *, timeout_s: float = _COLD_START_TIMEOUT_S) -> str:
    """Wait until the runner's cold-start binds agy's real cascade id.

    :param session_id: The antigravity-native session id (also its bridge id).
    :param timeout_s: Max seconds to wait for the placeholder to be replaced.
    :returns: The bound cascade/conversation id.
    :raises AssertionError: When the cold-start never lands — a launch/driving
        problem, distinct from the missing-notification bug this test guards.
    """
    deadline = time.monotonic() + timeout_s
    observed: str | None = None
    while time.monotonic() < deadline:
        state = read_bridge_state(bridge_dir_for_bridge_id(session_id))
        if state is not None:
            observed = state.conversation_id
            if observed and not is_placeholder_conversation_id(observed):
                return observed
        time.sleep(0.5)
    raise AssertionError(
        f"agy cold-start did not bind a real cascade id within {timeout_s:.0f}s "
        f"(conversation_id still {observed!r}); this is a launch/driving problem, "
        "not the supersession-notice bug under test"
    )


def _wait_for_rotation(old_session_id: str, *, timeout_s: float = _ROTATION_TIMEOUT_S) -> str:
    """Wait until the reader rotates the bridge onto a replacement session.

    ``_rotate_session_for_cascade`` rewrites the bridge state (keyed by the
    original session id) with the replacement session id as its final step, so
    this is the harness's own signal that ``/clear`` rotation landed.

    :param old_session_id: The session id ``/clear`` rotates away from.
    :param timeout_s: Max seconds to wait for the rotation to land.
    :returns: The replacement Omnigent session id.
    :raises AssertionError: When no rotation lands within the budget.
    """
    deadline = time.monotonic() + timeout_s
    observed: str | None = None
    while time.monotonic() < deadline:
        state = read_bridge_state(bridge_dir_for_bridge_id(old_session_id))
        if state is not None:
            observed = state.session_id
            if observed and observed != old_session_id:
                return observed
        time.sleep(0.5)
    raise AssertionError(
        f"agy /clear did not rotate the Omnigent session within {timeout_s:.0f}s "
        f"(bridge session_id still {observed!r}); this is a TUI-driving problem, "
        "not the supersession-notice bug under test"
    )


@pytest.mark.nightly
@pytest.mark.timeout(300)
def test_agy_clear_rotation_notifies_superseded_conversation(
    page: Page,
    fake_agy_antigravity_session: tuple[str, str],
) -> None:
    """A ``/clear`` in the agy TUI redirects and annotates the old conversation."""
    base_url, old_session_id = fake_agy_antigravity_session

    page.goto(f"{base_url}/c/{old_session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _wait_for_cold_start(old_session_id)

    nonce = uuid.uuid4().hex[:8]
    _type_into_tui(page, f"Reply please {nonce}")
    _ensure_chat_view(page)
    expect(
        page.locator(_ASSISTANT, has_text=f"FAKE_AGY_REPLY Reply please {nonce}").first
    ).to_be_visible(timeout=_MIRROR_TIMEOUT_MS)

    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _type_into_tui(page, "/clear")

    new_session_id = _wait_for_rotation(old_session_id)
    replacement = httpx.get(f"{base_url}/v1/sessions/{new_session_id}", timeout=10.0)
    replacement.raise_for_status()

    # Best-effort return to the chat surface: the toggle can vanish once the
    # terminal transfers to the replacement session.
    chat_segment = page.get_by_test_id("view-mode-chat")
    if chat_segment.is_visible():
        chat_segment.click()

    expect(page).to_have_url(
        re.compile(re.escape(f"/c/{new_session_id}")), timeout=_REDIRECT_TIMEOUT_MS
    )

    _wait_for_supersession_notice(base_url, old_session_id, new_session_id)
