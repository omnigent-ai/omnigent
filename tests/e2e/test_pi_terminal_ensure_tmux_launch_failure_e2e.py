"""
A failed Pi terminal launch must keep tmux's own diagnostic in the runner log.

Spawns the real ``omnigent pi --server ''`` CLI under a pseudo-TTY (it
auto-starts a local server, host daemon and runner) with a ``tmux`` shim first
on ``PATH`` that passes the version preflight but exits 1 from ``new-session``
with its reason on stdout. The CLI must die on the terminal ensure, and the
runner log's ``tmux launch failed (rc=1): ...`` line must carry that reason
instead of an empty string. No Pi model turn runs.

Usage::

    python -m pytest tests/e2e/test_pi_terminal_ensure_tmux_launch_failure_e2e.py \\
        --timeout=420
"""

from __future__ import annotations

import contextlib
import io
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.helpers import POLL_INTERVAL_S

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]

_TMUX_DIAG_MARKER = "failing-tmux-shim-diagnostic: new-session refused by shim"
_TMUX_VERSION_LINE = "tmux 3.4"

_PTY_ROWS = 50
_PTY_COLS = 220

# Server auto-spawn + daemon + runner online + the ensure failure, on a loaded CI box.
_JOURNEY_TIMEOUT_S = 240
_LOG_SETTLE_TIMEOUT_S = 30

_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")

# Ambient credentials, runner identity, proxies and HOME-derived XDG dirs would
# route the auto-spawned local stack away from the isolated per-test dirs.
_STALE_ENV_VARS = (
    "DATABRICKS_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OMNIGENT_RUNNER_ID",
    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN",
    "RUNNER_SERVER_URL",
    "OMNIGENT_RUNNER_WORKSPACE",
    "TMUX",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "http_proxy",
    "https_proxy",
)

# Satisfies the host credential gate without contacting any provider.
_MOCK_PROVIDER_CONFIG = (
    "providers:\n"
    "  mock:\n"
    "    kind: key\n"
    "    default: pi\n"
    "    openai:\n"
    "      base_url: http://127.0.0.1:9/v1\n"
    "      api_key_ref: env:PI_TEST_API_KEY\n"
)


def _make_failing_tmux(bin_dir: Path) -> None:
    """Write a ``tmux`` shim that answers ``-V`` and fails every other command on stdout."""
    shim = bin_dir / "tmux"
    shim.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        f'  if [ "$a" = "-V" ]; then echo "{_TMUX_VERSION_LINE}"; exit 0; fi\n'
        "done\n"
        f'echo "{_TMUX_DIAG_MARKER}"\n'
        "exit 1\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)


def _journey_env(
    shim_dir: Path, config_home: Path, data_dir: Path, home_dir: Path
) -> dict[str, str]:
    """Isolated environment for the ``omnigent pi`` subprocess and everything it spawns."""
    env = dict(os.environ)
    for stale in _STALE_ENV_VARS:
        env.pop(stale, None)
    env["PATH"] = f"{shim_dir}{os.pathsep}{env['PATH']}"
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    env["OMNIGENT_DATA_DIR"] = str(data_dir)
    env["HOME"] = str(home_dir)
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["TERM"] = "xterm-256color"
    env["LINES"] = str(_PTY_ROWS)
    env["COLUMNS"] = str(_PTY_COLS)
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    env["OMNIGENT_SKIP_ONBOARD"] = "1"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    env["PI_TEST_API_KEY"] = "mock-key"
    return env


def _plain_text(raw: bytes) -> str:
    return _ANSI_RE.sub(b"", raw).decode("utf-8", "replace")


def _runner_log_text(data_dir: Path) -> str:
    log_dir = data_dir / "logs" / "runner"
    if not log_dir.is_dir():
        return ""
    return "\n".join(p.read_text(errors="replace") for p in sorted(log_dir.glob("*.log")))


def _wait_for_runner_log(data_dir: Path, needle: str, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    text = _runner_log_text(data_dir)
    while needle not in text and time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL_S)
        text = _runner_log_text(data_dir)
    return text


def _stop_local_stack(omnigent: Path, env: dict[str, str]) -> None:
    for args in (["server", "stop"], ["stop"]):
        with contextlib.suppress(Exception):
            subprocess.run([str(omnigent), *args], env=env, capture_output=True, timeout=60)


@pytest.mark.skipif(
    (_PI_REASON := cli_unavailable_reason("pi")) is not None,
    reason=f"the pi terminal-ensure journey requires a runnable 'pi' CLI; {_PI_REASON}",
)
@pytest.mark.timeout(_JOURNEY_TIMEOUT_S + 120)
def test_pi_terminal_ensure_failure_keeps_tmux_diagnostic(tmp_path: Path) -> None:
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    _make_failing_tmux(shim_dir)
    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(_MOCK_PROVIDER_CONFIG, encoding="utf-8")
    data_dir = tmp_path / "data"
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    env = _journey_env(shim_dir, config_home, data_dir, home_dir)

    omnigent = Path(sys.executable).parent / "omnigent"
    assert omnigent.is_file(), f"omnigent console script not found at {omnigent}"

    captured = io.BytesIO()
    child = pexpect.spawn(
        str(omnigent),
        ["pi", "--server", ""],
        env=env,
        encoding=None,
        dimensions=(_PTY_ROWS, _PTY_COLS),
        timeout=_JOURNEY_TIMEOUT_S,
        cwd=str(tmp_path),
    )
    child.logfile_read = captured
    try:
        outcome = child.expect(
            [rb"Pi terminal ensure failed", pexpect.EOF, pexpect.TIMEOUT],
            timeout=_JOURNEY_TIMEOUT_S,
        )
        with contextlib.suppress(Exception):
            child.expect(pexpect.EOF, timeout=15)
        cli_output = _plain_text(captured.getvalue())
        assert outcome == 0, (
            "`omnigent pi` with a launch-failing tmux never printed 'Pi terminal "
            f"ensure failed' within {_JOURNEY_TIMEOUT_S}s (outcome={outcome}). "
            f"CLI output tail:\n{cli_output[-2500:]}"
        )

        log_text = _wait_for_runner_log(data_dir, "tmux launch failed", _LOG_SETTLE_TIMEOUT_S)
        assert "Pi terminal ensure failed for session=" in log_text, (
            "Runner log never recorded 'Pi terminal ensure failed for session='. "
            f"Log tail:\n{log_text[-2500:]}"
        )
        failure_lines = [line for line in log_text.splitlines() if "tmux launch failed" in line]
        assert failure_lines, (
            f"Runner log never recorded the tmux launch failure. Log tail:\n{log_text[-2500:]}"
        )
        assert _TMUX_DIAG_MARKER in log_text, (
            "The runner log dropped the failing tmux's own diagnostic "
            f"({_TMUX_DIAG_MARKER!r}); its launch-failure lines carry an empty reason, "
            "so nobody reading the log can tell why tmux failed. Logged lines:\n"
            + "\n".join(failure_lines)
        )
    finally:
        with contextlib.suppress(Exception):
            child.close(force=True)
        _stop_local_stack(omnigent, env)
