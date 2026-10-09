"""Restore a live Claude pane's missing advertisement before message injection."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import (
    _TMUX_FILE,
    bridge_dir_for_conversation_id,
    tmux_target_advertised,
)
from tests.runner.conftest import _runner_client
from tests.runner.test_app_sessions_native_session_change_heal import (
    _open_claude_native_session,
    _plant_live_claude_pane,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "expected_command"),
    [
        ({"type": "model_change", "model": "claude-opus-4-7"}, "/model claude-opus-4-7"),
        ({"type": "effort_change", "effort": "high"}, "/effort high"),
    ],
    ids=["model_change", "effort_change"],
)
async def test_live_unadvertised_pane_is_readvertised_before_inject(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    event: dict[str, Any],
    expected_command: str,
) -> None:
    """Live pane + missing tmux.json must re-advertise and inject, not fail.

    ``inject_slash_command`` keeps the real advertisement dependency (it
    waits on ``tmux.json`` exactly like the production inject), so without
    the re-advertise heal the handler answers 503 -- the same
    "tmux target is not advertised" hard-fail the web turn surfaced.
    """
    conv_id = "e5f60718293a4b5c6d7e8f901a2b3c4d"
    if event["type"] == "effort_change":
        conv_id = "f60718293a4b5c6d7e8f901a2b3c4d5e"
    auto_create_calls: list[str] = []
    app, registry = await _open_claude_native_session(
        monkeypatch, conv_id=conv_id, auto_create_calls=auto_create_calls
    )
    auto_create_calls.clear()
    bridge_dir = bridge_dir_for_conversation_id(conv_id)
    instance = _plant_live_claude_pane(registry, conv_id, tmp_path, bridge_dir)
    # The fault: the pane is alive but nothing advertises its tmux target.
    (bridge_dir / _TMUX_FILE).unlink()

    captured: list[tuple[str, dict[str, str]]] = []

    def _inject_requires_advertisement(
        inject_bridge_dir: Path,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del timeout_s, auto_confirm, confirm_hint
        # Keep the real inject's advertisement dependency: this raises
        # TmuxSessionNotAdvertised when tmux.json is still missing.
        info = claude_native_bridge._wait_for_tmux_info(inject_bridge_dir, timeout_s=0.2)
        captured.append((command, info))

    monkeypatch.setattr(
        claude_native_bridge, "inject_slash_command", _inject_requires_advertisement
    )

    async with _runner_client(app) as client:
        resp = await client.post(f"/v1/sessions/{conv_id}/events", json=event)

    assert resp.status_code == 204, (
        f"live-but-unadvertised pane must be re-advertised before inject; "
        f"got {resp.status_code}: {resp.text}"
    )
    assert auto_create_calls == [], (
        "a live pane must be re-advertised in place, never torn down and recreated"
    )
    assert captured == [
        (
            expected_command,
            {"socket_path": str(instance.socket_path), "tmux_target": instance.tmux_target},
        )
    ], f"inject must see the live pane's re-advertised target; got {captured!r}"
    # The heal is durable: the advertisement survives for subsequent injects.
    assert tmux_target_advertised(bridge_dir)


@pytest.mark.asyncio
async def test_live_advertised_pane_advertisement_is_left_untouched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A live pane whose advertisement is present must not be rewritten."""
    conv_id = "0718293a4b5c6d7e8f901a2b3c4d5e6f"
    auto_create_calls: list[str] = []
    app, registry = await _open_claude_native_session(
        monkeypatch, conv_id=conv_id, auto_create_calls=auto_create_calls
    )
    auto_create_calls.clear()
    bridge_dir = bridge_dir_for_conversation_id(conv_id)
    instance = _plant_live_claude_pane(registry, conv_id, tmp_path, bridge_dir)
    # Advertise a valid target up front; the heal must keep it verbatim.
    claude_native_bridge.write_tmux_target(
        bridge_dir,
        socket_path=instance.socket_path,
        tmux_target=instance.tmux_target,
    )
    advertised_before = (bridge_dir / _TMUX_FILE).read_text(encoding="utf-8")

    def _inject_records(
        inject_bridge_dir: Path,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del inject_bridge_dir, command, timeout_s, auto_confirm, confirm_hint

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _inject_records)

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "model_change", "model": "claude-opus-4-7"},
        )

    assert resp.status_code == 204, resp.text
    assert auto_create_calls == []
    assert (bridge_dir / _TMUX_FILE).read_text(encoding="utf-8") == advertised_before, (
        "a valid advertisement must not be rewritten on every inject"
    )


def _browser_test_module():
    """Import the browser regression lazily; its module needs Playwright."""
    pytest.importorskip("playwright.sync_api")
    from tests.e2e_ui.chat import test_web_turn_unadvertised_tmux_target as browser_test

    return browser_test


def _advertised_bridge(root: Path, name: str, session_id: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / claude_native_bridge._CONFIG_FILE).write_text(
        json.dumps({"active_session_id": session_id}), encoding="utf-8"
    )
    target = directory / _TMUX_FILE
    target.write_text("synthetic advertisement", encoding="utf-8")
    return target


def test_browser_fault_injection_only_removes_its_session_advertisement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    browser_test = _browser_test_module()

    own = _advertised_bridge(tmp_path, "own", "fixture-session")
    other = _advertised_bridge(tmp_path, "other", "unrelated-session")
    os.utime(own, (1, 1))
    other_content = other.read_bytes()
    monkeypatch.setattr(browser_test, "_BRIDGE_ROOT", tmp_path)

    assert browser_test._remove_tmux_advertisement("fixture-session") == str(own)
    assert not own.exists()
    assert other.read_bytes() == other_content


@pytest.mark.parametrize("own_advertisements", [0, 2], ids=["missing", "ambiguous"])
def test_browser_fault_injection_requires_one_owned_advertisement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, own_advertisements: int
) -> None:
    browser_test = _browser_test_module()

    targets = [_advertised_bridge(tmp_path, "other", "unrelated-session")]
    targets.extend(
        _advertised_bridge(tmp_path, f"own-{i}", "fixture-session")
        for i in range(own_advertisements)
    )
    contents = {target: target.read_bytes() for target in targets}
    monkeypatch.setattr(browser_test, "_BRIDGE_ROOT", tmp_path)

    with pytest.raises(AssertionError, match="exactly one advertisement"):
        browser_test._remove_tmux_advertisement("fixture-session")
    assert {target: target.read_bytes() for target in targets} == contents
