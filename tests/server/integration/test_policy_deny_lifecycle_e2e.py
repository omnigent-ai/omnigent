"""Integration tests for the DENY policy attach/remove lifecycle.

Exercises the full user journey:

1. Create a session with an agent.
2. Attach a DENY policy via ``POST /v1/sessions/{session_id}/policies``.
3. Send a user message — verify the DENY fires (synchronous inline verdict).
4. Remove the policy via ``DELETE``.
5. Send another message — verify the mock LLM responds normally.
6. Verify the policy is gone from the list endpoint.

Also covers phase-scoping: a DENY policy attached on ``tool_call`` phase
only must not block ``input`` (REQUEST) phase messages.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

from tests.server.conftest import ControllableMockClient
from tests.server.helpers import create_session_for_agent as _create_session
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture()
async def policy_client(
    policy_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[httpx.AsyncClient]:
    """Extend the parent policy client with the DENY scenarios' factory allowlist."""
    # Allow the make_fixed_action_callable factory through the registry
    # allowlist. In production this would be added via policy_modules config.
    # Patch at the use site (the route module imports the function directly).
    from omnigent.server.routes import session_policies as _sp_mod

    _original_is_registered = _sp_mod.is_registered_handler
    monkeypatch.setattr(
        _sp_mod,
        "is_registered_handler",
        lambda handler: (
            handler == "omnigent.policies.function.make_fixed_action_callable"
            or _original_is_registered(handler)
        ),
    )

    yield policy_client


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _attach_deny_policy(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    name: str = "test_deny_policy",
    reason: str = "Blocked by test policy",
    factory_params: dict | None = None,
) -> str:
    """Attach a DENY policy to a session and return its policy id.

    :param client: Test HTTP client.
    :param session_id: Session to attach the policy to.
    :param name: Policy name.
    :param reason: Deny reason.
    :param factory_params: Override factory params if needed.
    :returns: The created policy id.
    """
    params = {"action": "deny", "reason": reason} if factory_params is None else factory_params
    resp = await client.post(
        f"/v1/sessions/{session_id}/policies",
        json={
            "name": name,
            "type": "python",
            "handler": "omnigent.policies.function.make_fixed_action_callable",
            "factory_params": params,
        },
    )
    assert resp.status_code == 200, f"policy create failed: {resp.status_code} {resp.text}"
    body = resp.json()
    assert len(body["id"]) == 32
    return body["id"]


async def _send_user_message(
    client: httpx.AsyncClient,
    session_id: str,
    text: str,
) -> httpx.Response:
    """Post a user message event and return the raw response.

    :param client: Test HTTP client.
    :param session_id: Target session.
    :param text: Message text.
    :returns: The raw HTTP response.
    """
    return await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        },
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


async def test_deny_policy_lifecycle(
    policy_client: httpx.AsyncClient,
    mock_llm: ControllableMockClient,
) -> None:
    """Full DENY lifecycle: attach -> get rejected -> remove -> get through.

    1. Create a session with an agent.
    2. Attach a DENY policy via the session policies endpoint.
    3. Send a user message — verify the DENY fires synchronously.
    4. Remove the policy via DELETE.
    5. Send another message — verify the mock LLM responds normally.
    6. Verify the policy is gone from the list endpoint.
    """
    agent = await create_test_agent(policy_client)
    session_id = await _create_session(policy_client, agent["id"])

    # ── Step 2: attach DENY policy ──
    policy_id = await _attach_deny_policy(policy_client, session_id)

    # ── Step 3: send message, expect DENY ──
    resp_denied = await _send_user_message(
        policy_client, session_id, "Hello, this should be blocked."
    )
    assert resp_denied.status_code == 202, (
        f"expected 202 from events endpoint; got {resp_denied.status_code} {resp_denied.text}"
    )
    verdict = resp_denied.json()
    assert verdict.get("denied") is True, f"expected synchronous DENY verdict; got {verdict}"
    assert "Blocked by test policy" in verdict.get("reason", ""), (
        f"expected deny reason to contain 'Blocked by test policy'; got {verdict}"
    )

    # ── Step 4: remove the policy ──
    del_resp = await policy_client.delete(
        f"/v1/sessions/{session_id}/policies/{policy_id}",
    )
    assert del_resp.status_code == 200, (
        f"policy delete failed: {del_resp.status_code} {del_resp.text}"
    )
    assert del_resp.json()["deleted"] is True

    # ── Step 5: send another message, expect it passes policy ──
    resp_allowed = await _send_user_message(
        policy_client, session_id, "Hello, this should go through."
    )
    # After policy removal the message must NOT be denied by policy.
    # It may return 202 (queued) or 503 (no runner bound) — both prove
    # the policy layer allowed it through.
    assert resp_allowed.status_code in {202, 503}, (
        f"expected 202 or 503 after policy removal; "
        f"got {resp_allowed.status_code} {resp_allowed.text}"
    )
    body = resp_allowed.json()
    # A synchronous DENY verdict ({"denied": true}) would mean the policy is still active.
    assert body.get("denied") is not True, f"message was denied after policy removal; got {body}"

    # ── Step 6: verify the policy list is empty ──
    list_resp = await policy_client.get(f"/v1/sessions/{session_id}/policies")
    assert list_resp.status_code == 200
    policies = list_resp.json()["data"]
    session_policies = [p for p in policies if p.get("source") == "session"]
    assert len(session_policies) == 0, (
        f"expected no session policies after deletion; got {session_policies}"
    )


async def test_deny_policy_only_blocks_matching_phase(
    policy_client: httpx.AsyncClient,
    mock_llm: ControllableMockClient,
) -> None:
    """A DENY policy scoped to ``tool_call`` phase does not block input messages.

    1. Attach a DENY policy that fires only on ``tool_call`` events.
    2. Send a user message (INPUT/REQUEST phase) — verify it goes through.

    This proves that phase-scoping in ``make_fixed_action_callable``
    correctly causes the callable to abstain (return ``None``) on
    non-matching phases, which the engine coerces to ALLOW.
    """
    agent = await create_test_agent(policy_client)
    session_id = await _create_session(policy_client, agent["id"])

    # Attach DENY on tool_call only.
    await _attach_deny_policy(
        policy_client,
        session_id,
        name="deny_tool_call_only",
        factory_params={
            "action": "deny",
            "reason": "Tool calls are blocked",
            "on_phases": ["tool_call"],
        },
    )

    # Send user message (REQUEST phase) — should NOT be denied.
    # May return 202 (queued) or 503 (no runner) — both prove the
    # policy layer allowed it through; only {"denied": true} is a failure.
    resp = await _send_user_message(policy_client, session_id, "Hello, this should go through.")
    assert resp.status_code in {202, 503}, (
        f"expected 202 or 503; got {resp.status_code} {resp.text}"
    )
    body = resp.json()
    assert body.get("denied") is not True, (
        f"tool_call-only DENY policy incorrectly blocked an input message; got {body}"
    )


async def test_input_deny_publishes_committed_item_event(
    policy_client: httpx.AsyncClient,
    mock_llm: ControllableMockClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An input-phase DENY publishes the sentinel as ``output_item.done``.

    The deny text streams live as an ``output_text.delta`` (a provisional
    web preview) and is persisted as an assistant item. Without a commit
    event the web preview is swept by the terminal ``response.completed``,
    so the deny only reappeared on refresh. Assert the persisted item is
    published as ``response.output_item.done`` — carrying a real itemId —
    so the web reconciles it into a durable block.
    """
    published: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, ev: published.append((sid, ev)),
    )

    agent = await create_test_agent(policy_client)
    session_id = await _create_session(policy_client, agent["id"])
    await _attach_deny_policy(policy_client, session_id)

    resp = await _send_user_message(policy_client, session_id, "Hello, this should be blocked.")
    assert resp.json().get("denied") is True, f"expected synchronous DENY; got {resp.json()}"

    done_events = [ev for _sid, ev in published if ev.get("type") == "response.output_item.done"]
    assert len(done_events) == 1, f"expected one committed-item event; got {done_events}"
    item = done_events[0]["item"]
    assert item.get("id"), f"committed item must carry a store-assigned id; got {item}"
    text = "".join(
        part.get("text", "") for part in item.get("content", []) if isinstance(part, dict)
    )
    assert "Blocked by test policy" in text, (
        f"deny sentinel missing from committed item; got {item}"
    )
