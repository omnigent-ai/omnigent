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
# Surfaces a session may store as its ``omnigent.client_surface`` label. Internal
# components never are: their sessions take the originating surface.
SESSION_SURFACES = frozenset({"web", "desktop", "ios", "android", "mobile", "cli"})
# Request surfaces stored as another session surface. SDK and Slack sessions store
# ``cli``, as on main: runners older than those surfaces only accept main's labels.
# ``script`` is stored as ``web``, as on main.
_SESSION_SURFACE_ALIASES = {"sdk": "cli", "slack": "cli", "script": "web"}
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


def resolve_client_surface(headers: Mapping[str, str]) -> str:
    """Classify the process that sent this request (request attribution).

    Used for per-request telemetry; session labels come from
    :func:`resolve_session_surface`.

    :param headers: Request headers (lower-cased keys).
    :returns: A value in :data:`CLIENT_SURFACES`, or ``"unknown"``.
    """
    surface = headers.get("x-omnigent-client")
    if surface is not None and surface in CLIENT_SURFACES:
        return surface
    return classify_surface(headers.get("user-agent"))


def resolve_session_surface(
    headers: Mapping[str, str],
    *,
    parent_labels: Mapping[str, str] | None = None,
    source_labels: Mapping[str, str] | None = None,
    requested_labels: Mapping[str, str] | None = None,
) -> str:
    """Return the surface to store as a session's ``omnigent.client_surface`` label.

    A runner, host, or harness acts for the user's session, so its session takes
    the originating surface: the parent's label, then the originating session's
    (an independent placement's source, or a fork's source), then the create body's
    copied label (a native ``/clear`` replacement). With none of these it gets what
    main gave the request. Other clients keep their own surface.

    :param headers: Request headers (lower-cased keys).
    :param parent_labels: The parent session's labels, for a child session.
    :param source_labels: Labels of the session this request originates from.
    :param requested_labels: Labels in the create body; only used for internal components.
    :returns: A value in :data:`SESSION_SURFACES`, or ``"unknown"``.
    """
    surface = headers.get("x-omnigent-client")
    stored = _storable(surface) if surface is not None else None
    if stored is not None:
        return stored
    inherited = _stored_surface(parent_labels)
    if inherited is not None:
        return inherited
    user_agent = headers.get("user-agent")
    classified = classify_surface(user_agent)
    if classified not in _INTERNAL_SURFACES:
        return _storable(classified) or "unknown"
    # Attribution is self-reported (User-Agent and labels), not authenticated.
    for labels in (source_labels, requested_labels):
        origin = _stored_surface(labels)
        if origin is not None:
            return origin
    return _main_surface(user_agent)


def _storable(surface: str) -> str | None:
    """Return the session label for a request surface, or ``None`` if it has none."""
    stored = _SESSION_SURFACE_ALIASES.get(surface, surface)
    return stored if stored in SESSION_SURFACES else None


def _stored_surface(labels: Mapping[str, str] | None) -> str | None:
    """Return the session surface stored in *labels*, if it is a storable one."""
    if labels is None:
        return None
    value = labels.get(CLIENT_SURFACE_LABEL_KEY)
    return _storable(value) if value is not None else None


def _main_surface(user_agent: str | None) -> str:
    """Return the surface origin/main gave this User-Agent, before component tokens.

    Used as the fallback for an internal component with no originating session,
    so its session keeps the label it had before component User-Agents existed.
    """
    if user_agent is None:
        return "unknown"
    if not user_agent or "python-httpx" in user_agent or "python-requests" in user_agent:
        return "cli"
    if "Electron" in user_agent:
        return "desktop"
    if "iPhone" in user_agent or "iPad" in user_agent:
        return "ios"
    if "Android" in user_agent:
        return "android"
    return "web"


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
    * empty / bare ``"python-httpx"`` / ``"python-requests"`` → ``"cli"``: older
      Omnigent CLIs, hosts and runners send only httpx, with no component token
    * ``"Electron"`` in UA → ``"desktop"``
    * ``"iPhone"`` or ``"iPad"`` in UA → ``"ios"``
    * ``"Android"`` in UA → ``"android"``
    * a browser (``"Mozilla/"``) → ``"web"``
    * anything else (``Python-urllib``, ``node``, ``Bun``, ``curl``, ...)
      → ``"script"``: a non-browser client that is not Omnigent's own

    :param user_agent: Raw ``User-Agent`` header value, or ``None``.
    :returns: Surface label string.
    """
    if user_agent is None:
        return "unknown"
    for prefix, surface in _OMNIGENT_USER_AGENT_SURFACES.items():
        if user_agent.startswith(prefix):
            return surface
    # Deprecated: bare httpx/requests also covers custom scripts. Classify them as
    # "script" in v0.24.0, once clients older than v0.22 are no longer supported.
    if not user_agent or "python-httpx" in user_agent or "python-requests" in user_agent:
        return "cli"
    if "Electron" in user_agent:
        return "desktop"
    if "iPhone" in user_agent or "iPad" in user_agent:
        return "ios"
    if "Android" in user_agent:
        return "android"
    if "Mozilla/" in user_agent:
        return "web"
    return "script"
