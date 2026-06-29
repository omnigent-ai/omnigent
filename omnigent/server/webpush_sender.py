"""Web Push delivery (#8) — fan a notification out to a user's subscriptions.

Pulls the user's registered subscriptions, encrypts the payload for each
(:mod:`omnigent.server.webpush`), and POSTs to each push service. A service
that reports the endpoint gone (HTTP 404/410) gets its subscription pruned, so
dead registrations self-clean. No-op when push isn't configured (no VAPID key).
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from omnigent.server.webpush import Subscription, build_push_request

_logger = logging.getLogger(__name__)

# Push services return these when a subscription is permanently gone.
_GONE_STATUSES = frozenset({404, 410})


async def notify_user_push(
    user_id: str,
    *,
    title: str,
    body: str,
    navigate_path: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> int:
    """Send a Web Push notification to every subscription ``user_id`` has.

    :param user_id: The recipient.
    :param title: Notification headline.
    :param body: Notification body line.
    :param navigate_path: In-app path the service worker opens on click, e.g.
        ``"/c/conv_abc"``.
    :param client: An ``httpx.AsyncClient`` to POST with; one is created and
        closed per call when ``None`` (tests inject a mock-transport client).
    :returns: The number of subscriptions successfully delivered to. ``0`` when
        push is unconfigured or the user has no subscriptions.
    """
    from omnigent.runtime import get_caps, get_push_subscription_store

    store = get_push_subscription_store()
    caps = get_caps()
    if store is None or caps.vapid_private_key is None:
        return 0

    subs = await asyncio.to_thread(store.list_for_user, user_id)
    if not subs:
        return 0

    payload = json.dumps({"title": title, "body": body, "navigatePath": navigate_path}).encode(
        "utf-8"
    )

    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    sent = 0
    try:
        for sub in subs:
            url, headers, data = build_push_request(
                Subscription(endpoint=sub.endpoint, p256dh=sub.p256dh, auth=sub.auth),
                payload,
                caps.vapid_private_key,
                caps.vapid_subject,
            )
            try:
                resp = await client.post(url, headers=headers, content=data)
            except Exception:  # noqa: BLE001 — one bad endpoint must not abort the fan-out
                _logger.warning("web push POST failed for %s", sub.endpoint, exc_info=True)
                continue
            if resp.status_code in _GONE_STATUSES:
                await asyncio.to_thread(store.delete_by_endpoint, sub.endpoint)
                _logger.info(
                    "pruned gone push subscription %s (HTTP %s)",
                    sub.endpoint,
                    resp.status_code,
                )
                continue
            if resp.status_code >= 400:
                _logger.warning("web push %s → HTTP %s", sub.endpoint, resp.status_code)
                continue
            sent += 1
    finally:
        if own_client:
            await client.aclose()
    return sent
