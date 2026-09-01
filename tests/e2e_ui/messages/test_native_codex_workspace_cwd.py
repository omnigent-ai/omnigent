"""E2E: runner-owned Codex sessions must honor the selected workspace.

A runner-owned ``codex-native`` session created with a specific ``workspace``
must run its shell tools from that workspace. The runner-owned launch path
writes ``CodexNativeBridgeState`` without ``cwd``, so ``CodexNativeExecutor``
falls back to the harness process's own working directory (the runner
service's cwd) when building the turn's ``environments`` — and shell commands
run from the wrong directory.

Journey:

1. The runner service runs with a working directory that is NOT the session
   workspace (here: the repo root, where the e2e fixtures spawn the runner).
2. Create a ``codex-native`` session bound to that runner with the session
   workspace set to a distinct directory.
3. From the web composer, send a prompt; the (mocked) model asks Codex to run
   ``pwd`` in the shell.
4. Observe the recorded shell call's output in the chat transcript.

Expected: ``pwd`` prints the selected session workspace.
Actual (bug): ``pwd`` prints the runner service's working directory.

Runs against the mock LLM server (deterministic scripted tool call), with the
real ``codex`` CLI booted in the session terminal — the same lane as
``test_native_codex_render_parity.py``.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import tarfile
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _CODEX_MOCK_MODEL,
    _bind_session_runner,
    _ensure_runner_online,
    _server_state,
    _temp_omnigent_mock_config,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _open_terminal_view,
    _wait_terminal_connected,
)

# Real codex CLI boots in the session terminal; skip when it isn't installed
# (matching the other native-codex e2e_ui tests).
pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="codex CLI is required for native Codex e2e",
)

_NATIVE_CODEX_TIMEOUT_MS = 180_000
_DONE_TOKEN = "WORKSPACE_CWD_TURN_DONE"


def _expand_collapsible(page: Page, trigger) -> None:
    """Expand a chat collapsible, retrying across re-renders.

    The shell-command widget re-renders while a turn settles, which detaches
    the node Playwright resolved for a normal ``click()``. Clicking through
    the DOM re-resolves the element on every attempt, and the loop stops once
    the trigger reports ``aria-expanded=true``.

    :param page: The Playwright page.
    :param trigger: Locator of the ``collapsible-trigger`` button.
    """
    deadline_ms = 30_000
    interval_ms = 500
    for _ in range(deadline_ms // interval_ms):
        if trigger.first.get_attribute("aria-expanded") == "true":
            return
        trigger.first.evaluate("el => el.click()")
        page.wait_for_timeout(interval_ms)
    expect(trigger.first).to_have_attribute("aria-expanded", "true")


def _create_codex_session_with_workspace(
    base_url: str,
    runner_id: str,
    workspace: Path,
) -> str:
    """Create a runner-bound ``codex-native`` session with a pinned workspace.

    Mirrors ``tests.e2e_ui.conftest._create_native_codex_session`` (same
    production wrapper spec + labels) but pins ``metadata.workspace`` to
    *workspace* instead of the repo root, so the session's selected workspace
    genuinely differs from the runner process's working directory — the
    precondition of the reported bug.

    :param base_url: Spawned server base URL.
    :param runner_id: The token-bound runner id to bind.
    :param workspace: The session workspace directory the user selected.
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        CODEX_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.codex_native import _materialize_codex_agent_spec

    with tempfile.TemporaryDirectory() as _tmp:
        spec_path = _materialize_codex_agent_spec(Path(_tmp), model=None)
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname → omnigent compat translator (the spec has
        # no spec_version), matching the conftest fixture.
        info = tarfile.TarInfo("codex-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE,
    }
    metadata = {"labels": labels, "workspace": str(workspace)}
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("codex-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


@pytest.fixture
def codex_workspace_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """A runner-bound codex-native session whose workspace differs from the runner cwd.

    Always routes the native Codex LLM calls to the mock LLM server (the test
    scripts an exact tool call, so it can never run against a live gateway).

    :returns: ``(base_url, session_id, workspace)``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    workspace = tmp_path_factory.mktemp("codex_selected_workspace").resolve()
    with _temp_omnigent_mock_config(mock_llm_server_url, "codex"):
        session_id = _create_codex_session_with_workspace(live_server, runner_id, workspace)
        try:
            yield (live_server, session_id, workspace)
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            if respawned is not None:
                respawned.terminate()
                try:
                    respawned.wait(timeout=5)
                except Exception:
                    respawned.kill()
                    respawned.wait(timeout=5)


@pytest.mark.timeout(300)
def test_codex_native_shell_runs_in_selected_workspace(
    page: Page,
    codex_workspace_session: tuple[str, str, Path],
    mock_llm_server_url: str,
) -> None:
    """A web-composer Codex turn runs its shell tool in the session workspace.

    The mocked model replies to the user's prompt with one ``exec_command``
    tool call (``pwd``), then a completion token. The output of ``pwd`` —
    rendered in the chat's shell-command widget — must be the session's
    selected workspace, not the runner service's working directory.
    """
    base_url, session_id, workspace = codex_workspace_session

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    reset_mock_llm(mock_llm_server_url)
    # Incidental internal Codex calls (that don't carry a marker) settle on
    # an empty text response instead of consuming a scripted queue.
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    # Throwaway first turn: the runner's background title generator embeds
    # the FIRST user message in its own LLM call, which would race the
    # scripted queue below (both requests carry the same routing marker).
    # Queue two identical text replies so whichever of the turn / title call
    # arrives first, both settle on text.
    boot_marker = f"WSCWD-BOOT-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "WSCWD_BOOTED"}, {"text": "WSCWD_BOOTED"}],
        key=boot_marker,
        match=boot_marker,
    )
    _send(page, f"Say WSCWD_BOOTED and nothing else. {boot_marker}")
    expect(page.locator(_ASSISTANT, has_text="WSCWD_BOOTED").first).to_be_visible(
        timeout=_NATIVE_CODEX_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_NATIVE_CODEX_TIMEOUT_MS)

    # Clear the boot queue: content routing prefers the LONGEST live match
    # token, and Codex resends the full conversation each request, so a
    # leftover (longer) boot token would shadow the journey marker below.
    reset_mock_llm(mock_llm_server_url)

    # The real journey turn: (1) run `pwd` in the shell, (2) settle with a
    # done token.
    marker = f"WSCWD-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call-{marker}",
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": "pwd"}),
                    }
                ]
            },
            {"text": _DONE_TOKEN},
        ],
        key=marker,
        match=marker,
    )

    _send(page, f"Run pwd in the shell and reply with only its exact output. {marker}")

    expect(page.locator(_ASSISTANT, has_text=_DONE_TOKEN).first).to_be_visible(
        timeout=_NATIVE_CODEX_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_NATIVE_CODEX_TIMEOUT_MS)

    # Expand the recorded shell call so its output is on screen. The
    # settled turn nests it behind two collapsed headers:
    # "Worked for Ns" -> "Ran 1 shell command" -> the `pwd` command row.
    worked = page.get_by_role("button", name=re.compile(r"^Worked for"))
    expect(worked.first).to_be_visible(timeout=30_000)
    _expand_collapsible(page, worked)
    shell_run = page.get_by_role("button", name="Ran 1 shell command")
    expect(shell_run.first).to_be_visible(timeout=30_000)
    _expand_collapsible(page, shell_run)
    command_row = page.locator('button[title="pwd"]')
    expect(command_row.first).to_be_visible(timeout=30_000)
    _expand_collapsible(page, command_row)

    # THE BUG: the recorded shell call must carry the session's selected
    # workspace (the expanded widget shows the call's `cwd` parameter and,
    # where the sandbox permits, `pwd`'s output). On the buggy build the
    # bridge state has no cwd, the executor falls back to the runner
    # process's working directory, and this locator never appears (the
    # widget shows the runner cwd instead).
    expect(page.locator(_ASSISTANT).get_by_text(str(workspace), exact=False).first).to_be_visible(
        timeout=15_000
    )
