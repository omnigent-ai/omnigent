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
