"""A message sent while Claude's launcher waits for a Databricks sign-in is sent after it.

A launcher wrapper in front of ``claude`` (Isaac's ``ucode``, for example) can stop
on a device-code sign-in before Claude Code starts. This drives a real server,
runner and tmux pane with such a launcher: the message must wait for the sign-in,
then reach Claude's composer once, in order with a message queued behind it,
without a failure card or a "send your message again" request.
"""

from __future__ import annotations

import contextlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import httpx
import pytest

from tests._helpers.native_session import create_native_session
from tests._helpers.session import send_user_text
from tests.e2e.test_claude_native_delivery_timeout_e2e import _FAKE_CLAUDE_TUI
from tests.e2e.test_native_terminal_start_error_log_correlation_e2e import (
    _bind_session,
    _native_stack,
)

pytestmark = [
    pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is required"),
    pytest.mark.timeout(240),
]

_T = TypeVar("_T")

_SIGN_IN_URL = (
    "https://dbc-e2e.cloud.databricks.com/oidc/v1/authorize"
    "?client_id=databricks-cli&response_type=code"
)


def _launcher_script(tui: Path, signed_in: Path) -> str:
    """A ``claude`` launcher that asks for a device-code sign-in before starting Claude."""
    return (
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then echo "2.1.296 (Claude Code)"; exit 0; fi\n'
        # The runner's ``-p`` probes and subcommands return at once; only the
        # interactive launch meets the sign-in, as behind Isaac's launcher.
        'case "$1" in -p|--print) exit 0 ;; -*) ;; ?*) exit 0 ;; esac\n'
        "echo 'Signing in to Databricks before starting Claude Code.'\n"
        f"echo 'Open this URL to sign in: {_SIGN_IN_URL}'\n"
        "echo 'Enter the code: QWRT-7XKP'\n"
        "echo 'Waiting for sign-in to complete...'\n"
        f"while [ ! -e {shlex.quote(str(signed_in))} ]; do sleep 0.2; done\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(tui))} unused-trigger 0\n"
    )


def _items(client: httpx.Client, base_url: str, session_id: str) -> list[dict[str, Any]]:
    return client.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"order": "asc", "limit": 100},
    ).json()["data"]


def _errors(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in items if item["type"] == "error"]


def _notices(items: list[dict[str, Any]], code: str) -> list[dict[str, Any]]:
    return [error for error in _errors(items) if error.get("code") == code]


def _pane(advert: dict[str, str]) -> str:
    return subprocess.run(
        ["tmux", "-S", advert["socket_path"], "capture-pane", "-p", "-t", advert["tmux_target"]],
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout


def _sent(pane: str) -> list[str]:
    """Messages the fake Claude received, in order, without the clear-input keys it echoes."""
    return [
        re.sub(r"\[[0-9;]+u", "", line.removeprefix("sent: ")).strip()
        for line in pane.splitlines()
        if line.startswith("sent: ")
    ]


def _wait(poll: Callable[[], _T], *, timeout: float, describe: Callable[[], str]) -> _T:
    """Return the first truthy value of ``poll`` within ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value := poll():
            return value
        time.sleep(0.2)
    raise AssertionError(describe())


def test_message_waits_for_launcher_sign_in_then_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tui = tmp_path / "claude-tui.py"
    tui.write_text(_FAKE_CLAUDE_TUI)
    signed_in = tmp_path / "signed-in"
    first = f"first-{uuid.uuid4().hex[:8]}"
    second = f"second-{uuid.uuid4().hex[:8]}"
    with (
        tempfile.TemporaryDirectory(prefix="omni-sign-in-") as native_tmp,
        contextlib.ExitStack() as resources,
    ):
        monkeypatch.setenv("TMPDIR", native_tmp)
        stack = resources.enter_context(_native_stack(tmp_path, _launcher_script(tui, signed_in)))
        client = resources.enter_context(
            httpx.Client(base_url=stack.base_url, trust_env=False, timeout=120)
        )
        session_id = create_native_session(client, stack.base_url, harness="claude")["session_id"]
        _bind_session(stack, session_id)
        # As in production, the first message starts Claude's pane, so it meets the
        # launcher's sign-in before Claude Code is up.
        send_user_text(client, session_id=session_id, text=first)
        adverts = _wait(
            lambda: list(Path(native_tmp).rglob("tmux.json")),
            timeout=20,
            describe=lambda: "the Claude pane was never advertised",
        )
        advert = json.loads(adverts[0].read_text())
        _wait(
            lambda: "QWRT-7XKP" in _pane(advert),
            timeout=20,
            describe=lambda: f"the launcher never showed its sign-in:\n{_pane(advert)}",
        )

        held = _wait(
            lambda: _notices(
                _items(client, stack.base_url, session_id), "databricks_sign_in_pending"
            ),
            timeout=60,
            describe=lambda: f"no sign-in notice was posted\n{stack.runner_log_tail()}",
        )
        # The refusal is a notice, not a failure, and it no longer asks for a resend.
        assert [notice.get("level") for notice in held] == ["info"], held
        assert held[0]["title"] == "Your message will be sent once you sign in to Databricks"
        assert "then send your message again" not in json.dumps(held).lower(), held
        snapshot = client.get(f"{stack.base_url}/v1/sessions/{session_id}").json()
        assert snapshot.get("status") != "failed", snapshot
        # A message sent during the wait queues behind the held one.
        send_user_text(client, session_id=session_id, text=second)
        time.sleep(3)  # custom-lint: disable=sleep-then-assert -- nothing may reach Claude
        pane = _pane(advert)
        assert first not in pane and second not in pane, pane
        assert "QWRT-7XKP" in pane, pane

        signed_in.touch()
        _wait(
            lambda: second in _sent(_pane(advert)),
            timeout=60,
            describe=lambda: (
                f"the held messages never reached Claude:\n{_pane(advert)}\n"
                f"{stack.runner_log_tail()}"
            ),
        )
        pane = _pane(advert)
        assert _sent(pane) == [first, second], pane

        # The fake TUI writes no Claude transcript, so the run is judged by what reached
        # the pane and by the notices: one hold notice, one "sending now", no failure.
        _wait(
            lambda: _notices(
                _items(client, stack.base_url, session_id), "databricks_sign_in_completed"
            ),
            timeout=30,
            describe=lambda: "no signed-in notice was posted",
        )
        items = _items(client, stack.base_url, session_id)
        completed = _notices(items, "databricks_sign_in_completed")
        assert [notice["message"] for notice in completed] == [
            "Claude Code is ready. Sending your message now."
        ], completed
        assert len(_notices(items, "databricks_sign_in_pending")) == 1, items
        failures = [error for error in _errors(items) if error.get("level") != "info"]
        assert not failures, failures
        snapshot = client.get(f"{stack.base_url}/v1/sessions/{session_id}").json()
        assert snapshot.get("status") != "failed", snapshot
        runner_logs = "".join(
            log.read_text()
            for log in (tmp_path / "home" / ".omnigent" / "logs" / "runner").glob("*.log")
        )
        assert "Holding a message for" in runner_logs
        assert "turn surfaced to UI as failed" not in runner_logs
