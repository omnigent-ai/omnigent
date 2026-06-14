#!/usr/bin/env bash
set -euo pipefail

ROOT=${ROOT:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}
export ROOT

export CI=${CI:-1}
export OMNIGENT_SKIP_WEB_UI=${OMNIGENT_SKIP_WEB_UI:-true}
export UV_INDEX_URL=${UV_INDEX_URL:-https://pypi.org/simple}
export PIP_INDEX_URL=${PIP_INDEX_URL:-https://pypi.org/simple}
export PYTHONFAULTHANDLER=${PYTHONFAULTHANDLER:-1}
export TERM=${TERM:-xterm-256color}

CRABBOX_ARTIFACT_DIR=${CRABBOX_ARTIFACT_DIR:-"$ROOT/artifacts/crabbox"}
export CRABBOX_ARTIFACT_DIR

mkdir -p "$CRABBOX_ARTIFACT_DIR"

log() {
  printf '[crabbox] %s\n' "$*"
}

die() {
  printf '[crabbox] error: %s\n' "$*" >&2
  exit 1
}

need_command() {
  local name=$1
  local hint=${2:-}
  if ! command -v "$name" >/dev/null 2>&1; then
    if [ -n "$hint" ]; then
      die "missing '$name'. $hint"
    fi
    die "missing '$name'"
  fi
}

maybe_install_apt_packages() {
  if ! command -v apt-get >/dev/null 2>&1 || ! command -v sudo >/dev/null 2>&1; then
    return
  fi

  local packages=()
  for package in "$@"; do
    case "$package" in
      bubblewrap)
        command -v bwrap >/dev/null 2>&1 || packages+=("$package")
        ;;
      ripgrep)
        command -v rg >/dev/null 2>&1 || packages+=("$package")
        ;;
      *)
        command -v "$package" >/dev/null 2>&1 || packages+=("$package")
        ;;
    esac
  done

  if [ "${#packages[@]}" -eq 0 ]; then
    return
  fi

  log "installing apt packages: ${packages[*]}"
  sudo apt-get update
  sudo apt-get install -y "${packages[@]}"
}

prepare_linux_sandbox() {
  if [ "$(uname -s)" != "Linux" ]; then
    return
  fi

  maybe_install_apt_packages tmux ripgrep bubblewrap

  if command -v sudo >/dev/null 2>&1 && [ -e /proc/sys/kernel/apparmor_restrict_unprivileged_userns ]; then
    sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 || true
  fi
}

ensure_python_toolchain() {
  need_command uv "Run this job with Crabbox Actions hydration, or install uv on the target box."
  uv --version
}

ensure_node_toolchain() {
  need_command node "Run this job with Crabbox Actions hydration, or install Node.js on the target box."
  need_command npm "Run this job with Crabbox Actions hydration, or install npm on the target box."
  node --version
  npm --version
}

sync_python_deps() {
  ensure_python_toolchain
  log "syncing Python dependencies"
  uv sync --extra all --extra dev
}

install_ci_binaries() {
  ensure_node_toolchain
  log "installing harness CLI dependencies"
  (
    cd "$ROOT/.github/ci-deps"
    npm install --ignore-scripts
    node node_modules/@anthropic-ai/claude-code/install.cjs
  )
  export PATH="$ROOT/.github/ci-deps/node_modules/.bin:$PATH"
}

write_databricks_profile() {
  if [ -z "${LLM_API_KEY:-}" ]; then
    die "LLM_API_KEY is required for live Omnigent E2E jobs."
  fi
  if [ -z "${GATEWAY_BASE_URL:-}" ]; then
    die "GATEWAY_BASE_URL is required for live Omnigent E2E jobs."
  fi

  local host=${GATEWAY_BASE_URL%/serving-endpoints}
  mkdir -p "$HOME"
  cat > "$HOME/.databrickscfg" <<EOF
[default]
host  = $host
token = $LLM_API_KEY
EOF
  export DATABRICKS_BEARER=${DATABRICKS_BEARER:-$LLM_API_KEY}
}

prepare_backend() {
  cd "$ROOT"
  prepare_linux_sandbox
  sync_python_deps
}

prepare_live_backend() {
  prepare_backend
  install_ci_binaries
  write_databricks_profile
}
