"""End-to-end repro: `omnigent start` against an unreachable configured server.

With `server: http://127.0.0.1:<port>` configured and nothing listening
there, `omnigent start --non-interactive` prints nothing at all while the
host daemon retries the connection, then fails ~35s later with a generic
"did not register within 30s" error that never names the server it tried
or the connection refusal, followed by a stale-host HTTP 401 hint that
cannot apply to a connection-refused failure.

This drives the real user journey: the actual `omnigent start
--non-interactive` process with the unreachable server in config, reading
its output live with per-line timestamps.

Run with::

    python -m pytest tests/e2e/test_start_unreachable_server_feedback_e2e.py -v
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# How long the user may reasonably stare at a silent terminal before the
# CLI says anything (which server it is contacting, that connection is
# being refused, or a fast failure). Well above any legitimate startup
# print, well below the 30s registration timeout the buggy build waits
# out in silence.
_FEEDBACK_DEADLINE_S = 15.0

# Registration timeout (30s) + startup grace, with headroom. A fixed
# build fails much sooner or prints progress; a hang past this is killed.
_JOURNEY_DEADLINE_S = 120.0

_LOG_TAIL_MARKER = "--- host log tail ---"


@dataclass
class _StartRun:
    port: int
    lines: list[tuple[float, str]]
    returncode: int | None
    elapsed_s: float

    @property
    def output(self) -> str:
        return "\n".join(text for _, text in self.lines)

    @property
    def first_output_s(self) -> float | None:
        return self.lines[0][0] if self.lines else None


def _free_localhost_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def unreachable_start_run(tmp_path_factory: pytest.TempPathFactory) -> _StartRun:
    """Run `omnigent start --non-interactive` once against a dead server."""
    base = tmp_path_factory.mktemp("start-unreachable")
    home = base / "home"
    config_home = base / "config"
    data_dir = base / "data"
    for directory in (home, config_home, data_dir):
        directory.mkdir()

    port = _free_localhost_port()
    (config_home / "config.yaml").write_text(f"server: http://127.0.0.1:{port}\n")

    env = {
        **os.environ,
        "HOME": str(home),
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_DATA_DIR": str(data_dir),
        # Absolute SDK paths keep the in-repo workspace packages importable
        # from the child's cwd (the isolated home, not the repo root).
        "PYTHONPATH": os.pathsep.join(
            [
                str(_REPO_ROOT),
                str(_REPO_ROOT / "sdks" / "python-client"),
                str(_REPO_ROOT / "sdks" / "ui"),
                os.environ.get("PYTHONPATH", ""),
            ]
        ).rstrip(os.pathsep),
    }
    # A proxy would intercept the connection and change the refusal into a
    # proxy-side failure; the report is about a direct connection refusal.
    for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(proxy_var, None)

    start = time.monotonic()
    proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent", "start", "--non-interactive"],
        env=env,
        cwd=str(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    lines: list[tuple[float, str]] = []

    def _read() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append((time.monotonic() - start, line.rstrip("\n")))

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    try:
        proc.wait(timeout=_JOURNEY_DEADLINE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    finally:
        reader.join(timeout=10)
        elapsed = time.monotonic() - start
        # The failed `start` is expected to reap its own daemon; sweep any
        # leftover so a bug in that cleanup can't leak into other tests.
        subprocess.run(
            [sys.executable, "-m", "omnigent", "stop", "--force"],
            env=env,
            cwd=str(home),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
    return _StartRun(port=port, lines=lines, returncode=proc.returncode, elapsed_s=elapsed)


def test_start_gives_feedback_before_registration_timeout(
    unreachable_start_run: _StartRun,
) -> None:
    """The CLI must say something well before the 30s registration timeout."""
    run = unreachable_start_run
    assert run.first_output_s is not None, (
        f"`omnigent start` produced no output at all (exit {run.returncode} "
        f"after {run.elapsed_s:.1f}s) with an unreachable configured server"
    )
    assert run.first_output_s <= _FEEDBACK_DEADLINE_S, (
        f"first output only after {run.first_output_s:.1f}s (exit "
        f"{run.returncode} at {run.elapsed_s:.1f}s): the CLI sat silent past "
        f"{_FEEDBACK_DEADLINE_S:.0f}s instead of reporting the unreachable "
        f"server http://127.0.0.1:{run.port} or failing fast"
    )


def test_start_failure_names_unreachable_server_and_omits_401_hint(
    unreachable_start_run: _StartRun,
) -> None:
    """The failure must name the server it tried and skip the 401 hint."""
    run = unreachable_start_run
    assert run.returncode not in (0, None), (
        f"`omnigent start` did not fail (exit {run.returncode}) although the "
        f"configured server http://127.0.0.1:{run.port} is unreachable; "
        f"output:\n{run.output}"
    )
    error_text = run.output.split(_LOG_TAIL_MARKER)[0]
    assert f"127.0.0.1:{run.port}" in error_text, (
        "the error never names the unreachable server it tried "
        f"(http://127.0.0.1:{run.port}); a user cannot tell what `start` was "
        f"waiting for. Error text:\n{error_text}"
    )
    assert "HTTP 401" not in run.output, (
        "the stale-host HTTP 401 hint was printed for a connection-refused "
        f"failure it cannot apply to; output:\n{run.output}"
    )
