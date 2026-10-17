"""E2e: a provider declared in an earlier user config file must reach the spawned
``opencode serve`` when a later file only supplies the model pin.

The runner's per-session ``XDG_CONFIG_HOME`` hides the user's global config from
the spawned server, so ``maybe_merge_user_provider_config()`` carries the user's
providers across. This drives the real daemon-to-runner-to-server boundary (host
daemon -> host-bound ``opencode-native-ui`` session -> ``opencode serve`` ->
``GET /config/providers``) for the ``opencode.json``/``opencode.jsonc`` split.
File-name discovery (including ``config.json``) and merge semantics are
unit-tested in ``tests/test_opencode_native_provider.py`` and
``tests/test_opencode_native_bridge.py``. The ground truth is first checked
against the installed CLI (``opencode debug config``) so an upstream merge change
fails as a rig error, not an Omnigent failure.

Needs ``opencode`` and ``tmux`` on PATH; no LLM turn is driven.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.harnesses.opencode_native.bridge import _ID_HASH_CHARS
from omnigent.native.native_coding_agents import OPENCODE_NATIVE_AGENT_NAME
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.helpers import POLL_INTERVAL_S


def _functional_opencode_dir() -> str | None:
    """Return the dir of the first PATH ``opencode`` real binary that answers ``--version``.

    Skip shell-script wrappers: some environments put a shim earlier on PATH
    that rewrites ``XDG_CONFIG_HOME`` to its own gateway config when the caller
    leaves it unset, which would hide the staged user config from the
    ground-truth ``debug config`` run. Pick the first non-shim binary that
    actually works, so both that run and the runner's ``opencode serve`` see the
    user's config.
    """
    for path_dir in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(path_dir) / "opencode"
        if not (candidate.is_file() and os.access(candidate, os.X_OK)):
            continue
        try:
            with candidate.open("rb") as handle:
                if handle.read(2) == b"#!":
                    continue
        except OSError:
            continue
        try:
            probe = subprocess.run([str(candidate), "--version"], capture_output=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return path_dir
    return None


_OPENCODE_BIN_DIR = _functional_opencode_dir()

pytestmark = pytest.mark.skipif(
    _OPENCODE_BIN_DIR is None or shutil.which("tmux") is None,
    reason="opencode-native user-config e2e needs a working `opencode` and `tmux` on PATH",
)

_PROVIDER_ID = "my-gateway"
_PROVIDER_BLOCK: dict[str, Any] = {
    _PROVIDER_ID: {
        "npm": "@ai-sdk/openai-compatible",
        "name": "my-gateway",
        "options": {"baseURL": "https://my-gateway.example/v1", "apiKey": "sk-test"},
        "models": {"gpt-4": {"name": "gpt-4"}},
    }
}


def _spawn_host_daemon(
    *, tmp_path: Path, live_server: str, home: Path, data_dir: Path
) -> subprocess.Popen[bytes]:
    """Spawn an ``omnigent host`` daemon whose HOME is the staged user machine."""
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    # Absolute entries only: the runner's cwd is the session workspace, so a
    # relative sdks/python-client entry would stop resolving.
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(repo_root),
            str(repo_root / "sdks" / "python-client"),
            str(repo_root / "sdks" / "ui"),
            env.get("PYTHONPATH", ""),
        ]
    )
    # HOME is allowlisted daemon->runner, so the runner resolves the staged
    # ~/.config/opencode as the user's real config and keeps its bridge dirs
    # under the isolated home instead of the CI user's.
    env["HOME"] = str(home)
    env.pop("XDG_CONFIG_HOME", None)
    env["OMNIGENT_DATA_DIR"] = str(data_dir)
    # The daemon gates opencode-native launches on an available provider
    # credential; no LLM call is made, so a placeholder un-gates the launch.
    env.setdefault("OPENAI_API_KEY", "sk-e2e-placeholder")
    if _OPENCODE_BIN_DIR:
        env["PATH"] = os.pathsep.join([_OPENCODE_BIN_DIR, env.get("PATH", "")])
    daemon_log = tmp_path / "host-daemon.log"
    with open(daemon_log, "w") as log_fh:
        return subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )


def _online_host_id(client: httpx.Client, timeout: float = 60.0) -> str:
    """Poll ``GET /v1/hosts`` until at least one host is online."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get("/v1/hosts")
        if resp.status_code == 200:
            online = [h for h in resp.json().get("hosts", []) if h["status"] == "online"]
            if online:
                return str(online[0]["host_id"])
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"No host came online within {timeout}s")


def _wait_for_terminal(
    client: httpx.Client, *, session_id: str, resource_id: str, timeout: float = 240.0
) -> None:
    """Poll resources until the runner registers the opencode terminal."""
    deadline = time.monotonic() + timeout
    last: list[object] = []
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}/resources")
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            last = [r.get("id") for r in data]
            if any(r.get("id") == resource_id and r.get("type") == "terminal" for r in data):
                return
        time.sleep(1.0)
    raise AssertionError(
        f"Terminal {resource_id!r} never appeared for {session_id} within {timeout}s; saw {last!r}"
    )


def _bridge_state(home: Path, session_id: str, timeout: float = 60.0) -> dict[str, Any]:
    """Read the session's opencode-native ``state.json`` from the daemon's home."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:_ID_HASH_CHARS]
    state_path = home / ".omnigent" / "opencode-native" / digest / "state.json"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            time.sleep(1.0)
    raise AssertionError(f"Bridge state never appeared at {state_path}")


def _spawned_server_get(state: dict[str, Any], path: str) -> Any:
    """GET *path* from the session's spawned ``opencode serve``."""
    headers: dict[str, str] = {}
    secret = state.get("auth_secret")
    if secret:
        token = base64.b64encode(f"opencode:{secret}".encode()).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    resp = httpx.get(f"{state['server_base_url']}{path}", headers=headers, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def _opencode_cli_effective_config(home: Path) -> dict[str, Any]:
    """Ground truth: the effective global config the opencode CLI itself resolves."""
    assert _OPENCODE_BIN_DIR is not None
    env = os.environ.copy()
    env["HOME"] = str(home)
    env.pop("XDG_CONFIG_HOME", None)
    # cwd must not contain a project opencode config, which would merge in too.
    proc = subprocess.run(
        [str(Path(_OPENCODE_BIN_DIR) / "opencode"), "debug", "config"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(home),
    )
    assert proc.returncode == 0, f"rig failure: `opencode debug config` failed: {proc.stderr}"
    out = proc.stdout
    start = out.find("{")
    assert start != -1, f"rig failure: `opencode debug config` emitted no JSON: {out[:400]!r}"
    try:
        return json.loads(out[start:])
    except json.JSONDecodeError as err:
        raise AssertionError(
            f"rig failure: could not parse `opencode debug config` output as JSON: {err}; "
            f"output was: {out[:400]!r}"
        ) from err


def _spawned_provider_ids(state: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """Return provider ids of the spawned server, plus the raw payload."""
    payload = _spawned_server_get(state, "/config/providers")
    providers = payload.get("providers", [])
    return [p.get("id") for p in providers], payload


def _run_session_and_get_providers(
    http_client: httpx.Client, tmp_path: Path, live_server: str, home: Path
) -> tuple[list[str], dict[str, Any], dict[str, Any]]:
    """Create a host-bound opencode-native session; return the spawned server's providers."""
    resp = http_client.get("/v1/agents")
    resp.raise_for_status()
    agent_id = next(
        (a["id"] for a in resp.json()["data"] if a["name"] == OPENCODE_NATIVE_AGENT_NAME), None
    )
    assert agent_id is not None, "opencode-native-ui agent not seeded"

    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    data_dir = tmp_path / "omnigent-data"
    data_dir.mkdir(exist_ok=True)

    daemon = _spawn_host_daemon(
        tmp_path=tmp_path, live_server=live_server, home=home, data_dir=data_dir
    )
    try:
        host_id = _online_host_id(http_client)
        create = http_client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
            timeout=60.0,
        )
        create.raise_for_status()
        session_id = create.json()["id"]

        _wait_for_terminal(
            http_client,
            session_id=session_id,
            resource_id=terminal_resource_id("opencode", "main"),
        )
        state = _bridge_state(home, session_id)
        provider_ids, payload = _spawned_provider_ids(state)
        effective = _spawned_server_get(state, "/config")
        return provider_ids, payload, effective
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon.kill()


@pytest.mark.timeout(420)
def test_user_provider_in_opencode_json_reaches_spawned_server(
    http_client: httpx.Client,
    tmp_path: Path,
    live_server: str,
) -> None:
    """A split-file provider and model pin both reach the spawned OpenCode server."""
    home = tmp_path / "home"
    cfg_dir = home / ".config" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.jsonc").write_text(
        '{\n  // pin the default model\n  "$schema": "https://opencode.ai/config.json",\n'
        '  "model": "my-gateway/gpt-4"\n}\n',
        encoding="utf-8",
    )
    (cfg_dir / "opencode.json").write_text(
        json.dumps({"$schema": "https://opencode.ai/config.json", "provider": _PROVIDER_BLOCK}),
        encoding="utf-8",
    )

    ground_truth = _opencode_cli_effective_config(home)
    assert _PROVIDER_ID in ground_truth.get("provider", {}), (
        "rig failure: the installed opencode CLI does not merge opencode.json into its "
        "effective global config; this test's premise does not hold for it. "
        f"Effective config: {json.dumps(ground_truth)[:800]}"
    )
    assert ground_truth.get("model") == "my-gateway/gpt-4"

    provider_ids, payload, effective = _run_session_and_get_providers(
        http_client, tmp_path, live_server, home
    )

    assert _PROVIDER_ID in provider_ids, (
        f"The provider declared in opencode.json never reached the session's spawned "
        f"`opencode serve`, which is pinned to model {effective.get('model')!r} with no such "
        f"provider.\nProviders seen by the spawned server: {provider_ids}\n"
        f"Spawned server effective config: {json.dumps(effective)[:800]}"
    )
    my_gateway = next(p for p in payload["providers"] if p.get("id") == _PROVIDER_ID)
    assert "gpt-4" in (my_gateway.get("models") or {}), (
        f"Provider {_PROVIDER_ID!r} reached the spawned server without its models map: "
        f"{json.dumps(my_gateway)[:400]}"
    )
    assert effective.get("model") == "my-gateway/gpt-4", (
        "The opencode.jsonc model pin did not survive into the spawned server's effective "
        "config, so the session would not default to the restored provider. Spawned server "
        f"model: {effective.get('model')!r}, expected 'my-gateway/gpt-4'.\n"
        f"Spawned server effective config: {json.dumps(effective)[:800]}"
    )
