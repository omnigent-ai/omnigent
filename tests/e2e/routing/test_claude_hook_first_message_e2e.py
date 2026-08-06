"""CUJ 3 — claude-native in-harness routing of the first message TYPED into the TUI.

A bare ``omnigent claude`` / bare web session starts with no prompt, so there is
nothing to route at create time. The first real user message triggers exactly
one routing call from inside the harness — a ``UserPromptSubmit`` hook — and the
routed model is applied *before* that message runs.

The prompt is **typed into the pane**, never POSTed. A posted message enters
through the server's composer path, which routes on a different gate entirely;
only a keystroke reaches the harness hook, so a POST here would score the
composer and quietly prove nothing about this CUJ.

What is scored:

* **Block-and-replay, once.** Claude's hook cannot retarget a turn in flight, so
  it blocks the prompt, the model is switched while nothing is running, and the
  runner replays the captured prompt. The block notice must appear **once** —
  twice means the replay itself was routed again.
* **The switch actually lands.** The apply layer types ``/model <id>``, so the
  pane must report the switch before the replay runs. Claude Code answers that
  form by also rewriting the ``model`` key in the developer's own
  ``~/.claude/settings.json`` — an accepted trade-off — so everything *else* in
  that file must be unchanged.
* **One decision, one turn.** A leaked replay shows up as two user messages for
  one thing typed.
* **The second prompt fast-skips.** Turn 2 must issue **zero** routing calls —
  the gate is the session's routing-decision label, with the bridge-dir marker
  as a local fast skip.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from omnigent.runner.turn_routing import MARKER_FILE
from tests.e2e.routing._helpers import (
    capture_pane,
    claude_bridge_dir,
    claude_settings_apart_from_the_model,
    decisions,
    same_arm,
    settle,
    tmux_target,
    user_messages,
    wait_for,
    wait_for_bridge_file,
    wait_for_pane_text,
)
from tests.e2e.routing._mock_router import MockRouter, decide
from tests.e2e.routing.conftest import (
    bypass_args,
    create_routed_session,
    require_tmux,
    trusted_workspace,
    unique_trivial_prompt,
)

# The suite's default per-test cap is 300s; this CUJ's own waits (launch, a real
# turn, the spawn or the replay) can legitimately exceed it end to end. The budget
# sits above their sum so an internal wait names what never happened instead of
# the harness firing first on a sleep.
pytestmark = [pytest.mark.smart_routing, pytest.mark.timeout(900)]

#: The hook's own block notice, printed into the pane before the replay.
_BLOCK_NOTICE = "Smart Routing selected"

#: What Claude Code prints once ``/model <id>`` has been applied.
_MODEL_APPLIED_HINT = "Set model to"

#: Text claude's TUI shows once its input box is mounted. Typing before this is
#: the dropped-first-message race: claude flushes pending input on boot.
_PANE_READY_HINTS = ("for shortcuts", "Welcome to Claude", "❯")

_LAUNCH_TIMEOUT_S = 180.0
_ROUTE_TIMEOUT_S = 180.0

#: Quiet window used for the negative checks ("no second decision", "no second
#: routing call"). Comfortably past the hook's whole timeout ladder, whose
#: outermost hop is 45s.
_QUIET_WINDOW_S = 60.0


def _wait_for_pane_ready(socket_path: str, target: str) -> None:
    """Wait until claude's input box is mounted.

    :param socket_path: The tmux server socket.
    :param target: The pane target.
    """
    wait_for(
        lambda: (
            True
            if any(hint in capture_pane(socket_path, target) for hint in _PANE_READY_HINTS)
            else None
        ),
        timeout=_LAUNCH_TIMEOUT_S,
        what="claude's input box to mount",
    )


def test_claude_routes_the_first_typed_message_exactly_once(
    routing_client: httpx.Client,
    routing_host: str,
    routing_arms: object,
    mock_router: MockRouter,
    type_into_tui: Callable[[str, str, str], None],
    tmp_path: Path,
) -> None:
    require_tmux()
    routing_arms("claude-native")  # type: ignore[operator]
    settings_before = claude_settings_apart_from_the_model()
    workspace = trusted_workspace(tmp_path, "claude_hook_ws")

    # A BARE routed create: Smart Routing on, no prompt, so nothing is routed
    # yet and the pick is left to the in-harness hook.
    created = create_routed_session(
        routing_client,
        agent_name="claude-native-ui",
        host_id=routing_host,
        workspace=workspace,
        message=None,
        terminal_launch_args=bypass_args("claude-native"),
    )
    session_id = created["id"]
    assert not decisions(routing_client, session_id), (
        "a bare create must route nothing — the first typed message is the trigger"
    )

    bridge_dir = claude_bridge_dir(session_id)
    wait_for_bridge_file(bridge_dir, "tmux.json", timeout=_LAUNCH_TIMEOUT_S)
    socket_path, target = tmux_target(bridge_dir)
    _wait_for_pane_ready(socket_path, target)

    # ── Turn 1: typed, blocked, routed, replayed ────────────────────────────
    first = unique_trivial_prompt("claude-turn-1")
    type_into_tui(socket_path, target, first)

    pane = wait_for_pane_text(socket_path, target, _BLOCK_NOTICE, timeout=_ROUTE_TIMEOUT_S)
    assert pane.count(_BLOCK_NOTICE) == 1, (
        f"the block notice appeared {pane.count(_BLOCK_NOTICE)} times; the replayed "
        "prompt must not be routed again\n" + pane[-2000:]
    )

    decision = wait_for(
        lambda: next(iter(decisions(routing_client, session_id, scope="turn")), None),
        timeout=_ROUTE_TIMEOUT_S,
        what="the in-harness turn routing decision",
    )
    expected = decide(first, "cc")
    assert decision["rationale"] == expected.rationale
    assert decision["router_source"] == "databricks-aigw"
    raw_pick = decision.get("raw_model") or decision["model"]
    assert same_arm(raw_pick, expected.model), (
        f"the hook routed to {raw_pick!r}, expected the {expected.model!r} arm"
    )

    # The apply layer must have typed ``/model <id>`` and had it take effect.
    wait_for_pane_text(socket_path, target, _MODEL_APPLIED_HINT, timeout=_ROUTE_TIMEOUT_S)

    # The local fast-skip marker is written before the block.
    wait_for_bridge_file(bridge_dir, MARKER_FILE, timeout=_ROUTE_TIMEOUT_S)

    # One thing typed is one user turn: a leaked replay would show two.
    turn_one_messages = wait_for(
        lambda: msgs if (msgs := user_messages(routing_client, session_id)) else None,
        timeout=_ROUTE_TIMEOUT_S,
        what="the replayed prompt to land as a user message",
    )
    assert len(turn_one_messages) == 1, (
        f"one typed prompt produced {len(turn_one_messages)} user messages — "
        "the block-and-replay leaked the blocked prompt"
    )
    first_calls = mock_router.calls_with(first)
    assert len(first_calls) == 1, (
        f"turn 1 issued {len(first_calls)} routing calls for its own prompt, expected exactly 1"
    )

    # ── Turn 2: fast-skips, with zero network ───────────────────────────────
    second = unique_trivial_prompt("claude-turn-2")
    type_into_tui(socket_path, target, second)
    settle(_QUIET_WINDOW_S)

    assert len(decisions(routing_client, session_id)) == 1, (
        "turn 2 produced a second routing decision; the session-level gate did not hold"
    )
    assert mock_router.calls_with(second) == [], (
        "turn 2's prompt reached the router; the fast skip must be free"
    )

    assert claude_settings_apart_from_the_model() == settings_before, (
        "~/.claude/settings.json changed beyond its `model` key — `/model <id>` "
        "may move Claude Code's own default, nothing else"
    )
