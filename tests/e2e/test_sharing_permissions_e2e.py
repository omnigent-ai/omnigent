"""Cross-user sharing/permission e2e tests against the real server.

The e2e ``live_server`` runs header auth (``OMNIGENT_AUTH_PROVIDER=header``
pinned in tests/conftest.py) with a real permission store, so identity is
just the ``X-Forwarded-Email`` header. Only one test here makes an LLM
call; the rest pin the HTTP semantics of the permission model:

- insufficient level (some access) -> 403
- no access at all -> 404 (anti-enumeration: must not reveal existence)
- ``__public__`` grants are read-only

Those exact codes are deliberate assertions; if the anti-enumeration
policy ever changes, these tests are the tripwire.

Run::

    pytest tests/e2e/test_sharing_permissions_e2e.py --llm-api-key $KEY --profile <name> -v
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass

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

# Permission levels mirrored from omnigent/server/auth.py. Mirrored
# rather than imported so a server-side renumbering fails these tests
# loudly instead of silently tracking the change.
_LEVEL_READ = 1
_LEVEL_EDIT = 2
_LEVEL_MANAGE = 3
_PUBLIC = "__public__"


def _client_for(base_url: str, email: str) -> httpx.Client:
    """An httpx client authenticated as *email* via header identity.

    :param base_url: The live server base URL.
    :param email: Identity for ``X-Forwarded-Email``,
        e.g. ``"alice-ab12@e2e.test"``.
    """
    return httpx.Client(
        base_url=base_url,
        headers={"X-Forwarded-Email": email},
        timeout=300,
    )


def _extract_all_text(body: dict) -> str:  # type: ignore[type-arg]
    """Concatenate all message text blocks from a terminal turn body."""
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") == "message":
            for block in item.get("content", []):
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n".join(parts)


@dataclass
class _OwnedSession:
    """A session owned by the headerless ``local`` identity.

    The owner must be ``local`` because the fixture runner is owned by
    the identity that registered it (headerless), and the
    runner-ownership rule forbids binding a session to another user's
    runner. ``local`` is still a
    real, distinct user to the permission model, so the cross-user
    grants to Bob/Carol exercise exactly the multi-user paths.

    :param owner_email: The owner's user id (``"local"``).
    :param owner: Headerless httpx client (resolves to ``local``).
    :param session_id: Runner-bound session id, e.g. ``"conv_abc"``.
    """

    owner_email: str
    owner: httpx.Client
    session_id: str
    model: str = ""


@pytest.fixture
def owner_session(
    live_server: str,
    live_runner_id: str,
    mock_llm_server_url: str | None,
) -> Iterator[_OwnedSession]:
    """A runner-bound session owned by the ``local`` identity.

    Always uses mock LLM via an inline agent.
    """
    suffix = uuid.uuid4().hex[:6]
    model = f"mock-share-{suffix}"
    owner = httpx.Client(base_url=live_server, timeout=300)
    reset_mock_llm(mock_llm_server_url)
    agent_name = register_inline_agent(
        owner,
        name=f"sharing-e2e-{suffix}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a terse assistant. Follow instructions exactly.",
        mock_llm_base_url=(f"{mock_llm_server_url}/v1" if mock_llm_server_url else None),
    )
    session_id = create_runner_bound_session(
        owner, agent_name=agent_name, runner_id=live_runner_id
    )
    yield _OwnedSession(
        owner_email="local",
        owner=owner,
        session_id=session_id,
        model=model,
    )
    owner.close()


def test_read_grant_allows_snapshot_blocks_events(
    live_server: str, owner_session: _OwnedSession
) -> None:
    """READ lets Bob see the session but not act on it."""
    sid = owner_session.session_id
    with _client_for(live_server, f"bob-{uuid.uuid4().hex[:6]}@e2e.test") as bob:
        bob_email = bob.headers["X-Forwarded-Email"]
        # No grant yet: 404, not 403 — existence must not leak.
        assert bob.get(f"/v1/sessions/{sid}").status_code == 404

        grant = owner_session.owner.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": bob_email, "level": _LEVEL_READ},
        )
        assert grant.status_code == 200

        snap = bob.get(f"/v1/sessions/{sid}")
        assert snap.status_code == 200
        # The snapshot tells the SPA to disable the composer for level 1.
        assert snap.json()["permission_level"] == _LEVEL_READ

        owner = bob.get(f"/v1/sessions/{sid}/owner")
        assert owner.status_code == 200
        # READ suffices to see who owns the session.
        assert owner.json()["owner"] == owner_session.owner_email

        # Posting a turn needs EDIT: Bob has SOME access, so 403 (not 404).
        send = bob.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            },
        )
        assert send.status_code == 403
        # Listing grants needs MANAGE.
        assert bob.get(f"/v1/sessions/{sid}/permissions").status_code == 403


def test_edit_grant_bob_turn_completes_and_owner_sees_it(
    live_server: str,
    owner_session: _OwnedSession,
    mock_llm_server_url: str | None,
) -> None:
    """An EDIT collaborator's turn runs the LLM and lands in the
    owner's view of the conversation."""
    sid = owner_session.session_id
    marker = f"shared-turn-{uuid.uuid4().hex[:8]}"
    if owner_session.model:
        configure_mock_llm(mock_llm_server_url, [{"text": marker}], key=owner_session.model)
    with _client_for(live_server, f"bob-{uuid.uuid4().hex[:6]}@e2e.test") as bob:
        owner_session.owner.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": bob.headers["X-Forwarded-Email"], "level": _LEVEL_EDIT},
        ).raise_for_status()

        response_id = send_user_message_to_session(
            bob,
            session_id=sid,
            content=f"Reply with exactly this token and nothing else: {marker}",
        )
        body = poll_session_until_terminal(bob, session_id=sid, response_id=response_id)
        assert body["status"] == "completed", f"turn failed: {body.get('error')}"
        # The marker round-tripped through the real LLM under Bob's identity.
        assert marker in _extract_all_text(body)

        # The owner sees both Bob's user message and the assistant reply.
        snap = owner_session.owner.get(f"/v1/sessions/{sid}")
        snap.raise_for_status()
        all_text = str(snap.json()["items"])
        assert marker in all_text


def test_revoke_returns_404_for_bob(live_server: str, owner_session: _OwnedSession) -> None:
    """Revocation removes ALL access: Bob is back to anti-enumeration 404s."""
    sid = owner_session.session_id
    with _client_for(live_server, f"bob-{uuid.uuid4().hex[:6]}@e2e.test") as bob:
        bob_email = bob.headers["X-Forwarded-Email"]
        owner_session.owner.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": bob_email, "level": _LEVEL_READ},
        ).raise_for_status()
        assert bob.get(f"/v1/sessions/{sid}").status_code == 200

        revoke = owner_session.owner.delete(f"/v1/sessions/{sid}/permissions/{bob_email}")
        assert revoke.status_code == 204

        assert bob.get(f"/v1/sessions/{sid}").status_code == 404
        send = bob.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            },
        )
        assert send.status_code == 404
        # Revoking an absent grant is idempotent.
        assert (
            owner_session.owner.delete(f"/v1/sessions/{sid}/permissions/{bob_email}").status_code
            == 204
        )
        grants = owner_session.owner.get(f"/v1/sessions/{sid}/permissions")
        grants.raise_for_status()
        # The grant row is gone, not just downgraded.
        assert bob_email not in [g["user_id"] for g in grants.json()]


def test_public_grant_read_only_semantics(live_server: str, owner_session: _OwnedSession) -> None:
    """``__public__`` opens read access to everyone, and only read."""
    sid = owner_session.session_id
    with _client_for(live_server, f"carol-{uuid.uuid4().hex[:6]}@e2e.test") as carol:
        assert carol.get(f"/v1/sessions/{sid}").status_code == 404

        owner_session.owner.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": _PUBLIC, "level": _LEVEL_READ},
        ).raise_for_status()

        # Any identity can now read: Carol was never granted directly.
        assert carol.get(f"/v1/sessions/{sid}").status_code == 200
        # ...but writing still needs an explicit EDIT grant.
        send = carol.post(
            f"/v1/sessions/{sid}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            },
        )
        assert send.status_code == 403

        # Public grants above READ are rejected outright.
        too_high = owner_session.owner.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": _PUBLIC, "level": _LEVEL_EDIT},
        )
        assert too_high.status_code == 400


def test_bob_cannot_grant_or_escalate(live_server: str, owner_session: _OwnedSession) -> None:
    """EDIT does not confer grant management: no self-escalation, no
    revoking the owner."""
    sid = owner_session.session_id
    with _client_for(live_server, f"bob-{uuid.uuid4().hex[:6]}@e2e.test") as bob:
        bob_email = bob.headers["X-Forwarded-Email"]
        owner_session.owner.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": bob_email, "level": _LEVEL_EDIT},
        ).raise_for_status()

        # Granting (even to oneself) requires MANAGE: Bob has EDIT -> 403.
        escalate = bob.put(
            f"/v1/sessions/{sid}/permissions",
            json={"user_id": bob_email, "level": _LEVEL_MANAGE},
        )
        assert escalate.status_code == 403

        revoke_owner = bob.delete(f"/v1/sessions/{sid}/permissions/{owner_session.owner_email}")
        assert revoke_owner.status_code == 403
