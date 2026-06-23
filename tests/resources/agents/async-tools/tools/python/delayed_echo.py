"""delayed_echo fixture tool (slow; exercises async dispatch ↔ auto-delivery)."""

from __future__ import annotations

import time

from omnigent_client.tools import tool


@tool
def delayed_echo(label: str) -> str:
    """
    Sleep 2s, then echo ``label`` inside an unambiguous marker.

    The delay makes the dispatch -> auto-delivery sequence observable as
    distinct events; the marker is a distinctive substring so e2e
    assertions like ``"ECHO_FROM_ASYNC[..." in final_text`` are unambiguous.

    :param label: Text to echo back, e.g. ``"alpha"``.
    :returns: ``f"ECHO_FROM_ASYNC[{label}]"``.
    """
    time.sleep(2)
    return f"ECHO_FROM_ASYNC[{label}]"
