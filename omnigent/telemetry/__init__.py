"""Usage telemetry for the Omnigent server.

This package provides fire-and-forget product analytics.  Import the
top-level helpers rather than reaching into submodules directly:

    from omnigent.telemetry import emit, is_disabled

The :func:`emit` function accepts any event dataclass defined in
:mod:`omnigent.telemetry.events`.
"""

from __future__ import annotations

from omnigent.telemetry.client import emit, init_client, is_disabled
from omnigent.telemetry.routing import (
    SETTING_SUBAGENT_ROUTING,
    record_routing_decision,
    record_routing_setting_changed,
)

__all__ = [
    "SETTING_SUBAGENT_ROUTING",
    "emit",
    "init_client",
    "is_disabled",
    "record_routing_decision",
    "record_routing_setting_changed",
]
