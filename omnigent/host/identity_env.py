"""Environment-variable names a server-managed sandbox host launches with.

Leaf module with no third-party or heavy first-party imports. A process that
only needs these names — the warm-pool readiness probe in
:mod:`omnigent.host.warm_bootstrap`, which a warm Pod runs on a one-second
cadence — can read them without importing :mod:`omnigent.host.identity` and the
YAML/config machinery it pulls in.
"""

from __future__ import annotations

# The server provisions the sandbox, generates the identity + launch token, and
# injects all three so the host registers under the server-chosen identity
# without persisting anything to the sandbox's config.yaml (managed sandboxes
# are disposable). HOST_TOKEN is the tunnel credential (see
# MANAGED_HOST_TOKEN_HEADER in omnigent.host.identity); HOST_ID / HOST_NAME
# override the identity file and must be set together.
HOST_TOKEN_ENV_VAR = "OMNIGENT_HOST_TOKEN"
HOST_ID_ENV_VAR = "OMNIGENT_HOST_ID"
HOST_NAME_ENV_VAR = "OMNIGENT_HOST_NAME"
