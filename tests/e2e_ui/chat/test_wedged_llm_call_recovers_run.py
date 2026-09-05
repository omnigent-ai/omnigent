"""A transiently wedged LLM call must not hard-stop the whole run.

Reproduces the reported failure: an agent working through a multi-step task
runs several iterations of real progress, then one LLM call wedges — the
stream opens (or the request hangs) and emits nothing for the whole idle
window. The harness idle watchdog fires and the run stops completely with::

    Error · execution · RuntimeError
    turn exceeded the 600s harness idle watchdog (run_turn emitted no
    events for 600s; likely a wedged LLM or tool call)

All progress stops; the user must notice and manually re-prompt. The desired
behavior is that a single wedged LLM call is recovered (the wedged call is
abandoned and retried, or the failure is classified retryable so the platform
restarts the turn) and the run continues to completion.

Journey (the reported 600 s idle window is scaled to 10 s via the product's
own ``HARNESS_TURN_TIMEOUT_S`` env knob on a dedicated runner — the same
time-scaling ``test_absolute_watchdog_spares_active_turn.py`` uses — so the
reproduction runs in seconds while exercising the identical code path in
``omnigent/runtime/harnesses/_scaffold.py``):

1. start a session on a scaffolded-harness agent (openai-agents, driven by
   the mock LLM),
2. send a message that starts a multi-step task: the agent completes two
   tool-call iterations of real progress,
3. the next LLM call wedges — the mock holds the request open on its gate
   and emits nothing past the idle window,
4. observe: the turn dies with the "harness idle watchdog" RuntimeError, the
   chat shows the error pill, and the run never resumes (the wedged call is
   not retried; the queued fallback reply is never fetched).

On a buggy build this test FAILS at the ``watchdog_error is None`` assertion
(the reproduction); after a fix the same journey recovers from the wedged
call and completes, and the test passes.

Run::

    pytest tests/e2e_ui/chat/test_wedged_llm_call_recovers_run.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, set_fallback_mock_llm

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Scaled-down idle watchdog window (prod default: 600 s). The wedged LLM
# call below emits nothing for far longer than this, so the watchdog fires
# in seconds instead of ten minutes.
_IDLE_TIMEOUT_S = 10
# Absolute ceiling kept far above the journey so only the idle watchdog can
# trip — the point of the bug is a WEDGED call, not a long turn.
_ABSOLUTE_TIMEOUT_S = 600

# Seconds for the dedicated runner to tunnel into the shared server.
_RUNNER_ONLINE_TIMEOUT_S = 30.0

# Iterations of real progress before the wedge — the run visibly works
# through the task first, exactly as reported.
_PROGRESS_ROUNDS = 2

# The sentinel the post-recovery assistant reply carries; its presence in
# the transcript is the "run survived the wedged call" signal. On a buggy
# build the run hard-stops and this reply is never fetched from the mock.
_DONE_SENTINEL = "RUN_RECOVERED_AND_COMPLETED_SENTINEL"

# Ceiling for the whole turn to settle (recover-and-complete or fail).
# The wedge trips the 10 s idle window ~15-25 s into the turn including
# first-turn harness boot; recovery adds one more LLM round-trip.
_TURN_SETTLE_TIMEOUT_S = 120.0

_AGENT_YAML = """\
name: {name}
prompt: |
  You are a deterministic test assistant working through a multi-step
  task. You run each step with a shell command and report when done.

executor:
  model: {model}
  harness: openai-agents

os_env:
  type: caller_process
  cwd: {cwd}
  sandbox:
    type: none
"""


def _agent_bundle(name: str, model: str, cwd: str) -> bytes:
    """Gzip-tar the inline agent YAML for multipart upload."""
    yaml_text = _AGENT_YAML.format(name=name, model=model, cwd=cwd)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo(name=f"{name}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture(scope="module")
def short_idle_watchdog_runner(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """Spawn a dedicated runner whose harness subprocesses get a 10 s idle window.

    The harness scaffold reads ``HARNESS_TURN_TIMEOUT_S`` /
    ``HARNESS_TURN_ABSOLUTE_TIMEOUT_S`` from its process environment at import
    (``omnigent/runtime/harnesses/_scaffold.py``), and the runner's
    ``_build_harness_spawn_env`` passes the runner's own environment through
    to every spawned harness. A dedicated runner (same pattern as
    ``test_absolute_watchdog_spares_active_turn.py``) keeps the override out
    of the shared ``live_server`` runner and out of ``os.environ``.

    Yields the runner id to bind sessions to.
    """
    from omnigent.runner.identity import token_bound_runner_id

    runner_tmp = tmp_path_factory.mktemp("idle_watchdog_runner")
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
        # Route the openai-agents harness at the mock LLM server.
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
        # The reproduction's time scaling: prod's 600 s idle window becomes
        # 10 s; the absolute ceiling stays far above the journey.
        "HARNESS_TURN_TIMEOUT_S": str(_IDLE_TIMEOUT_S),
        "HARNESS_TURN_ABSOLUTE_TIMEOUT_S": str(_ABSOLUTE_TIMEOUT_S),
        # A fresh, empty config home: an ambient OMNIGENT_CONFIG_HOME (e.g.
        # a CI harness config with env-ref'd gateway credentials) would make
        # turn setup fail before the journey starts.
        "OMNIGENT_CONFIG_HOME": str(runner_tmp / "config-home"),
    }
    (runner_tmp / "config-home").mkdir(exist_ok=True)
    log_handle = open(log_path, "w")  # noqa: SIM115 — fd dup'd into child; closed below
    proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    log_handle.close()  # child holds its own dup of the fd

    deadline = time.monotonic() + _RUNNER_ONLINE_TIMEOUT_S
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"short-idle-watchdog runner exited early (code {proc.returncode}); "
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
            f"short-idle-watchdog runner did not register within "
            f"{_RUNNER_ONLINE_TIMEOUT_S:.0f}s; log:\n{log_path.read_text()[-3000:]}"
        )

    try:
        yield runner_id
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture
def wedged_turn_session(
    live_server: str,
    short_idle_watchdog_runner: str,
    mock_llm_server_url: str,
) -> Iterator[tuple[str, str, str]]:
    """Create a session on the short-idle-window runner; yield (base, sid, model)."""
    ws = Path(tempfile.mkdtemp(prefix="omnigent-e2e-idle-watchdog-"))
    name = f"wedge_probe_{uuid.uuid4().hex[:8]}"
    model = f"wedge-probe-{uuid.uuid4().hex[:8]}"

    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={
            "bundle": (
                "agent.tar.gz",
                _agent_bundle(name, model, str(ws)),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    try:
        httpx.patch(
            f"{live_server}/v1/sessions/{session_id}",
            json={"runner_id": short_idle_watchdog_runner},
            timeout=10.0,
        ).raise_for_status()

        # Wait for the runner-backed environment so the turn's sys_os_shell
        # tool calls have a filesystem to run in.
        env_resp = httpx.get(
            f"{live_server}/v1/sessions/{session_id}/resources/environments/default",
            timeout=10.0,
        )
        env_resp.raise_for_status()

        yield (live_server, session_id, model)
    finally:
        # Never leave the mock's gate holding the wedged request — a stuck
        # request outlives the test and wedges the dedicated runner's
        # teardown for the next one.
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{mock_llm_server_url}/gate/release", timeout=5.0)
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        shutil.rmtree(ws, ignore_errors=True)


def _queue_progress_then_wedge(mock_url: str, model: str) -> None:
    """Script iterations of real progress, then a wedged LLM call.

    The first rounds are ordinary tool calls — the harness emits real
    (non-heartbeat) events, so the idle watchdog is healthy and resetting.
    The next LLM request then blocks on the mock's gate and emits nothing:
    a wedged LLM call that outlasts the whole idle window. The fallback
    reply carries the completion sentinel — on a build that recovers the
    wedged call (abandon + retry), the retried request drains the queue to
    the fallback and the run finishes; on a buggy build the run hard-stops
    and the fallback is never fetched.
    """
    responses: list[dict[str, object]] = [
        {
            "tool_calls": [
                {
                    "call_id": f"call_iteration_{step}",
                    "name": "sys_os_shell",
                    "arguments": json.dumps({"command": f'echo "iteration {step} done"'}),
                }
            ]
        }
        for step in range(1, _PROGRESS_ROUNDS + 1)
    ]
    # The wedge: the request is accepted, then held open on the gate,
    # emitting no events. Never released during the test.
    responses.append({"block": True, "text": "never delivered"})
    configure_mock_llm(mock_url, responses, key=model)
    set_fallback_mock_llm(mock_url, model, _DONE_SENTINEL)


def _poll_turn_outcome(base_url: str, session_id: str) -> tuple[bool, str | None, int]:
    """Poll the transcript until the turn settles.

    :returns: ``(completed, watchdog_error_message, function_call_count)`` —
        ``completed`` when the final assistant sentinel landed;
        ``watchdog_error_message`` when an ``error`` item mentioning the
        idle watchdog was persisted.
    """
    deadline = time.monotonic() + _TURN_SETTLE_TIMEOUT_S
    completed = False
    watchdog_error: str | None = None
    call_count = 0
    while time.monotonic() < deadline:
        resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/items?limit=200", timeout=10.0)
        resp.raise_for_status()
        items = resp.json()["data"]
        call_count = 0
        for item in items:
            data = item.get("data") or {}
            item_type = item.get("type")
            if item_type == "function_call":
                call_count += 1
            elif item_type == "error":
                message = str(item.get("message") or data.get("message") or "")
                if "idle watchdog" in message:
                    watchdog_error = message
            elif item_type == "message":
                role = item.get("role") or data.get("role")
                content = item.get("content") or data.get("content") or []
                text = " ".join(
                    str(part.get("text", "")) for part in content if isinstance(part, dict)
                )
                if role == "assistant" and _DONE_SENTINEL in text:
                    completed = True
        if completed or watchdog_error is not None:
            break
        time.sleep(1.0)
    return completed, watchdog_error, call_count


@pytest.mark.timeout(280)
def test_wedged_llm_call_must_not_hard_stop_the_run(
    page: Page,
    wedged_turn_session: tuple[str, str, str],
    mock_llm_server_url: str,
) -> None:
    """A run whose LLM call wedges mid-task must recover, not stop completely.

    On a build with the bug this fails at the ``watchdog_error`` assertion:
    after two iterations of real progress the wedged LLM call trips the
    harness idle watchdog, the turn dies with the ``RuntimeError`` the user
    reported, the chat shows the failure pill, and the run never resumes —
    the wedged call is not retried and the queued completion reply is never
    fetched from the mock.
    """
    base_url, session_id, model = wedged_turn_session
    _queue_progress_then_wedge(mock_llm_server_url, model)

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Work through the multi-step task and report when done.")
    page.get_by_role("button", name="Send", exact=True).click()

    completed, watchdog_error, call_count = _poll_turn_outcome(base_url, session_id)

    # The run made real progress before the wedge — this is what
    # distinguishes the reported failure (a productive run killed by one
    # wedged call) from a turn that never started.
    assert call_count >= 1, (
        f"Expected at least one executed tool call before the turn settled; "
        f"got {call_count}. Without prior progress this journey would not "
        f"exercise the mid-run wedged-LLM-call path."
    )

    # When the watchdog killed the run, let the user-visible failure land
    # on screen (the error pill) before failing — the recorded journey then
    # ends on exactly what the user sees.
    if watchdog_error is not None:
        with contextlib.suppress(AssertionError):
            expect(page.get_by_test_id("error-pill").first).to_be_visible(timeout=15_000)
        page.wait_for_timeout(1_500)

    # THE BUG: one wedged LLM call terminally fails the whole run.
    assert watchdog_error is None, (
        f"The run hard-stopped with the harness idle watchdog error after "
        f"{call_count} executed tool call(s): {watchdog_error!r}. A single "
        f"wedged LLM call must be recovered (abandoned and retried, or "
        f"failed with a retryable classification) so the run continues "
        f"instead of stopping completely."
    )

    assert completed, (
        f"The turn neither completed nor failed with the idle-watchdog "
        f"error within {_TURN_SETTLE_TIMEOUT_S:.0f}s — the journey never "
        f"settled (mock LLM mis-scripted or harness never started)."
    )

    # The user-visible outcome: the run recovered and the final reply
    # rendered, no error pill.
    expect(
        page.locator('[data-testid="message-bubble"][data-role="assistant"]').last
    ).to_contain_text(_DONE_SENTINEL, timeout=30_000)
    expect(page.get_by_test_id("error-pill")).to_have_count(0)
