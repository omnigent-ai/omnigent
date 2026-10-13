"""E2E: Codex starts when launch-metadata HTTP reads fail.

With a complete initialization envelope, every config GET raises ReadTimeout:
the terminal must launch without attempting that callback. Without an
envelope, the first GET raises ReadTimeout and the next uses the real server:
the legacy-server path must retry and launch successfully.

Both cases start an isolated real server, runner, Codex app-server and TUI,
then ensure the terminal through the public API. No model turn is requested.

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


# Fault only the launch-config loader's HTTP client. Other runner requests
# still use the real server, including initialization and terminal ensure.
_RUNNER_BOOTSTRAP = """
import httpx
import omnigent.runner.native.orchestration as _orch

_orig_launch_config = _orch._codex_native_launch_config
_USE_ENVELOPE = __USE_ENVELOPE__


class _ConfigFetchTimesOut:
    def __init__(self, real):
        self._real = real
        self._failed_once = False

    async def get(self, url, *args, **kwargs):
        if _USE_ENVELOPE or not self._failed_once:
            self._failed_once = True
            raise httpx.ReadTimeout(
                "simulated runner->server GET /v1/sessions read timeout",
                request=httpx.Request("GET", url),
            )
        return await self._real.get(url, *args, **kwargs)


async def _launch_config_with_fault(*, session_id, server_client, session_init=None):
    kwargs = {
        "session_id": session_id,
        "server_client": _ConfigFetchTimesOut(server_client),
    }
    if _USE_ENVELOPE:
        assert session_init is not None, "test server must supply initialization metadata"
        kwargs["session_init"] = session_init
    return await _orig_launch_config(**kwargs)


_orch._codex_native_launch_config = _launch_config_with_fault

from omnigent.runner._entry import main

main()
"""

_POLL_S = 1.0
# Allow time for spec resolution, legacy retries, and terminal/forwarder startup.
_LAUNCH_TIMEOUT_S = 180.0

pytestmark = [
    pytest.mark.skipif(
        shutil.which("tmux") is None,
        reason="codex-native terminals run inside tmux; tmux not installed",
    ),
    pytest.mark.skipif(
        shutil.which("codex") is None,
        reason="the launch starts the codex CLI; codex not installed",
    ),
]


@pytest.mark.parametrize("use_envelope", [False, True], ids=["legacy", "envelope"])
def test_codex_native_launch_handles_config_fetch_timeouts(
    tmp_path: Path,
    use_envelope: bool,
) -> None:
    """Initialization avoids failing reads; legacy metadata reads still retry."""
    # Pin the runner's process log to a known file so the launch records are
    # readable from the test without globbing ~/.omnigent/logs/runner/.
    runner_log_file = tmp_path / "runner-process.log"

    with server_runner(tmp_path) as stack:
        base_url, runner_id = stack.base_url, stack.runner_id
        stack.start_runner(
            bootstrap=_RUNNER_BOOTSTRAP.replace("__USE_ENVELOPE__", str(use_envelope)),
            env={
                "OMNIGENT_PROCESS_LOG_FILE": str(runner_log_file),
                "OMNIGENT_LOG_LEVEL": "INFO",
            },
        )

        def _runner_log() -> str:
            return runner_log_file.read_text() if runner_log_file.exists() else ""

        session_id = str(create_native_session(_http, base_url, harness="codex")["session_id"])

        # Binding the runner must create a terminal despite the injected fault.
        _http.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=_LAUNCH_TIMEOUT_S,
        ).raise_for_status()

        deadline = time.monotonic() + _LAUNCH_TIMEOUT_S
        launched = False
        while time.monotonic() < deadline:
            log = _runner_log()
            if f"Auto-created codex terminal + forwarder for session {session_id}" in log:
                launched = True
                break
            if f"Failed to auto-create codex terminal for {session_id}" in log:
                break
            time.sleep(_POLL_S)
        log = _runner_log()
        assert launched, (
            "codex terminal did not launch with config-fetch faults "
            f"(expected 'Auto-created codex terminal + forwarder for session "
            f"{session_id}'); runner log:\n{log[-4000:]}"
        )
        assert f"Failed to auto-create codex terminal for {session_id}" not in log, (
            f"launch logged an auto-create failure; runner log:\n{log[-4000:]}"
        )
        retried = "Transient Codex launch-config fetch error" in log
        assert retried == (not use_envelope), log[-4000:]

        # Opening the terminal must return the resource created during startup.
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
            f"terminal ensure failed after launch: {ensure.status_code} "
            f"{ensure.text[:500]}; runner log:\n{_runner_log()[-4000:]}"
        )
        try:
            ensure_body = ensure.json()
        except ValueError:
            ensure_body = {}
        ensure_error = ensure_body.get("error") if isinstance(ensure_body, dict) else None
        assert not (isinstance(ensure_error, dict) and ensure_error.get("code")), (
            f"ensure returned a structured error after launch: {ensure.text[:500]}"
        )
        assert f"Codex terminal ensure failed for session={session_id}" not in _runner_log(), (
            "ensure path logged a start failure after successful launch; "
            f"runner log:\n{_runner_log()[-4000:]}"
        )
