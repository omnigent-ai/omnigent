"""Launching several native codex sessions must not open a browser tab per session.

Each native-pane launch runs the configured ``codex-native`` launcher. A
Databricks ``dbcert`` launcher opens ``$BROWSER`` for SSO, so a pane that
inherits the runner's opener opens one tab per session. This rig points
``harness.codex-native.command`` at a stub launcher that records every
``$BROWSER`` call, brings up several sessions, and asserts nothing opened.
"""

from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.e2e_ui.conftest import _create_native_codex_session, _find_free_port

_REPO_ROOT = Path(__file__).resolve().parents[3]
_HEALTH_TIMEOUT_S = 90.0
_LAUNCH_TIMEOUT_S = 120.0
_OPENER_QUIET_S = 5.0
_OPENER_SETTLE_TIMEOUT_S = 60.0
_SESSION_COUNT = 3


def _count_lines(path: Path) -> int:
    try:
        with path.open() as lines:
            return sum(1 for _ in lines)
    except FileNotFoundError:
        return 0


def _settled_line_count(path: Path, *, quiet_s: float, timeout_s: float) -> int:
    """Return the line count once it has stayed unchanged for ``quiet_s``."""
    deadline = time.monotonic() + timeout_s
    count = _count_lines(path)
    quiet_since = time.monotonic()
    while time.monotonic() < deadline:
        time.sleep(0.5)
        current = _count_lines(path)
        if current != count:
            count, quiet_since = current, time.monotonic()
        elif time.monotonic() - quiet_since >= quiet_s:
            break
    return count


def _write_script(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def dbcert_browser_rig(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, object]]:
    """Run a server and runner whose codex-native launcher stub records each
    launch and each attempted ``$BROWSER`` open."""
    # Loopback traffic from this process and its children must bypass the CI
    # egress proxy.
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(
            var, ",".join(filter(None, [os.environ.get(var, ""), "127.0.0.1,localhost"]))
        )

    work = tmp_path_factory.mktemp("dbcert_browser")
    config_home = work / "config-home"
    codex_home = work / "codex-home"
    home_dir = work / "home"
    state_dir = work / "codex-native-state"
    artifacts = work / "artifacts"
    for path in (config_home, codex_home, home_dir, state_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)

    opener_log = work / "opener.log"
    launch_log = work / "launch.log"
    opener = work / "open-browser.sh"
    launcher = work / "launcher.sh"

    _write_script(
        opener,
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{opener_log}"\n',
    )
    # Mimic dbcert: open ``$BROWSER`` on an SSO address, print the sign-in
    # prompt the runner surfaces, then wait so the runner does not relaunch.
    _write_script(
        launcher,
        "#!/usr/bin/env bash\n"
        "set -u\n"
        f'printf "launch %s\\n" "$$" >> "{launch_log}"\n'
        'url="https://databricks.okta.com/oauth2/v1/authorize?client_id=omni-dbcert&state=${RANDOM}${RANDOM}"\n'
        'if [ -n "${BROWSER:-}" ]; then "$BROWSER" "$url" || true; fi\n'
        'echo "dbcert: Logging in via SSO..."\n'
        'echo "If the browser does not open automatically, open this URL:"\n'
        'echo "$url"\n'
        'echo "waiting for sign-in..."\n'
        "while true; do sleep 1; done\n",
    )

    # harness.codex-native.command => our launcher (read fresh per pane launch).
    (config_home / "config.yaml").write_text(
        "harness:\n  codex-native:\n    command: " + str(launcher) + "\n"
    )

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)

    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    shared_env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
        "CODEX_HOME": str(codex_home),
        "HOME": str(home_dir),
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        # The user's URL opener, which native panes inherit from the runner.
        "BROWSER": str(opener),
    }

    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    with (
        httpx.Client(trust_env=False) as client,
        (work / "server.log").open("w") as server_log,
        (work / "runner.log").open("w") as runner_log,
    ):
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
                stdout=server_log,
                stderr=subprocess.STDOUT,
                cwd=str(_REPO_ROOT),
            )
            runner_proc = subprocess.Popen(
                [sys.executable, "-m", "omnigent.runner._entry"],
                env=runner_env,
                stdout=runner_log,
                stderr=subprocess.STDOUT,
                cwd=str(_REPO_ROOT),
            )

            deadline = time.monotonic() + _HEALTH_TIMEOUT_S
            online = False
            while time.monotonic() < deadline:
                if server_proc.poll() is not None or runner_proc.poll() is not None:
                    break
                try:
                    if client.get(f"{base_url}/health", timeout=2).status_code == 200:
                        status = client.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                        if status.status_code == 200 and status.json().get("online"):
                            online = True
                            break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            if not online:
                raise RuntimeError(
                    "dbcert browser rig did not come online within "
                    f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n"
                    f"{(work / 'server.log').read_text()[-3000:]}\nRunner log:\n"
                    f"{(work / 'runner.log').read_text()[-3000:]}"
                )

            yield {
                "base_url": base_url,
                "runner_id": runner_id,
                "opener_log": opener_log,
                "launch_log": launch_log,
            }
        finally:
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


@pytest.mark.timeout(300)
def test_bringing_up_many_native_sessions_opens_no_browser_tab(
    dbcert_browser_rig: dict[str, object],
) -> None:
    base_url = str(dbcert_browser_rig["base_url"])
    runner_id = str(dbcert_browser_rig["runner_id"])
    opener_log = dbcert_browser_rig["opener_log"]
    launch_log = dbcert_browser_rig["launch_log"]
    assert isinstance(opener_log, Path) and isinstance(launch_log, Path)

    for _ in range(_SESSION_COUNT):
        _create_native_codex_session(base_url, runner_id)

    # All panes launch on both the buggy and fixed builds; wait for that.
    deadline = time.monotonic() + _LAUNCH_TIMEOUT_S
    while time.monotonic() < deadline and _count_lines(launch_log) < _SESSION_COUNT:
        time.sleep(1)
    launches = _count_lines(launch_log)
    assert launches >= _SESSION_COUNT, (
        f"only {launches}/{_SESSION_COUNT} native panes launched; rig setup failed"
    )

    # The stub opens the browser right after logging its launch; wait until the
    # opener log has gone quiet so every automatic open is counted.
    browser_opens = _settled_line_count(
        opener_log, quiet_s=_OPENER_QUIET_S, timeout_s=_OPENER_SETTLE_TIMEOUT_S
    )
    assert browser_opens == 0, (
        f"bringing up {_SESSION_COUNT} native codex sessions opened {browser_opens} "
        "browser tabs (one per session); sign-in must open only from the chat "
        f"sign-in card. opener.log:\n{opener_log.read_text()}"
    )
