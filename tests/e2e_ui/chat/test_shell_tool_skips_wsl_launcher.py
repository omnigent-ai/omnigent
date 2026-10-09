"""E2E: the agent shell tool must not run inside a WSL launcher ``bash``.

On Windows with WSL installed, the first ``bash`` on PATH is usually
``%SystemRoot%\\System32\\bash.exe`` — the WSL launcher — so every
``sys_os_shell`` call runs in the Linux distro instead of against the
Windows checkout, and the tool result reports that launcher as ``shell``.

This journey stands in for that machine on Linux: a runner is spawned with
a ``Windows/System32/bash`` launcher stand-in ahead of the real shell on
PATH, a mock-LLM agent with an ``os_env`` runs ``echo $0; uname -a``, and
the test reads which shell the product actually used from the Output panel
and the persisted tool result: the launcher must be skipped in favour of the
next shell on PATH.
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
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.runner.identity import token_bound_runner_id
from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _create_bundled_session,
    configure_mock_llm,
    set_fallback_mock_llm,
)

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

_COMMAND = "echo $0; uname -a"
_REPLY = "Probe finished."
_WSL_UNAME = (
    "Linux DESKTOP-WSL 5.15.167.4-microsoft-standard-WSL2 #1 SMP "
    "Tue Nov 5 00:21:55 UTC 2024 x86_64 x86_64 x86_64 GNU/Linux"
)

# Stand-in for the WSL launcher: runs the command through the real bash so
# the product's ``--noprofile --norc -c`` argv works, but answers ``uname``
# the way a WSL distro would and keeps ``$0`` pointing at itself.
_LAUNCHER = """\
#!/bin/bash
standin_dir=$(cd "$(dirname "$0")" && pwd)
export PATH="$standin_dir/wsl-bin:$PATH"
exec -a "$0" /usr/bin/bash "$@"
"""
_UNAME = f'#!/bin/sh\necho "{_WSL_UNAME}"\n'

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

_RUNNER_ONLINE_TIMEOUT_S = 60.0


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(0o755)


def _wsl_launcher_dir(root: Path) -> Path:
    """Create ``Windows/System32/bash`` (launcher stand-in) under *root*."""
    system32 = root / "Windows" / "System32"
    (system32 / "wsl-bin").mkdir(parents=True)
    _write_executable(system32 / "bash", _LAUNCHER)
    _write_executable(system32 / "wsl-bin" / "uname", _UNAME)
    return system32


@pytest.fixture
def wsl_first_runner(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> Iterator[tuple[str, Path]]:
    """A runner whose PATH starts with the WSL launcher stand-in.

    Mirrors ``_spawn_runner_against_external_server`` but with the stand-in
    directory prepended to PATH, so the runner's shell resolution sees the
    launcher before ``/usr/bin/bash`` exactly like a PowerShell-started host.

    :returns: ``(runner_id, system32_dir)``.
    """
    system32 = _wsl_launcher_dir(tmp_path)
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    env = {
        **os.environ,
        "PATH": f"{system32}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": live_server,
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
    }
    log_path = tmp_path / "runner.log"
    with open(log_path, "w") as log_handle:
        proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )

    deadline = time.monotonic() + _RUNNER_ONLINE_TIMEOUT_S
    last_error = "not polled yet"
    online = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            last_error = f"runner exited early with code {proc.returncode}"
            break
        try:
            status = httpx.get(f"{live_server}/v1/runners/{runner_id}/status", timeout=2)
            if status.status_code == 200 and status.json().get("online") is True:
                online = True
                break
            last_error = f"runner status HTTP {status.status_code}: {status.text[:200]}"
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)

    try:
        if not online:
            raise RuntimeError(
                f"stand-in runner did not come online ({last_error}).\n"
                f"Runner log:\n{log_path.read_text()[-3000:]}"
            )
        yield runner_id, system32
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@pytest.fixture
def wsl_shell_session(
    live_server: str,
    mock_llm_server_url: str,
    wsl_first_runner: tuple[str, Path],
) -> Iterator[tuple[str, str, Path]]:
    """A session on the stand-in runner whose turn runs ``echo $0; uname -a``.

    The mock queue is keyed by a per-fixture model name: one ``sys_os_shell``
    call with the probe command, then a text fallback for the wrap-up call.

    :returns: ``(base_url, session_id, system32_dir)``.
    """
    runner_id, system32 = wsl_first_runner
    ws = Path(tempfile.mkdtemp(prefix="omnigent-e2e-wsl-shell-"))
    name = f"wsl_shell_probe_{uuid.uuid4().hex[:8]}"
    model = f"wsl-shell-probe-{uuid.uuid4().hex[:8]}"

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_wsl_shell_probe",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": _COMMAND}),
                    }
                ]
            },
        ],
        key=model,
    )
    set_fallback_mock_llm(mock_llm_server_url, model, _REPLY)

    yaml_text = _AGENT_YAML.format(name=name, model=model, cwd=str(ws))
    session_id = _create_bundled_session(live_server, runner_id, yaml_text)
    print(f"\n[wsl-shell-probe] session_id={session_id} runner_id={runner_id} system32={system32}")

    try:
        yield (live_server, session_id, system32)
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def _send(page: Page, text: str) -> None:
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _expand_tool_output(page: Page) -> str:
    """Expand Worked fold → tool-run summary → tool row and return the Output text."""
    worked = page.get_by_test_id("turn-worked-fold")
    expect(worked).to_be_visible(timeout=90_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)
    worked.locator('[data-slot="collapsible-trigger"]').first.click()

    fold = page.get_by_text("Ran 1 shell command", exact=True)
    expect(fold).to_be_visible(timeout=15_000)
    fold.click()

    row = page.locator('[data-slot="collapsible-trigger"]', has_text="uname -a").first
    expect(row).to_be_visible(timeout=15_000)
    row.click()

    expect(page.get_by_text("Output", exact=True)).to_be_visible(timeout=15_000)
    panel = page.locator("[data-language] pre", has_text='"shell"').first
    expect(panel).to_be_visible(timeout=15_000)
    panel.scroll_into_view_if_needed()
    return panel.inner_text()


def _persisted_shell_result(base_url: str, session_id: str) -> dict[str, object]:
    """Return the persisted ``sys_os_shell`` result for the session's only tool call."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/items?limit=200", timeout=10.0)
    resp.raise_for_status()
    for item in resp.json()["data"]:
        data = item.get("data") or {}
        raw = item.get("output", data.get("output"))
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                continue
        if isinstance(raw, dict) and "shell" in raw:
            return raw
    return {}


@pytest.mark.timeout(300)
def test_shell_tool_does_not_run_in_wsl_launcher_bash(
    page: Page,
    wsl_shell_session: tuple[str, str, Path],
) -> None:
    """``sys_os_shell`` must resolve a real shell, never the System32 WSL launcher."""
    base_url, session_id, system32 = wsl_shell_session
    page.goto(f"{base_url}/c/{session_id}")

    _send(page, "Run this in your shell: echo $0; uname -a")
    expect(page.locator(_ASSISTANT, has_text=_REPLY).first).to_be_visible(timeout=90_000)

    output_text = _expand_tool_output(page)
    persisted = _persisted_shell_result(base_url, session_id)
    shell = str(persisted.get("shell", ""))
    print(f"\n[wsl-shell-probe] persisted shell={shell!r} stdout={persisted.get('stdout')!r}")
    print(f"[wsl-shell-probe] Output panel:\n{output_text}")

    # Hold the expanded Output panel on screen so the recording shows which
    # shell answered before the assertion settles.
    page.wait_for_timeout(2_500)

    launcher = system32 / "bash"
    assert shell and Path(shell) != launcher and str(launcher) not in output_text, (
        f"sys_os_shell ran inside the WSL launcher {launcher}: shell={shell!r}; "
        f"Output panel:\n{output_text}"
    )
    assert "microsoft" not in output_text.lower(), (
        f"uname -a reported a WSL distro instead of the host:\n{output_text}"
    )
