"""Product approval: a feature someone other than the owner asked for waits for a product OK.

Every ticket carries a ``kind`` label: ``fix``, ``perf``, ``ux`` or ``feature``. The
orchestrator sets it at dispatch and the Reviewer confirms or corrects it. A
``feature`` whose requester isn't the project owner lands only once a product
approver (the owner, plus the project's ``product_approvers`` config) has added
``product-ok`` themselves. Agents can never add it.
"""

from __future__ import annotations

import re
from typing import Any

from omnigent.server.board_readers import BOARD_READERS_CONFIG_KEY

KIND_LABEL = "kind"
KINDS = ("fix", "perf", "ux", "feature")
FEATURE_KIND = "feature"
PRODUCT_OK_LABEL = "product-ok"
APPROVERS_CONFIG_KEY = "product_approvers"
# Project config keys only the owner may change, by hand.
OWNER_ONLY_CONFIG_KEYS = ("landing", APPROVERS_CONFIG_KEY, BOARD_READERS_CONFIG_KEY)


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
REQUEST_RISK_OK = "risk-ok"
REQUEST_OWNER = "owner"


def human_request_routing(
    project: Any, labels: list[dict[str, Any]] | None, requested_by: str | None = None
) -> tuple[str, set[str | None]]:
    """Who answers a ticket's ``needs-human`` ask, and why.

    A feature that still needs ``product-ok`` (:func:`needs_product_ok`: not the owner's
    own) waits on any product approver (the owner plus ``product_approvers``). A ``risk``
    ticket still missing ``risk-ok`` also waits on whoever may give it
    (:func:`risk_ok_givers`). Every other ask waits on the owner.
    """
    keys = {label.get("key") for label in labels or []}
    ticket = {"labels": labels or [], "requested_by": requested_by}
    responders: set[str | None] = set()
    reason = REQUEST_OWNER
    if RISK_LABEL in keys and RISK_OK_LABEL not in keys:
        reason = REQUEST_RISK_OK
        responders |= risk_ok_givers(project, requested_by)
    if needs_product_ok(project, ticket) and PRODUCT_OK_LABEL not in keys:
        reason = REQUEST_PRODUCT_OK
        responders |= {*product_approvers(project), project.user_id}
    return reason, responders or {project.user_id}


def is_plain_ask(
    project: Any, labels: list[dict[str, Any]] | None, requested_by: str | None
) -> bool:
    """Whether a ``needs-human`` ask is a plain question rather than a sign-off.

    It is one when :func:`human_request_routing` routes it to nobody for a sign-off
    (no missing ``product-ok`` on a feature, no missing ``risk-ok``) and no
    ``owner-ok`` is owed.
    """
    keys = {label.get("key") for label in labels or []}
    if ticket_kind(labels) == FEATURE_KIND and PRODUCT_OK_LABEL not in keys:
        return False
    if RISK_LABEL in keys and RISK_OK_LABEL not in keys:
        return False
    return not owner_sign_off_pending(labels, requested_by)


_PRODUCT_OK_ASK = re.compile(r"product[\s-]*ok|product\s+(?:approv|sign[\s-]*off)", re.I)


def asks_for_product_ok(*texts: str | None) -> bool:
    """Whether an ask's question or label asks for product approval, not just a question."""
    return any(text and _PRODUCT_OK_ASK.search(text) for text in texts)


def only_product_ok_owed(labels: list[dict[str, Any]] | None, requested_by: str | None) -> bool:
    """Whether a feature's missing ``product-ok`` is the only sign-off its ask could be for."""
    keys = {label.get("key") for label in labels or []}
    return (
        ticket_kind(labels) == FEATURE_KIND
        and PRODUCT_OK_LABEL not in keys
        and not (RISK_LABEL in keys and RISK_OK_LABEL not in keys)
        and not owner_sign_off_pending(labels, requested_by)
    )


# The owner's own sign-offs; mirrors the landing gate's labels.
RISK_LABEL = "risk"
RISK_OK_LABEL = "risk-ok"
OWNER_OK_LABEL = "owner-ok"


def owner_sign_off_pending(labels: list[dict[str, Any]] | None, requested_by: str | None) -> bool:
    """Whether the ticket still waits on a sign-off only the owner can give.

    That is ``owner-ok`` on an imported outside PR. ``risk-ok`` can also come from a
    product approver; see :func:`risk_ok_givers`.
    """
    from omnigent.server.github_pr_intake import is_outside_requester

    keys = {label.get("key") for label in labels or []}
    return is_outside_requester(requested_by) and OWNER_OK_LABEL not in keys


def pending_sign_offs(
    project: Any, labels: list[dict[str, Any]] | None, requested_by: str | None
) -> list[str]:
    """The sign-off labels a ticket still waits on: ``risk-ok``, ``product-ok``, ``owner-ok``."""
    from omnigent.server.github_pr_intake import is_outside_requester

    keys = {label.get("key") for label in labels or []}
    ticket = {"labels": labels or [], "requested_by": requested_by}
    pending = []
    if RISK_LABEL in keys and RISK_OK_LABEL not in keys:
        pending.append(RISK_OK_LABEL)
    if needs_product_ok(project, ticket) and PRODUCT_OK_LABEL not in keys:
        pending.append(PRODUCT_OK_LABEL)
    if is_outside_requester(requested_by) and OWNER_OK_LABEL not in keys:
        pending.append(OWNER_OK_LABEL)
    return pending


def risk_ok_givers(project: Any, requested_by: str | None) -> set[str | None]:
    """Who may give ``risk-ok``: the owner, and product approvers on tickets they didn't request.

    Keeping the requester out means nobody but the owner signs off risk on their own change.
    """
    givers: set[str | None] = {
        approver for approver in product_approvers(project) if approver != requested_by
    }
    givers.add(project.user_id)
    return givers


def risk_ok_refusal(project: Any, requested_by: str | None) -> str:
    """Why a person can't give ``risk-ok`` on this ticket, and who can."""
    givers = sorted(giver for giver in risk_ok_givers(project, requested_by) if giver)
    return (
        f"Only the project owner or a product approver who didn't request this ticket can add "
        f"`{RISK_OK_LABEL}`" + (f": {', '.join(givers)}" if givers else "")
    )


def sign_offs_for(
    project: Any, ticket: dict[str, Any], viewer: str | None
) -> list[dict[str, Any]]:
    """The sign-off labels a ticket's open ``needs-human`` ask waits on that *viewer* may add.

    Each is ``{"key", "value"}`` as it would be added: ``risk-ok`` on a ``risk`` ticket for
    :func:`risk_ok_givers`, ``owner-ok`` (bound to the approved SHA) on an outside PR for the
    owner, and ``product-ok`` on another requester's feature for product approvers.
    """
    from omnigent.server.github_pr_intake import is_outside_requester

    labels = ticket.get("labels") or []
    values = {label.get("key"): label.get("value") for label in labels}
    if "needs-human" not in values:
        return []
    # Single-user servers have no user ID: there the one person is the owner.
    owner = viewer == project.user_id
    offers: list[dict[str, Any]] = []
    if (
        RISK_LABEL in values
        and RISK_OK_LABEL not in values
        and viewer in risk_ok_givers(project, ticket.get("requested_by"))
    ):
        offers.append({"key": RISK_OK_LABEL, "value": None})
    if (
        needs_product_ok(project, ticket)
        and PRODUCT_OK_LABEL not in values
        and (owner or viewer in product_approvers(project))
    ):
        offers.append({"key": PRODUCT_OK_LABEL, "value": None})
    approved = values.get("approved")
    if (
        owner
        and is_outside_requester(ticket.get("requested_by"))
        and OWNER_OK_LABEL not in values
        and approved
    ):
        offers.append({"key": OWNER_OK_LABEL, "value": approved})
    return offers
