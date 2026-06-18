"""End-to-end tests: ``omnigent cursor`` drives the native Cursor TUI.

The cursor-native sibling of ``test_codex_native_cli_cwd_e2e`` /
``test_codex_native_cli_resume_e2e``. ``cursor-native`` is a *terminal-first*
harness: ``omnigent cursor`` launches the official ``cursor-agent`` TUI in a
runner-owned tmux pane, and each web-UI turn is injected into that pane
(bracketed paste + Enter) by
:class:`omnigent.inner.cursor_native_executor.CursorNativeExecutor`. The TUI's
own conversation store is tailed by
:mod:`omnigent.cursor_native_forwarder`, which mirrors ``cursor-agent``'s
replies back onto the Omnigent conversation as assistant items.

These tests drive the full stack the way a user does — spawn ``omnigent
cursor``, then talk to the session **through the server** (``POST
/v1/sessions/{id}/events``, the web-UI path) — and assert on the persisted
assistant items:

* **smoke** — inject a prompt that makes ``cursor-agent`` emit a unique marker
  word, and confirm the marker comes back as an assistant item. This exercises
  CLI parse -> daemon runner spawn -> cursor terminal launch -> tmux injection
  -> ``cursor-agent`` turn -> forwarder mirror -> conversation store.
* **cwd** — drop a marker file in the launch cwd and ask ``cursor-agent`` to
  read it. The file exists only in the launch directory (never in the runner's
  spec-bundle dir), so a correct answer proves both that the TUI launched in
  the launch cwd *and* that its built-in Read tool ran.

Unlike the SDK ``cursor`` harness (``test_per_harness_cursor``), cursor-native
authenticates from the **ambient ``cursor-agent login``** under ``$HOME/.cursor``
— there is no ``CURSOR_API_KEY``. The TUI is launched with ``-f`` (Cursor's
force/trust flag) so it neither blocks on the per-directory "Workspace Trust"
prompt nor on per-tool approval prompts — either of which would hang the pane.

Environment requirements (why this is opt-in, not pure-CI)
----------------------------------------------------------
* **Opt-in only**: set ``OMNIGENT_E2E_CURSOR_NATIVE=1`` to run. Like the other
  native-TUI e2e tests, cursor-native needs an interactive ``cursor-agent
  login`` anchored to the real ``$HOME`` and a ``tmux`` binary; the
  ``cursor-agent`` binary may be present on CI but unauthenticated, which would
  hang the TUI. The env-var gate keeps it out of CI; a developer with a
  logged-in Cursor opts in. ``tmux`` and ``cursor-agent`` on ``PATH`` are also
  required (checked below).
* Run it like the codex-native CLI tests::

    OMNIGENT_E2E_CURSOR_NATIVE=1 \
    .venv/bin/python -m pytest tests/e2e/test_cursor_native_cli_e2e.py \
        --profile oss \
        --llm-api-key "$(databricks auth token -p oss \
            | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')" \
        -v

  The ``--profile`` / ``--llm-api-key`` only satisfy the test server's startup
  (``resume_test_server``); the ``cursor-agent`` turn itself authenticates via
  the ambient Cursor login, not the Databricks gateway.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import httpx
import pytest

from tests.e2e._native_resume_helpers import (
    cli_env,
    inject_user_message,
    omnigent_console_script,
    poll_for_assistant_marker,
    spawn_cli_background,
    wait_for_conversation_id,
    wait_for_terminal_ready,
)

# ``resume_test_server`` is provided by tests/e2e/conftest.py (the allow-list-
# free server the CLI wrapper's self-spawned host daemon can register against).

# Opt-in only — see module docstring. Binary presence is not a sufficient gate
# (present-but-unauthenticated hangs the TUI), so require the explicit env var,
# plus the two binaries the terminal-first harness needs on PATH.
pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CURSOR_NATIVE") != "1"
    or shutil.which("cursor-agent") is None
    or shutil.which("tmux") is None,
    reason=(
        "cursor-native CLI e2e needs an interactive `cursor-agent login` and a "
        "`tmux` binary; set OMNIGENT_E2E_CURSOR_NATIVE=1 (and have `cursor-agent` "
        "installed + logged in and `tmux` on PATH) to run"
    ),
)

# Cursor's force/trust flag, passed as a raw cursor-agent arg. Clears the
# per-directory "Workspace Trust" gate and per-tool approval prompts so the TUI
# never blocks the tmux pane waiting on a y/n the test can't answer.
_FORCE_FLAG = "-f"

_CWD_MARKER_FILE = "CWD_MARKER.txt"

# cursor-agent cold-starts the TUI and round-trips to Cursor's backend; mirror
# the headroom the codex-native CLI tests allow on a contended host.
_CONV_ID_TIMEOUT = 120.0
_TERMINAL_READY_TIMEOUT = 90.0
_REPLY_TIMEOUT = 180.0


def test_cursor_native_cli_smoke(
    resume_test_server: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """A cursor-native turn driven through the server returns the model's reply.

    Spawns a backgrounded ``omnigent cursor`` session, waits for its terminal
    to register, injects (via ``/events`` — the web-UI path) a prompt asking
    ``cursor-agent`` to emit a unique marker word, and asserts the marker comes
    back as an assistant item. The marker is a fresh per-run nonce so a match
    cannot be coincidental and a parallel run cannot leak it.

    This is the end-to-end smoke gate for the cursor-native harness: it covers
    the whole path from CLI parse through tmux injection to the forwarder
    mirroring ``cursor-agent``'s reply onto the conversation store.

    :param resume_test_server: Base URL of the allow-list-free test server.
    :param tmp_path: Per-test temp dir; its ``pwd`` subdir is the launch cwd.
    :param request: Pytest request — reads ``--profile`` for the test server.
    """
    profile = request.config.getoption("--profile")
    assert profile, "this test requires --profile (e.g. --profile oss) for the test server"

    pwd_dir = tmp_path / "pwd"
    pwd_dir.mkdir()
    marker = f"CURSOR_{uuid.uuid4().hex[:8].upper()}"

    omni = str(omnigent_console_script())
    handle = spawn_cli_background(
        [omni, "cursor", "--server", resume_test_server, _FORCE_FLAG],
        env=cli_env(profile=profile),
        cwd=str(pwd_dir),
    )
    try:
        conversation_id = wait_for_conversation_id(handle, timeout=_CONV_ID_TIMEOUT)
        with httpx.Client(base_url=resume_test_server, timeout=30) as client:
            wait_for_terminal_ready(
                client,
                conversation_id=conversation_id,
                harness="cursor",
                timeout=_TERMINAL_READY_TIMEOUT,
            )
            inject_user_message(
                client,
                conversation_id=conversation_id,
                text=f"Reply with ONLY this exact word and nothing else: {marker}",
            )
            try:
                poll_for_assistant_marker(
                    client,
                    conversation_id=conversation_id,
                    marker=marker,
                    timeout=_REPLY_TIMEOUT,
                )
            except AssertionError as exc:
                raise AssertionError(
                    f"`omnigent cursor` did not return marker {marker!r}. The "
                    "cursor-native path regressed somewhere between tmux injection, "
                    "the cursor-agent turn, and the forwarder mirroring the reply "
                    f"onto the conversation.\n\nCLI output tail:\n{handle.output()[-2000:]}"
                ) from exc
    finally:
        handle.terminate()


def test_cursor_native_cli_runs_in_launch_cwd(
    resume_test_server: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """``omnigent cursor`` launches ``cursor-agent`` in the directory it was run from.

    Spawns a backgrounded ``omnigent cursor`` whose process cwd is a temp
    directory containing a marker file, then injects (via the server, the
    web-UI path) a request to read that file. The marker exists only in the
    launch cwd (never in the runner's spec-bundle dir), so it can come back
    only if the wrapper launched the TUI in the launch directory *and*
    ``cursor-agent``'s built-in Read tool ran (the ``-f`` flag auto-approves
    it). The cursor-native sibling of ``test_codex_native_cli_runs_in_launch_cwd``.

    :param resume_test_server: Base URL of the allow-list-free test server.
    :param tmp_path: Per-test temp dir; its ``pwd`` subdir is the launch cwd.
    :param request: Pytest request — reads ``--profile`` for the test server.
    """
    profile = request.config.getoption("--profile")
    assert profile, "this test requires --profile (e.g. --profile oss) for the test server"

    pwd_dir = tmp_path / "pwd"
    pwd_dir.mkdir()
    marker = f"PWD_{uuid.uuid4().hex[:6].upper()}"
    (pwd_dir / _CWD_MARKER_FILE).write_text(marker + "\n")

    omni = str(omnigent_console_script())
    handle = spawn_cli_background(
        [omni, "cursor", "--server", resume_test_server, _FORCE_FLAG],
        env=cli_env(profile=profile),
        cwd=str(pwd_dir),
    )
    try:
        conversation_id = wait_for_conversation_id(handle, timeout=_CONV_ID_TIMEOUT)
        with httpx.Client(base_url=resume_test_server, timeout=30) as client:
            wait_for_terminal_ready(
                client,
                conversation_id=conversation_id,
                harness="cursor",
                timeout=_TERMINAL_READY_TIMEOUT,
            )
            inject_user_message(
                client,
                conversation_id=conversation_id,
                text=(
                    f"Read the file {_CWD_MARKER_FILE} in your current directory "
                    "and reply with its exact contents and nothing else."
                ),
            )
            try:
                poll_for_assistant_marker(
                    client,
                    conversation_id=conversation_id,
                    marker=marker,
                    timeout=_REPLY_TIMEOUT,
                )
            except AssertionError as exc:
                raise AssertionError(
                    f"`omnigent cursor` did not return marker {marker!r} from "
                    f"{_CWD_MARKER_FILE} — it did not run cursor-agent in its launch "
                    "cwd (the wrapper-path cwd resolution regressed, likely the "
                    f"spec-bundle dir).\n\nCLI output tail:\n{handle.output()[-2000:]}"
                ) from exc
    finally:
        handle.terminate()
