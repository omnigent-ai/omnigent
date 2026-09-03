"""An actively-progressing turn must not be killed by the absolute watchdog.

Reproduces the reported failure: a healthy harness
turn that keeps emitting real progress events (tool calls, deltas — each of
which resets the idle watchdog) is forcibly failed the moment its cumulative
duration crosses ``HARNESS_TURN_ABSOLUTE_TIMEOUT_S`` (default 10,800 s). The
user sees the turn die mid-work with::

    turn exceeded the 10800s harness absolute watchdog (total turn duration
    cap; the turn kept emitting but never finished)

Journey (the reported 3 h ceiling is scaled to 10 s via the product's own
``HARNESS_TURN_ABSOLUTE_TIMEOUT_S`` env knob on a dedicated runner — the
same time-scaling the scaffold's unit tests use — so the reproduction runs
in seconds while exercising the identical code path):

1. start a session on a scaffolded-harness agent (openai-agents, driven by
   the mock LLM),
2. send a message that starts a long multi-step turn: the agent runs several
   tool-call rounds, each emitting progress events well inside the idle
   window (idle is set to 120 s and is reset by every emit — it never trips),
3. keep the turn active past the absolute ceiling,
4. observe: on a build with the bug the stream ends in ``response.failed``
   with the "absolute watchdog" error and the chat shows an error pill; the
   expected behavior is that the still-progressing turn completes normally.

On a buggy build this test FAILS at the ``watchdog_error is None`` assertion
(the reproduction); after a fix the same journey completes and the test
passes.

Run::

    pytest tests/e2e_ui/chat/test_absolute_watchdog_spares_active_turn.py
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

# Scaled-down absolute turn ceiling (prod default: 10,800 s). The scripted
# turn below is guaranteed to outlast it by LLM-side delays alone.
_ABSOLUTE_TIMEOUT_S = 10
# Idle watchdog kept far above the inter-event cadence so it never trips —
# the point of the bug is that the turn IS making progress.
_IDLE_TIMEOUT_S = 120

# Seconds for the dedicated runner to tunnel into the shared server.
_RUNNER_ONLINE_TIMEOUT_S = 30.0

# Number of scripted tool-call rounds and the mock LLM's per-response delay.
# 5 rounds + final text at 2.0 s each = >= 12 s of LLM time, always past the
# 10 s absolute ceiling regardless of how fast tool execution is.
_TOOL_ROUNDS = 5
_LLM_DELAY_S = 2.0

# The sentinel the final (post-tool-rounds) assistant reply carries; its
# presence in the transcript is the "turn completed" signal.
_DONE_SENTINEL = "LONG_TASK_COMPLETED_SENTINEL"

# Ceiling for the whole turn to settle (complete or fail) — natural
# completion is ~15-25 s including first-turn harness boot.
_TURN_SETTLE_TIMEOUT_S = 120.0

_AGENT_YAML = """\
name: {name}
prompt: |
  You are a deterministic test assistant working through a long multi-step
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
def short_watchdog_runner(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """Spawn a dedicated runner whose harness subprocesses get a 10 s ceiling.

    The harness scaffold reads ``HARNESS_TURN_ABSOLUTE_TIMEOUT_S`` /
    ``HARNESS_TURN_TIMEOUT_S`` from its process environment at import
    (``omnigent/runtime/harnesses/_scaffold.py``), and the runner's
    ``_build_harness_spawn_env`` passes the runner's own environment through
    to every spawned harness. A dedicated runner (same pattern as
    ``stub_harness_runner`` in ``test_stop_button_interrupts_turn.py``) keeps
    the override out of the shared ``live_server`` runner and out of
    ``os.environ``.

    Yields the runner id to bind sessions to.
    """
    from omnigent.runner.identity import token_bound_runner_id

    runner_tmp = tmp_path_factory.mktemp("watchdog_runner")
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
        # The reproduction's time scaling: prod's 3 h absolute ceiling
        # becomes 10 s; the idle watchdog stays far above the event cadence.
        "HARNESS_TURN_ABSOLUTE_TIMEOUT_S": str(_ABSOLUTE_TIMEOUT_S),
        "HARNESS_TURN_TIMEOUT_S": str(_IDLE_TIMEOUT_S),
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
                f"short-watchdog runner exited early (code {proc.returncode}); "
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
            f"short-watchdog runner did not register within "
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
def watchdog_session(
    live_server: str,
    short_watchdog_runner: str,
) -> Iterator[tuple[str, str, str]]:
    """Create a session on the short-ceiling runner; yield (base, sid, model)."""
    ws = Path(tempfile.mkdtemp(prefix="omnigent-e2e-abs-watchdog-"))
    name = f"watchdog_probe_{uuid.uuid4().hex[:8]}"
    model = f"watchdog-probe-{uuid.uuid4().hex[:8]}"

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
            json={"runner_id": short_watchdog_runner},
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
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        shutil.rmtree(ws, ignore_errors=True)


def _queue_long_progressing_turn(mock_url: str, model: str) -> None:
    """Script a turn that keeps emitting progress past the absolute ceiling.

    Each tool-call round makes the harness emit real (non-heartbeat) events
    — resetting the idle watchdog exactly as the bug report describes — while
    the per-response ``delay`` stretches the turn's total duration beyond
    ``_ABSOLUTE_TIMEOUT_S`` on LLM time alone.
    """
    responses: list[dict[str, object]] = [
        {
            "tool_calls": [
                {
                    "call_id": f"call_step_{step}",
                    "name": "sys_os_shell",
                    "arguments": json.dumps({"command": f'echo "progress step {step}"'}),
                }
            ],
            "delay": _LLM_DELAY_S,
        }
        for step in range(1, _TOOL_ROUNDS + 1)
    ]
    responses.append({"text": _DONE_SENTINEL, "delay": _LLM_DELAY_S})
    configure_mock_llm(mock_url, responses, key=model)
    set_fallback_mock_llm(mock_url, model, _DONE_SENTINEL)


def _poll_turn_outcome(base_url: str, session_id: str) -> tuple[bool, str | None, int]:
    """Poll the transcript until the turn settles.

    :returns: ``(completed, watchdog_error_message, function_call_count)`` —
        ``completed`` when the final assistant sentinel landed;
        ``watchdog_error_message`` when an ``error`` item mentioning the
        absolute watchdog was persisted.
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
                if "absolute watchdog" in message:
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
def test_actively_progressing_turn_survives_absolute_watchdog(
    page: Page,
    watchdog_session: tuple[str, str, str],
    mock_llm_server_url: str,
) -> None:
    """A turn emitting steady progress must complete, not die at the ceiling.

    On a build with the bug this fails at the ``watchdog_error``
    assertion: the harness kills the still-emitting turn the moment its
    cumulative duration crosses ``HARNESS_TURN_ABSOLUTE_TIMEOUT_S``, and the
    chat shows the failure pill instead of the final assistant reply.
    """
    base_url, session_id, model = watchdog_session
    _queue_long_progressing_turn(mock_llm_server_url, model)

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Work through the long multi-step task and report when done.")
    page.get_by_role("button", name="Send", exact=True).click()

    completed, watchdog_error, call_count = _poll_turn_outcome(base_url, session_id)

    # The turn made real progress before it settled — this is what
    # distinguishes the reported bug (an ACTIVE turn killed by the absolute
    # ceiling) from a legitimately wedged turn the idle watchdog should kill.
    assert call_count >= 1, (
        f"Expected at least one executed tool call before the turn settled; "
        f"got {call_count}. Without progress events this journey would not "
        f"exercise the active-turn-vs-absolute-watchdog path."
    )

    # When the watchdog killed the turn, let the user-visible failure land
    # on screen (the error pill) before failing — the recorded journey then
    # ends on exactly what the user sees.
    if watchdog_error is not None:
        with contextlib.suppress(AssertionError):
            expect(page.get_by_test_id("error-pill").first).to_be_visible(timeout=15_000)
        page.wait_for_timeout(1_500)

    # THE BUG: the absolute watchdog fails the turn despite ongoing progress.
    assert watchdog_error is None, (
        f"An actively-progressing turn was killed by the absolute turn "
        f"watchdog after {call_count} executed tool call(s): {watchdog_error!r}. "
        f"Turns that keep emitting real progress must not be failed solely "
        f"because their cumulative duration crossed "
        f"HARNESS_TURN_ABSOLUTE_TIMEOUT_S."
    )

    assert completed, (
        f"The turn neither completed nor failed with the absolute-watchdog "
        f"error within {_TURN_SETTLE_TIMEOUT_S:.0f}s — the journey never "
        f"settled (mock LLM mis-scripted or harness never started)."
    )

    # The user-visible outcome: the final reply rendered, no error pill.
    expect(
        page.locator('[data-testid="message-bubble"][data-role="assistant"]').last
    ).to_contain_text(_DONE_SENTINEL, timeout=30_000)
    expect(page.get_by_test_id("error-pill")).to_have_count(0)
