#!/usr/bin/env bash
set -euo pipefail

demo_root="/private/tmp/omnigent-prechat-manual-demo"
state_file="$demo_root/processes.env"
if [[ ! -f "$state_file" ]]; then
  echo "No retained desktop demo is running."
  exit 0
fi

# shellcheck disable=SC1090
source "$state_file"
for pid in "$electron_pid" "$omnidev_pid" "$page_pid" "$mock_pid"; do
  kill "$pid" 2>/dev/null || true
done
# Omnidev's supervised children outlive the TUI wrapper if the launcher is
# interrupted. Every command below is rooted in this disposable demo path.
pkill -f "$demo_root" 2>/dev/null || true
rm -f "$state_file"
echo "Stopped retained desktop demo processes."
