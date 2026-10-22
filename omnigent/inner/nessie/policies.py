"""Backward-compat shim — policy handler paths in deployed configs still reference
``omnigent.inner.nessie.policies.*``.  Real implementation lives at
``omnigent.policies.builtins.orchestration``.
"""

from omnigent.policies.builtins.orchestration import *  # noqa: F403
from omnigent.policies.builtins.orchestration import POLICY_REGISTRY as _new_registry

# Re-advertise under the legacy handler paths so the policy registry accepts
# bundles that were deployed before the module was renamed.
_OLD = "omnigent.inner.nessie.policies."
_NEW = "omnigent.policies.builtins.orchestration."


def _legacy_entry(entry: dict[str, object]) -> dict[str, object]:
    handler = entry.get("handler")
    if not isinstance(handler, str):
        raise TypeError("policy registry handler must be a string")
    return {**entry, "handler": handler.replace(_NEW, _OLD), "internal_only": True}


POLICY_REGISTRY = [_legacy_entry(entry) for entry in _new_registry]
