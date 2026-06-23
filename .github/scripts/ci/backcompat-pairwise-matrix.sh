#!/usr/bin/env bash
# Emit the FULL pairwise (server, runner) backwards-compat matrices on
# $GITHUB_OUTPUT as `e2e_matrix` and `integration_matrix`.
#
# The version universe is `main` (the checked-out code = client + tests, always)
# plus every non-rc release tag AT OR ABOVE the backcompat floor (MIN_VERSION,
# default 0.2.0 — the first release with the mock-LLM e2e infra; see below).
# We cross every server version with every runner
# version — each cell pins the server and/or runner subprocess to that build
# (an empty/"main" value leaves that component on the checked-out code). The
# (main, main) cell is omitted: it pins nothing and is exactly the normal e2e
# gate. Integration is the single openai-agents leg (claude-sdk/codex reject the
# mock LLM's "mock-model" — see integration-matrix.sh), crossed with the pairs.
#
# Env in:
#   VERSIONS    optional comma-separated override of the version set used for
#               BOTH axes (e.g. "main,v0.2.0"). Empty = main + all non-rc tags.
#               Blank entries are dropped and surrounding whitespace trimmed.
#   NUM_SHARDS  e2e shard count per cell (default 4).
# Out (GITHUB_OUTPUT):
#   e2e_matrix={"include":[{"server":..,"runner":..,"shard_id":..,"num_shards":..}, ...]}
#   integration_matrix={"include":[{"server":..,"runner":..,"harness":..,"model":..,"workers":..}, ...]}

set -euo pipefail

# A version token is "main" or a release tag (vX.Y[.Z][pre/dev suffix]). Anything
# else is rejected so it can't break the matrix JSON or reach a `git worktree add`.
_valid_version() {
  [ "$1" = "main" ] || [[ "$1" =~ ^v?[0-9]+\.[0-9]+(\.[0-9]+)?([a-z0-9.]*)?$ ]]
}

# Minimum release the backcompat matrix tests against. v0.2.0 is the first
# release with the mock-LLM e2e infrastructure (tests/e2e/conftest.py has 0
# mock-LLM refs at v0.1.x, 31 at v0.2.0) AND the runner-side harness mock
# routing — empirically, main's mock-based e2e suite 401s ("Incorrect API key
# provided: mock-key") against v0.1.0/v0.1.1 server+runner builds, so those
# pairs are guaranteed-red infrastructure mismatch, not a compat signal.
# `main` is the dev tip and always sorts above any release, so it is never
# floored. Override with BACKCOMPAT_MIN_VERSION (e.g. "0.0.0" to disable).
# Strip a leading "v" so a "v0.2.0"-style override compares cleanly against the
# v-stripped tags in _below_floor (without this, the floor version itself would
# be dropped).
MIN_VERSION="${BACKCOMPAT_MIN_VERSION:-0.2.0}"
MIN_VERSION="${MIN_VERSION#v}"

# True (0) when release tag $1 is older than MIN_VERSION (by PEP-440-ish release
# order). "main" is never below the floor. Compares the numeric tuple via
# `sort -V` after stripping the leading "v".
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
  raw=("main")
  # `[^a-z]rc[0-9]` so we drop vX.Y.ZrcN without over-excluding tags that merely
  # contain the substring "rc" (e.g. a hypothetical "...march").
  while IFS= read -r tag; do raw+=("$tag"); done < <(git tag --sort=-v:refname | grep -viE '(^|[^a-z])rc[0-9]')
fi

# Trim whitespace, drop blanks, reject invalid tokens, drop below-floor releases.
V=()
for v in "${raw[@]}"; do
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  [ -z "$v" ] && continue
  if ! _valid_version "$v"; then
    echo "skipping invalid version token: '$v'" >&2
    continue
  fi
  if _below_floor "$v"; then
    echo "skipping '$v': below backcompat floor $MIN_VERSION (predates the mock-LLM e2e infra)" >&2
    continue
  fi
  V+=("$v")
done

num_shards="${NUM_SHARDS:-4}"

# GitHub caps a matrix at 256 jobs. e2e jobs = (|V|² − [main present]) × shards.
# If we'd exceed it, drop the OLDEST versions (V is newest-first in auto mode)
# until under, logging each drop — never silently truncate.
_pairs() {
  local n=${#V[@]} mm=0 x
  for x in "${V[@]}"; do [ "$x" = "main" ] && mm=1 && break; done
  echo "$((n * n - mm))"
}
max_e2e=256
while [ "${#V[@]}" -gt 2 ] && [ "$(($(_pairs) * num_shards))" -gt "$max_e2e" ]; do
  dropped="${V[${#V[@]} - 1]}"
  unset 'V[${#V[@]}-1]'
  V=("${V[@]}")
  echo "version-matrix cap: dropped oldest version '$dropped' to keep e2e jobs <= $max_e2e" >&2
done

# The integration suite runs a single openai-agents leg in mock mode (matches
# integration-matrix.sh); the model name is unused under the mock LLM.
integ_harness="openai-agents"
integ_model="databricks-gpt-5-4-mini"
integ_workers="4"

e2e_items=()
integ_items=()
for s in "${V[@]}"; do
  for r in "${V[@]}"; do
    # Skip the all-main cell: it pins nothing (== the normal e2e gate).
    if [ "$s" = "main" ] && [ "$r" = "main" ]; then
      continue
    fi
    integ_items+=("{\"server\":\"$s\",\"runner\":\"$r\",\"harness\":\"$integ_harness\",\"model\":\"$integ_model\",\"workers\":$integ_workers}")
    for ((i = 0; i < num_shards; i++)); do
      e2e_items+=("{\"server\":\"$s\",\"runner\":\"$r\",\"shard_id\":$i,\"num_shards\":$num_shards}")
    done
  done
done

e2e_json=$(
  IFS=,
  echo "${e2e_items[*]:-}"
)
integ_json=$(
  IFS=,
  echo "${integ_items[*]:-}"
)

{
  echo "e2e_matrix={\"include\":[$e2e_json]}"
  echo "integration_matrix={\"include\":[$integ_json]}"
} >>"${GITHUB_OUTPUT:-/dev/stdout}"

echo "versions: ${V[*]:-(none)}" >&2
echo "pairs: ${#integ_items[@]} (excludes main/main); e2e jobs: ${#e2e_items[@]}; integration jobs: ${#integ_items[@]}" >&2
