"""Rebuild a native Pi session JSONL from committed Omnigent items.

Pi-native fork/resume parity with claude-native / codex-native.

Like Claude Code's ``~/.claude/projects/<cwd>/<sid>.jsonl`` and Codex's
``$CODEX_HOME/sessions/.../rollout-*-<thread>.jsonl``, the Pi CLI persists
each conversation as a local JSONL session file and re-reads it on resume.
Pi's CLI exposes this directly:

- ``pi --session-dir <dir> --session <id>`` resumes the session whose id
  matches ``<id>`` under ``<dir>``.
- The on-disk format is documented JSONL (one ``session`` header line, then
  ``message`` / tool entries linked by ``id`` / ``parentId``). See
  ``@earendil-works/pi-coding-agent/docs/session-format.md``.

So pi-native CAN carry chat history into a fork/resume after all: synthesize
the session JSONL from Omnigent items and write it where ``--session`` looks.
This is the same "rebuild the native session file the CLI expects, write it
before launch, let the CLI discover it on startup" mechanism claude-native and
codex-native use.

Critically, Pi only fires ``message_start`` / ``message_end`` / ``tool_call``
events for messages produced *inside a live agent loop* (after ``agent_start``)
— NOT for entries loaded from a session file on ``session_start`` (see the
lifecycle in ``docs/extensions.md``). So the Omnigent Pi extension does not
re-post the rebuilt history to the server, and the carried turns are not
duplicated in the Omnigent transcript. This is the same guarantee claude /
codex get from seeding their forwarder cursor to start-at-end; pi-native gets
it for free from Pi's event semantics.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from omnigent.host.daemon_launch import error_text
from omnigent.native_terminal import url_component

# Pi session ids are UUIDv7 hex with dashes (e.g.
# ``019efdb8-54c8-7c02-be27-875eb2620635``). Restrict the value we use in a
# session filename / header to that safe shape so a persisted id can never
# inject path separators or shell-meaningful characters into the rollout path.
_PI_SESSION_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Pi session-file format version this module emits. Version 3 (the current
# format as of pi 0.79) renamed the legacy ``hookMessage`` role to ``custom``;
# pi auto-migrates older versions on load, but emitting the current version
# avoids a needless migration pass. See docs/session-format.md.
_PI_SESSION_VERSION = 3

# 8-char hex entry ids, matching Pi's own ``SessionEntryBase.id`` shape.
_ENTRY_ID_LEN = 8


def is_safe_pi_session_id(external_session_id: str) -> bool:
    """Return whether *external_session_id* is a safe Pi session id.

    :param external_session_id: Candidate Pi session id, e.g.
        ``"019efdb8-54c8-7c02-be27-875eb2620635"``.
    :returns: ``True`` when the id matches Pi's UUID session-id shape and is
        therefore safe to use unescaped in a session filename and header.
    """
    return bool(_PI_SESSION_ID_RE.fullmatch(external_session_id))


def mint_pi_session_id() -> str:
    """Return a fresh Pi-compatible session id.

    Pi mints UUIDv7 ids; a UUIDv4 has the same textual shape and is accepted by
    ``--session`` (which matches on the literal id), so a v4 is sufficient for a
    synthesized session that Omnigent owns.

    :returns: A new session id, e.g.
        ``"02857840-6362-408f-b41f-309e396ed7c6"``.
    """
    return str(uuid.uuid4())


def _pi_entry_timestamp() -> str:
    """Return an ISO-8601 UTC timestamp with a trailing ``Z``.

    :returns: Timestamp string, e.g. ``"2026-06-25T08:00:00.000Z"``.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _synthetic_pi_entry_id(
    *,
    session_id: str,
    external_session_id: str,
    item: dict[str, Any],
    index: int,
    suffix: str = "",
) -> str:
    """Build a stable 8-char hex entry id for one synthesized Pi entry.

    Deterministic so a re-synthesis of the same conversation yields a stable
    tree (matching how claude-native / codex-native derive synthetic ids).

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param external_session_id: Pi session id for the rebuilt file.
    :param item: Omnigent item dict. Its ``id`` is used when present.
    :param index: Zero-based fallback index in the item list.
    :param suffix: Discriminator when one item maps to multiple entries
        (e.g. an assistant text entry plus a tool-call entry).
    :returns: 8-char lowercase hex id, e.g. ``"a1b2c3d4"``.
    """
    item_id = item.get("id")
    stable_item_id = item_id if isinstance(item_id, str) and item_id else f"index-{index}"
    digest = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"omnigent-pi-resume:{session_id}:{external_session_id}:{stable_item_id}:{suffix}",
    ).hex
    return digest[:_ENTRY_ID_LEN]


def _pi_text_blocks_from_api_content(content: object, *, api_type: str) -> list[dict[str, Any]]:
    """Extract Pi text content blocks from an Omnigent content array.

    :param content: Omnigent content array, e.g.
        ``[{"type": "input_text", "text": "hello"}]``.
    :param api_type: Omnigent block type to include, ``"input_text"`` or
        ``"output_text"``.
    :returns: Pi ``{"type": "text", "text": ...}`` blocks.
    """
    if not isinstance(content, list):
        return []
    blocks: list[dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != api_type:
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            blocks.append({"type": "text", "text": text})
    return blocks


def _pi_tool_arguments(value: object) -> dict[str, Any]:
    """Parse an Omnigent function-call ``arguments`` string into a Pi object.

    Pi's ``toolCall.arguments`` is a structured object, whereas Omnigent stores
    the JSON-encoded string the model emitted.

    :param value: Omnigent ``arguments`` value, e.g. ``'{"path": "a.txt"}'``.
    :returns: Parsed arguments object, or ``{}`` for non-object / unparseable
        input.
    """
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_interrupted_assistant_item(item: dict[str, Any]) -> bool:
    """Return whether an Omnigent item is an interrupted assistant partial.

    Omnigent persists these so the web transcript can show the partial text and
    the interrupted label after refresh. They must not be replayed into Pi's
    rebuilt session, or Pi would treat a cancelled answer as completed history.

    :param item: Flat Omnigent item dict.
    :returns: ``True`` for interrupted assistant messages.
    """
    return (
        item.get("type") == "message"
        and item.get("role") == "assistant"
        and item.get("interrupted") is True
    )


def _interrupted_response_ids(items: list[dict[str, Any]]) -> set[str]:
    """Return response ids for Omnigent turns that ended interrupted.

    :param items: Flat Omnigent item dicts in chronological order.
    :returns: Response ids to exclude from the rebuilt Pi session.
    """
    response_ids: set[str] = set()
    for item in items:
        if not _is_interrupted_assistant_item(item):
            continue
        response_id = item.get("response_id")
        if isinstance(response_id, str) and response_id:
            response_ids.add(response_id)
    return response_ids


def pi_session_records_from_session_items(
    items: list[dict[str, Any]],
    *,
    session_id: str,
    external_session_id: str,
    cwd: Path,
    provider: str = "omnigent",
    model: str = "",
) -> list[dict[str, Any]]:
    """Convert Omnigent session items into Pi session JSONL records.

    The generated records follow Pi's v3 session format: one ``session``
    header, then ``message`` entries linked by ``id`` / ``parentId``. Omnigent
    items map as:

    - user ``message`` -> Pi ``message`` with ``role: "user"``.
    - assistant ``message`` -> Pi ``message`` with ``role: "assistant"`` and a
      text content block.
    - ``function_call`` -> Pi ``message`` with ``role: "assistant"`` whose
      content carries a ``toolCall`` block.
    - ``function_call_output`` -> Pi ``message`` with ``role: "toolResult"``.

    Interrupted assistant turns (and the rest of their response group) are
    skipped so a cancelled turn isn't restored as completed history.

    :param items: Flat Omnigent item dicts in chronological order, e.g.
        ``{"type": "message", "role": "user", "content": [...]}``.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``. Used
        for deterministic synthetic entry ids.
    :param external_session_id: Pi session id for the rebuilt file's header.
    :param cwd: Working directory written into the session header.
    :param provider: Provider id stamped on assistant messages, e.g.
        ``"omnigent"``. Informational; Pi's resume uses the live provider.
    :param model: Model id stamped on assistant messages, e.g.
        ``"claude-opus-4-8"``. May be overridden per-item by the item's own
        ``model``.
    :returns: Pi session record dictionaries (header first).
    """
    timestamp = _pi_entry_timestamp()
    header: dict[str, Any] = {
        "type": "session",
        "version": _PI_SESSION_VERSION,
        "id": external_session_id,
        "timestamp": timestamp,
        "cwd": str(cwd),
    }
    records: list[dict[str, Any]] = [header]
    parent_id: str | None = None
    skip_response_ids = _interrupted_response_ids(items)

    for index, item in enumerate(items):
        response_id = item.get("response_id")
        if isinstance(response_id, str) and response_id in skip_response_ids:
            continue
        entries = _pi_entries_from_session_item(
            item,
            session_id=session_id,
            external_session_id=external_session_id,
            index=index,
            timestamp=timestamp,
            provider=provider,
            model=model,
        )
        for entry in entries:
            entry["parentId"] = parent_id
            records.append(entry)
            parent_id = entry["id"]
    return records


def _pi_entries_from_session_item(
    item: dict[str, Any],
    *,
    session_id: str,
    external_session_id: str,
    index: int,
    timestamp: str,
    provider: str,
    model: str,
) -> list[dict[str, Any]]:
    """Convert one Omnigent item into zero or more Pi session entries.

    :param item: Flat Omnigent item dict.
    :param session_id: Omnigent conversation id (for synthetic ids).
    :param external_session_id: Pi session id (for synthetic ids).
    :param index: Zero-based index of *item* in the source list.
    :param timestamp: ISO timestamp to stamp on each entry.
    :param provider: Provider id for assistant messages.
    :param model: Default model id for assistant messages.
    :returns: Pi entry dicts (without ``parentId``, which the caller links).
    """
    item_type = item.get("type")
    item_model = item.get("model")
    eff_model = item_model if isinstance(item_model, str) and item_model else model

    def entry_id(suffix: str = "") -> str:
        return _synthetic_pi_entry_id(
            session_id=session_id,
            external_session_id=external_session_id,
            item=item,
            index=index,
            suffix=suffix,
        )

    if item_type == "message":
        role = item.get("role")
        if role == "user":
            blocks = _pi_text_blocks_from_api_content(item.get("content"), api_type="input_text")
            if not blocks:
                return []
            return [
                {
                    "type": "message",
                    "id": entry_id(),
                    "timestamp": timestamp,
                    "message": {
                        "role": "user",
                        "content": blocks,
                        "timestamp": 0,
                    },
                }
            ]
        if role == "assistant":
            blocks = _pi_text_blocks_from_api_content(item.get("content"), api_type="output_text")
            if not blocks:
                return []
            return [
                {
                    "type": "message",
                    "id": entry_id(),
                    "timestamp": timestamp,
                    "message": _pi_assistant_message(blocks, provider=provider, model=eff_model),
                }
            ]
        return []

    if item_type == "function_call":
        name = item.get("name")
        call_id = item.get("call_id")
        if not isinstance(name, str) or not name:
            return []
        if not isinstance(call_id, str) or not call_id:
            return []
        tool_call_block = {
            "type": "toolCall",
            "id": call_id,
            "name": name,
            "arguments": _pi_tool_arguments(item.get("arguments")),
        }
        return [
            {
                "type": "message",
                "id": entry_id(),
                "timestamp": timestamp,
                "message": _pi_assistant_message(
                    [tool_call_block], provider=provider, model=eff_model
                ),
            }
        ]

    if item_type == "function_call_output":
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return []
        output = item.get("output")
        if not isinstance(output, str):
            output = "" if output is None else json.dumps(output, separators=(",", ":"))
        return [
            {
                "type": "message",
                "id": entry_id(),
                "timestamp": timestamp,
                "message": {
                    "role": "toolResult",
                    "toolCallId": call_id,
                    "toolName": "",
                    "content": [{"type": "text", "text": output}],
                    "isError": False,
                    "timestamp": 0,
                },
            }
        ]

    return []


def _pi_assistant_message(
    content: list[dict[str, Any]],
    *,
    provider: str,
    model: str,
) -> dict[str, Any]:
    """Build a Pi assistant ``message`` body with the required metadata.

    Pi's ``AssistantMessage`` requires ``provider`` / ``model`` / ``usage`` /
    ``stopReason`` fields; a malformed assistant entry would fail to parse on
    load. The usage is zeroed (synthesized history has no real token accounting).

    :param content: Assistant content blocks (text and/or toolCall).
    :param provider: Provider id, e.g. ``"omnigent"``.
    :param model: Model id, e.g. ``"claude-opus-4-8"``.
    :returns: Pi assistant message dict.
    """
    return {
        "role": "assistant",
        "content": content,
        "api": "anthropic-messages",
        "provider": provider,
        "model": model,
        "usage": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "totalTokens": 0,
            "cost": {
                "input": 0,
                "output": 0,
                "cacheRead": 0,
                "cacheWrite": 0,
                "total": 0,
            },
        },
        "stopReason": "stop",
        "timestamp": 0,
    }


def pi_resume_session_path(session_dir: Path, external_session_id: str) -> Path:
    """Return the session JSONL path to write for a Pi cold resume / fork.

    Reuses an existing on-disk file for the id when present (Pi appends to a
    session file as a conversation grows, so an existing file is live runtime
    state Omnigent should not clobber). Otherwise builds a timestamped path
    matching Pi's ``<timestamp>_<uuid>.jsonl`` naming under the session dir.

    :param session_dir: Directory passed to ``pi --session-dir``.
    :param external_session_id: Pi session id / file stem.
    :returns: Session JSONL path to reuse or create.
    """
    existing = _find_pi_session_file(session_dir, external_session_id)
    if existing is not None:
        return existing
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-%f")[:-3] + "Z"
    return session_dir / f"{stamp}_{external_session_id}.jsonl"


def _find_pi_session_file(session_dir: Path, external_session_id: str) -> Path | None:
    """Return an existing session file for *external_session_id*, if any.

    Pi names files ``<timestamp>_<uuid>.jsonl``; match on the trailing uuid.

    :param session_dir: Directory passed to ``pi --session-dir``.
    :param external_session_id: Pi session id / file stem.
    :returns: The matching file, or ``None`` when none exists.
    """
    if not session_dir.is_dir():
        return None
    suffix = f"_{external_session_id}.jsonl"
    for entry in session_dir.iterdir():
        if entry.is_file() and entry.name.endswith(suffix):
            return entry
    return None


async def fetch_all_session_items_for_pi_resume(
    client: httpx.AsyncClient,
    session_id: str,
) -> list[dict[str, Any]]:
    """Fetch committed Omnigent session items in chronological order.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :returns: Flat API item dicts from ``GET /v1/sessions/{id}/items``.
    :raises RuntimeError: If an item page cannot be fetched or parsed.
    """
    items: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        params: dict[str, str | int] = {"limit": 1000, "order": "asc"}
        if after is not None:
            params["after"] = after
        resp = await client.get(
            f"/v1/sessions/{url_component(session_id)}/items",
            params=params,
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Failed to fetch history for {session_id!r} "
                f"({resp.status_code}): {error_text(resp)}"
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RuntimeError(
                f"History fetch for {session_id!r} returned non-JSON body: {exc}"
            ) from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise RuntimeError(f"History fetch for {session_id!r} returned an invalid item list.")
        for item in data:
            if isinstance(item, dict):
                items.append(item)
        if not payload.get("has_more"):
            return items
        last_id = payload.get("last_id")
        if not isinstance(last_id, str) or not last_id:
            raise RuntimeError(f"History fetch for {session_id!r} set has_more without last_id.")
        after = last_id


def write_pi_session_records(target: Path, records: list[dict[str, Any]]) -> None:
    """Atomically write Pi session JSONL *records* to *target*.

    :param target: Session JSONL path to write.
    :param records: Pi session records (header first).
    :raises RuntimeError: If the file cannot be written.
    """
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = target.with_suffix(".jsonl.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
        os.replace(tmp, target)
    except OSError as exc:
        raise RuntimeError(f"Failed to write Pi resume session {target}: {exc}") from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


async def ensure_local_pi_resume_session(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    external_session_id: str,
    session_dir: Path,
    workspace: Path,
    provider: str = "omnigent",
    model: str = "",
) -> Path | None:
    """Ensure Pi has a local session JSONL to resume with prior history.

    Synthesizes the Pi session file from committed Omnigent items so a
    cold-resume (no local file — cross-machine, fresh runner, or cleared
    bridge dir) or a fork (a freshly minted session id) opens with the prior
    conversation as Pi context. Mirrors
    :func:`omnigent.codex_native._ensure_local_codex_resume_rollout` and
    :func:`omnigent.claude_native._ensure_local_claude_resume_transcript`.

    An EXISTING local file for the id is left untouched (Pi treats it as
    append-only runtime state). Returns ``None`` when the synthesized session
    has no resumable messages (an empty session is pointless to resume — the
    caller should launch fresh) or when *external_session_id* is unsafe for a
    filename.

    :param client: HTTP client pointed at the Omnigent server.
    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param external_session_id: Pi session id for the rebuilt file.
    :param session_dir: Directory passed to ``pi --session-dir``.
    :param workspace: Resolved cwd Pi will run in (written into the header).
    :param provider: Provider id for synthesized assistant messages.
    :param model: Default model id for synthesized assistant messages.
    :returns: Path to the existing or written session file, or ``None`` when
        nothing resumable was produced.
    :raises RuntimeError: If Omnigent history cannot be fetched or the session
        cannot be written.
    """
    if not is_safe_pi_session_id(external_session_id):
        return None
    existing = _find_pi_session_file(session_dir, external_session_id)
    if existing is not None:
        return existing
    items = await fetch_all_session_items_for_pi_resume(client, session_id)
    records = pi_session_records_from_session_items(
        items,
        session_id=session_id,
        external_session_id=external_session_id,
        cwd=workspace,
        provider=provider,
        model=model,
    )
    # records[0] is always the header; a session with only the header has no
    # turns to resume, so launch fresh instead of resuming an empty session.
    if len(records) <= 1:
        return None
    target = pi_resume_session_path(session_dir, external_session_id)
    write_pi_session_records(target, records)
    return target
