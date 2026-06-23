"""compute tool (e2e fixture, multiple primitive args + default)."""

from __future__ import annotations

from omnigent_client.tools import tool


@tool
def compute(value: int, multiplier: int = 2, note: str = "") -> dict[str, int | str]:
    """
    Multiply ``value`` by ``multiplier`` and echo the optional note.

    :param value: Base integer value, e.g. ``5``.
    :param multiplier: Multiplier (defaults to ``2``).
    :param note: Optional note to echo back, e.g. ``"hi"``.
    :returns: ``{"product": value * multiplier, "note": note}``, e.g.
        ``{"product": 10, "note": ""}``.
    """
    return {"product": value * multiplier, "note": note}
