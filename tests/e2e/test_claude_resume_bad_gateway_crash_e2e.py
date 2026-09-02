"""
``omni claude --resume`` must not crash on a gateway 5xx from session listing.

Reproduces the journey from the "[Crash] ServerError: Bad Gateway" report
(OMNI-5902 / GH #6058): the user runs ``omni claude --resume`` against a
remote server whose fronting gateway answers the resume picker's session
listing (``GET /v1/sessions``) with ``502 Bad Gateway`` -- the shape a
Databricks Apps edge / reverse proxy produces while the app replica is
down or restarting.

Today the raw ``omnigent_client._errors.ServerError`` escapes
``_resolve_session_id_for_resume`` (omnigent/claude_native.py) uncaught,
so the CLI dies through the crash handler: the "ran into an issue" crash
screen, a saved crash report, and an auto-filed-bug prompt -- for what is
a transient server-side condition the user can do nothing about except
retry. The neighboring ``httpx.ConnectError`` handler in
``_run_with_remote_server`` already converts an unreachable server into
an actionable ``click.ClickException``; a 5xx from the picker deserves
the same treatment.

The test drives the real user journey under a PTY:

1. stand up a stub "remote server" whose gateway is mid-hiccup
   (``/v1/me`` answers 200 so auth preflight passes; ``/v1/sessions``
   answers 502 with a plain-text proxy body),
2. run ``omnigent claude --resume --server <stub>`` exactly as the user
   did,
3. assert the CLI surfaces an actionable error INSTEAD of the crash
   screen + raw traceback.

It FAILS on the bug (crash screen shown) and PASSES once the resume
picker's server errors are handled gracefully.

Usage::

    python -m pytest tests/e2e/test_claude_resume_bad_gateway_crash_e2e.py -v
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import stat
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pexpect = pytest.importorskip("pexpect")

# Worktree root: tests/e2e/<this file> -> parents[2]. Threaded onto the CLI
# subprocess's PYTHONPATH so it imports THIS worktree's code, not an
# editable install from a sibling checkout.
_REPO_ROOT = Path(__file__).resolve().parents[2]

_CLI_TIMEOUT_S = 120

# The crash handler's user-visible signature (omnigent/crash_ui.py renders
# "<App> ran into an issue."; the handler saves "crash report"). Neither may
# appear for a server-side 5xx on this journey.
_CRASH_SCREEN_MARKER = "ran into an issue"
_CRASH_REPORT_MARKER = "crash report"
_RAW_TRACEBACK_MARKER = "Traceback (most recent call last)"

# Env vars that, leaked from a parent omnigent/runner process, would
# mis-route the daemon or runner spawned by the CLI under test.
_STALE_ENV_VARS = (
    "OMNIGENT_RUNNER_ID",
    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN",
    "OMNIGENT_RUNNER_TUNNEL_TOKEN",
    "OMNIGENT_RUNNER_PARENT_PID",
    "OMNIGENT_RUNNER_ISOLATE_SESSION",
    "OMNIGENT_RUNNER_WORKSPACE",
    "OMNIGENT_HOST_ID",
    "OMNIGENT_HOST_TOKEN",
    "OMNIGENT_HOST_NAME",
    "RUNNER_SERVER_URL",
    "OMNIGENT_REMOTE_AUTH_TOKEN",
)


class _HiccupingGatewayHandler(BaseHTTPRequestHandler):
    """A remote Omnigent server whose gateway 502s the session listing.

    ``/v1/me`` answers 200 (the auth preflight passes -- the user IS signed
    in); ``/v1/sessions`` answers 502 with a plain-text proxy body, the
    shape a reverse proxy emits while its upstream app replica is down.
    """

    # Populated by the fixture; records paths so the test can assert the
    # journey actually reached the session listing (guards against a
    # vacuous pass when the CLI fails before contacting the server).
    seen_paths: list[str] = []

    def _send(self, code: int, body: str, ctype: str = "text/html") -> None:
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        type(self).seen_paths.append(path)
        if path.startswith("/v1/me"):
            self._send(200, json.dumps({"user_id": "tester"}), "application/json")
        elif path.startswith("/v1/sessions"):
            self._send(502, "Bad Gateway")
        elif path.startswith(("/health", "/v1/info", "/api/version")):
            self._send(200, json.dumps({"status": "ok"}), "application/json")
        else:
            self._send(404, "not found", "text/plain")

    do_POST = do_GET
    do_PATCH = do_GET

    def log_message(self, *args: object) -> None:  # quiet
        pass


@pytest.fixture()
def hiccuping_gateway() -> tuple[str, list[str]]:
    """Serve the stub gateway on a free loopback port for one test."""
    _HiccupingGatewayHandler.seen_paths = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _HiccupingGatewayHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_address[1]}",
            _HiccupingGatewayHandler.seen_paths,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _make_fake_claude(bin_dir: Path) -> None:
    """Put a fake ``claude`` on PATH so the wrapper's tool preflight passes.

    The crash under test happens before Claude ever launches, so the
    executable's behavior is irrelevant -- only its presence is checked
    (``_preflight_local_tools``).
    """
    fake = bin_dir / "claude"
    fake.write_text("#!/bin/sh\nexit 0\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _cli_env(tmp_path: Path, bin_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    for var in _STALE_ENV_VARS:
        env.pop(var, None)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(_REPO_ROOT), env.get("PYTHONPATH")) if p)
    # Isolate config/state and keep the daemon this run spawns findable
    # for teardown.
    env["OMNIGENT_DATA_DIR"] = str(tmp_path / "data")
    env["OMNIGENT_CONFIG_HOME"] = str(tmp_path / "config")
    # The stub is loopback; never route it through a corporate proxy.
    env["NO_PROXY"] = "*"
    env["no_proxy"] = "*"
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    # Deterministic, uncolored output for assertions.
    env["NO_COLOR"] = "1"
    env.pop("FORCE_COLOR", None)
    return env


def _kill_spawned_daemon(data_dir: Path) -> None:
    """Tear down the host daemon the CLI run spawned for the stub target."""
    pid_file = data_dir / "host.pid"
    if not pid_file.exists():
        return
    with contextlib.suppress(ValueError, OSError):
        pid = int(pid_file.read_text().strip().splitlines()[0])
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                return
            time.sleep(0.1)
        os.kill(pid, signal.SIGKILL)


def test_claude_resume_survives_bad_gateway_on_session_listing(
    hiccuping_gateway: tuple[str, list[str]],
    tmp_path: Path,
) -> None:
    """A 502 from the resume picker's session listing must not crash the CLI.

    Journey (from the bug report): the user is connected to a remote
    server; its gateway hiccups (upstream down -> 502 on /v1/sessions);
    the user runs ``omni claude --resume``. Expected: an actionable error
    (retry / check the server), like the unreachable-server path already
    produces. Bug: the raw ServerError escapes and the crash handler
    renders the crash screen and offers to auto-file a bug.
    """
    base_url, seen_paths = hiccuping_gateway
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _make_fake_claude(bin_dir)
    env = _cli_env(tmp_path, bin_dir)

    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent", "claude", "--resume", "--server", base_url],
        cwd=str(_REPO_ROOT),
        env=env,
        encoding="utf-8",
        timeout=_CLI_TIMEOUT_S,
        dimensions=(60, 220),
    )
    try:
        # Drain until exit, answering the crash handler's interactive
        # "file a GitHub issue? [Y/n]" prompt (present only on the buggy
        # path) so the run never hangs on it.
        output_parts: list[str] = []
        while True:
            index = child.expect(
                [r"\[Y/n\]", pexpect.EOF, pexpect.TIMEOUT], timeout=_CLI_TIMEOUT_S
            )
            output_parts.append(child.before or "")
            if index == 0:
                output_parts.append(child.after or "")
                child.sendline("n")
                continue
            if index == 1:
                break
            pytest.fail(
                "CLI did not exit within "
                f"{_CLI_TIMEOUT_S}s; output so far:\n{''.join(output_parts)}"
            )
        child.close()
        output = "".join(output_parts)
        exit_status = child.exitstatus
    finally:
        with contextlib.suppress(Exception):
            if child.isalive():
                child.kill(signal.SIGKILL)
        _kill_spawned_daemon(tmp_path / "data")

    # Vacuous-pass guard: the journey must have reached the resume
    # picker's session listing on the stub server.
    assert any(p.startswith("/v1/sessions") for p in seen_paths), (
        f"CLI never requested /v1/sessions from the stub server "
        f"(saw {seen_paths!r}); the journey did not reach the resume "
        f"picker. Output:\n{output}"
    )

    # The bug: the raw ServerError escapes to the crash handler. A
    # server-side 5xx on this journey must never render the crash screen,
    # save a crash report, or dump the raw traceback.
    lowered = output.lower()
    assert _CRASH_SCREEN_MARKER not in lowered, (
        "`omni claude --resume` rendered the crash screen for a gateway "
        f"502 on session listing (transient server-side condition). "
        f"Output:\n{output}"
    )
    assert _CRASH_REPORT_MARKER not in lowered, (
        "`omni claude --resume` saved a crash report for a gateway 502 "
        f"on session listing. Output:\n{output}"
    )
    assert _RAW_TRACEBACK_MARKER not in output, (
        "`omni claude --resume` dumped a raw traceback for a gateway 502 "
        f"on session listing. Output:\n{output}"
    )

    # The fix's contract: an actionable, retryable error message that
    # names the failure — not merely the absence of the crash screen.
    assert "could not list sessions" in lowered, (
        "`omni claude --resume` did not print the actionable session-"
        f"listing error for a gateway 502. Output:\n{output}"
    )
    assert "retry" in lowered, (
        f"`omni claude --resume`'s gateway-502 error lacks retry guidance. Output:\n{output}"
    )
    assert exit_status not in (0, None), (
        "`omni claude --resume` must exit non-zero when the session "
        f"listing fails (got {exit_status!r}). Output:\n{output}"
    )
