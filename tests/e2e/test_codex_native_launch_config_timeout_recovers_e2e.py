"""E2E: a native Codex terminal launch recovers from a transient config-fetch timeout.

The runner's ``_codex_native_launch_config`` fetches the session snapshot with
``GET /v1/sessions/<id>`` (a short client timeout) to build the Codex launch
config. Under load that one read can exceed the timeout and raise
``httpx.ReadTimeout``. Historically the function re-raised it as
``RuntimeError("Could not fetch Codex launch config for '<id>'.")``, and that
single transient blip tore through every Codex-terminal entry point:

* the **launch** path (``_launch_native_terminal`` -> ``_auto_create_codex_terminal``)
  logged ``Failed to auto-create codex terminal for <id>`` and published
  ``session.status: failed``;
* the **ensure** path (``_ensure_native_terminal`` -> ``_auto_create_codex_terminal``)
  logged ``Codex terminal ensure failed for session=<id>`` and returned HTTP 500
  ``native_terminal_start_failed``;
* the next **user turn** on that session was then rejected with a durable
  ``error`` item ("Native Codex terminal failed to start").

The read is idempotent, so a bounded retry rides over one blip instead of
failing the whole launch. This test proves that behavior end to end.

It drives the REAL user journey: a real ``omnigent server`` subprocess, a real
runner subprocess bound over the tunnel, a real codex-native wrapper session.
The only injected element is the transient fault -- the runner is booted with
``_codex_native_launch_config`` wrapped so the *first* snapshot ``GET`` raises
the exact ``httpx.ReadTimeout`` the deployed stack showed, then delegates to the
real client (which succeeds). So the production ``try``/retry runs for real, and
all of the launch / ensure machinery below is unmodified product code.

On a build that never retries a transient config-fetch timeout, the first fetch
aborts the launch: ``Failed to auto-create codex terminal`` is logged and the
ensure path returns ``native_terminal_start_failed`` -- the assertions below
fail. On a build that retries the idempotent read, the launch recovers on the
second attempt: the terminal is auto-created, ``Auto-created codex terminal +
forwarder`` is logged, and the ensure path returns the terminal view -- the
assertions pass.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_codex_native_launch_config_timeout_recovers_e2e.py -v
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import httpx
import pytest

from tests._helpers.native_session import create_native_session
from tests._helpers.server_runner import server_runner

# CI shells can carry an egress proxy in the environment; every HTTP call in
# this test targets 127.0.0.1, so bypass proxy autodetection entirely.
_http = httpx.Client(trust_env=False)


# Runner bootstrap: wrap ``_codex_native_launch_config`` so only the FIRST
# snapshot GET raises the deployed ``httpx.ReadTimeout``; every later attempt
# delegates to the real client and succeeds. The REAL function body then runs
# its production fetch-and-retry: an un-retried build re-raises on the first
# timeout (launch fails); a build that retries the idempotent read recovers on
# the second attempt (launch succeeds). No other runner->server call is touched.
_RUNNER_BOOTSTRAP = """
import httpx
import omnigent.runner.native.orchestration as _orch

_orig_launch_config = _orch._codex_native_launch_config


class _FirstConfigFetchTimesOut:
    def __init__(self, real):
        self._real = real
        self._failed_once = False

    async def get(self, url, *args, **kwargs):
        if not self._failed_once:
            self._failed_once = True
            raise httpx.ReadTimeout(
                "simulated first-attempt runner->server GET /v1/sessions read timeout",
                request=httpx.Request("GET", url),
            )
        return await self._real.get(url, *args, **kwargs)


async def _launch_config_first_fetch_times_out(*, session_id, server_client):
    return await _orig_launch_config(
        session_id=session_id,
        server_client=_FirstConfigFetchTimesOut(server_client),
    )


_orch._codex_native_launch_config = _launch_config_first_fetch_times_out

from omnigent.runner._entry import main

main()
"""

_POLL_S = 1.0
# Terminal auto-create includes the pre-launch snapshot read + spec resolve, one
# retried config fetch, then the tmux terminal + forwarder wiring; generous for CI.
_LAUNCH_TIMEOUT_S = 180.0

pytestmark = [
    pytest.mark.skipif(
        shutil.which("tmux") is None,
        reason="codex-native terminals run inside tmux; tmux not installed",
    ),
    pytest.mark.skipif(
        shutil.which("codex") is None,
        reason="the recovered launch starts the codex CLI; codex not installed",
    ),
]


def test_codex_native_launch_recovers_from_transient_config_fetch_timeout(
    tmp_path: Path,
) -> None:
    """A first-attempt config-fetch ReadTimeout is retried and the launch recovers.

    Journey (the reporter's): a codex-native session is bound to a runner; the
    runner's ``GET /v1/sessions/<id>`` launch-config read times out once under
    load. On the fixed build the idempotent read is retried, the Codex terminal
    is auto-created, and opening the terminal succeeds -- so the user's terminal
    launches instead of erroring.

    :param tmp_path: Per-test temp dir (server DB, runner HOME, workspace).
    """
    # Pin the runner's process log to a known file so the launch records are
    # readable from the test without globbing ~/.omnigent/logs/runner/.
    runner_log_file = tmp_path / "runner-process.log"

    with server_runner(tmp_path) as stack:
        base_url, runner_id = stack.base_url, stack.runner_id
        stack.start_runner(
            bootstrap=_RUNNER_BOOTSTRAP,
            env={
                "OMNIGENT_PROCESS_LOG_FILE": str(runner_log_file),
                "OMNIGENT_LOG_LEVEL": "INFO",
            },
        )

        def _runner_log() -> str:
            return runner_log_file.read_text() if runner_log_file.exists() else ""

        session_id = str(create_native_session(_http, base_url, harness="codex")["session_id"])

        # ---- Launch path: binding the runner auto-creates the Codex terminal.
        # The first launch-config fetch times out; the retried read recovers and
        # the terminal is created. ----
        _http.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=_LAUNCH_TIMEOUT_S,
        ).raise_for_status()

        deadline = time.monotonic() + _LAUNCH_TIMEOUT_S
        launch_recovered = False
        while time.monotonic() < deadline:
            log = _runner_log()
            if f"Auto-created codex terminal + forwarder for session {session_id}" in log:
                launch_recovered = True
                break
            if f"Failed to auto-create codex terminal for {session_id}" in log:
                # The launch aborted on the transient timeout instead of
                # recovering -- the reported (un-retried) behavior.
                break
            time.sleep(_POLL_S)
        log = _runner_log()
        assert launch_recovered, (
            "codex terminal launch did not recover from the first-attempt config-fetch "
            f"timeout (expected 'Auto-created codex terminal + forwarder for session "
            f"{session_id}'); runner log:\n{log[-4000:]}"
        )
        assert f"Failed to auto-create codex terminal for {session_id}" not in log, (
            "launch logged an auto-create failure despite recovering; the transient "
            f"config-fetch timeout was not ridden out. runner log:\n{log[-4000:]}"
        )

        # ---- Ensure path: opening the terminal returns the created terminal
        # view, not the native_terminal_start_failed error the un-retried build
        # produced. ----
        ensure = _http.post(
            f"{base_url}/v1/sessions/{session_id}/resources/terminals",
            json={
                "terminal": "codex",
                "session_key": "main",
                "ensure_native_terminal": True,
            },
            timeout=_LAUNCH_TIMEOUT_S,
        )
        assert ensure.status_code < 400, (
            f"terminal ensure failed after launch recovery: {ensure.status_code} "
            f"{ensure.text[:500]}; runner log:\n{_runner_log()[-4000:]}"
        )
        try:
            ensure_body = ensure.json()
        except ValueError:
            ensure_body = {}
        ensure_error = ensure_body.get("error") if isinstance(ensure_body, dict) else None
        assert not (isinstance(ensure_error, dict) and ensure_error.get("code")), (
            f"ensure returned a structured error despite launch recovery: {ensure.text[:500]}"
        )
        assert f"Codex terminal ensure failed for session={session_id}" not in _runner_log(), (
            "ensure path logged a start failure despite the launch having recovered; "
            f"runner log:\n{_runner_log()[-4000:]}"
        )
