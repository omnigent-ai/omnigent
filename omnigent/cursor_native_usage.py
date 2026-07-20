"""Token-usage capture for the cursor-native harness.

cursor-agent surfaces per-turn token usage ONLY through its lifecycle hooks —
the SQLite chat store (tailed by :mod:`omnigent.cursor_native_forwarder`) and
the on-disk ``agent-transcripts`` JSONL carry none, and the headless
``result.usage`` is unavailable to the interactive TUI the harness drives. So we
register a ``hooks.json`` ``stop`` hook (see
:func:`omnigent.cursor_native_bridge.write_hooks_config`) whose command runs
``record-usage`` here. cursor invokes it once per completed turn with a JSON
payload on stdin:

    {"generation_id": "...", "model": "claude-4-sonnet",
     "input_tokens": 23666, "output_tokens": 5,
     "cache_read_tokens": 23617, "cache_write_tokens": 47, ...}

``record-usage`` appends one normalized line per turn to
``<bridge_dir>/cursor_usage.jsonl``. The runner-owned poller
(:func:`forward_cursor_usage_to_session`) tails that file, accumulates the
per-turn counts into cumulative SESSION totals, and POSTs them as an
``external_session_usage`` event — the SAME server contract claude-native and
codex-native use, so the web UI's Session-cost badge and per-model token
breakdown light up with no frontend changes.

The recorder path imports only the stdlib so the hook stays fast (cursor blocks
the turn end on the hook); ``httpx`` is imported lazily inside the poller.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

_logger = logging.getLogger(__name__)

#: Append-only log of per-turn usage written by the ``stop`` hook recorder and
#: tailed by the poller. One JSON object per line (per completed turn).
USAGE_FILE = "cursor_usage.jsonl"

#: Durable poller state (cumulative totals + processed generation ids), so a
#: supervisor restart resumes without double-counting already-posted turns.
_USAGE_STATE_FILE = "cursor_usage_forwarder.json"

#: Per-turn usage fields we lift out of cursor's ``stop`` payload. cursor's
#: ``input_tokens`` is INCLUSIVE of cache-read + cache-write (the TUI subtracts
#: them for its own display); we forward it inclusive and let the server split
#: the cache-read portion out via ``cumulative_cache_read_input_tokens``.
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

_DEFAULT_POLL_INTERVAL_S = 0.7
_POST_TIMEOUT_S = 30.0

# Supervisor backoff (mirrors cursor_native_forwarder.supervise_cursor_forwarder).
_SUPERVISOR_INITIAL_BACKOFF_S = 1.0
_SUPERVISOR_MAX_BACKOFF_S = 30.0
_SUPERVISOR_HEALTHY_UPTIME_S = 60.0


# --------------------------------------------------------------------------- #
# Hook-side recorder (stdlib only — keep fast; cursor blocks the turn on it).
# --------------------------------------------------------------------------- #


def _coerce_int(value: object) -> int:
    """Coerce a hook token field to a non-negative int (0 on anything odd)."""
    try:
        out = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return out if out >= 0 else 0


def normalize_hook_payload(payload: object) -> dict[str, object] | None:
    """Reduce a cursor ``stop`` hook payload to the usage line we persist.

    :returns: ``{"generation_id", "model", <token fields>}`` when the payload
        carries a generation id and at least one positive token count, else
        ``None`` (skip — nothing to bill for this turn).
    """
    if not isinstance(payload, dict):
        return None
    gen_id = payload.get("generation_id") or payload.get("conversation_id")
    if not isinstance(gen_id, str) or not gen_id:
        return None
    tokens = {field_name: _coerce_int(payload.get(field_name)) for field_name in _TOKEN_FIELDS}
    if not any(tokens.values()):
        return None
    model = payload.get("model")
    line: dict[str, object] = {"generation_id": gen_id}
    if isinstance(model, str) and model:
        line["model"] = model
    line.update(tokens)
    return line


def record_usage_payload(bridge_dir: Path, payload: object) -> bool:
    """Append a normalized usage line for one turn to ``cursor_usage.jsonl``.

    :returns: ``True`` if a line was appended, ``False`` if the payload had no
        billable usage (skipped).
    """
    line = normalize_hook_payload(payload)
    if line is None:
        return False
    bridge_dir.mkdir(parents=True, exist_ok=True)
    # O_APPEND keeps concurrent writers (a fast-firing hook) from interleaving
    # within a single write() of one short JSON line.
    with open(bridge_dir / USAGE_FILE, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True) + "\n")
    return True


def _cli_record_usage(bridge_dir: Path) -> int:
    """Hook entrypoint: read the JSON payload from stdin and append it.

    The cursor ``stop`` hook fires once per completed turn, so this is also the
    authoritative "turn finished" signal: it records a turn-end marker
    (:func:`omnigent.cursor_native_status.record_turn_end`) on EVERY firing — even
    a turn with no billable usage, which :func:`record_usage_payload` skips — so
    the forwarder can POST an ``external_session_status: idle`` edge and wake the
    parent orchestrator.

    Always emits ``{}`` (a no-op hook response cursor reads as "continue") and
    exits 0 — a usage/idle-capture failure must never block or fail the agent
    turn.
    """
    from omnigent import cursor_native_status

    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        # Record the turn-end marker FIRST (and unconditionally) so the parent
        # wake never depends on usage parsing succeeding or the turn being
        # billable; usage capture is a best-effort addition on top.
        cursor_native_status.record_turn_end(bridge_dir, payload)
        record_usage_payload(bridge_dir, payload)
    except Exception:  # noqa: BLE001 — never let usage/idle capture break the turn
        _logger.debug("cursor usage recorder failed", exc_info=True)
    # cursor expects JSON on stdout; an empty object is the "continue" response.
    sys.stdout.write("{}")
    return 0


# --------------------------------------------------------------------------- #
# Poller-side accumulator + forwarder (runner-owned).
# --------------------------------------------------------------------------- #


@dataclass
class _UsageAccumulator:
    """Cumulative session totals plus the generation ids already counted.

    cursor reports PER-TURN counts; session billing is their sum (each turn is
    billed for the full context it re-sent, so summing per-turn ``input_tokens``
    — cache reads included — is the correct cumulative input). Dedup by
    ``generation_id`` so a re-read of the append-only file (every poll, and
    after a restart) never counts a turn twice.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    model: str | None = None
    seen: set[str] = field(default_factory=set)

    def add_line(self, line: dict[str, object]) -> bool:
        """Fold one usage line in if unseen. Returns ``True`` when it counted."""
        gen_id = line.get("generation_id")
        if not isinstance(gen_id, str) or gen_id in self.seen:
            return False
        self.seen.add(gen_id)
        self.input_tokens += _coerce_int(line.get("input_tokens"))
        self.output_tokens += _coerce_int(line.get("output_tokens"))
        self.cache_read_tokens += _coerce_int(line.get("cache_read_tokens"))
        model = line.get("model")
        if isinstance(model, str) and model:
            self.model = model  # latest turn's model wins (mirrors a /model switch)
        return True

    def seen_count(self) -> int:
        """Distinct turns folded in so far (one per ``generation_id``).

        Drives the forwarder's turn-completion wake edge: each newly-seen turn
        posts one ``external_session_status: idle``.
        """
        return len(self.seen)


def _read_usage_state(bridge_dir: Path) -> _UsageAccumulator:
    """Load the persisted accumulator, or a cold zero default."""
    try:
        data = json.loads((bridge_dir / _USAGE_STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _UsageAccumulator()
    if not isinstance(data, dict):
        return _UsageAccumulator()
    seen = data.get("seen")
    return _UsageAccumulator(
        input_tokens=_coerce_int(data.get("input_tokens")),
        output_tokens=_coerce_int(data.get("output_tokens")),
        cache_read_tokens=_coerce_int(data.get("cache_read_tokens")),
        model=data.get("model") if isinstance(data.get("model"), str) else None,
        seen={s for s in seen if isinstance(s, str)} if isinstance(seen, list) else set(),
    )


def _write_usage_state(bridge_dir: Path, acc: _UsageAccumulator) -> None:
    """Atomically persist the accumulator (tmp write + rename)."""
    bridge_dir.mkdir(parents=True, exist_ok=True)
    tmp = bridge_dir / (_USAGE_STATE_FILE + ".tmp")
    tmp.write_text(
        json.dumps(
            {
                "input_tokens": acc.input_tokens,
                "output_tokens": acc.output_tokens,
                "cache_read_tokens": acc.cache_read_tokens,
                "model": acc.model,
                "seen": sorted(acc.seen),
            }
        ),
        encoding="utf-8",
    )
    os.replace(tmp, bridge_dir / _USAGE_STATE_FILE)


def _read_usage_lines(bridge_dir: Path) -> list[dict[str, object]]:
    """Read every usage line currently in ``cursor_usage.jsonl`` (skip junk)."""
    out: list[dict[str, object]] = []
    try:
        text = (bridge_dir / USAGE_FILE).read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _usage_post_body(acc: _UsageAccumulator) -> dict[str, object]:
    """Build the ``external_session_usage`` ``data`` payload from cumulative totals.

    ``cumulative_input_tokens`` is sent INCLUSIVE of cache reads (cursor's
    semantics); the server splits ``cumulative_cache_read_input_tokens`` back
    out and prices it at the cache-read rate. ``model`` lets the server price
    the tokens via the MLflow catalog (absent/unpriced models show tokens with
    cost "—").
    """
    data: dict[str, object] = {
        "cumulative_input_tokens": acc.input_tokens,
        "cumulative_output_tokens": acc.output_tokens,
        "cumulative_cache_read_input_tokens": acc.cache_read_tokens,
    }
    if acc.model:
        data["model"] = acc.model
    return data


async def forward_cursor_usage_to_session(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: object | None = None,
) -> None:
    """Tail ``cursor_usage.jsonl`` and POST cumulative usage to the AP session.

    Each poll re-reads the append-only usage log, folds any unseen turns into
    the cumulative accumulator (deduped by ``generation_id``), and — when the
    totals advanced — POSTs an ``external_session_usage`` event. The accumulator
    is persisted to ``bridge_dir`` so a supervisor restart resumes without
    re-counting. Never returns normally; cancel the task to stop it.

    A newly-observed usage line means cursor-agent fired its ``stop`` hook — a
    turn completed. On that edge we also POST ``external_session_status: idle``,
    the only signal that reaches the parent inbox wake (the forwarder mirrors
    only conversation items, and the PTY-activity watcher is suppressed for
    cursor-native). Idle delivery is idempotent server-side.
    """
    import httpx

    from omnigent._native_post_delivery import post_external_session_status

    acc = _read_usage_state(bridge_dir)
    timeout = httpx.Timeout(_POST_TIMEOUT_S)
    # Turns already woken this run. Seeded at 0 (not from persisted usage state):
    # a restart re-posts one idle, which the server dedupes if already delivered
    # — the safe direction. Seeding from persisted usage would permanently skip a
    # wake whose idle POST crashed after the usage flush persisted.
    idle_posted_turns = 0
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                lines = await asyncio.to_thread(_read_usage_lines, bridge_dir)
                changed = False
                for line in lines:
                    if acc.add_line(line):
                        changed = True
                if changed:
                    resp = await client.post(
                        f"/v1/sessions/{session_id}/events",
                        json={"type": "external_session_usage", "data": _usage_post_body(acc)},
                    )
                    resp.raise_for_status()
                    # Persist only after a successful POST so a failed flush is
                    # retried (the unseen turns stay unseen until they land).
                    await asyncio.to_thread(_write_usage_state, bridge_dir, acc)
                # Wake the parent once per newly-seen turn, after the usage flush
                # so the inbox read sees up-to-date cost. A failed idle is retried
                # next poll (the counter only advances on success).
                seen_turns = acc.seen_count()
                if seen_turns > idle_posted_turns:
                    await post_external_session_status(
                        client, session_id=session_id, status="idle"
                    )
                    idle_posted_turns = seen_turns
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "cursor usage forwarder poll failed; session=%s bridge_dir=%s",
                    session_id,
                    bridge_dir,
                )
            await asyncio.sleep(poll_interval_s)


async def supervise_cursor_usage_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: object | None = None,
) -> None:
    """Run :func:`forward_cursor_usage_to_session` under a restart supervisor.

    Mirrors :func:`omnigent.cursor_native_forwarder.supervise_cursor_forwarder`:
    the poll loop swallows per-poll errors, but a crash in client setup would
    otherwise stop usage updates for the session. Restart with bounded
    exponential backoff; :class:`asyncio.CancelledError` propagates for clean
    teardown. The persisted accumulator means restarts resume exactly.
    """
    backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
    while True:
        run_started_at = time.monotonic()
        crash_exc: Exception | None = None
        try:
            await forward_cursor_usage_to_session(
                base_url=base_url,
                headers=headers,
                session_id=session_id,
                bridge_dir=bridge_dir,
                poll_interval_s=poll_interval_s,
                auth=auth,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — supervisor restarts on any Exception
            crash_exc = exc
        if time.monotonic() - run_started_at >= _SUPERVISOR_HEALTHY_UPTIME_S:
            backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
        if crash_exc is not None:
            _logger.error(
                "cursor usage forwarder crashed; restarting in %.1fs; session=%s",
                backoff_s,
                session_id,
                exc_info=crash_exc,
            )
        await asyncio.sleep(backoff_s)
        backoff_s = min(backoff_s * 2.0, _SUPERVISOR_MAX_BACKOFF_S)


def clear_cursor_usage_state(bridge_dir: Path) -> None:
    """Remove persisted usage state + log so a re-created terminal starts clean."""
    for name in (_USAGE_STATE_FILE, USAGE_FILE):
        with contextlib.suppress(OSError):
            (bridge_dir / name).unlink()


def _main(argv: list[str] | None = None) -> int:
    """CLI used by the cursor ``stop`` hook: ``record-usage --bridge-dir <dir>``."""
    parser = argparse.ArgumentParser(prog="omnigent.cursor_native_usage")
    sub = parser.add_subparsers(dest="command", required=True)
    rec = sub.add_parser("record-usage", help="Append a stop-hook usage payload (stdin JSON).")
    rec.add_argument("--bridge-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "record-usage":
        return _cli_record_usage(args.bridge_dir)
    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
