"""Bridge utilities for native Pi TUI sessions."""

from __future__ import annotations

import contextlib
import hashlib
import itertools
import json
import os
import tempfile
import time
import uuid
from importlib.resources import files
from pathlib import Path
from typing import Any

# Per-process tiebreaker for inbox ordering. The extension delivers inbox
# files in lexicographic filename order, so a high-resolution timestamp alone
# can still tie when two payloads are queued within the same nanosecond; the
# counter disambiguates them in enqueue order.
_ENQUEUE_SEQUENCE = itertools.count()

PI_NATIVE_BRIDGE_DIR_ENV_VAR = "HARNESS_PI_NATIVE_BRIDGE_DIR"
PI_NATIVE_REQUEST_SESSION_ID_ENV_VAR = "HARNESS_PI_NATIVE_REQUEST_SESSION_ID"
PI_NATIVE_CONFIG_ENV_VAR = "OMNIGENT_PI_NATIVE_CONFIG"

_BRIDGE_ROOT = Path.home() / ".omnigent" / "pi-native"
_CONFIG_FILE = "config.json"
_EXTENSION_FILE = "omnigent_pi_native_extension.js"
_EXTENSION_PACKAGE = "omnigent.resources.pi_native"
_INBOX_DIR = "inbox"
_SESSIONS_DIR = "sessions"


def bridge_dir_for_session_id(session_id: str) -> Path:
    """
    Return the bridge directory for a native Pi session.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :returns: Absolute bridge directory under ``~/.omnigent/pi-native``.
    """
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    return _BRIDGE_ROOT / digest


def prepare_bridge_dir(session_id: str) -> Path:
    """
    Create the bridge directory and inbox for *session_id*.

    :param session_id: Omnigent conversation id.
    :returns: Prepared bridge directory.
    """
    bridge_dir = bridge_dir_for_session_id(session_id)
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(bridge_dir, 0o700)
    (bridge_dir / _INBOX_DIR).mkdir(mode=0o700, exist_ok=True)
    (bridge_dir / _SESSIONS_DIR).mkdir(mode=0o700, exist_ok=True)
    return bridge_dir


def clear_inbox(bridge_dir: Path) -> None:
    """
    Drop leftover inbox payloads from a previous Pi process.

    A freshly launched Pi process has an empty in-memory dedup set, so any
    payload a prior process left undelivered would replay into it. Call on
    terminal (re)launch — mirroring codex-native's ``clear_bridge_state``.
    Best-effort: a concurrent drain removing a file first is benign.

    :param bridge_dir: The session's prepared bridge directory.
    :returns: None.
    """
    inbox = bridge_dir / _INBOX_DIR
    if not inbox.is_dir():
        return
    for entry in inbox.iterdir():
        if entry.is_file():
            with contextlib.suppress(OSError):
                entry.unlink()


def build_pi_native_spawn_env(conversation_id: str) -> dict[str, str]:
    """
    Build spawn env for the ``pi-native`` harness process.

    :param conversation_id: Omnigent conversation id.
    :returns: Environment variables needed by the Pi-native harness executor.
    """
    return {
        PI_NATIVE_BRIDGE_DIR_ENV_VAR: str(bridge_dir_for_session_id(conversation_id)),
        PI_NATIVE_REQUEST_SESSION_ID_ENV_VAR: conversation_id,
    }


def pi_session_dir(bridge_dir: Path) -> Path:
    """
    Return the Pi session directory under *bridge_dir*.

    :param bridge_dir: Prepared Pi bridge directory.
    :returns: Directory passed to ``pi --session-dir``.
    """
    path = bridge_dir / _SESSIONS_DIR
    path.mkdir(mode=0o700, exist_ok=True)
    return path


def extension_path(bridge_dir: Path) -> Path:
    """Return the generated Pi extension path for *bridge_dir*."""
    return bridge_dir / _EXTENSION_FILE


def config_path(bridge_dir: Path) -> Path:
    """Return the generated Pi extension config path for *bridge_dir*."""
    return bridge_dir / _CONFIG_FILE


def enqueue_user_message(bridge_dir: Path, content: str) -> str:
    """
    Queue a web-originated user message for the resident Pi extension.

    The extension polls ``inbox/*.json`` and deletes each file after
    handing it to ``pi.sendUserMessage``. Writing through a temporary file
    plus atomic rename keeps the poller from reading partial JSON.

    :param bridge_dir: Native Pi bridge directory.
    :param content: Plain text user message.
    :returns: Opaque message id.
    """
    message_id = f"msg_{uuid.uuid4().hex}"
    payload = {
        "id": message_id,
        "type": "user_message",
        "content": content,
        "created_at": time.time(),
    }
    _enqueue_payload(bridge_dir, message_id, payload)
    return message_id


def enqueue_interrupt(bridge_dir: Path) -> str:
    """
    Queue a UI-originated interrupt for the resident Pi extension.

    Pi-native turns run inside the already-open TUI process, so the runner
    cannot cancel them by cancelling its short-lived harness task. The
    extension consumes this inbox payload and calls Pi's active
    ``ExtensionContext.abort()``.

    :param bridge_dir: Native Pi bridge directory.
    :returns: Opaque interrupt id.
    """
    interrupt_id = f"interrupt_{uuid.uuid4().hex}"
    payload = {
        "id": interrupt_id,
        "type": "interrupt",
        "created_at": time.time(),
    }
    _enqueue_payload(bridge_dir, interrupt_id, payload)
    return interrupt_id


def enqueue_compact(bridge_dir: Path, custom_instructions: str | None = None) -> str:
    """
    Queue a UI-originated ``/compact`` for the resident Pi extension.

    Pi owns its own context window inside the already-open TUI process, so
    explicit compaction must run there rather than as AP-side compaction
    (which would only summarise the transcript mirror and desync the two
    context windows). Mirrors :func:`enqueue_interrupt`: the extension
    consumes this inbox payload in the Pi process and calls Pi's active
    ``ExtensionContext.compact()`` (see ``docs/compaction.md`` / ``ctx.compact``
    in the pi-coding-agent extension API), which summarises older context and
    appends a ``CompactionEntry`` to the Pi session.

    :param bridge_dir: Native Pi bridge directory.
    :param custom_instructions: Optional text passed to Pi's
        ``compact({customInstructions})`` to focus the summary. Empty/blank
        values are dropped so the extension uses Pi's default summarisation.
    :returns: Opaque compact id.
    """
    compact_id = f"compact_{uuid.uuid4().hex}"
    payload: dict[str, Any] = {
        "id": compact_id,
        "type": "compact",
        "created_at": time.time(),
    }
    if isinstance(custom_instructions, str) and custom_instructions.strip():
        payload["custom_instructions"] = custom_instructions
    _enqueue_payload(bridge_dir, compact_id, payload)
    return compact_id


def _enqueue_payload(bridge_dir: Path, item_id: str, payload: dict[str, Any]) -> None:
    inbox = bridge_dir / _INBOX_DIR
    inbox.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Order-preserving filename. The extension polls ``inbox/*.json`` and
    # delivers them in lexicographic order, but ``item_id`` is a random uuid
    # with no time ordering — and an ``interrupt_`` id sorts before a ``msg_``
    # id regardless of which was queued first. Prefix with a zero-padded
    # nanosecond timestamp plus a per-process counter so the on-disk sort
    # matches enqueue order. The payload's ``id`` (used by the extension for
    # dedup) stays ``item_id``; only the filename carries the ordinal.
    ordinal = f"{time.time_ns():020d}_{next(_ENQUEUE_SEQUENCE):08d}"
    file_stem = f"{ordinal}_{item_id}"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{file_stem}.", suffix=".tmp", dir=str(inbox))
    final_path = inbox / f"{file_stem}.json"
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, final_path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def write_extension_files(
    bridge_dir: Path,
    *,
    session_id: str,
    server_url: str,
    conversation_url: str,
    auth_headers: dict[str, str] | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> tuple[Path, Path]:
    """
    Write the Pi extension and config used by a native Pi terminal.

    :param bridge_dir: Prepared bridge directory.
    :param session_id: Omnigent conversation id.
    :param server_url: Omnigent server base URL.
    :param conversation_url: Human-facing web conversation URL.
    :param auth_headers: HTTP headers the extension should use when posting
        terminal-originated events back to Omnigent.
    :param tools: Flat Omnigent tool schemas
        (``[{"name", "description", "parameters"}]``) the extension should
        register via ``pi.registerTool``. Each tool's ``execute`` round-trips
        the call through ``POST /v1/sessions/{id}/mcp`` (the same MCP proxy the
        runner uses), so the Pi agent can invoke Omnigent ``sys_*`` tools with
        centralized server-side policy enforcement. ``None``/empty registers no
        tools (Pi falls back to its own built-in tool surface only).
    :returns: ``(extension_path, config_path)``.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "sessionId": session_id,
        "serverUrl": server_url.rstrip("/"),
        "conversationUrl": conversation_url,
        "bridgeDir": str(bridge_dir),
        "inboxDir": str(bridge_dir / _INBOX_DIR),
        "authHeaders": auth_headers or {},
        "tools": tools or [],
    }
    _atomic_json(config_path(bridge_dir), payload)
    _atomic_text(extension_path(bridge_dir), _extension_source())
    return extension_path(bridge_dir), config_path(bridge_dir)


def _extension_source() -> str:
    """
    Return the packaged Pi extension source.

    Keeping the JavaScript in a resource file makes the extension lintable and
    reviewable without embedding JS syntax in a Python raw string.
    """
    resource = files(_EXTENSION_PACKAGE).joinpath(_EXTENSION_FILE)
    return resource.read_text(encoding="utf-8")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _atomic_text(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
