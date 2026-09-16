"""Keep the Pi exit regression scoped to its own live terminal process."""

from __future__ import annotations

import os
import subprocess

import httpx
import pytest

from tests.e2e.test_pi_main_terminal_tmux_disappears_e2e import (
    _live_pane_process,
    _pi_terminal_resource,
)


def _resource(**overrides: object) -> dict:
    return {
        "id": "terminal_pi_main",
        "type": "terminal",
        "session_id": "owned",
        "metadata": {
            "terminal_name": "pi",
            "session_key": "main",
            "tmux_socket": "/tmp/test-owned.sock",
            "tmux_target": "main",
        },
        **overrides,
    }


def test_pi_resource_selection_ignores_other_sessions_and_terminals() -> None:
    owned = _resource()
    resources = [
        _resource(session_id="other"),
        _resource(id="terminal_shell_main"),
        _resource(metadata={"terminal_name": "pi", "session_key": "other"}),
        owned,
    ]
    with httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"data": resources})),
    ) as client:
        assert _pi_terminal_resource(client, "owned") == owned
        assert _pi_terminal_resource(client, "absent") is None


@pytest.mark.parametrize("output", ["0 0", "1 0", "123 1", "123 0\n456 0", "invalid 0"])
def test_pane_resolution_rejects_dead_ambiguous_or_invalid_pids(monkeypatch, output) -> None:
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 0, output)
    )
    assert _live_pane_process(_resource()) is None


def test_pane_resolution_uses_exact_socket_and_target_without_matching_argv(monkeypatch) -> None:
    def probe(argv, **kwargs):
        assert argv == [
            "tmux",
            "-S",
            "/tmp/test-owned.sock",
            "-f",
            "/dev/null",
            "list-panes",
            "-t",
            "main",
            "-F",
            "#{pane_pid} #{pane_dead}",
        ]
        return subprocess.CompletedProcess(argv, 0, f"{os.getpid()} 0\n")

    monkeypatch.setattr(subprocess, "run", probe)
    process = _live_pane_process(_resource())
    assert process is not None
    assert process.pid == os.getpid()
