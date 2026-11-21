"""Timeout contract for native Codex startup and live settings forwarding."""

# A loaded host can take over 10 seconds to start its session app-server.
# Match the native terminal's startup allowance; an early exit fails at once.
APP_SERVER_READY_TIMEOUT_SECONDS = 60.0

# Thread discovery starts after terminal creation, including cold-resume repair.
SETTINGS_BRIDGE_READY_TIMEOUT_SECONDS = 15.0

# Preserve 25 seconds for settings RPCs, confirmation mirrors, and cleanup after
# the bounded launch and bridge-discovery phases finish.
SETTINGS_FORWARD_TIMEOUT_SECONDS = (
    APP_SERVER_READY_TIMEOUT_SECONDS + SETTINGS_BRIDGE_READY_TIMEOUT_SECONDS + 25.0
)
