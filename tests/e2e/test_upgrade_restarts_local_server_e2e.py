"""`omnigent upgrade` must bring the `omnigent start` server and host daemon back.

Drives the real CLI as a subprocess against a real background server + local
host daemon in an isolated HOME. The package is imported from a plain copy with
a fabricated uv-tool install layout, because `upgrade` refuses source checkouts
and editable installs; the installer command is a stand-in `uv` that advances
the recorded version. No LLM or network is needed:

    pytest tests/e2e/test_upgrade_restarts_local_server_e2e.py -v
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.e2e.helpers import POLL_INTERVAL_S

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: sh stand-in installer and signal-based cleanup"
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INSTALLED_VERSION = "0.19.0"
_AVAILABLE_VERSION = "0.19.1"
_CLI_TIMEOUT_S = 240.0
_RESTART_WINDOW_S = 10.0

_ENV_TO_CLEAR = (
    "DATABRICKS_TOKEN",
    "DATABRICKS_CONFIG_PROFILE",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "CLAUDE_CODE",
    "CODEX",
    "OMNIGENT_DATA_DIR",
    "OMNIGENT_CONFIG_HOME",
    "OMNIGENT_AUTH_ENABLED",
    "OMNIGENT_OIDC_ISSUER",
    "OMNIGENT_AUTH_PROVIDER",
    "OMNIGENT_DATABASE_URI",
    "OMNIGENT_RUNNER_TUNNEL_TOKEN",
    "OMNIGENT_INDEX_URL",
    "UV_DEFAULT_INDEX",
    "UV_INDEX_URL",
    "PIP_INDEX_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)

_SIMPLE_INDEX_PAGE = """<!DOCTYPE html><html><body>
<a href="omnigent-{v}-py3-none-any.whl">omnigent-{v}-py3-none-any.whl</a><br/>
</body></html>
"""

# Stand-in for the real installer: `uv tool upgrade omnigent` advances the
# fabricated install's recorded version (what the CLI re-probes) and the
# runtime's VERSION constant (what the restarted processes import and serve).
_FAKE_UV = """#!/bin/sh
set -e
if [ "$1 $2 $3" != "tool upgrade omnigent" ]; then
  echo "fake uv: unexpected arguments: $*" >&2
  exit 1
fi
{python} - <<'PY'
import pathlib, re
path = pathlib.Path({metadata})
path.write_text(re.sub(r"(?m)^Version: .*", "Version: {new}", path.read_text()))
version = pathlib.Path({version_py})
version.write_text(re.sub(r"(?m)^VERSION = .*", 'VERSION = "{new}"', version.read_text()))
PY
echo "Updated omnigent v{old} -> v{new}" >&2
"""


def _fabricate_uv_tool_install(root: Path) -> dict[str, str]:
    """Lay out a copied package that `omnigent upgrade` detects as a registry uv-tool install."""
    runtime = root / "runtime"
    shutil.copytree(
        _REPO_ROOT / "omnigent",
        runtime / "omnigent",
        ignore=shutil.ignore_patterns("__pycache__", "web-ui"),
    )
    dist_info = runtime / f"omnigent-{_INSTALLED_VERSION}.dist-info"
    dist_info.mkdir()
    metadata = dist_info / "METADATA"
    metadata.write_text(f"Metadata-Version: 2.1\nName: omnigent\nVersion: {_INSTALLED_VERSION}\n")
    (dist_info / "INSTALLER").write_text("uv\n")
    tool_dir = root / "uv-tools"
    (tool_dir / "omnigent").mkdir(parents=True)
    (tool_dir / "omnigent" / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "omnigent" }]\n'
    )
    bin_dir = root / "bin"
    bin_dir.mkdir()
    fake_uv = bin_dir / "uv"
    fake_uv.write_text(
        _FAKE_UV.format(
            python=shlex.quote(sys.executable),
            metadata=json.dumps(str(metadata)),
            version_py=json.dumps(str(runtime / "omnigent" / "version.py")),
            old=_INSTALLED_VERSION,
            new=_AVAILABLE_VERSION,
        )
    )
    fake_uv.chmod(0o755)
    return {
        "PYTHONPATH": str(runtime),
        "UV_TOOL_DIR": str(tool_dir),
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
    }


@contextlib.contextmanager
def _simple_index(root: Path) -> Iterator[str]:
    """Serve a loopback PEP 503 index advertising the newer release."""
    project_dir = root / "index" / "simple" / "omnigent"
    project_dir.mkdir(parents=True)
    (project_dir / "index.html").write_text(_SIMPLE_INDEX_PAGE.format(v=_AVAILABLE_VERSION))

    class _Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        lambda *a, **k: _Handler(*a, directory=str(root / "index"), **k),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/simple"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def _isolated_env(home: Path, overrides: dict[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    for key in _ENV_TO_CLEAR:
        env.pop(key, None)
    env["HOME"] = str(home)
    env.update(overrides)
    return env


def _omnigent(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-P", "-m", "omnigent.cli", *args],
        env=env,
        cwd=env["HOME"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT_S,
    )


def _server_status(env: dict[str, str]) -> dict[str, object]:
    result = _omnigent(["server", "status", "--json"], env)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _local_daemon_pid(env: dict[str, str]) -> int | None:
    result = _omnigent(["host", "status", "--json"], env)
    assert result.returncode == 0, result.stderr
    for daemon in json.loads(result.stdout)["daemons"]:
        if daemon.get("mode") == "local":
            return daemon.get("pid")
    return None


def _server_version(url: str) -> str:
    return str(httpx.get(f"{url}/api/version", timeout=2.0, trust_env=False).json()["version"])


def _health_ok(url: str) -> bool:
    try:
        return httpx.get(f"{url}/health", timeout=2.0, trust_env=False).status_code == 200
    except httpx.HTTPError:
        return False


def _recorded_pids(home: Path) -> list[int]:
    """PIDs the CLI recorded under HOME: the server pidfile and the daemon registry."""
    pids: list[int] = []
    with contextlib.suppress(OSError, ValueError, IndexError):
        pids.append(int((home / ".omnigent" / "local_server.pid").read_text().splitlines()[0]))
    for record in (home / ".omnigent" / "daemons").glob("*.json"):
        with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
            pids.append(int(json.loads(record.read_text())["pid"]))
    return pids


def _wait_for_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(POLL_INTERVAL_S)
    return False


def _terminate(pid: int) -> None:
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    if _wait_for_exit(pid, 10.0):
        return
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)
    _wait_for_exit(pid, 5.0)


def _stop_everything(home: Path, env: dict[str, str]) -> None:
    """`omnigent stop --force`, then reap whatever it left behind by recorded PID."""
    pids = _recorded_pids(home)
    with contextlib.suppress(subprocess.SubprocessError):
        _omnigent(["stop", "--force"], env)
    for pid in pids:
        _terminate(pid)


@pytest.mark.timeout(300)
def test_upgrade_restarts_running_local_server_and_host(tmp_path: Path) -> None:
    """A background server + daemon running before `upgrade` are running again after it."""
    home = tmp_path / "home"
    home.mkdir()
    overrides = _fabricate_uv_tool_install(tmp_path)
    with _simple_index(tmp_path) as index_url:
        env = _isolated_env(home, {**overrides, "OMNIGENT_INDEX_URL": index_url})
        try:
            start = _omnigent(["start", "--no-open"], env)
            assert start.returncode == 0, f"{start.stdout}\n{start.stderr}"
            before = _server_status(env)
            assert before["running"] and before["daemon_attached"], before
            url = str(before["url"])
            assert _health_ok(url)
            before_version = _server_version(url)
            before_daemon_pid = _local_daemon_pid(env)
            assert before_daemon_pid is not None

            upgrade = _omnigent(["upgrade"], env)
            assert upgrade.returncode == 0, f"{upgrade.stdout}\n{upgrade.stderr}"
            assert f"Upgraded to v{_AVAILABLE_VERSION}" in upgrade.stdout, upgrade.stdout

            deadline = time.monotonic() + _RESTART_WINDOW_S
            after = _server_status(env)
            while time.monotonic() < deadline and not (
                after["running"] and after["daemon_attached"]
            ):
                time.sleep(0.5)
                after = _server_status(env)
            assert after["running"] and after["daemon_attached"], (
                f"`omnigent upgrade` left the background server/host daemon stopped: {after}\n"
                f"upgrade output:\n{upgrade.stdout}"
            )
            assert _health_ok(str(after["url"]))
            # Both processes were replaced, not merely left running, and the new
            # server imported the upgraded install rather than the old modules.
            assert after["pid"] != before["pid"], (before["pid"], after["pid"])
            after_daemon_pid = _local_daemon_pid(env)
            assert after_daemon_pid not in (None, before_daemon_pid), (
                before_daemon_pid,
                after_daemon_pid,
            )
            after_version = _server_version(str(after["url"]))
            assert after_version == _AVAILABLE_VERSION != before_version, (
                before_version,
                after_version,
            )
        finally:
            _stop_everything(home, env)
