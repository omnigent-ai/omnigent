#!/usr/bin/env bash

# Launch the installed local Omnigent + Hermes stack in ambient Hermes mode.
#
# This intentionally uses the machine-installed Omnigent and Hermes binaries
# instead of this checkout. Hermes credentials and provider config stay in the
# user's Hermes home; no secret-bearing files are copied into the repo.

set -euo pipefail

default_omni="/Users/spencer/.local/bin/omni"
default_hermes="/Users/spencer/.local/bin/hermes"

resolve_bin() {
  local env_name="$1"
  local default_path="$2"
  local primary_name="$3"
  local fallback_name="${4:-}"
  local requested="${!env_name:-}"

  if [ -n "$requested" ]; then
    if [ -x "$requested" ]; then
      printf '%s\n' "$requested"
      return 0
    fi
    printf 'ERROR: %s=%s is not executable.\n' "$env_name" "$requested" >&2
    return 1
  fi

  if [ -x "$default_path" ]; then
    printf '%s\n' "$default_path"
    return 0
  fi

  if command -v "$primary_name" >/dev/null 2>&1; then
    command -v "$primary_name"
    return 0
  fi

  if [ -n "$fallback_name" ] && command -v "$fallback_name" >/dev/null 2>&1; then
    command -v "$fallback_name"
    return 0
  fi

  printf 'ERROR: could not find %s. Set %s=/absolute/path.\n' "$primary_name" "$env_name" >&2
  return 1
}

omni_bin="$(resolve_bin OMNIGENT_CLI "$default_omni" omni omnigent)"
hermes_bin="$(resolve_bin OMNIGENT_HERMES_PATH "$default_hermes" hermes)"
omni_dir="$(dirname "$omni_bin")"
hermes_dir="$(dirname "$hermes_bin")"

export PATH="$omni_dir:$hermes_dir:$PATH"
export OMNIGENT_HERMES_PATH="${OMNIGENT_HERMES_PATH:-$hermes_bin}"

printf 'Omnigent CLI: %s\n' "$omni_bin"
"$omni_bin" --version
printf 'Hermes CLI:   %s\n' "$hermes_bin"
"$hermes_bin" --version | sed -n '1,4p'

if "$hermes_bin" config show 2>/dev/null | grep -q "qwen3-coder-next-local"; then
  printf 'Hermes model: qwen3-coder-next-local via ambient ~/.hermes config\n'
else
  printf 'WARN: Hermes config did not report qwen3-coder-next-local.\n' >&2
fi

if command -v curl >/dev/null 2>&1; then
  if curl -fsS --max-time 3 http://127.0.0.1:8080/v1/models >/dev/null 2>&1; then
    printf 'Local model endpoint: http://127.0.0.1:8080/v1/models reachable\n'
  else
    printf 'WARN: local model endpoint was not reachable before launch.\n' >&2
  fi
fi

printf 'Starting or reusing local Omnigent server...\n'
"$omni_bin" server start >/dev/null

printf 'Launching Hermes native UI through Omnigent. Press Ctrl-C or detach from tmux to exit.\n'
exec "$omni_bin" hermes --server "" "$@"
