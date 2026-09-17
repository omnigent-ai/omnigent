"""
E2E reproduction for the ``spawn_bounds`` sub-agent dispatch gap.

A bundle declares a function-type ``spawn_bounds`` guardrail capping
worker dispatches at ``max_dispatches_per_turn: 1``. The parent
orchestrator's scripted turn dispatches THREE workers via
``sys_session_send`` within that single turn, so dispatches #2 and #3
must be DENIED (``Exceeded 1 worker dispatches this turn``) and at most
one child session may spawn.

The defect: ``spawn_bounds`` keeps its per-turn dispatch counter in an
in-memory closure, but the enforcement point that sees the dispatch (the
server's ``/mcp`` proxy) rebuilds its policy engine — and with it that
closure — on every tool call, so the counter reads 1 on every dispatch
and the configured fan-out bound is decorative. The runner, whose state
does live across the calls of a turn, evaluated no TOOL_CALL policies
for the built-in dispatch tools at all. On the buggy build all three
dispatches succeed, three child sessions spawn, and this test FAILS.

Topology mirrors tests/e2e/test_subagent_tool_limit_e2e.py: real server +
real runner, mock LLM scripted per-agent (parent and child each route to
their own mock model queue via a per-agent ``executor.auth.base_url``).
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

from tests.e2e.conftest import (
    OMNIGENT_INTERNAL_WS_ORIGIN,
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    reset_mock_llm,
    send_user_message_to_session,
    set_fallback_mock_llm,
)
from tests.e2e.helpers import POLL_INTERVAL_S

# Fixture bundle: parent orchestrator with a spawn_bounds guardrail plus a
# trivial agents/worker child. The dispatch cap is stamped per-test.
_BUNDLE_DIR = (
    Path(__file__).resolve().parents[1] / "resources" / "agents" / "spawn-bounds-dispatch"
)

# Per-child mock-LLM routing (each agent on its own mock model + auth
# base_url) requires a server >= 0.3.0 — same constraint as
# tests/e2e/test_subagent_tool_limit_e2e.py.
pytestmark = [
    pytest.mark.min_server_version("0.3.0"),
    # One dispatch turn with three spawn rounds plus child turns and
    # parent auto-wakes, so allow headroom under signal-based timeout.
    pytest.mark.timeout(600, method="signal"),
]

# Number of workers the scripted parent tries to dispatch in ONE turn.
_DISPATCH_ATTEMPTS = 3

# Per-turn dispatch cap stamped into the parent's spawn_bounds guardrail.
_DISPATCH_CAP = 1

# Sentinels emitted by the scripted mock turns so the test can wait for
# each stage deterministically.
_PARENT_TURN_DONE = "PARENT_DISPATCH_TURN_DONE"
_WORKER_DONE = "WORKER_SPAWN_TEST_DONE"


def _register_spawn_bounds_bundle(
    client: httpx.Client,
    *,
    name: str,
    parent_model: str,
    child_model: str,
    max_dispatches_per_turn: int,
    mock_llm_base_url: str,
) -> str:
    """
    Upload the parent+worker bundle with per-agent models, auth and cap.

    Stamps ``executor.model`` + an ``executor.auth`` api-key block onto BOTH
    the parent config.yaml and the child agents/worker/config.yaml so each
    agent routes to its own mock-LLM queue, and stamps the parent's
    ``spawn_bounds`` ``max_dispatches_per_turn`` cap.

    :param client: HTTP client pointed at the live server.
    :param name: Unique agent name for this registration.
    :param parent_model: Mock model key for the parent's response queue.
    :param child_model: Mock model key for the worker's response queue.
    :param max_dispatches_per_turn: Per-turn dispatch cap for the parent.
    :param mock_llm_base_url: Mock server base URL including ``/v1``.
    :returns: The registered agent name.
    """
    auth = {"type": "api_key", "api_key": "mock-key", "base_url": mock_llm_base_url}

    parent_cfg = yaml.safe_load((_BUNDLE_DIR / "config.yaml").read_text())
    parent_cfg["name"] = name
    parent_cfg.setdefault("executor", {})["model"] = parent_model
    parent_cfg["executor"]["auth"] = auth
    parent_cfg["guardrails"]["policies"]["spawn_bounds"]["function"]["arguments"][
        "max_dispatches_per_turn"
    ] = max_dispatches_per_turn

    child_cfg = yaml.safe_load((_BUNDLE_DIR / "agents" / "worker" / "config.yaml").read_text())
    child_cfg.setdefault("executor", {})["model"] = child_model
    child_cfg["executor"]["auth"] = auth

    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:

            def _add_yaml(arcname: str, config: dict[str, Any]) -> None:
                data = yaml.dump(config).encode()
                info = tarfile.TarInfo(arcname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

            _add_yaml("config.yaml", parent_cfg)
            _add_yaml("agents/worker/config.yaml", child_cfg)
        bundle = buf.getvalue()

    resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    if resp.status_code not in (200, 201, 409):
        raise RuntimeError(f"bundle register failed: {resp.status_code} {resp.text[:500]}")
    return name


def _configure_parent_and_child_mocks(
    mock_llm_server_url: str,
    *,
    parent_model: str,
    child_model: str,
) -> None:
    """
    Script both mock queues: the parent dispatches three workers in one
    turn (one ``sys_session_send`` round per worker), the workers each
    acknowledge and finish.

    Fallback texts absorb the parent's inbox auto-wake continuations
    (one per child that actually ran) so queue exhaustion can't fail a
    wake turn regardless of how many dispatches were allowed through.

    :param mock_llm_server_url: Mock server base URL.
    :param parent_model: Parent queue key.
    :param child_model: Worker queue key.
    """
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call_dispatch_worker_{i}",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "worker",
                                "title": f"fanout-{i}",
                                "args": f"Acknowledge task {i} and finish.",
                            }
                        ),
                    },
                ],
            }
            for i in range(1, _DISPATCH_ATTEMPTS + 1)
        ]
        + [{"text": _PARENT_TURN_DONE}],
        key=parent_model,
    )
    set_fallback_mock_llm(mock_llm_server_url, parent_model, "PARENT_WAKE_DONE")
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _WORKER_DONE} for _ in range(_DISPATCH_ATTEMPTS)],
        key=child_model,
    )
    set_fallback_mock_llm(mock_llm_server_url, child_model, _WORKER_DONE)


def _dispatch_outputs(items: list[dict[str, Any]]) -> list[str]:
    """
    Extract the ``sys_session_send`` tool outputs from session items, in order.

    :param items: Parent session conversation items.
    :returns: Output payloads of ``function_call_output`` items whose call_id
        matches a ``sys_session_send`` function_call.
    """
    flattened: list[dict[str, Any]] = []
    for item in items:
        # Session items carry the payload under "data"; keep the item type.
        data = item.get("data") or {}
        flattened.append({"type": item.get("type"), **data})
    dispatch_call_ids = {
        p.get("call_id")
        for p in flattened
        if p.get("type") == "function_call" and p.get("name") == "sys_session_send"
    }
    return [
        str(p.get("output", ""))
        for p in flattened
        if p.get("type") == "function_call_output" and p.get("call_id") in dispatch_call_ids
    ]


def _is_denial(output: str) -> bool:
    """Return whether a tool output is a policy denial payload."""
    return "Denied by policy" in output or "Exceeded" in output


def _count_children(
    http_client: httpx.Client,
    *,
    parent_session_id: str,
) -> int:
    """
    Count the sub-agent child sessions spawned under *parent_session_id*.

    :param http_client: HTTP client pointed at the live server.
    :param parent_session_id: The dispatching parent session id.
    :returns: Number of child sessions whose parent is the given session.
    """
    resp = http_client.get("/v1/sessions", params={"kind": "sub_agent", "limit": 1000})
    resp.raise_for_status()
    count = 0
    for item in resp.json().get("data", []):
        snap = http_client.get(f"/v1/sessions/{item['id']}")
        snap.raise_for_status()
        if snap.json().get("parent_session_id") == parent_session_id:
            count += 1
    return count


def _wait_for_children(
    http_client: httpx.Client,
    *,
    parent_session_id: str,
    minimum: int,
    timeout: float = 120.0,
) -> int:
    """
    Poll until at least *minimum* children exist, then return the count.

    ``sys_session_send`` spawns children asynchronously, so give the
    allowed dispatch time to materialize before counting.

    :param http_client: HTTP client pointed at the live server.
    :param parent_session_id: The dispatching parent session id.
    :param minimum: Stop polling once this many children are visible.
    :param timeout: Max seconds to wait for *minimum* children.
    :returns: The observed child count (may exceed *minimum*).
    """
    deadline = time.monotonic() + timeout
    count = 0
    while time.monotonic() < deadline:
        count = _count_children(http_client, parent_session_id=parent_session_id)
        if count >= minimum:
            return count
        time.sleep(POLL_INTERVAL_S)
    return count


def test_spawn_bounds_denies_dispatches_beyond_per_turn_cap(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> None:
    """
    A parent with ``spawn_bounds max_dispatches_per_turn: 1`` that tries
    three ``sys_session_send`` dispatches in one turn must have the
    second and third DENIED.

    The scripted parent turn attempts three sequential worker dispatches,
    so:

    - at most 1 dispatch may execute successfully (a real spawn), and
    - the excess dispatches' outputs must be the spawn_bounds denial
      (``Exceeded 1 worker dispatches this turn``), and
    - at most 1 child session may exist under the parent.

    On the buggy build no enforcement point holds the dispatch counter
    across the turn's calls, all three dispatches succeed, three
    children spawn, and this test FAILS.
    """
    uid = uuid.uuid4().hex[:6]
    parent_model = f"mock-spawnbounds-parent-{uid}"
    child_model = f"mock-spawnbounds-child-{uid}"

    reset_mock_llm(mock_llm_server_url)
    assert mock_llm_server_url is not None
    agent_name = _register_spawn_bounds_bundle(
        http_client,
        name=f"spawn-bounds-dispatch-{uid}",
        parent_model=parent_model,
        child_model=child_model,
        max_dispatches_per_turn=_DISPATCH_CAP,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    _configure_parent_and_child_mocks(
        mock_llm_server_url,
        parent_model=parent_model,
        child_model=child_model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="RUN: dispatch the worker sub-agent for three tasks.",
    )
    result = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=240
    )
    assert result["status"] == "completed", (
        f"parent dispatch turn did not complete cleanly: {result.get('error')!r}"
    )

    snap = http_client.get(f"/v1/sessions/{session_id}")
    snap.raise_for_status()
    outputs = _dispatch_outputs(snap.json().get("items", []))
    assert len(outputs) == _DISPATCH_ATTEMPTS, (
        f"expected {_DISPATCH_ATTEMPTS} sys_session_send outputs in the parent turn, "
        f"got {len(outputs)}: {outputs!r}"
    )

    successful = [o for o in outputs if not _is_denial(o)]
    denied = [o for o in outputs if _is_denial(o)]

    assert len(successful) <= _DISPATCH_CAP, (
        f"parent executed {len(successful)} worker dispatches in one turn but "
        f"spawn_bounds caps it at {_DISPATCH_CAP} — the guardrail was not "
        f"enforced on sys_session_send (outputs: {outputs!r})"
    )
    assert any(f"Exceeded {_DISPATCH_CAP} worker dispatches this turn" in o for o in denied), (
        f"dispatches beyond the cap should have been denied with "
        f"'Exceeded {_DISPATCH_CAP} worker dispatches this turn'; got outputs: {outputs!r}"
    )

    # The allowed dispatch spawns asynchronously; wait for it, then assert
    # the denied ones never materialized as child sessions.
    children = _wait_for_children(
        http_client,
        parent_session_id=session_id,
        minimum=_DISPATCH_CAP,
    )
    assert children <= _DISPATCH_CAP, (
        f"{children} child sessions spawned under the parent but "
        f"spawn_bounds allows only {_DISPATCH_CAP} dispatch per turn"
    )
