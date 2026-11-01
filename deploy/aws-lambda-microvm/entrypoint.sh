#!/usr/bin/env bash
# MicroVM container entrypoint. Starts the lifecycle-hooks server in the
# foreground and lets it own host startup. The host is NOT started here:
# RunMicrovm carries no per-launch environment, so the host identity + token
# arrive later as the body of the /run lifecycle hook (see hooks_server.py).
# At build time the platform boots this image, calls /ready, and snapshots a
# warm image parked on the hooks port; each launch thaws that snapshot and
# fires /run, which spawns start_host.sh with the delivered identity.
set -euo pipefail

# The hooks server is the container's main process: it serves /ready and the
# runtime lifecycle probes for the life of the microVM, and on /run it spawns
# start_host.sh with the identity payload.
exec python3 /opt/omnigent/hooks_server.py
