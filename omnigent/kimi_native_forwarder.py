"""Mirror a kimi-native TUI session's transcript into the Omnigent web chat.

The kimi-native harness launches the interactive ``kimi`` TUI in a tmux pane and
injects web-UI turns into it (see :mod:`omnigent.kimi_native_bridge`). The TUI's
reply renders live in the embedded terminal, but — unlike the SDK ``KimiExecutor``
— nothing flows the assistant's response back into Omnigent's conversation
transcript (the chat bubbles). This module closes that gap, the kimi analog of
:mod:`omnigent.cursor_native_forwarder`.

Data source: kimi persists each session to an append-only JSONL "wire" log at
``$KIMI_CODE_HOME/sessions/<wd_…>/<session_…>/agents/main/wire.jsonl``. The
native harness points ``KIMI_CODE_HOME`` at ``<bridge_dir>/kimi-code-home`` whose
``sessions/`` is symlinked to the user's global store, so several workspaces'
sessions share the tree; we disambiguate by ``workDir`` (via ``session_index.jsonl``)
and recency. Relevant wire events:

- ``{"type": "turn.prompt", "input": [{"type":"text","text":…}], "origin": {"kind":"user"}}``
  → a user message.
- ``{"type": "context.append_loop_event", "event": {"type": "content.part",
  "part": {"type": "text", "text": …}, "uuid": …}}`` → an assistant message.
  (``part.type == "think"`` is reasoning, mirrored as a transient
  ``external_output_reasoning_delta`` from ``part["think"]``; ``tool.call`` /
  ``tool.result`` events are still skipped — the embedded terminal shows them.)
- ``{"type": "usage.record", "model": …, "usage": {"inputOther", "output",
  "inputCacheRead", "inputCacheCreation"}, "usageScope": "turn"}`` → token
  usage for one LLM call. Accumulated into per-session cumulative totals and
  POSTed as coalesced ``external_session_usage`` events (the server prices
  them into ``total_cost_usd`` via the model catalog; kimi emits no monetary
  data, same as codex-native).
- ``{"type": "llm.request", "model": …, "modelAlias": …}`` → the effective
  model (the provider-resolved id, falling back to the configured alias).
  Mirrored as a deduped ``external_model_change`` so the cost gate and the
  web model picker see the real model — never seeded at spawn (the codex
  pattern: the spawn model must land in ``model_override`` too).

Each mirrored turn is POSTed as an ``external_conversation_item`` to
``/v1/sessions/{id}/events`` (the same shape :mod:`omnigent.kimi_native_hook`
uses for its read-only approval surface). A per-session line offset is persisted
in ``<bridge_dir>/kimi_forwarder.json`` so restarts resume without double-posting.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import httpx

_logger = logging.getLogger(__name__)

#: Poll cadence for new wire-log lines (matches cursor_native_forwarder).
_POLL_INTERVAL_S = 0.25
#: Persisted forwarder state (discovered wire path + high-water line count).
_STATE_FILE = "kimi_forwarder.json"
#: Persisted cumulative usage/model state. Deliberately SEPARATE from the wire
#: cursor: the cursor is disposable (terminal recreation / wire rediscovery
#: start a fresh tail), but the usage totals are session-cumulative — resetting
#: them would make every later post look like a decrease the server ignores.
_USAGE_STATE_FILE = "kimi_usage_state.json"
#: Clock-skew tolerance when matching a session created at/after launch.
_DISCOVER_SKEW_MS = 10_000
#: Supervisor backoff bounds.
_BACKOFF_INITIAL_S = 1.0
_BACKOFF_MAX_S = 30.0

#: One-shot log guard keys (schema drift / unreadable files log once, not per
#: poll). Module-level: the forwarder loop can be restarted by its supervisor.
_WARNED_KEYS: set[str] = set()


def _warn_once(key: str, msg: str, *args: object) -> None:
    """Log *msg* at warning level only the first time *key* is seen."""
    if key in _WARNED_KEYS:
        return
    _WARNED_KEYS.add(key)
    _logger.warning(msg, *args)


@dataclass
class _ForwardState:
    """Durable cursor for the wire-log tail."""

    wire_path: str
    last_line: int


@dataclass
class _UsageState:
    """Durable cumulative usage/model mirror state.

    Survives forwarder restarts, terminal recreation, and wire-log switches
    within the same Omnigent session: the posted fields are cumulative
    (SET-semantics, clamped server-side), so zeroing them mid-session would
    silently drop every later post until fresh totals re-crossed the peak.
    ``model`` / ``posted_model`` are persisted so a restart between an
    ``llm.request`` and its ``usage.record`` neither downgrades attribution
    nor re-posts an unchanged model. ``context_tokens`` is persisted so a
    post that failed right before a restart retries WITH the context
    occupancy. ``billed_wire`` / ``billed_line`` are the billed high-water
    mark (the wire log identity + the last recorded row's line in it):
    totals and mark persist in ONE write, so a crash between recording a
    row and advancing the separate wire cursor cannot re-bill the row on
    restart. The mark is scoped to its wire — line numbers restart per log,
    and a new log's rows must bill regardless of the old log's mark.
    """

    totals: dict[str, int] = field(default_factory=dict)
    model: str | None = None
    posted_model: str | None = None
    context_tokens: int | None = None
    billed_wire: str | None = None
    billed_line: int = -1


@dataclass
class KimiWireItem:
    """Stable parsed-wire contract shared by forwarding and offline import."""

    line_no: int
    role: str
    text: str
    response_id: str
    # "message" (a user/assistant turn → external_conversation_item),
    # "reasoning" (a think block → external_output_reasoning_delta),
    # "turn_end" (an ``end_turn`` step → external_session_status: idle),
    # "usage" (a per-call token record → coalesced external_session_usage), or
    # "model" (an llm.request's effective model → deduped external_model_change).
    kind: str = "message"
    # For kind == "usage": the record's token counts, keyed
    # input_other / output / cache_read / cache_creation.
    usage: dict[str, int] | None = None
    # For kind == "usage": the record's model alias (pricing fallback).
    # For kind == "model": the effective model id.
    model: str | None = None
    # For kind == "usage": the record's wall-clock ``time`` in epoch ms, so
    # the forwarder can skip history that predates this Omnigent session.
    time_ms: int | None = None


_MirrorItem = KimiWireItem


def clear_kimi_bridge_state(bridge_dir: Path) -> None:
    """Drop the stale wire cursor so a new terminal starts a fresh tail.

    Mirrors ``cursor_native_forwarder.clear_cursor_bridge_state``: without this,
    a re-created terminal would resume the prior session's line offset against a
    different wire log. The cumulative usage state (``_USAGE_STATE_FILE``) is
    deliberately KEPT — it belongs to the Omnigent session, not the terminal,
    and zeroing it would silently drop later usage posts (server-side clamps).
    """
    with contextlib.suppress(OSError):
        (bridge_dir / _STATE_FILE).unlink()


def _read_state(bridge_dir: Path) -> _ForwardState | None:
    try:
        raw = (bridge_dir / _STATE_FILE).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    wire_path = data.get("wire_path")
    last_line = data.get("last_line")
    if isinstance(wire_path, str) and isinstance(last_line, int):
        return _ForwardState(wire_path=wire_path, last_line=last_line)
    return None


def _write_state(bridge_dir: Path, state: _ForwardState) -> None:
    payload = {"wire_path": state.wire_path, "last_line": state.last_line}
    tmp = bridge_dir / (_STATE_FILE + ".tmp")
    with contextlib.suppress(OSError):
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(bridge_dir / _STATE_FILE)


#: The cumulative token counters the writer always persists — all four, even
#: as zeros, so a partial ``totals`` on disk can only be corruption.
_TOTAL_KEYS = ("input_other", "output", "cache_read", "cache_creation")

#: The exact top-level field set the writer emits; anything else is corruption.
_STATE_KEYS = frozenset(
    {"totals", "model", "posted_model", "context_tokens", "billed_wire", "billed_line"}
)


def _parse_usage_state(data: object) -> _UsageState | None:
    """Validate a decoded state payload; ``None`` on any schema mismatch.

    Strict on purpose: the only writer always emits exactly this shape, so
    any deviation is corruption. Trusting a partial one is worse than a
    fresh start — a subset of totals next to a valid billed watermark would
    zero the missing counters while the watermark suppresses re-billing: a
    permanent undercount that never self-corrects.
    """

    def _count(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    if not isinstance(data, dict) or set(data) != _STATE_KEYS:
        return None
    totals = data.get("totals")
    if (
        not isinstance(totals, dict)
        or set(totals) != set(_TOTAL_KEYS)
        or not all(_count(v) for v in totals.values())
    ):
        return None
    model = data.get("model")
    posted_model = data.get("posted_model")
    billed_wire = data.get("billed_wire")
    if not all(
        v is None or (isinstance(v, str) and v) for v in (model, posted_model, billed_wire)
    ):
        return None
    context_tokens = data.get("context_tokens")
    if context_tokens is not None and not _count(context_tokens):
        return None
    billed_line = data.get("billed_line")
    if not isinstance(billed_line, int) or isinstance(billed_line, bool):
        return None
    # The watermark persists as a pair: -1 is the no-mark sentinel, so a wire
    # without a real line (or a line without its wire) was never written and
    # cannot be trusted to suppress replay.
    if (billed_wire is None) != (billed_line == -1) or billed_line < -1:
        return None
    return _UsageState(
        totals=dict(totals),
        model=model,
        posted_model=posted_model,
        context_tokens=context_tokens,
        billed_wire=billed_wire,
        billed_line=billed_line,
    )


def _read_usage_state(bridge_dir: Path) -> tuple[_UsageState | None, bool]:
    """Load the persisted usage state as ``(state, trusted)``.

    ``trusted`` is False only on a TRANSIENT failure — the file exists but
    cannot be read right now (EACCES/EIO/…): the already-billed totals are
    then unknown, so callers must suspend billing and re-attempt the read
    rather than start fresh (fresh totals are lower cumulative SET values
    the server's monotonic clamp drops). Any content that is not a valid
    state — empty, undecodable, unparseable, or schema-invalid — is
    confirmed corruption and IS trusted as a fresh start: the only writer
    is atomic (tmp + replace), so re-reading cannot improve on it.
    """
    path = bridge_dir / _USAGE_STATE_FILE

    def _corrupt() -> tuple[None, bool]:
        _warn_once(
            f"usage-state-corrupt:{bridge_dir}",
            "kimi forwarder: usage state %s is corrupt; starting fresh "
            "(previously billed totals are lost)",
            path,
        )
        return None, True

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, True
    except UnicodeDecodeError:
        return _corrupt()
    except OSError as exc:
        _warn_once(
            f"usage-state-unreadable:{bridge_dir}",
            "kimi forwarder: usage state %s unreadable (%s); billing suspended "
            "until it reads again",
            path,
            exc,
        )
        return None, False
    try:
        data = json.loads(raw)
    except ValueError:
        return _corrupt()
    state = _parse_usage_state(data)
    if state is None:
        return _corrupt()
    return state, True


def _write_usage_state(bridge_dir: Path, state: _UsageState) -> bool:
    """Persist the usage state; ``False`` when the write failed (logged once)."""
    payload = {
        "totals": state.totals,
        "model": state.model,
        "posted_model": state.posted_model,
        "context_tokens": state.context_tokens,
        "billed_wire": state.billed_wire,
        "billed_line": state.billed_line,
    }
    tmp = bridge_dir / (_USAGE_STATE_FILE + ".tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(bridge_dir / _USAGE_STATE_FILE)
    except OSError as exc:
        _warn_once(
            f"usage-state-unwritable:{bridge_dir}",
            "kimi forwarder: cannot persist usage state under %s: %s",
            bridge_dir,
            exc,
        )
        return False
    return True


def workdirs_for_kimi_sessions(kimi_home: Path) -> dict[str, str]:
    """Map each session dir → its ``workDir`` from ``session_index.jsonl``.

    Returns ``{}`` when the index is absent/unreadable (a brand-new home before
    kimi has written any session).
    """
    index = kimi_home / "session_index.jsonl"
    mapping: dict[str, str] = {}
    try:
        text = index.read_text(encoding="utf-8")
    except OSError:
        return mapping
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            session_dir = row.get("sessionDir")
            work_dir = row.get("workDir")
            if isinstance(session_dir, str) and isinstance(work_dir, str):
                mapping[session_dir] = work_dir
    return mapping


_workdirs_for_sessions = workdirs_for_kimi_sessions


def _discover_wire(kimi_home: Path, workspace: str, launch_epoch_ms: int) -> Path | None:
    """Locate the wire log for *workspace*'s newest session created at/after launch.

    Globs ``sessions/*/session_*/agents/main/wire.jsonl`` under *kimi_home*,
    keeps only sessions whose ``session_index`` ``workDir`` matches *workspace*
    (when the index lists them), and returns the most-recently-modified wire log
    whose mtime is at/after ``launch_epoch_ms`` (minus skew). Returns ``None``
    until kimi has created the session.
    """
    sessions_root = kimi_home / "sessions"
    if not sessions_root.exists():
        return None
    workdirs = workdirs_for_kimi_sessions(kimi_home)
    floor_s = (launch_epoch_ms - _DISCOVER_SKEW_MS) / 1000.0
    best: tuple[float, Path] | None = None
    for wire in sessions_root.glob("*/session_*/agents/main/wire.jsonl"):
        # session_index keys on the session dir (…/<wd_…>/<session_…>).
        session_dir = str(wire.parent.parent.parent)
        work_dir = workdirs.get(session_dir)
        # When the index doesn't list it yet, fall back to recency alone — a
        # freshly created session may not be indexed until its first turn.
        if work_dir is not None and work_dir != workspace:
            continue
        try:
            mtime = wire.stat().st_mtime
        except OSError:
            continue
        if mtime < floor_s:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, wire)
    return best[1] if best is not None else None


def _input_text(blocks: object) -> str:
    """Concatenate the ``text`` of an ``input`` / ``content`` block list."""
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _token_count(raw: object) -> int | None:
    """A pinned token-count field: a non-boolean, non-negative int, else None."""
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return None


def _usage_counts(raw: object) -> dict[str, int] | None:
    """Validate a ``usage.record``'s ``usage`` dict against the pinned schema.

    All four Kimi Code 0.34.0 fields must be present as non-boolean,
    non-negative ints; anything else returns ``None`` so the whole record is
    skipped — partial/zeroed accounting from schema drift must never be
    emitted (the wire cursor advances irreversibly).
    """
    if not isinstance(raw, dict):
        return None
    counts: dict[str, int] = {}
    for wire_key, out_key in (
        ("inputOther", "input_other"),
        ("output", "output"),
        ("inputCacheRead", "cache_read"),
        ("inputCacheCreation", "cache_creation"),
    ):
        value = _token_count(raw.get(wire_key))
        if value is None:
            return None
        counts[out_key] = value
    return counts


def _row_to_item(line_no: int, row: dict[str, object]) -> KimiWireItem | None:
    """Map one wire-log row to a conversation item, or ``None`` to skip it."""
    row_type = row.get("type")
    if row_type == "usage.record":
        # Pinned to the Kimi Code 0.34.0 shape: only turn-scoped records carry
        # the session's own token spend; an unknown scope is skipped fail-safe.
        if row.get("usageScope") != "turn":
            return None
        counts = _usage_counts(row.get("usage"))
        model = row.get("model")
        time_ms = _token_count(row.get("time"))
        if counts is None or not (isinstance(model, str) and model) or time_ms is None:
            # Schema drift: emit nothing rather than partial/zero accounting.
            _warn_once(
                "usage.record-drift",
                "kimi forwarder: skipping usage.record not matching the pinned "
                "0.34.0 schema (further drift logged at debug): %r",
                row,
            )
            _logger.debug("kimi forwarder: skipped drifted usage.record: %r", row)
            return None
        return KimiWireItem(
            line_no=line_no,
            role="assistant",
            text="",
            response_id=f"kimi:usage:{line_no}",
            kind="usage",
            usage=counts,
            model=model,
            time_ms=time_ms,
        )
    if row_type == "llm.request":
        # Prefer the provider-resolved model id (e.g. ``system.ai.kimi-k3``)
        # over the configured alias (e.g. ``kimi-k3-databricks``).
        model = row.get("model")
        if not (isinstance(model, str) and model):
            model = row.get("modelAlias")
        if not (isinstance(model, str) and model):
            return None
        return KimiWireItem(
            line_no=line_no,
            role="assistant",
            text="",
            response_id=f"kimi:model:{line_no}",
            kind="model",
            model=model,
        )
    if row_type == "turn.prompt":
        origin = row.get("origin")
        if isinstance(origin, dict) and origin.get("kind") != "user":
            return None
        text = _input_text(row.get("input"))
        if not text:
            return None
        return KimiWireItem(
            line_no=line_no,
            role="user",
            text=text,
            response_id=f"kimi:turn:{line_no}",
        )
    if row_type == "context.append_loop_event":
        event = row.get("event")
        if not isinstance(event, dict):
            return None
        event_type = event.get("type")
        if event_type == "step.end":
            # kimi's agent loop keeps stepping while a step stops for ``tool_use``;
            # ``end_turn`` is the only finish reason that ends the turn. Without
            # this edge a native sub-agent never reports terminal status, so a
            # parent orchestrator waits on it forever.
            if event.get("finishReason") != "end_turn":
                return None
            return KimiWireItem(
                line_no=line_no,
                role="assistant",
                text="",
                response_id=f"kimi:turn_end:{line_no}",
                kind="turn_end",
            )
        if event_type != "content.part":
            return None
        part = event.get("part")
        if not isinstance(part, dict):
            return None
        uuid = event.get("uuid")
        response_id = f"kimi:{uuid}" if isinstance(uuid, str) and uuid else f"kimi:line:{line_no}"
        part_type = part.get("type")
        if part_type == "text":
            part_text = part.get("text")
            if not isinstance(part_text, str) or not part_text:
                return None
            return KimiWireItem(
                line_no=line_no,
                role="assistant",
                text=part_text,
                response_id=response_id,
            )
        if part_type == "think":
            # Reasoning lives in ``part["think"]`` (not ``part["text"]``). Mirror it
            # as a transient reasoning event so the web UI paints a thinking block —
            # the kimi analogue of codex-native's #1254 reasoning fix.
            think = part.get("think")
            if not isinstance(think, str) or not think:
                return None
            return KimiWireItem(
                line_no=line_no,
                role="assistant",
                text=think,
                response_id=response_id,
                kind="reasoning",
            )
        return None
    return None


def read_kimi_wire_items(wire_path: Path, last_line: int) -> list[KimiWireItem]:
    """Parse wire-log lines beyond *last_line* into the stable shared contract.

    The wire log is append-only JSONL, so a line count is a stable high-water
    mark. Non-JSON / unrecognized lines advance the cursor without emitting.
    Invalid UTF-8 (a torn concurrent write) is replace-decoded rather than
    raised — a persistently malformed file must not crash-loop the supervisor.
    """
    try:
        text = wire_path.read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        _warn_once(
            f"wire-unreadable:{wire_path}", "kimi forwarder: cannot read %s: %s", wire_path, exc
        )
        return []
    lines = text.splitlines()
    items: list[KimiWireItem] = []
    for idx in range(last_line, len(lines)):
        line = lines[idx].strip()
        if not line:
            continue
        row: object = None
        if line.startswith("{"):
            try:
                row = json.loads(line)
            except ValueError:
                row = None
        if not isinstance(row, dict):
            # Torn write / non-JSON noise: skip, but keep the diagnostic.
            _warn_once(
                f"wire-badrow:{wire_path}",
                "kimi forwarder: skipping unparseable wire row(s) in %s "
                "(first: line %d; further rows logged at debug)",
                wire_path,
                idx + 1,
            )
            _logger.debug("kimi forwarder: unparseable wire row %d: %.200s", idx + 1, line)
            continue
        item = _row_to_item(idx, row)
        if item is not None:
            items.append(item)
    return items


_read_new_items = read_kimi_wire_items


async def _post_conversation_item(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    item: KimiWireItem,
    agent_name: str,
) -> None:
    """POST one mirrored turn as an external conversation item."""
    content_type = "input_text" if item.role == "user" else "output_text"
    item_data: dict[str, object] = {
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
    url = f"{base_url.rstrip('/')}/v1/sessions/{session_id}/events"
    resp = await client.post(url, headers=headers, json=body)
    resp.raise_for_status()


async def _post_external_session_status(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    status: str,
    output: str,
) -> None:
    """POST one ``external_session_status`` event to the Sessions API.

    For a sub-agent conversation the server maps an ``idle`` edge to a terminal
    completion that wakes the parent orchestrator's inbox — the SAME contract
    claude-/codex-/opencode-/cursor-native use. ``output`` carries the turn's
    final assistant text, since the runner delivers an empty result when an idle
    edge forwards none.

    :raises httpx.HTTPError: If the Omnigent request fails or is rejected.
    """
    url = f"{base_url.rstrip('/')}/v1/sessions/{session_id}/events"
    resp = await client.post(
        url,
        headers=headers,
        json={"type": "external_session_status", "data": {"status": status, "output": output}},
    )
    resp.raise_for_status()


async def _post_reasoning_item(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    item: KimiWireItem,
) -> None:
    """POST one mirrored think block as a transient reasoning event.

    Mirrors codex-native (#1254): a one-shot ``external_output_reasoning_delta``
    with ``started: true`` opens a reasoning block in the web UI. Kimi persists
    completed think parts (not streamed deltas), so one delta per part is correct.
    """
    body = {
        "type": "external_output_reasoning_delta",
        "data": {"delta": item.text, "started": True},
    }
    url = f"{base_url.rstrip('/')}/v1/sessions/{session_id}/events"
    resp = await client.post(url, headers=headers, json=body)
    resp.raise_for_status()


class _KimiUsageSync:
    """Mirror kimi token usage and the effective model to Omnigent.

    The kimi analog of codex-native's ``_SessionUsageCoalescer`` +
    ``_sync_model_change``: per-call ``usage.record`` rows accumulate into
    cumulative (SET-semantics) session totals posted as coalesced
    ``external_session_usage`` events with the model riding along on every
    post (the server prices the tokens via the model catalog — kimi emits no
    monetary data). ``llm.request`` rows mirror the effective model as a
    deduped ``external_model_change``; the baseline is never seeded at spawn,
    so the real spawn model lands in ``model_override`` for the cost gate.

    Every post is best-effort: a failure is logged and swallowed so usage
    mirroring can never stall transcript forwarding. Failed posts are NOT
    lost: :meth:`sync` runs on every poll and re-posts whatever still differs
    from the last successfully delivered payload/model, so a turn-final
    failure retries even when no further wire records ever arrive.

    Cumulative usage/model state is persisted to ``_USAGE_STATE_FILE`` so
    forwarder restarts and terminal recreation resume the counters. Design
    ruling: persistence never gates the shared wire cursor — transcript
    liveness wins over usage durability. A failed persist keeps the totals
    in memory (the per-poll sync retries the write); a crash while the
    bridge dir is unwritable undercounts by the delta since the last
    successful persist, which is clamp-safe and mirrors the stateless
    claude/codex forwarder trust model.
    """

    def __init__(
        self,
        *,
        base_url: str,
        headers: dict[str, str],
        session_id: str,
        bridge_dir: Path,
        state: _UsageState | None = None,
        trusted: bool = True,
        billing_floor_ms: int = 0,
    ) -> None:
        self._events_url = f"{base_url.rstrip('/')}/v1/sessions/{session_id}/events"
        self._headers = headers
        self._bridge_dir = bridge_dir
        # Records stamped before this Omnigent session launched belong to a
        # resumed pre-existing kimi session — never billed. STRICT floor: the
        # discovery mtime skew must not leak into billing, or a turn finishing
        # just before terminal recreation would be re-billed.
        self._billing_floor_ms = billing_floor_ms
        # Transiently unreadable prior state: the already-billed totals are
        # unknown, so billing stays suspended — never persisting, so the
        # intact on-disk state can't be clobbered — until a re-read succeeds
        # (fresh-start totals would be clamped away by the server).
        self._suspended = not trusted
        # A persist that failed after the in-memory state advanced; the
        # per-poll sync retries the write. Never gates the wire cursor.
        self._dirty = False
        self._totals: dict[str, int] = dict.fromkeys(_TOTAL_KEYS, 0)
        # Effective model: llm.request's provider-resolved id wins; a
        # usage.record's alias fills in until one is seen.
        self._model: str | None = None
        self._posted_model: str | None = None
        # Latest record's context occupancy (inputOther + inputCacheRead +
        # inputCacheCreation) — NOT cumulative.
        self._context_tokens: int | None = None
        # Billed high-water mark: the wire log identity + the last recorded
        # row's line in it. Persisted atomically WITH the totals, so replaying
        # rows after a crash (the wire cursor persists separately) is
        # idempotent. Scoped per wire: line numbers restart in a new log.
        self._billed_wire: str | None = None
        self._billed_line = -1
        self._last_posted: dict[str, int] | None = None
        self._window_cache: dict[str, int | None] = {}
        if state is not None:
            self._adopt(state)

    def _adopt(self, state: _UsageState) -> None:
        """Take a persisted state as the billing baseline.

        The in-memory model wins when already set (an ``llm.request`` seen
        while suspended is newer than the disk copy).
        """
        for key in _TOTAL_KEYS:
            value = state.totals.get(key)
            if isinstance(value, int) and value >= 0:
                self._totals[key] = value
        self._model = self._model or state.model
        self._posted_model = state.posted_model
        self._context_tokens = state.context_tokens
        self._billed_wire = state.billed_wire
        self._billed_line = state.billed_line

    def _persist(self) -> bool:
        # Suspended = the on-disk state is the trusted copy a transient read
        # failure hid; writing now would clobber it with unknown-baseline
        # in-memory values. Nothing persists until the re-read succeeds.
        if self._suspended:
            return False
        ok = _write_usage_state(
            self._bridge_dir,
            _UsageState(
                totals=dict(self._totals),
                model=self._model,
                posted_model=self._posted_model,
                context_tokens=self._context_tokens,
                billed_wire=self._billed_wire,
                billed_line=self._billed_line,
            ),
        )
        self._dirty = not ok
        return ok

    def _try_recover(self) -> None:
        """Re-attempt reading the state file while suspended.

        Adopts the on-disk baseline once it reads again; a missing or
        confirmed-corrupt file unsuspends as a fresh start (re-reading cannot
        improve on either). Still-transient failures keep the suspension.
        """
        state, trusted = _read_usage_state(self._bridge_dir)
        if not trusted:
            return
        if state is not None:
            self._adopt(state)
        self._suspended = False
        _logger.info("kimi forwarder: usage state readable again; billing resumed")
        # Anything adopted in memory while suspended (e.g. a newer model from
        # an llm.request) must reach disk now — the wire cursor may already be
        # past the row that carried it. A failed write retries via the dirty
        # flag and never gates the cursor.
        self._persist()

    def note_new_wire(self) -> None:
        """Adopt a freshly discovered wire log.

        Only the per-log view resets (context occupancy, delivery dedup); the
        cumulative totals and model carry forward — they are session-scoped,
        and a zero-reset would make every later post a server-ignored
        decrease. The billed mark needs no reset: it is scoped to its wire
        identity, so a different log's rows never compare against it.
        """
        self._context_tokens = None
        self._last_posted = None

    def record(self, item: KimiWireItem, *, wire: str) -> None:
        """Fold one validated ``usage.record`` item into the cumulative totals.

        Skips rows at/below the billed high-water mark within the same *wire*
        (a crash between the totals write and the wire-cursor write replays
        rows on restart) and rows stamped before the billing floor (a resumed
        session's history). Timestamps gate ONLY the floor — replay
        idempotency is per-wire line-based, so a clock regression can never
        suppress a later legitimate row.

        Never gates the caller: a failed persist keeps the totals in memory
        and the per-poll sync retries the write; while suspended (prior state
        transiently unreadable) rows are dropped rather than billed against an
        unknown baseline — both directions undercount at worst, which the
        server's monotonic clamp makes safe.
        """
        if self._suspended:
            # The prior state may have become readable since the last poll:
            # recover first so this poll's rows bill instead of dropping.
            self._try_recover()
        if self._suspended:
            _warn_once(
                f"usage-suspended-skip:{wire}",
                "kimi forwarder: dropping usage.record row(s) from %s while the "
                "usage state is unreadable (billing suspended)",
                wire,
            )
            return
        time_ms = item.time_ms
        if time_ms is None or time_ms < self._billing_floor_ms:
            _warn_once(
                f"usage-floor-skip:{wire}",
                "kimi forwarder: skipping pre-launch usage.record row(s) in %s "
                "(resumed session history is not billed)",
                wire,
            )
            return
        if wire == self._billed_wire and item.line_no <= self._billed_line:
            _warn_once(
                f"usage-replay-skip:{wire}",
                "kimi forwarder: skipping already-billed usage.record row(s) replayed from %s",
                wire,
            )
            return
        usage = item.usage or {}
        for key in _TOTAL_KEYS:
            self._totals[key] += usage.get(key, 0)
        self._context_tokens = (
            usage.get("input_other", 0)
            + usage.get("cache_read", 0)
            + usage.get("cache_creation", 0)
        )
        if self._model is None and item.model:
            self._model = item.model
        self._billed_wire = wire
        self._billed_line = item.line_no
        self._persist()

    def note_model(self, model: str | None) -> None:
        """Adopt the effective model from an ``llm.request`` row."""
        if not model or model == self._model:
            return
        self._model = model
        self._persist()

    async def sync(self, client: httpx.AsyncClient) -> None:
        """Deliver any undelivered model change and usage totals (best-effort).

        Cheap no-op when everything already matches the delivered baseline;
        called per poll so a previously failed post retries without waiting
        for another wire record. While suspended, only the state-file re-read
        happens — no posts (the baseline is unknown) and no persists (the
        intact on-disk state must not be clobbered).
        """
        if self._suspended:
            self._try_recover()
            if self._suspended:
                return
        if self._dirty:
            # Retry a previously failed persist so a crash loses at most the
            # delta since the last successful write.
            self._persist()
        if self._model and self._model != self._posted_model:
            if await self._post(client, "external_model_change", {"model": self._model}):
                self._posted_model = self._model
                self._persist()
        await self._flush(client)

    async def _flush(self, client: httpx.AsyncClient) -> None:
        """POST the cumulative totals when they differ from the delivered ones."""
        # Nothing accumulated yet (fresh session, no restored state): posting
        # zeros would SET the server's token fields to 0.
        if self._context_tokens is None and not any(self._totals.values()):
            return
        # The server's cumulative_input_tokens is INCLUSIVE of cache reads (it
        # splits cumulative_cache_read_input_tokens back out to price them at
        # the cache-read rate). There is no dedicated cumulative cache-creation
        # field, so creation tokens fold into the input total — they price at
        # the full input rate, the accepted approximation.
        payload: dict[str, int] = {
            "cumulative_input_tokens": (
                self._totals["input_other"]
                + self._totals["cache_creation"]
                + self._totals["cache_read"]
            ),
            "cumulative_cache_read_input_tokens": self._totals["cache_read"],
            "cumulative_output_tokens": self._totals["output"],
        }
        if self._context_tokens is not None:
            payload["context_tokens"] = self._context_tokens
        window = await self._context_window()
        if window is not None:
            payload["context_window"] = window
        if payload == self._last_posted:
            return
        # The model rides along on every token post (outside the dedup): the
        # server reprices the cumulative totals per post and needs it each time.
        body: dict[str, object] = dict(payload)
        if self._model:
            body["model"] = self._model
        if await self._post(client, "external_session_usage", body):
            self._last_posted = payload

    async def _context_window(self) -> int | None:
        """Resolve the effective model's context window, or ``None``.

        Uses the model-catalog helpers (never ``llm.request.maxTokens``,
        which is the max *output* tokens) and caches per model id. Omitted
        when no metadata source resolves the model — a guessed default would
        draw a wrong context ring.
        """
        model = self._model
        if not model:
            return None
        if model not in self._window_cache:
            from omnigent.llms.context_window import find_model_context_window

            try:
                window = await asyncio.to_thread(find_model_context_window, model)
            except Exception:
                # Best-effort boundary: a metadata lookup failure must not
                # stop usage mirroring; the window is simply omitted.
                _logger.exception("kimi forwarder: context-window lookup failed for %s", model)
                window = None
            self._window_cache[model] = window
        return self._window_cache[model]

    async def _post(
        self, client: httpx.AsyncClient, event_type: str, data: dict[str, object]
    ) -> bool:
        """POST one session event; log-and-swallow failures (best-effort)."""
        try:
            resp = await client.post(
                self._events_url, headers=self._headers, json={"type": event_type, "data": data}
            )
        except httpx.HTTPError as exc:
            _logger.warning("kimi forwarder: %s POST failed: %s", event_type, exc)
            return False
        if resp.status_code >= 400:
            _logger.warning(
                "kimi forwarder: %s POST rejected with HTTP %d", event_type, resp.status_code
            )
            return False
        return True


async def forward_kimi_wire_to_session(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    kimi_home: Path,
    workspace: str,
    launch_epoch_ms: int,
    agent_name: str = "kimi-native-ui",
) -> None:
    """Poll the kimi session wire log and mirror new turns into the chat.

    Runs until cancelled. Discovers the wire log lazily (kimi writes it after the
    first turn), then tails it, POSTing each new user/assistant turn and
    persisting the line offset after every post.
    """
    # Route the transcript mirror to the replica holding this session's runner
    # tunnel: the POST /events is published to that pod's in-process session
    # stream, so an off-replica POST persists the item (shows on reload) but the
    # live SSE tail never sees it ("no stream until refresh"). Unlike the other
    # native forwarders, this client carries no _RunnerDatabricksAuth (whose
    # auth_flow would stamp the key), so key the shared headers dict directly
    # from the runner-env host_id (databricks_request_headers with no explicit
    # host_id reads OMNIGENT_RUNNER_SLICE_KEY; emitted only on the workspace
    # mount). One point covers the client default + every helper POST below,
    # which all forward this same dict.
    from omnigent.cli_auth import databricks_request_headers

    headers = {**headers, **databricks_request_headers(base_url)}
    state = _read_state(bridge_dir)
    wire_path = Path(state.wire_path) if state is not None else None
    last_line = state.last_line if state is not None else 0
    usage_state, usage_state_trusted = _read_usage_state(bridge_dir)
    usage_sync = _KimiUsageSync(
        base_url=base_url,
        headers=headers,
        session_id=session_id,
        bridge_dir=bridge_dir,
        state=usage_state,
        trusted=usage_state_trusted,
        # STRICT: records stamped before launch are a resumed session's
        # history (the mtime skew applies to discovery only, never billing).
        billing_floor_ms=launch_epoch_ms,
    )
    # Final assistant text of the turn in flight, forwarded on the ``end_turn``
    # edge so the parent's inbox gets the real result instead of an empty one.
    last_assistant_text = ""
    async with httpx.AsyncClient(timeout=15.0) as client:
        while True:
            if wire_path is None or not wire_path.exists():
                discovered = await asyncio.to_thread(
                    _discover_wire, kimi_home, workspace, launch_epoch_ms
                )
                if discovered is not None and discovered != wire_path:
                    wire_path = discovered
                    last_line = 0
                    # Cursor resets; cumulative usage totals carry forward.
                    usage_sync.note_new_wire()
                    _write_state(bridge_dir, _ForwardState(str(wire_path), last_line))
            if wire_path is not None and wire_path.exists():
                items = await asyncio.to_thread(read_kimi_wire_items, wire_path, last_line)
                for item in items:
                    try:
                        if item.kind == "usage":
                            # Accumulate only; delivery happens in the per-poll
                            # sync below. Never gates the cursor: transcript
                            # liveness wins over usage durability (recorded
                            # design ruling — see the class docstring).
                            usage_sync.record(item, wire=str(wire_path))
                        elif item.kind == "model":
                            usage_sync.note_model(item.model)
                        elif item.kind == "turn_end":
                            await _post_external_session_status(
                                client,
                                base_url=base_url,
                                headers=headers,
                                session_id=session_id,
                                status="idle",
                                output=last_assistant_text,
                            )
                            last_assistant_text = ""
                            # Turn boundary: deliver the final totals promptly.
                            await usage_sync.sync(client)
                        elif item.kind == "reasoning":
                            await _post_reasoning_item(
                                client,
                                base_url=base_url,
                                headers=headers,
                                session_id=session_id,
                                item=item,
                            )
                        else:
                            await _post_conversation_item(
                                client,
                                base_url=base_url,
                                headers=headers,
                                session_id=session_id,
                                item=item,
                                agent_name=agent_name,
                            )
                            if item.role == "assistant":
                                last_assistant_text = item.text
                    except httpx.HTTPError as exc:
                        _logger.warning("kimi forwarder: POST failed (will retry): %s", exc)
                        break
                    last_line = item.line_no + 1
                    _write_state(bridge_dir, _ForwardState(str(wire_path), last_line))
                # Deliver pending usage/model every poll (no-op when nothing
                # changed): retries any previously failed post even when no
                # further wire records arrive, e.g. a turn-final failure.
                await usage_sync.sync(client)
            await asyncio.sleep(_POLL_INTERVAL_S)


async def supervise_kimi_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    kimi_home: Path,
    workspace: str,
    launch_epoch_ms: int,
    agent_name: str = "kimi-native-ui",
) -> None:
    """Run :func:`forward_kimi_wire_to_session` with restart-on-crash backoff.

    Propagates :class:`asyncio.CancelledError` cleanly (terminal teardown), but
    restarts on any other exception with exponential backoff — mirrors
    ``cursor_native_forwarder.supervise_cursor_forwarder``.
    """
    backoff = _BACKOFF_INITIAL_S
    while True:
        try:
            await forward_kimi_wire_to_session(
                base_url=base_url,
                headers=headers,
                session_id=session_id,
                bridge_dir=bridge_dir,
                kimi_home=kimi_home,
                workspace=workspace,
                launch_epoch_ms=launch_epoch_ms,
                agent_name=agent_name,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("kimi forwarder crashed for session %s; restarting", session_id)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)
        else:
            return
