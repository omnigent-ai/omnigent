"""Unit tests for the goose-native executor's web→TUI injection (fork preamble)."""

from __future__ import annotations

from pathlib import Path

from omnigent.cursor_native_bridge import read_fork_preamble, write_fork_preamble
from omnigent.inner import goose_native_executor as ge


async def _drain(it) -> list[object]:
    return [event async for event in it]


async def test_run_turn_injects_fork_preamble_then_consumes_it(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        ge,
        "inject_user_message",
        lambda _bridge_dir, *, content: captured.__setitem__("text", content),
    )
    write_fork_preamble(tmp_path, "You: hi\n\nAssistant: hello")

    ex = ge.GooseNativeExecutor(bridge_dir=tmp_path)
    events = await _drain(ex.run_turn([{"role": "user", "content": "real question"}], [], ""))

    # The first injected message carries the fenced prior history + the user text.
    assert "omnigent_fork_history" in captured["text"]
    assert "You: hi" in captured["text"]
    assert "real question" in captured["text"]
    # Consumed only after a successful inject, so a retry would keep it.
    assert read_fork_preamble(tmp_path) is None
    assert any(type(e).__name__ == "TurnComplete" for e in events)


async def test_run_turn_without_preamble_injects_plain_text(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, str] = {}
    monkeypatch.setattr(
        ge,
        "inject_user_message",
        lambda _bridge_dir, *, content: captured.__setitem__("text", content),
    )
    ex = ge.GooseNativeExecutor(bridge_dir=tmp_path)
    await _drain(ex.run_turn([{"role": "user", "content": "hello"}], [], ""))
    assert captured["text"] == "hello"
    assert "omnigent_fork_history" not in captured["text"]
