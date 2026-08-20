"""End-to-end smoke test: ``omnigent kiro`` drives the native Kiro TUI.

This opt-in test covers the user-facing Kiro native path: the CLI starts a
runner-owned ``kiro-cli chat --tui`` terminal, the server accepts a web-style
``POST /v1/sessions/{id}/events`` message, the Kiro bridge injects it into the
TUI, and the Kiro session forwarder mirrors the assistant response back into
the Omnigent conversation.

Run locally with a logged-in Kiro CLI::

    OMNIGENT_E2E_KIRO_NATIVE=1 \
    .venv/bin/python -m pytest tests/e2e/test_kiro_native_cli_e2e.py -v

The test is skipped by default because ``kiro-cli`` authentication is anchored
to the developer's ambient Kiro login; a binary present on CI may still be
unauthenticated and would hang the TUI.
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

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_KIRO_NATIVE") != "1"
    or shutil.which("kiro-cli") is None
    or shutil.which("tmux") is None,
    reason=(
        "kiro-native CLI e2e needs an interactive Kiro login and a `tmux` "
        "binary; set OMNIGENT_E2E_KIRO_NATIVE=1 and have `kiro-cli` logged in"
    ),
)

_CONV_ID_TIMEOUT = 120.0
_TERMINAL_READY_TIMEOUT = 90.0
_REPLY_TIMEOUT = 180.0


def test_kiro_native_cli_smoke(
    resume_test_server: str,
    tmp_path: Path,
) -> None:
    """A Kiro-native turn driven through the server returns an assistant item."""
    pwd_dir = tmp_path / "pwd"
    pwd_dir.mkdir()
    marker = f"KIRO_{uuid.uuid4().hex[:8].upper()}"

    omni = str(omnigent_console_script())
    handle = spawn_cli_background(
        [omni, "kiro", "--server", resume_test_server],
        env=cli_env(),
        cwd=str(pwd_dir),
    )
    try:
        conversation_id = wait_for_conversation_id(handle, timeout=_CONV_ID_TIMEOUT)
        with httpx.Client(base_url=resume_test_server, timeout=30) as client:
            wait_for_terminal_ready(
                client,
                conversation_id=conversation_id,
                harness="kiro",
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
                    f"`omnigent kiro` did not return marker {marker!r}. The "
                    "kiro-native path regressed somewhere between tmux input, "
                    "the Kiro TUI turn, and session-forwarder mirroring.\n\n"
                    f"CLI output tail:\n{handle.output()[-2000:]}"
                ) from exc
    finally:
        handle.terminate()
