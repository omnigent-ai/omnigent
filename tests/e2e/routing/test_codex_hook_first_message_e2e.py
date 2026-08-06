"""CUJ 4 — codex-native in-harness routing of the first message TYPED into the TUI.

The codex half of CUJ 3, plus the two things only codex can show:

* **The applied spelling is codex's own slug.** Codex serves either spelling but
  recognizes only its own, so an untranslated switch runs the right model while
  the TUI warns about missing metadata and ``/model`` keeps highlighting the
  launch slug. The hook translates before ``thread/settings/update`` and mirrors
  the accepted value into the session's ``config.toml`` — which must therefore
  read ``gpt-5.6-luna``, not ``databricks-gpt-5-6-luna``.
* **``/model`` highlights the routed arm.** The picker reads the live thread, so
  the routed arm showing up there is what proves the switch reached the process
  rather than only the file.

The gate for the fast skip is the session's **routing-decision label**, not the
bridge-dir marker and not ``model_override``; the marker is a local optimization
that saves a round trip. Turn 2 must therefore issue zero routing calls.

The prompt is typed, never POSTed — see CUJ 3's module docstring for why.

A 5x repeat variant burns in the block-and-replay handshake, whose failure mode
is a timing race. It multiplies an already slow suite, so it is gated behind
``OMNIGENT_E2E_RELIABILITY=1`` on top of the suite's own opt-in.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from omnigent.codex_model_vocabulary import codex_spawn_model
from omnigent.runner.subagent_routing import ROUTING_DECISION_LABEL_KEY
from omnigent.runner.turn_routing import MARKER_FILE
from tests.e2e.routing._helpers import (
    capture_pane,
    codex_bridge_dir,
    decisions,
    same_arm,
    session_snapshot,
    settle,
    user_messages,
    wait_for,
    wait_for_bridge_file,
    wait_for_codex_config_arm,
    wait_for_pane_text,
    wait_for_session_pane,
)
from tests.e2e.routing._mock_router import MockRouter, decide
from tests.e2e.routing.conftest import (
    RELIABILITY_GATE_ENV,
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

_BLOCK_NOTICE = "Smart Routing selected"

#: Text codex's TUI shows once its composer is mounted.
_PANE_READY_HINTS = ("Ask Codex", "for shortcuts", "▌", "›")

_LAUNCH_TIMEOUT_S = 180.0
_ROUTE_TIMEOUT_S = 180.0
_PICKER_TIMEOUT_S = 60.0
_QUIET_WINDOW_S = 60.0


def _wait_for_pane_ready(socket_path: str, target: str) -> None:
    """Wait until codex's composer is mounted.

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
        what="codex's composer to mount",
    )


def _open_model_picker(socket_path: str, target: str) -> None:
    """Type a bare ``/model`` into the pane and submit it.

    :param socket_path: The tmux server socket.
    :param target: The pane target.
    """
    for args in (
        ("send-keys", "-t", target, "C-u"),
        ("send-keys", "-l", "-t", target, "/model"),
        ("send-keys", "-t", target, "Enter"),
    ):
        subprocess.run(["tmux", "-S", socket_path, *args], check=True, timeout=15)


def _dismiss_model_picker(socket_path: str, target: str) -> None:
    """Close the picker without changing anything.

    :param socket_path: The tmux server socket.
    :param target: The pane target.
    """
    subprocess.run(
        ["tmux", "-S", socket_path, "send-keys", "-t", target, "Escape"],
        check=False,
        timeout=15,
    )


def _run_cuj(
    routing_client: httpx.Client,
    routing_host: str,
    mock_router: MockRouter,
    type_into_tui: Callable[[str, str, str], None],
    workspace: Path,
) -> None:
    """Drive and score one full pass of the codex first-message CUJ.

    Factored out so the reliability variant can repeat it without duplicating
    the assertions.

    :param routing_client: HTTP client pointed at the routing server.
    :param routing_host: Host to launch on.
    :param mock_router: The routing mock, for the call-count assertions.
    :param type_into_tui: Keystroke helper.
    :param workspace: Workspace for this pass (one per pass — a fresh session
        must not inherit another's bridge dir).
    """
    created = create_routed_session(
        routing_client,
        agent_name="codex-native-ui",
        host_id=routing_host,
        workspace=workspace,
        message=None,
        terminal_launch_args=bypass_args("codex-native"),
    )
    session_id = created["id"]
    assert not decisions(routing_client, session_id), (
        "a bare create must route nothing — the first typed message is the trigger"
    )

    bridge_dir = codex_bridge_dir(session_id)
    socket_path, target = wait_for_session_pane(
        routing_client, session_id, timeout=_LAUNCH_TIMEOUT_S
    )
    _wait_for_pane_ready(socket_path, target)

    # ── Turn 1: typed, blocked, routed, replayed ────────────────────────────
    first = unique_trivial_prompt("codex-turn-1")
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
    expected = decide(first, "codex")
    assert decision["rationale"] == expected.rationale
    assert decision["router_source"] == "databricks-aigw"
    raw_pick = decision.get("raw_model") or decision["model"]
    assert same_arm(raw_pick, expected.model), (
        f"the hook routed to {raw_pick!r}, expected the {expected.model!r} arm"
    )

    # ── The applied spelling is codex's own slug ─────────────────────────────
    applied = wait_for_codex_config_arm(session_id, decision["model"], timeout=_ROUTE_TIMEOUT_S)
    slug = codex_spawn_model(decision["model"])
    if slug is not None:
        assert applied == slug, (
            f"config.toml carries {applied!r}; thread/settings/update must apply "
            f"codex's own slug {slug!r}, or /model keeps highlighting the launch model"
        )

    # ── /model highlights the routed arm ────────────────────────────────────
    _open_model_picker(socket_path, target)
    try:
        wait_for_pane_text(socket_path, target, applied, timeout=_PICKER_TIMEOUT_S)
    finally:
        _dismiss_model_picker(socket_path, target)

    wait_for_bridge_file(bridge_dir, MARKER_FILE, timeout=_ROUTE_TIMEOUT_S)

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

    # ── The gate is the label, not the marker ───────────────────────────────
    labels = session_snapshot(routing_client, session_id).get("labels") or {}
    assert labels.get(ROUTING_DECISION_LABEL_KEY), (
        "the routing-decision label is the authoritative gate and must be stamped; "
        f"labels={labels}"
    )

    # ── Turn 2: fast-skips, with zero network ───────────────────────────────
    second = unique_trivial_prompt("codex-turn-2")
    type_into_tui(socket_path, target, second)
    settle(_QUIET_WINDOW_S)

    assert len(decisions(routing_client, session_id)) == 1, (
        "turn 2 produced a second routing decision; the label gate did not hold"
    )
    assert mock_router.calls_with(second) == [], (
        "turn 2's prompt reached the router; the fast skip must be free"
    )


def test_codex_routes_the_first_typed_message_exactly_once(
    routing_client: httpx.Client,
    routing_host: str,
    routing_arms: object,
    mock_router: MockRouter,
    type_into_tui: Callable[[str, str, str], None],
    tmp_path: Path,
) -> None:
    require_tmux()
    routing_arms("codex-native")  # type: ignore[operator]
    _run_cuj(
        routing_client,
        routing_host,
        mock_router,
        type_into_tui,
        trusted_workspace(tmp_path, "codex_hook_ws"),
    )


@pytest.mark.nightly
@pytest.mark.skipif(
    os.environ.get(RELIABILITY_GATE_ENV) != "1",
    reason=(
        f"repeat variant: set {RELIABILITY_GATE_ENV}=1 to burn in the "
        "block-and-replay handshake across 5 passes"
    ),
)
@pytest.mark.parametrize("attempt", range(5))
def test_codex_first_typed_message_is_reliable(
    routing_client: httpx.Client,
    routing_host: str,
    routing_arms: object,
    mock_router: MockRouter,
    type_into_tui: Callable[[str, str, str], None],
    tmp_path: Path,
    attempt: int,
) -> None:
    require_tmux()
    routing_arms("codex-native")  # type: ignore[operator]
    _run_cuj(
        routing_client,
        routing_host,
        mock_router,
        type_into_tui,
        trusted_workspace(tmp_path, f"codex_hook_ws_{attempt}"),
    )
