#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "$repo_root"

# Keep provider discovery away from the developer's OS keychain. Any fallback
# file remains inside the per-run OMNIGENT_CONFIG_HOME created by the test.
export OMNIGENT_DISABLE_KEYRING=1

pnpm install --frozen-lockfile
uv sync --frozen --extra all --group dev
cargo build --manifest-path dev/omnidev/Cargo.toml --locked --release
pnpm --filter web run build

cd web/electron
node --test e2e/desktop_pre_session_workspace.e2e.js
