"""User-Agent → client surface classifier."""

from __future__ import annotations

from collections.abc import Mapping

CLIENT_SURFACE_LABEL_KEY = "omnigent.client_surface"
CLIENT_SURFACE_ENV_VAR = "OMNIGENT_CLIENT_SURFACE"
CLIENT_SURFACES = frozenset({"web", "desktop", "ios", "android", "mobile", "cli"})


def resolve_client_surface(
    headers: Mapping[str, str], *, parent_labels: Mapping[str, str] | None = None
) -> str:
    """Identify the creating client; internal child requests inherit their parent's surface."""
    surface = headers.get("x-omnigent-client")
    if surface is not None and surface in CLIENT_SURFACES:
        return surface
    if parent_labels is not None:
        inherited = parent_labels.get(CLIENT_SURFACE_LABEL_KEY)
        if inherited is not None and inherited in CLIENT_SURFACES:
            return inherited
    return classify_surface(headers.get("user-agent"))


def classify_surface(user_agent: str | None) -> str:
    """Return a surface label for the given User-Agent string.

    Used as a fallback when the ``X-Omnigent-Client`` request header is absent
    or unrecognised.

    Mapping:
    * ``None`` or absent → ``"unknown"``
    * ``"Electron"`` in UA → ``"desktop"``
    * ``"iPhone"`` or ``"iPad"`` in UA → ``"ios"``
    * ``"Android"`` in UA → ``"android"``
    * empty / ``"python-httpx"`` / ``"python-requests"`` → ``"cli"``
    * anything else → ``"web"``

    :param user_agent: Raw ``User-Agent`` header value, or ``None``.
    :returns: Surface label string.
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
