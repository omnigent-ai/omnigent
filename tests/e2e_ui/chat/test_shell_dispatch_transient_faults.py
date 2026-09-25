"""E2E: a ``sys_os_shell`` command survives one transient dispatch fault.

A ``sys_os_shell`` call from an openai-agents turn makes two hops, each of
which can hit a transient fault: the runner's ``tools/call`` to the server MCP
proxy (``POST /v1/sessions/{id}/mcp``) and, once the server forwards to the
runner's ``/mcp/execute``, the ``Popen`` of the OS-environment helper. A single
HTTP 500 from the proxy or a single fork ``EAGAIN`` must be retried rather than
delivered to the model as the tool's result; the user then sees the command's
output in the tool card instead of ``Error: RuntimeError: MCP proxy call
failed ...`` or ``{"error": "[Errno 11] Resource temporarily unavailable"}``.

The journey is the real user path on a dedicated server + runner pair: the
mock LLM makes the agent run one ``echo`` through ``sys_os_shell``, and a
one-shot fault, armed by a marker in the command (``_shell_dispatch_faults``),
misbehaves exactly once. The test then opens the settled turn's tool card and
asserts its Output panel shows the echoed token.

Run::

    pytest tests/e2e_ui/chat/test_shell_dispatch_transient_faults.py --ui-skip-build
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.chat._shell_dispatch_faults import FORK_EAGAIN_MARKER, PROXY_500_MARKER
from tests.e2e_ui.conftest import (
    _create_bundled_session,
    _find_free_port,
    configure_mock_llm,
    set_fallback_mock_llm,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FAULTS_MODULE = "tests.e2e_ui.chat._shell_dispatch_faults"

_STACK_READY_TIMEOUT_S = 90.0
_TURN_TIMEOUT_MS = 120_000

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_REPLY = "Probe command handled."

_AGENT_YAML = """\
spec_version: 1
name: {name}
prompt: |
  You are a deterministic test assistant. When asked to run the probe
  command you call sys_os_shell exactly once and then reply with one
  short sentence.

executor:
  model: {model}
  config:
    harness: openai-agents

os_env:
  type: caller_process
  cwd: {cwd}
  sandbox:
    type: none
"""


@dataclass(frozen=True)
class _FaultedStack:
    base_url: str
    runner_id: str
    server_log: Path
    runner_stdout: Path
    runner_log: Path


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _tails(*paths: Path) -> str:
    return "\n".join(
        f"{path.name}:\n{path.read_text(errors='replace')[-3000:] if path.exists() else ''}"
        for path in paths
    )


def _wait_until_online(
    base_url: str,
    runner_id: str,
    procs: tuple[subprocess.Popen[bytes], ...],
    logs: tuple[Path, ...],
) -> None:
    deadline = time.monotonic() + _STACK_READY_TIMEOUT_S
    last_error = "not polled yet"
    while time.monotonic() < deadline:
        for proc in procs:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"faulted stack process exited early (code {proc.returncode});\n"
                    + _tails(*logs)
                )
        try:
            health = httpx.get(f"{base_url}/health", timeout=2)
            if health.status_code == 200:
                status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                if status.status_code == 200 and status.json().get("online") is True:
                    return
                last_error = f"runner status HTTP {status.status_code}: {status.text[:200]}"
            else:
                last_error = f"health HTTP {health.status_code}"
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    raise RuntimeError(
        f"faulted stack not online within {_STACK_READY_TIMEOUT_S:.0f}s "
        f"(last_error={last_error});\n" + _tails(*logs)
    )


@pytest.fixture(scope="module")
def faulted_stack(
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_FaultedStack]:
    """A dedicated server + runner pair carrying the one-shot dispatch faults.

    Both processes are the real product entry points; the ``-c`` preamble only
    installs the marker-armed faults before handing over to them. A dedicated
    pair keeps the faults away from the shared ``live_server`` stack.
    """
    from omnigent.runner.identity import token_bound_runner_id

    stack_tmp = tmp_path_factory.mktemp("shell_dispatch_faults")
    (stack_tmp / "artifacts").mkdir()
    (stack_tmp / "config-home").mkdir()
    server_log = stack_tmp / "server.log"
    runner_stdout = stack_tmp / "runner-stdout.log"
    runner_log = stack_tmp / "runner-process.log"

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    common_env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
    }
    with server_log.open("w") as server_out:
        server = subprocess.Popen(
            [
                sys.executable,
                "-c",
                f"from {_FAULTS_MODULE} import install_server_fault; install_server_fault(); "
                "from omnigent.cli import main; main()",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{stack_tmp / 'test.db'}",
                "--artifact-location",
                str(stack_tmp / "artifacts"),
            ],
            env={**common_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token},
            stdout=server_out,
            stderr=subprocess.STDOUT,
        )
    with runner_stdout.open("w") as runner_out:
        runner = subprocess.Popen(
            [
                sys.executable,
                "-c",
                f"from {_FAULTS_MODULE} import install_runner_fault; install_runner_fault(); "
                "from omnigent.runner._entry import main; main()",
            ],
            env={
                **common_env,
                "OMNIGENT_RUNNER_ID": runner_id,
                "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                "RUNNER_SERVER_URL": base_url,
                # A fresh config home keeps ambient harness credentials out of turn setup.
                "OMNIGENT_CONFIG_HOME": str(stack_tmp / "config-home"),
                "OMNIGENT_PROCESS_LOG_FILE": str(runner_log),
            },
            stdout=runner_out,
            stderr=subprocess.STDOUT,
        )

    procs = (server, runner)
    logs = (server_log, runner_stdout, runner_log)
    try:
        _wait_until_online(base_url, runner_id, procs, logs)
        yield _FaultedStack(base_url, runner_id, server_log, runner_stdout, runner_log)
    finally:
        for proc in reversed(procs):
            _stop(proc)


@pytest.fixture
def probe_session(faulted_stack: _FaultedStack) -> Iterator[tuple[str, str]]:
    """A runner-bound session on a fresh probe agent; yields ``(session_id, model)``."""
    ws = Path(tempfile.mkdtemp(prefix="omnigent-e2e-shell-dispatch-"))
    suffix = uuid.uuid4().hex[:8]
    model = f"shell-dispatch-probe-{suffix}"
    yaml_text = _AGENT_YAML.format(name=f"shell_dispatch_probe_{suffix}", model=model, cwd=ws)
    session_id = _create_bundled_session(
        faulted_stack.base_url, faulted_stack.runner_id, yaml_text
    )
    httpx.get(
        f"{faulted_stack.base_url}/v1/sessions/{session_id}/resources/environments/default",
        timeout=30.0,
    ).raise_for_status()
    try:
        yield session_id, model
    finally:
        httpx.delete(f"{faulted_stack.base_url}/v1/sessions/{session_id}", timeout=10.0)
        shutil.rmtree(ws, ignore_errors=True)


def _run_probe_command(
    page: Page,
    stack: _FaultedStack,
    session_id: str,
    model: str,
    mock_url: str,
    token: str,
) -> Locator:
    """Drive the turn that echoes *token* via ``sys_os_shell``; return the Output ``<pre>``."""
    command = f"echo {token}"
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call_{token}",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": command}),
                    }
                ]
            },
        ],
        key=model,
    )
    set_fallback_mock_llm(mock_url, model, _REPLY)

    page.goto(f"{stack.base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Run the probe command.")
    page.get_by_role("button", name="Send", exact=True).click()

    expect(page.locator(_ASSISTANT, has_text=_REPLY).first).to_be_visible(timeout=_TURN_TIMEOUT_MS)

    worked = page.get_by_test_id("turn-worked-fold")
    expect(worked).to_be_visible(timeout=30_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)
    worked.locator('[data-slot="collapsible-trigger"]').first.click()

    fold = page.get_by_text("Ran 1 shell command", exact=True)
    expect(fold).to_be_visible(timeout=15_000)
    fold.click()

    row = page.locator('[data-slot="collapsible-trigger"]', has_text=command).first
    expect(row).to_be_visible(timeout=15_000)
    row.click()

    output_panel = page.locator("[data-language]", has=page.get_by_text("Output", exact=True))
    expect(output_panel).to_be_visible(timeout=15_000)
    return output_panel.locator("pre")


def _assert_command_ran(
    page: Page,
    output: Locator,
    token: str,
    stack: _FaultedStack,
    signature: str,
) -> None:
    observed: str | None = None
    try:
        expect(output).to_contain_text(token, timeout=10_000)
    except AssertionError:
        observed = output.inner_text()
    # Hold the tool card on screen so a recording ends on the Output panel.
    page.wait_for_timeout(1_500)
    if observed is None:
        return
    log_lines = [
        line
        for line in stack.runner_log.read_text(errors="replace").splitlines()
        if signature in line
    ]
    pytest.fail(
        f"sys_os_shell did not run {token!r} after one transient fault; the tool "
        f"Output panel shows:\n{observed}\nrunner log signature lines "
        f"({signature!r}):\n" + ("\n".join(log_lines) or "<none>")
    )


@pytest.mark.timeout(300)
def test_shell_command_survives_transient_mcp_proxy_500(
    page: Page,
    faulted_stack: _FaultedStack,
    probe_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """One HTTP 500 from the server MCP proxy must not fail the shell command."""
    session_id, model = probe_session
    token = f"{PROXY_500_MARKER}{uuid.uuid4().hex[:8]}"
    output = _run_probe_command(page, faulted_stack, session_id, model, mock_llm_server_url, token)
    _assert_command_ran(page, output, token, faulted_stack, "tool sys_os_shell failed")


@pytest.mark.timeout(300)
def test_shell_command_survives_transient_helper_fork_eagain(
    page: Page,
    faulted_stack: _FaultedStack,
    probe_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """One EAGAIN while spawning the OS helper must not fail the shell command."""
    session_id, model = probe_session
    token = f"{FORK_EAGAIN_MARKER}{uuid.uuid4().hex[:8]}"
    output = _run_probe_command(page, faulted_stack, session_id, model, mock_llm_server_url, token)
    _assert_command_ran(
        page,
        output,
        token,
        faulted_stack,
        "runner OSEnvironment dispatch failed for sys_os_shell",
    )
