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


def test_enqueue_compact_payload_shape(tmp_path: Path) -> None:
    """A queued compact request round-trips as well-formed JSON the extension reads.

    The extension dispatches on ``payload.type == "compact"`` and feeds
    ``payload.custom_instructions`` (when present) to Pi's
    ``ctx.compact({customInstructions})``. The id must equal the returned
    ``compact_`` id (the extension dedups on it).
    """
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / "inbox").mkdir(parents=True)

    compact_id = pi_native_bridge.enqueue_compact(bridge_dir)

    (path,) = list((bridge_dir / "inbox").glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["id"] == compact_id
    assert compact_id.startswith("compact_")
    assert payload["type"] == "compact"
    assert isinstance(payload["created_at"], (int, float))
    # No instructions given → key omitted so the extension uses Pi's default
    # summarisation rather than passing an empty customInstructions string.
    assert "custom_instructions" not in payload


def test_enqueue_compact_includes_custom_instructions(tmp_path: Path) -> None:
    """Non-blank custom instructions ride along; blank ones are dropped.

    Pi's ``compact({customInstructions})`` focuses the summary on the given
    text. An empty/whitespace string must be dropped so it doesn't override
    Pi's default summarisation with a no-op instruction.
    """
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / "inbox").mkdir(parents=True)

    pi_native_bridge.enqueue_compact(bridge_dir, "focus on the refactor")
    pi_native_bridge.enqueue_compact(bridge_dir, "   ")

    payloads = sorted(
        (json.loads(p.read_text(encoding="utf-8")) for p in (bridge_dir / "inbox").glob("*.json")),
        key=lambda payload: payload["created_at"],
    )
    assert len(payloads) == 2, payloads
    with_instructions = [p for p in payloads if "custom_instructions" in p]
    assert len(with_instructions) == 1, payloads
    assert with_instructions[0]["custom_instructions"] == "focus on the refactor"


def test_enqueue_model_change_payload_shape(tmp_path: Path) -> None:
    """A queued model change round-trips as well-formed JSON the extension reads.

    The extension dispatches on ``payload.type == "model_change"`` and resolves
    ``payload.model`` against ``ctx.modelRegistry`` before calling Pi's
    ``setModel``. The id must equal the returned ``model_change_`` id (the
    extension dedups on it).
    """
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / "inbox").mkdir(parents=True)

    model_change_id = pi_native_bridge.enqueue_model_change(
        bridge_dir, "databricks-claude-sonnet-4-6"
    )

    (path,) = list((bridge_dir / "inbox").glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["id"] == model_change_id
    assert model_change_id.startswith("model_change_")
    assert payload["type"] == "model_change"
    assert payload["model"] == "databricks-claude-sonnet-4-6"
    assert isinstance(payload["created_at"], (int, float))


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


def test_write_extension_files_embeds_tools(tmp_path: Path) -> None:
    """write_extension_files embeds the tool list so the extension can register it.

    The runner builds the session's Omnigent tool surface (sys_* tools) and
    passes it to write_extension_files; the extension reads ``config.tools`` and
    registers each via ``pi.registerTool``. The config must round-trip the list
    verbatim so the schemas reach the Pi agent unchanged.
    """
    bridge_dir = tmp_path / "bridge"
    tools = [
        {
            "name": "sys_os_read",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
        {
            "name": "sys_os_shell",
            "description": "Run a command",
            "parameters": {"type": "object", "properties": {}},
        },
    ]

    _ext, cfg = pi_native_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_abc",
        server_url="http://omnigent.test/",
        conversation_url="http://omnigent.test/c/conv_abc",
        auth_headers={"Authorization": "Bearer t"},
        tools=tools,
    )

    payload = json.loads(cfg.read_text(encoding="utf-8"))
    assert payload["sessionId"] == "conv_abc"
    # serverUrl trailing slash is stripped so the extension can append paths.
    assert payload["serverUrl"] == "http://omnigent.test"
    assert payload["tools"] == tools


def test_write_extension_files_defaults_tools_to_empty(tmp_path: Path) -> None:
    """Omitting tools writes an empty list (Pi runs with its built-ins only)."""
    bridge_dir = tmp_path / "bridge"

    _ext, cfg = pi_native_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_abc",
        server_url="http://omnigent.test",
        conversation_url="http://omnigent.test/c/conv_abc",
    )

    payload = json.loads(cfg.read_text(encoding="utf-8"))
    assert payload["tools"] == []


def test_refresh_config_auth_headers_replaces_only_auth(tmp_path: Path) -> None:
    """Refreshing the bearer rewrites only ``authHeaders``, leaving the rest.

    The baked launch token dies with the ~1h Databricks OAuth lifetime; the
    runner re-mints it into ``config.json`` each turn and the extension
    re-reads per request. The rewrite must touch only ``authHeaders`` so
    ``serverUrl`` / ``tools`` (read once at startup) stay intact.
    """
    bridge_dir = tmp_path / "bridge"
    pi_native_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_abc",
        server_url="http://omnigent.test",
        conversation_url="http://omnigent.test/c/conv_abc",
        auth_headers={"Authorization": "Bearer stale"},
        tools=[{"name": "t"}],
    )

    assert (
        pi_native_bridge.refresh_config_auth_headers(bridge_dir, {"Authorization": "Bearer fresh"})
        is True
    )
    payload = json.loads(pi_native_bridge.config_path(bridge_dir).read_text(encoding="utf-8"))
    assert payload["authHeaders"] == {"Authorization": "Bearer fresh"}
    # Untouched: the static fields the extension reads once at startup.
    assert payload["serverUrl"] == "http://omnigent.test"
    assert payload["tools"] == [{"name": "t"}]


def test_refresh_config_auth_headers_noops(tmp_path: Path) -> None:
    """No-op (returns False) on empty headers, a missing config, or no change."""
    bridge_dir = tmp_path / "bridge"
    # Missing config: nothing to rewrite.
    assert pi_native_bridge.refresh_config_auth_headers(bridge_dir, {"a": "b"}) is False

    pi_native_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_abc",
        server_url="http://omnigent.test",
        conversation_url="http://omnigent.test/c/conv_abc",
        auth_headers={"Authorization": "Bearer x"},
    )
    # Empty headers (local/unauthenticated) must never blank out a live token.
    assert pi_native_bridge.refresh_config_auth_headers(bridge_dir, {}) is False
    payload = json.loads(pi_native_bridge.config_path(bridge_dir).read_text(encoding="utf-8"))
    assert payload["authHeaders"] == {"Authorization": "Bearer x"}
    # Identical headers: skip the rewrite.
    assert (
        pi_native_bridge.refresh_config_auth_headers(bridge_dir, {"Authorization": "Bearer x"})
        is False
    )


def test_refresh_config_auth_headers_preserves_launch_written_headers(tmp_path: Path) -> None:
    """Bearer refresh merges over existing headers; launch-written extras survive.

    On guest-on-shared-host runners the extension config is written at launch
    with both the OAuth bearer and an ``X-Omnigent-Runner-Tunnel-Token`` header
    needed for the extension's ``/events`` POSTs to be authorised as self-access.
    The per-turn bearer refresh must not wipe that header — it only knows about
    the fresh bearer, not the tunnel token.
    """
    bridge_dir = tmp_path / "bridge"
    pi_native_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_abc",
        server_url="http://omnigent.test",
        conversation_url="http://omnigent.test/c/conv_abc",
        auth_headers={
            "Authorization": "Bearer stale",
            "X-Omnigent-Runner-Tunnel-Token": "tunnel-tok",
        },
    )

    assert (
        pi_native_bridge.refresh_config_auth_headers(bridge_dir, {"Authorization": "Bearer fresh"})
        is True
    )

    payload = json.loads(pi_native_bridge.config_path(bridge_dir).read_text(encoding="utf-8"))
    # Bearer is updated to the fresh value.
    assert payload["authHeaders"]["Authorization"] == "Bearer fresh"
    # Tunnel token written at launch is preserved across the bearer rotation.
    assert payload["authHeaders"]["X-Omnigent-Runner-Tunnel-Token"] == "tunnel-tok"
