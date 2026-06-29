"""E2E: the Web Push subscription API works on the live server (#8).

Fetches the VAPID public key and registers a subscription via
``/v1/push/subscriptions`` — proving the push store + routes + VAPID config are
wired end to end. (The RFC 8291 / VAPID crypto is unit-covered in
``tests/server/test_webpush.py`` against the spec's published vectors.)
"""

from __future__ import annotations

import httpx


def test_push_subscribe_roundtrip(http_client: httpx.Client) -> None:
    # VAPID is configured on the live server (the public key is non-empty).
    key = http_client.get("/v1/push/vapid-public-key")
    key.raise_for_status()
    assert key.json().get("key")

    resp = http_client.post(
        "/v1/push/subscriptions",
        json={
            "endpoint": "https://push.example.com/sub/e2e",
            "keys": {"p256dh": "BPxxBASE64TESTKEYxx", "auth": "YXV0aHNlY3JldA"},
        },
    )
    resp.raise_for_status()
    assert resp.json().get("object") == "push_subscription"
