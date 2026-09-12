"""E2E regression tests for the silent post-compaction sub-agent stall.

A sub-agent that hits repeated context compactions during its
read-only phase silently stalls: the transcript tail freezes at the
compaction summary while ``sys_session_get_info`` /
``GET /v1/sessions/{id}`` keep reporting ``status: "running"``,
``runner_online: true`` and zero pending elicitations. An orchestrator
polling session *metadata* therefore cannot distinguish a
compacted-but-parked leaf from one making healthy progress — the stall
is only visible by scraping the transcript tail and the filesystem.

The reconstructed journey: orchestrator dispatches a coding sub-agent
with a large read-phase prompt → the sub-agent triggers two
back-to-back compactions → the transcript stops advancing → the
orchestrator polls the session snapshot → every metadata signal looks
healthy, so it waits indefinitely.

These tests drive the same wire path an orchestrator uses (the
``sys_session_get_info`` / ``sys_session_list`` tools proxy
``GET /v1/sessions/{id}``): seed a real turn on a runner-bound
session, persist two back-to-back ``compaction`` events exactly as the
runner does when the harness compacts, then assert the session
snapshot's metadata (NOT the transcript items) lets a poller detect
the repeated-compaction stall.

Runs entirely against the mock LLM server — no real API key needed::

    pytest tests/e2e/test_compaction_stall_metadata.py -v
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)


def _register_mock_stall_agent(
    client: httpx.Client,
    mock_llm_server_url: str,
) -> tuple[str, str]:
    """
    Register an inline mock-LLM agent for stall-detection tests.

    :param client: HTTP client pointed at the live server.
    :param mock_llm_server_url: Mock LLM server base URL.
    :returns: ``(agent_name, model)`` — the model doubles as the
        keyed-queue key on the mock LLM server.
    """
    model = f"mock-stall-{uuid.uuid4().hex[:6]}"
    agent_name = register_inline_agent(
        client,
        name=f"stall-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a terse test assistant.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )
    return agent_name, model


def _seed_read_phase_turn(
    client: httpx.Client,
    *,
    agent_name: str,
    runner_id: str,
) -> str:
    """
    Create a runner-bound session and complete one read-phase turn.

    Mirrors the reported journey's setup: the sub-agent has done some
    context-heavy reading (here condensed to a single mock turn) before
    the compactions hit.

    :param client: HTTP client pointed at the live server.
    :param agent_name: Display name of an already-uploaded agent.
    :param runner_id: Registered runner id to bind the session to.
    :returns: The seeded session id, e.g. ``"conv_abc"``.
    """
    session_id = create_runner_bound_session(client, agent_name=agent_name, runner_id=runner_id)
    response_id = send_user_message_to_session(
        client,
        session_id=session_id,
        content="Read the docs tree before producing output. Reply with just OK.",
    )
    body = poll_session_until_terminal(client, session_id=session_id, response_id=response_id)
    assert body["status"] == "completed", f"seed turn failed: {body.get('error')}"
    return session_id


def _snapshot(client: httpx.Client, session_id: str) -> dict[str, Any]:
    """
    Fetch the session snapshot an orchestrator's metadata poll sees.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session/conversation id, e.g. ``"conv_abc"``.
    :returns: The parsed ``GET /v1/sessions/{id}`` body.
    """
    resp = client.get(f"/v1/sessions/{session_id}")
    resp.raise_for_status()
    return resp.json()


def _last_item_id(client: httpx.Client, session_id: str) -> str:
    """
    Return the id of the session's most recent conversation item.

    Used as the ``last_item_id`` compaction boundary, exactly as the
    runner stamps it when the harness compacts.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session/conversation id, e.g. ``"conv_abc"``.
    :returns: The last committed item's id.
    """
    items = _snapshot(client, session_id).get("items", [])
    assert items, "seed turn left no conversation items"
    return str(items[-1]["id"])


def _post_compaction_event(
    client: httpx.Client,
    session_id: str,
    *,
    boundary_item_id: str,
    ordinal: int,
) -> None:
    """
    Persist one compaction item via ``POST /v1/sessions/{id}/events``.

    This is the same wire event the runner emits when the harness
    performs an internal context compaction, so the session ends up in
    exactly the state the stalled sub-agent was observed in.

    :param client: HTTP client pointed at the live server.
    :param session_id: Session/conversation id, e.g. ``"conv_abc"``.
    :param boundary_item_id: Item id the summary compacts up to.
    :param ordinal: 1-based compaction sequence number (for the
        summary text only).
    """
    resp = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "compaction",
            "data": {
                "summary": (
                    f"Compaction {ordinal}: condensed the read-phase context; respond TEXT ONLY."
                ),
                "last_item_id": boundary_item_id,
                "token_count": 64,
            },
        },
    )
    assert resp.status_code == 202, resp.text


def _stalled_session_after_two_compactions(
    client: httpx.Client,
    runner_id: str,
    mock_llm_server_url: str,
) -> dict[str, Any]:
    """
    Drive the journey to the stalled state and return the snapshot.

    Seeds a runner-bound session with one completed turn, persists two
    back-to-back compaction events with no tool output between them
    (the report's stall signature), and returns the metadata snapshot
    an orchestrator polls.

    :param client: HTTP client pointed at the live server.
    :param runner_id: Registered runner id to bind the session to.
    :param mock_llm_server_url: Mock LLM server base URL.
    :returns: The parsed ``GET /v1/sessions/{id}`` body.
    """
    reset_mock_llm(mock_llm_server_url)
    agent_name, model = _register_mock_stall_agent(client, mock_llm_server_url)
    configure_mock_llm(mock_llm_server_url, [{"text": "OK"}], key=model)
    session_id = _seed_read_phase_turn(client, agent_name=agent_name, runner_id=runner_id)
    boundary = _last_item_id(client, session_id)
    _post_compaction_event(client, session_id, boundary_item_id=boundary, ordinal=1)
    _post_compaction_event(client, session_id, boundary_item_id=boundary, ordinal=2)

    snap = _snapshot(client, session_id)
    persisted = [i for i in snap.get("items", []) if i.get("type") == "compaction"]
    assert len(persisted) == 2, (
        "precondition failed: expected exactly two persisted compaction "
        f"items, found {len(persisted)} — the stall state was not reached"
    )
    return snap


# Compaction items land through the post-v0.11 events ingestion contract
# (same boundary as the fork compaction-cursor tests).
@pytest.mark.min_server_version("0.12.0")
def test_repeated_compactions_surface_in_session_metadata(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Two back-to-back compactions must be visible in snapshot metadata.

    The bug: the compaction items ARE persisted in ``items``, but the
    session snapshot's metadata carries no aggregate signal
    (``compaction_count`` / ``last_compaction_at``), so
    ``sys_session_get_info`` — which projects only metadata, never the
    transcript — cannot tell an orchestrator that the leaf just
    compacted twice with no tool calls in between (the stall
    signature from the report).

    **What breaks if wrong:** an orchestrator polling session metadata
    concludes a compaction-stalled sub-agent is progressing and waits
    indefinitely, burning wall-clock and metered-model cost with no
    deliverable.
    """
    snap = _stalled_session_after_two_compactions(http_client, live_runner_id, mock_llm_server_url)

    compaction_count = snap.get("compaction_count")
    assert compaction_count is not None and int(compaction_count) >= 2, (
        "session snapshot metadata exposes no compaction_count: two "
        "back-to-back compactions were persisted to this session's "
        "transcript, but GET /v1/sessions/{id} (the source for "
        "sys_session_get_info) reports no aggregate compaction signal, "
        f"got compaction_count={compaction_count!r}. An orchestrator "
        "cannot detect the repeated-compaction stall without scraping "
        "the transcript."
    )
    assert snap.get("last_compaction_at") is not None, (
        "session snapshot metadata exposes no last_compaction_at "
        "timestamp: without it an orchestrator cannot tell how recently "
        "the session compacted, so 'repeatedly compacting with no "
        "progress' is indistinguishable from healthy running."
    )


@pytest.mark.min_server_version("0.12.0")
def test_compaction_stall_detectable_from_metadata_alone(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    The stalled state must be detectable without reading the transcript.

    The bug: after two compactions with zero tool output between them,
    every metadata field an orchestrator polls looks healthy —
    ``status`` stays a plain ``"running"``/``"idle"`` lifecycle value
    (the Literal has no ``"stalled"`` member), there is no stall flag,
    and no compaction aggregate exists. No watchdog ever flips the
    session to a distinct state.

    The assertion accepts ANY metadata-level stall-observability
    signal (a distinct ``stalled`` status, a stall flag/timestamp, or
    the compaction aggregates) so the guard passes once a fix ships
    any of the report's suggested mechanisms, and fails while none
    exist.
    """
    snap = _stalled_session_after_two_compactions(http_client, live_runner_id, mock_llm_server_url)
    metadata = {k: v for k, v in snap.items() if k != "items"}

    has_stall_signal = (
        metadata.get("compaction_count") is not None
        or metadata.get("last_compaction_at") is not None
        or metadata.get("status") == "stalled"
        or any(
            metadata.get(key) is not None for key in ("stalled", "stall_detected", "stalled_at")
        )
    )
    assert has_stall_signal, (
        "a session parked after two back-to-back compactions is "
        "indistinguishable from a healthy one via metadata alone: "
        f"status={metadata.get('status')!r}, no compaction_count, no "
        "last_compaction_at, no stalled state or stall flag. The stall "
        "is only visible by cross-referencing the transcript tail and "
        "the filesystem, which sys_session_get_info/sys_session_list "
        "consumers never see."
    )
