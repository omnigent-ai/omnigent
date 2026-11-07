"""Mirror a Command Code session transcript into the Omnigent chat.

The ``omnigent commandcode`` wrapper boots the resident ``cmd`` TUI in a
runner-owned tmux pane. This module tails that session's on-disk transcript and
POSTs each user turn + assistant reply into the Omnigent session, so the reply
renders in the web UI (not only the embedded terminal).

Transcript (per Command Code's session store)::

    ~/.commandcode/projects/<project-slug>/<session-id>.jsonl

A JSONL file: a one-line ``{"type":"session",...}`` header (with ``cwd``),
then ``{"type":"message","id","parentId","timestamp","message":{...}}`` entries
where ``message`` is ``{role, content[], meta:{source}}``:

* ``role:"user"`` + ``meta.source:"user"``  → a real user turn (text blocks);
* ``role:"assistant"`` + ``meta.source:"model"`` → the reply (``text`` /
  ``thinking`` / ``tool_use`` blocks);
* ``role:"user"`` + ``meta.source:"tool"`` → tool results (not mirrored; the
  embedded terminal renders tool activity).

The project slug is the workspace path with ``/`` → ``-`` and no leading slash
(e.g. ``/home/alan`` → ``home-alan``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.25
_BACKOFF_INITIAL_S = 1.0
_BACKOFF_MAX_S = 30.0
_STATE_FILE = "commandcode_forwarder.json"
_DEFAULT_AGENT_NAME = "commandcode-native-ui"
_EVENTS_PATH = "/v1/sessions/{session_id}/events"


@dataclass(frozen=True)
class _MirroredItem:
    """One item extracted from the transcript, ready to POST.

    ``kind`` is one of ``message`` | ``reasoning`` | ``function_call`` |
    ``function_call_output``. ``role``/``text`` carry message + reasoning + tool
    output payloads; ``call_id``/``name``/``arguments`` carry tool-call payloads.
    """

    kind: str
    role: str = ""
    text: str = ""
    response_id: str = ""
    call_id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass
class _ForwardState:
    """Durable cursor into the session transcript + cumulative usage."""

    path: str | None = None
    offset: int = 0
    line: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    context_tokens: int = 0
    cost_usd: float = 0.0
    model: str | None = None


@dataclass
class _UsageDelta:
    """Usage summed over newly-read transcript entries."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    context_tokens: int = 0
    cost_usd: float = 0.0
    model: str | None = None

    @property
    def advanced(self) -> bool:
        """:returns: ``True`` when any token count or cost advanced."""
        return bool(
            self.input_tokens or self.output_tokens or self.cache_read_tokens or self.cost_usd
        )


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def clear_commandcode_bridge_state(bridge_dir: Path) -> None:
    """Drop the forwarder cursor so a re-created terminal tails the fresh file."""
    with contextlib.suppress(FileNotFoundError):
        (bridge_dir / _STATE_FILE).unlink()


def preseed_commandcode_resume_state(
    bridge_dir: Path,
    *,
    commandcode_home: Path,
    workspace: str,
    external_session_id: str,
) -> bool:
    """Point the forwarder at the resumed transcript, at its current end.

    On a ``cmd --resume`` relaunch the session transcript already exists, so
    launch-recency discovery would reject it (and adoption from offset 0 would
    re-mirror turns already in the Omnigent chat). Seeding the cursor past the
    existing content leaves only post-relaunch entries to mirror.

    :param external_session_id: The persisted Command Code session id (its
        ``--resume`` key); the transcript is ``<id>.jsonl`` under the
        workspace's project slug dir.
    :returns: ``True`` when the transcript exists and the state was written;
        ``False`` when it is absent (caller should launch fresh).
    """
    transcript_path = (
        commandcode_home / "projects" / _project_slug(workspace) / f"{external_session_id}.jsonl"
    )
    if not transcript_path.is_file():
        return False
    # One full read seeds both the cursor (past every mirrored entry) and the
    # cumulative usage, so the cost badge doesn't drop back to zero on relaunch.
    _items, usage, offset, line = _read_new_items(transcript_path, 0, 0)
    if offset == 0:
        return False
    bridge_dir.mkdir(parents=True, exist_ok=True)
    _write_state(
        bridge_dir,
        _ForwardState(
            path=str(transcript_path),
            offset=offset,
            line=line,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            context_tokens=usage.context_tokens,
            cost_usd=usage.cost_usd,
            model=usage.model,
        ),
    )
    return True


async def supervise_commandcode_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    commandcode_home: Path,
    workspace: str,
    launch_epoch_ms: int,
    agent_name: str = _DEFAULT_AGENT_NAME,
    auth: httpx.Auth | None = None,
    pane_alive: Callable[[], bool] | None = None,
) -> None:
    """Run the forward loop with restart-on-crash backoff.

    Propagates :class:`asyncio.CancelledError` cleanly (terminal teardown);
    restarts on any other exception with exponential backoff.
    """
    backoff = _BACKOFF_INITIAL_S
    while True:
        try:
            await forward_commandcode_transcript_to_session(
                base_url=base_url,
                headers=headers,
                session_id=session_id,
                bridge_dir=bridge_dir,
                commandcode_home=commandcode_home,
                workspace=workspace,
                launch_epoch_ms=launch_epoch_ms,
                agent_name=agent_name,
                auth=auth,
                pane_alive=pane_alive,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "commandcode forwarder crashed for session %s; restarting", session_id
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)
        else:
            return


async def forward_commandcode_transcript_to_session(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    commandcode_home: Path,
    workspace: str,
    launch_epoch_ms: int,
    agent_name: str = _DEFAULT_AGENT_NAME,
    auth: httpx.Auth | None = None,
    pane_alive: Callable[[], bool] | None = None,
) -> None:
    """Tail the session transcript and mirror turns until cancelled or the pane dies."""
    state = _read_state(bridge_dir)
    base = base_url.rstrip("/")
    timeout = httpx.Timeout(30.0, read=30.0)
    mirrored_external_id: str | None = None
    async with httpx.AsyncClient(timeout=timeout, auth=auth) as client:
        while True:
            if pane_alive is not None and not pane_alive():
                return
            path = state.path
            if path is None:
                discovered = _discover_transcript(
                    commandcode_home=commandcode_home,
                    workspace=workspace,
                    launch_epoch_ms=launch_epoch_ms,
                )
                if discovered is None:
                    await asyncio.sleep(_POLL_INTERVAL_S)
                    continue
                state.path = str(discovered)
                path = state.path
            # Record the Command Code session id (its ``--resume`` key) as the
            # Omnigent ``external_session_id`` once the transcript exists, so
            # resume/fork can reattach — mirrors the other native forwarders.
            # Command Code writes its transcript only after the first reply, so
            # this lands on the first observed turn.
            external_id = _external_session_id(Path(path))
            if external_id and external_id != mirrored_external_id:
                await _patch_external_session_id(
                    client,
                    base_url=base,
                    headers=headers,
                    session_id=session_id,
                    external_session_id=external_id,
                )
                mirrored_external_id = external_id
            items, usage, next_offset, next_line = _read_new_items(
                Path(path), state.offset, state.line
            )
            if items:
                for item in items:
                    await _mirror_item(
                        client,
                        base_url=base,
                        headers=headers,
                        session_id=session_id,
                        item=item,
                        agent_name=agent_name,
                    )
            if usage.advanced:
                state.input_tokens += usage.input_tokens
                state.output_tokens += usage.output_tokens
                state.cache_read_tokens += usage.cache_read_tokens
                state.context_tokens = max(state.context_tokens, usage.context_tokens)
                state.cost_usd += usage.cost_usd
                if usage.model:
                    state.model = usage.model
                await _post_usage(
                    client,
                    base_url=base,
                    headers=headers,
                    session_id=session_id,
                    state=state,
                )
            if next_offset != state.offset or next_line != state.line:
                state.offset = next_offset
                state.line = next_line
                _write_state(bridge_dir, state)
            await asyncio.sleep(_POLL_INTERVAL_S)


# ---------------------------------------------------------------------------
# transcript discovery + parsing
# ---------------------------------------------------------------------------


def _project_slug(workspace: str) -> str:
    """Map a workspace path to Command Code's project slug (``/`` → ``-``)."""
    return workspace.strip("/").replace("\\", "/").replace("/", "-")


def _discover_transcript(
    *, commandcode_home: Path, workspace: str, launch_epoch_ms: int
) -> Path | None:
    """Return the newest transcript for *workspace* created at/after launch."""
    slug_dir = commandcode_home / "projects" / _project_slug(workspace)
    if not slug_dir.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    for path in slug_dir.glob("*.jsonl"):
        if path.name.endswith(".checkpoints.jsonl"):
            continue
        header = _read_header(path)
        if header is None or header.get("cwd") != workspace:
            continue
        candidates.append((path.stat().st_mtime, path))
    if not candidates:
        return None
    newest = max(candidates, key=lambda pair: pair[0])
    # Only adopt a transcript written at/after this launch. Adopting a stale
    # one (e.g. left by a prior ``cmd`` run in the same workspace) would
    # re-mirror its whole history into a fresh conversation. Resumed sessions
    # skip discovery via :func:`preseed_commandcode_resume_state`.
    if newest[0] * 1000 >= launch_epoch_ms - 2000:
        return newest[1]
    return None


def _read_header(path: Path) -> dict[str, Any] | None:
    """Read and parse the one-line session header, or ``None``."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            first = handle.readline()
    except OSError:
        return None
    try:
        parsed = json.loads(first)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _external_session_id(path: Path) -> str | None:
    """Return the Command Code session id (its ``--resume`` key) for *path*.

    The transcript header carries it as ``id``; fall back to the file stem.
    """
    header = _read_header(path)
    if header is not None:
        value = header.get("id")
        if isinstance(value, str) and value:
            return value
    return path.stem or None


def _read_new_items(
    path: Path, last_offset: int, last_line: int
) -> tuple[list[_MirroredItem], _UsageDelta, int, int]:
    """Read transcript entries appended after the cursor and map them.

    :returns: ``(items, usage, next_offset, next_line)``. ``usage`` sums the
        token usage of the newly-read message entries. ``next_*`` equal the
        inputs when the file is missing or unchanged.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return [], _UsageDelta(), last_offset, last_line
    if size <= last_offset:
        return [], _UsageDelta(), last_offset, last_line

    items: list[_MirroredItem] = []
    usage = _UsageDelta()
    try:
        with path.open("r", encoding="utf-8") as handle:
            handle.seek(last_offset)
            consumed = handle.read()
    except OSError:
        return [], _UsageDelta(), last_offset, last_line

    # Keep a trailing partial line unconsumed so it is re-read once complete.
    if consumed.endswith("\n"):
        chunk = consumed
    else:
        last_newline = consumed.rfind("\n")
        chunk = consumed[: last_newline + 1] if last_newline >= 0 else ""

    offset = last_offset + len(chunk.encode("utf-8"))
    line_no = last_line
    for raw in chunk.splitlines():
        line_no += 1
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        # Usage rides on the top-level entry (message entries carry it), even
        # for rows whose content we don't mirror.
        _accumulate_usage(row, usage)
        if row.get("type") != "message":
            continue
        items.extend(_row_to_items(row))
    return items, usage, offset, line_no


def _accumulate_usage(row: dict[str, Any], usage: _UsageDelta) -> None:
    """Add one entry's top-level ``usage``/``model`` fields into *usage*."""
    raw = row.get("usage")
    if isinstance(raw, dict):
        inp = _as_int(raw.get("inputTokens"))
        usage.input_tokens += inp
        usage.output_tokens += _as_int(raw.get("outputTokens"))
        usage.cache_read_tokens += _as_int(raw.get("cacheReadTokens"))
        usage.cost_usd += _as_float(raw.get("costUsd"))
        # The latest request's prompt size approximates the live context size.
        usage.context_tokens = max(usage.context_tokens, inp)
    model = row.get("model")
    if isinstance(model, str) and model:
        usage.model = model


def _as_int(value: Any) -> int:
    """Coerce a JSON number to ``int`` (booleans/others → 0)."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _as_float(value: Any) -> float:
    """Coerce a JSON number to ``float`` (booleans/others → 0.0)."""
    if isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _row_to_items(row: dict[str, Any]) -> list[_MirroredItem]:
    """Map one transcript message entry into mirrored items."""
    message = row.get("message")
    if not isinstance(message, dict):
        return []
    role = message.get("role")
    meta = message.get("meta")
    source = meta.get("source") if isinstance(meta, dict) else None
    content = message.get("content")
    if not isinstance(content, list):
        return []
    response_id = str(row.get("id") or "")

    if role == "user" and source == "user":
        text = _join_text_blocks(content)
        return (
            [_MirroredItem(kind="message", role="user", text=text, response_id=response_id)]
            if text
            else []
        )
    if role == "assistant" and source == "model":
        out: list[_MirroredItem] = []
        thinking = _join_thinking_blocks(content)
        if thinking:
            out.append(
                _MirroredItem(
                    kind="reasoning", role="assistant", text=thinking, response_id=response_id
                )
            )
        text = _join_text_blocks(content)
        if text:
            out.append(
                _MirroredItem(kind="message", role="assistant", text=text, response_id=response_id)
            )
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            call_id = block.get("id")
            name = block.get("name")
            if isinstance(call_id, str) and call_id and isinstance(name, str) and name:
                args = block.get("input")
                out.append(
                    _MirroredItem(
                        kind="function_call",
                        response_id=response_id,
                        call_id=call_id,
                        name=name,
                        arguments=json.dumps(args if args is not None else {}, ensure_ascii=False),
                    )
                )
        return out
    if role == "user" and source == "tool":
        out = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            call_id = block.get("tool_use_id")
            if isinstance(call_id, str) and call_id:
                out.append(
                    _MirroredItem(
                        kind="function_call_output",
                        response_id=response_id,
                        call_id=call_id,
                        text=_tool_result_text(block.get("content")),
                    )
                )
        return out
    # Any other entry: not mirrored.
    return []


def _tool_result_text(content: Any) -> str:
    """Extract the text of a Command Code ``tool_result`` block's ``content``."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n\n".join(parts)
    return ""


def _join_text_blocks(content: list[Any]) -> str:
    """Join the ``text`` fields of text blocks, in order."""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n\n".join(parts)


def _join_thinking_blocks(content: list[Any]) -> str:
    """Join the ``thinking`` fields of thinking blocks, in order."""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "thinking":
            text = block.get("thinking")
            if isinstance(text, str) and text:
                parts.append(text)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# posting
# ---------------------------------------------------------------------------


async def _mirror_item(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    item: _MirroredItem,
    agent_name: str,
) -> None:
    """POST one mirrored item (and its turn-status edge) to the Sessions API."""
    url = base_url + _EVENTS_PATH.format(session_id=session_id)
    if item.kind == "reasoning":
        body: dict[str, Any] = {
            "type": "external_output_reasoning_delta",
            "data": {"delta": item.text, "started": True},
        }
    elif item.kind == "function_call":
        body = {
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call",
                "item_data": {
                    "agent": agent_name,
                    "name": item.name,
                    "arguments": item.arguments,
                    "call_id": item.call_id,
                },
                "response_id": item.response_id,
            },
        }
    elif item.kind == "function_call_output":
        body = {
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call_output",
                "item_data": {"call_id": item.call_id, "output": item.text},
                "response_id": item.response_id,
            },
        }
    else:
        content_type = "input_text" if item.role == "user" else "output_text"
        item_data: dict[str, Any] = {
            "role": item.role,
            "content": [{"type": content_type, "text": item.text}],
        }
        if item.role == "assistant":
            item_data["agent"] = agent_name
        body = {
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": item_data,
                "response_id": item.response_id,
            },
        }
    resp = await client.post(url, headers=headers, json=body)
    resp.raise_for_status()

    # Turn-status edges: a user item starts a turn; a finished assistant
    # message ends it (carrying the reply text so a sub-agent parent wakes with
    # the result). Reasoning is transient and posts no edge.
    if item.kind == "message":
        status = "running" if item.role == "user" else "idle"
        output = item.text if item.role == "assistant" else ""
        await _post_status(client, url=url, headers=headers, status=status, output=output)


async def _post_status(
    client: httpx.AsyncClient,
    *,
    url: str,
    headers: dict[str, str],
    status: str,
    output: str,
) -> None:
    """POST one ``external_session_status`` edge."""
    resp = await client.post(
        url,
        headers=headers,
        json={"type": "external_session_status", "data": {"status": status, "output": output}},
    )
    resp.raise_for_status()


async def _post_usage(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    state: _ForwardState,
) -> None:
    """POST cumulative token usage as an ``external_session_usage`` event.

    Mirrors the shared contract claude-/codex-/cursor-native use: cumulative
    SESSION totals (not per-turn deltas) with the model, so the server prices
    the tokens via the model catalog and the web cost badge lights up.
    """
    data: dict[str, Any] = {
        "cumulative_input_tokens": state.input_tokens,
        "cumulative_output_tokens": state.output_tokens,
        "cumulative_cache_read_input_tokens": state.cache_read_tokens,
    }
    if state.context_tokens:
        data["context_tokens"] = state.context_tokens
    if state.cost_usd > 0:
        data["cumulative_cost_usd"] = state.cost_usd
    if state.model:
        data["model"] = state.model
    url = f"{base_url}/v1/sessions/{session_id}/events"
    resp = await client.post(
        url, headers=headers, json={"type": "external_session_usage", "data": data}
    )
    resp.raise_for_status()


async def _patch_external_session_id(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    external_session_id: str,
) -> None:
    """Best-effort PATCH the Omnigent session's ``external_session_id``.

    The server's ``set_external_session_id`` is set-once, so a repeat PATCH is
    harmless. Failures are logged, never raised — mirroring the other native
    forwarders (a missing id only disables resume/fork, not the live mirror).
    """
    url = f"{base_url}/v1/sessions/{session_id}"
    try:
        resp = await client.patch(
            url, headers=headers, json={"external_session_id": external_session_id}
        )
    except httpx.HTTPError:
        logger.warning(
            "commandcode forwarder could not PATCH external_session_id for %s; "
            "resume/fork will start fresh",
            session_id,
        )
        return
    if resp.status_code >= 400:
        logger.warning(
            "commandcode forwarder external_session_id PATCH rejected (%s); session=%s id=%s",
            resp.status_code,
            session_id,
            external_session_id,
        )


# ---------------------------------------------------------------------------
# cursor persistence
# ---------------------------------------------------------------------------


def _read_state(bridge_dir: Path) -> _ForwardState:
    """Load the durable cursor, or a fresh state when absent/invalid."""
    try:
        raw = (bridge_dir / _STATE_FILE).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (FileNotFoundError, json.JSONDecodeError):
        return _ForwardState()
    if not isinstance(data, dict):
        return _ForwardState()
    path = data.get("path")
    model = data.get("model")
    return _ForwardState(
        path=path if isinstance(path, str) and path else None,
        offset=int(data.get("offset") or 0),
        line=int(data.get("line") or 0),
        input_tokens=int(data.get("input_tokens") or 0),
        output_tokens=int(data.get("output_tokens") or 0),
        cache_read_tokens=int(data.get("cache_read_tokens") or 0),
        context_tokens=int(data.get("context_tokens") or 0),
        cost_usd=float(data.get("cost_usd") or 0.0),
        model=model if isinstance(model, str) and model else None,
    )


def _write_state(bridge_dir: Path, state: _ForwardState) -> None:
    """Persist the cursor + cumulative usage atomically."""
    payload = {
        "path": state.path,
        "offset": state.offset,
        "line": state.line,
        "input_tokens": state.input_tokens,
        "output_tokens": state.output_tokens,
        "cache_read_tokens": state.cache_read_tokens,
        "context_tokens": state.context_tokens,
        "cost_usd": state.cost_usd,
        "model": state.model,
    }
    tmp = bridge_dir / (_STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(bridge_dir / _STATE_FILE)


__all__ = [
    "clear_commandcode_bridge_state",
    "forward_commandcode_transcript_to_session",
    "preseed_commandcode_resume_state",
    "supervise_commandcode_forwarder",
]
