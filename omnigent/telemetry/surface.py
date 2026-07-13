"""User-Agent → client surface classifier."""

from __future__ import annotations


def classify_surface(user_agent: str | None) -> str:
    """Return a surface label for the given User-Agent string.

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
