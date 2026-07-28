"""Structured audit trail for operator-relevant host mutations.

Host shutdowns and permission grant/revoke changes are security-relevant
actions taken by one principal against a resource that affects others,
so they are recorded with actor + target on a dedicated logger
(``omnigent.audit``) as single-line JSON. Operators grep the server log
for ``audit:`` (or filter the logger name) to reconstruct who did what,
when — no new storage, works on every deploy target.
"""

from __future__ import annotations

import json
import logging

from omnigent.db.utils import now_epoch

_audit_logger = logging.getLogger("omnigent.audit")


def audit_event(action: str, *, actor: str | None, target: str, **fields: object) -> None:
    """Record one audit entry.

    :param action: What happened, e.g. ``"host.shutdown"``,
        ``"host.permission.grant"``.
    :param actor: The authenticated principal that performed it, or
        ``None`` when auth is disabled (recorded as ``"local"``).
    :param target: The resource acted on, e.g. ``"host_a1b2c3d4..."``.
    :param fields: Extra action-specific context, e.g.
        ``principal="bob@example.com", level="use"``.
    """
    entry: dict[str, object] = {
        "action": action,
        "actor": actor if actor is not None else "local",
        "target": target,
        "ts": now_epoch(),
        **fields,
    }
    _audit_logger.info("audit: %s", json.dumps(entry, sort_keys=True, default=str))
