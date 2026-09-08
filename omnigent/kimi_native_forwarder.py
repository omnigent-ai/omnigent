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
``sessions/`` store is session-private (see
:mod:`omnigent.kimi_native_credentials`), so discovery only ever sees this
session's own wire logs; ``workDir`` (via ``session_index.jsonl``) and recency
narrow it further. Relevant wire events:

- ``{"type": "turn.prompt", "input": [{"type":"text","text":…}], "origin": {"kind":"user"}}``
  → a user message.
- ``{"type": "context.append_loop_event", "event": {"type": "content.part",
  "part": {"type": "text", "text": …}, "uuid": …}}`` → an assistant message.
  (``part.type == "think"`` is reasoning, mirrored as a transient
  ``external_output_reasoning_delta`` from ``part["think"]``; ``tool.call`` /
  ``tool.result`` events are never posted — the embedded terminal shows them —
  but are tracked so a long-running tool doesn't look like an orphaned turn.)
- ``{"type": "turn.ended", "reason": "completed" | "failed" | "cancelled", …}``
  → the authoritative terminal record for a turn. Failed and cancelled turns
  emit no ``step.end`` at all (see ``tests/fixtures/kimi_wire``), so ``step.end``
  finish reasons are never treated as turn edges.
- ``{"type": "usage.record", "model": …, "usage": {"inputOther", "output",
  "inputCacheRead", "inputCacheCreation"}, "usageScope": "turn"}`` → token
  usage for one LLM call. Accumulated into per-session cumulative totals and
  POSTed as coalesced ``external_session_usage`` events. Kimi emits no
  monetary data, so the forwarder prices the totals itself as a sum over
  per-model segments (``cumulative_cost_usd``); when a segment's model has
  no resolvable pricing the cost is omitted and the server falls back to
  pricing the token totals at the current model.
- ``{"type": "llm.request", "model": …, "modelAlias": …}`` → the effective
  model (the provider-resolved id, falling back to the configured alias).
  Mirrored as a deduped ``external_model_change`` so the cost gate and the
  web model picker see the real model — never seeded at spawn (the codex
  pattern: the spawn model must land in ``model_override`` too).

Each mirrored turn is POSTed as an ``external_conversation_item`` to
``/v1/sessions/{id}/events`` (the same shape :mod:`omnigent.kimi_native_hook`
uses for its read-only approval surface). A per-session byte offset (plus the
line count that keys stable response ids) is persisted in
``<bridge_dir>/kimi_forwarder.json`` so restarts resume without double-posting
and each poll reads only the log's new tail.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnigent.llms.context_window import ModelPricing

import httpx

from omnigent._native_forwarder_health import (
    note_post_success as note_native_post_success,
)
from omnigent._native_forwarder_health import (
    record_post_failure as record_native_post_failure,
)

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
#: Supervisor backoff bounds (also reused for per-POST retry backoff).
_BACKOFF_INITIAL_S = 1.0
_BACKOFF_MAX_S = 30.0
#: Consecutive 4xx rejections of one item before it is dropped so a single
#: poison line cannot stall the tail (and any turn edge behind it) forever.
_POISON_MAX_ATTEMPTS = 3
#: 4xx statuses that are transient (auth lapse, rate limit, contention), never
#: poison: the item is retried with backoff instead of being discarded.
_TRANSIENT_POST_STATUSES = frozenset({401, 403, 408, 409, 425, 429})
#: Quiet-wire window before an in-flight turn is closed as idle. Deliberately
#: long: kimi appends wire rows only at part/step boundaries, so a slow model
#: step can be legitimately silent for minutes — closing a live turn early
#: would falsely complete a sub-agent, which is worse than closing late.
_TURN_QUIESCENCE_S = 300.0
#: Ceiling on wire silence while a tool call is in flight. A tool may
#: legitimately run for many minutes, but a wire with NO row of any kind for
#: this long means the tool (or kimi itself) hung — fail the turn instead of
#: suppressing quiescence forever against an alive-but-wedged pane. Accepted
#: tradeoff: a zero-row tool can strand a turn for up to this long; per-tool
#: heartbeats/limits are deliberately not implemented.
_TOOL_QUIESCENCE_S = 1800.0
#: Continuous edge-delivery failure past this long logs an error (rate-limited
#: to the same interval) so the outage is visible without extra plumbing.
_EDGE_FAILURE_ALERT_S = 300.0
#: Minimum silence window left after a restart re-seeds the activity clock
#: (a few poll intervals): a forward wall-clock jump (sleep/wake, NTP step)
#: must not read as elapsed silence and fire a watchdog before the resumed
#: kimi has had a moment to write a row.
_RESTART_GRACE_S = 1.0
#: Sentinel: the wire's first line is partial or unparseable (mid-write) —
#: defer adoption for this discovery cycle instead of trusting the coarse floor.
_HEADER_UNREADABLE = object()

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
    """Durable cursor for the wire-log tail plus the mirrored turn lifecycle.

    ``turn_open``/``tools_in_flight`` keep the pane-death and quiescence
    fallbacks armed (or suppressed) across a forwarder restart mid-turn;
    ``last_edge_id`` dedupes a turn-edge POST replayed after a crash landed
    between the POST and the cursor persist.
    """

    wire_path: str
    last_line: int
    offset: int
    turn_open: bool = False
    tools_in_flight: int = 0
    last_edge_id: str | None = None
    dropped_edge_status: str | None = None
    dropped_edge_output: str = ""
    # The in-flight turn's latest assistant text, persisted so a restart
    # landing between the assistant message and its turn edge still forwards
    # the real result on the edge instead of an empty output.
    last_assistant_text: str = ""
    # Monotone count of observed user prompts, persisted so fallback edge ids
    # stay unique per turn even when the delivery cursor is stalled (failed
    # POSTs freeze last_line, and a line-only id would collide across turns).
    turn_seq: int = 0
    # Wall-clock of the last genuine wire activity, so a restart resumes the
    # quiescence timers instead of re-arming their full windows.
    last_activity_ts: float | None = None
    # High-water of wire bytes observed (>= the delivery cursor), so a crash
    # loop replaying undelivered rows can't refresh the timers every restart.
    last_seen_offset: int | None = None
    # Filesystem identity of this path's current generation. A replacement can
    # be larger than the old cursor, so size regression alone cannot detect it.
    wire_dev: int | None = None
    wire_ino: int | None = None
    # Physical size observed on the prior poll. A dead writer's unterminated
    # final row is consumed only after this size remains stable across reads.
    last_observed_size: int | None = None
    # Last user row that opened lifecycle state. Kept after fallback closure so
    # a stalled-cursor redelivery cannot reopen and close the same turn again.
    last_prompt_id: str | None = None


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
    occupancy. ``billed`` maps each wire log to its billed high-water line
    (the last recorded row's line in that log): totals and marks persist in
    ONE write, so a crash between recording a row and advancing the
    separate wire cursor cannot re-bill the row on restart. Marks are kept
    PER wire (bounded, oldest-touched pruned): discovery can bounce between
    logs (A→B→A), and a single mark overwritten by B would re-bill A's
    rows on return.
    ``by_model`` splits the same cumulative totals per effective model (the
    model active when each record accrued), so the posted cost can price
    each segment at its own rate — repricing the whole total at the current
    model would mis-charge every mid-session model switch.
    """

    totals: dict[str, int] = field(default_factory=dict)
    model: str | None = None
    posted_model: str | None = None
    context_tokens: int | None = None
    billed: dict[str, int] = field(default_factory=dict)
    by_model: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class KimiWireItem:
    """Stable parsed-wire contract shared by forwarding and offline import."""

    line_no: int
    role: str
    text: str
    response_id: str
    # "message" (a user/assistant turn → external_conversation_item),
    # "reasoning" (a think block → external_output_reasoning_delta),
    # "turn_end" (turn.ended completed/cancelled → external_session_status:
    # idle), "turn_failed" (turn.ended failed → status: failed),
    # "tool_call"/"tool_result" (never posted; in-flight tool bookkeeping),
    # "usage" (a per-call token record → coalesced external_session_usage), or
    # "model" (an llm.request's effective model → deduped external_model_change).
    kind: str = "message"
    # Byte offset just past this item's wire line; the tail cursor after it posts.
    offset_after: int = 0
    # For kind == "usage": the record's token counts, keyed
    # input_other / output / cache_read / cache_creation.
    usage: dict[str, int] | None = None
    # For kind == "usage": the record's model alias (pricing fallback).
    # For kind == "model": the effective model id.
    model: str | None = None
    # For kind == "usage" and the turn-edge kinds: the row's wall-clock
    # ``time`` in epoch ms, so the forwarder can skip history that predates
    # this Omnigent session (billing floor / historical-edge gate).
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
    offset = data.get("offset")
    last_edge_id = data.get("last_edge_id")
    tools_in_flight = data.get("tools_in_flight")
    dropped_edge_status = data.get("dropped_edge_status")
    dropped_edge_output = data.get("dropped_edge_output")
    last_activity_ts = data.get("last_activity_ts")
    last_seen_offset = data.get("last_seen_offset")
    wire_dev = data.get("wire_dev")
    wire_ino = data.get("wire_ino")
    last_observed_size = data.get("last_observed_size")
    last_prompt_id = data.get("last_prompt_id")
    if isinstance(wire_path, str) and isinstance(last_line, int) and isinstance(offset, int):
        return _ForwardState(
            wire_path=wire_path,
            last_line=last_line,
            offset=offset,
            turn_open=data.get("turn_open") is True,
            tools_in_flight=tools_in_flight if isinstance(tools_in_flight, int) else 0,
            last_edge_id=last_edge_id if isinstance(last_edge_id, str) else None,
            dropped_edge_status=(
                dropped_edge_status if isinstance(dropped_edge_status, str) else None
            ),
            dropped_edge_output=(
                dropped_edge_output if isinstance(dropped_edge_output, str) else ""
            ),
            last_assistant_text=(
                data["last_assistant_text"]
                if isinstance(data.get("last_assistant_text"), str)
                else ""
            ),
            last_activity_ts=(
                float(last_activity_ts) if isinstance(last_activity_ts, (int, float)) else None
            ),
            last_seen_offset=last_seen_offset if isinstance(last_seen_offset, int) else None,
            wire_dev=wire_dev if isinstance(wire_dev, int) else None,
            wire_ino=wire_ino if isinstance(wire_ino, int) else None,
            last_observed_size=(
                last_observed_size if isinstance(last_observed_size, int) else None
            ),
            last_prompt_id=last_prompt_id if isinstance(last_prompt_id, str) else None,
            turn_seq=(
                data["turn_seq"]
                if isinstance(data.get("turn_seq"), int)
                and not isinstance(data.get("turn_seq"), bool)
                else 0
            ),
        )
    return None


def _write_state(bridge_dir: Path, state: _ForwardState) -> None:
    payload = {
        "wire_path": state.wire_path,
        "last_line": state.last_line,
        "offset": state.offset,
        "turn_open": state.turn_open,
        "tools_in_flight": state.tools_in_flight,
        "last_edge_id": state.last_edge_id,
        "dropped_edge_status": state.dropped_edge_status,
        "dropped_edge_output": state.dropped_edge_output,
        "last_assistant_text": state.last_assistant_text,
        "last_activity_ts": state.last_activity_ts,
        "last_seen_offset": state.last_seen_offset,
        "wire_dev": state.wire_dev,
        "wire_ino": state.wire_ino,
        "last_observed_size": state.last_observed_size,
        "last_prompt_id": state.last_prompt_id,
        "turn_seq": state.turn_seq,
    }
    tmp = bridge_dir / (_STATE_FILE + ".tmp")
    with contextlib.suppress(OSError):
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(bridge_dir / _STATE_FILE)


#: The cumulative token counters the writer always persists — all four, even
#: as zeros, so a partial ``totals`` on disk can only be corruption.
_TOTAL_KEYS = ("input_other", "output", "cache_read", "cache_creation")

#: The full per-model segment key set: the four token counters, the cost
#: accrued so far for this segment, and the token counts already folded into
#: that cost (``priced``) — so new usage prices only the DELTA at the
#: then-current rate, making cumulative cost a true running sum.
_SEGMENT_KEYS = frozenset({*_TOTAL_KEYS, "cost_usd", "priced"})


def _new_segment() -> dict[str, Any]:
    """A zeroed per-model segment (tokens, accrued cost, priced snapshot)."""
    segment: dict[str, Any] = dict.fromkeys(_TOTAL_KEYS, 0)
    segment["cost_usd"] = 0.0
    segment["priced"] = dict.fromkeys(_TOTAL_KEYS, 0)
    return segment


def _copy_segment(segment: dict[str, Any]) -> dict[str, Any]:
    copied = dict(segment)
    priced = segment.get("priced")
    copied["priced"] = dict(priced) if isinstance(priced, dict) else dict.fromkeys(_TOTAL_KEYS, 0)
    return copied


#: The exact top-level field set the writer emits; anything else is corruption.
_STATE_KEYS = frozenset(
    {"totals", "model", "posted_model", "context_tokens", "billed", "by_model"}
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
    if not all(v is None or (isinstance(v, str) and v) for v in (model, posted_model)):
        return None
    context_tokens = data.get("context_tokens")
    if context_tokens is not None and not _count(context_tokens):
        return None
    # Per-wire billed high-water lines: every entry must be a real mark
    # (non-empty wire path, non-negative line) — a malformed entry cannot be
    # trusted to suppress replay and taints the whole state.
    billed = data.get("billed")
    if not isinstance(billed, dict):
        return None
    for billed_wire, billed_line in billed.items():
        if not (isinstance(billed_wire, str) and billed_wire) or not _count(billed_line):
            return None
    by_model = data.get("by_model")
    if not isinstance(by_model, dict):
        return None
    for segment_model, segment in by_model.items():
        if not (isinstance(segment_model, str) and segment_model):
            return None
        if not isinstance(segment, dict) or set(segment) != _SEGMENT_KEYS:
            return None
        if not all(_count(segment[key]) for key in _TOTAL_KEYS):
            return None
        cost_usd = segment.get("cost_usd")
        if (
            isinstance(cost_usd, bool)
            or not isinstance(cost_usd, (int, float))
            or not math.isfinite(cost_usd)
            or cost_usd < 0
        ):
            return None
        priced = segment.get("priced")
        if (
            not isinstance(priced, dict)
            or set(priced) != set(_TOTAL_KEYS)
            or not all(_count(v) for v in priced.values())
        ):
            return None
        # The priced snapshot can only trail the tokens (cost is accrued from
        # deltas, never un-accrued): a snapshot ahead of the tokens would
        # under-price every later delta forever.
        if any(priced[key] > segment[key] for key in _TOTAL_KEYS):
            return None
    # The writer folds every record into totals AND its model segment in the
    # same call, so diverging sums are corruption (a partial write would let
    # the priced per-segment cost drift from the token totals forever).
    for key in _TOTAL_KEYS:
        if sum(segment[key] for segment in by_model.values()) != totals[key]:
            return None
    return _UsageState(
        totals=dict(totals),
        model=model,
        posted_model=posted_model,
        context_tokens=context_tokens,
        billed=dict(billed),
        by_model={m: _copy_segment(seg) for m, seg in by_model.items()},
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
        "billed": state.billed,
        "by_model": state.by_model,
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


def _wire_created_at_ms(wire: Path) -> int | None | object:
    """``created_at`` (ms epoch) of the wire's first row — kimi's metadata header.

    Returns ``None`` only for a COMPLETE first line that carries no timestamp;
    a partial or unparseable first line (mid-write) returns
    :data:`_HEADER_UNREADABLE` so discovery retries next poll instead of
    falling back to the coarse mtime floor.
    """
    try:
        with open(wire, "rb") as fh:
            first = fh.readline(4096)
    except OSError:
        return _HEADER_UNREADABLE
    if not first.endswith(b"\n"):
        return _HEADER_UNREADABLE
    try:
        row = json.loads(first)
    except ValueError:
        return _HEADER_UNREADABLE
    if isinstance(row, dict):
        created_at = row.get("created_at")
        if isinstance(created_at, int):
            return created_at
    return None


def _discover_wire(kimi_home: Path, workspace: str, launch_epoch_ms: int) -> Path | None:
    """Locate the wire log for *workspace*'s newest session created at/after launch.

    Globs ``sessions/*/session_*/agents/main/wire.jsonl`` under *kimi_home*,
    keeps only sessions whose ``session_index`` ``workDir`` matches *workspace*
    (when the index lists them), and returns the most-recently-modified wire log
    whose mtime is at/after ``launch_epoch_ms``. Returns ``None`` until kimi has
    created the session.
    """
    sessions_root = kimi_home / "sessions"
    if not sessions_root.exists():
        return None
    workdirs = workdirs_for_kimi_sessions(kimi_home)
    # Pin adoption to the exact launch epoch: the private sessions store
    # survives terminal re-creation, so a prior launch's wire that ended just
    # before this launch must not be re-adopted. A whole-second mtime may be
    # filesystem truncation, so it only needs to reach the launch second.
    floor_ns = launch_epoch_ms * 1_000_000
    floor_coarse_ns = (launch_epoch_ms // 1000) * 1_000_000_000
    best: tuple[int, Path] | None = None
    for wire in sessions_root.glob("*/session_*/agents/main/wire.jsonl"):
        # session_index keys on the session dir (…/<wd_…>/<session_…>).
        session_dir = str(wire.parent.parent.parent)
        work_dir = workdirs.get(session_dir)
        # When the index doesn't list it yet, fall back to recency alone — a
        # freshly created session may not be indexed until its first turn.
        if work_dir is not None and work_dir != workspace:
            continue
        try:
            mtime_ns = wire.stat().st_mtime_ns
        except OSError:
            continue
        precise = mtime_ns % 1_000_000_000 != 0
        if mtime_ns < (floor_ns if precise else floor_coarse_ns):
            continue
        if not precise and mtime_ns < floor_ns:
            # A truncated mtime in the launch second can't tell before from
            # after; let the wire's own metadata header break the tie, keeping
            # the coarse floor only when a COMPLETE first line has no timestamp.
            created_at = _wire_created_at_ms(wire)
            if created_at is _HEADER_UNREADABLE:
                # Header mid-write: defer this candidate to the next poll.
                continue
            if isinstance(created_at, int) and created_at < launch_epoch_ms:
                continue
        if best is None or mtime_ns > best[0]:
            best = (mtime_ns, wire)
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
            time_ms=_token_count(row.get("time")),
        )
    if row_type == "turn.ended":
        # The authoritative terminal record: failed and cancelled turns write no
        # step.end at all, so step.end finish reasons never drive turn edges.
        # ``time`` rides along (real 0.34.0 rows carry it) so the forward loop
        # can drop a resumed wire's HISTORICAL edges instead of replaying a
        # dead turn's terminal status over a live one.
        reason = row.get("reason")
        edge_time_ms = _token_count(row.get("time"))
        if reason in ("completed", "cancelled"):
            return KimiWireItem(
                line_no=line_no,
                role="assistant",
                text="",
                response_id=f"kimi:turn_end:{line_no}",
                kind="turn_end",
                time_ms=edge_time_ms,
            )
        if reason == "failed":
            error = row.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            return KimiWireItem(
                line_no=line_no,
                role="assistant",
                text=message if isinstance(message, str) else "",
                response_id=f"kimi:turn_failed:{line_no}",
                kind="turn_failed",
                time_ms=edge_time_ms,
            )
        # Unknown vocabulary: never guess "failed" — leave the turn open for
        # the pane-death/quiescence fallbacks to close.
        _logger.warning("kimi forwarder: unknown turn.ended reason %r (line %d)", reason, line_no)
        return None
    if row_type == "context.append_loop_event":
        event = row.get("event")
        if not isinstance(event, dict):
            return None
        event_type = event.get("type")
        if event_type in ("tool.call", "tool.result"):
            # Bookkeeping only (never posted): an open tool call keeps the wire
            # legitimately silent, which must suppress the quiescence fallback.
            kind = "tool_call" if event_type == "tool.call" else "tool_result"
            return KimiWireItem(
                line_no=line_no,
                role="assistant",
                text="",
                response_id=f"kimi:{kind}:{line_no}",
                kind=kind,
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


def read_new_kimi_wire_items(
    wire_path: Path,
    offset: int,
    line_no: int,
    *,
    include_unterminated: bool = False,
) -> tuple[list[KimiWireItem], int, int]:
    """Parse newline-terminated wire rows past byte *offset* into items.

    Incremental tail read: only the bytes past *offset* are read, up to the last
    complete line (a partially-written trailing line is left for the next poll).
    When the caller has established that the writer is dead and the file size
    is stable, *include_unterminated* consumes the final bytes as one row.
    *line_no* is the 0-based number of the first unread line, carried so item
    response ids stay stable across polls. A file smaller than *offset* (a
    recreated log) restarts the tail from the top. Returns
    ``(items, new_offset, new_line_no)`` covering every consumed line; each
    item's ``offset_after``/``line_no`` lets the caller advance per posted item.
    """
    try:
        size = wire_path.stat().st_size
    except OSError:
        return [], offset, line_no
    if size < offset:
        offset, line_no = 0, 0
    if size == offset:
        return [], offset, line_no
    try:
        with open(wire_path, "rb") as fh:
            fh.seek(offset)
            data = fh.read(size - offset)
    except OSError:
        return [], offset, line_no
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        if not include_unterminated:
            return [], offset, line_no
        consumed = data
    elif include_unterminated:
        consumed = data
    else:
        consumed = data[: last_nl + 1]
    items: list[KimiWireItem] = []
    pos = offset
    parts = consumed.split(b"\n")
    for index, raw in enumerate(parts):
        terminated = index < len(parts) - 1
        if not terminated and not raw:
            continue
        pos += len(raw) + int(terminated)
        this_line = line_no
        line_no += 1
        line = raw.decode("utf-8", errors="replace").strip()
        if not line or not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        item = _row_to_item(this_line, row)
        if item is not None:
            item.offset_after = pos
            items.append(item)
    return items, offset + len(consumed), line_no


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
    post. Kimi emits no monetary data, so the totals are also split per
    effective model and priced client-side as a sum over segments
    (``cumulative_cost_usd`` — see :meth:`_cumulative_cost`), keeping cost
    correct across mid-session model switches under the server's monotonic
    clamp. ``llm.request`` rows mirror the effective model as a
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
        # FIFO request attribution. Kimi can overlap requests, so the latest
        # session model is not necessarily the model for the next usage row.
        self._pending_models: list[str] = []
        # Latest record's context occupancy (inputOther + inputCacheRead +
        # inputCacheCreation) — NOT cumulative.
        self._context_tokens: int | None = None
        # Billed high-water mark: the wire log identity + the last recorded
        # row's line in it. Persisted atomically WITH the totals, so replaying
        # rows after a crash (the wire cursor persists separately) is
        # idempotent. Scoped per wire: line numbers restart in a new log.
        self._billed: dict[str, int] = {}
        # Cumulative totals split per effective model (the model active when
        # each record accrued), so cost is priced per segment — see _flush.
        self._by_model: dict[str, dict[str, Any]] = {}
        self._last_posted: dict[str, int] | None = None
        self._window_cache: dict[str, int | None] = {}
        # Successful pricing lookups only; a failed lookup retries on the next
        # totals change instead of pinning a permanently-unpriced session.
        self._pricing_cache: dict[str, ModelPricing] = {}
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
        self._billed = dict(state.billed)
        self._by_model = {m: _copy_segment(seg) for m, seg in state.by_model.items()}

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
                billed=dict(self._billed),
                by_model={m: _copy_segment(seg) for m, seg in self._by_model.items()},
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

        Only the per-log view resets (context occupancy, request attribution,
        delivery dedup); the cumulative totals and model carry forward — they
        are session-scoped, and a zero-reset would make every later post a
        server-ignored decrease. The billed mark needs no reset: it is scoped
        to its wire identity, so a different log's rows never compare against
        it.
        """
        self._pending_models.clear()
        self._context_tokens = None
        self._last_posted = None

    def note_wire_restarted(self, wire: str) -> None:
        """The SAME wire path was truncated/recreated: drop its billed mark.

        Line numbers restart in the recreated log, so a stale mark scoped to
        this path would silently reject every new row up to the old
        watermark — a permanent undercount. The cumulative totals still
        carry forward (session-scoped), and marks for other wires are
        untouched. Resets the per-log view like :meth:`note_new_wire`.
        """
        if wire in self._billed:
            del self._billed[wire]
            self._persist()
        self.note_new_wire()

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
        paired_model = self._pending_models.pop(0) if self._pending_models else None
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
        if item.line_no <= self._billed.get(wire, -1):
            _warn_once(
                f"usage-replay-skip:{wire}",
                "kimi forwarder: skipping already-billed usage.record row(s) replayed from %s",
                wire,
            )
            return
        usage = item.usage or {}
        # Attribute to the model active when the tokens accrued: the effective
        # id from the preceding llm.request when one was seen, else the
        # record's own alias (the parser requires it, so the final fallback
        # is unreachable in practice — kept so a state key is never empty).
        segment_model = paired_model or self._model or item.model or "unknown"
        segment = self._by_model.setdefault(segment_model, _new_segment())
        for key in _TOTAL_KEYS:
            self._totals[key] += usage.get(key, 0)
            segment[key] += usage.get(key, 0)
        self._context_tokens = (
            usage.get("input_other", 0)
            + usage.get("cache_read", 0)
            + usage.get("cache_creation", 0)
        )
        if self._model is None and item.model:
            self._model = item.model
        self._billed[wire] = item.line_no
        self._persist()

    def note_model(self, model: str | None) -> None:
        """Adopt the effective model from an ``llm.request`` row."""
        if not model:
            return
        self._pending_models.append(model)
        if model == self._model:
            return
        self._model = model
        self._persist()

    def note_turn_boundary(self) -> None:
        """Discard requests that ended without a turn-scoped usage row."""
        self._pending_models.clear()

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
        # server needs it for per-model attribution and token-price fallback.
        body: dict[str, object] = dict(payload)
        if self._model:
            body["model"] = self._model
        cost = await self._cumulative_cost()
        if cost is not None:
            # Client-priced sum over model segments, each priced at the model
            # active when its tokens accrued. Posted as the exact cumulative
            # cost (server SETs it, monotonic clamp) so a mid-session model
            # switch never reprices the WHOLE total at the current model —
            # that under-charged expensive→cheap switches to $0 turns (the
            # clamp ate the decrease) and overcharged cheap→expensive ones.
            body["cumulative_cost_usd"] = cost
        if await self._post(client, "external_session_usage", body):
            self._last_posted = payload

    async def _cumulative_cost(self) -> float | None:
        """Accrue and sum the per-segment running costs.

        Each segment persists its accrued ``cost_usd`` plus the token counts
        already folded into it (``priced``); only the DELTA since that
        snapshot is priced, at the CURRENT rate, and added — so the
        cumulative cost is a true running sum: monotone across restarts AND
        rate changes (a rate decrease affects only future tokens instead of
        repricing all history below the server's clamp and freezing the
        badge; an increase never retroactively overcharges billed tokens).

        ``None`` when any segment has an unpriced delta whose model has no
        resolvable pricing — the server then falls back to pricing the token
        totals at the current model (correct whenever the session only ever
        ran unpriceable models). Segments with no outstanding delta still
        contribute their already-accrued cost. Failed lookups are not
        cached, so pricing that becomes available later is picked up on the
        next totals change.
        """
        from omnigent.llms.context_window import compute_llm_cost, fetch_model_pricing

        total = 0.0
        unpriced_delta = False
        accrued_any = False
        for model, segment in self._by_model.items():
            priced = segment["priced"]
            delta = {key: int(segment[key]) - int(priced[key]) for key in _TOTAL_KEYS}
            if any(delta.values()):
                pricing = self._pricing_cache.get(model)
                if pricing is None:
                    try:
                        pricing = await asyncio.to_thread(fetch_model_pricing, model)
                    except Exception:
                        _logger.exception("kimi forwarder: pricing lookup failed for %s", model)
                        pricing = None
                    else:
                        if pricing is not None:
                            self._pricing_cache[model] = pricing
                if pricing is None:
                    # Keep the delta outstanding; other segments still accrue.
                    unpriced_delta = True
                else:
                    segment["cost_usd"] = float(segment["cost_usd"]) + compute_llm_cost(
                        {
                            "input_tokens": delta["input_other"],
                            "output_tokens": delta["output"],
                            "cache_read_input_tokens": delta["cache_read"],
                            "cache_creation_input_tokens": delta["cache_creation"],
                        },
                        pricing,
                    )
                    segment["priced"] = {key: int(segment[key]) for key in _TOTAL_KEYS}
                    accrued_any = True
            total += float(segment["cost_usd"])
        if accrued_any:
            # Accrual is billing state: persist it with the totals so a
            # restart never reprices already-accrued tokens at a new rate.
            self._persist()
        if unpriced_delta or not self._by_model:
            return None
        return total

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
                return None
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
    auth: httpx.Auth | None = None,
    pane_alive: Callable[[], bool] | None = None,
    quiescence_s: float = _TURN_QUIESCENCE_S,
    tool_quiescence_s: float = _TOOL_QUIESCENCE_S,
    poll_interval_s: float = _POLL_INTERVAL_S,
    post_backoff_initial_s: float = _BACKOFF_INITIAL_S,
) -> None:
    """Poll the kimi session wire log and mirror new turns into the chat.

    Runs until cancelled. Discovers the wire log lazily (kimi writes it after the
    first turn), then tails it incrementally past a persisted byte offset,
    POSTing each new user/assistant turn and advancing the cursor per post.

    Besides mirroring, this owns the session's turn-status edges: the top-level
    ``turn.ended`` wire record posts idle (completed/cancelled) or failed; a
    dead kimi pane mid-turn posts failed; a turn whose wire has been quiet
    for *quiescence_s* with no tool call in flight is closed as idle; and a
    turn silent past *tool_quiescence_s* WITH a tool in flight is failed (the
    tool hung), so a crashed or wedged kimi never strands the session
    'running'. The turn lifecycle is persisted alongside the tail cursor, so
    the fallbacks stay armed across a forwarder restart mid-turn.

    :param auth: Optional refresh-capable httpx Auth so long sessions survive
        bearer-token expiry (mirrors the qwen forwarder).
    :param pane_alive: Optional probe for the kimi tmux pane's liveness; used
        only to fail a turn whose pane died. ``None`` disables the edge.
    :param post_backoff_initial_s: Initial backoff after a failed POST, doubling
        up to a cap so an auth/rate-limit outage never becomes a request storm.
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
    offset = state.offset if state is not None else 0
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
    # Final assistant text of the turn in flight, forwarded on the terminal
    # edge so the parent's inbox gets the real result instead of an empty one.
    # Restored from persisted state so a restart between the assistant message
    # and the turn edge doesn't post an empty output to the parent.
    last_assistant_text = state.last_assistant_text if state is not None else ""
    # True from an observed user prompt until a terminal edge posts; gates the
    # pane-death and quiescence fallbacks to turns that actually started.
    turn_open = state.turn_open if state is not None else False
    # Open tool calls in the current turn; a positive count means a silent wire
    # is a tool still running, not an orphaned turn.
    tools_in_flight = state.tools_in_flight if state is not None else 0
    # Id of the last terminal edge POSTed, persisted so a restart or wire
    # replay never double-fires the same edge at the server.
    last_edge_id = state.last_edge_id if state is not None else None
    # Terminal status of an edge the server permanently rejected (poison-drop):
    # the fallback close posts THIS status so a failed turn isn't closed idle.
    dropped_edge_status = state.dropped_edge_status if state is not None else None
    dropped_edge_output = state.dropped_edge_output if state is not None else ""
    # Per-turn discriminator for fallback edge ids (see _ForwardState).
    turn_seq = state.turn_seq if state is not None else 0
    last_prompt_id = state.last_prompt_id if state is not None else None
    wire_dev = state.wire_dev if state is not None else None
    wire_ino = state.wire_ino if state is not None else None
    last_observed_size = (
        state.last_observed_size
        if state is not None and state.last_observed_size is not None
        else state.last_seen_offset
        if state is not None and state.last_seen_offset is not None
        else state.offset
        if state is not None
        else None
    )
    last_wire_activity = time.monotonic()
    last_wire_activity_wall = time.time()
    if state is not None and state.last_activity_ts is not None:
        # Resume the silence timers where the previous forwarder left them so
        # a crash-looping forwarder can't re-arm the full windows forever —
        # but floor the remaining window at a short grace so a wall-clock jump
        # (sleep/wake, NTP step) can't fire a watchdog on the spot.
        elapsed = max(0.0, time.time() - state.last_activity_ts)
        window = tool_quiescence_s if tools_in_flight > 0 else quiescence_s
        elapsed = min(elapsed, max(0.0, window - _RESTART_GRACE_S))
        last_wire_activity = time.monotonic() - elapsed
        last_wire_activity_wall = time.time() - elapsed
    # High-water of wire bytes actually observed: a redelivery retry (in this
    # run or a crash-loop replay) must not refresh the silence timers.
    last_seen_offset = (
        state.last_seen_offset
        if state is not None and state.last_seen_offset is not None
        else offset
    )
    # Consecutive permanent-4xx rejections of the item at the head of the tail.
    poison_id: str | None = None
    poison_attempts = 0
    post_backoff = post_backoff_initial_s
    # Start of the current unbroken edge-delivery failure streak, and the last
    # time it was alerted on (both monotonic).
    edge_failing_since: float | None = None
    last_edge_alert = 0.0
    async with httpx.AsyncClient(timeout=15.0, auth=auth) as client:

        def _persist() -> None:
            _write_state(
                bridge_dir,
                _ForwardState(
                    wire_path=str(wire_path),
                    last_line=last_line,
                    offset=offset,
                    turn_open=turn_open,
                    tools_in_flight=tools_in_flight,
                    last_edge_id=last_edge_id,
                    dropped_edge_status=dropped_edge_status,
                    dropped_edge_output=dropped_edge_output,
                    last_assistant_text=last_assistant_text,
                    last_activity_ts=last_wire_activity_wall,
                    last_seen_offset=last_seen_offset,
                    wire_dev=wire_dev,
                    wire_ino=wire_ino,
                    last_observed_size=last_observed_size,
                    last_prompt_id=last_prompt_id,
                    turn_seq=turn_seq,
                ),
            )

        def _advance_past(item: KimiWireItem) -> None:
            """Move the delivery cursor past *item* and persist."""
            nonlocal offset, last_line
            offset = item.offset_after
            last_line = item.line_no + 1
            _persist()

        async def _backoff_after_post_failure() -> None:
            nonlocal post_backoff
            await asyncio.sleep(post_backoff)
            post_backoff = min(post_backoff * 2, _BACKOFF_MAX_S)

        async def _close_turn(status: str, edge: str) -> None:
            """Post the terminal edge for a turn that will write no more wire rows."""
            nonlocal turn_open, last_assistant_text, tools_in_flight, last_edge_id, post_backoff
            nonlocal dropped_edge_status, dropped_edge_output, edge_failing_since, last_edge_alert
            # A wire edge the server permanently rejected already told us how
            # this turn ended; the fallback must not soften a failure to idle.
            status = dropped_edge_status or status
            # Turn+line anchored id: a restart retrying this fallback dedupes
            # against the already-posted edge (turn_seq and last_line persist
            # together), while a later turn gets a fresh id EVEN when failed
            # POSTs have stalled the cursor at the same line — a line-only id
            # collided there, took the dedupe branch without posting, and
            # stranded the session with both watchdogs disarmed.
            edge_id = f"kimi:{edge}:{turn_seq}:{last_line}"
            if edge_id == last_edge_id:
                turn_open = False
                usage_sync.note_turn_boundary()
                _persist()
                return
            try:
                await _post_external_session_status(
                    client,
                    base_url=base_url,
                    headers=headers,
                    session_id=session_id,
                    status=status,
                    output=last_assistant_text or dropped_edge_output,
                )
            except httpx.HTTPError as exc:
                # An undelivered edge is a delivery outage whatever the cause;
                # record it so the idle-turn watchdog can name it.
                record_native_post_failure("external_session_status", exc)
                _logger.warning("kimi forwarder: %s edge failed (will retry): %s", edge, exc)
                now = time.monotonic()
                if edge_failing_since is None:
                    edge_failing_since = now
                if (
                    now - edge_failing_since >= _EDGE_FAILURE_ALERT_S
                    and now - last_edge_alert >= _EDGE_FAILURE_ALERT_S
                ):
                    _logger.error(
                        "kimi forwarder: %s edge undelivered for %.0fs (still retrying): %s",
                        edge,
                        now - edge_failing_since,
                        exc,
                    )
                    last_edge_alert = now
                await _backoff_after_post_failure()
            else:
                note_native_post_success()
                post_backoff = post_backoff_initial_s
                turn_open = False
                tools_in_flight = 0
                last_assistant_text = ""
                last_edge_id = edge_id
                dropped_edge_status = None
                dropped_edge_output = ""
                edge_failing_since = None
                usage_sync.note_turn_boundary()
                _persist()
                # The turn is over and no further wire rows are coming (pane
                # death / quiescence): deliver the final usage totals promptly,
                # matching the wire-edge branch.
                await usage_sync.sync(client)

        while True:
            if wire_path is None or not wire_path.exists():
                discovered = await asyncio.to_thread(
                    _discover_wire, kimi_home, workspace, launch_epoch_ms
                )
                if discovered is not None and discovered != wire_path:
                    wire_path = discovered
                    last_line = 0
                    offset = 0
                    last_seen_offset = 0
                    last_observed_size = None
                    last_prompt_id = None
                    try:
                        discovered_stat = discovered.stat()
                    except OSError:
                        wire_dev = wire_ino = None
                    else:
                        wire_dev = discovered_stat.st_dev
                        wire_ino = discovered_stat.st_ino
                    # A new wire restarts line numbering, so a stale edge id
                    # could silently dedupe the new wire's edge at the same line.
                    last_edge_id = None
                    last_assistant_text = ""
                    tools_in_flight = 0
                    dropped_edge_status = None
                    dropped_edge_output = ""
                    poison_id = None
                    poison_attempts = 0
                    # Cursor resets; cumulative usage totals carry forward.
                    usage_sync.note_new_wire()
                    _persist()
            all_posted = True
            pane_is_alive = pane_alive() if pane_alive is not None else None
            dead_tail_pending = False
            if wire_path is not None and wire_path.exists():
                try:
                    wire_stat = wire_path.stat()
                except OSError:
                    wire_stat = None
                if wire_stat is not None:
                    generation = (wire_stat.st_dev, wire_stat.st_ino)
                    if wire_dev is None or wire_ino is None:
                        wire_dev, wire_ino = generation
                    elif generation != (wire_dev, wire_ino):
                        # The path now names a different file. It may already be
                        # larger than the old cursor, so size checks cannot find
                        # this generation change after the fact.
                        offset = 0
                        last_line = 0
                        last_seen_offset = 0
                        last_observed_size = None
                        last_edge_id = None
                        last_prompt_id = None
                        last_assistant_text = ""
                        tools_in_flight = 0
                        dropped_edge_status = None
                        dropped_edge_output = ""
                        poison_id = None
                        poison_attempts = 0
                        wire_dev, wire_ino = generation
                        usage_sync.note_wire_restarted(str(wire_path))
                        last_wire_activity = time.monotonic()
                        last_wire_activity_wall = time.time()
                        _persist()
                    observed_size = wire_stat.st_size
                else:
                    observed_size = None
                stable_dead_tail = (
                    pane_is_alive is False
                    and observed_size is not None
                    and observed_size == last_observed_size
                )
                items, new_offset, new_line = await asyncio.to_thread(
                    read_new_kimi_wire_items,
                    wire_path,
                    offset,
                    last_line,
                    include_unterminated=stable_dead_tail,
                )
                dead_tail_pending = (
                    pane_is_alive is False
                    and not stable_dead_tail
                    and observed_size is not None
                    and new_offset < observed_size
                )
                observed_size_changed = (
                    observed_size is not None and observed_size != last_observed_size
                )
                if observed_size is not None:
                    last_observed_size = observed_size
                if new_offset > last_seen_offset or new_offset < offset:
                    # Genuinely new rows (or a truncated/recreated wire) refresh
                    # the silence timers; a redelivery retry re-reading the same
                    # unposted tail must NOT defer the quiescence fallbacks.
                    # Persisted immediately (cursor untouched) so a crash loop
                    # can't refresh the timers with the same rows every restart.
                    if new_offset < offset:
                        # The reader restarted a truncated/recreated wire: the
                        # new log restarts line numbering, so generation-scoped
                        # dedupe state must reset exactly like the discovery
                        # branch — a stale edge id would swallow the new log's
                        # edge at the same line, and the stale billed mark
                        # would reject its usage rows up to the old watermark.
                        last_edge_id = None
                        last_prompt_id = None
                        last_assistant_text = ""
                        tools_in_flight = 0
                        dropped_edge_status = None
                        dropped_edge_output = ""
                        poison_id = None
                        poison_attempts = 0
                        usage_sync.note_wire_restarted(str(wire_path))
                    last_seen_offset = new_offset
                    last_wire_activity = time.monotonic()
                    last_wire_activity_wall = time.time()
                    _persist()
                elif new_offset < last_seen_offset:
                    # The SAME wire path was recreated/compacted below the
                    # observed high-water (delivery cursor still under the new
                    # size, so the read didn't restart): the stale high-water
                    # would blind the refresh gate and quiescence could falsely
                    # close a live turn. Restart the tail like the truncation
                    # branch does and re-read next poll.
                    offset = 0
                    last_line = 0
                    last_seen_offset = 0
                    # Same-path recreation: reset the generation-scoped dedupe
                    # state (stale edge id / billed mark) like the discovery
                    # and reader-restart paths do.
                    last_edge_id = None
                    last_prompt_id = None
                    last_assistant_text = ""
                    tools_in_flight = 0
                    dropped_edge_status = None
                    dropped_edge_output = ""
                    poison_id = None
                    poison_attempts = 0
                    usage_sync.note_wire_restarted(str(wire_path))
                    last_wire_activity = time.monotonic()
                    last_wire_activity_wall = time.time()
                    _persist()
                    await asyncio.sleep(poll_interval_s)
                    continue
                elif observed_size_changed:
                    # A partial append is real activity even before its newline
                    # makes the delivery cursor advance. Persisting the size is
                    # also the stability proof used after writer death.
                    last_wire_activity = time.monotonic()
                    last_wire_activity_wall = time.time()
                    _persist()
                for item in items:
                    if item.kind in ("tool_call", "tool_result"):
                        tools_in_flight = (
                            tools_in_flight + 1
                            if item.kind == "tool_call"
                            else max(0, tools_in_flight - 1)
                        )
                        _advance_past(item)
                        continue
                    if item.kind == "message" and item.role == "user":
                        historical_prompt = (
                            item.time_ms is not None and item.time_ms < launch_epoch_ms
                        )
                        new_prompt = item.response_id != last_prompt_id
                        if (
                            not historical_prompt
                            and new_prompt
                            and dropped_edge_status is not None
                        ):
                            # Session status is single-valued: a new prompt
                            # legitimately supersedes the prior turn's pending
                            # status, but a swallowed FAILURE deserves a trace.
                            _logger.error(
                                "kimi forwarder: undeliverable %s edge superseded by a "
                                "new prompt; the parent never saw it",
                                dropped_edge_status,
                            )
                        if not historical_prompt and new_prompt:
                            turn_open = True
                            tools_in_flight = 0
                            dropped_edge_status = None
                            dropped_edge_output = ""
                            turn_seq += 1
                            last_prompt_id = item.response_id
                            # Lifecycle state advances even if the conversation
                            # POST stalls, so retries keep the same turn id.
                            _persist()
                    if item.kind in ("turn_end", "turn_failed") and (
                        (item.time_ms is None and launch_epoch_ms > 0)
                        or (item.time_ms is not None and item.time_ms < launch_epoch_ms)
                    ):
                        # A resumed wire's HISTORICAL edge (stamped before this
                        # Omnigent session launched): replaying it would post a
                        # dead turn's terminal status — with its stale error —
                        # over the live turn. Skip it entirely; the same
                        # launch-epoch floor already gates usage billing.
                        usage_sync.note_turn_boundary()
                        _advance_past(item)
                        continue
                    if item.kind in ("turn_end", "turn_failed") and (
                        item.response_id == last_edge_id
                    ):
                        # Edge already posted; a restart replayed the wire line
                        # before the cursor persisted. Just close out locally.
                        # (An ambiguous in-flight retry may still double-post,
                        # which is safe: the server treats a duplicate
                        # same-status terminal report as already delivered —
                        # no second parent wake, no status flip. Across a
                        # RUNNER restart the reconstructed work entry starts
                        # undelivered, so a replay could deliver again — but
                        # only the identical terminal status, and only when
                        # the crash landed between the POST and this persist;
                        # if the restart also lost the parent inbox, that
                        # re-delivery repairs it rather than duplicating.)
                        turn_open = False
                        usage_sync.note_turn_boundary()
                        _advance_past(item)
                        continue
                    try:
                        if item.kind == "usage":
                            # Accumulate only; delivery happens in the per-poll
                            # sync below. Never gates the cursor: transcript
                            # liveness wins over usage durability (recorded
                            # design ruling — see the class docstring).
                            usage_sync.record(item, wire=str(wire_path))
                        elif item.kind == "model":
                            usage_sync.note_model(item.model)
                        elif item.kind in ("turn_end", "turn_failed"):
                            await _post_external_session_status(
                                client,
                                base_url=base_url,
                                headers=headers,
                                session_id=session_id,
                                status="idle" if item.kind == "turn_end" else "failed",
                                # A failed turn.ended carries the provider error
                                # message; surface it when no assistant text ran.
                                output=last_assistant_text or item.text,
                            )
                            last_assistant_text = ""
                            turn_open = False
                            tools_in_flight = 0
                            last_edge_id = item.response_id
                            dropped_edge_status = None
                            dropped_edge_output = ""
                            usage_sync.note_turn_boundary()
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
                        event_type = (
                            "external_session_status"
                            if item.kind in ("turn_end", "turn_failed")
                            else "external_conversation_item"
                        )
                        status_code = (
                            exc.response.status_code
                            if isinstance(exc, httpx.HTTPStatusError)
                            else None
                        )
                        if status_code is None or status_code in _TRANSIENT_POST_STATUSES:
                            # Transport failures AND endlessly-retried transient
                            # rejections (e.g. a revoked token's 401s) are a
                            # delivery outage the idle-turn watchdog must see.
                            record_native_post_failure(event_type, exc)
                        else:
                            # A permanent status is a payload problem, resolved
                            # shortly by the poison drop — the server is fine.
                            note_native_post_success()
                        if (
                            status_code is not None
                            and 400 <= status_code < 500
                            and status_code not in _TRANSIENT_POST_STATUSES
                        ):
                            # Only a malformed payload poisons; auth/rate-limit
                            # rejections retry with backoff and are never dropped.
                            if poison_id == item.response_id:
                                poison_attempts += 1
                            else:
                                poison_id, poison_attempts = item.response_id, 1
                            if poison_attempts >= _POISON_MAX_ATTEMPTS:
                                _logger.error(
                                    "kimi forwarder: dropping item %s rejected %d times: %s",
                                    item.response_id,
                                    poison_attempts,
                                    exc,
                                )
                                if item.kind in ("turn_end", "turn_failed"):
                                    # The turn's true terminal status is known;
                                    # hand it to the fallback close instead of
                                    # letting quiescence report idle.
                                    dropped_edge_status = (
                                        "idle" if item.kind == "turn_end" else "failed"
                                    )
                                    dropped_edge_output = item.text
                                    tools_in_flight = 0
                                    usage_sync.note_turn_boundary()
                                poison_id, poison_attempts = None, 0
                                _advance_past(item)
                                continue
                        _logger.warning("kimi forwarder: POST failed (will retry): %s", exc)
                        all_posted = False
                        break
                    else:
                        note_native_post_success()
                        post_backoff = post_backoff_initial_s
                        if poison_id == item.response_id:
                            poison_id, poison_attempts = None, 0
                        _advance_past(item)
                if all_posted and new_offset != offset:
                    # Consume trailing non-item lines so the next poll skips them.
                    offset = new_offset
                    last_line = new_line
                    _persist()
            # Deliver pending usage/model every poll (no-op when nothing
            # changed): retries any previously failed post even when no
            # further wire records arrive — a turn-final failure, or a wire
            # that has since vanished (the totals are already persisted, so a
            # missing log must not orphan an undelivered post forever).
            await usage_sync.sync(client)
            if turn_open and pane_is_alive is False and not dead_tail_pending:
                # The pane died mid-turn: no further wire rows are coming, so
                # post the failed edge instead of stranding the session. Give
                # a final unterminated row its stable-size acceptance poll first.
                await _close_turn("failed", "pane-death")
            elif (
                turn_open
                and tools_in_flight == 0
                and time.monotonic() - last_wire_activity > quiescence_s
            ):
                # Turns that end without any wire edge (wedged kimi, lost edge
                # row): a long-quiet wire means the turn is over; close it as
                # idle. A silent wire mid-tool is a long tool call, not an
                # orphan — then only pane death (above) closes the turn.
                await _close_turn("idle", "quiescence")
            elif (
                turn_open
                and tools_in_flight > 0
                and time.monotonic() - last_wire_activity > tool_quiescence_s
            ):
                # Ceiling on the mid-tool suppression: no wire row of any kind
                # for this long means the tool (or kimi) hung in an alive pane —
                # fail the turn rather than suppress quiescence forever.
                _logger.error(
                    "kimi forwarder: %d tool call(s) in flight but wire silent > %.0fs; "
                    "failing the turn as hung",
                    tools_in_flight,
                    tool_quiescence_s,
                )
                await _close_turn("failed", "tool-quiescence")
            if not all_posted:
                await _backoff_after_post_failure()
            await asyncio.sleep(poll_interval_s)


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
    auth: httpx.Auth | None = None,
    pane_alive: Callable[[], bool] | None = None,
) -> None:
    """Run :func:`forward_kimi_wire_to_session` with restart-on-crash backoff.

    Propagates :class:`asyncio.CancelledError` cleanly (terminal teardown), but
    restarts on any other exception with exponential backoff — mirrors
    ``cursor_native_forwarder.supervise_cursor_forwarder``. ``auth`` /
    ``pane_alive`` pass through to the forward loop.
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
                auth=auth,
                pane_alive=pane_alive,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.exception("kimi forwarder crashed for session %s; restarting", session_id)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)
        else:
            return
