"""Turn-completion ("idle") poster state for the hermes-native harness.

The completion->parent-wake path needs a harness to POST an
``external_session_status: idle`` event to the Sessions API: the server maps
that edge to a sub-agent turn-terminal and wakes the parent orchestrator's
inbox (the SAME contract claude-/codex-/opencode-/cursor-native use). Hermes'
PTY-activity watcher only emits a ``session.status: idle`` SSE edge that drives
the web "Working…" spinner and never wakes a parent — so without an explicit
post a hermes-native sub-agent finishes silently and the orchestrator hangs.

Unlike cursor-agent, hermes-agent exposes NO per-turn ``stop`` hook (only a
``pre_tool_call`` hook, used for policy enforcement), so there is no separate
process writing a turn-end marker. Instead the runner-owned
:func:`omnigent.hermes_native_forwarder.forward_hermes_store_to_session` poll
loop *derives* turn completion from Hermes' ``state.db`` itself — an
``assistant`` row with no ``tool_calls`` is the agentic loop's terminal step,
i.e. one completed turn (see ``_count_completed_turns`` in that module). The
``messages`` table is the append-only "marker store"; this module owns only the
*poster* state: how many of those completed turns have already been turned into
an ``external_session_status: idle`` post.

It is the hermes analog of the poster-state half of
:mod:`omnigent.cursor_native_status`. Persisting the posted-count means a
supervisor restart never re-wakes the parent for a turn it already reported.
Stdlib-only (no httpx) so it stays a cheap, dependency-free state file.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

#: Durable poster state: how many completed turns the forwarder has already
#: turned into an ``external_session_status: idle`` post. Persisted so a
#: supervisor restart never re-wakes the parent for a turn it already reported.
_STATE_FILE = "hermes_status_forwarder.json"


def read_posted_count(bridge_dir: Path) -> int:
    """Load the count of completed turns already POSTed as idle (0 on cold/unreadable)."""
    try:
        data = json.loads((bridge_dir / _STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    posted = data.get("posted") if isinstance(data, dict) else None
    return posted if isinstance(posted, int) and posted >= 0 else 0


def write_posted_count(bridge_dir: Path, posted: int) -> None:
    """Atomically persist the count of completed turns already POSTed as idle.

    Persisted only AFTER a successful idle POST so a failed flush is retried (the
    unreported turns stay unreported until the post lands).
    """
    bridge_dir.mkdir(parents=True, exist_ok=True)
    tmp = bridge_dir / (_STATE_FILE + ".tmp")
    tmp.write_text(json.dumps({"posted": posted}), encoding="utf-8")
    os.replace(tmp, bridge_dir / _STATE_FILE)


def clear_hermes_status_state(bridge_dir: Path) -> None:
    """Remove the idle poster state so a re-created terminal starts clean.

    Sibling of :func:`omnigent.hermes_native_forwarder.clear_hermes_bridge_state`:
    the runner calls this when it re-creates a hermes terminal so a stale
    posted-count from a prior terminal can't make the new forwarder skip (or
    re-fire) the ``external_session_status: idle`` parent-wake edge. The
    completed-turn count is derived per ``hermes_session_id`` (not per terminal),
    so after an in-session compaction re-pins to a forked child the forwarder
    rebases this posted-count to the child's current count; only the poster state
    is persisted here, so only it needs clearing.
    """
    with contextlib.suppress(OSError):
        (bridge_dir / _STATE_FILE).unlink()
