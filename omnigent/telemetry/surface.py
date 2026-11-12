"""User-Agent → client surface classifier."""

from __future__ import annotations

import os
from collections.abc import Mapping

CLIENT_SURFACE_LABEL_KEY = "omnigent.client_surface"
CLIENT_SURFACE_ENV_VAR = "OMNIGENT_CLIENT_SURFACE"
CLIENT_SURFACES = frozenset(
    {
        "web",
        "desktop",
        "ios",
        "android",
        "mobile",
        "cli",
        "runner",
        "host",
        "harness",
        "sdk",
        "script",
        "slack",
    }
)
# Omnigent components acting for a session another surface created.
_INTERNAL_SURFACES = frozenset({"runner", "host", "harness"})
# User-Agent product tokens Omnigent's own Python clients send, e.g.
# ``omnigent-runner/0.22.0 python-httpx/0.28.1``; ``omnigent-client`` is the SDK.
_OMNIGENT_USER_AGENT_SURFACES = {
    "omnigent-cli/": "cli",
    "omnigent-runner/": "runner",
    "omnigent-host/": "host",
    "omnigent-harness/": "harness",
    "omnigent-client/": "sdk",
    "omnigent-slack/": "slack",
}


def resolve_client_surface(
    headers: Mapping[str, str],
    *,
    parent_labels: Mapping[str, str] | None = None,
    requested_labels: Mapping[str, str] | None = None,
) -> str:
    """Identify the creating client; internal child requests inherit their parent's surface.

    :param headers: Request headers (lower-cased keys).
    :param parent_labels: The parent session's labels, for a child session.
    :param requested_labels: Labels in the create body. A runner, host, or harness
        re-creating a session (e.g. native ``/clear``) copies the original's labels,
        so their surface is kept instead of naming the internal component.
    :returns: A value in :data:`CLIENT_SURFACES`, or ``"unknown"``.
    """
    surface = headers.get("x-omnigent-client")
    if surface is not None and surface in CLIENT_SURFACES:
        return surface
    if parent_labels is not None:
        inherited = parent_labels.get(CLIENT_SURFACE_LABEL_KEY)
        if inherited is not None and inherited in CLIENT_SURFACES:
            return inherited
    # Attribution is self-reported (User-Agent and labels), not authenticated:
    # an internal UA may carry its re-created session's original surface forward.
    classified = classify_surface(headers.get("user-agent"))
    if classified in _INTERNAL_SURFACES and requested_labels is not None:
        requested = requested_labels.get(CLIENT_SURFACE_LABEL_KEY)
        if requested is not None and requested in CLIENT_SURFACES:
            return requested
    return classified


def harness_client_surface() -> str | None:
    """Return the creating surface the runner recorded in this harness worker's environment.

    Only harness worker processes read this; terminals inherit it but never trust it.
    """
    return os.environ.get(CLIENT_SURFACE_ENV_VAR)


def classify_surface(user_agent: str | None) -> str:
    """Return a surface label for the given User-Agent string.

    Used as a fallback when the ``X-Omnigent-Client`` request header is absent
    or unrecognised.

    Mapping:
    * ``None`` or absent → ``"unknown"``
    * ``"omnigent-<component>/"`` prefix → ``"cli"``, ``"runner"``, ``"host"``,
      ``"harness"``, ``"sdk"`` (``omnigent-client``), or ``"slack"``
    * ``"Electron"`` in UA → ``"desktop"``
    * ``"iPhone"`` or ``"iPad"`` in UA → ``"ios"``
    * ``"Android"`` in UA → ``"android"``
    * a browser (``"Mozilla/"``) → ``"web"``
    * anything else (empty, bare ``python-httpx``, ``Python-urllib``, ``node``,
      ``Bun``, ``curl``, ...) → ``"script"``: a client that is not Omnigent's own

    :param user_agent: Raw ``User-Agent`` header value, or ``None``.
    :returns: Surface label string.
    """
    if user_agent is None:
        return "unknown"
    for prefix, surface in _OMNIGENT_USER_AGENT_SURFACES.items():
        if user_agent.startswith(prefix):
            return surface
    if "Electron" in user_agent:
        return "desktop"
    if "iPhone" in user_agent or "iPad" in user_agent:
        return "ios"
    if "Android" in user_agent:
        return "android"
    if "Mozilla/" in user_agent:
        return "web"
    return "script"
