"""Operator-configurable idle timeouts shared by the harness executors.

The ACP and Qwen executors read their ``HARNESS_<NAME>_PROMPT_TIMEOUT_S``
override at import time, so a bad value aborts the harness child at startup
instead of surfacing mid-turn as a mysterious deadline.
"""

from __future__ import annotations

import math
import os


def timeout_seconds_from_env(name: str, default: float) -> float:
    """Return the timeout in seconds set by environment variable ``name``.

    :param name: Environment variable holding the override.
    :param default: Seconds used when the variable is unset.
    :returns: The parsed override, or ``default`` when ``name`` is unset.
    :raises ValueError: When the variable is set to anything other than a
        positive finite number, including an empty string.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    error = f"{name} must be a positive finite number of seconds"
    try:
        seconds = float(raw)
    except ValueError as exc:
        raise ValueError(error) from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError(error)
    return seconds
