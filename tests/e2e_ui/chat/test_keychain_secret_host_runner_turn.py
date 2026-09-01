"""A web-UI turn must resolve a ``keychain:`` provider secret stored on the host.

Reproduces the reported journey: the user ran ``omnigent setup`` on their
desktop, which stored the provider API key in the OS keyring (GNOME Keyring on
the reporter's Ubuntu box) and wrote ``api_key_ref: keychain:quickrouter`` into
``~/.omnigent/config.yaml``. Setup succeeds and the CLI works — but when they
open the web UI and ask a question, the turn dies with::

    omnigent.errors.OmnigentError: no stored secret named 'quickrouter'; run
    `omnigent setup --no-internal-beta` to set it.

Journey:

1. run ``omnigent setup`` on the desktop: the provider key lands in the OS
   keyring and config.yaml references it as ``keychain:quickrouter``,
2. connect the machine as a host,
3. open the web UI and start a session on that host,
4. ask a question,
5. observe: on a buggy build the turn fails with the "no stored secret named
   'quickrouter'" error (the chat shows the error pill); the expected behavior
   is a normal completed turn.

Mechanism exercised (root-cause lead, not asserted): the host daemon spawns
the session runner through ``_build_runner_env`` (omnigent/host/connect.py),
whose allowlist strips the environment the OS keyring needs — on a real
desktop ``DBUS_SESSION_BUS_ADDRESS`` / ``XDG_RUNTIME_DIR``, here stood in by
``PYTHON_KEYRING_BACKEND``, which is likewise not allowlisted. The runner then
resolves the ``keychain:`` ref against ``keyring.backends.fail.Keyring``,
falls back to the (empty) ``secrets.json`` file store, and the provider-config
secret resolution fails loud. ``env:`` refs are covered by the
``provider_credential_env_vars`` forwarding; ``keychain:`` refs
were excluded from it. Direct CLI runs are unaffected because
``_start_cli_runner_process`` inherits the full environment — matching the
report (setup works; the web-UI turn fails).

The OS keyring is stood in by a file-backed ``keyring`` backend selected via
``PYTHON_KEYRING_BACKEND`` in the daemon's environment. Both the real desktop
keyring and the stand-in share the property under test: reachable from the
daemon's environment, unreachable once the runner-spawn env strip drops the
selector. The secret is stored through the real setup storage code path
(``keychain_desktop_store.py``, which calls the same helper setup calls).

On a buggy build this test FAILS at the ``secret_error is None`` assertion
(the reproduction); after a fix the same journey completes and the test
passes.

Run::

    pytest tests/e2e_ui/chat/test_keychain_secret_host_runner_turn.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import set_fallback_mock_llm

_REPO_ROOT = Path(__file__).resolve().parents[3]

# The secret name from the bug report: the providers: entry (and thus the
# keychain slot) the reporter's setup created.
_SECRET_NAME = "quickrouter"
_SECRET_VALUE = "sk-mock-quickrouter"

# The final assistant reply on a healthy build; its presence in the
# transcript is the "turn completed" signal.
_DONE_SENTINEL = "KEYCHAIN_TURN_COMPLETED_SENTINEL"

# Seconds for the host daemon to register and for its runner to tunnel in.
_HOST_ONLINE_TIMEOUT_S = 40.0
_RUNNER_ONLINE_TIMEOUT_S = 40.0

# Ceiling for the turn to settle (complete or fail).
_TURN_SETTLE_TIMEOUT_S = 120.0

# The stand-in for the user's desktop OS keyring (GNOME Keyring): a real
# ``keyring`` backend, selected via PYTHON_KEYRING_BACKEND, persisting to a
# JSON file next to its module. Reachable wherever the selector env var is
# set; unreachable (fail.Keyring) wherever the selector is stripped — the
# same reachability contract as the D-Bus-bound desktop keyring.
_FAKE_KEYRING_MODULE = """\
import json
from pathlib import Path

import keyring.backend

_STORE = Path(__file__).with_name("fake_keyring_store.json")


class FakeDesktopKeyring(keyring.backend.KeyringBackend):
    priority = 100

    def _read(self):
        return json.loads(_STORE.read_text()) if _STORE.exists() else {}

    def get_password(self, service, username):
        return self._read().get(f"{service}/{username}")

    def set_password(self, service, username, password):
        data = self._read()
        data[f"{service}/{username}"] = password
        _STORE.write_text(json.dumps(data))

    def delete_password(self, service, username):
        data = self._read()
        data.pop(f"{service}/{username}", None)
        _STORE.write_text(json.dumps(data))
"""

_FAKE_BACKEND_SELECTOR = "fake_desktop_keyring.FakeDesktopKeyring"

_AGENT_YAML = """\
name: {name}
description: Keychain-secret resolution probe for host-launched runners.
executor:
  harness: openai-agents
  model: {model}
prompt: |
  You are a terse test agent. Reply concisely.
"""


def _agent_bundle(name: str, model: str) -> bytes:
    """Gzip-tar the inline agent YAML for multipart upload."""
    yaml_text = _AGENT_YAML.format(name=name, model=model)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo(name=f"{name}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _loopback_no_proxy(env: dict[str, str]) -> dict[str, str]:
    """Exempt loopback from any ambient HTTP proxy in *env*.

    CI sandboxes route all traffic through a proxy; the daemon and the
    driver must reach the loopback test server directly.
    """
    for var in ("NO_PROXY", "no_proxy"):
        current = env.get(var, "")
        env[var] = "127.0.0.1,localhost" + (f",{current}" if current else "")
    return env


@pytest.fixture(scope="module")
def keychain_host(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """Stand up the reporter's desktop: keyring-stored secret + host daemon.

    Performs journey steps 1-2 (the ``omnigent setup`` outcome, then
    connecting the machine as a host):

    - a fake desktop OS keyring importable via PYTHONPATH and selected via
      ``PYTHON_KEYRING_BACKEND``,
    - the provider secret stored through the real setup storage path
      (asserting it landed in the keyring, NOT the file fallback),
    - ``config.yaml`` with a default openai-family gateway provider whose
      ``api_key_ref`` is ``keychain:quickrouter``, pointed at the mock LLM,
    - a host daemon (the process that spawns web-session runners) running
      with that home + keyring selector.

    Yields ``(host_id, model)``.
    """
    work = tmp_path_factory.mktemp("keychain_host")
    home = work / "home"
    cfg_home = home / ".omnigent"
    cfg_home.mkdir(parents=True)
    kb_dir = work / "kb"
    kb_dir.mkdir()
    (kb_dir / "fake_desktop_keyring.py").write_text(_FAKE_KEYRING_MODULE)

    model = f"keychain-probe-{uuid.uuid4().hex[:8]}"
    host_id = uuid.uuid4().hex
    (cfg_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "host": {"host_id": host_id, "name": f"keychain-host-{host_id[:8]}"},
                "providers": {
                    _SECRET_NAME: {
                        "kind": "gateway",
                        "default": True,
                        "openai": {
                            "base_url": f"{mock_llm_server_url}/v1",
                            "api_key_ref": f"keychain:{_SECRET_NAME}",
                            "models": {"default": model},
                        },
                    }
                },
            }
        )
    )

    pythonpath = os.pathsep.join(
        str(p)
        for p in (
            _REPO_ROOT,
            _REPO_ROOT / "sdks" / "python-client",
            _REPO_ROOT / "sdks" / "ui",
            kb_dir,
        )
    )
    desktop_env = _loopback_no_proxy(
        {
            **os.environ,
            "HOME": str(home),
            "OMNIGENT_CONFIG_HOME": str(cfg_home),
            "PYTHONPATH": pythonpath,
            "PYTHON_KEYRING_BACKEND": _FAKE_BACKEND_SELECTOR,
            "OMNIGENT_SKIP_ONBOARD": "1",
            "OMNIGENT_NO_UPDATE_CHECK": "1",
        }
    )
    # Strip ambient credentials so the configured provider is the only
    # route — exactly the reporter's machine, which had no exported keys.
    for var in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY"):
        desktop_env.pop(var, None)

    # Journey step 1: setup stores the key. Same code path `omnigent setup`
    # calls (see keychain_desktop_store.py); run in a subprocess so the
    # keyring-backend selector applies.
    store = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("keychain_desktop_store.py")),
            _SECRET_NAME,
            _SECRET_VALUE,
        ],
        env=desktop_env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert f"LOADED:{_SECRET_VALUE}" in store.stdout, (
        f"desktop-side secret storage failed: stdout={store.stdout!r} stderr={store.stderr!r}"
    )
    # The secret went to the OS keyring, not the file fallback — the
    # precondition that makes the runner's env-stripped resolution fail.
    assert not (cfg_home / "secrets.json").exists(), (
        "secret unexpectedly landed in the file store; the keyring backend "
        "was not selected and this journey would not exercise the bug"
    )

    # Journey step 2: connect the machine as a host.
    daemon_log = work / "daemon.log"
    log_handle = open(daemon_log, "w")  # noqa: SIM115 — fd dup'd into child; closed below
    proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", live_server],
        env=desktop_env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    log_handle.close()  # child holds its own dup of the fd

    deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
    online = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"host daemon exited early (code {proc.returncode}); "
                f"log:\n{daemon_log.read_text()[-3000:]}"
            )
        try:
            resp = httpx.get(f"{live_server}/v1/hosts", timeout=2)
            if resp.status_code == 200 and any(
                h.get("host_id") == host_id and h.get("status") == "online"
                for h in resp.json().get("hosts", [])
            ):
                online = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.5)

    if not online:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        raise RuntimeError(
            f"host daemon never came online within {_HOST_ONLINE_TIMEOUT_S:.0f}s; "
            f"log:\n{daemon_log.read_text()[-3000:]}"
        )

    try:
        yield host_id, model
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture
def host_session(
    live_server: str,
    keychain_host: tuple[str, str],
    tmp_path: Path,
) -> Iterator[tuple[str, str]]:
    """Create a session and launch its runner on the keychain host.

    Journey step 3 (start a session on that host): the same
    create-session → launch-host-runner → bind sequence the web UI's
    new-chat flow performs. Yields ``(session_id, model)``.
    """
    host_id, model = keychain_host
    name = f"keychain_probe_{uuid.uuid4().hex[:8]}"

    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={
            "bundle": (
                "agent.tar.gz",
                _agent_bundle(name, model),
                "application/gzip",
            )
        },
        headers={"Origin": "omnigent://internal"},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    try:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        launch_resp = httpx.post(
            f"{live_server}/v1/hosts/{host_id}/runners",
            json={"session_id": session_id, "workspace": str(workspace)},
            timeout=60.0,
        )
        assert launch_resp.status_code == 200, (
            f"host runner launch failed: {launch_resp.status_code} {launch_resp.text}"
        )
        runner_id = launch_resp.json()["runner_id"]

        deadline = time.monotonic() + _RUNNER_ONLINE_TIMEOUT_S
        while time.monotonic() < deadline:
            status_resp = httpx.get(f"{live_server}/v1/runners/{runner_id}/status", timeout=2)
            if status_resp.status_code == 200 and status_resp.json().get("online") is True:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(f"host-launched runner {runner_id} never came online")

        httpx.patch(
            f"{live_server}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=10.0,
        ).raise_for_status()

        yield session_id, model
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)


def _poll_turn_outcome(base_url: str, session_id: str) -> tuple[bool, str | None]:
    """Poll the transcript until the turn settles.

    :returns: ``(completed, secret_error_message)`` — ``completed`` when the
        assistant sentinel landed; ``secret_error_message`` when an ``error``
        item carrying the "no stored secret" failure was persisted.
    """
    deadline = time.monotonic() + _TURN_SETTLE_TIMEOUT_S
    completed = False
    secret_error: str | None = None
    while time.monotonic() < deadline:
        resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        resp.raise_for_status()
        body = resp.json()
        for item in body.get("items", []):
            data = item.get("data") or {}
            item_type = item.get("type")
            if item_type == "error":
                message = str(item.get("message") or data.get("message") or "")
                if "no stored secret named" in message:
                    secret_error = message
            elif item_type == "message":
                role = item.get("role") or data.get("role")
                content = item.get("content") or data.get("content") or []
                text = " ".join(
                    str(part.get("text", "")) for part in content if isinstance(part, dict)
                )
                if role == "assistant" and _DONE_SENTINEL in text:
                    completed = True
        last_error = str(body.get("last_task_error") or "")
        if "no stored secret named" in last_error:
            secret_error = secret_error or last_error
        if completed or secret_error is not None:
            break
        time.sleep(1.0)
    return completed, secret_error


@pytest.mark.timeout(240)
def test_keychain_secret_resolves_on_host_launched_web_turn(
    page: Page,
    host_session: tuple[str, str],
    live_server: str,
    mock_llm_server_url: str,
) -> None:
    """Asking a question in the web UI must not lose the keychain secret.

    On a buggy build this fails at the ``secret_error is None`` assertion:
    the host-daemon-spawned runner cannot reach the OS keyring the setup
    wizard stored the provider key in (the spawn env strip drops the
    keyring's environment), so turn setup dies with the reported
    "no stored secret named 'quickrouter'" error and the chat shows the
    error pill instead of a reply.
    """
    session_id, model = host_session
    set_fallback_mock_llm(mock_llm_server_url, model, _DONE_SENTINEL)

    # Journey step 4: open the session in the web UI and ask a question.
    page.goto(f"{live_server}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Say hello.")
    page.get_by_role("button", name="Send", exact=True).click()

    completed, secret_error = _poll_turn_outcome(live_server, session_id)

    # When the secret failed to resolve, let the user-visible failure land
    # on screen (the error pill) before failing — the recorded journey then
    # ends on exactly what the user sees.
    if secret_error is not None:
        with contextlib.suppress(AssertionError):
            expect(page.get_by_test_id("error-pill").first).to_be_visible(timeout=15_000)
        page.wait_for_timeout(1_500)

    # THE BUG: the runner spawned by the host daemon cannot resolve the
    # keychain: secret that `omnigent setup` stored in the OS keyring.
    assert secret_error is None, (
        f"web-UI turn on a host-launched runner failed to resolve the "
        f"keychain-stored provider secret: {secret_error!r}. The setup "
        f"wizard stored this secret in the OS keyring on the same machine "
        f"the host daemon runs on; a session runner spawned by that daemon "
        f"must resolve it (the spawn-env strip must not sever the runner "
        f"from the host's secret store)."
    )

    assert completed, (
        f"The turn neither completed nor failed with the no-stored-secret "
        f"error within {_TURN_SETTLE_TIMEOUT_S:.0f}s — the journey never "
        f"settled (mock LLM mis-scripted or the runner never started)."
    )

    # The user-visible outcome: the reply rendered, no error pill.
    expect(
        page.locator('[data-testid="message-bubble"][data-role="assistant"]').last
    ).to_contain_text(_DONE_SENTINEL, timeout=30_000)
    expect(page.get_by_test_id("error-pill")).to_have_count(0)
