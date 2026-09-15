"""A ``keychain:`` provider secret must survive the host daemon -> runner spawn.

Journey (a Linux host with an OS-keyring-backed provider key):

1. ``omnigent setup`` stores the provider API key in the OS keyring and writes
   a provider whose ``api_key_ref`` is ``keychain:quickrouter``.
2. The machine comes online as a host (``omnigent host``/``connect``).
3. A web session starts on that host and the user sends a chat message.

Before the fix, the host daemon's ``_build_runner_env`` allowlist dropped the
env the keyring needs (``DBUS_SESSION_BUS_ADDRESS`` / ``XDG_RUNTIME_DIR`` /
``PYTHON_KEYRING_BACKEND``), so the spawned runner fell back to an empty secret
store and the turn failed during provider resolution with::

    no stored secret named 'quickrouter'; run `omnigent setup ...` to set it.

Two keyring lanes cover both halves of that allowlist fix:

- ``secretservice``: a real GNOME Keyring on a private D-Bus session bus (the
  reported desktop setup); the runner reaches it only if
  ``DBUS_SESSION_BUS_ADDRESS`` propagates. Skipped when ``dbus-daemon`` /
  ``gnome-keyring-daemon`` / ``secretstorage`` are unavailable.
- ``pinned_backend``: a file-backed backend selected via
  ``PYTHON_KEYRING_BACKEND`` (keyring's documented selector), which must
  propagate the same way. Runs anywhere.

Both lanes assert the fixed behavior (the turn completes, no keychain error),
so they fail on a build that strips the env and pass once it propagates.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import shutil
import signal
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import (
    apply_runner_env,
    compat_runner_cwd,
    compat_runner_python,
    runner_executable,
)
from tests.e2e.conftest import (
    POLL_INTERVAL_S,
    configure_mock_llm,
    lookup_agent_id,
    poll_session_until_terminal,
    send_user_message_to_session,
    upload_agent,
)
from tests.e2e.helpers import final_assistant_text

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _abs_source_pythonpath(*prepend: Path) -> str:
    """Absolute PYTHONPATH covering every omnigent source root.

    The ambient ``PYTHONPATH`` names the ``sdks`` roots relative to the repo
    root (the pytest cwd); a daemon-spawned runner runs from a different cwd, so
    each relative entry is resolved to an absolute path here.
    """
    entries: list[str] = [str(p) for p in prepend]
    for raw in os.environ.get("PYTHONPATH", "").split(os.pathsep):
        if not raw:
            continue
        entries.append(raw if os.path.isabs(raw) else str((_REPO_ROOT / raw).resolve()))
    return os.pathsep.join(entries)


_SECRET_NAME = "quickrouter"
_SECRET_VALUE = "mock-keychain-key"

_SECRETSERVICE_AVAILABLE = (
    shutil.which("dbus-daemon") is not None
    and shutil.which("gnome-keyring-daemon") is not None
    and importlib.util.find_spec("secretstorage") is not None
)

# A minimal ``keyring`` backend that persists to a JSON file under the config
# home. It is only ever *selected* when ``PYTHON_KEYRING_BACKEND`` names it —
# exactly the env the runner used to lose.
_KEYRING_BACKEND_SOURCE = """\
import json
import os

import keyring.backend


def _store_path():
    home = os.environ.get("OMNIGENT_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".omnigent"
    )
    return os.path.join(home, "e2e_file_keyring.json")


class FileKeyring(keyring.backend.KeyringBackend):
    priority = 5

    def _read(self):
        try:
            with open(_store_path(), encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            return {}

    def _write(self, data):
        path = _store_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def get_password(self, service, username):
        return self._read().get(f"{service}/{username}")

    def set_password(self, service, username, password):
        data = self._read()
        data[f"{service}/{username}"] = password
        self._write(data)

    def delete_password(self, service, username):
        data = self._read()
        data.pop(f"{service}/{username}", None)
        self._write(data)
"""

_KEYRING_BACKEND = "e2e_file_keyring.FileKeyring"

_KEYCHAIN_AGENT_YAML = """\
name: keychain-e2e-agent
description: Minimal agent that resolves a keychain provider secret.
executor:
  harness: openai-agents
  model: gpt-4o-mini
prompt: |
  You are a terse smoke-test assistant. Follow the user's instruction exactly.
"""


def _terminate(proc: subprocess.Popen[bytes] | subprocess.Popen[str]) -> None:
    with contextlib.suppress(ProcessLookupError):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@contextlib.contextmanager
def _pinned_backend_lane(tmp_path: Path) -> Iterator[dict[str, str]]:
    """Select a file-backed keyring via ``PYTHON_KEYRING_BACKEND``."""
    keyring_dir = tmp_path / "keyring_backend"
    keyring_dir.mkdir(parents=True, exist_ok=True)
    (keyring_dir / "e2e_file_keyring.py").write_text(_KEYRING_BACKEND_SOURCE)
    yield {
        "PYTHON_KEYRING_BACKEND": _KEYRING_BACKEND,
        "PYTHONPATH": _abs_source_pythonpath(keyring_dir, _REPO_ROOT),
    }


@contextlib.contextmanager
def _secretservice_lane(tmp_path: Path) -> Iterator[dict[str, str]]:
    """Run a real GNOME Keyring on a private session bus, like a desktop."""
    runtime_dir = tmp_path / "xdg_runtime"
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    dbus = subprocess.Popen(
        ["dbus-daemon", "--session", "--print-address=1", "--nofork"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert dbus.stdout is not None
        bus_address = dbus.stdout.readline().strip()
        assert bus_address, "dbus-daemon did not print a session bus address"
        keyring_env = {
            **os.environ,
            "DBUS_SESSION_BUS_ADDRESS": bus_address,
            "HOME": str(tmp_path),
            "XDG_RUNTIME_DIR": str(runtime_dir),
        }
        gkr = subprocess.Popen(
            ["gnome-keyring-daemon", "--foreground", "--unlock", "--components=secrets"],
            env=keyring_env,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
        )
        try:
            assert gkr.stdin is not None
            gkr.stdin.write("e2e-keyring-password\n")
            gkr.stdin.close()
            yield {
                "DBUS_SESSION_BUS_ADDRESS": bus_address,
                "XDG_RUNTIME_DIR": str(runtime_dir),
                "PYTHONPATH": _abs_source_pythonpath(_REPO_ROOT),
            }
        finally:
            _terminate(gkr)
    finally:
        _terminate(dbus)


_KEYRING_LANES = {
    "pinned_backend": _pinned_backend_lane,
    "secretservice": _secretservice_lane,
}

_KEYRING_ENV_VARS = ("PYTHON_KEYRING_BACKEND", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR")


def _lane_scoped_env(base: dict[str, str], lane_env: dict[str, str]) -> dict[str, str]:
    """Only the lane's keyring env may reach the journey, never the machine's."""
    env = {**base, **lane_env}
    env.pop("OMNIGENT_DISABLE_KEYRING", None)
    for name in _KEYRING_ENV_VARS:
        if name not in lane_env:
            env.pop(name, None)
    return env


def _write_keychain_agent(tmp_path: Path) -> Path:
    agent_dir = tmp_path / "keychain-e2e-agent"
    agent_dir.mkdir()
    (agent_dir / "keychain-e2e-agent.yaml").write_text(_KEYCHAIN_AGENT_YAML)
    return agent_dir


def _provider_config(mock_llm_server_url: str, host_id: str, host_name: str) -> str:
    return yaml.safe_dump(
        {
            "host": {"host_id": host_id, "name": host_name},
            "providers": {
                "quickrouter": {
                    "kind": "key",
                    "default": True,
                    "openai": {
                        "base_url": f"{mock_llm_server_url}/v1",
                        "api_key_ref": f"keychain:{_SECRET_NAME}",
                        "models": {"default": "gpt-4o-mini"},
                    },
                }
            },
        },
        default_flow_style=False,
        sort_keys=True,
    )


def _store_keychain_secret(config_home: Path, lane_env: dict[str, str]) -> None:
    """Store the secret through the lane's keyring, like ``omnigent setup``.

    Retries briefly: the SecretService lane's keyring daemon may still be
    coming up when the first store attempt runs.
    """
    env = _lane_scoped_env({**os.environ, "OMNIGENT_CONFIG_HOME": str(config_home)}, lane_env)
    store_cmd = [
        runner_executable(),
        "-c",
        f"import keyring; keyring.set_password('omnigent', {_SECRET_NAME!r}, {_SECRET_VALUE!r})",
    ]
    deadline = time.monotonic() + 20.0
    while True:
        store = subprocess.run(
            store_cmd, env=env, capture_output=True, text=True, cwd=compat_runner_cwd()
        )
        if store.returncode == 0:
            break
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"keychain store failed to set up: rc={store.returncode} "
                f"out={store.stdout!r} err={store.stderr!r}"
            )
        time.sleep(0.5)
    # Sanity: the same env resolves the ref (the host side works before spawn).
    probe = subprocess.run(
        [
            runner_executable(),
            "-c",
            "from omnigent.onboarding.provider_config import resolve_secret; "
            f"print(resolve_secret('keychain:{_SECRET_NAME}'))",
        ],
        env=env,
        capture_output=True,
        text=True,
        cwd=compat_runner_cwd(),
    )
    assert probe.returncode == 0 and _SECRET_VALUE in probe.stdout, (
        f"host-side keychain resolution failed to set up: "
        f"rc={probe.returncode} out={probe.stdout!r} err={probe.stderr!r}"
    )


def _spawn_keychain_host_daemon(
    *,
    tmp_path: Path,
    live_server: str,
    mock_llm_server_url: str,
    lane_env: dict[str, str],
) -> tuple[subprocess.Popen[bytes], str, Path]:
    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)

    host_id = uuid.uuid4().hex
    host_name = f"keychain-host-{uuid.uuid4().hex[:12]}"
    (omni_dir / "config.yaml").write_text(
        _provider_config(mock_llm_server_url, host_id, host_name)
    )
    _store_keychain_secret(omni_dir, lane_env)

    daemon_log = tmp_path / "host-daemon.log"
    # lane_env is the env the fix must propagate to the runner: it lets the
    # runner reach the same keyring that holds the secret.
    env = _lane_scoped_env(
        {
            **os.environ,
            "HOME": str(tmp_path),
            "OMNIGENT_CONFIG_HOME": str(omni_dir),
            PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
        },
        lane_env,
    )
    # No OPENAI_* here: the mock is reached through the provider's base_url, so
    # the turn depends on resolving keychain:quickrouter, not an ambient key.
    env.pop("OPENAI_API_KEY", None)
    env.pop("OPENAI_BASE_URL", None)

    command = [
        runner_executable(),
        "-m",
        "omnigent.host._daemon_entry",
        "--server",
        live_server,
    ]
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            command,
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    return proc, host_id, daemon_log


def _wait_for_host_online(client: httpx.Client, host_id: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = client.get("/v1/hosts")
            if resp.status_code == 200:
                for host in resp.json().get("hosts", []):
                    if host["host_id"] == host_id and host["status"] == "online":
                        return
        except httpx.ConnectError:
            pass
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"Host {host_id!r} did not appear online within {timeout}s")


@pytest.mark.skipif(
    compat_runner_python() is not None,
    reason="keyring backends and client libs ride PYTHONPATH, which compat mode strips",
)
@pytest.mark.parametrize("keyring_lane", sorted(_KEYRING_LANES))
def test_host_runner_resolves_keychain_provider_secret(
    keyring_lane: str,
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A host-launched turn resolves a ``keychain:`` provider secret and completes."""
    if keyring_lane == "secretservice" and not _SECRETSERVICE_AVAILABLE:
        pytest.skip("needs dbus-daemon, gnome-keyring-daemon, and secretstorage")

    marker = "KEYCHAIN_SECRET_RESOLVED_OK"
    match_token = f"keychain-probe-{keyring_lane}"
    prompt = f"{match_token} reply with exactly {marker} and nothing else."
    configure_mock_llm(mock_llm_server_url, [{"text": marker}], match=match_token)

    with _KEYRING_LANES[keyring_lane](tmp_path) as lane_env:
        proc, host_id, _daemon_log = _spawn_keychain_host_daemon(
            tmp_path=tmp_path,
            live_server=live_server,
            mock_llm_server_url=mock_llm_server_url,
            lane_env=lane_env,
        )
        try:
            _wait_for_host_online(http_client, host_id, timeout=30.0)

            agent_name = upload_agent(http_client, _write_keychain_agent(tmp_path))
            agent_id = lookup_agent_id(http_client, agent_name)

            session_resp = http_client.post("/v1/sessions", json={"agent_id": agent_id})
            session_resp.raise_for_status()
            session_id = session_resp.json()["id"]

            launch_resp = http_client.post(
                f"/v1/hosts/{host_id}/runners",
                json={"session_id": session_id, "workspace": str(tmp_path)},
                timeout=60.0,
            )
            assert launch_resp.status_code == 200, (
                f"Launch failed: {launch_resp.status_code} {launch_resp.text}"
            )
            runner_id = launch_resp.json()["runner_id"]

            deadline = time.monotonic() + 30.0
            runner_online = False
            while time.monotonic() < deadline:
                status_resp = http_client.get(f"/v1/runners/{runner_id}/status")
                if status_resp.status_code == 200 and status_resp.json().get("online") is True:
                    runner_online = True
                    break
                time.sleep(0.5)
            assert runner_online, f"Runner {runner_id} never came online after launch"

            http_client.patch(
                f"/v1/sessions/{session_id}",
                json={"runner_id": runner_id},
            ).raise_for_status()

            response_id = send_user_message_to_session(
                http_client, session_id=session_id, content=prompt
            )
            body = poll_session_until_terminal(
                http_client,
                session_id=session_id,
                response_id=response_id,
                timeout=180,
            )

            error_text = str(body.get("error") or "")
            # The bug: the runner lost the keyring-selecting env and fell back
            # to an empty secret store, so keychain:quickrouter never resolved.
            assert f"no stored secret named '{_SECRET_NAME}'" not in error_text, (
                "runner could not resolve the keychain provider secret (host->runner "
                f"env strip dropped the keyring env): {error_text!r}"
            )
            assert body["status"] == "completed", f"turn did not complete: {body.get('error')!r}"
            assert marker in final_assistant_text(body), (
                f"marker {marker!r} missing from response: {final_assistant_text(body)!r}"
            )
        finally:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
