"""E2E journey: ``sys_session_create`` children must be listable.

Drives the reported orchestrator journey against a live server + runner
(mock LLM scripting the parent's turns): the parent creates two child
sessions — one with a free-form title, one whose title contains a colon —
verifies both are live and readable by id, then calls ``sys_session_list``.

One test per reported defect, each pinning the expected behavior:

* the free-form-titled child appears in the ``sub_agents`` view;
* both children appear in the global ``sessions`` view;
* the colon-titled child's ``agent`` field reports its real agent binding,
  not the prefix of a user-chosen title.

Usage::

    pytest tests/e2e/test_session_create_child_listing_e2e.py -v
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    lookup_agent_id,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)

_FREEFORM_TITLE = "omnigent dropout test"
_COLON_TITLE = "probe:colon-title"


def _function_call_results(
    client: httpx.Client,
    session_id: str,
    tool_name: str,
) -> list[tuple[dict[str, Any], str]]:
    """Return ``(arguments, output)`` per *tool_name* call, in conversation order."""
    resp = client.get(f"/v1/sessions/{session_id}/items?limit=200")
    resp.raise_for_status()
    items = resp.json()["data"]

    calls: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in items:
        data = item.get("data") or {}
        if item.get("type") != "function_call":
            continue
        name = item.get("name") or data.get("name")
        call_id = item.get("call_id") or data.get("call_id")
        if name != tool_name or not call_id:
            continue
        raw_args = item.get("arguments") or data.get("arguments") or "{}"
        calls[call_id] = raw_args if isinstance(raw_args, dict) else json.loads(raw_args)
        order.append(call_id)

    outputs: dict[str, str] = {}
    for item in items:
        data = item.get("data") or {}
        if item.get("type") != "function_call_output":
            continue
        call_id = item.get("call_id") or data.get("call_id")
        if call_id in calls:
            outputs[call_id] = str(item.get("output") or data.get("output") or "")
    return [(calls[cid], outputs.get(cid, "")) for cid in order]


@pytest.fixture(scope="module")
def listing_journey(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> dict[str, Any]:
    """Run the create→create→list journey once and return its artifacts.

    Everything asserted here holds on the running build even with the bug
    present (the turn completes, both children are created, live, and
    readable by id, and the listing call returns both views with the parent
    visible globally) — a failure in this fixture is an environment problem,
    not the reported defect.
    """
    model = f"mock-sess-list-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)

    agent_name = register_inline_agent(
        http_client,
        name=f"sess-list-parent-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt=(
            "You are an orchestrator. When asked, create child sessions "
            "with sys_session_create and list sessions with "
            "sys_session_list."
        ),
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={"spawn": True},
    )
    agent_id = lookup_agent_id(http_client, agent_name)

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_create_freeform",
                        "name": "sys_session_create",
                        "arguments": json.dumps({"agent_id": agent_id, "title": _FREEFORM_TITLE}),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_create_colon",
                        "name": "sys_session_create",
                        "arguments": json.dumps({"agent_id": agent_id, "title": _COLON_TITLE}),
                    }
                ]
            },
            {
                "tool_calls": [
                    {
                        "call_id": "call_list_children",
                        "name": "sys_session_list",
                        "arguments": json.dumps({}),
                    }
                ]
            },
            {"text": "Created both children and listed sessions."},
        ],
        key=model,
    )

    parent_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=parent_id,
        content=(
            "Create two child sessions with sys_session_create — one titled "
            f"'{_FREEFORM_TITLE}' and one titled '{_COLON_TITLE}' — then "
            "call sys_session_list and report what it returns."
        ),
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=parent_id,
        response_id=response_id,
        timeout=180,
    )
    assert body["status"] == "completed", (
        f"journey turn did not complete: status={body.get('status')!r}, "
        f"error={body.get('error')!r}"
    )

    creates = _function_call_results(http_client, parent_id, "sys_session_create")
    assert len(creates) == 2, f"expected 2 sys_session_create calls, saw {len(creates)}"
    children: dict[str, str] = {}
    for args, output in creates:
        handle = json.loads(output)
        assert handle.get("kind") == "sub_agent", (
            f"sys_session_create did not return a child handle: {output!r}"
        )
        children[str(args.get("title"))] = handle["conversation_id"]
    freeform_child_id = children[_FREEFORM_TITLE]
    colon_child_id = children[_COLON_TITLE]

    for child_id in (freeform_child_id, colon_child_id):
        snap = http_client.get(f"/v1/sessions/{child_id}")
        assert snap.status_code == 200, (
            f"child {child_id} is not readable by id: HTTP {snap.status_code}"
        )
        assert snap.json().get("parent_session_id") == parent_id, (
            f"child {child_id} does not report the parent session"
        )

    lists = _function_call_results(http_client, parent_id, "sys_session_list")
    assert len(lists) == 1, f"expected 1 sys_session_list call, saw {len(lists)}"
    listing = json.loads(lists[0][1])
    assert isinstance(listing.get("sub_agents"), list), f"malformed listing: {listing!r}"
    assert isinstance(listing.get("sessions"), list), f"malformed listing: {listing!r}"
    global_ids = {entry.get("session_id") for entry in listing["sessions"]}
    assert parent_id in global_ids, (
        "global 'sessions' view is broken beyond the reported defects — the "
        f"parent session itself is missing: {listing['sessions']!r}"
    )

    return {
        "agent_name": agent_name,
        "parent_id": parent_id,
        "freeform_child_id": freeform_child_id,
        "colon_child_id": colon_child_id,
        "listing": listing,
    }


def test_freeform_title_child_listed_in_sub_agents(
    listing_journey: dict[str, Any],
) -> None:
    listing = listing_journey["listing"]
    sub_ids = {entry.get("conversation_id") for entry in listing["sub_agents"]}
    assert listing_journey["freeform_child_id"] in sub_ids, (
        "sys_session_create child with a free-form title is missing from the "
        f"sub_agents view; entries: {listing['sub_agents']!r}"
    )


def test_children_listed_in_global_sessions_view(
    listing_journey: dict[str, Any],
) -> None:
    listing = listing_journey["listing"]
    global_ids = {entry.get("session_id") for entry in listing["sessions"]}
    missing = {
        title: child_id
        for title, child_id in (
            (_FREEFORM_TITLE, listing_journey["freeform_child_id"]),
            (_COLON_TITLE, listing_journey["colon_child_id"]),
        )
        if child_id not in global_ids
    }
    assert not missing, (
        f"sys_session_create children are missing from the global 'sessions' view: {missing!r}"
    )


def test_colon_title_child_agent_is_real_binding(
    listing_journey: dict[str, Any],
) -> None:
    listing = listing_journey["listing"]
    entry = next(
        (
            e
            for e in listing["sub_agents"]
            if e.get("conversation_id") == listing_journey["colon_child_id"]
        ),
        None,
    )
    assert entry is not None, (
        f"colon-titled child is missing from the sub_agents view: {listing['sub_agents']!r}"
    )
    assert entry.get("agent") == listing_journey["agent_name"], (
        "sub_agents 'agent' must report the child's real agent binding "
        f"({listing_journey['agent_name']!r}), not the user title's prefix: "
        f"{entry!r}"
    )
