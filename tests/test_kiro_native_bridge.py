"""Tests for Kiro native tmux bridge helpers."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import omnigent.kiro_native_bridge as bridge
from omnigent.kiro_native_bridge import (
    KIRO_ACP_RECORD_PATH_ENV_VAR,
    KIRO_NATIVE_BRIDGE_DIR_ENV_VAR,
    acp_record_path,
    build_kiro_native_terminal_env,
    inject_user_message,
    send_kiro_permission_verdict,
    write_forwarder_ready,
    write_tmux_target,
)

_READY_PANE = (
    "old output\n────────────────\nkiro_default · auto\n\n ask a question or describe a task ↵"
)
_PERMISSION_PANE = """
────────────────────────────────────────────────────────────────────────────────
↓ Shell pwd

 shell requires approval
 ❯ Yes, single permission
   Trust, always allow in this session
   No (Tab to edit)
────────────────────────────────────────────────────────────────────────────────
ESC to close · Tab to edit
"""
_PERMISSION_PANE_TRUST_FOCUSED = _PERMISSION_PANE.replace(
    "❯ Yes, single permission\n   Trust, always allow in this session",
    "  Yes, single permission\n ❯ Trust, always allow in this session",
)
_PERMISSION_PANE_REJECT_FOCUSED = _PERMISSION_PANE.replace(
    "❯ Yes, single permission\n   Trust, always allow in this session\n   No (Tab to edit)",
    "  Yes, single permission\n   Trust, always allow in this session\n ❯ No (Tab to edit)",
)
_PERMISSION_PANE_DATE = _PERMISSION_PANE.replace("↓ Shell pwd", "↓ Shell date")


def _install_fake_tmux(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pane_outputs: list[str] | None = None,
) -> list[list[str]]:
    """Replace subprocess.run with a successful tmux stub."""
    calls: list[list[str]] = []
    captures = list(pane_outputs or [_READY_PANE])
    last_capture = captures[-1]

    def _fake_run(args: list[str], **_kwargs: Any) -> SimpleNamespace:
        nonlocal last_capture
        calls.append(args)
        if "capture-pane" in args:
            if captures:
                last_capture = captures.pop(0)
            return SimpleNamespace(
                returncode=0,
                stdout=last_capture,
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return calls


def test_inject_user_message_does_not_wait_for_forwarder_on_fresh_kiro_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A brand-new Kiro session has no JSONL yet, so injection cannot require it."""
    monkeypatch.setattr(bridge, "_TYPE_COMMIT_TIMEOUT_S", 0.0)
    bridge_dir = tmp_path / "bridge"
    calls = _install_fake_tmux(monkeypatch)
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/tmux.sock"),
        tmux_target="main",
    )

    inject_user_message(bridge_dir, content="hello", timeout_s=0.1)

    assert any(call[-1] == "Enter" for call in calls)
    # Message text is delivered via a bracketed paste, never ``send-keys -l``.
    assert any("load-buffer" in call for call in calls)
    assert any("paste-buffer" in call and "-p" in call for call in calls)
    assert not any("-l" in call for call in calls)


def test_build_terminal_env_adds_bridge_dir_and_acp_record_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridge-root")

    env = build_kiro_native_terminal_env("conv_kiro", source_env={"PATH": "/usr/bin"})

    bridge_dir = bridge.bridge_dir_for_session_id("conv_kiro")
    assert env[KIRO_NATIVE_BRIDGE_DIR_ENV_VAR] == str(bridge_dir)
    assert env[KIRO_ACP_RECORD_PATH_ENV_VAR] == str(acp_record_path(bridge_dir))
    assert env["PATH"] == "/usr/bin"


def test_send_kiro_permission_verdict_accepts_default_option(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "_PERMISSION_KEY_INTERVAL_S", 0.0)
    monkeypatch.setattr(bridge, "_PERMISSION_ENTER_SETTLE_S", 0.0)
    bridge_dir = tmp_path / "bridge"
    calls = _install_fake_tmux(monkeypatch, pane_outputs=[_PERMISSION_PANE])
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/tmux.sock"),
        tmux_target="main",
    )

    send_kiro_permission_verdict(
        bridge_dir, action="accept", expected_title="Running: pwd", timeout_s=0.1
    )

    sent_keys = [call[-1] for call in calls if "send-keys" in call]
    assert sent_keys == ["Enter"]


def test_send_kiro_permission_verdict_refuses_accept_when_focus_drifts_after_settle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accept re-validates the allow row after the settle delay before Enter."""
    monkeypatch.setattr(bridge, "_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(bridge, "_PERMISSION_KEY_INTERVAL_S", 0.0)
    monkeypatch.setattr(bridge, "_PERMISSION_ENTER_SETTLE_S", 0.0)
    bridge_dir = tmp_path / "bridge"
    calls = _install_fake_tmux(
        monkeypatch,
        pane_outputs=[_PERMISSION_PANE, _PERMISSION_PANE_TRUST_FOCUSED],
    )
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/tmux.sock"),
        tmux_target="main",
    )

    with pytest.raises(RuntimeError, match="allow option was not safely focused"):
        send_kiro_permission_verdict(
            bridge_dir, action="accept", expected_title="Running: pwd", timeout_s=0.1
        )

    sent_keys = [call[-1] for call in calls if "send-keys" in call]
    assert sent_keys == []


def test_send_kiro_permission_verdict_declines_with_slow_navigation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "_PERMISSION_KEY_INTERVAL_S", 0.0)
    monkeypatch.setattr(bridge, "_PERMISSION_ENTER_SETTLE_S", 0.0)
    bridge_dir = tmp_path / "bridge"
    calls = _install_fake_tmux(
        monkeypatch,
        pane_outputs=[
            _PERMISSION_PANE,
            _PERMISSION_PANE_REJECT_FOCUSED,
        ],
    )
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/tmux.sock"),
        tmux_target="main",
    )

    send_kiro_permission_verdict(
        bridge_dir, action="decline", expected_title="Running: pwd", timeout_s=0.1
    )

    sent_keys = [call[-1] for call in calls if "send-keys" in call]
    assert sent_keys == ["Down", "Down", "Enter"]


def test_send_kiro_permission_verdict_requires_visible_permission_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "_POLL_INTERVAL_S", 0.0)
    bridge_dir = tmp_path / "bridge"
    _install_fake_tmux(monkeypatch, pane_outputs=[_READY_PANE])
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/tmux.sock"),
        tmux_target="main",
    )

    with pytest.raises(RuntimeError, match="permission prompt was not safely focused"):
        send_kiro_permission_verdict(bridge_dir, action="accept", timeout_s=0.01)


def test_send_kiro_permission_verdict_refuses_when_focus_moved_to_trust(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "_POLL_INTERVAL_S", 0.0)
    bridge_dir = tmp_path / "bridge"
    _install_fake_tmux(monkeypatch, pane_outputs=[_PERMISSION_PANE_TRUST_FOCUSED])
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/tmux.sock"),
        tmux_target="main",
    )

    with pytest.raises(RuntimeError, match="permission prompt was not safely focused"):
        send_kiro_permission_verdict(
            bridge_dir, action="accept", expected_title="Running: pwd", timeout_s=0.01
        )


def test_send_kiro_permission_verdict_refuses_when_prompt_title_differs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "_POLL_INTERVAL_S", 0.0)
    bridge_dir = tmp_path / "bridge"
    _install_fake_tmux(monkeypatch, pane_outputs=[_PERMISSION_PANE])
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/tmux.sock"),
        tmux_target="main",
    )

    with pytest.raises(RuntimeError, match="permission prompt was not safely focused"):
        send_kiro_permission_verdict(
            bridge_dir, action="accept", expected_title="Running: date", timeout_s=0.01
        )


def test_send_kiro_permission_verdict_ignores_matching_text_outside_active_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "_POLL_INTERVAL_S", 0.0)
    pane = "old transcript mentioned Running: pwd\n" + _PERMISSION_PANE_DATE
    bridge_dir = tmp_path / "bridge"
    _install_fake_tmux(monkeypatch, pane_outputs=[pane])
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/tmux.sock"),
        tmux_target="main",
    )

    with pytest.raises(RuntimeError, match="permission prompt was not safely focused"):
        send_kiro_permission_verdict(
            bridge_dir, action="accept", expected_title="Running: pwd", timeout_s=0.01
        )


def test_send_kiro_permission_verdict_refuses_decline_when_reject_not_focused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(bridge, "_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(bridge, "_PERMISSION_KEY_INTERVAL_S", 0.0)
    monkeypatch.setattr(bridge, "_PERMISSION_ENTER_SETTLE_S", 0.0)
    bridge_dir = tmp_path / "bridge"
    calls = _install_fake_tmux(
        monkeypatch,
        pane_outputs=[_PERMISSION_PANE, _PERMISSION_PANE_TRUST_FOCUSED],
    )
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/tmux.sock"),
        tmux_target="main",
    )

    with pytest.raises(RuntimeError, match="reject option was not safely focused"):
        send_kiro_permission_verdict(
            bridge_dir, action="decline", expected_title="Running: pwd", timeout_s=0.01
        )

    sent_keys = [call[-1] for call in calls if "send-keys" in call]
    assert sent_keys == ["Down", "Down"]


def test_inject_user_message_waits_for_forwarder_on_resumed_kiro_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed Kiro session waits for JSONL forwarder catch-up before typing."""
    monkeypatch.setattr(bridge, "_TYPE_COMMIT_TIMEOUT_S", 0.0)
    bridge_dir = tmp_path / "bridge"
    calls = _install_fake_tmux(monkeypatch)
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/tmux.sock"),
        tmux_target="main",
        requires_forwarder_ready=True,
    )
    write_forwarder_ready(bridge_dir)

    inject_user_message(bridge_dir, content="hello", timeout_s=0.1)

    assert any(call[-1] == "Enter" for call in calls)


def test_inject_user_message_waits_for_kiro_input_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restarted Kiro TUI must render its input prompt before typing."""
    monkeypatch.setattr(bridge, "_TYPE_COMMIT_TIMEOUT_S", 0.0)
    monkeypatch.setattr(bridge, "_POLL_INTERVAL_S", 0.0)
    bridge_dir = tmp_path / "bridge"
    calls = _install_fake_tmux(
        monkeypatch,
        pane_outputs=[
            "Kiro loading...",
            _READY_PANE,
        ],
    )
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/tmux.sock"),
        tmux_target="main",
    )

    inject_user_message(bridge_dir, content="hello", timeout_s=0.1)

    capture_indexes = [index for index, call in enumerate(calls) if "capture-pane" in call]
    paste_index = next(index for index, call in enumerate(calls) if "paste-buffer" in call)
    assert len(capture_indexes) >= 2
    assert max(capture_indexes[:2]) < paste_index


def test_inject_user_message_fails_when_kiro_input_prompt_never_renders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lost first input should fail instead of typing into a booting pane."""
    monkeypatch.setattr(bridge, "_POLL_INTERVAL_S", 0.0)
    bridge_dir = tmp_path / "bridge"
    _install_fake_tmux(monkeypatch, pane_outputs=["Kiro loading..."])
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/tmux.sock"),
        tmux_target="main",
    )

    with pytest.raises(RuntimeError, match="input prompt was not ready"):
        inject_user_message(bridge_dir, content="hello", timeout_s=0.01)


def test_paste_payload_bytes_encodes_line_breaks_as_cr() -> None:
    """Newlines (incl. CRLF/CR) become CR, tabs survive, other control bytes drop.

    The TUI treats a bracketed-paste CR as an in-draft newline, so encoding all
    line breaks to CR is what keeps a multi-line message a single draft. A stray
    ESC (or other control byte) would close the bracketed paste early, so those
    are dropped.
    """
    assert bridge._paste_payload_bytes("a\nb\r\nc\rd") == b"a\rb\rc\rd"
    assert bridge._paste_payload_bytes("keep\ttab") == b"keep\ttab"
    assert bridge._paste_payload_bytes("drop\x1bESC") == b"dropESC"


def test_inject_user_message_multiline_routes_through_bracketed_paste(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A multi-line web message is pasted whole, not typed line-by-line.

    The bug: ``send-keys -l`` delivers the interior newlines as Enter keys, so
    the first line submits on its own. The fix routes the message through a
    single bracketed paste (``paste-buffer -p``) — the CR encoding the TUI needs
    to keep the breaks as draft data is covered by
    :func:`test_paste_payload_bytes_encodes_line_breaks_as_cr`. Here we just pin
    that multi-line content never reaches ``send-keys -l`` and is committed by a
    single Enter.
    """
    monkeypatch.setattr(bridge, "_TYPE_COMMIT_TIMEOUT_S", 0.0)
    bridge_dir = tmp_path / "bridge"
    calls = _install_fake_tmux(monkeypatch)
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/tmux.sock"),
        tmux_target="main",
    )

    inject_user_message(bridge_dir, content="line one\nline two\nline three", timeout_s=0.1)

    assert any("paste-buffer" in call and "-p" in call for call in calls)
    assert not any("-l" in call for call in calls)
    # Exactly one Enter commits the whole draft — not one per line.
    assert sum(1 for call in calls if call[-1] == "Enter") == 1


def test_inject_user_message_fails_when_resumed_forwarder_is_not_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed first message must fail instead of being pasted too early."""
    bridge_dir = tmp_path / "bridge"
    _install_fake_tmux(monkeypatch)
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/tmux.sock"),
        tmux_target="main",
        requires_forwarder_ready=True,
    )

    with pytest.raises(RuntimeError, match="session forwarder was not ready"):
        inject_user_message(bridge_dir, content="hello", timeout_s=0.1)


def test_draft_in_input_region_ignores_matching_history_and_baseline() -> None:
    """Short messages like '2' must match only a changed Kiro input region."""
    baseline = "kiro_default · auto · ◔ 2%\n\n ask a question or describe a task ↵"
    pane_with_history_only = "2\n\nold answer\n────────────────\n" + baseline
    pane_with_draft = "old 2\n────────────────\nkiro_default · auto · ◔ 2%\n\n 2"

    assert not bridge._draft_in_input_region(pane_with_history_only, "2", baseline)
    assert bridge._draft_in_input_region(pane_with_draft, "2", baseline)


def test_draft_in_input_region_ignores_kiro_chrome_for_short_messages() -> None:
    """One-character prompts must not match cwd, branch, or placeholder chrome."""
    baseline = (
        "kiro_default · auto · ◔ 3%             ~/Work/omnigent · "
        "(feat/kiro-cli-harness)\n\n ask a question or describe a task ↵"
    )
    pane_after_submit = (
        "c\n\n🙂\n────────────────\nkiro_default · auto · ◔ 4%             "
        "~/Work/omnigent · (feat/kiro-cli-harness)\n\n "
        "ask a question or describe a task ↵\n/copy to clipboard"
    )
    pane_with_draft = (
        "old answer\n────────────────\nkiro_default · auto · ◔ 3%             "
        "~/Work/omnigent · (feat/kiro-cli-harness)\n\n c"
    )

    assert not bridge._draft_in_input_region(pane_after_submit, "c", baseline)
    assert bridge._draft_in_input_region(pane_with_draft, "c", baseline)


def test_inject_interrupt_sends_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interrupt cancels the running turn with a single Escape, nothing else.

    Verified against kiro-cli 2.10.0: Escape stops generation and leaves an empty
    composer, so (unlike cursor) there is no draft to clear afterwards.
    """
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        bridge, "_wait_for_tmux_info", lambda *_a, **_k: {"socket_path": "/s", "tmux_target": "t"}
    )
    monkeypatch.setattr(bridge, "_run_tmux", lambda _sock, *args: calls.append(args))

    bridge.inject_interrupt(tmp_path)

    assert calls == [("send-keys", "-t", "t", "Escape")]


def test_kill_session_kills_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hard-stop kills the tmux session (ends ``kiro-cli`` and the pane)."""
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        bridge, "_wait_for_tmux_info", lambda *_a, **_k: {"socket_path": "/s", "tmux_target": "t"}
    )
    monkeypatch.setattr(bridge, "_run_tmux", lambda _sock, *args: calls.append(args))

    bridge.kill_session(tmp_path)

    assert calls == [("kill-session", "-t", "t")]


def test_write_mcp_bridge_config_writes_token_idempotently(tmp_path: Path) -> None:
    """serve-mcp's bridge.json gets a token; a second call keeps the same one."""
    bridge_dir = tmp_path / "bridge"
    bridge.write_mcp_bridge_config(bridge_dir)
    config_path = bridge_dir / "bridge.json"
    first = json.loads(config_path.read_text())
    assert isinstance(first.get("token"), str) and first["token"]

    bridge.write_mcp_bridge_config(bridge_dir)

    assert json.loads(config_path.read_text())["token"] == first["token"]


def test_build_kiro_mcp_config_targets_serve_mcp(tmp_path: Path) -> None:
    """The mcp.json entry runs the shared serve-mcp against the bridge dir."""
    bridge_dir = tmp_path / "bridge"
    server = bridge.build_kiro_mcp_config(bridge_dir, python_executable="/usr/bin/python3")[
        "mcpServers"
    ]["omnigent"]
    assert server["command"] == "/usr/bin/python3"
    assert server["args"] == [
        "-I",
        "-m",
        "omnigent.claude_native_bridge",
        "serve-mcp",
        "--bridge-dir",
        str(bridge_dir),
    ]
    # Defaults to the running interpreter when no executable is given.
    default_cmd = bridge.build_kiro_mcp_config(bridge_dir)["mcpServers"]["omnigent"]["command"]
    assert default_cmd == sys.executable


def test_write_kiro_workspace_mcp_config_merges_preserving_user_servers(tmp_path: Path) -> None:
    """The Omnigent server is merged into <workspace>/.kiro/settings/mcp.json
    without clobbering a user's pre-existing workspace servers."""
    workspace = tmp_path / "repo"
    settings = workspace / ".kiro" / "settings"
    settings.mkdir(parents=True)
    (settings / "mcp.json").write_text(
        json.dumps({"mcpServers": {"user_server": {"command": "x", "args": []}}}),
        encoding="utf-8",
    )
    bridge_dir = tmp_path / "bridge"

    path = bridge.write_kiro_workspace_mcp_config(workspace, bridge_dir)

    assert path == workspace / ".kiro" / "settings" / "mcp.json"
    written = json.loads(path.read_text())
    assert set(written["mcpServers"]) == {"user_server", "omnigent"}
    assert "serve-mcp" in written["mcpServers"]["omnigent"]["args"]
    # serve-mcp's token file is written alongside.
    assert (bridge_dir / "bridge.json").exists()


def test_inject_model_command_switches_and_confirms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/model <id>`` is typed literally and confirmed via kiro's success line."""
    monkeypatch.setattr(bridge, "_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(bridge, "_TYPE_SETTLE_S", 0.0)
    bridge_dir = tmp_path / "bridge"
    marker_pane = "Model changed to claude-haiku-4.5 (saved as default)\n" + _READY_PANE
    calls = _install_fake_tmux(monkeypatch, pane_outputs=[_READY_PANE, marker_pane])
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/tmux.sock"), tmux_target="main")

    bridge.inject_model_command(bridge_dir, model="claude-haiku-4.5", timeout_s=0.1)

    sent = [call[-1] for call in calls if "send-keys" in call]
    # Clear the draft (C-a/C-k), send the literal slash command, then Enter.
    assert sent == ["C-a", "C-k", "/model claude-haiku-4.5", "Enter"]


def test_inject_model_command_raises_when_switch_not_confirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing ``Model changed to <id>`` line fails loudly (bad/unavailable id)."""
    monkeypatch.setattr(bridge, "_POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(bridge, "_TYPE_SETTLE_S", 0.0)
    monkeypatch.setattr(bridge, "_MODEL_CONFIRM_TIMEOUT_S", 0.05)
    bridge_dir = tmp_path / "bridge"
    # Pane stays at the input prompt and never shows the confirmation line.
    _install_fake_tmux(monkeypatch, pane_outputs=[_READY_PANE])
    write_tmux_target(bridge_dir, socket_path=Path("/tmp/tmux.sock"), tmux_target="main")

    with pytest.raises(RuntimeError, match="did not confirm the model switch"):
        bridge.inject_model_command(bridge_dir, model="bogus-model", timeout_s=0.1)
