"""CUJ 1 — claude-native, Smart Routing at session create, then a routed spawn.

The web UI's "Claude Code + Model = Smart Routing" path, driven over the API:

1. ``POST /v1/sessions`` with ``cost_control_mode_override="on"``, a
   ``smart_routing_message`` and **no** model pin — exactly what the UI sends
   (a pin silently disables routing). A ``claude-native`` session's turns originate in the TUI,
   so the server has to route the model during the create or not at all.
2. Score the create: one ``session``-scope decision whose ``router_source`` is
   ``databricks-aigw`` (the external client answered — this deployment has no
   built-in judge configured), carrying the router's own rule trace, with the
   pick applied to the session's ``model_override``.
3. POST a spawn-inducing message and score the ``native_subagent`` decision the
   spawn produces. A ``cc`` session offers Claude arms only, so the spawn must
   land on a Claude arm — never a Codex one.

**Spawns are asserted, never awaited.** The bar is that the spawn was issued
and routed; the subagent's own answer is out of scope, so nothing here waits on
child output or an inbox return. Nor does anything assert on answer content:
the pane does real inference, and a slow generation must not be able to redden
a routing test.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omnigent.server.smart_routing import models_in_family
from tests.e2e.routing._helpers import (
    arm_in,
    claude_settings_apart_from_the_model,
    decisions,
    same_arm,
    session_snapshot,
    wait_for,
    wait_for_decision,
)
from tests.e2e.routing._mock_router import CLAUDE_ARMS, CODEX_ARMS, MockRouter, decide
from tests.e2e.routing.conftest import (
    bypass_args,
    create_routed_session,
    post_user_message,
    prompts,
    require_tmux,
    sequence,
    spawn_prompt,
    spawn_tool_for,
    trusted_workspace,
    unique_trivial_prompt,
)

# The suite's default per-test cap is 300s; this CUJ's own waits (launch, a real
# turn, the spawn or the replay) can legitimately exceed it end to end. The budget
# sits above their sum so an internal wait names what never happened instead of
# the harness firing first on a sleep.
pytestmark = [pytest.mark.smart_routing, pytest.mark.timeout(900)]

#: Seconds to wait for the pane to boot, run a turn and issue its spawn. Real
#: inference on a real CLI, so generous — but bounded, and nothing after the
#: spawn is waited on.
_SPAWN_TIMEOUT_S = 300.0


def test_claude_session_create_routes_the_model_and_then_the_spawn(
    routing_client: httpx.Client,
    routing_host: str,
    routing_arms: object,
    mock_router: MockRouter,
    tmp_path: Path,
) -> None:
    require_tmux()
    routing_arms("claude-native")  # type: ignore[operator]
    settings_before = claude_settings_apart_from_the_model()
    workspace = trusted_workspace(tmp_path, "claude_ui_ws")

    # ── 1. The routed create ────────────────────────────────────────────────
    # Unique but still rule-0 (short, no error/code markers), so the mock's
    # served-request log can be filtered to this test's own calls even while a
    # pane from another CUJ is alive.
    create_message = unique_trivial_prompt("claude-create")
    created = create_routed_session(
        routing_client,
        agent_name="claude-native-ui",
        host_id=routing_host,
        workspace=workspace,
        message=create_message,
        terminal_launch_args=bypass_args("claude-native"),
    )
    session_id = created["id"]

    # ── 2. Score the create-time decision ───────────────────────────────────
    decision = wait_for_decision(routing_client, session_id, scope="session", timeout=60.0)
    expected = decide(create_message, "cc")
    assert decision["rationale"] == expected.rationale, (
        f"the decision carries {decision['rationale']!r}, not the router's rule trace"
    )
    assert decision["router_source"] == "databricks-aigw", (
        "the external routes:select client answered, so the chip must disclose the "
        f"gateway as the source; got {decision.get('router_source')!r}"
    )
    assert decision["applied"] is True
    assert decision["harness"] == "claude-native"
    # The raw pick is the router's vocabulary; ``model`` is what this deployment
    # can actually serve. Both must name the sonnet arm the cc recipe chose.
    raw_pick = decision.get("raw_model") or decision["model"]
    assert same_arm(raw_pick, expected.model), (
        f"router picked {raw_pick!r}, expected the {expected.model!r} arm"
    )
    assert models_in_family("claude-native", [decision["model"]]), (
        f"a cc session was pinned to {decision['model']!r}, which is not a Claude arm"
    )

    snapshot = session_snapshot(routing_client, session_id)
    assert snapshot["cost_control_mode_override"] == "on", (
        "Smart Routing must stay stamped on the session row"
    )
    assert same_arm(snapshot.get("model_override"), decision["model"]), (
        f"the routed model {decision['model']!r} was not pinned to the session "
        f"(model_override={snapshot.get('model_override')!r})"
    )

    # Exactly one routing call served the create.
    create_calls = mock_router.calls_with(create_message)
    assert len(create_calls) == 1, (
        f"the create issued {len(create_calls)} routing calls, expected 1"
    )
    assert "claude-opus-4-8" in create_calls[0].offered, (
        "the client must inject the full cc menu, not just the servable catalog"
    )

    # ── 3. The spawn: issued and routed, never awaited ──────────────────────
    task = prompts()["delegate"]
    post_user_message(
        routing_client,
        session_id,
        spawn_prompt(task=task, tool_hint=spawn_tool_for("claude-native")),
    )
    spawn_decision = wait_for(
        lambda: next(
            iter(decisions(routing_client, session_id, scope="native_subagent")),
            None,
        ),
        timeout=_SPAWN_TIMEOUT_S,
        what="a native_subagent routing decision for the spawn",
    )
    # A cc session's spawn menu is Claude-only: the arm the router hands back is
    # the one the spawn is rewritten onto, and it may never be a Codex arm.
    assert not arm_in(spawn_decision["model"], CODEX_ARMS), (
        f"a cc session spawned a Codex arm ({spawn_decision['model']!r}) — "
        "the same-harness constraint was not applied"
    )
    spawn_raw = spawn_decision.get("raw_model") or spawn_decision["model"]
    assert arm_in(spawn_raw, CLAUDE_ARMS), (
        f"the spawn was routed to {spawn_raw!r}, which is not a Claude arm "
        f"({sequence(CLAUDE_ARMS)})"
    )
    assert spawn_decision["rationale"] == decide(task, "cc").rationale, (
        "the spawn decision must carry the router's trace for the spawn's own task text"
    )
    assert spawn_decision["router_source"] == "databricks-aigw"

    # The spawn's own task text reached the router as a separate call.
    assert mock_router.calls_with("parse_duration"), (
        "the spawn's task text never reached the router"
    )

    # ── 4. Isolation: only Claude Code's own `model` key may have moved ──────
    assert claude_settings_apart_from_the_model() == settings_before, (
        "~/.claude/settings.json changed beyond its `model` key — routing must "
        "confine its hook wiring to the session's bridge dir"
    )
