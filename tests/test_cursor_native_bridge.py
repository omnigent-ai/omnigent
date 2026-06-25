"""Unit tests for :mod:`omnigent.cursor_native_bridge` composer handling.

Focused on the leftover-draft clear (:func:`_clear_composer`) and its use by
:func:`inject_user_message`. cursor-agent restores the interrupted prompt into
the composer when a turn is cancelled (web-UI Stop -> ``inject_interrupt`` sends
``Escape``), and its input widget ignores the readline ``C-a``/``C-k`` keys the
clear used to send — so the leftover survived and prepended the next message.
The clear now floods ``Backspace`` until the pane stops changing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent import cursor_native_bridge
from omnigent.cursor_native_bridge import write_tmux_target

_SOCK = "/tmp/example/cursor.sock"
_TARGET = "cursor:0.0"


class _FakeCompleted:
    """Minimal stand-in for ``subprocess.CompletedProcess``."""

    def __init__(self, stdout: str = "") -> None:
        self.returncode = 0
        self.stdout = stdout
        self.stderr = ""


def _install_fake_tmux(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pane_captures: list[str],
) -> list[list[str]]:
    """Patch ``subprocess.run`` so tmux is mocked.

    ``send-keys`` calls are recorded (and returned). ``capture-pane`` calls pop
    the next value from *pane_captures* (the last value repeats once exhausted,
    modelling a composer that has settled).

    :returns: The list that accumulates every tmux argv invoked.
    """
    captured: list[list[str]] = []
    remaining = list(pane_captures)

    def _fake_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
        del kwargs
        captured.append(cmd)
        if "capture-pane" in cmd:
            value = remaining.pop(0) if len(remaining) > 1 else (remaining[0] if remaining else "")
            return _FakeCompleted(stdout=value)
        return _FakeCompleted()

    monkeypatch.setattr("subprocess.run", _fake_run)
    return captured


def _send_keys_calls(captured: list[list[str]]) -> list[list[str]]:
    """The send-keys argv tails (everything after ``send-keys``)."""
    return [cmd[cmd.index("send-keys") + 1 :] for cmd in captured if "send-keys" in cmd]


def test_clear_composer_floods_backspace_until_pane_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The clear sends End, then Backspace bursts, stopping once the pane settles.

    Two distinct captures (draft shrinking) then a repeat (empty) means three
    bursts: the third sees no change and returns. It must never send the old
    ``C-a``/``C-k`` keys, which cursor-agent's composer ignores.
    """
    # previous=capture#1 (draft); round1 sees #2 (empty, changed -> continue);
    # round2 sees #3 (empty, unchanged -> stop).
    captured = _install_fake_tmux(
        monkeypatch,
        pane_captures=["draft-content", "empty", "empty"],
    )

    cursor_native_bridge._clear_composer(_SOCK, _TARGET)

    tails = _send_keys_calls(captured)
    # End once, then one Backspace burst per round until stable.
    assert tails[0] == ["-t", _TARGET, "End"]
    bursts = [t for t in tails if "BSpace" in t]
    assert all(
        t == ["-t", _TARGET, "-N", str(cursor_native_bridge._COMPOSER_CLEAR_CHUNK), "BSpace"]
        for t in bursts
    )
    assert len(bursts) == 2, f"expected to stop once the pane stabilized; got {len(bursts)}"
    # The removed, ineffective readline clears must not come back.
    assert not any("C-a" in t or "C-k" in t for t in tails)


def test_clear_composer_terminates_on_empty_composer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-empty composer settles immediately (one harmless burst).

    A burst on empty input is a no-op (unlike ``C-c``, which would arm exit), so
    the clear is safe to run before every injection. The pane never changes, so
    the first burst's capture matches and the loop returns at once — never
    reaching the round cap.
    """
    captured = _install_fake_tmux(monkeypatch, pane_captures=["idle-placeholder"])

    cursor_native_bridge._clear_composer(_SOCK, _TARGET)

    bursts = [t for t in _send_keys_calls(captured) if "BSpace" in t]
    assert len(bursts) == 1, "empty composer should settle after a single burst"


def test_clear_composer_is_bounded_when_pane_never_settles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pane that keeps changing is bounded by the round cap, not infinite."""

    counter = {"n": 0}

    def _fake_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
        del kwargs
        if "capture-pane" in cmd:
            counter["n"] += 1
            return _FakeCompleted(stdout=f"ever-changing-{counter['n']}")
        return _FakeCompleted()

    monkeypatch.setattr("subprocess.run", _fake_run)

    cursor_native_bridge._clear_composer(_SOCK, _TARGET)

    # One capture seeds `previous`, then one per round up to the cap.
    assert counter["n"] == cursor_native_bridge._COMPOSER_CLEAR_MAX_ROUNDS + 1


def test_inject_user_message_clears_before_pasting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The leftover-draft clear runs before the paste, so it can't survive it.

    Regression for the reported bug: pressing Stop left the previous message in
    the composer, which then prepended (blocked) the next web-UI message. The
    Backspace flood must precede ``load-buffer``/``paste-buffer``.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path(_SOCK),
        tmux_target=_TARGET,
    )
    # Pane always reports an idle marker: _settle_pane returns immediately, the
    # clear settles at once, and the paste-commit poll sees the needle.
    captured = _install_fake_tmux(monkeypatch, pane_captures=["Add a follow-up hello marker"])
    # Avoid real sleeps in the paste-commit settle.
    monkeypatch.setattr(cursor_native_bridge.time, "sleep", lambda *_a, **_k: None)

    cursor_native_bridge.inject_user_message(bridge_dir, content="hello marker")

    first_backspace = next(
        i for i, cmd in enumerate(captured) if "send-keys" in cmd and "BSpace" in cmd
    )
    first_paste = next(i for i, cmd in enumerate(captured) if "paste-buffer" in cmd)
    assert first_backspace < first_paste, "draft must be cleared before the new paste"
    # The old ineffective readline clear is gone.
    assert not any("C-a" in cmd or "C-k" in cmd for cmd in captured)
    # Submit still happens last.
    assert any("send-keys" in cmd and "Enter" in cmd for cmd in captured)
