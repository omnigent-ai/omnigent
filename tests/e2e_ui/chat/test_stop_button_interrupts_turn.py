"""UI journey: the web Stop button must interrupt a running hermes/qwen turn.

Reproduces the stop-button no-interrupt defect: the ``hermes`` and
``qwen`` SDK executors historically inherited the base no-op
``interrupt_session`` (``omnigent/inner/executor.py``), so clicking the web
Stop button while a turn was in flight cancelled nothing — the active harness
CLI subprocess kept running to completion, unlike kimi
(``omnigent/inner/kimi_executor.py``) which terminates it.

Journey per harness:

1. create a session on an agent bound to the harness (the vendor CLI is a
   local stub, so no real Hermes/Qwen install or auth is needed),
2. send a message from the composer — the stub CLI starts a long "turn" and
   records its PID under the stub state dir,
3. click the composer's Stop (Interrupt) button while the turn is running,
4. assert the running harness subprocess observes the interrupt within
   ``_INTERRUPT_DEADLINE_S``: either it is terminated (kimi-style
   ``proc.terminate()``) or it receives an ACP ``session/cancel`` (qwen's
   graceful path; the stub records both as a ``.cancelled`` marker).

On a build with the bug, the ``hermes`` parametrization FAILS at step 4 —
the interrupt never reaches the stub, which keeps running long after Stop.
The ``qwen`` parametrization PASSES (qwen gained ``interrupt_session`` with
the ACP event-handling completion).

The stub CLIs are faithful to what each executor actually drives:

- ``hermes``: ``hermes chat -q <msg> -Q --source tool`` — prints the
  ``session_id:`` line the executor parses, then sleeps (the long turn),
  exiting immediately with a marker on SIGTERM/SIGINT.
- ``qwen``: ``qwen --acp`` — a minimal NDJSON JSON-RPC agent that answers
  ``initialize`` / ``session/new``, streams one ``agent_message_chunk``,
  leaves the ``session/prompt`` request pending (the long turn), and
  resolves it with ``stopReason: cancelled`` when ``session/cancel``
  arrives.

The stub CLI paths reach the harness through a **dedicated runner**: the
``stub_harness_runner`` fixture spawns its own runner subprocess whose
environment carries ``OMNIGENT_HERMES_PATH`` / ``OMNIGENT_QWEN_PATH`` (and
the ``HERMES_``/``QWEN_``-prefixed state-dir vars that survive the
executors' deny-by-default spawn-env filter), then binds the test session
to it. Nothing is written to ``os.environ`` and nothing depends on when the
shared ``live_server`` runner booted, so the test is order-independent
under the sharded CI invocation and standalone runs alike::

    pytest tests/e2e_ui/chat/test_stop_button_interrupts_turn.py -k hermes
    pytest tests/e2e_ui/chat/test_stop_button_interrupts_turn.py -k qwen
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import secrets
import signal
import subprocess
import sys
import tarfile
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Seconds for the dedicated stub runner to tunnel into the shared server.
_RUNNER_ONLINE_TIMEOUT_S = 30.0

# Seconds the harness subprocess gets to observe the interrupt after Stop is
# clicked. kimi's terminate lands in well under a second; 12s absorbs slow-CI
# scheduling while staying far below the stubs' 180s "turn".
_INTERRUPT_DEADLINE_S = 12.0

# Seconds to wait for the stub CLI to actually be mid-turn (first turn also
# pays the harness-wrap subprocess boot).
_TURN_START_TIMEOUT_S = 90.0

_HERMES_STUB = textwrap.dedent(
    """\
    #!/usr/bin/env bash
    # Stub Hermes CLI: mimics `hermes chat -q <msg> -Q --source
    # tool` — prints the session_id line the executor parses, then "works"
    # for a long time. An interrupt (SIGTERM from interrupt_session) ends it
    # immediately and leaves a .cancelled marker.
    set -u
    STATE_DIR="${HERMES_STUB_STATE_DIR:?}"
    echo "session_id: stub_hermes_session"
    echo "$$" > "${STATE_DIR}/hermes.pid"
    trap 'date > "${STATE_DIR}/hermes.cancelled"; exit 143' TERM INT
    for _ in $(seq 1 180); do
      sleep 1
    done
    echo "finished the long task"
    """
)

_QWEN_STUB = textwrap.dedent(
    '''\
    #!/usr/bin/env python3
    """Stub Qwen ACP agent: NDJSON JSON-RPC over stdio.

    Answers initialize/session/new, streams one message chunk, then leaves
    the session/prompt request pending (the long-running turn). A
    session/cancel notification — what QwenExecutor.interrupt_session sends
    — resolves the prompt with stopReason=cancelled and exits, leaving a
    .cancelled marker. SIGTERM (the fallback path) does the same.
    """
    import json
    import os
    import signal
    import sys

    STATE_DIR = os.environ["QWEN_STUB_STATE_DIR"]


    def _marker(name: str, text: str) -> None:
        with open(os.path.join(STATE_DIR, name), "w") as fh:
            fh.write(text)


    def _send(obj) -> None:
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()


    def _on_term(signum, frame):
        _marker("qwen.cancelled", "terminated")
        sys.exit(143)


    signal.signal(signal.SIGTERM, _on_term)

    pending_prompt_id = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "agentCapabilities": {"promptCapabilities": {"image": False}}
                    },
                }
            )
        elif method == "session/new":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"sessionId": "stub-qwen-session"},
                }
            )
        elif method == "session/prompt":
            pending_prompt_id = msg_id
            _marker("qwen.pid", str(os.getpid()))
            _send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": "stub-qwen-session",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {
                                "type": "text",
                                "text": "working on the long task...",
                            },
                        },
                    },
                }
            )
            # Deliberately no response: the turn stays in flight until
            # session/cancel (or the executor's own timeout) ends it.
        elif method == "session/cancel":
            _marker("qwen.cancelled", "session/cancel")
            if pending_prompt_id is not None:
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": pending_prompt_id,
                        "result": {"stopReason": "cancelled"},
                    }
                )
            sys.exit(0)
        elif msg_id is not None:
            # Anything else the executor requests (e.g. session/set_config_option)
            # gets a generic success so the turn setup never stalls.
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
    '''
)


@pytest.fixture(scope="session")
def stub_harness_runner(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, Path]]:
    """Spawn a dedicated runner whose environment carries the stub CLI paths.

    The shared ``live_server`` runner boots with whatever environment the
    pytest process had at session start, so injecting the stubs there would
    be order-dependent under sharded CI. Instead this spawns a second runner
    (the unauthenticated local server accepts any runner whose id is derived
    from its binding token) with ``OMNIGENT_HERMES_PATH`` /
    ``OMNIGENT_QWEN_PATH`` and the stub state-dir vars in its subprocess env
    — ``os.environ`` is never mutated and no boot-order assumption is made.

    Yields ``(runner_id, state_dir)``: the runner to bind sessions to, and
    the dir the stubs write their pid/cancel markers into.
    """
    from omnigent.runner.identity import token_bound_runner_id

    state_dir = tmp_path_factory.mktemp("harness_stub_state")
    bin_dir = tmp_path_factory.mktemp("harness_stub_bin")
    runner_tmp = tmp_path_factory.mktemp("harness_stub_runner")
    log_path = runner_tmp / "runner.log"

    hermes_stub = bin_dir / "hermes"
    hermes_stub.write_text(_HERMES_STUB)
    hermes_stub.chmod(0o755)

    qwen_stub = bin_dir / "qwen"
    qwen_stub.write_text(_QWEN_STUB)
    qwen_stub.chmod(0o755)

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": live_server,
        # The harness wraps resolve the vendor CLI from OMNIGENT_<H>_PATH;
        # the STUB_STATE_DIR vars ride the executors' filtered spawn env
        # because they carry the harness's own allowed prefix
        # (HERMES_ / QWEN_).
        "OMNIGENT_HERMES_PATH": str(hermes_stub),
        "OMNIGENT_QWEN_PATH": str(qwen_stub),
        "HERMES_STUB_STATE_DIR": str(state_dir),
        "QWEN_STUB_STATE_DIR": str(state_dir),
    }
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
                f"stub-harness runner exited early (code {proc.returncode}); "
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
            f"stub-harness runner did not register within "
            f"{_RUNNER_ONLINE_TIMEOUT_S:.0f}s; log:\n{log_path.read_text()[-3000:]}"
        )

    try:
        yield runner_id, state_dir
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _create_stub_session(base_url: str, runner_id: str, harness: str) -> str:
    """Create a session on an inline agent bound to *harness* and the runner."""
    agent_name = f"stop_probe_{harness}"
    agent_yaml = (
        f"name: {agent_name}\n"
        f"prompt: You are a test agent for the stop-button reproduction.\n"
        f"executor:\n"
        f"  model: stub-model\n"
        f"  harness: {harness}\n"
    )
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            payload = agent_yaml.encode()
            info = tarfile.TarInfo(f"{agent_name}.yaml")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        bundle = buf.getvalue()

    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    patch_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    return session_id


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for(predicate, timeout_s: float, interval_s: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


@pytest.mark.parametrize("harness", ["hermes", "qwen"])
def test_stop_button_interrupts_running_turn(
    page: Page,
    live_server: str,
    stub_harness_runner: tuple[str, Path],
    harness: str,
) -> None:
    """Clicking Stop while a turn runs must cancel the harness subprocess.

    The user-observable contract (matching kimi and the other SDK
    harnesses): Stop ends the in-flight work. Concretely, within
    ``_INTERRUPT_DEADLINE_S`` of the click the stub CLI must either be
    terminated or receive the harness's cancel signal. With the bug, the
    hermes executor's base no-op ``interrupt_session`` means neither ever
    happens and the stub keeps "working" — this test then fails.
    """
    stub_runner_id, state_dir = stub_harness_runner
    pid_file = state_dir / f"{harness}.pid"
    cancel_marker = state_dir / f"{harness}.cancelled"
    # Clean any leftovers from a prior parametrization/retry.
    for stale in (pid_file, cancel_marker):
        if stale.exists():
            stale.unlink()

    session_id = _create_stub_session(live_server, stub_runner_id, harness)
    try:
        page.goto(f"{live_server}/c/{session_id}")
        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)
        composer.fill("Please do a long-running task.")
        page.get_by_role("button", name="Send", exact=True).click()

        # The turn is genuinely in flight once the stub CLI reports its PID
        # (first turn also pays the harness-wrap subprocess boot).
        assert _wait_for(pid_file.exists, _TURN_START_TIMEOUT_S), (
            f"stub {harness} CLI never started a turn within "
            f"{_TURN_START_TIMEOUT_S:.0f}s — check the harness wrap booted "
            f"(OMNIGENT_{harness.upper()}_PATH resolution / runner logs)"
        )
        stub_pid = int(pid_file.read_text().strip())
        assert _pid_alive(stub_pid), "stub CLI exited before Stop was clicked"

        # The Send button flips to the destructive Interrupt (Stop) square
        # while the session is working and the draft is empty.
        interrupt_button = page.get_by_role("button", name="Interrupt", exact=True)
        expect(interrupt_button).to_be_visible(timeout=30_000)
        interrupt_button.click()

        # The reproduction's observable: the running harness subprocess must
        # see the interrupt — terminated, or handed the harness's cancel.
        interrupted = _wait_for(
            lambda: cancel_marker.exists() or not _pid_alive(stub_pid),
            _INTERRUPT_DEADLINE_S,
        )
        # Give the recording a beat to show the post-Stop UI state.
        page.wait_for_timeout(1_000)
        assert interrupted, (
            f"Stop button did not interrupt the running {harness} turn: the "
            f"stub {harness} CLI (pid {stub_pid}) is still running "
            f"{_INTERRUPT_DEADLINE_S:.0f}s after the interrupt was sent and "
            f"never received a cancel — {harness}'s executor does not "
            f"implement interrupt_session"
        )
    finally:
        # Best-effort cleanup; never mask the test's own assertion failure.
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
