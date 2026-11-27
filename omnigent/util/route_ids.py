"""Identifiers that must stay inside one URL path segment."""

from __future__ import annotations

import re

_ROUTE_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,128}")


def route_identifier(value: object, kind: str) -> str:
    """
    Return *value* when it is safe as one URL path segment, including legacy prefixed ids.

    :param value: The identifier, e.g. ``"conv_abc123"``.
    :param kind: What it identifies, for the error, e.g. ``"Session"``.
    :returns: *value*, unchanged.
    :raises ValueError: When *value* has characters such as ``/``, ``?`` or ``#``
        that would change the route.
    """
    if not isinstance(value, str) or not _ROUTE_IDENTIFIER.fullmatch(value):
        raise ValueError(f"{kind} id must contain only letters, digits, underscores or hyphens")
    return value
