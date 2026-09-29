"""Lightweight timing state for cold host daemon bootstrap."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

_SERVER_TIMING_RE = re.compile(
    r"(?:^|,)\s*omnigent-host-auth\s*;\s*dur=([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE,
)


@dataclass
class HostStartupTiming:
    """Monotonic milestones from daemon claim through accepted upgrade."""

    claimed_ns: int = field(default_factory=time.monotonic_ns)
    marks_ns: dict[str, int] = field(default_factory=dict)
    reported: bool = False

    def mark(self, name: str) -> None:
        """Record or replace one milestone with the current monotonic time."""
        self.marks_ns[name] = time.monotonic_ns()

    def durations_ms(self, *, server_auth_ms: float | None = None) -> dict[str, float]:
        """Return bounded phase durations once an upgrade has been accepted."""

        def between(start: str | None, end: str) -> float | None:
            start_ns = self.claimed_ns if start is None else self.marks_ns.get(start)
            end_ns = self.marks_ns.get(end)
            if start_ns is None or end_ns is None:
                return None
            return max(0.0, (end_ns - start_ns) / 1_000_000)

        phases: dict[str, float] = {}
        candidates = {
            "identity_config": between(None, "identity_ready"),
            "daemon_record": between("identity_ready", "record_written"),
            "host_connect_import": between("connect_import_started", "connect_imported"),
            "connect_headers": between("attempt_started", "headers_ready"),
            "tls_context": between("headers_ready", "tls_ready"),
            "client_bootstrap": between(None, "upgrade_started"),
            "upgrade_wait": between("upgrade_started", "upgrade_accepted"),
            "claim_to_upgrade": between(None, "upgrade_accepted"),
        }
        for phase, duration in candidates.items():
            if duration is not None:
                phases[phase] = duration
        if server_auth_ms is not None and math.isfinite(server_auth_ms):
            bounded_server = max(0.0, server_auth_ms)
            upgrade_wait = phases.get("upgrade_wait")
            if upgrade_wait is not None:
                bounded_server = min(bounded_server, upgrade_wait)
                phases["network_handshake"] = max(0.0, upgrade_wait - bounded_server)
            phases["server_auth_upgrade"] = bounded_server
        return phases


def server_auth_timing_ms(headers: Mapping[str, str] | None) -> float | None:
    """Read the server's bounded pre-accept duration from ``Server-Timing``."""
    if headers is None:
        return None
    try:
        get_all = getattr(headers, "get_all", None)
        if callable(get_all):
            values = cast(list[str], get_all("Server-Timing"))
        else:
            value = headers.get("Server-Timing") or headers.get("server-timing")
            values = [value] if value else []
        for value in values:
            if not isinstance(value, str):
                continue
            match = _SERVER_TIMING_RE.search(value)
            if match is not None:
                duration = float(match.group(1))
                return duration if math.isfinite(duration) else None
    except Exception:  # noqa: BLE001 - optional timing cannot disrupt registration
        # Timing metadata is optional and must never disrupt registration.
        return None
    return None
