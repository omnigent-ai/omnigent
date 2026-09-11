"""E2E: a new shell must run the user's login shell, not bash.

Journey: a user whose login
shell (``$SHELL``) is not bash opens a session with a standard SDK coding
agent, clicks the workspace rail's "+" (Open new) -> "Shell (...)" default,
and the terminal that opens must run that login shell -- not bash.
Native-wrapper sessions declare the host's installed shells with
``$SHELL`` first (``native_shell_terminal_spec``); SDK-agent sessions
launch what the agent spec declares, and a generic ``shell`` terminal
with no pinned ``command`` (the shipped default-agent shape,
``examples/polly/config.yaml``) must resolve to the host user's login
shell at launch.

This test pins the *fixed* behavior: with the session runner's environment
carrying ``SHELL=/bin/sh`` (``sh`` stands in for the reporter's zsh purely
as the non-bash login shell value -- it is always installed and, like zsh,
is a member of ``omnigent._platform._KNOWN_INTERACTIVE_SHELLS``; the buggy
path never consults ``$SHELL`` at all, so any non-bash value manifests
identically), the default "+ -> Shell" click must produce a terminal
actually running the user's login shell.

The agent's ``terminals:`` block is read at runtime from
``examples/polly/config.yaml`` (the shipped default-agent shape, the
in-repo stand-in for the internal-beta ``databricks_coding_agent.yaml``
the reporter used) so the test tracks the shipped default shape,
whichever way it expresses "follow the user's ``$SHELL``".

The which-shell probe is a file side-effect at an absolute ``tmp_path``
(the pane and this test share a host), because xterm renders to a WebGL
canvas so the pane's output never reaches the DOM -- same technique as
``test_shell_wheel_scroll_reaches_mouse_tracking_program``.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _REPO_ROOT,
    _server_state,
    open_right_rail,
)

_AGENT_NAME = "login_shell_probe"

# The user's login shell for this journey: always installed, non-bash, and a
# member of ``omnigent._platform._KNOWN_INTERACTIVE_SHELLS`` (like the
# reporter's zsh, which Ubuntu CI images don't ship).
_LOGIN_SHELL = "/bin/sh"


def _agent_yaml() -> str:
    """Build the SDK test agent: minimal chat spec + the shipped default-agent
    ``terminals:`` block, copied verbatim at runtime from
    ``examples/polly/config.yaml`` so the test tracks whatever shape the
    shipped default declares (today: ``shell: command: bash`` first).
    """
    polly = yaml.safe_load((_REPO_ROOT / "examples" / "polly" / "config.yaml").read_text())
    terminals_block = yaml.safe_dump({"terminals": polly["terminals"]}, sort_keys=False)
    return f"""\
name: {_AGENT_NAME}
prompt: |
  You are a placeholder assistant for a UI-driven terminal test. The test
  never sends you a chat message.

executor:
  model: gpt-4o-mini
  harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none

{terminals_block}"""


def _fixture_runner_pids(base_url: str) -> list[int]:
    """PIDs of runner processes belonging to the spawned test server.

    Same ``pgrep -f omnigent.runner._entry`` stand-in as the sibling shell
    tests, but filtered by the child's ``RUNNER_SERVER_URL`` (read from
    ``/proc/<pid>/environ``) so an unrelated runner on the machine is never
    touched. A PID whose environ can't be read is skipped -- the fixture
    runner is our own same-uid child, so its environ is always readable.
    """
    result = subprocess.run(
        ["pgrep", "-f", "omnigent.runner._entry"], capture_output=True, text=True
    )
    if result.returncode != 0:
        return []
    needle = f"RUNNER_SERVER_URL={base_url}".encode()
    pids: list[int] = []
    for line in result.stdout.split():
        try:
            pid = int(line)
            environ = Path(f"/proc/{pid}/environ").read_bytes()
        except (ValueError, OSError):
            continue
        # /proc environ is NUL-separated; the needle carries no NULs, so a
        # plain substring check is exact.
        if needle in environ:
            pids.append(pid)
    return pids


def _wait_runner_status(
    base_url: str, runner_id: str, *, online: bool, timeout_s: float = _HEALTH_TIMEOUT_S
) -> None:
    """Poll the runner status route until it reports the wanted online state."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
            is_online = resp.status_code == 200 and resp.json().get("online") is True
        except httpx.HTTPError:
            is_online = False
        if is_online == online:
            return
        time.sleep(_HEALTH_POLL_INTERVAL_S)
    raise AssertionError(
        f"runner {runner_id} never became {'online' if online else 'offline'} "
        f"within {timeout_s:.0f}s"
    )


@pytest.fixture
def login_shell_session(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A runner-bound SDK-agent session whose runner env carries a non-bash
    ``$SHELL``.

    The shared ``live_server`` runner inherits the pytest process environment
    (where ``$SHELL`` may be anything, including bash), so this fixture
    replaces it: kill the shared runner, respawn one under the same
    token-bound identity with ``SHELL=/bin/sh``, and bind a fresh session
    using the shipped default-agent ``terminals:`` shape to it. Teardown
    deletes the session and kills the replacement runner; later tests
    respawn via ``_ensure_runner_online`` exactly as they already do after
    the suite's other runner-killing tests (``test_stale_stream``,
    ``test_empty_terminal_view_remains_selectable_and_resumable``).

    :returns: ``(base_url, session_id)``.
    """
    import io
    import json as _json
    import tarfile

    runner_id = str(_server_state["runner_id"])
    binding_token = str(_server_state["binding_token"])
    mock_url = str(_server_state.get("mock_llm_url", ""))

    # Drop the shared runner (whatever $SHELL it inherited) ...
    for pid in _fixture_runner_pids(live_server):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)
    _wait_runner_status(live_server, runner_id, online=False)

    # ... and respawn it under the same identity, with the user's login
    # shell pinned to a non-bash value. Same env recipe as the conftest's
    # ``_ensure_runner_online``.
    runner_tmp = tmp_path_factory.mktemp("login_shell_runner")
    log_path = runner_tmp / "runner.log"
    log_handle = open(log_path, "w")  # noqa: SIM115 - fd dup'd into child; closed below
    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": live_server,
        # The user's login shell the fixed product must honor.
        "SHELL": _LOGIN_SHELL,
        **(
            {"OPENAI_BASE_URL": f"{mock_url}/v1", "OPENAI_API_KEY": "mock-key"} if mock_url else {}
        ),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    log_handle.close()  # child holds its own dup of the fd
    try:
        _wait_runner_status(live_server, runner_id, online=True)
    except AssertionError:
        proc.kill()
        raise AssertionError(
            f"replacement runner did not register; log:\n{log_path.read_text()[-3000:]}"
        ) from None

    # Register the agent + create the session in one bundle POST (the same
    # shape as the conftest's terminal fixtures: a non-config.yaml arcname
    # routes through the omnigent compat translator, which is the parser
    # that threads ``terminals:`` into the agent spec).
    yaml_bytes = _agent_yaml().encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=f"{_AGENT_NAME}.yaml")
        info.size = len(yaml_bytes)
        tar.addfile(info, io.BytesIO(yaml_bytes))
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": _json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    patch_resp = httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()

    try:
        yield (live_server, session_id)
    finally:
        try:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def test_new_shell_uses_users_login_shell(
    page: Page, login_shell_session: tuple[str, str], tmp_path: Path
) -> None:
    """The "+ -> Shell" default launches the user's login shell, not bash.

    Opens the session page, launches the default new shell from the
    workspace rail's "+" (Open new) menu, and has the pane report which
    shell is actually running (``$0``) both on screen and into a probe
    file. With the runner's ``$SHELL`` set to ``/bin/sh``, the reported
    shell must be ``sh``.
    """
    base_url, session_id = login_shell_session
    expected = os.path.basename(_LOGIN_SHELL)
    probe = tmp_path / "login_shell_probe.txt"

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    # The "+" (Open new) menu's "Shell (<default>)" row launches the default
    # shell type directly -- the click journey the report describes.
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile(r"^Shell")).click()

    # The shell opens as a rail tab; wait for its xterm to connect before
    # typing (input sent before the WS attach opens is dropped).
    terminal_view = rail.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=20_000)
    page.wait_for_timeout(1_000)

    # Ask the running shell to identify itself: visibly in the pane AND
    # into the probe file (tee), since xterm's WebGL output is not in the
    # DOM. ``$SHELL`` / ``$0`` expand inside the pane's shell.
    textarea = terminal_view.locator("textarea.xterm-helper-textarea")
    textarea.focus()
    page.keyboard.type(f'echo "login shell is $SHELL but this terminal runs: $0" | tee {probe}')
    page.keyboard.press("Enter")

    deadline = time.monotonic() + 20
    reported: str | None = None
    while time.monotonic() < deadline:
        if probe.exists():
            match = re.search(r"runs: (\S+)", probe.read_text())
            if match:
                reported = match.group(1)
                break
        page.wait_for_timeout(500)
    assert reported is not None, "the new shell never executed the probe command"

    # Normalize: a login-shell spawn reports "-sh", an absolute-path spawn
    # reports "/bin/sh" -- both are the user's login shell.
    running = os.path.basename(reported).lstrip("-")
    assert running == expected, (
        f"the new terminal is running {running!r}, but the user's login shell "
        f"($SHELL) is {_LOGIN_SHELL!r} -- new shells must use the user's "
        f"default shell instead of always bash"
    )
