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


def approver_list(project: Any) -> list[str]:
    """:func:`product_approvers` in a stable order to show people: the owner first."""
    others = sorted(product_approvers(project) - {project.user_id})
    return [project.user_id, *others] if project.user_id is not None else others


def approver_refusal(project: Any) -> str:
    """Why a non-approver can't add ``product-ok``, and how to become one."""
    owner = project.user_id or "the project owner"
    return (
        f"Only product approvers can add `{PRODUCT_OK_LABEL}`: "
        f"{', '.join(approver_list(project)) or 'none are set'}. "
        f"To approve, ask {owner} to add you under Product approvers in project settings"
    )


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


# Why a ticket's open ``needs-human`` ask is routed to the people it is.
REQUEST_PRODUCT_OK = "product-ok"
REQUEST_OWNER = "owner"


def human_request_routing(
    project: Any, labels: list[dict[str, Any]] | None
) -> tuple[str, set[str | None]]:
    """Who answers a ticket's ``needs-human`` ask, and why.

    A feature still missing ``product-ok`` waits on any product approver (the owner
    plus ``product_approvers``); every other ask, including ``risk-ok``, waits on the owner.
    """
    keys = {label.get("key") for label in labels or []}
    if ticket_kind(labels) == FEATURE_KIND and PRODUCT_OK_LABEL not in keys:
        return REQUEST_PRODUCT_OK, {*product_approvers(project), project.user_id}
    return REQUEST_OWNER, {project.user_id}


# The owner's own sign-offs; mirrors the landing gate's labels.
RISK_LABEL = "risk"
RISK_OK_LABEL = "risk-ok"
OWNER_OK_LABEL = "owner-ok"


def owner_sign_off_pending(labels: list[dict[str, Any]] | None, requested_by: str | None) -> bool:
    """Whether the ticket still waits on a sign-off only the owner can give.

    That is ``risk-ok`` on a ``risk`` ticket, or ``owner-ok`` on an imported outside PR.
    """
    from omnigent.server.github_pr_intake import is_outside_requester

    keys = {label.get("key") for label in labels or []}
    return (RISK_LABEL in keys and RISK_OK_LABEL not in keys) or (
        is_outside_requester(requested_by) and OWNER_OK_LABEL not in keys
    )
