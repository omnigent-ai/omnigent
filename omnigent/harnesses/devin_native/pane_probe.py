"""Pane turn probe for devin-native: the tail of its hook event log."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from omnigent.runner.native.pane_probe_types import NativeProbeContext, TurnProbe, TurnState

# Enough of ``hooks.jsonl`` to hold the last prompt/stop pair of any turn.
_TAIL_BYTES = 256 * 1024
_PROMPT = "UserPromptSubmit"
_TURN_END = frozenset({"Stop", "SessionEnd"})


async def probe_pane_turn(ctx: NativeProbeContext) -> TurnProbe | None:
    """Infer whether Devin's last prompt is still open from its hook log.

    ACTIVE when the last ``UserPromptSubmit`` Devin actually ran (not one the
    policy blocked) has no later ``Stop``. Inferred: a missed ``Stop`` would
    read ACTIVE, so the caller discounts it when the prompt predates a newer
    accepted interrupt (the answer carries the prompt's record time).

    :param ctx: Probe context for the session.
    :returns: The probe answer.
    """
    from omnigent.harnesses.devin_native.bridge import (
        DEVIN_POLICY_BLOCKED_KEY,
        bridge_dir_for_session_id,
        hooks_path,
    )

    path = hooks_path(bridge_dir_for_session_id(ctx.session_id))
    events = await asyncio.to_thread(_tail_hook_events, path)
    if events is None:
        return TurnProbe(TurnState.UNKNOWN, "inferred", detail="no hook log")
    for recorded_at, payload in reversed(events):
        name = payload.get("hook_event_name")
        if name in _TURN_END:
            return TurnProbe(TurnState.INACTIVE, "inferred", detail=f"last edge {name}")
        if name == _PROMPT and not payload.get(DEVIN_POLICY_BLOCKED_KEY):
            return TurnProbe(
                TurnState.ACTIVE,
                "inferred",
                detail="prompt without Stop",
                started_wall=recorded_at,
            )
    return TurnProbe(TurnState.INACTIVE, "inferred", detail="no open prompt")


def _tail_hook_events(path: Path) -> list[tuple[float | None, dict[str, object]]] | None:
    """Parse the complete trailing lines of ``hooks.jsonl``, oldest first.

    :returns: ``(recorded_at, payload)`` pairs; ``recorded_at`` is the wall
        time the hook wrote the line, or ``None`` if the line has none.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            start = max(0, size - _TAIL_BYTES)
            handle.seek(start)
            raw = handle.read()
    except OSError:
        return None
    lines = raw.split(b"\n")
    if start > 0:
        lines = lines[1:]  # the first line may be cut mid-record
    events: list[tuple[float | None, dict[str, object]]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            envelope = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(envelope, dict):
            continue
        payload = envelope.get("payload")
        recorded_at = envelope.get("recorded_at")
        if isinstance(payload, dict):
            stamp = float(recorded_at) if isinstance(recorded_at, (int, float)) else None
            events.append((stamp, payload))
    return events
