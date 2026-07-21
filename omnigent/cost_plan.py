"""Cost-control label namespace shared between the server and runner.

Defines the label-key prefix that the server reserves for policy-owned
cost-control metadata, and the helper that identifies which keys in a
client-supplied label map fall under that namespace.
"""

from __future__ import annotations

from collections.abc import Mapping

# Label-key prefix of the policy-owned cost-control namespace. Labels
# under it are runner-written telemetry; the server rejects them in
# client-supplied label writes (see ``update_session`` /
# ``create_session`` in :mod:`omnigent.server.routes.sessions`).
COST_CONTROL_LABEL_NAMESPACE = "cost_control."

# Label key (under the reserved namespace) the session's bound runner sets
# when the native harness authenticates via a flat-rate SUBSCRIPTION
# (ChatGPT/Codex or Claude Pro/Max) rather than pay-per-use API credentials.
# Value ``"true"`` marks the session subscription-covered: token usage is still
# recorded for display, but the server prices the cost at $0 rather than at
# per-token catalog rates (real marginal spend is ~$0), so the cost-budget gate
# never fires on a phantom figure. A present $0 (not a missing cost key) is
# deliberate — it also keeps the gate's "unpriced model" ASK/DENY from firing.
# It shares the runner-authority write path of the rest of the namespace, so a
# session owner cannot forge it to disable their own budget.
COST_CONTROL_UNPRICED_LABEL_KEY = COST_CONTROL_LABEL_NAMESPACE + "unpriced"

# Stored string value that turns the unpriced marker on. Any other value
# (or an absent key) leaves the session priced.
COST_CONTROL_UNPRICED_LABEL_VALUE = "true"


def cost_unpriced_label_set(labels: Mapping[str, str]) -> bool:
    """
    Return whether *labels* marks the session as subscription-unpriced.

    :param labels: A conversation label mapping, e.g.
        ``{"cost_control.unpriced": "true", "team": "ml"}``.
    :returns: ``True`` only when :data:`COST_CONTROL_UNPRICED_LABEL_KEY`
        is present with value :data:`COST_CONTROL_UNPRICED_LABEL_VALUE`.
    """
    return labels.get(COST_CONTROL_UNPRICED_LABEL_KEY) == COST_CONTROL_UNPRICED_LABEL_VALUE


def reserved_cost_control_keys(labels: Mapping[str, str]) -> tuple[str, ...]:
    """
    Return the policy-owned ``cost_control.*`` keys present in *labels*.

    :param labels: A label mapping from a client request body, e.g.
        ``{"cost_control.plan": "{...}", "team": "ml"}``.
    :returns: The keys under :data:`COST_CONTROL_LABEL_NAMESPACE`, in
        mapping order, e.g. ``("cost_control.plan",)``. Empty when the
        mapping touches no reserved keys.
    """
    return tuple(key for key in labels if key.startswith(COST_CONTROL_LABEL_NAMESPACE))
