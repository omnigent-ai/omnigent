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
POLICY_REGISTRY = [
    {**entry, "handler": entry["handler"].replace(_NEW, _OLD), "internal_only": True}
    for entry in _new_registry
]
