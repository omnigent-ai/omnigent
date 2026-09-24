"""UI journey: Stop on a wedged-but-online runner must not hang for minutes.

The server forwards the composer's Stop click to the session's runner as
``POST /v1/sessions/{id}/events`` (``interrupt``) with ``timeout=5.0``. That
forward rides the runner's WebSocket tunnel, whose transport must honor the
httpx timeout: when the runner process is wedged (alive, tunnel registered,
answering nothing), the user's Stop request has to come back on the order of
that timeout instead of hanging until the tunnel's ping liveness (90s+)
finally drops the runner.

Journey:

1. create a session on a dedicated runner (mock LLM, no credentials),
2. send a message the mock LLM holds open — the agent looks stuck,
3. wedge the runner with ``SIGSTOP`` (it stays online but answers nothing),
4. click the Interrupt (Stop) control and measure when its
   ``POST /v1/sessions/{id}/events`` response lands.

On a build with the bug, step 4 FAILS: the response arrives only when the
tunnel is declared dead (~2 minutes), not within the forward's timeout.

``tests/e2e/test_ws_tunnel_wedged_runner_timeouts.py`` measures the same defect
(plus the runner-stream relay's dead-stream detection) at the HTTP level::

    pytest tests/e2e_ui/chat/test_stop_wedged_runner.py
"""

from __future__ import annotations

import contextlib
import os
import secrets
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.e2e_ui.conftest import (
    _create_bundled_session,
    configure_mock_llm,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]

_RUNNER_ONLINE_TIMEOUT_S = 30.0
# The interrupt forward passes timeout=5.0. The bound sits far above it and
# far below the ~90s+ tunnel ping death the buggy build waits for instead.
_INTERRUPT_RETURN_BOUND_S = 30.0
# Keep observing past the bound so the failure message reports how long the
# hang actually lasted (and the recording shows the sustained stuck state).
_INTERRUPT_OBSERVE_CEILING_S = 60.0

_WEDGED_AGENT_YAML = """\
spec_version: 1
name: wedged_stop_probe
prompt: |
  You are a terse smoke-test assistant. Answer in one short sentence.
executor:
  model: {model}
  config:
    harness: openai-agents
"""


@pytest.fixture
def wedged_probe_runner(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, subprocess.Popen[bytes]]]:
    """Spawn a dedicated runner this test may freely wedge with SIGSTOP.

    The shared ``live_server`` runner is left alone: wedging it would stall
    every other test bound to it. Teardown resumes the process before
    terminating so a stopped runner cannot outlive the test.

    :returns: ``(runner_id, process)`` — bind sessions to the id, wedge the
        process.
    """
    from omnigent.runner.identity import token_bound_runner_id

    runner_tmp = tmp_path_factory.mktemp("wedged_probe_runner")
    log_path = runner_tmp / "runner.log"
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": live_server,
        # Route the openai-agents harness to the mock LLM server so the held
        # turn needs no real provider credentials.
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
    }
    log_handle = open(log_path, "w")  # noqa: SIM115 — fd dup'd into child; closed below
    proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    log_handle.close()

    deadline = time.monotonic() + _RUNNER_ONLINE_TIMEOUT_S
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"wedged-probe runner exited early (code {proc.returncode}); "
                f"log:\n{log_path.read_text()[-3000:]}"
            )
        try:
            resp = httpx.get(f"{live_server}/v1/runners/{runner_id}/status", timeout=2)
            if resp.status_code == 200 and resp.json().get("online") is True:
                ready = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    if not ready:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        raise RuntimeError(
            f"wedged-probe runner did not register within "
            f"{_RUNNER_ONLINE_TIMEOUT_S:.0f}s; log:\n{log_path.read_text()[-3000:]}"
        )

    try:
        yield runner_id, proc
    finally:
        if proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(proc.pid, signal.SIGCONT)
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_stop_click_on_wedged_runner_returns_within_timeout(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    wedged_probe_runner: tuple[str, subprocess.Popen[bytes]],
) -> None:
    """The Stop click's request must respect the forward's timeout."""
    runner_id, runner_proc = wedged_probe_runner
    model = f"wedged-stop-probe-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "WEDGED_RUNNER_HOLD", "block": True}],
        key=model,
    )
    session_id = _create_bundled_session(
        live_server, runner_id, _WEDGED_AGENT_YAML.format(model=model)
    )
    runner_stopped = False
    try:
        page.goto(f"{live_server}/c/{session_id}")
        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)
        composer.fill("Summarize the workspace in one sentence.")
        page.get_by_role("button", name="Send", exact=True).click()

        # The turn is genuinely in flight once the runner's LLM call is
        # parked on the mock gate — the moment a user sees a stuck agent.
        gate_deadline = time.monotonic() + 60.0
        gate_pending = False
        while time.monotonic() < gate_deadline:
            g = httpx.get(f"{mock_llm_server_url}/gate/pending", timeout=5.0)
            if g.status_code == 200 and g.json().get("pending") is True:
                gate_pending = True
                break
            time.sleep(0.25)
        assert gate_pending, "The held turn never reached the mock LLM (no pending gate)"

        interrupt_button = page.get_by_role("button", name="Interrupt", exact=True)
        expect(interrupt_button).to_be_visible(timeout=30_000)

        # Wedge the runner: its tunnel stays registered (online) but nothing
        # is answered — a hung runner process, not a disconnected one.
        os.kill(runner_proc.pid, signal.SIGSTOP)
        runner_stopped = True
        online = httpx.get(f"{live_server}/v1/runners/{runner_id}/status", timeout=5.0)
        assert online.status_code == 200 and online.json().get("online") is True, (
            "Precondition failed: the wedged runner should still be online"
        )

        def _is_interrupt_response(response: object) -> bool:
            try:
                request = response.request  # type: ignore[attr-defined]
                return (
                    str(response.url).endswith(f"/v1/sessions/{session_id}/events")  # type: ignore[attr-defined]
                    and request.method == "POST"
                    and "interrupt" in (request.post_data or "")
                )
            except Exception:  # noqa: BLE001 — a torn-down request must not kill the wait
                return False

        started = time.monotonic()
        interrupt_elapsed: float | None = None
        try:
            with page.expect_response(
                _is_interrupt_response,
                timeout=_INTERRUPT_OBSERVE_CEILING_S * 1_000,
            ) as response_info:
                interrupt_button.click()
            _ = response_info.value
            interrupt_elapsed = time.monotonic() - started
        except PlaywrightTimeoutError:
            interrupt_elapsed = None

        # A beat so the recording ends on the post-Stop UI state.
        page.wait_for_timeout(1_500)

        took = (
            f"{interrupt_elapsed:.1f}s"
            if interrupt_elapsed is not None
            else f"no response within {_INTERRUPT_OBSERVE_CEILING_S:.0f}s"
        )
        assert (
            interrupt_elapsed is not None and interrupt_elapsed < _INTERRUPT_RETURN_BOUND_S
        ), (
            "Stop on a wedged-but-online runner hung past its forward timeout: "
            f"the interrupt request took {took} (expected < "
            f"{_INTERRUPT_RETURN_BOUND_S:.0f}s; the 5s forward timeout is a "
            "no-op, so the request is freed only when the tunnel's ping "
            "liveness drops the runner)"
        )
    finally:
        if runner_stopped and runner_proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(runner_proc.pid, signal.SIGCONT)
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5.0)
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
