"""Tests for the Web Push sender (#8) — fan-out, send-count, dead-endpoint prune.

Drives the real ``notify_user_push`` against a fake store and an injected
``httpx`` mock-transport client, so the list → encrypt → POST → prune logic is
exercised without a live push service.
"""

from __future__ import annotations

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

import omnigent.runtime as runtime
from omnigent.entities.push_subscription import PushSubscription
from omnigent.runtime.caps import RuntimeCaps
from omnigent.server.webpush import b64url_encode
from omnigent.server.webpush_sender import notify_user_push


def _sub(sub_id: str, endpoint: str) -> PushSubscription:
    ua = ec.generate_private_key(ec.SECP256R1())
    pub = ua.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return PushSubscription(
        id=sub_id,
        user_id="u",
        endpoint=endpoint,
        p256dh=b64url_encode(pub),
        auth=b64url_encode(b"0123456789abcdef"),
        created_at=0,
    )


class _FakeStore:
    def __init__(self, subs: list[PushSubscription]) -> None:
        self._subs = subs
        self.deleted: list[str] = []

    def list_for_user(self, user_id: str) -> list[PushSubscription]:
        return [s for s in self._subs if s.user_id == user_id]

    def delete_by_endpoint(self, endpoint: str) -> bool:
        self.deleted.append(endpoint)
        return True


async def test_fan_out_counts_successes_and_prunes_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore([_sub("a", "http://push.test/A"), _sub("b", "http://push.test/B")])
    caps = RuntimeCaps(
        vapid_private_key=ec.generate_private_key(ec.SECP256R1()),
        vapid_subject="mailto:test@localhost",
    )
    monkeypatch.setattr(runtime, "get_push_subscription_store", lambda: store)
    monkeypatch.setattr(runtime, "get_caps", lambda: caps)

    # Endpoint A accepts (201); endpoint B is gone (410) → must be pruned.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201 if request.url.path == "/A" else 410)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sent = await notify_user_push("u", title="Agent ready", body="Needs you", client=client)
    await client.aclose()

    assert sent == 1
    assert store.deleted == ["http://push.test/B"]


async def test_noop_without_vapid_key(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore([_sub("a", "http://push.test/A")])
    monkeypatch.setattr(runtime, "get_push_subscription_store", lambda: store)
    monkeypatch.setattr(runtime, "get_caps", lambda: RuntimeCaps())  # vapid_private_key=None

    sent = await notify_user_push("u", title="x", body="y")
    assert sent == 0


async def test_noop_without_subscriptions(monkeypatch: pytest.MonkeyPatch) -> None:
    caps = RuntimeCaps(
        vapid_private_key=ec.generate_private_key(ec.SECP256R1()),
        vapid_subject="mailto:test@localhost",
    )
    monkeypatch.setattr(runtime, "get_push_subscription_store", lambda: _FakeStore([]))
    monkeypatch.setattr(runtime, "get_caps", lambda: caps)

    sent = await notify_user_push("u", title="x", body="y")
    assert sent == 0
