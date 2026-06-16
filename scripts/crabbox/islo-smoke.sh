#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/common.sh"

cd "$ROOT"

smoke_dir="$CRABBOX_ARTIFACT_DIR/islo-smoke"
rm -rf "$smoke_dir"
mkdir -p "$smoke_dir"

proof="$smoke_dir/proof.txt"

log "running Islo provider smoke"
{
  echo "status=ok"
  echo "sentinel=CRABBOX_ISLO_PROVIDER_SMOKE_OK"
  echo "root=$ROOT"
  echo "pwd=$(pwd)"
  echo "uname=$(uname -a)"
  if command -v python3 >/dev/null 2>&1; then
    echo "python3=$(python3 --version 2>&1)"
  else
    echo "python3=missing"
  fi
  if command -v git >/dev/null 2>&1; then
    echo "git=$(git --version 2>&1)"
    echo "git_head=$(git rev-parse --short HEAD 2>/dev/null || true)"
  else
    echo "git=missing"
  fi
} | tee "$proof"

grep -q "CRABBOX_ISLO_PROVIDER_SMOKE_OK" "$proof"
