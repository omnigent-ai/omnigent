"""UI e2e: the app subscribes to Web Push and registers with the server (#8).

Drives the real browser path: notification permission granted → the app's
subscribe-on-load hook (``useIdleNotifications`` → ``enablePushNotifications``)
fetches the VAPID key, subscribes, and POSTs the subscription to
``/v1/push/subscriptions`` — asserted by intercepting that real request's
response (which also exercises the server's endpoint validation accepting a
legitimate FCM-shaped endpoint).

Two deliberate substitutions, both documented:

- **Own browser, full-Chromium channel**: pytest-playwright's default headless
  *shell* hard-codes ``Notification.permission == "denied"``, which blocks the
  subscribe-on-load path. The full build (``channel="chromium"``, what CI's
  ``playwright install chromium`` provides) honors granted permissions. Skips
  if the channel is unavailable.
- **Stubbed ``PushManager``**: headless Chromium has no FCM connection, so the
  service-worker registration is stubbed to hand the app a subscription with
  real P-256 key material. Everything from the app code down — key fetch,
  subscribe call path, REST registration, endpoint validation, storage — is
  the real code path. (Payload crypto is pinned separately against the RFC
  8291 vector in tests/server/test_webpush.py.)
"""

from __future__ import annotations

import os

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from playwright.sync_api import Error as PlaywrightError

from omnigent.server.webpush import b64url_encode


def test_app_subscribes_and_registers_push_subscription(playwright, live_server: str) -> None:
    try:
        browser = playwright.chromium.launch(channel="chromium", headless=True)
    except PlaywrightError:
        pytest.skip("full chromium channel unavailable (headless shell can't grant notifications)")

    # Real client-side key material (what a browser's push service would mint).
    ua_private = ec.generate_private_key(ec.SECP256R1())
    p256dh = b64url_encode(
        ua_private.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
    )
    auth = b64url_encode(os.urandom(16))
    # FCM-shaped and public-resolving: must pass the server's HTTPS+public-host
    # endpoint validation. Never POSTed to by this test.
    endpoint = "https://fcm.googleapis.com/fcm/send/e2e-omnigent-push"

    stub = f"""
    (() => {{
      const sub = {{
        endpoint: {endpoint!r},
        toJSON: () => ({{
          endpoint: {endpoint!r},
          keys: {{ p256dh: {p256dh!r}, auth: {auth!r} }},
        }}),
        unsubscribe: async () => true,
      }};
      const registration = {{
        pushManager: {{ getSubscription: async () => null, subscribe: async () => sub }},
        addEventListener: () => {{}},
        removeEventListener: () => {{}},
        update: async () => {{}},
        unregister: async () => true,
        installing: null,
        waiting: null,
        active: {{ state: 'activated', addEventListener: () => {{}} }},
        scope: '/',
      }};
      if (navigator.serviceWorker) {{
        navigator.serviceWorker.register = async () => registration;
        try {{
          Object.defineProperty(navigator.serviceWorker, 'ready', {{
            value: Promise.resolve(registration), configurable: true,
          }});
        }} catch (e) {{ /* best-effort */ }}
      }}
    }})();
    """

    ctx = browser.new_context(viewport={"width": 1280, "height": 800})
    ctx.grant_permissions(["notifications"], origin=live_server)
    ctx.add_init_script(stub)
    page = ctx.new_page()
    try:
        with page.expect_response(
            lambda r: r.url.endswith("/v1/push/subscriptions") and r.request.method == "POST",
            timeout=30_000,
        ) as resp_info:
            page.goto(live_server)
            # Permission is pre-granted, so the hook subscribes on load; the
            # click also covers the one-shot user-gesture path.
            page.mouse.click(640, 300)
        response = resp_info.value
        assert response.ok, f"subscribe POST failed: {response.status} {response.text()[:200]}"
        body = response.json()
        assert body["object"] == "push_subscription"
        assert body["id"]
    finally:
        page.close()
        ctx.close()
        browser.close()
