"""End-to-end tests: ``omnigent goose`` drives the native Goose TUI.

The goose-native sibling of ``test_cursor_native_cli_e2e``. ``goose-native`` is a
*terminal-first* harness: ``omnigent goose`` launches Block's ``goose session``
TUI in a runner-owned tmux pane, and each web-UI turn is injected into that pane
(bracketed paste + Enter) by
:class:`omnigent.inner.goose_native_executor.GooseNativeExecutor`. The TUI's own
SQLite session store is tailed by :mod:`omnigent.goose_native_forwarder`, which
mirrors Goose's replies back onto the Omnigent conversation as assistant items.

These tests drive the full stack the way a user does — spawn ``omnigent goose``,
then talk to the session **through the server** (``POST /v1/sessions/{id}/events``,
the web-UI path) — and assert on the persisted assistant items.

Environment requirements (why this is opt-in, not pure-CI)
----------------------------------------------------------
* **Opt-in only**: set ``OMNIGENT_E2E_GOOSE_NATIVE=1`` to run. Like the other
  native-TUI e2e tests, goose-native needs a configured Goose provider (via
  ``goose configure`` or ``GOOSE_PROVIDER``/``GOOSE_MODEL`` + a provider key in
  the environment) and a ``tmux`` binary; the ``goose`` binary may be present on
  CI but unconfigured, which would error the TUI. The env-var gate keeps it out
  of CI; a developer with a configured Goose opts in. ``tmux`` and ``goose`` on
  ``PATH`` are also required (checked below).
* ``GOOSE_MODE=auto`` is recommended in the test environment so the cwd test's
  file-read tool call is not blocked on an in-terminal approval prompt.

    OMNIGENT_E2E_GOOSE_NATIVE=1 GOOSE_MODE=auto \
    GOOSE_PROVIDER=openrouter GOOSE_MODEL=openai/gpt-4o-mini \
    OPENROUTER_API_KEY=... \
    .venv/bin/python -m pytest tests/e2e/test_goose_native_cli_e2e.py \
        --profile oss --llm-api-key "..." -v
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

# ``resume_test_server`` is provided by tests/e2e/conftest.py.

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_GOOSE_NATIVE") != "1"
    or shutil.which("goose") is None
    or shutil.which("tmux") is None,
    reason=(
        "goose-native CLI e2e needs a configured Goose provider and a `tmux` "
        "binary; set OMNIGENT_E2E_GOOSE_NATIVE=1 (and have `goose` installed + "
        "configured and `tmux` on PATH) to run"
    ),
)

_CWD_MARKER_FILE = "CWD_MARKER.txt"

# Goose cold-starts the TUI and round-trips to the configured provider; mirror
# the headroom the cursor-native CLI tests allow on a contended host.
_CONV_ID_TIMEOUT = 120.0
_TERMINAL_READY_TIMEOUT = 90.0
_REPLY_TIMEOUT = 180.0


def test_goose_native_cli_smoke(
    resume_test_server: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """A goose-native turn driven through the server returns the model's reply.

    Spawns a backgrounded ``omnigent goose`` session, waits for its terminal to
    register, injects (via ``/events`` — the web-UI path) a prompt asking Goose
    to emit a unique marker word, and asserts the marker comes back as an
    assistant item. Covers the whole path from CLI parse through tmux injection
    to the forwarder mirroring Goose's reply onto the conversation store.
    """
    # goose-native authenticates via Goose's OWN provider config (e.g.
    # GOOSE_PROVIDER/OPENROUTER_API_KEY), so the test server's LLM is irrelevant
    # here — it runs the mock LLM when no --profile is given. Profile is optional.
    profile = request.config.getoption("--profile") or ""

    pwd_dir = tmp_path / "pwd"
    pwd_dir.mkdir()
    marker = f"GOOSE_{uuid.uuid4().hex[:8].upper()}"

    omni = str(omnigent_console_script())
    handle = spawn_cli_background(
        [omni, "goose", "--server", resume_test_server],
        env=cli_env(profile=profile),
        cwd=str(pwd_dir),
    )
    try:
        conversation_id = wait_for_conversation_id(handle, timeout=_CONV_ID_TIMEOUT)
        with httpx.Client(base_url=resume_test_server, timeout=30) as client:
            wait_for_terminal_ready(
                client,
                conversation_id=conversation_id,
                harness="goose",
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
                    f"`omnigent goose` did not return marker {marker!r}. The "
                    "goose-native path regressed somewhere between tmux injection, "
                    "the goose turn, and the forwarder mirroring the reply onto the "
                    f"conversation.\n\nCLI output tail:\n{handle.output()[-2000:]}"
                ) from exc
    finally:
        handle.terminate()


def test_goose_native_cli_runs_in_launch_cwd(
    resume_test_server: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """``omnigent goose`` launches ``goose session`` in the directory it was run from.

    Spawns a backgrounded ``omnigent goose`` whose process cwd is a temp dir
    containing a marker file, then injects (via the server) a request to read it.
    The marker exists only in the launch cwd, so it can come back only if the
    wrapper launched the TUI there *and* Goose's read tool ran. Requires
    ``GOOSE_MODE=auto`` in the environment so the tool call isn't approval-gated.
    """
    # goose-native authenticates via Goose's OWN provider config (e.g.
    # GOOSE_PROVIDER/OPENROUTER_API_KEY), so the test server's LLM is irrelevant
    # here — it runs the mock LLM when no --profile is given. Profile is optional.
    profile = request.config.getoption("--profile") or ""

    pwd_dir = tmp_path / "pwd"
    pwd_dir.mkdir()
    marker = f"PWD_{uuid.uuid4().hex[:6].upper()}"
    (pwd_dir / _CWD_MARKER_FILE).write_text(marker + "\n")

    omni = str(omnigent_console_script())
    handle = spawn_cli_background(
        [omni, "goose", "--server", resume_test_server],
        env=cli_env(profile=profile),
        cwd=str(pwd_dir),
    )
    try:
        conversation_id = wait_for_conversation_id(handle, timeout=_CONV_ID_TIMEOUT)
        with httpx.Client(base_url=resume_test_server, timeout=30) as client:
            wait_for_terminal_ready(
                client,
                conversation_id=conversation_id,
                harness="goose",
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
                    f"`omnigent goose` did not return marker {marker!r} from "
                    f"{_CWD_MARKER_FILE} — it did not run goose in its launch cwd "
                    f"(or GOOSE_MODE!=auto blocked the read tool).\n\n"
                    f"CLI output tail:\n{handle.output()[-2000:]}"
                ) from exc
    finally:
        handle.terminate()
