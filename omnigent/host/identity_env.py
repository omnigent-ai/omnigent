"""Environment-variable names a server-managed sandbox host launches with.

Leaf module with no third-party or heavy first-party imports. A process that
only needs these names — the warm-pool readiness probe in
:mod:`omnigent.host.warm_bootstrap`, which a warm Pod runs on a one-second
cadence — can read them without importing :mod:`omnigent.host.identity` and the
YAML/config machinery it pulls in.
"""

from __future__ import annotations

import os

# The server provisions the sandbox, generates the identity + launch token, and
# injects all three so the host registers under the server-chosen identity
# without persisting anything to the sandbox's config.yaml (managed sandboxes
# are disposable). HOST_TOKEN is the tunnel credential (see
# MANAGED_HOST_TOKEN_HEADER in omnigent.host.identity); HOST_ID / HOST_NAME
# override the identity file and must be set together.
HOST_TOKEN_ENV_VAR = "OMNIGENT_HOST_TOKEN"
HOST_ID_ENV_VAR = "OMNIGENT_HOST_ID"
HOST_NAME_ENV_VAR = "OMNIGENT_HOST_NAME"

# Set by the managed-sandbox host image/Pod spec, never on a person's own host.
# Claude Code's devcontainer convention sets it too, so such a devcontainer is
# treated as managed, like the git and Databricks credential gates already do.
MANAGED_SANDBOX_ENV_VAR = "IS_SANDBOX"


def in_managed_sandbox() -> bool:
    """Return whether this process runs inside a server-managed sandbox host."""
    return (os.environ.get(MANAGED_SANDBOX_ENV_VAR) or "").strip() == "1"
