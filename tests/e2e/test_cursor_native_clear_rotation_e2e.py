"""End-to-end test: a native ``/clear`` rotates the Omnigent session.

``cursor-agent``'s TUI has ``/clear`` (aliases ``/new``, ``/new-chat``,
``/newchat``), which starts a fresh Cursor chat in the SAME tmux pane. The
transcript forwarder detects that new chat and rotates Omnigent onto a fresh
conversation: it creates the replacement session, binds the runner, points
``external_session_id`` at the new Cursor chat, transfers the ``cursor:main``
terminal, rebinds the bridge config, and notifies the superseded conversation.

This test drives that journey the way a user does — spawn ``omnigent cursor``,
talk to the session through the server (the web-UI path), type ``/clear`` into
the pane, then talk again — and asserts on what the user actually sees:

* the superseded conversation gets the notice naming the new conversation, and
  loses its terminal;
* the replacement conversation owns the pane, mirrors the new chat's reply, and
  carries a different ``external_session_id``;
* the bridge config's ``active_session_id`` follows the rotation, and a message
  composed from the *replacement* conversation still reaches the pane — the
  bridge-id label is what keeps the injector pointed at the launching
  conversation's bridge dir, so this is the check that fails without it;
* exactly one pane exists throughout: a second ``cursor:main`` would 409 the
  transfer and loop the rotation.

Environment requirements (why this is opt-in, not pure-CI)
----------------------------------------------------------
Same gate as ``tests/e2e/test_cursor_native_cli_e2e.py``: set
``OMNIGENT_E2E_CURSOR_NATIVE=1`` and have an interactive ``cursor-agent login``
plus ``tmux`` on ``PATH``. Run it like the cursor-native CLI tests::

    OMNIGENT_E2E_CURSOR_NATIVE=1 \
    .venv/bin/python -m pytest tests/e2e/test_cursor_native_clear_rotation_e2e.py \
        --profile <profile> --llm-api-key "<gateway token>" -v

Those two options only satisfy the test server's startup (``resume_test_server``);
the ``cursor-agent`` turn authenticates via the ambient Cursor login. See
``test_cursor_native_cli_e2e`` for how to derive the token for a given profile.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.harnesses.cursor_native.bridge import (
    bridge_dir_for_session_id,
    capture_cursor_pane,
    kill_session,
    read_active_session_id,
    send_cursor_pane_keys,
)
from tests.e2e._native_resume_helpers import (
    cli_env,
    inject_user_message,
    omnigent_console_script,
    poll_external_session_id,
    poll_for_assistant_marker,
    spawn_cli_background,
    wait_for_conversation_id,
    wait_for_terminal_ready,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CURSOR_NATIVE") != "1"
    or shutil.which("cursor-agent") is None
    or shutil.which("tmux") is None,
    reason=(
        "cursor-native clear-rotation e2e needs an interactive `cursor-agent login` "
        "and a `tmux` binary; set OMNIGENT_E2E_CURSOR_NATIVE=1 (and have "
        "`cursor-agent` installed + logged in and `tmux` on PATH) to run"
    ),
)

# Cursor's force/trust flag — clears the workspace-trust and per-tool approval
# gates that would otherwise block the pane on a y/n the test can't answer.
_FORCE_FLAG = "-f"

_CONV_ID_TIMEOUT = 120.0
_TERMINAL_READY_TIMEOUT = 90.0
_REPLY_TIMEOUT = 180.0
# The rotation only fires on the FIRST message of the new chat (cursor creates
# the chat dir lazily), so allow a full reply's headroom on top of the poll.
_ROTATION_TIMEOUT = 180.0
_BRIDGE_REBIND_TIMEOUT = 60.0
# Let the TUI echo the typed text / render its slash-command popup.
_SLASH_ECHO_TIMEOUT_S = 20.0
# After Enter, how long to wait for the command to actually run before retrying
# Enter once (the popup can swallow the first one).
_SLASH_RUN_TIMEOUT_S = 8.0
# Let the slash-command picker finish filtering before Enter selects from it.
_SLASH_PICKER_SETTLE_S = 1.5
# After /clear, nothing must rotate until the next message. Long enough for
# several forwarder polls (0.7s each) to have run and found nothing.
_CLEAR_QUIET_S = 5.0

_ROTATION_LINK = re.compile(r"\(/c/([^)]+)\)")


def _pane_shows(bridge_dir: Path, needle: str) -> bool | None:
    """Whether the Cursor pane renders *needle*, or ``None`` if unreadable.

    A failed or empty capture is deliberately NOT reported as "absent": callers
    wait on the disappearance of the typed command, and treating an unreadable
    pane as proof it ran would skip the retry that makes the wait meaningful.
    """
    pane = capture_cursor_pane(bridge_dir)
    return needle in pane if pane else None


def _wait_for_pane(bridge_dir: Path, needle: str, *, present: bool, timeout: float) -> bool:
    """Wait until *needle* is (or is no longer) on the pane. Returns success."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _pane_shows(bridge_dir, needle) is present:
            return True
        time.sleep(0.5)
    return False


def _run_slash_clear(bridge_dir: Path) -> None:
    """Type ``/clear`` into the Cursor pane and run it.

    ``-l`` makes tmux send the text literally, so the leading ``/`` is typed
    rather than looked up as a key name; Enter goes separately (unprefixed) so
    tmux interprets it. The composer is emptied first (a mirrored reply can be
    observed before the TUI finishes drawing, leaving a draft behind) and the
    slash-command picker is given time to settle, because that picker can
    consume the first Enter — hence the second one when the command did not run.

    :param bridge_dir: The cursor-native bridge dir holding ``tmux.json``.
    :returns: None.
    :raises AssertionError: If the pane never echoes the typed command, or the
        command never runs.
    """
    # Drop any half-typed draft and dismiss a popup, then start from empty.
    send_cursor_pane_keys(bridge_dir, "Escape")
    send_cursor_pane_keys(bridge_dir, "C-u")
    assert _wait_for_pane(bridge_dir, "/clear", present=False, timeout=_SLASH_ECHO_TIMEOUT_S), (
        "the Cursor pane is unreadable or still holds a `/clear` draft before the test typed one"
    )
    send_cursor_pane_keys(bridge_dir, "-l", "/clear")
    assert _wait_for_pane(bridge_dir, "/clear", present=True, timeout=_SLASH_ECHO_TIMEOUT_S), (
        "the Cursor pane never echoed the typed `/clear`; the TUI is not accepting input"
    )
    # Let the slash-command picker finish filtering before Enter selects from it.
    time.sleep(_SLASH_PICKER_SETTLE_S)
    send_cursor_pane_keys(bridge_dir, "Enter")
    if not _wait_for_pane(bridge_dir, "/clear", present=False, timeout=_SLASH_RUN_TIMEOUT_S):
        send_cursor_pane_keys(bridge_dir, "Enter")
        assert _wait_for_pane(bridge_dir, "/clear", present=False, timeout=_SLASH_RUN_TIMEOUT_S), (
            "`/clear` stayed in the Cursor composer after two Enters; the slash command "
            "never ran, so nothing below could have rotated"
        )


def _kill_cursor_pane(bridge_dir: Path) -> None:
    """Tear the pane down, falling back to tmux when the bridge state is unusable.

    ``handle.terminate()`` only kills the attached CLI: the tmux server and its
    ``cursor-agent`` are independent processes. A stale ``tmux.json`` (target
    already gone, or the file half-written) makes :func:`kill_session` raise, so
    fall back to killing the whole server on whatever socket it still names
    rather than leak a live pane into the next test.
    """
    try:
        kill_session(bridge_dir, timeout_s=30.0)
        return
    except Exception:
        pass
    try:
        raw = json.loads((bridge_dir / "tmux.json").read_text(encoding="utf-8"))
        socket_path = raw.get("socket_path")
    except (OSError, ValueError, AttributeError):
        return
    if isinstance(socket_path, str) and socket_path:
        subprocess.run(
            ["tmux", "-S", socket_path, "kill-server"],
            check=False,
            capture_output=True,
            timeout=15,
        )


def _poll_for_rotation_target(
    client: httpx.Client, *, conversation_id: str, timeout: float
) -> str:
    """Poll the superseded conversation's items for the ``/c/<id>`` notice.

    The forwarder posts the supersession notice as a persisted assistant
    message linking to the replacement conversation, so this both discovers the
    new id and asserts the durable record a reload has to explain.

    :param client: HTTP client pointed at the test server.
    :param conversation_id: The superseded conversation, e.g. ``"conv_old"``.
    :param timeout: Max seconds to wait for the notice.
    :returns: The replacement conversation id.
    :raises AssertionError: If no notice arrives within *timeout*.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(
            f"/v1/sessions/{conversation_id}/items", params={"limit": 50, "order": "desc"}
        )
        if resp.status_code == 200:
            for item in resp.json().get("data", []):
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    text = block.get("text") if isinstance(block, dict) else None
                    match = _ROTATION_LINK.search(text) if isinstance(text, str) else None
                    if match and "ended by `/clear`" in text:
                        return match.group(1)
        time.sleep(0.5)
    raise AssertionError(
        f"{conversation_id} never received a /clear supersession notice within {timeout}s; "
        "the pane's new chat did not rotate the Omnigent session."
    )


def _poll_until(predicate: Callable[[], bool], *, timeout: float, what: str) -> None:
    """Poll *predicate* until true, else fail naming *what*.

    :param predicate: Condition to wait for.
    :param timeout: Max seconds to wait.
    :param what: Description used in the failure message.
    :returns: None.
    :raises AssertionError: If *predicate* stays false for *timeout*.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.5)
    raise AssertionError(f"{what} did not happen within {timeout}s")


def _terminal_ids(client: httpx.Client, conversation_id: str) -> list[str]:
    """Return the terminal resource ids currently owned by *conversation_id*."""
    resp = client.get(f"/v1/sessions/{conversation_id}/resources")
    if resp.status_code != 200:
        return []
    return [
        str(row.get("id")) for row in resp.json().get("data", []) if row.get("type") == "terminal"
    ]


def test_cursor_native_clear_rotates_the_omnigent_session(
    resume_test_server: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """A ``/clear`` in the pane hands the pane to a fresh Omnigent conversation.

    :param resume_test_server: Base URL of the allow-list-free test server.
    :param tmp_path: Per-test temp dir; its ``pwd`` subdir is the launch cwd.
    :param request: Pytest request — reads ``--profile`` for the test server.
    """
    profile = request.config.getoption("--profile")
    assert profile, "this test requires --profile (e.g. --profile oss) for the test server"

    pwd_dir = tmp_path / "pwd"
    pwd_dir.mkdir()
    before = f"BEFORE_{uuid.uuid4().hex[:8].upper()}"
    after = f"AFTER_{uuid.uuid4().hex[:8].upper()}"
    composed = f"COMPOSED_{uuid.uuid4().hex[:8].upper()}"
    terminal_id = terminal_resource_id("cursor", "main")

    omni = str(omnigent_console_script())
    handle = spawn_cli_background(
        [omni, "cursor", "--server", resume_test_server, _FORCE_FLAG],
        env=cli_env(profile=profile),
        cwd=str(pwd_dir),
    )
    bridge_dir: Path | None = None
    try:
        old_conv = wait_for_conversation_id(handle, timeout=_CONV_ID_TIMEOUT)
        bridge_dir = bridge_dir_for_session_id(old_conv)
        with httpx.Client(base_url=resume_test_server, timeout=30) as client:
            wait_for_terminal_ready(
                client,
                conversation_id=old_conv,
                harness="cursor",
                timeout=_TERMINAL_READY_TIMEOUT,
            )
            inject_user_message(
                client,
                conversation_id=old_conv,
                text=f"Reply with ONLY this exact word and nothing else: {before}",
            )
            poll_for_assistant_marker(
                client, conversation_id=old_conv, marker=before, timeout=_REPLY_TIMEOUT
            )
            assert read_active_session_id(bridge_dir) == old_conv, (
                "the launching conversation must own the bridge before any rotation"
            )
            old_chat_id = poll_external_session_id(
                client, conversation_id=old_conv, timeout=_REPLY_TIMEOUT
            )

            # Cursor creates the new chat dir lazily, on its first message, so a
            # bare /clear must rotate nothing.
            _run_slash_clear(bridge_dir)
            time.sleep(_CLEAR_QUIET_S)
            assert read_active_session_id(bridge_dir) == old_conv, (
                "a bare /clear rotated the session before the new chat had any content"
            )

            # The first message of the new chat is what the forwarder detects.
            inject_user_message(
                client,
                conversation_id=old_conv,
                text=f"Reply with ONLY this exact word and nothing else: {after}",
            )
            new_conv = _poll_for_rotation_target(
                client, conversation_id=old_conv, timeout=_ROTATION_TIMEOUT
            )
            assert new_conv != old_conv

            # The bridge config, the terminal, and the mirror all moved.
            _poll_until(
                lambda: read_active_session_id(bridge_dir) == new_conv,
                timeout=_BRIDGE_REBIND_TIMEOUT,
                what=f"bridge active_session_id rebinding onto {new_conv}",
            )
            wait_for_terminal_ready(
                client,
                conversation_id=new_conv,
                harness="cursor",
                timeout=_TERMINAL_READY_TIMEOUT,
            )
            _poll_until(
                lambda: terminal_id not in _terminal_ids(client, old_conv),
                timeout=_BRIDGE_REBIND_TIMEOUT,
                what=f"{terminal_id} leaving the superseded conversation",
            )
            assert _terminal_ids(client, new_conv) == [terminal_id], (
                "the replacement conversation must own exactly one cursor pane; a "
                "second one means auto-create raced the transfer"
            )
            poll_for_assistant_marker(
                client, conversation_id=new_conv, marker=after, timeout=_REPLY_TIMEOUT
            )
            new_chat_id = poll_external_session_id(
                client, conversation_id=new_conv, timeout=_REPLY_TIMEOUT
            )
            assert new_chat_id != old_chat_id, (
                "the replacement conversation is still bound to the pre-/clear Cursor "
                "chat, so a later cold resume would reattach to the wrong chat"
            )

            # Composing from the REPLACEMENT conversation must still reach the
            # pane: without the bridge-id label its bridge dir would be a fresh
            # empty one with no tmux.json, and injection would fail.
            inject_user_message(
                client,
                conversation_id=new_conv,
                text=f"Reply with ONLY this exact word and nothing else: {composed}",
            )
            poll_for_assistant_marker(
                client, conversation_id=new_conv, marker=composed, timeout=_REPLY_TIMEOUT
            )
    finally:
        if bridge_dir is not None:
            _kill_cursor_pane(bridge_dir)
        handle.terminate()
