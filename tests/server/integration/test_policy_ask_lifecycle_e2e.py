"""
End-to-end integration test for the ASK policy approve/refuse lifecycle.

Exercises the full user journey:

1. Create a session.
2. Attach an ASK policy via ``POST /v1/sessions/{session_id}/policies``
   using the registered ``ask_on_os_tools`` handler.
3. Trigger policy evaluation via ``POST /v1/sessions/{id}/policies/evaluate``
   with a Bash tool call.
4. Observe the parked elicitation in the session snapshot.
5. Resolve with accept (approve) or decline (refuse) via
   ``POST /v1/sessions/{id}/elicitations/{eid}/resolve``.
6. Assert the evaluate endpoint returns ``POLICY_ACTION_ALLOW`` (approve)
   or ``POLICY_ACTION_DENY`` (refuse).

Uses the shared ``runtime_init`` and ``mock_llm`` fixtures from
``tests/server/conftest.py``, plus the shared policy-enabled app
fixture that wires in a :class:`SqlAlchemyPolicyStore` so the session
policy CRUD routes are mounted and the evaluate endpoint picks up
session-attached policies.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from omnigent.runtime import pending_elicitations, session_stream
from omnigent.runtime.policies.builder import invalidate_default_policy_specs_cache
from tests.server.helpers import create_session_for_agent as _create_session
from tests.server.helpers import create_test_agent
from tests.server.helpers import policy_tool_call_request as _tool_call_request

pytestmark = pytest.mark.asyncio


# ── Fixtures ────────────────────────────────────────────────


@pytest.fixture()
def client(policy_client: httpx.AsyncClient) -> httpx.AsyncClient:
    """Use the shared policy-enabled runtime client for this module."""
    return policy_client


# ── Helpers ─────────────────────────────────────────────────


async def _attach_ask_policy(
    client: httpx.AsyncClient,
    session_id: str,
) -> str:
    """
    Attach the registered ``ask_on_os_tools`` ASK policy to a session.

    This builtin policy ASKs for approval on any file or shell tool
    call (Bash, Read, Write, Edit, Glob, Grep). Using a registered
    handler avoids the policy registry allowlist rejection.

    :param client: Test HTTP client.
    :param session_id: Session to attach the policy to.
    :returns: The created policy id.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/policies",
        json={
            "name": "test_ask_policy",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
    )
    assert resp.status_code == 200, f"attach policy failed: {resp.status_code} {resp.text}"
    body = resp.json()
    assert body["name"] == "test_ask_policy"
    return body["id"]


async def _attach_global_ask_policy(client: httpx.AsyncClient) -> str:
    """Create the same ASK policy as a UI-managed global policy."""
    resp = await client.post(
        "/v1/policies",
        json={
            "name": "test_global_ask_policy",
            "type": "python",
            "handler": "omnigent.policies.builtins.safety.ask_on_os_tools",
        },
    )
    assert resp.status_code == 200, f"attach policy failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


async def _drain_elicitation_id(
    session_id: str,
    *,
    timeout_s: float = 5.0,
) -> str:
    """
    Block on the session SSE stream until a
    ``response.elicitation_request`` arrives; return its id.

    :param session_id: Session to subscribe to.
    :param timeout_s: Max seconds to wait before failing the test.
    :returns: The published ``elicitation_id``.
    """
    async with asyncio.timeout(timeout_s):
        async for event in session_stream.subscribe(session_id):
            if event.get("type") == "response.elicitation_request":
                eid = event.get("elicitation_id")
                assert isinstance(eid, str) and eid, f"missing id: {event!r}"
                return eid
    raise AssertionError("subscribe loop ended without an elicitation event")


# ── Tests ───────────────────────────────────────────────────


@pytest.mark.parametrize("scope", ["session", "global"])
async def test_ask_policy_approve_flow(
    client: httpx.AsyncClient,
    scope: str,
) -> None:
    """
    Attach a session or global ASK policy, evaluate, approve → ALLOW.

    Full journey: create session → attach ``ask_on_os_tools`` policy
    → trigger evaluate with a Bash tool call → observe pending
    elicitation in the session snapshot → resolve with accept →
    evaluate returns ``POLICY_ACTION_ALLOW``. Proves both policy scopes
    survive the native-evaluation fast path, park a real server-side Future, and the
    URL-based resolve wakes it with the correct verdict.
    """
    agent = await create_test_agent(client, "test-ask-approve")
    session_id = await _create_session(client, agent["id"])
    if scope == "global":
        await _attach_global_ask_policy(client)
    else:
        await _attach_ask_policy(client, session_id)

    drain = asyncio.create_task(_drain_elicitation_id(session_id))
    evaluate = None
    try:
        await asyncio.sleep(0.05)

        # The evaluate POST parks until the verdict arrives.
        evaluate = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/policies/evaluate",
                json=_tool_call_request("Bash"),
            )
        )

        # Learn the elicitation id from the stream.
        elicitation_id = await drain

        # Verify the session snapshot shows a pending elicitation.
        snapshot = await client.get(f"/v1/sessions/{session_id}")
        assert snapshot.status_code == 200, snapshot.text
        pending = snapshot.json().get("pending_elicitations", [])
        pending_ids = [p["elicitation_id"] for p in pending]
        assert elicitation_id in pending_ids, (
            f"elicitation {elicitation_id} not in snapshot pending list: {pending_ids}"
        )

        # Approve.
        verdict = await client.post(
            f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json={"action": "accept"},
        )
        assert verdict.status_code == 202, verdict.text

        # The parked evaluate call should now return ALLOW.
        resp = await evaluate
        assert resp.status_code == 200, resp.text
        assert resp.json()["result"] == "POLICY_ACTION_ALLOW"
    finally:
        for task in [drain, evaluate]:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        pending_elicitations.reset_for_tests()
        if scope == "global":
            invalidate_default_policy_specs_cache()


async def test_ask_policy_refuse_flow(
    client: httpx.AsyncClient,
) -> None:
    """
    Attach ASK policy, evaluate, refuse → DENY.

    Same setup as the approve flow but resolves with ``decline``.
    The evaluate endpoint must collapse the ASK to
    ``POLICY_ACTION_DENY`` — fail-closed. Proves the session-attached
    ASK policy's refuse path terminates correctly and the DENY
    sentinel propagates.
    """
    agent = await create_test_agent(client, "test-ask-refuse")
    session_id = await _create_session(client, agent["id"])
    await _attach_ask_policy(client, session_id)

    drain = asyncio.create_task(_drain_elicitation_id(session_id))
    evaluate = None
    try:
        await asyncio.sleep(0.05)

        evaluate = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/policies/evaluate",
                json=_tool_call_request("Bash"),
            )
        )

        elicitation_id = await drain

        # Refuse.
        verdict = await client.post(
            f"/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve",
            json={"action": "decline"},
        )
        assert verdict.status_code == 202, verdict.text

        # The parked evaluate call should now return DENY.
        resp = await evaluate
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["result"] == "POLICY_ACTION_DENY"
    finally:
        for task in [drain, evaluate]:
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        pending_elicitations.reset_for_tests()
