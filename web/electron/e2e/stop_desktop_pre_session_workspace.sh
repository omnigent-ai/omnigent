#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
source "$repo_root/web/electron/e2e/desktop_pre_session_workspace_processes.sh"
python="$repo_root/.venv/bin/python"
expect="/usr/bin/expect"
demo_root="$(desktop_demo_root)"
state_file="$demo_root/processes.env"
if [[ ! -e "$demo_root" && ! -L "$demo_root" ]]; then
  echo "No retained desktop demo is running."
  exit 0
fi
ensure_secure_demo_root "$demo_root"
if [[ ! -e "$state_file" && ! -L "$state_file" ]]; then
  echo "No retained desktop demo is running."
  exit 0
fi

load_demo_process_state "$state_file"
electron="$(cd "$repo_root/web/electron" && node -p "require('electron')")"
stop_demo_processes
rm -f -- "$state_file"
rmdir -- "$demo_root/launch.lock" 2>/dev/null || true
echo "Stopped retained desktop demo processes."
