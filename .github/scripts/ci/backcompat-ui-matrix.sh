#!/usr/bin/env bash
# Emit the UI backwards-compat matrix on $GITHUB_OUTPUT as `ui_matrix`.
#
# The UI matrix is server-only: for each final (non-prerelease) release tag
# at or above the backcompat floor, emit one cell per shard where the server
# is that release and the SPA + runner are both main. The runner axis is
# omitted because the SPA is always served by the server binary in production,
# so "new SPA vs old runner" is not a meaningful compat scenario for the UI.
#
# Env in:
#   VERSIONS    optional comma-separated override (e.g. "main,v0.9.0").
#               When set, only release tokens (non-"main") become cells.
#   NUM_SHARDS  e2e_ui shard count per cell (default 3, mirrors e2e-ui.yml).
# Out (GITHUB_OUTPUT):
#   ui_matrix={"include":[{"server":..,"shard_id":..,"num_shards":..}, ...]}

set -euo pipefail

_valid_version() {
  [ "$1" = "main" ] || [[ "$1" =~ ^v?[0-9]+\.[0-9]+(\.[0-9]+)?([a-z0-9.]*)?$ ]]
}

MIN_VERSION="${BACKCOMPAT_MIN_VERSION:-0.9.0}"
MIN_VERSION="${MIN_VERSION#v}"

_below_floor() {
  [ "$1" = "main" ] && return 1
  local v="${1#v}"
  [ "$v" = "$MIN_VERSION" ] && return 1
  [ "$(printf '%s\n%s\n' "$v" "$MIN_VERSION" | sort -V | head -1)" = "$v" ]
}

raw=()
if [ -n "${VERSIONS:-}" ]; then
  IFS=',' read -ra raw <<<"$VERSIONS"
else
  raw=()
  while IFS= read -r tag; do raw+=("$tag"); done < <(git tag --sort=-v:refname | grep -viE '(^|[^a-z])(rc|dev|pre)[0-9]')
fi

# Collect only release tokens (skip "main" — "main vs main" is the normal gate).
V=()
for v in "${raw[@]}"; do
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  [ -z "$v" ] && continue
  [ "$v" = "main" ] && continue
  if ! _valid_version "$v"; then
    echo "skipping invalid version token: '$v'" >&2; continue
  fi
  if _below_floor "$v"; then
    echo "skipping '$v': below UI backcompat floor $MIN_VERSION" >&2; continue
  fi
  V+=("$v")
done

num_shards="${NUM_SHARDS:-3}"

# Cap: each cell produces num_shards jobs; GitHub limits a matrix to 256.
max_ui=256
while [ "${#V[@]}" -gt 0 ] && [ "$(( ${#V[@]} * num_shards ))" -gt "$max_ui" ]; do
  dropped="${V[${#V[@]} - 1]}"
  unset 'V[${#V[@]}-1]'
  V=("${V[@]}")
  echo "ui-matrix cap: dropped oldest version '$dropped' to keep UI jobs <= $max_ui" >&2
done

items=()
for s in "${V[@]}"; do
  for ((i = 0; i < num_shards; i++)); do
    items+=("{\"server\":\"$s\",\"shard_id\":$i,\"num_shards\":$num_shards}")
  done
done

json=$(IFS=,; echo "${items[*]:-}")
echo "ui_matrix={\"include\":[$json]}" >>"${GITHUB_OUTPUT:-/dev/stdout}"
echo "versions: ${V[*]:-(none)}; UI jobs: ${#items[@]}" >&2
