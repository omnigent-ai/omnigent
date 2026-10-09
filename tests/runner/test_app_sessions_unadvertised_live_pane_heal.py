"""Restore a live Claude pane's missing or stale advertisement before message injection."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native.bridge import (
    _TMUX_FILE,
    bridge_dir_for_conversation_id,
    read_tmux_target,
    tmux_target_advertised,
    write_tmux_target,
)
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner.native.orchestration import _readvertise_live_claude_tmux_target
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import _runner_client
from tests.runner.test_app_sessions_native_session_change_heal import (
    _open_claude_native_session,
    _plant_live_claude_pane,
)


def _advertisement_of(instance: TerminalInstance) -> dict[str, str]:
    return {"socket_path": str(instance.socket_path), "tmux_target": instance.tmux_target}


def _inject_requiring_advertisement(
    captured: list[tuple[str, dict[str, str]]],
):
    """Stand in for ``inject_slash_command`` while keeping its advertisement wait."""

    def _inject(
        inject_bridge_dir: Path,
        *,
        command: str,
        timeout_s: float,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del timeout_s, auto_confirm, confirm_hint
        info = claude_native_bridge._wait_for_tmux_info(inject_bridge_dir, timeout_s=0.2)
        captured.append((command, info))

    return _inject


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
    """Restore a live pane's missing advertisement before injecting, without recreating it."""
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
    (bridge_dir / _TMUX_FILE).unlink()

    captured: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        claude_native_bridge, "inject_slash_command", _inject_requiring_advertisement(captured)
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
    assert captured == [(expected_command, _advertisement_of(instance))], (
        f"inject must see the live pane's re-advertised target; got {captured!r}"
    )
    # The heal is durable: the advertisement survives for subsequent injects.
    assert tmux_target_advertised(bridge_dir)


@pytest.mark.asyncio
async def test_stale_advertisement_is_rewritten_for_the_live_pane(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An advertisement naming another socket or target is replaced by the live pane's."""
    conv_id = "60718293a4b5c6d7e8f901a2b3c4d5e6"
    auto_create_calls: list[str] = []
    app, registry = await _open_claude_native_session(
        monkeypatch, conv_id=conv_id, auto_create_calls=auto_create_calls
    )
    auto_create_calls.clear()
    bridge_dir = bridge_dir_for_conversation_id(conv_id)
    instance = _plant_live_claude_pane(registry, conv_id, tmp_path, bridge_dir)
    # A stale file left by an earlier pane: valid fields, dead socket.
    write_tmux_target(
        bridge_dir, socket_path=tmp_path / "gone" / "tmux.sock", tmux_target="claude:0.0"
    )
    assert read_tmux_target(bridge_dir) != _advertisement_of(instance)

    captured: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        claude_native_bridge, "inject_slash_command", _inject_requiring_advertisement(captured)
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "model_change", "model": "claude-opus-4-7"},
        )

    assert resp.status_code == 204, resp.text
    assert auto_create_calls == []
    assert captured == [("/model claude-opus-4-7", _advertisement_of(instance))], captured


@pytest.mark.asyncio
async def test_failed_readvertise_keeps_pane_and_surfaces_inject_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A repair that cannot write the advertisement leaves the inject's own failure.

    The heal is best-effort: the live pane must not be recreated, and the
    injection still reports that the target is not advertised.
    """
    conv_id = "18293a4b5c6d7e8f901a2b3c4d5e6f70"
    auto_create_calls: list[str] = []
    app, registry = await _open_claude_native_session(
        monkeypatch, conv_id=conv_id, auto_create_calls=auto_create_calls
    )
    auto_create_calls.clear()
    bridge_dir = bridge_dir_for_conversation_id(conv_id)
    _plant_live_claude_pane(registry, conv_id, tmp_path, bridge_dir)
    (bridge_dir / _TMUX_FILE).unlink()

    def _write_fails(*_args: object, **_kwargs: object) -> None:
        raise OSError("bridge directory is read-only")

    monkeypatch.setattr(claude_native_bridge, "write_tmux_target", _write_fails)
    captured: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(
        claude_native_bridge, "inject_slash_command", _inject_requiring_advertisement(captured)
    )

    async with _runner_client(app) as client:
        resp = await client.post(
            f"/v1/sessions/{conv_id}/events",
            json={"type": "model_change", "model": "claude-opus-4-7"},
        )

    assert resp.status_code == 503, resp.text
    assert captured == [], "the inject must have waited on the still-missing advertisement"
    assert auto_create_calls == [], "a failed repair must not recreate the live pane"
    assert not tmux_target_advertised(bridge_dir)


@pytest.mark.asyncio
async def test_live_advertised_pane_advertisement_is_left_untouched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An advertisement that already names the live pane must not be rewritten."""
    conv_id = "0718293a4b5c6d7e8f901a2b3c4d5e6f"
    auto_create_calls: list[str] = []
    app, registry = await _open_claude_native_session(
        monkeypatch, conv_id=conv_id, auto_create_calls=auto_create_calls
    )
    auto_create_calls.clear()
    bridge_dir = bridge_dir_for_conversation_id(conv_id)
    instance = _plant_live_claude_pane(registry, conv_id, tmp_path, bridge_dir)
    # The planter advertises a different target; publish the pane's own first.
    write_tmux_target(
        bridge_dir, socket_path=instance.socket_path, tmux_target=instance.tmux_target
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


def test_repair_does_not_overwrite_a_replacement_pane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A repair paused after its missing-file check must not publish over a successor.

    Between the check and the publication, pane A is deleted and pane B is
    launched and advertised. The publication runs through
    ``TerminalRegistry.publish_if_registered``, so A's stale target is dropped
    and B stays advertised.
    """
    conv_id = "293a4b5c6d7e8f901a2b3c4d5e6f7081"
    registry = TerminalRegistry()
    bridge_dir = bridge_dir_for_conversation_id(conv_id)
    bridge_dir.mkdir(parents=True, exist_ok=True)
    pane_a = _plant_live_claude_pane(registry, conv_id, tmp_path / "a", bridge_dir)
    (bridge_dir / _TMUX_FILE).unlink()
    real_read = claude_native_bridge.read_tmux_target
    replacement: list[TerminalInstance] = []

    def _read_then_replace_pane(read_bridge_dir: Path) -> dict[str, str] | None:
        missing = real_read(read_bridge_dir)
        # The pause point: A is closed and B launched and advertised before A publishes.
        pane_b = _plant_live_claude_pane(registry, conv_id, tmp_path / "b", bridge_dir)
        write_tmux_target(
            bridge_dir, socket_path=pane_b.socket_path, tmux_target=pane_b.tmux_target
        )
        replacement.append(pane_b)
        return missing

    monkeypatch.setattr(claude_native_bridge, "read_tmux_target", _read_then_replace_pane)

    _readvertise_live_claude_tmux_target(
        bridge_dir, pane_a, terminal_registry=registry, session_id=conv_id
    )

    assert registry.get(conv_id, "claude", "main") is replacement[0]
    assert real_read(bridge_dir) == _advertisement_of(replacement[0]), (
        "the retired pane must not advertise over its replacement"
    )
