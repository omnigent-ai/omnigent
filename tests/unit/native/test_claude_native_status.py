"""Tests for the Claude Code statusLine wrapper."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent import claude_native_status


def _run(
    *,
    stdin_payload: str,
    bridge_dir: Path,
    chain: str | None = None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str] | None = None,
) -> int:
    """
    Invoke the wrapper's ``main()`` with a stubbed stdin.

    :param stdin_payload: Raw stdin text the wrapper will read.
    :param bridge_dir: Bridge directory the wrapper writes into.
    :param chain: Optional chained command, e.g. ``"echo claude-hud"``.
    :param monkeypatch: Pytest fixture for patching ``sys.stdin``.
    :param capsys: Pytest stdout/stderr capture fixture, unused here.
    :returns: Process exit code from ``main()``.
    """
    del capsys
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_payload))
    argv = ["--bridge-dir", str(bridge_dir)]
    if chain is not None:
        argv.extend(["--chain", chain])
    return claude_native_status.main(argv)


def test_status_wrapper_writes_context_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The wrapper persists ``context_window_size`` and ``current_usage``.

    These are the two fields the forwarder consumes; missing either
    silently breaks the ring on a real claude-native session, so this
    test pins the exact wire shape Claude Code provides on statusLine
    stdin (modeled on claude-hud's reverse-engineered schema).
    """
    stdin = json.dumps(
        {
            "session_id": "abc",
            "model": {"display_name": "Opus 4.7"},
            "context_window": {
                "context_window_size": 1_000_000,
                "current_usage": {
                    "input_tokens": 6,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 200,
                    "output_tokens": 50,
                },
                "used_percentage": 31,
            },
        }
    )

    rc = _run(stdin_payload=stdin, bridge_dir=tmp_path, monkeypatch=monkeypatch)
    assert rc == 0

    persisted = json.loads((tmp_path / "context.json").read_text(encoding="utf-8"))
    assert persisted["context_window_size"] == 1_000_000
    assert persisted["current_usage"]["input_tokens"] == 6
    assert persisted["current_usage"]["cache_creation_input_tokens"] == 100
    assert persisted["used_percentage"] == 31


def test_status_wrapper_captures_cost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The wrapper persists Claude Code's cumulative ``cost.total_cost_usd``.

    Claude Code's statusLine stdin carries a top-level ``cost`` block with its
    own session billing. claude-native never produces a ``response.completed``
    event, so the Omnigent relay's cost accumulation never runs for it — capturing
    this is the only way native session cost reaches ``session_usage``. A
    failure here means native Cost-Ask policies always see $0.
    """
    stdin = json.dumps(
        {
            "session_id": "abc",
            "context_window": {"context_window_size": 1_000_000},
            "cost": {"total_cost_usd": 0.42, "total_duration_ms": 1234},
        }
    )

    rc = _run(stdin_payload=stdin, bridge_dir=tmp_path, monkeypatch=monkeypatch)
    assert rc == 0

    persisted = json.loads((tmp_path / "context.json").read_text(encoding="utf-8"))
    assert persisted["total_cost_usd"] == 0.42


def test_status_wrapper_drops_payload_without_context_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Payloads with no ``context_window`` block leave no file behind.

    Older Claude Code versions (pre v2.x) didn't include ``context_window``
    on statusLine stdin. The wrapper must degrade gracefully so the
    ring keeps the spec default rather than rendering a half-init state.
    """
    stdin = json.dumps({"session_id": "abc", "model": {"display_name": "Opus 4.7"}})

    rc = _run(stdin_payload=stdin, bridge_dir=tmp_path, monkeypatch=monkeypatch)
    assert rc == 0
    assert not (tmp_path / "context.json").exists()


def test_status_wrapper_chains_to_user_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    The chained command receives the original stdin and renders its stdout.

    Claude Code only invokes a single statusLine command. Without
    chaining, overriding for context capture would silently hide
    claude-hud / any user-installed status bar.
    """
    captured_calls: list[dict[str, object]] = []

    class _FakeProc:
        returncode = 0
        stdout = "claude-hud line\n"
        stderr = ""

    def fake_run(*args: object, **kwargs: object) -> _FakeProc:
        captured_calls.append({"args": args, "kwargs": kwargs})
        return _FakeProc()

    monkeypatch.setattr(subprocess, "run", fake_run)

    stdin = json.dumps({"context_window": {"context_window_size": 200_000}})
    rc = _run(
        stdin_payload=stdin,
        bridge_dir=tmp_path,
        chain="echo claude-hud",
        monkeypatch=monkeypatch,
    )
    assert rc == 0
    assert len(captured_calls) == 1
    assert captured_calls[0]["args"] == ("echo claude-hud",)
    assert captured_calls[0]["kwargs"]["input"] == stdin
    assert captured_calls[0]["kwargs"]["shell"] is True
    out, _err = capsys.readouterr()
    assert "claude-hud line" in out


def test_status_wrapper_chain_swallows_subprocess_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    Chained-command failures don't propagate — context capture stays best-effort.

    The statusLine command runs on every Claude Code render tick. A
    crash there would yank the user's terminal status bar away on
    every tick; the wrapper logs to stderr and returns 0 instead.
    """

    def fake_run(*args: object, **kwargs: object) -> None:
        raise OSError("chain broken")

    monkeypatch.setattr(subprocess, "run", fake_run)
    stdin = json.dumps({"context_window": {"context_window_size": 200_000}})
    rc = _run(
        stdin_payload=stdin,
        bridge_dir=tmp_path,
        chain="bogus",
        monkeypatch=monkeypatch,
    )
    assert rc == 0
    _out, err = capsys.readouterr()
    assert "chain failed" in err
