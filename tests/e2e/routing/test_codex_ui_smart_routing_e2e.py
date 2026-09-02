"""CUJ 2 — codex-native, Smart Routing at session create, then a delegated spawn.

The codex half of CUJ 1, plus the process truth only codex can give:

1. A routed create over the ``codex`` scenario (Codex arms only) routes the
   session's model before the terminal launches.
2. **Process truth.** The routed model is checked against the session's own
   ``config.toml`` and — when the thread has run a turn — the rollout's
   ``turn_context``, compared with :func:`~tests.e2e.routing._helpers.same_arm`
   because the codex apply layer deliberately writes codex's own slug spelling
   (``gpt-5.6-luna``) rather than the router's (``gpt-5-6-luna``). A byte
   comparison here is a false red.
3. A delegate-class spawn — narrow, code-shaped, short — is what task_v1 hands
   down to ``glm-5-2``, so the ``native_subagent`` decision must name that arm
   and stay inside the Codex family.
4. No ``BAD_REQUEST`` anywhere in the server log: a routed model the gateway
   cannot serve under the spelling we applied shows up there and nowhere else.

The spawn is asserted, never awaited.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omnigent.server.smart_routing import models_in_family
from tests.e2e.routing._helpers import (
    arm_in,
    codex_bridge_dir,
    decisions,
    grep_log,
    rollout_turn_contexts,
    same_arm,
    session_snapshot,
    wait_for,
    wait_for_bridge_file,
    wait_for_codex_config_arm,
    wait_for_decision,
)
from tests.e2e.routing._mock_router import CLAUDE_ARMS, MockRouter, decide
from tests.e2e.routing.conftest import (
    bypass_args,
    create_routed_session,
    post_user_message,
    prompts,
    require_tmux,
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

_SPAWN_TIMEOUT_S = 300.0

#: Seconds to wait for the launch to write the session's private ``CODEX_HOME``.
_CONFIG_TIMEOUT_S = 120.0


def test_codex_session_create_routes_the_model_and_delegates_the_spawn(
    routing_client: httpx.Client,
    routing_host: str,
    routing_arms: object,
    mock_router: MockRouter,
    routing_server_log: Path,
    tmp_path: Path,
) -> None:
    require_tmux()
    routing_arms("codex-native")  # type: ignore[operator]
    workspace = trusted_workspace(tmp_path, "codex_ui_ws")

    # Trivial routes to the codex scenario's cheapest arm, which is the arm this
    # workspace most reliably serves — the session model only has to be routed
    # and runnable; the delegate arm is exercised by the spawn below. Unique so
    # the mock's log can be filtered to this test's own calls.
    create_message = unique_trivial_prompt("codex-create")
    created = create_routed_session(
        routing_client,
        agent_name="codex-native-ui",
        host_id=routing_host,
        workspace=workspace,
        message=create_message,
        terminal_launch_args=bypass_args("codex-native"),
    )
    session_id = created["id"]

    decision = wait_for_decision(routing_client, session_id, scope="session", timeout=60.0)
    expected = decide(create_message, "codex")
    assert decision["rationale"] == expected.rationale
    assert decision["router_source"] == "databricks-aigw"
    assert decision["applied"] is True
    assert decision["harness"] == "codex-native"
    raw_pick = decision.get("raw_model") or decision["model"]
    assert same_arm(raw_pick, expected.model), (
        f"router picked {raw_pick!r}, expected the {expected.model!r} arm"
    )
    assert models_in_family("codex-native", [decision["model"]]), (
        f"a codex session was pinned to {decision['model']!r}, not a Codex arm"
    )

    snapshot = session_snapshot(routing_client, session_id)
    assert snapshot["cost_control_mode_override"] == "on"
    assert same_arm(snapshot.get("model_override"), decision["model"])

    # ── Process truth: config.toml carries the routed model in codex's slug ──
    bridge_dir = codex_bridge_dir(session_id)
    wait_for_bridge_file(bridge_dir, "codex-home", timeout=_CONFIG_TIMEOUT_S)
    wait_for_codex_config_arm(session_id, decision["model"], timeout=_CONFIG_TIMEOUT_S)

    # ── The spawn: delegate-class, so task_v1 hands it down to glm ───────────
    task = prompts()["delegate"]
    post_user_message(
        routing_client,
        session_id,
        spawn_prompt(task=task, tool_hint=spawn_tool_for("codex-native")),
    )
    spawn_decision = wait_for(
        lambda: next(
            iter(decisions(routing_client, session_id, scope="native_subagent")),
            None,
        ),
        timeout=_SPAWN_TIMEOUT_S,
        what="a native_subagent routing decision for the delegated spawn",
    )
    assert not arm_in(spawn_decision["model"], CLAUDE_ARMS), (
        f"a codex session spawned a Claude arm ({spawn_decision['model']!r}) — "
        "the same-harness constraint was not applied"
    )
    spawn_raw = spawn_decision.get("raw_model") or spawn_decision["model"]
    assert same_arm(spawn_raw, "glm-5-2"), (
        f"the delegate-class spawn was routed to {spawn_raw!r}, expected glm-5-2"
    )
    assert spawn_decision["rationale"] == decide(task, "codex").rationale
    assert spawn_decision["router_source"] == "databricks-aigw"

    # ── The rollout is what the process actually ran ─────────────────────────
    # Every turn the process ran must be on a model this session was actually
    # routed to: the session arm, or a delegate arm a routed spawn switched the
    # thread onto. Deliberately order-free — the parent's turns and the routed
    # spawn's share one rollout, and which lands first is not a routing property,
    # so pinning an index would test rollout bookkeeping instead. An un-routed
    # model appearing here is the real failure, and so is any Claude arm: a codex
    # thread must never leave its family. An empty rollout is not a failure —
    # this CUJ never waits for a turn to finish.
    routed_models = [
        row["model"] for row in decisions(routing_client, session_id) if row.get("model")
    ]
    for context in rollout_turn_contexts(session_id):
        model = context.get("model")
        if not isinstance(model, str) or not model:
            continue
        assert not arm_in(model, CLAUDE_ARMS), (
            f"a rollout turn_context ran on {model!r} — a codex thread must never leave its family"
        )
        assert arm_in(model, routed_models), (
            f"a rollout turn_context ran on {model!r}, which this session was never "
            f"routed to (routed: {routed_models})"
        )

    # ── The gateway never rejected an applied spelling ──────────────────────
    bad = grep_log(routing_server_log, ["BAD_REQUEST"])
    assert not bad, "the server log carries BAD_REQUEST lines:\n" + "\n".join(bad[:5])
    assert mock_router.calls_with("parse_duration"), (
        "the spawn's task text never reached the router"
    )
