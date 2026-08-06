"""Mock-LLM integration coverage for agent-team PEER messaging.

Exercises the whole ``sys_session_send``-by-``session_id`` chain against a live
server + runner with a scripted mock LLM: authorization
(``_peer_send_allowed``), completion routing (``awaiter_session_id``), and the
SSE/topology invariant that a peer send leaves the target's structural parent
alone.

The load-bearing claim: when teammate ALICE messages teammate BOB by
``session_id`` (both under the same team root), BOB's reply is delivered into
ALICE's inbox — NOT the lead's — while BOB's ``parent_session_id`` stays the
lead. Unit tests cover the registry in isolation; only a live run proves the
runner, the server's ``team`` resolution, and the inbox wake agree.

Why the team bundle is built here rather than registered inline: sub-agents are
discovered from an ``agents/<name>/`` directory tree
(``omnigent.spec.parser._discover_sub_agents``), so a team needs a real
directory bundle, which ``register_inline_agent``'s single-file tarball cannot
express.

A peer send addresses BOB by his RUNTIME ``session_id``, which does not exist
until BOB is spawned — so each test spawns the team first, discovers BOB's real
id, and only then scripts ALICE's peer turn.

Runs in the default suite in mock mode (no ``--llm-api-key``); the
``tests/integration`` package gate is lifted in mock mode by
``tests/integration/conftest.py``.
"""

from __future__ import annotations

import io
import json
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    send_user_message_to_session,
)

_COORD_MODEL = "mock-coordinator"
_ALICE_MODEL = "mock-alice"
_BOB_MODEL = "mock-bob"

_BOB_SENTINEL = "BOB_ANSWER_7F3A"
_ALICE_QUESTION = "what is your favorite data structure and why?"

_CHILD_WAIT_S = 60.0
_SENTINEL_WAIT_S = 60.0
_POLL_INTERVAL_S = 0.5


def _peer_send_call(session_id: str, args: object, *, call_id: str) -> dict[str, Any]:
    """A ``tool_calls`` entry: by-session-id PEER send (no agent/title)."""
    return {
        "call_id": call_id,
        "name": "sys_session_send",
        "arguments": json.dumps({"session_id": session_id, "args": args}),
    }


def _named_send_call(agent: str, title: str, args: object, *, call_id: str) -> dict[str, Any]:
    """A ``tool_calls`` entry: named ``(agent, title)`` child dispatch."""
    return {
        "call_id": call_id,
        "name": "sys_session_send",
        "arguments": json.dumps({"agent": agent, "title": title, "args": args}),
    }


def _tool_call(name: str, arguments: dict[str, Any], *, call_id: str) -> dict[str, Any]:
    return {"call_id": call_id, "name": name, "arguments": json.dumps(arguments)}


def _write_team_bundle(
    root: Path,
    mock_llm_base_url: str,
    *,
    team: bool,
    peer_send_cap: int | None = None,
) -> Path:
    """Write a lead + alice/bob directory bundle wired to the mock LLM.

    ``team`` toggles the TOP-LEVEL opt-in flag. Declared sub-agents share the
    parent bundle's ``agent_id``, so the lead and both teammates resolve this
    one flag — setting it ``False`` is how the negative case proves refusal, and
    it is also why a teammate cannot self-promote.

    ``spawn: true`` on each teammate is what REGISTERS ``sys_session_send`` on a
    leaf (read tools are always on; the write tool needs declared sub-agents or
    spawn). ``team`` is what AUTHORIZES the peer target.
    """

    def _executor(model: str) -> dict[str, Any]:
        return {
            "type": "omnigent",
            "config": {"harness": "openai-agents"},
            "model": model,
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": mock_llm_base_url},
            "connection": {"base_url": mock_llm_base_url, "api_key": "mock-key"},
        }

    def _teammate(name: str, model: str) -> dict[str, Any]:
        return {
            "spec_version": 1,
            "name": name,
            "description": f"{name} — a peer teammate that messages siblings by session_id.",
            "team": True,
            "spawn": True,
            "executor": _executor(model),
            "prompt": f"You are {name}. Follow the scripted mock LLM tool calls exactly.\n",
            "async": True,
            "cancellable": True,
            "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
        }

    lead: dict[str, Any] = {
        "spec_version": 1,
        "name": "team_demo",
        "description": "An agent-team lead that spawns two peer teammates.",
        "team": team,
        "executor": _executor(_COORD_MODEL),
        "prompt": "You are the team coordinator. Follow the scripted mock LLM tool calls.\n",
        "async": True,
        "cancellable": True,
        "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
        "tools": {"agents": ["alice", "bob"]},
    }
    if peer_send_cap is not None:
        # The bound must live at the TOP level: sub-agents share the parent's
        # agent_id, so alice's tool calls are evaluated against this spec's
        # guardrails, never her own sub-config's.
        lead["guardrails"] = {
            "policies": {
                "team_bounds": {
                    "type": "function",
                    "function": {
                        "path": "omnigent.inner.nessie.policies.team_bounds",
                        "arguments": {"max_peer_sends_per_turn": peer_send_cap},
                    },
                }
            }
        }

    (root / "agents" / "alice").mkdir(parents=True)
    (root / "agents" / "bob").mkdir(parents=True)
    (root / "config.yaml").write_text(yaml.safe_dump(lead, sort_keys=False))
    (root / "agents" / "alice" / "config.yaml").write_text(
        yaml.safe_dump(_teammate("alice", _ALICE_MODEL), sort_keys=False)
    )
    (root / "agents" / "bob" / "config.yaml").write_text(
        yaml.safe_dump(_teammate("bob", _BOB_MODEL), sort_keys=False)
    )
    return root


def _register_team_bundle(client: httpx.Client, bundle_dir: Path, *, name: str) -> str:
    """Tar *bundle_dir* and register it, returning the agent name.

    Mirrors ``register_dir_agent_with_mock_llm`` but keeps the bundle's own
    ``executor`` blocks (each agent needs its OWN mock model key, which that
    helper's single-model stamp would collapse).
    """
    spec = yaml.safe_load((bundle_dir / "config.yaml").read_text())
    spec["name"] = name
    (bundle_dir / "config.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))

    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for entry in sorted(bundle_dir.rglob("*")):
                if entry.is_file():
                    tar.add(str(entry), arcname=str(entry.relative_to(bundle_dir)))
        bundle = buf.getvalue()

    resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    if resp.status_code not in (200, 201, 409):
        raise RuntimeError(f"team bundle register failed: {resp.status_code} {resp.text[:500]}")
    return name


def _items_blob(client: httpx.Client, session_id: str) -> str:
    """All of a session's items as one JSON string, for substring assertions."""
    resp = client.get(f"/v1/sessions/{session_id}/items", params={"limit": 200, "order": "asc"})
    resp.raise_for_status()
    return json.dumps(resp.json().get("data", []))


def _wait_for_child(client: httpx.Client, parent_id: str, title: str) -> str:
    """Return the child session id whose title matches *title*."""
    deadline = time.monotonic() + _CHILD_WAIT_S
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{parent_id}/child_sessions")
        if resp.status_code == 200:
            for child in resp.json().get("data", resp.json()) or []:
                if isinstance(child, dict) and child.get("title") == title:
                    return str(child["id"])
        time.sleep(_POLL_INTERVAL_S)
    raise AssertionError(f"child {title!r} never appeared under {parent_id}")


def _drive_turn(client: httpx.Client, session_id: str, content: str) -> None:
    """Send one user message and wait for the turn to go terminal."""
    response_id = send_user_message_to_session(client, session_id=session_id, content=content)
    poll_session_until_terminal(
        client, session_id=session_id, response_id=response_id, timeout=180
    )


def _spawn_team(
    client: httpx.Client,
    *,
    mock_url: str,
    runner_id: str,
    tmp_path: Path,
    team: bool = True,
    peer_send_cap: int | None = None,
) -> tuple[str, str, str]:
    """Register a team, spawn alice + bob, return ``(lead, alice, bob)`` ids."""
    configure_mock_llm(mock_url, [{"text": "alice ready"}], key=_ALICE_MODEL)
    configure_mock_llm(mock_url, [{"text": "bob ready"}], key=_BOB_MODEL)
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    _named_send_call("alice", "alice", "You are alice.", call_id="c_a"),
                    _named_send_call("bob", "bob", "You are bob.", call_id="c_b"),
                ]
            },
            {"text": "team spawned"},
        ],
        key=_COORD_MODEL,
    )

    name = f"team-demo-{uuid.uuid4().hex[:6]}"
    bundle = _write_team_bundle(
        tmp_path / name,
        f"{mock_url}/v1",
        team=team,
        peer_send_cap=peer_send_cap,
    )
    agent_name = _register_team_bundle(client, bundle, name=name)
    lead_id = create_runner_bound_session(client, agent_name=agent_name, runner_id=runner_id)
    _drive_turn(client, lead_id, "Spawn alice and bob.")

    alice_id = _wait_for_child(client, lead_id, "alice:alice")
    bob_id = _wait_for_child(client, lead_id, "bob:bob")
    return lead_id, alice_id, bob_id


@pytest.fixture
def team(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
    tmp_path: Path,
) -> Any:
    """Factory yielding ``(lead_id, alice_id, bob_id)`` for a fresh team."""
    if mock_llm_server_url is None:
        pytest.skip("team peer-messaging coverage is mock-LLM only")

    def _make(*, team: bool = True, peer_send_cap: int | None = None) -> tuple[str, str, str]:
        return _spawn_team(
            http_client,
            mock_url=mock_llm_server_url,
            runner_id=live_runner_id,
            tmp_path=tmp_path,
            team=team,
            peer_send_cap=peer_send_cap,
        )

    return _make


def test_peer_reply_lands_in_sender_inbox_not_the_lead(
    http_client: httpx.Client,
    mock_llm_server_url: str | None,
    team: Any,
) -> None:
    """Bob's reply reaches ALICE, and never the lead.

    The headline routing claim: ``awaiter_session_id`` sends the completion to
    the sender. A regression that delivered to the structural parent would
    strand alice (she never wakes) and leak bob's answer into the lead's
    transcript.
    """
    lead_id, alice_id, bob_id = team()

    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"My favorite is a trie. {_BOB_SENTINEL}"}],
        key=_BOB_MODEL,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"tool_calls": [_peer_send_call(bob_id, _ALICE_QUESTION, call_id="c_send")]},
            {"tool_calls": [_tool_call("sys_read_inbox", {}, call_id="c_inbox")]},
            {"text": "reported bob's answer"},
        ],
        key=_ALICE_MODEL,
    )

    _drive_turn(http_client, alice_id, f"Message bob: {_ALICE_QUESTION}")

    # The completion lands via the inbox wake, which is asynchronous to alice's
    # own turn going terminal.
    deadline = time.monotonic() + _SENTINEL_WAIT_S
    alice_blob = ""
    while time.monotonic() < deadline:
        alice_blob = _items_blob(http_client, alice_id)
        if _BOB_SENTINEL in alice_blob:
            break
        time.sleep(_POLL_INTERVAL_S)

    assert _BOB_SENTINEL in alice_blob, f"bob's reply never reached alice: {alice_blob[-400:]}"
    assert _BOB_SENTINEL not in _items_blob(http_client, lead_id), (
        "bob's reply leaked into the lead's transcript"
    )


def test_peer_send_is_refused_when_the_team_flag_is_off(
    http_client: httpx.Client,
    mock_llm_server_url: str | None,
    team: Any,
) -> None:
    """Without the top-level ``team`` opt-in, a by-id send stays child-only.

    Proves the flag actually gates the peer path end-to-end: same tree, same
    tool call, only ``team: false`` differs, and the send must be refused
    without bob ever running.
    """
    _lead_id, alice_id, bob_id = team(team=False)

    configure_mock_llm(
        mock_llm_server_url,
        [
            {"tool_calls": [_peer_send_call(bob_id, _ALICE_QUESTION, call_id="c_send")]},
            {"text": "peer send attempted"},
        ],
        key=_ALICE_MODEL,
    )
    _drive_turn(http_client, alice_id, "Try to message bob.")

    alice_blob = _items_blob(http_client, alice_id)
    refused = "session_out_of_tree" in alice_blob or "authorized team peer" in alice_blob
    assert refused, f"peer send was not refused: {alice_blob[-400:]}"
    assert _BOB_SENTINEL not in alice_blob, "bob ran despite the refusal"


def test_teammate_discovers_peer_via_session_list(
    http_client: httpx.Client,
    mock_llm_server_url: str | None,
    team: Any,
) -> None:
    """``sys_session_list`` surfaces a sibling's id, which a peer send needs.

    Discovery is a precondition of the feature: a teammate cannot address a
    peer it cannot enumerate.
    """
    _lead_id, alice_id, bob_id = team()

    configure_mock_llm(
        mock_llm_server_url,
        [
            {"tool_calls": [_tool_call("sys_session_list", {}, call_id="c_list")]},
            {"text": "listed my team"},
        ],
        key=_ALICE_MODEL,
    )
    _drive_turn(http_client, alice_id, "List your team.")

    assert bob_id in _items_blob(http_client, alice_id), "bob's id absent from sys_session_list"


def test_peer_send_leaves_structural_parent_unchanged(
    http_client: httpx.Client,
    mock_llm_server_url: str | None,
    team: Any,
) -> None:
    """A peer send does not re-parent the target.

    Completion routing moves to the sender, but the tree — and with it the SSE
    fan-out that renders bob's live status in the lead's Agents rail — must
    keep pointing at bob's real parent.
    """
    lead_id, alice_id, bob_id = team()

    configure_mock_llm(mock_llm_server_url, [{"text": f"ok {_BOB_SENTINEL}"}], key=_BOB_MODEL)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"tool_calls": [_peer_send_call(bob_id, "ping", call_id="c_send")]},
            {"text": "sent"},
        ],
        key=_ALICE_MODEL,
    )
    _drive_turn(http_client, alice_id, "Ping bob.")

    resp = http_client.get(
        f"/v1/sessions/{bob_id}",
        params={"include_items": "false", "include_liveness": "false"},
    )
    resp.raise_for_status()
    snapshot = resp.json()
    assert snapshot["parent_session_id"] == lead_id, (
        f"peer send re-parented bob to {snapshot['parent_session_id']!r}"
    )
    assert snapshot["team"] is True, "bob's team flag did not resolve from the shared bundle"


def test_peer_send_wave_reaches_the_dispatch_path(
    http_client: httpx.Client,
    mock_llm_server_url: str | None,
    team: Any,
) -> None:
    """Several peer sends in one turn all reach the peer-send tool.

    Substrate check only. ``team_bounds``' per-turn cap CANNOT trip here: the
    server rebuilds the policy engine per ``tools/call``
    (``_build_policy_engine_from_spec``), so the stateful counter resets every
    call. That is a pre-existing property of this path — ``spawn_bounds``
    behaves identically — so this asserts dispatch, not the bound. The counting
    logic itself is covered by
    ``tests/inner/nessie/test_policies.py::test_team_bounds_*``.
    """
    _lead_id, alice_id, bob_id = team(peer_send_cap=2)

    configure_mock_llm(mock_llm_server_url, [{"text": f"ok {_BOB_SENTINEL}"}], key=_BOB_MODEL)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    _peer_send_call(bob_id, "m1", call_id="c1"),
                    _peer_send_call(bob_id, "m2", call_id="c2"),
                    _peer_send_call(bob_id, "m3", call_id="c3"),
                ]
            },
            {"text": "three sends attempted"},
        ],
        key=_ALICE_MODEL,
    )
    _drive_turn(http_client, alice_id, "Send bob three messages.")

    alice_blob = _items_blob(http_client, alice_id)
    # A peer send returns a running handle, or the single-turn-per-session
    # guard message — either proves the wave reached the tool.
    assert "running" in alice_blob or "session" in alice_blob, (
        f"peer-send wave never reached dispatch: {alice_blob[-400:]}"
    )
