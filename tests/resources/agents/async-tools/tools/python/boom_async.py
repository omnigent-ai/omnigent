"""boom_async fixture tool (always raises, to exercise the async failure path)."""

from __future__ import annotations

from omnigent_client.tools import tool


@tool
def boom_async() -> str:
    """
    Always raise so the failure path of the async pipeline is exercised.

    :raises RuntimeError: Always, with message ``ASYNC_TOOL_BOOM_MARKER``.
    :returns: Never returns normally.
    """
    raise RuntimeError("ASYNC_TOOL_BOOM_MARKER")
