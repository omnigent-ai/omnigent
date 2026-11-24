"""Product approval: a feature someone other than the owner asked for waits for a product OK.

Every ticket carries a ``kind`` label: ``fix``, ``perf``, ``ux`` or ``feature``. The
orchestrator sets it at dispatch and the Reviewer confirms or corrects it. A
``feature`` whose requester isn't the project owner lands only once a product
approver (the owner, plus the project's ``product_approvers`` config) has added
``product-ok`` themselves. Agents can never add it.
"""

from __future__ import annotations

from typing import Any

KIND_LABEL = "kind"
KINDS = ("fix", "perf", "ux", "feature")
FEATURE_KIND = "feature"
PRODUCT_OK_LABEL = "product-ok"
APPROVERS_CONFIG_KEY = "product_approvers"
# Project config keys only the owner may change, by hand.
OWNER_ONLY_CONFIG_KEYS = ("landing", APPROVERS_CONFIG_KEY)


def product_approvers(project: Any) -> set[str]:
    """The people whose own ``product-ok`` counts: the owner plus configured approvers."""
    config = getattr(project, "config", None) or {}
    configured = config.get(APPROVERS_CONFIG_KEY)
    approvers = {
        entry.strip()
        for entry in (configured if isinstance(configured, list) else [])
        if isinstance(entry, str) and entry.strip()
    }
    if project.user_id is not None:
        approvers.add(project.user_id)
    return approvers


def ticket_kind(labels: list[dict[str, Any]] | None) -> str | None:
    for label in labels or []:
        if label.get("key") == KIND_LABEL:
            value = (label.get("value") or "").strip().lower()
            return value or None
    return None


def requested_by_owner(project: Any, ticket: dict[str, Any]) -> bool:
    return ticket.get("requested_by") == project.user_id


def product_gate_problem(project: Any, ticket: dict[str, Any]) -> str | None:
    """Why the ticket's kind blocks landing before any product OK, or None.

    The owner's own tickets are never gated. For anyone else the kind must be set,
    and a feature then needs ``product-ok``, which the caller checks.
    """
    if requested_by_owner(project, ticket):
        return None
    kind = ticket_kind(ticket.get("labels"))
    if kind not in KINDS:
        return (
            f"The ticket needs a `{KIND_LABEL}` label ({', '.join(KINDS)}) before it lands, "
            "since the owner didn't request it; the orchestrator or Reviewer sets it"
        )
    return None


def needs_product_ok(project: Any, ticket: dict[str, Any]) -> bool:
    return (
        not requested_by_owner(project, ticket)
        and ticket_kind(ticket.get("labels")) == FEATURE_KIND
    )
