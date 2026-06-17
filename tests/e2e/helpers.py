"""Shared helpers and constants for e2e tests.

The response-body extraction helpers (:func:`get_output_items`,
:func:`final_assistant_text`)
were originally duplicated in several terminal e2e files
(``test_terminal_async.py``, ``test_terminal_interactive.py``,
``test_terminal_hierarchy.py``, ``test_terminal.py``). Promoted here per the testing skill's
"shared helpers go in helpers.py, not conftest" rule. The older
copies remain inline for now; new files should import from here.

The module-level polling constants (:data:`POLL_INTERVAL_S`,
:data:`HEALTH_TIMEOUT_S`) live here so the session-scoped
``live_server`` fixture in :mod:`tests.e2e.conftest` and any
per-module health waits share a single source of truth.
"""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import Any

# Polling cadence for server-health and response-poll loops. 0.1s
# was empirically the sweet spot — tighter feedback than the prior
# 0.5s without measurable CPU cost on the e2e harness.
POLL_INTERVAL_S: float = 0.1

# Hard ceiling for "wait until /health returns 200" loops. 60s is
# generous: a healthy spawn returns 200 in <2s; the only reason to
# breach 60s is a hard failure, in which case the loop exits and
# the captured server log is surfaced. Bumped from 30s because under
# xdist -n 8 the cumulative startup cost of the sessions stack's new
# modules can exceed the prior budget on contended runners.
HEALTH_TIMEOUT_S: float = 60.0

_DATABRICKSCFG_PATH = Path.home() / ".databrickscfg"


def lookup_databricks_host(profile: str) -> str | None:
    """Return the workspace ``host`` for *profile* from
    ``~/.databrickscfg``.

    :param profile: The Databricks profile name to look up.
    :returns: The workspace host with any trailing ``/`` stripped,
        or ``None`` when the profile is absent from
        ``~/.databrickscfg`` or the section has no ``host`` key.
    """
    cfg = configparser.ConfigParser()
    if _DATABRICKSCFG_PATH.exists():
        cfg.read(_DATABRICKSCFG_PATH)
    host = cfg[profile].get("host") if profile in cfg else None
    return host.rstrip("/") if host else None


def get_output_items(
    body: dict[str, Any],
    item_type: str,
    name: str | None = None,
) -> list[dict[str, Any]]:
    """Filter ``response.output`` by type and optional tool name.

    :param body: Response body from ``GET /v1/responses/{id}``.
    :param item_type: Item type to keep, e.g. ``"function_call"``
        or ``"function_call_output"``.
    :param name: Optional tool-name filter, e.g. ``"sys_os_shell"``
        for function_call items. When ``None`` every item of the
        matching type is kept.
    :returns: Matching items in original order. Empty list if none
        match.
    """
    items = body.get("output", [])
    filtered = [i for i in items if i.get("type") == item_type]
    if name is not None:
        filtered = [i for i in filtered if i.get("name") == name]
    return filtered


def final_assistant_text(body: dict[str, Any]) -> str:
    """Concatenate every assistant message's ``output_text`` blocks.

    A single response may contain multiple assistant messages
    (one per iteration of the LLM loop); their text concatenated
    with double newlines is usually what the user sees. Used by
    tests that want the "final user-facing text" without worrying
    about which iteration produced it.

    :param body: Response body from ``GET /v1/responses/{id}``.
    :returns: The assistant's text content, ``"\\n\\n"``-joined
        across messages. Empty string if no assistant text is
        present.
    """
    parts: list[str] = []
    for item in body.get("output", []):
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        for block in item.get("content", []):
            if block.get("type") == "output_text":
                text = block.get("text")
                if text:
                    parts.append(text)
    return "\n\n".join(parts)
