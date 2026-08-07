"""
JSON dump of an omnigent conversation — the Omnigent mode port of the
legacy ``--log`` feature (see designs/RUN_OMNIGENT_REPL_PARITY.md).

The legacy non-AP path wrote a session-shaped JSON to
``~/.omnigent/logs/`` at REPL exit (`_write_session_log` /
``_session_log_dict`` in ``omnigent/inner/cli.py``). That format
was tied to the in-memory :class:`Session` machinery — omnigent
has no Session, just conversations + items, so this module emits an
**AP-native** shape instead of translating into the legacy schema:

.. code-block:: json

    {
      "version": 1,
      "written_at": "2026-04-27T18:01:23+00:00",
      "format": "omnigent-conversation",
      "agent_name": "resume_test",
      "conversation": {
        "id": "conv_abc...",
        "title": "...",
        "created_at": 1714248083,
        "labels": {...},
        "items": [
          {"id": "item_...", "type": "message", "role": "user", ...},
          {"id": "item_...", "type": "message", "role": "assistant", ...},
          ...
        ],
        "children": [
          {"id": "conv_child_...", "items": [...], "children": [...]},
          ...
        ]
      }
    }

The grep across the codebase confirmed there are no consumers of
the legacy JSON shape (all references are help text in ``cli.py``
or stale worktree copies), so picking an AP-native shape costs us
no compat — and it's a more honest dump of what's actually
persisted.

Sub-agent / child conversations are walked via the parent's items:
``sys_session_send`` spawn calls are persisted as
``function_call_output`` rows whose ``output`` decodes to a
handle dict with ``kind == "sub_agent"`` and a
``conversation_id`` pointing at the child. Same parser the
Ctrl+O debug overlay uses (``_parse_sub_agent_handle`` in
``omnigent/repl/_repl.py``), so spawn handles are recognized
identically across surfaces. Recursion is bounded by a
``visited`` set; a cycle (a child handle that points back at an
ancestor — shouldn't happen in practice) emits a stub node with
``"cycle": true`` rather than recursing forever.
"""

from __future__ import annotations

import json
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from omnigent_client import OmnigentClient
from omnigent_ui_sdk import state_dir

from omnigent.stores.conversation_store import (
    ConversationNotFoundError,
    ConversationStore,
)

# Schema version — bump only on breaking shape changes. Readers MAY
# accept earlier versions; writers SHOULD always emit the current
# value. Kept here (top of module) rather than scattered through the
# dict construction so a reader can grep for the constant.
_LOG_SCHEMA_VERSION = 1

# Format string identifying the AP-native shape. Allows future
# alternative writers (e.g. a legacy-compat translator) to share
# this module while disambiguating their output via this field.
_LOG_FORMAT = "omnigent-conversation"

# Default log directory. Derives from the shared ``state_dir()``
# (``~/.omnigent``) so the root is defined in one place. Mirrors
# the legacy non-AP path's ``~/.omnigent/logs/`` (see
# ``_default_session_log_path`` in ``omnigent/inner/cli.py``) so
# users don't have to learn a new location when migrating. Created
# on first write.
DEFAULT_LOG_DIR = state_dir() / "logs"
DEFAULT_LOG_ZIP_DIR = DEFAULT_LOG_DIR


def _safe_session_slug(session_id: str | None) -> str | None:
    """Return a filesystem-safe short session id slug for filenames."""
    if not session_id:
        return None
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)
    return safe[:32] or None


def default_log_zip_path(
    output_dir: Path | None = None,
    *,
    session_id: str | None = None,
) -> Path:
    """
    Compose the default zip path for the current-session log bundle.

    Format: ``{output_dir}/omnigent-logs-{session_slug}-{timestamp}.zip``
    when a session id is available, otherwise
    ``{output_dir}/omnigent-logs-{timestamp}.zip``. ``None`` uses
    :data:`DEFAULT_LOG_ZIP_DIR`, i.e. ``~/.omnigent/logs``.

    :param output_dir: Directory where the zip should be written.
    :param session_id: Optional active session/conversation id used
        to make the bundle name self-describing.
    :returns: Absolute path for the zip file (parent not yet created).
    """
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    base = output_dir if output_dir is not None else DEFAULT_LOG_ZIP_DIR
    slug = _safe_session_slug(session_id)
    if slug is not None:
        return base / f"omnigent-logs-{slug}-{timestamp}.zip"
    return base / f"omnigent-logs-{timestamp}.zip"


def collect_log_files(log_paths: list[Path]) -> list[tuple[Path, str]]:
    """
    Normalize an explicit set of current-session log files for zipping.

    ``/logs`` is intentionally session-scoped: callers pass only the
    files known to belong to the active REPL invocation/session (for
    example the freshly-written conversation JSON, this process' CLI
    diagnostics log, and the current ``--debug-events`` JSONL tape).
    This helper does **not** walk ``~/.omnigent/logs`` or
    ``~/.omnigent/debug`` wholesale; doing so would include unrelated
    sessions.

    Zip files are skipped so repeated ``/logs`` invocations do not nest
    older bundles inside newer bundles.

    :param log_paths: Explicit files to include. Missing paths and
        directories are ignored.
    :returns: ``(path, archive_name)`` pairs sorted by archive name.
        ``archive_name`` is rooted at the parent directory name, e.g.
        ``logs/cli-...log`` or ``debug/events-...jsonl``.
    """
    files: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    used_arcnames: set[str] = set()
    for raw_path in log_paths:
        path = raw_path.expanduser()
        if not path.exists() or not path.is_file() or path.suffix == ".zip":
            continue
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        arcname = str(Path(path.parent.name) / path.name)
        if arcname in used_arcnames:
            # Extremely uncommon, but two explicit files can share
            # parent-dir + filename (e.g. temp dirs in tests). Keep
            # both by prefixing a stable ordinal.
            arcname = str(Path(path.parent.name) / f"{len(used_arcnames):02d}-{path.name}")
        used_arcnames.add(arcname)
        files.append((path, arcname))
    return sorted(files, key=lambda item: item[1])


def write_logs_zip(
    output_path: Path | None = None,
    *,
    log_paths: list[Path],
    session_id: str | None = None,
) -> tuple[Path, int]:
    """
    Zip an explicit set of current-session log files into one bundle.

    :param output_path: Destination zip path. ``None`` writes a
        timestamped file under :data:`DEFAULT_LOG_ZIP_DIR`.
    :param log_paths: Explicit files to include. The caller is
        responsible for passing only current-session files.
    :param session_id: Optional active session id used in the default
        output filename.
    :returns: ``(zip_path, file_count)``. ``file_count`` may be zero;
        in that case an empty zip is still created so the caller has
        a concrete artifact to share.
    """
    target = (
        output_path.expanduser()
        if output_path is not None
        else default_log_zip_path(session_id=session_id)
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    entries = collect_log_files(log_paths)
    with zipfile.ZipFile(target, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        written = 0
        try:
            target_resolved = target.resolve(strict=False)
        except OSError:
            target_resolved = target
        for path, arcname in entries:
            try:
                if path.resolve(strict=True) == target_resolved:
                    continue
            except OSError:
                continue
            zf.write(path, arcname)
            written += 1
    return target, written


def default_log_path(conversation_id: str, log_dir: Path | None = None) -> Path:
    """
    Compose the default file path for a conversation log.

    Format: ``{log_dir}/{timestamp}-{conv_short}.json`` where
    ``timestamp`` is ``YYYYMMDD-HHMMSS`` (UTC, sortable) and
    ``conv_short`` is the first 16 characters of *conversation_id*
    with any path separators stripped — matching the legacy slug
    treatment in ``omnigent/inner/cli.py::_default_session_log_path``.

    :param conversation_id: The conversation id, e.g.
        ``"conv_a1b2c3d4e5f6..."``.
    :param log_dir: Override the default location. ``None`` uses
        :data:`DEFAULT_LOG_DIR`.
    :returns: Absolute path the JSON should be written to (parent
        directory not yet created).
    """
    timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    conv_slug = conversation_id.replace("/", "_")[:16]
    base = log_dir if log_dir is not None else DEFAULT_LOG_DIR
    return base / f"{timestamp}-{conv_slug}.json"


async def write_session_log(
    client: OmnigentClient,
    conversation_id: str,
    *,
    agent_name: str,
    log_dir: Path | None = None,
) -> Path:
    """
    Fetch the session + its items + sub-agent children via the
    SDK and write the JSON dump to
    ``{log_dir}/{timestamp}-{conv_short}.json``.

    Pages through all items via cursor-based pagination so long
    sessions don't get truncated to the API's per-call cap.
    Items are emitted in chronological order (the API's ``"asc"``
    default).

    Children are discovered by scanning the parent's items for
    ``function_call_output`` rows that decode to a
    ``sys_session_send`` handle (see :func:`_parse_sub_agent_handle`
    in ``omnigent/repl/_repl.py`` — same parser the Ctrl+O debug
    overlay uses for sidebar tab discovery, so handles parse
    identically across the two surfaces). Walk recursively from the
    root, dumping each child's items + its own children. A
    ``visited`` set guards against cycles in case a sub-agent
    handle ever points back at an ancestor.

    :param client: A connected :class:`OmnigentClient`. The
        REPL's existing client is fine — this helper does not
        open a new connection.
    :param conversation_id: The session to dump,
        e.g. ``"conv_abc123"``.
    :param agent_name: Agent name to include in the dump (for
        readers that want to know which agent owned this thread
        without separately querying the agent registry),
        e.g. ``"resume_test"``.
    :param log_dir: Override the default location. ``None`` uses
        :data:`DEFAULT_LOG_DIR` (``~/.omnigent/logs/``).
    :returns: Path to the written JSON file. The parent directory
        is created if it didn't exist.
    """
    target = default_log_path(conversation_id, log_dir)
    target.parent.mkdir(parents=True, exist_ok=True)

    visited: set[str] = set()
    root_node = await _build_node_async(
        client,
        conversation_id,
        visited,
    )

    payload: dict[str, object] = {
        "version": _LOG_SCHEMA_VERSION,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "format": _LOG_FORMAT,
        "agent_name": agent_name,
        "conversation": root_node,
    }
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


async def _build_node_async(
    client: OmnigentClient,
    conversation_id: str,
    visited: set[str],
) -> dict[str, object]:
    """
    Build one node of the session tree (id + items + children)
    via the SDK, recursing into sub-agent children.

    :param client: Connected SDK client.
    :param conversation_id: Session to dump at this node.
    :param visited: Set of session ids already in the tree —
        prevents cycles. Mutated in place as we descend.
    :returns: A node dict in the AP-native log shape, ready to be
        embedded under ``conversation`` (root) or ``children``
        (descendants).
    """
    if conversation_id in visited:
        # Already in the tree — break the cycle and emit a stub so
        # the structure stays well-formed without recursing forever.
        return {
            "id": conversation_id,
            "cycle": True,
            "items": [],
            "children": [],
        }
    visited.add(conversation_id)

    snap = await client.sessions.get(conversation_id)
    items = await _fetch_all_items_via_sessions(client, conversation_id)
    children = []
    for child_id in _extract_child_conversation_ids(items):
        if child_id in visited:
            continue
        children.append(
            await _build_node_async(
                client,
                child_id,
                visited,
            )
        )
    return {
        "id": snap.id,
        "title": snap.title,
        "created_at": snap.created_at,
        "labels": dict(snap.labels),
        "items": items,
        "children": children,
    }


def _extract_child_conversation_ids(items: list[dict[str, object]]) -> list[str]:
    """
    Walk a conversation's items in chronological order and pull out
    the ``conversation_id`` of every distinct sub-agent spawn.

    Reuses :func:`_parse_sub_agent_handle` from the REPL's debug
    overlay so the parsing rules stay in one place — both shapes
    the spawn output is persisted in (raw JSON handle for native
    builtins, MCP content-parts wrapper for the claude-sdk
    harness) get unwrapped here. Anything else is silently
    skipped.

    Tolerates two on-the-wire item shapes:

    - **API shape** (what ``client.sessions.list_items``
      returns): ``output`` is flattened to the top level by
      ``ConversationItem.to_api_dict``. Used by the async SDK
      writer.
    - **Entity shape** (what ``ConversationItem.model_dump``
      produces): ``output`` lives under ``data.output`` because
      the entity wraps it in a typed
      :class:`FunctionCallOutputData`. Used by the sync
      store-direct writer.

    Same item, two equally-valid serializations — the walker
    needs to read either.

    :param items: Items as returned by the SDK / store, in
        chronological order.
    :returns: Distinct child conversation ids, deduped while
        preserving first-seen order. Multiple
        ``sys_session_send`` calls to the same handle (the
        continuation pattern) collapse to one entry.
    """
    # Lazy import to break a potential circular dependency at module
    # load time (``_repl`` imports from this module's siblings).
    from omnigent.repl._repl import _parse_sub_agent_handle

    seen: set[str] = set()
    children: list[str] = []
    for item in items:
        if item.get("type") != "function_call_output":
            continue
        # Flat (API) shape first, then nested (entity) shape. Both
        # forms are valid; the writer that produced the items
        # determines which we see.
        raw = item.get("output")
        if not isinstance(raw, str):
            data = item.get("data")
            if isinstance(data, dict):
                nested = data.get("output")
                raw = nested if isinstance(nested, str) else None
        if not isinstance(raw, str):
            continue
        handle = _parse_sub_agent_handle(raw)
        if handle is None:
            continue
        child_id = handle.get("conversation_id")
        if not isinstance(child_id, str) or child_id in seen:
            continue
        seen.add(child_id)
        children.append(child_id)
    return children


def write_session_log_from_store(
    conv_store: ConversationStore,
    conversation_id: str,
    *,
    agent_name: str,
    log_dir: Path | None = None,
) -> Path:
    """
    Sync sibling of :func:`write_session_log` that reads through a
    :class:`omnigent.stores.conversation_store.ConversationStore`
    directly instead of the SDK.

    Used by the one-shot ``omnigent run <yaml> -p "…" --log`` path
    where the in-process ASGI app doesn't have a connected
    :class:`OmnigentClient` — the run goes through raw httpx +
    ``httpx.ASGITransport`` and tearing all that down to construct
    an SDK client just for the log write would be silly when the
    store is already in scope.

    Output JSON is byte-identical to :func:`write_session_log` so a
    reader can't tell which path produced the dump.

    :param conv_store: A :class:`ConversationStore` instance with
        ``get_conversation`` + ``list_items`` methods.
    :param conversation_id: The conversation to dump.
    :param agent_name: Agent name to embed in the dump.
    :param log_dir: Override the default location.
    :returns: Path to the written JSON file.
    """
    target = default_log_path(conversation_id, log_dir)
    target.parent.mkdir(parents=True, exist_ok=True)

    visited: set[str] = set()
    root_node = _build_node_sync(conv_store, conversation_id, visited)

    payload: dict[str, object] = {
        "version": _LOG_SCHEMA_VERSION,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "format": _LOG_FORMAT,
        "agent_name": agent_name,
        "conversation": root_node,
    }
    target.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def _build_node_sync(
    conv_store: ConversationStore,
    conversation_id: str,
    visited: set[str],
) -> dict[str, object]:
    """
    Sync sibling of :func:`_build_node_async`. Same recursion +
    cycle handling, just driven by direct store calls.

    :param conv_store: ConversationStore instance.
    :param conversation_id: Conversation to dump at this node.
    :param visited: Set of conversation_ids already in the tree.
    :returns: A node dict in the AP-native log shape.
    """
    if conversation_id in visited:
        return {
            "id": conversation_id,
            "cycle": True,
            "items": [],
            "children": [],
        }
    visited.add(conversation_id)

    conversation = conv_store.get_conversation(conversation_id)
    if conversation is None:
        raise ConversationNotFoundError(f"conversation {conversation_id!r} does not exist")
    items = _fetch_all_items_sync(conv_store, conversation_id)
    children = []
    for child_id in _extract_child_conversation_ids(items):
        if child_id in visited:
            continue
        children.append(_build_node_sync(conv_store, child_id, visited))
    return {
        "id": conversation.id,
        "title": conversation.title,
        "created_at": conversation.created_at,
        "labels": dict(getattr(conversation, "labels", {}) or {}),
        "items": items,
        "children": children,
    }


def _fetch_all_items_sync(
    conv_store: ConversationStore,
    conversation_id: str,
) -> list[dict[str, object]]:
    """
    Sync sibling of :func:`_fetch_all_items` that pages through a
    :class:`ConversationStore` directly. See that function's
    docstring for the pagination strategy and the empty-page
    end-of-data convention.

    Items are converted to plain dicts so the JSON dump is
    self-contained — a reader doesn't need any of Omnigent'
    Pydantic models to consume the file.

    :param conv_store: ConversationStore instance.
    :param conversation_id: Conversation to page.
    :returns: All items as plain dicts in chronological order.
    """
    collected: list[dict[str, object]] = []
    cursor: str | None = None
    page_size = 100
    while True:
        page = conv_store.list_items(
            conversation_id=conversation_id,
            limit=page_size,
            after=cursor,
            order="asc",
        )
        rows = list(page.data)
        if not rows:
            return collected
        for item in rows:
            # Items from the store are Pydantic models. ``model_dump``
            # produces a JSON-safe dict; the SDK path emits plain
            # dicts already. Falling back to ``dict(item)`` would
            # lose nested typed fields.
            collected.append(item.model_dump())
        if len(rows) < page_size:
            return collected
        last_id = rows[-1].id
        if not last_id:
            return collected
        cursor = last_id


async def _fetch_all_items_via_sessions(
    client: OmnigentClient,
    session_id: str,
) -> list[dict[str, object]]:
    """
    Fetch all items for a session using ``GET /v1/sessions/{id}/items``.

    Same pagination pattern as :func:`_fetch_all_items` but uses the
    sessions items endpoint instead of conversations.

    :param client: The connected :class:`OmnigentClient`.
    :param session_id: Session to page through.
    :returns: All items in chronological order.
    """
    collected: list[dict[str, object]] = []
    cursor: str | None = None
    page_size = 100
    while True:
        page = await client.sessions.list_items(
            session_id,
            limit=page_size,
            after=cursor,
            order="asc",
        )
        if page is None or not page:
            return collected
        collected.extend(page)
        last_id = page[-1].get("id")
        if not isinstance(last_id, str) or not last_id:
            return collected
        cursor = last_id
        if len(page) < page_size:
            return collected
