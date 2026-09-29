"""Telemetry headers stamped on outbound Omnigent server requests.

A client process (CLI, host-launched runner, native harness forwarder) knows
its own machine's installation ID; the server does not, and looking it up
would cost a read on the request path. Sending it as a header lets a
server-side emitter attribute an event to the machine that produced it for
free — and lets it skip the field when the header is absent.
"""

from __future__ import annotations

import uuid

# Installation ID of the machine making the request. Read by the server's
# session-event route to attribute native turn-end / usage telemetry to the
# host running the session.
INSTALLATION_ID_HEADER = "X-Omnigent-Installation-Id"


def telemetry_request_headers() -> dict[str, str]:
    """Return the telemetry headers for an outbound server request.

    :returns: ``{INSTALLATION_ID_HEADER: <id>}``, or ``{}`` when telemetry is
        opted out of or this machine has no installation ID. Never raises.
    """
    try:
        from omnigent.telemetry.client import is_disabled
        from omnigent.telemetry.installation_id import get_installation_id

        if is_disabled():
            return {}
        installation_id = get_installation_id()
        return {INSTALLATION_ID_HEADER: installation_id} if installation_id else {}
    except Exception:  # telemetry must never break a request
        return {}


def parse_installation_id_header(value: str | None) -> str | None:
    """Validate a client-sent installation ID before it reaches telemetry.

    The header is attacker-controllable, so only a well-formed UUID (what
    :func:`~omnigent.telemetry.installation_id.get_installation_id` writes) is
    accepted — anything else would let a caller stuff arbitrary text into an
    event field.

    :param value: Raw header value, e.g. the request's
        ``X-Omnigent-Installation-Id``; ``None`` when the client sent none.
    :returns: The installation ID, or ``None`` when absent or malformed.
    """
    if not value:
        return None
    try:
        return str(uuid.UUID(value.strip()))
    except (ValueError, AttributeError):
        return None
