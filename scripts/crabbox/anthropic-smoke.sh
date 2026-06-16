#!/usr/bin/env bash
set -euo pipefail

source "$(dirname "$0")/common.sh"

cd "$ROOT"

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  die "ANTHROPIC_API_KEY is required for the Anthropic Omnigent smoke job."
fi

prepare_backend
install_ci_binaries

smoke_dir="$CRABBOX_ARTIFACT_DIR/anthropic-smoke"
rm -rf "$smoke_dir"
mkdir -p "$smoke_dir"

agent_yaml="$smoke_dir/anthropic-smoke.yaml"
output="$smoke_dir/output.txt"
proof="$smoke_dir/proof.txt"
marker="OMNIGENT_ANTHROPIC_SMOKE_OK"
model="${OMNIGENT_ANTHROPIC_SMOKE_MODEL:-claude-sonnet-4-6}"

cat > "$agent_yaml" <<EOF
name: anthropic-smoke
prompt: |
  You are a smoke-test agent. Reply with the requested marker exactly.
executor:
  harness: claude-sdk
  model: $model
  auth:
    type: api_key
    api_key: \${ANTHROPIC_API_KEY}
    base_url: https://api.anthropic.com
os_env:
  type: caller_process
  sandbox:
    type: none
EOF

log "running Anthropic Omnigent smoke with model $model"
env -u OPENAI_API_KEY -u OPENAI_BASE_URL -u DATABRICKS_TOKEN -u DATABRICKS_BEARER \
  uv run omnigent run "$agent_yaml" --no-session -p "Reply exactly: $marker" \
  >"$output" 2>&1

if ! grep -q "$marker" "$output"; then
  sed -n '1,200p' "$output" >&2
  die "Anthropic smoke did not emit $marker."
fi

{
  echo "status=ok"
  echo "sentinel=$marker"
  echo "model=$model"
  echo "output=$output"
} | tee "$proof"
