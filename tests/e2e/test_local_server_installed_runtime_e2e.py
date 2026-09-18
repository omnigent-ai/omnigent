"""Background servers use the selected runtime, even inside another checkout.

Cover both importable and broken workspace packages, using isolated server state.
No LLM is needed: pytest tests/e2e/test_local_server_installed_runtime_e2e.py -v
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil

from tests.e2e.helpers import POLL_INTERVAL_S

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Allow the server's 120-second cold-start budget plus time for CLI imports.
_CLI_TIMEOUT_S = 240.0

# Keep inherited credentials and state paths out of the test server.
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
)

# Record workspace imports, then delegate to the real package so startup
# succeeds even when the server loads the wrong checkout.
_DELEGATING_CHECKOUT = """\
import json as _json
import os as _os
import sys as _sys
from pathlib import Path as _Path

with open({marker!r}, "a") as _fh:
    _fh.write(_json.dumps({{"pid": _os.getpid(), "argv": list(_sys.orig_argv)}}) + "\\n")

_real_init = _Path({real_pkg!r}) / "__init__.py"
__path__ = [{real_pkg!r}]
exec(compile(_real_init.read_text(), str(_real_init), "exec"))
__file__ = str(_real_init)
"""

_BROKEN_CHECKOUT = 'raise RuntimeError("conflicting workspace omnigent checkout was imported")\n'


def _isolated_env(home: Path) -> dict[str, str]:
    """Isolate server state and credentials; use this checkout as the runtime."""
    env = dict(os.environ)
    for key in _ENV_TO_CLEAR:
        env.pop(key, None)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    return env


def _write_conflicting_checkout(workspace: Path, init_body: str) -> None:
    """Create a workspace package that competes with the selected runtime."""
    pkg = workspace / "omnigent"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(init_body)


def _run_background_server(
    workspace: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Start a background server from the workspace and capture CLI output."""
    return subprocess.run(
        [sys.executable, "-P", "-m", "omnigent.cli", "server", "--background"],
        cwd=str(workspace),
        env=env,
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT_S,
    )


def _pidfile_path(home: Path) -> Path:
    """Return the server pidfile path under the isolated home."""
    return home / ".omnigent" / "local_server.pid"


def _read_pidfile(path: Path) -> tuple[int, int] | None:
    """Read the server PID and port, or return None for a missing or invalid file."""
    try:
        lines = path.read_text().strip().splitlines()
    except OSError:
        return None
    if len(lines) < 2:
        return None
    try:
        return int(lines[0]), int(lines[1])
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    """Check whether the process still exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _health_ok(port: int) -> bool:
    """Check whether the server returns HTTP 200 from /health."""
    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0, trust_env=False)
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


def _read_marker(marker: Path) -> list[dict[str, object]]:
    """Read the process records written when the workspace package was imported."""
    try:
        lines = marker.read_text().splitlines()
    except OSError:
        return []
    return [json.loads(line) for line in lines if line.strip()]


def _stop_pidfile_server(home: Path) -> None:
    """Stop the detached server so it does not outlive the test."""
    entry = _read_pidfile(_pidfile_path(home))
    if entry is None or not _pid_alive(entry[0]):
        return
    pid = entry[0]
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(POLL_INTERVAL_S)
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGKILL)


def test_local_server_child_ignores_conflicting_workspace_checkout(tmp_path: Path) -> None:
    """The server uses the selected runtime and keeps the workspace as its cwd."""
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    marker = tmp_path / "workspace-imports.jsonl"
    _write_conflicting_checkout(
        workspace,
        _DELEGATING_CHECKOUT.format(marker=str(marker), real_pkg=str(_REPO_ROOT / "omnigent")),
    )

    try:
        result = _run_background_server(workspace, _isolated_env(home))
        assert result.returncode == 0, (
            f"`server --background` failed (rc={result.returncode}).\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
        entry = _read_pidfile(_pidfile_path(home))
        assert entry is not None and _pid_alive(entry[0]) and _health_ok(entry[1]), (
            f"background server not healthy after a successful spawn: {entry}"
        )

        workspace_imports = _read_marker(marker)
        assert not workspace_imports, (
            "the local-server child resolved `omnigent` from the workspace "
            f"checkout instead of the installed runtime:\n{workspace_imports}"
        )
        child_cwd = Path(psutil.Process(entry[0]).cwd()).resolve()
        assert child_cwd == workspace.resolve(), (
            f"local server child did not preserve the workspace working "
            f"directory: cwd={child_cwd}, workspace={workspace.resolve()}"
        )
    finally:
        _stop_pidfile_server(home)


def test_local_server_boots_despite_broken_workspace_checkout(tmp_path: Path) -> None:
    """A broken workspace package must not prevent the selected runtime from booting."""
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    _write_conflicting_checkout(workspace, _BROKEN_CHECKOUT)

    try:
        result = _run_background_server(workspace, _isolated_env(home))
        assert result.returncode == 0, (
            f"`server --background` failed from a workspace with a broken "
            f"conflicting checkout (rc={result.returncode}).\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
        entry = _read_pidfile(_pidfile_path(home))
        assert entry is not None and _pid_alive(entry[0]) and _health_ok(entry[1]), (
            f"background server not healthy after a successful spawn: {entry}"
        )
    finally:
        _stop_pidfile_server(home)
