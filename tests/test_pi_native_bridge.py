"""Tests for omnigent.pi_native_bridge inbox enqueue contract."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from omnigent import pi_native_bridge


def _inbox_files(bridge_dir: Path) -> list[str]:
    """Return the inbox's ``*.json`` filenames in the extension's read order.

    The Pi extension delivers files via ``readdirSync(...).sort()`` — plain
    lexicographic order — so sorting the names here mirrors delivery order.

    :param bridge_dir: A prepared Pi bridge directory.
    :returns: Sorted inbox filenames.
    """
    return sorted(p.name for p in (bridge_dir / "inbox").glob("*.json"))


def test_enqueue_preserves_send_order_across_types(tmp_path: Path) -> None:
    """Inbox filenames sort in enqueue order regardless of payload type.

    The extension delivers inbox files in lexicographic order. The payload id
    is a random uuid (no time ordering), and an ``interrupt_`` id sorts before
    a ``msg_`` id — so without an ordering prefix a message queued before an
    interrupt would be delivered *after* it. This pins that send order is
    preserved even when a message is followed by an interrupt.
    """
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / "inbox").mkdir(parents=True)

    first = pi_native_bridge.enqueue_user_message(bridge_dir, "first")
    interrupt = pi_native_bridge.enqueue_interrupt(bridge_dir)
    second = pi_native_bridge.enqueue_user_message(bridge_dir, "second")

    files = _inbox_files(bridge_dir)
    assert len(files) == 3, files
    # Delivery order must match enqueue order, not the lexicographic order of
    # the raw ids (where "interrupt_" would jump ahead of "msg_").
    assert first in files[0], files
    assert interrupt in files[1], files
    assert second in files[2], files


def test_enqueue_user_message_payload_shape(tmp_path: Path) -> None:
    """A queued user message round-trips as well-formed JSON the extension reads.

    The extension dedups on ``payload.id`` and delivers ``payload.content`` via
    ``pi.sendUserMessage``, so the id must equal the returned ``msg_`` id and
    the content must be preserved verbatim.
    """
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / "inbox").mkdir(parents=True)

    message_id = pi_native_bridge.enqueue_user_message(bridge_dir, "hello world")

    (path,) = list((bridge_dir / "inbox").glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["id"] == message_id
    assert payload["type"] == "user_message"
    assert payload["content"] == "hello world"
    assert isinstance(payload["created_at"], (int, float))


def test_enqueue_leaves_no_partial_tmp_files(tmp_path: Path) -> None:
    """Only the final ``.json`` lands in the inbox — no ``.tmp`` residue.

    The atomic temp-write-then-rename exists so the 250 ms poller never reads a
    half-written file. A leftover ``.tmp`` (or a non-``.json`` name) would mean
    the rename contract regressed.
    """
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / "inbox").mkdir(parents=True)

    pi_native_bridge.enqueue_user_message(bridge_dir, "x")
    pi_native_bridge.enqueue_interrupt(bridge_dir)

    names = [p.name for p in (bridge_dir / "inbox").iterdir()]
    assert all(n.endswith(".json") for n in names), names
    assert not any(n.endswith(".tmp") for n in names), names


def test_prepare_bridge_dir_is_owner_only(tmp_path: Path, monkeypatch) -> None:
    """The bridge dir and its inbox are created 0o700 (per-session isolation).

    The bearer token written alongside the inbox makes owner-only perms the
    isolation boundary between sessions sharing ``~/.omnigent/pi-native``.
    """
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-native")

    bridge_dir = pi_native_bridge.prepare_bridge_dir("conv_perms")

    assert stat.S_IMODE((bridge_dir).stat().st_mode) == 0o700
    assert stat.S_IMODE((bridge_dir / "inbox").stat().st_mode) == 0o700


def test_clear_inbox_drops_leftover_payloads(tmp_path: Path) -> None:
    """clear_inbox empties the queue so a relaunched Pi process can't replay.

    A fresh Pi process starts with an empty dedup set; without clearing, a
    payload a prior process left behind would replay into the new session.
    """
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / "inbox").mkdir(parents=True)
    pi_native_bridge.enqueue_user_message(bridge_dir, "stale")
    pi_native_bridge.enqueue_interrupt(bridge_dir)
    assert _inbox_files(bridge_dir), "precondition: inbox has payloads"

    pi_native_bridge.clear_inbox(bridge_dir)

    assert _inbox_files(bridge_dir) == []
    # The inbox dir itself survives (only its contents are dropped).
    assert (bridge_dir / "inbox").is_dir()


def test_clear_inbox_is_a_noop_without_an_inbox(tmp_path: Path) -> None:
    """clear_inbox tolerates a bridge dir that has no inbox yet."""
    pi_native_bridge.clear_inbox(tmp_path / "nonexistent")  # must not raise
