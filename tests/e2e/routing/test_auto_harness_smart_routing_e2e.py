"""CUJ 5 — the auto harness: routing picks the family, and a spawn crosses it.

The web UI's top-level "Smart Routing" harness row, driven over the API. This
is the only scenario where cross-harness spawns are legal, so it is the only
place the redirect can be exercised end to end:

1. ``harness_override="auto"`` + ``cost_control_mode_override="on"`` +
   ``smart_routing_message``. The terminal launches with the session row, so a
   native session's HARNESS cannot wait for a first message — the create routes
   it over the ``both`` menu (all five arms) and rebinds the session to the
   wrapper it picked.
2. Score the create: a concrete arm, the auto-harness label recorded durably
   (it is what lets subagent routing offer the other family later), and Smart
   Routing stamped on the row.
3. Send a message whose sub-task the ``both`` recipe places on the **counterpart**
   family. The spawn is denied with an instruction naming
   ``mcp__omnigent__sys_session_create`` — the tool a routed spawn actually
   holds — and the ``native_subagent`` decision records the cross-family arm.

**Nothing is awaited past the spawn.** The child conversation row is checked in
a short best-effort window only; the inbox return never is. A cross-harness
spawn's result arriving is a property of the child session, not of routing.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omnigent.runner.subagent_routing import AUTO_HARNESS_LABEL_KEY
from tests.e2e.routing._helpers import (
    arm_in,
    claude_bridge_dir,
    claude_settings_apart_from_the_model,
    decisions,
    same_arm,
    session_snapshot,
    tmux_target,
    wait_for,
    wait_for_bridge_file,
    wait_for_decision,
    wait_for_pane_identifier,
)
from tests.e2e.routing._mock_router import CLAUDE_ARMS, CODEX_ARMS, MockRouter, decide
from tests.e2e.routing.conftest import (
    bypass_args,
    create_routed_session,
    post_user_message,
    prompts,
    require_tmux,
    spawn_prompt,
    trusted_workspace,
    unique_trivial_prompt,
)

# The suite's default per-test cap is 300s, which this CUJ's own waits can
# legitimately exceed end to end (launch, a real turn, the spawn, the pane). The
# budget has to sit above the sum of them, or the harness fires first and the
# traceback points at a sleep instead of naming what never happened.
pytestmark = [pytest.mark.smart_routing, pytest.mark.timeout(900)]

#: The routed spawn's deny reason must name the tool the session actually holds,
#: in the spelling claude-native advertises it under.
_REDIRECT_TOOL = "mcp__omnigent__sys_session_create"

_SPAWN_TIMEOUT_S = 300.0
_PANE_TIMEOUT_S = 300.0

#: Best-effort window for the child conversation row. Short on purpose: the
#: child's existence is a nice-to-have signal, and waiting on it would turn this
#: CUJ into a test of child-session startup.
_CHILD_WINDOW_S = 45.0


def _cross_family_subtask() -> str:
    """Return a sub-task the ``both`` recipe places on the Codex family.

    Short and code-free, so rule-0 fires and picks the scenario's cheapest arm —
    which on any Codex-bearing scenario is a Codex arm, the counterpart of a
    Claude session.

    :returns: The sub-task text.
    """
    return unique_trivial_prompt("auto-subtask")


def test_auto_harness_lands_an_arm_then_redirects_a_cross_family_spawn(
    routing_client: httpx.Client,
    routing_host: str,
    routing_arms: object,
    mock_router: MockRouter,
    tmp_path: Path,
) -> None:
    require_tmux()
    # The auto route chooses between both families, so both must be backed.
    routing_arms("claude-native", "codex-native")  # type: ignore[operator]
    settings_before = claude_settings_apart_from_the_model()
    workspace = trusted_workspace(tmp_path, "auto_ws")

    # A contained, code-referencing task: on the ``both`` menu the recipe
    # escalates it to the Claude arm, which lands the session on claude-native
    # and makes the Codex family the counterpart.
    session_message = prompts()["escalate"]
    created = create_routed_session(
        routing_client,
        # The auto create binds the claude-native built-in as a placeholder; the
        # server rebinds it to whichever wrapper routing picks.
        agent_name="claude-native-ui",
        host_id=routing_host,
        workspace=workspace,
        message=session_message,
        harness_override="auto",
        terminal_launch_args=bypass_args("claude-native"),
    )
    session_id = created["id"]

    # ── The create landed a concrete arm on a concrete harness ───────────────
    decision = wait_for_decision(routing_client, session_id, scope="session", timeout=60.0)
    expected = decide(session_message, "both")
    assert decision["rationale"] == expected.rationale
    assert decision["router_source"] == "databricks-aigw"
    assert decision["applied"] is True
    raw_pick = decision.get("raw_model") or decision["model"]
    assert same_arm(raw_pick, expected.model), (
        f"the auto create picked {raw_pick!r}, expected the {expected.model!r} arm"
    )
    assert decision["harness"] == "claude-native", (
        f"the {expected.model!r} pick must land on claude-native, not {decision['harness']!r}"
    )
    create_calls = mock_router.calls_with("--dry-run")
    assert create_calls, "the create issued no routing call"
    for arm in (*CLAUDE_ARMS, *CODEX_ARMS):
        assert arm in create_calls[0].offered, (
            f"the auto create must offer the full both-scenario menu; {arm} was absent"
        )

    snapshot = session_snapshot(routing_client, session_id)
    assert snapshot["cost_control_mode_override"] == "on"
    assert (snapshot.get("labels") or {}).get(AUTO_HARNESS_LABEL_KEY) == "1", (
        "the auto start must be recorded durably — it is what lets subagent "
        "routing offer picks from the other harness family"
    )
    assert same_arm(snapshot.get("model_override"), decision["model"])

    # ── The cross-family spawn: routed, denied, and named ────────────────────
    subtask = _cross_family_subtask()
    post_user_message(
        routing_client,
        session_id,
        spawn_prompt(task=subtask, tool_hint="Task"),
    )

    spawn_decision = wait_for(
        lambda: next(
            iter(decisions(routing_client, session_id, scope="native_subagent")),
            None,
        ),
        timeout=_SPAWN_TIMEOUT_S,
        what="a native_subagent routing decision for the cross-family spawn",
    )
    spawn_raw = spawn_decision.get("raw_model") or spawn_decision["model"]
    assert same_arm(spawn_raw, decide(subtask, "both").model), (
        f"the sub-task routed to {spawn_raw!r}, expected the both-scenario "
        f"rule-0 arm {decide(subtask, 'both').model!r}"
    )
    assert arm_in(spawn_raw, CODEX_ARMS), (
        f"the spawn was expected on the counterpart (Codex) family; got {spawn_raw!r}"
    )
    assert spawn_decision["router_source"] == "databricks-aigw"

    # The deny is the actuation: it must name the tool the session holds, in the
    # spelling claude-native advertises it under, or the model drops the sub-task.
    bridge_dir = claude_bridge_dir(session_id)
    wait_for_bridge_file(bridge_dir, "tmux.json", timeout=120.0)
    socket_path, target = tmux_target(bridge_dir)
    wait_for_pane_identifier(socket_path, target, _REDIRECT_TOOL, timeout=_PANE_TIMEOUT_S)

    # ── Best effort only: the child row, never the child's result ────────────
    def _child() -> dict[str, object] | None:
        resp = routing_client.get("/v1/sessions", params={"limit": 100})
        if resp.status_code != 200:
            return None
        for row in resp.json().get("data", []):
            if row.get("parent_session_id") == session_id:
                return row
        return None

    try:
        child = wait_for(_child, timeout=_CHILD_WINDOW_S, what="a child conversation row")
    except AssertionError:
        child = None
    if child is not None:
        assert not arm_in(str(child.get("model_override") or ""), CLAUDE_ARMS), (
            "the redirected child session must run the routed Codex arm, not a "
            f"Claude one (model_override={child.get('model_override')!r})"
        )

    assert claude_settings_apart_from_the_model() == settings_before, (
        "~/.claude/settings.json changed beyond its `model` key — routing must "
        "confine its hook wiring to the session's bridge dir"
    )
