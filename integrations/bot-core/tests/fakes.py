"""Shared fixtures for the bot-core suite.

Only the pieces that describe the SERVER contract live here — the SSE
frame builders and the endpoint catalog the drift test reconciles against
``openapi.json``. Anything that fakes a chat platform belongs in that
bot's own suite.
"""

from __future__ import annotations

import json
from typing import Any

# ── Omnigent API contract the Slack client depends on ─────────────────────────
#
# The single source of truth for which server endpoints the bot calls, and
# which of those are part of the server's PUBLIC (schema-documented) surface.
# ``test_integration.py``'s drift test reconciles this catalog against the
# committed ``openapi.json`` so a server-side rename/removal of a
# ``documented=True`` endpoint fails a Slack test — surfacing the break here
# rather than silently at runtime against a deployed server.
#
# Each entry: (method, path_template, documented). ``documented=False`` marks
# an endpoint the server intentionally hides from its OpenAPI schema
# (``include_in_schema=False`` — the ``/oauth/device/*`` login routes and the
# internal ``/events`` + elicitation-resolve routes); the drift test asserts
# those are ABSENT from the schema so a future decision to publish one is a
# deliberate, noticed change.
#
# Keep in sync with ``OmnigentClient`` (integrations/bot-core/src/omnigent_bot_core/
# omnigent.py) and the login flow (oauth.py / auth_manager.py).
OMNIGENT_ENDPOINTS: list[tuple[str, str, bool]] = [
    # Setup / validation.
    ("GET", "/health", True),
    ("GET", "/v1/me", True),
    ("GET", "/v1/agents", True),
    ("GET", "/v1/hosts", True),
    ("GET", "/v1/hosts/{host_id}/filesystem", True),
    # Session lifecycle.
    ("POST", "/v1/sessions", True),
    ("GET", "/v1/sessions/{session_id}", True),
    ("GET", "/v1/sessions/{session_id}/items", True),
    ("GET", "/v1/sessions/{session_id}/stream", True),
    ("POST", "/v1/hosts/{host_id}/runners", True),
    ("GET", "/v1/runners/{runner_id}/status", True),
    # Internal (hidden from the public schema).
    ("POST", "/v1/sessions/{session_id}/events", False),
    ("POST", "/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve", False),
    # Device-grant login (oauth.py, accounts mode): authorize starts the grant;
    # /oauth/token both polls for the device-code token AND refreshes;
    # /oauth/revoke logs out.
    ("POST", "/oauth/device/authorize", False),
    ("POST", "/oauth/token", False),
    ("POST", "/oauth/revoke", False),
    # OIDC ticket login (oauth.py, oidc mode): start a CLI-login ticket, then poll.
    ("POST", "/auth/cli-login", False),
    ("GET", "/auth/cli-poll", False),
]

# Response fields the client actually reads off the two richest documented
# schemas. If the server renames one of these, the client silently degrades
# (a None harness, an empty agent list), so the drift test pins them.
OMNIGENT_RESPONSE_FIELDS: dict[str, tuple[str, ...]] = {
    # GET /v1/sessions/{session_id} → SessionResponse (get_session_info).
    "SessionResponse": ("harness", "agent_name"),
    # GET /v1/agents → PaginatedList (list_agents reads .data).
    "PaginatedList": ("data",),
}


def sse_status(status: str, response_id: str | None = None) -> str:
    """One ``session.status`` SSE frame (id-bearing when ``response_id`` given)."""
    payload: dict[str, Any] = {"type": "session.status", "status": status}
    if response_id is not None:
        payload["response_id"] = response_id
    return f"data: {json.dumps(payload)}\n\n"


def sse_delta(text: str, message_id: str | None = None) -> str:
    """One ``response.output_text.delta`` SSE frame."""
    payload: dict[str, Any] = {"type": "response.output_text.delta", "delta": text}
    if message_id is not None:
        payload["message_id"] = message_id
    return f"data: {json.dumps(payload)}\n\n"


# The bot's SSE turn-end shape reused across streaming scenarios: a running
# edge, one answer delta, then the id-bearing idle that ends the turn.
DEFAULT_SSE_BODY = (
    sse_status("running", "resp_1")
    + sse_delta("Here is the answer.", "m1")
    + 'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
    + sse_status("idle", "resp_1")
)
