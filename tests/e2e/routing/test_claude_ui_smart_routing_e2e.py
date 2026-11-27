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

**Gateway spawns are asserted, never awaited.** The bar is that the spawn was issued
and routed; the subagent's own answer is out of scope, so nothing here waits on
child output or an inbox return. Nor does anything assert on answer content:
the pane does real inference, and a slow generation must not be able to redden
a routing test. The isolated mock-provider variant also checks two complete
native/provider turns, exact-once public delivery and an unchanged saved default.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.claude_native import bridge, editor_handoff, model_switch
from omnigent.runner.turn_routing import read_pending_replay, turn_routing_marker_present
from omnigent.server.smart_routing import models_in_family
from tests._helpers.native_claude import (
    _PINNED_CLAUDE_MODELS,
    _assert_mock_claude_turn,
    _assert_native_claude_turn,
    _live_claude_snapshot,
    _require_session_only_claude,
    isolated_claude_mock_routing_stack,
)
from tests._helpers.polling import poll_until
from tests._helpers.session import (
    marker_turn_prompt,
    message_item_text,
    ordered_message_items,
    send_user_text,
)
from tests.e2e.conftest import get_mock_requests, set_fallback_mock_llm
from tests.e2e.routing._helpers import (
    arm_in,
    capture_pane,
    claude_settings_snapshot,
    claude_settings_unchanged,
    claude_user_settings_path,
    decisions,
    same_arm,
    session_snapshot,
    wait_for,
    wait_for_decision,
    wait_for_session_pane,
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
from tests.harnesses.claude_native._model_status_oracle import native_claude_wire_model

# The suite's default per-test cap is 300s; this CUJ's own waits (launch, a real
# turn, the spawn or the replay) can legitimately exceed it end to end. The budget
# sits above their sum so an internal wait names what never happened instead of
# the harness firing first on a sleep.
pytestmark = [pytest.mark.smart_routing, pytest.mark.timeout(900)]

#: Seconds to wait for the pane to boot, run a turn and issue its spawn. Real
#: inference on a real CLI, so generous — but bounded, and nothing after the
#: spawn is waited on.
_SPAWN_TIMEOUT_S = 300.0


def _assert_mock_created_session_preserves_default(
    mock_url: str,
    tmp_path: Path,
    *,
    entry: str = "create",
    type_into_tui: Callable[[str, str, str], None] | None = None,
    record_property: Callable[[str, object], None],
) -> None:
    judge_model = "mock-claude-routing-judge"
    target_model = _PINNED_CLAUDE_MODELS["opus"]
    with isolated_claude_mock_routing_stack(
        tmp_path,
        mock_url,
        models=_PINNED_CLAUDE_MODELS,
        judge_model=judge_model,
    ) as (base_url, client, host_id, workspace, settings_path):
        saved_settings = claude_settings_snapshot(settings_path)
        set_fallback_mock_llm(
            mock_url,
            judge_model,
            json.dumps(
                {
                    "harness": "claude-native",
                    "model": target_model,
                    "rationale": "Use the exact configured Opus model for this complex task.",
                }
            ),
        )
        nonce = uuid.uuid4().hex[:8]
        turns = [
            (f"route-user-{index}-{nonce}", f"route-reply-{index}-{nonce}") for index in (1, 2)
        ]
        agents = client.get("/v1/agents")
        agents.raise_for_status()
        agent_id = next(
            row["id"] for row in agents.json()["data"] if row["name"] == "claude-native-ui"
        )
        creation = {
            "agent_id": agent_id,
            "host_id": host_id,
            "workspace": str(workspace),
            "cost_control_mode_override": "on",
        }
        if entry == "create":
            creation["smart_routing_message"] = marker_turn_prompt(1, *turns[0])
        created = client.post("/v1/sessions", json=creation, timeout=120)
        created.raise_for_status()
        session_id = str(created.json()["id"])
        try:
            _require_session_only_claude(base_url, session_id)
            assert (
                _live_claude_snapshot(base_url, session_id)["cost_control_mode_override"] == "on"
            )
            assert claude_settings_snapshot(settings_path) == saved_settings
            if entry != "create":
                assert decisions(client, session_id) == []
            snapshot = _live_claude_snapshot(base_url, session_id)
            bridge_id = snapshot["labels"].get(bridge.BRIDGE_ID_LABEL_KEY) or session_id
            bridge_dir = bridge.bridge_dir_for_bridge_id(bridge_id)
            native_sessions = []
            delivered = []
            for index, (marker, reply) in enumerate(turns, start=1):
                set_fallback_mock_llm(mock_url, "default", reply)
                set_fallback_mock_llm(mock_url, native_claude_wire_model(target_model), reply)
                prompt = marker_turn_prompt(index, marker, reply)
                if entry != "create" and index == 1:
                    prefix = prompt + "\n\n"
                    prompt = (
                        prefix + ("wrapped continuation text " * 20)[: 336 - len(prefix)] + "\n"
                    )
                    assert len(prompt) == 337
                if entry == "terminal":
                    assert type_into_tui is not None
                    socket_path, target = wait_for_session_pane(client, session_id, timeout=60)
                    type_into_tui(socket_path, target, prompt)
                else:
                    send_user_text(client, session_id=session_id, text=prompt)
                poll_until(
                    lambda reply=reply: any(
                        item.get("role") == "assistant" and message_item_text(item) == reply
                        for item in ordered_message_items(base_url, session_id)
                    ),
                    timeout_s=120 if entry == "terminal" and index == 1 else 30,
                )
                messages = ordered_message_items(base_url, session_id)
                assert len([item for item in messages if item.get("role") == "assistant"]) == index
                native_session, user_text = _assert_native_claude_turn(
                    base_url, session_id, marker, reply, target_model
                )
                assert user_text == prompt
                _assert_mock_claude_turn(
                    mock_url,
                    marker,
                    target_model,
                    native_session=native_session,
                    user_text=user_text,
                )
                native_sessions.append(native_session)
                delivered.append(
                    {
                        "length": len(user_text),
                        "sha256": hashlib.sha256(user_text.encode()).hexdigest(),
                        "trailing_lf": user_text.endswith("\n"),
                    }
                )
                routed = decisions(client, session_id)
                assert routed and all(
                    row.get("applied") is True
                    and row.get("model") == target_model
                    and row.get("router_source") == "oss-llm"
                    for row in routed
                ), routed
                assert len(get_mock_requests(mock_url, key=judge_model)) == 1
                assert claude_settings_snapshot(settings_path) == saved_settings
                assert not _live_claude_snapshot(base_url, session_id).get("last_task_error")
                if entry == "terminal":
                    owner = editor_handoff.read_controller_editor_binding(bridge_dir)
                    assert owner is not None and owner.owner.native_session_id == native_session
                    assert owner.owner.bridge_session_id == session_id
                    assert turn_routing_marker_present(bridge_dir, session_id)
                    assert read_pending_replay(bridge_dir) is None
            assert native_sessions[0] == native_sessions[1]
            record_property(
                "routing_acceptance",
                json.dumps(
                    {
                        "entry": entry,
                        "provider": "mock",
                        "delivered": delivered,
                        "turns": 2,
                        "judge_calls": 1,
                        "same_native_session": True,
                        "saved_default_unchanged": True,
                        "owner_verified": entry == "terminal",
                    }
                ),
            )
        except Exception:
            with contextlib.suppress(Exception):
                snapshot = _live_claude_snapshot(base_url, session_id)
                bridge_id = snapshot["labels"].get(bridge.BRIDGE_ID_LABEL_KEY) or session_id
                bridge_dir = bridge.bridge_dir_for_bridge_id(bridge_id)
                target = bridge.read_tmux_target(bridge_dir)
                state_keys = ("status", "model_override", "llm_model", "last_task_error")
                owner = editor_handoff.read_controller_editor_binding(bridge_dir)
                pending = read_pending_replay(bridge_dir)
                choice = (
                    model_switch._target(
                        pending.model,
                        bridge.read_model_picker_options(bridge_dir),
                        bridge.read_model_picker_values(bridge_dir),
                        bridge.read_model_env(bridge_dir),
                    )
                    if pending is not None and pending.model is not None
                    else None
                )
                items = client.get(
                    f"/v1/sessions/{session_id}/items", params={"limit": 100, "order": "asc"}
                )
                items.raise_for_status()
                log_paths = sorted((tmp_path / "host").rglob("*.log"))
                record_property(
                    "routing_failure",
                    json.dumps(
                        {
                            "entry": entry,
                            "state": {key: snapshot.get(key) for key in state_keys},
                            "pending": repr(pending),
                            "owner": repr(owner),
                            "target": repr(choice),
                            "marker": turn_routing_marker_present(bridge_dir, session_id),
                            "native_status": bridge._read_json_file(
                                bridge_dir / bridge.CONTEXT_RAW_FILE
                            ),
                            "nonmessage_items": [
                                item
                                for item in items.json().get("data", [])
                                if item.get("type") != "message"
                            ],
                            "routing_logs": {
                                str(path.relative_to(tmp_path)): "\n".join(
                                    line
                                    for line in path.read_text(errors="replace").splitlines()
                                    if any(
                                        key in line.lower()
                                        for key in (
                                            "route-turn:",
                                            "replay",
                                            "model switch",
                                            "model_switch",
                                        )
                                    )
                                )[-12000:]
                                for path in log_paths
                                if path.is_file()
                            },
                            "pane": capture_pane(target["socket_path"], target["tmux_target"])
                            if target is not None
                            else None,
                        }
                    ),
                )
            raise
        finally:
            client.delete(f"/v1/sessions/{session_id}").raise_for_status()


@pytest.mark.parametrize(
    "creation_case",
    [
        "gateway-spawn",
        pytest.param("mock-provider", marks=pytest.mark.mock_only),
        pytest.param("mock-chat", marks=pytest.mark.mock_only),
        pytest.param("mock-terminal", marks=pytest.mark.mock_only),
    ],
)
def test_claude_session_create_routes_the_model_and_then_the_spawn(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    creation_case: str,
    claude_managed_settings: None,
    record_property: Callable[[str, object], None],
) -> None:
    require_tmux()
    if creation_case.startswith("mock-"):
        entry = {"mock-provider": "create", "mock-chat": "chat", "mock-terminal": "terminal"}[
            creation_case
        ]
        _assert_mock_created_session_preserves_default(
            request.getfixturevalue("isolated_mock_llm_server_url"),
            tmp_path,
            entry=entry,
            type_into_tui=request.getfixturevalue("type_into_tui")
            if entry == "terminal"
            else None,
            record_property=record_property,
        )
        return
    routing_client: httpx.Client = request.getfixturevalue("routing_client")
    routing_host: str = request.getfixturevalue("routing_host")
    routing_claude_settings_env = request.getfixturevalue("routing_claude_settings_env")
    routing_arms = request.getfixturevalue("routing_arms")
    mock_router: MockRouter = request.getfixturevalue("mock_router")
    routing_arms("claude-native")  # type: ignore[operator]
    workspace = trusted_workspace(tmp_path, "claude_ui_ws")
    settings_path = claude_user_settings_path(workspace, routing_claude_settings_env)
    settings_before = claude_settings_snapshot(settings_path)

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

    # Only newer CLIs promise to preserve the saved model default.
    assert claude_settings_unchanged(settings_path, settings_before, session_id), (
        "Claude settings changed fields promised by the running CLI version"
    )
