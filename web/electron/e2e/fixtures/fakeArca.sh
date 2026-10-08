#!/bin/sh
# Stand-in arca CLI: replays the remote output from the report's screenshot.
url=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--server" ]; then url=$(printf '%s' "$2" | tr -d "'"); fi
  shift
done
echo "Setting up the Omnigent runtime for \`isaac omni\` (first run, 25s)..."
sleep 1
echo "Version changed for eng-plugin-builder@eng-plugin-marketplace: 2.2.0 -> 2.3.4"
echo "Updated plugin: eng-plugin-builder@eng-plugin-marketplace"
echo "Ignoring non-boolean databricks.internaltools.omnigentDisableCredentialServiceFallback SAFE value: None" >&2
echo "Error: OMNIGENT_AUTH_REQUIRED: No usable Omnigent login for this server. Run \`isaac omni login '$url'\` on this machine, then retry." >&2
exit 1
