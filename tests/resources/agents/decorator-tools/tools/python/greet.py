"""Greet tool (e2e fixture, primitive str arg)."""

from __future__ import annotations

from omnigent_client.tools import tool


@tool
def greet(name: str) -> str:
    """
    Return a greeting for the given name.

    :param name: The name to greet, e.g. ``"Alice"``.
    :returns: ``f"Hello, {name}!"``, e.g. ``"Hello, Alice!"``.
    """
    return f"Hello, {name}!"
