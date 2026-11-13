"""Telemetry headers stamped on outbound Omnigent server requests.

A client process (CLI, host-launched runner, native harness forwarder) knows
its own machine's installation ID; the server does not, and looking it up
would cost a read on the request path. Sending it as a header lets a
server-side emitter attribute an event to the machine that produced it for
free — and lets it skip the field when the header is absent.
"""

from __future__ import annotations

import os
import re
import uuid

# Installation ID of the machine making the request. Read by the server's
# session-event route to attribute native turn-end / usage telemetry to the
# host running the session.
INSTALLATION_ID_HEADER = "X-Omnigent-Installation-Id"


# Which Omnigent component this process is, as a self-reported attribution hint
# (not an authenticated identity). Child processes (hooks, forwarders) inherit
# it, so a helper a runner spawns reports as that runner.
CLIENT_COMPONENT_ENV_VAR = "OMNIGENT_CLIENT_COMPONENT"
# ``omnigent-<component>/`` User-Agent product tokens.
CLIENT_COMPONENTS = frozenset({"cli", "host", "runner", "harness"})
_DETAIL_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def set_client_component(component: str, detail: str | None = None) -> None:
    """Record this process's component for its (and its children's) User-Agent.

    :param component: One of :data:`CLIENT_COMPONENTS`, e.g. ``"runner"``.
    :param detail: Optional qualifier, e.g. the harness name ``"claude-sdk"``.
    :raises ValueError: If *component* is not in :data:`CLIENT_COMPONENTS`.
    """
    if component not in CLIENT_COMPONENTS:
        raise ValueError(f"unknown client component {component!r}")
    os.environ[CLIENT_COMPONENT_ENV_VAR] = f"{component}/{detail}" if detail else component


def client_user_agent() -> str:
    """Return the User-Agent identifying this Omnigent component to the server.

    :returns: e.g. ``"omnigent-runner/0.22.0 python-httpx/0.28.1"`` or
        ``"omnigent-harness/0.22.0 (claude-sdk) python-httpx/0.28.1"``. The
        trailing httpx token is kept for anything that sniffs it.
    """
    import httpx

    from omnigent.version import VERSION

    component, _, detail = os.environ.get(CLIENT_COMPONENT_ENV_VAR, "").partition("/")
    # Unset (a library caller) or an env value we never wrote: report the default.
    if component not in CLIENT_COMPONENTS:
        component, detail = "cli", ""
    product = f"omnigent-{component}/{VERSION}"
    detail = _DETAIL_UNSAFE_RE.sub("", detail)[:64]
    if detail:
        product += f" ({detail})"
    return f"{product} python-httpx/{httpx.__version__}"


def client_identity_headers() -> dict[str, str]:
    """Return the headers naming this Omnigent component on a server request.

    Not gated on the telemetry opt-out: like any HTTP client's default
    User-Agent, it carries only a component name and version.

    :returns: ``{"User-Agent": client_user_agent()}``.
    """
    return {"User-Agent": client_user_agent()}


def pop_client_user_agent(headers: dict[str, str]) -> str:
    """Remove every User-Agent key from *headers* and return this component's UA.

    ``websockets`` adds its own User-Agent on top of ``additional_headers``, so a
    handshake must pass exactly one, as ``user_agent_header``. The component UA
    wins over a caller-supplied one, as in ``open_server_client``. Keys match
    case-insensitively, so a lowercase ``user-agent`` cannot survive.

    :param headers: Handshake headers, mutated in place.
    :returns: The User-Agent to pass as ``user_agent_header``.
    """
    for key in [key for key in headers if key.lower() == "user-agent"]:
        del headers[key]
    return client_user_agent()


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
