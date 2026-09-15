#!/usr/bin/env bash
set -euo pipefail

invocation_cwd="$PWD"
skill_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${OMNIGENT_VERIFY_REPO_ROOT:-}" ]]; then
  repo_root="$OMNIGENT_VERIFY_REPO_ROOT"
elif repo_root="$(git -C "$invocation_cwd" rev-parse --show-toplevel 2>/dev/null)"; then
  :
elif repo_root="$(git -C "$skill_root" rev-parse --show-toplevel 2>/dev/null)" &&
  [[ "$skill_root" == "$repo_root/.agents/skills/verify-omnigent" ]]; then
  :
else
  echo "verify-omnigent: target checkout is ambiguous; run from its Git worktree or set OMNIGENT_VERIFY_REPO_ROOT" >&2
  exit 2
fi
repo_root="$(cd "$repo_root" && pwd)"
export OMNIGENT_VERIFY_SKILL_ROOT="$skill_root"
cd "$repo_root"

profile="${1:-doctor}"
if [[ "$#" -gt 0 ]]; then
  shift
fi
doctor_profile="smoke"
if [[ "$profile" == "doctor" && "$#" -gt 0 && "${1:-}" != "--base-ref" ]]; then
  doctor_profile="$1"
  shift
fi
base_ref="${OMNIGENT_VERIFY_BASE_REF:-HEAD}"
oss_ref="${OMNIGENT_VERIFY_OSS_REF:-HEAD}"
with_universe=0
comparison_request=""
comparison_capability=""
max_p50_regression_percent="10"
max_p99_regression_percent="10"
while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --base-ref)
      if [[ "$#" -lt 2 ]]; then
        echo "verify-omnigent: --base-ref needs a value" >&2
        exit 2
      fi
      base_ref="$2"
      shift 2
      ;;
    --oss-ref)
      if [[ "$#" -lt 2 ]]; then
        echo "verify-omnigent: --oss-ref needs a value" >&2
        exit 2
      fi
      oss_ref="$2"
      shift 2
      ;;
    --with-universe)
      with_universe=1
      shift
      ;;
    --max-p50-regression-percent|--max-p99-regression-percent)
      if [[ "$#" -lt 2 ]] ||
        ! python3 -c 'import math,sys; value=float(sys.argv[1]); assert math.isfinite(value) and 0 <= value <= 1000' "$2" 2>/dev/null; then
        echo "verify-omnigent: $1 needs a finite value between 0 and 1000" >&2
        exit 2
      fi
      if [[ "$1" == "--max-p50-regression-percent" ]]; then
        max_p50_regression_percent="$2"
      else
        max_p99_regression_percent="$2"
      fi
      shift 2
      ;;
    --comparison-request)
      if [[ "$#" -lt 2 ]]; then
        echo "verify-omnigent: --comparison-request needs a value" >&2
        exit 2
      fi
      comparison_request="$2"
      shift 2
      ;;
    --comparison-capability)
      if [[ "$#" -lt 2 ]]; then
        echo "verify-omnigent: --comparison-capability needs a value" >&2
        exit 2
      fi
      comparison_capability="$2"
      shift 2
      ;;
    *)
      echo "verify-omnigent: unknown argument: $1" >&2
      exit 2
      ;;
  esac
done
manifest_doctor_profile="$doctor_profile"
if [[ "$profile" != "doctor" ]]; then
  manifest_doctor_profile="$profile"
fi
evidence_root="${OMNIGENT_VERIFY_EVIDENCE_DIR:-$repo_root/.artifacts/verify-omnigent}"
manifest_helper="$skill_root/scripts/evidence_manifest.py"
orchestration_helper="$skill_root/scripts/orchestration.py"
profile_runner="$skill_root/scripts/profile_runner.py"
universe_preflight="$skill_root/scripts/universe_preflight.py"
comparison_helper="$skill_root/scripts/comparison.py"
runtime_helper="$skill_root/scripts/runtime_support.py"
verify_script="$skill_root/scripts/verify.sh"
if [[ "$profile" == "prepare-hooks" ]]; then
  cache_root="$HOME/.cache/verify-omnigent/pre-commit"
  cache_parent="$(dirname "$cache_root")"
  mkdir -p "$cache_parent"
  chmod 700 "$cache_parent"
  staging_cache="$(mktemp -d "$cache_parent/pre-commit.version.XXXXXX")"
  # shellcheck disable=SC2329 # EXIT trap callback
  cleanup_preparation() {
    rm -rf "$staging_cache"
  }
  trap cleanup_preparation EXIT
  PRE_COMMIT_HOME="$staging_cache" uv run --no-sync pre-commit install-hooks \
    --config "$repo_root/.pre-commit-config.yaml"
  chmod -R go-w "$staging_cache"
  python3 "$runtime_helper" seal-pre-commit-cache \
    --repo-root "$repo_root" \
    --cache-root "$staging_cache" >/dev/null
  python3 "$runtime_helper" publish-pre-commit-cache \
    --repo-root "$repo_root" \
    --staging "$staging_cache" \
    --destination "$cache_root"
  staging_cache=""
  trap - EXIT
  echo "Prepared and authenticated configured pre-commit hooks in $cache_root"
  exit 0
fi
if [[ "$profile" == "prepare-tools" ]]; then
  cache_root="$HOME/.cache/verify-omnigent/pnpm"
  cache_parent="$(dirname "$cache_root")"
  mkdir -p "$cache_parent"
  chmod 700 "$cache_parent"
  staging_cache="$(mktemp -d "$cache_parent/pnpm.version.XXXXXX")"
  # shellcheck disable=SC2329 # EXIT trap callback
  cleanup_tool_preparation() {
    rm -rf "$staging_cache"
  }
  trap cleanup_tool_preparation EXIT
  python3 "$runtime_helper" prepare-pnpm-cache \
    --repo-root "$repo_root" \
    --staging "$staging_cache" \
    --allow-download >/dev/null
  python3 "$runtime_helper" publish-pnpm-cache \
    --staging "$staging_cache" \
    --destination "$cache_root"
  staging_cache=""
  trap - EXIT
  echo "Prepared authenticated pnpm from the exact packageManager pin."
  exit 0
fi
run_dir=""
run_id=""
run_status="failed"
final_exit_status=1
received_signal=""
child_pid=""
logging_started=0
finalized=0
managed_counter=0
initial_snapshot=""
initial_snapshot_sha256=""
control_dir=""

# shellcheck disable=SC2329 # EXIT/finalization callback
cleanup_control_dir() {
  if [[ -n "$control_dir" ]]; then
    rm -rf "$control_dir"
    control_dir=""
  fi
}

reject_internal_control() {
  local name="$1"
  local present="$2"
  if [[ "$present" == "set" ]]; then
    echo "verify-omnigent: caller-supplied internal control $name is forbidden" >&2
    exit 2
  fi
}
reject_internal_control \
  OMNIGENT_VERIFY_PARTIAL_COMPARISON "${OMNIGENT_VERIFY_PARTIAL_COMPARISON+set}"
reject_internal_control \
  OMNIGENT_VERIFY_ONLY_STEP_INDICES "${OMNIGENT_VERIFY_ONLY_STEP_INDICES+set}"
reject_internal_control \
  OMNIGENT_VERIFY_CHANGED_FILES_JSON "${OMNIGENT_VERIFY_CHANGED_FILES_JSON+set}"
reject_internal_control \
  OMNIGENT_VERIFY_DISABLE_BASELINE "${OMNIGENT_VERIFY_DISABLE_BASELINE+set}"
reject_internal_control \
  OMNIGENT_VERIFY_INTERNAL_REQUEST "${OMNIGENT_VERIFY_INTERNAL_REQUEST+set}"
reject_internal_control \
  OMNIGENT_VERIFY_CONTROL_SNAPSHOT "${OMNIGENT_VERIFY_CONTROL_SNAPSHOT+set}"
reject_internal_control \
  OMNIGENT_VERIFY_DEEP_DEPENDENCY_SNAPSHOT \
  "${OMNIGENT_VERIFY_DEEP_DEPENDENCY_SNAPSHOT+set}"
if [[ -n "$comparison_request" ]]; then
  case "$profile" in
    quality-gates | server | harness-client | cli | web-ui | desktop) ;;
    *)
      echo "verify-omnigent: comparison requests are unsupported for profile $profile" >&2
      exit 2
      ;;
  esac
fi
if [[ ( -n "$comparison_request" && -z "$comparison_capability" ) ||
  ( -z "$comparison_request" && -n "$comparison_capability" ) ]]; then
  echo "verify-omnigent: comparison request and capability must be supplied together" >&2
  exit 2
fi

usage() {
  echo "usage: $0 <prepare-hooks|doctor|auto|all-surfaces|quality-gates|universe|server|backend|db-migration-deploy|cli|web-ui|desktop|harness-client|harness-live|perf|smoke|collaboration|automations|core|full-ui> [doctor-profile] [--base-ref REF] [--oss-ref REF] [--with-universe]" >&2
}

json_array() {
  python3 -c 'import json, sys; print(json.dumps(sys.argv[1:]))' "$@"
}

run_dir_argument() {
  python3 - "$run_dir" "$repo_root" <<'PY'
import os
import sys
from pathlib import Path

run = Path(sys.argv[1]).resolve()
repo = Path(sys.argv[2]).resolve()
try:
    print(run.relative_to(repo))
except ValueError:
    print(os.fspath(run))
PY
}

# shellcheck disable=SC2329 # finalization callback
stop_logging() {
  if [[ "$logging_started" -ne 1 ]]; then
    return
  fi
  exec 1>&3 2>&4
  logging_started=0
}

# shellcheck disable=SC2329 # signal/finalization callback
terminate_child() {
  local target_pid="$1"
  local requested_signal="${2:-TERM}"
  local attempt

  if ! kill -0 "$target_pid" 2>/dev/null; then
    wait "$target_pid" 2>/dev/null || true
    return
  fi
  kill "-$requested_signal" "$target_pid" 2>/dev/null || true
  for ((attempt = 0; attempt < 20; attempt++)); do
    if ! kill -0 "$target_pid" 2>/dev/null; then
      break
    fi
    sleep 0.05
  done
  if kill -0 "$target_pid" 2>/dev/null; then
    kill -TERM "$target_pid" 2>/dev/null || true
    for ((attempt = 0; attempt < 100; attempt++)); do
      if ! kill -0 "$target_pid" 2>/dev/null; then
        break
      fi
      sleep 0.05
    done
  fi
  if kill -0 "$target_pid" 2>/dev/null; then
    kill -KILL "$target_pid" 2>/dev/null || true
  fi
  wait "$target_pid" 2>/dev/null || true
}

managed_probe() {
  managed_counter=$((managed_counter + 1))
  local result_path="$run_dir/managed/probe-${managed_counter}.json"
  local log_path="$run_dir/managed/probe-${managed_counter}.log"
  local -a args
  args=(
    python3 "$runtime_helper" run
    --console-fd 3
    --run-dir "$run_dir"
    --cwd "$repo_root"
    --log "$log_path"
    --result "$result_path"
    --timeout 120
    --set-env "OMNIGENT_VERIFY_REPO_ROOT=$repo_root"
    --set-env "OMNIGENT_VERIFY_RUN_DIR=$run_dir"
    --set-env "OMNIGENT_VERIFY_SKILL_ROOT=$skill_root"
  )
  if [[ "$profile" == "harness-live" || "$doctor_profile" == "harness-live" ]]; then
    args+=(--credentialed)
    if [[ -n "${OMNIGENT_VERIFY_DATABRICKS_PROFILE:-}" ]]; then
      args+=(--set-env "OMNIGENT_VERIFY_DATABRICKS_PROFILE=$OMNIGENT_VERIFY_DATABRICKS_PROFILE")
    fi
  fi
  args+=(-- "$@")
  if [[ -n "${managed_stdin_file:-}" ]]; then
    "${args[@]}" <"$managed_stdin_file" &
  else
    "${args[@]}" </dev/null &
  fi
  child_pid=$!
  local status=0
  wait "$child_pid" || status=$?
  child_pid=""
  return "$status"
}

managed_probe_script() {
  local input_path="$run_dir/managed/stdin-$((managed_counter + 1)).txt"
  mkdir -p "${input_path%/*}"
  cat >"$input_path"
  local status=0
  managed_stdin_file="$input_path" managed_probe "$@" || status=$?
  rm -f "$input_path"
  return "$status"
}

check_repository_state() {
  if [[ -z "$initial_snapshot" ]]; then
    echo "verification blocker: initial repository snapshot is unavailable" >&2
    return 1
  fi
  local current_snapshot_sha256
  current_snapshot_sha256="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$initial_snapshot" 2>/dev/null)" ||
    {
      echo "verification blocker: protected initial repository snapshot is unreadable" >&2
      return 1
    }
  if [[ "$current_snapshot_sha256" != "$initial_snapshot_sha256" ]]; then
    echo "verification blocker: protected initial repository snapshot was replaced" >&2
    return 1
  fi
  python3 "$runtime_helper" check \
    --repo-root "$repo_root" \
    --before "$initial_snapshot" \
    --deep-dependencies
}

# shellcheck disable=SC2329 # EXIT trap callback
finalize_run() {
  local shell_status=$?
  local -a finalize_args
  local finalize_ok=1
  if [[ "$finalized" -eq 1 || -z "$run_dir" ]]; then
    return
  fi
  finalized=1
  trap - EXIT INT TERM HUP

  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    terminate_child "$child_pid" TERM
  fi
  stop_logging
  if [[ -n "${orchestration_capability:-}" ]]; then
    rm -f "$orchestration_capability"
  fi
  if [[ -z "$received_signal" && "$run_status" != "passed" && "$final_exit_status" -eq 1 ]]; then
    final_exit_status="$shell_status"
  fi
  if [[ -n "$initial_snapshot_sha256" ]]; then
    local observed_snapshot_sha256=""
    observed_snapshot_sha256="$(
      python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' \
        "$initial_snapshot" 2>/dev/null
    )" || true
    if [[ "$observed_snapshot_sha256" != "$initial_snapshot_sha256" ]]; then
      echo "verify-omnigent: protected initial snapshot was tampered with" >&2
      cp "$control_dir/repository-before.backup.json" "$initial_snapshot"
      run_status="failed"
      final_exit_status=1
    fi
  fi

  finalize_args=(
    python3 "$manifest_helper" finalize
    --run-dir "$run_dir"
    --status "$run_status"
    --exit-status "$final_exit_status"
  )
  if [[ -n "$received_signal" ]]; then
    finalize_args+=(--signal "$received_signal")
  fi
  "${finalize_args[@]}" || {
    echo "verify-omnigent: failed to finalize manifest" >&2
    finalize_ok=0
  }
  if [[ "$finalize_ok" -eq 1 ]] &&
    ! python3 "$manifest_helper" assert-finalized \
      --run-dir "$run_dir" --expected-status "$run_status"; then
    echo "verify-omnigent: finalized manifest validation failed" >&2
    finalize_ok=0
  fi
  if [[ "$finalize_ok" -ne 1 ]]; then
    run_status="failed"
    final_exit_status=1
  fi
  python3 "$manifest_helper" summary --run-dir "$run_dir" || true
  printf 'OMNIGENT_VERIFY_SUMMARY schema=1 status=%s profile=%s run_id=%s manifest=%s\n' \
    "$run_status" "$profile" "$run_id" "$run_dir/manifest.json"
  cleanup_control_dir
  exit "$final_exit_status"
}

# shellcheck disable=SC2329 # INT/TERM/HUP trap callback
handle_signal() {
  local signal_name="$1"
  local signal_status="$2"
  received_signal="$signal_name"
  run_status="interrupted"
  final_exit_status="$signal_status"
  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    terminate_child "$child_pid" "$signal_name"
    child_pid=""
  fi
  exit "$signal_status"
}

doctor() {
  local target_profile="$1"

  require_file pyproject.toml "Restore the repository pyproject.toml." || return $?
  require_file "$skill_root/features/README.md" \
    "Restore the canonical verify-omnigent feature map." || return $?
  require_file "$skill_root/features/approvals-and-policies.md" \
    "Restore the approvals verification contract." || return $?
  require_file "$skill_root/features/harnesses-and-clients.md" \
    "Restore the harness verification contract." || return $?
  require_file "$manifest_helper" "Restore scripts/evidence_manifest.py." || return $?
  require_file "$orchestration_helper" "Restore scripts/orchestration.py." || return $?
  require_file "$profile_runner" "Restore scripts/profile_runner.py." || return $?
  require_file "$universe_preflight" "Restore scripts/universe_preflight.py." || return $?
  require_file "$comparison_helper" "Restore scripts/comparison.py." || return $?

  case "$target_profile" in
    quality-gates)
      require_command uv "Install uv and run: uv sync --locked --extra all --extra dev" || return $?
      require_file .pre-commit-config.yaml "Restore the repository pre-commit configuration." ||
        return $?
      managed_probe uv run --no-sync pre-commit --version || {
        echo "doctor blocker: pre-commit or an authenticated exact remote-hook cache is unavailable. Run: .agents/skills/verify-omnigent/scripts/verify.sh prepare-hooks" >&2
        return 1
      }
      local requirements_path="$run_dir/quality-requirements.json"
      managed_probe_script python3 - "$profile_runner" "$repo_root" "$base_ref" "$requirements_path" <<'PY'
import importlib.util
import json
import sys
from pathlib import Path

runner_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("verify_profile_runner", runner_path)
if spec is None or spec.loader is None:
    raise SystemExit("doctor blocker: profile runner could not be loaded")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
repo = Path(sys.argv[2])
files = module._changed_files(repo, sys.argv[3])
steps = module.profile_steps("quality-gates", "doctor", changed_files=files)
binaries = sorted(
    {
        f"{step.cwd}/{step.argv[0]}".removeprefix("./")
        for step in steps
        if "node_modules/.bin/" in step.argv[0]
    }
)
Path(sys.argv[4]).write_text(json.dumps(binaries) + "\n", encoding="utf-8")
PY
      require_file "$requirements_path" \
        "The quality prerequisite plan could not be generated." || return $?
      managed_probe python3 -c \
        'import json,sys; value=json.load(open(sys.argv[1], encoding="utf-8")); raise SystemExit(0 if isinstance(value,list) and all(isinstance(item,str) for item in value) else 1)' \
        "$requirements_path" || {
        echo "doctor blocker: quality prerequisite plan is invalid." >&2
        return 1
      }
      local -a quality_binaries=()
      while IFS= read -r binary; do
        quality_binaries+=("$binary")
      done < <(
        python3 - "$requirements_path" <<'PY'
import json
import sys

for value in json.load(open(sys.argv[1], encoding="utf-8")):
    print(value)
PY
      )
      if [[ "${#quality_binaries[@]}" -gt 0 ]]; then
        require_command pnpm "Install the pnpm version declared in package.json." || return $?
        require_command node "Install Node 22.12 or newer and put node on PATH." || return $?
        check_node_version || return $?
        check_pnpm_version || return $?
        check_pnpm_install_state || return $?
        local binary
        for binary in "${quality_binaries[@]}"; do
          require_executable "$binary" "Run: CI=true pnpm install --frozen-lockfile" || return $?
        done
      fi
      ;;
    backend)
      require_file scripts/backend-smoke.sh "Restore scripts/backend-smoke.sh." || return $?
      require_command python3 "Install Python 3.12 or newer and put python3 on PATH." || return $?
      require_command git "Install git and put it on PATH." || return $?
      require_command curl "Install curl and put it on PATH." || return $?
      managed_probe_script python3 - <<'PY'
import sys

if sys.version_info < (3, 12):
    raise SystemExit(
        "doctor blocker: backend smoke requires python3 >= 3.12. "
        "Install Python 3.12 or newer and retry."
    )
PY
      local status=$?
      [[ "$status" -eq 0 ]] || return "$status"
      ;;
    server)
      require_command uv "Install uv and run: uv sync --locked --extra all --extra dev" || return $?
      require_file tests/server/test_app.py "Restore tests/server/test_app.py." || return $?
      require_file tests/server/integration/test_app.py \
        "Restore tests/server/integration/test_app.py." || return $?
      managed_probe uv run --no-sync python -c "import httpx, omnigent" || {
        echo "doctor blocker: server test imports failed. Run: uv sync --locked --extra all --extra dev" >&2
        return 1
      }
      ;;
    db-migration-deploy)
      require_command uv "Install uv and run: uv sync --locked --extra all --extra dev" || return $?
      require_directory tests/db "Restore tests/db migration contracts." || return $?
      require_directory tests/stores "Restore tests/stores contracts." || return $?
      require_directory tests/deploy "Restore tests/deploy contracts." || return $?
      ;;
    perf)
      require_file dev/benchmarks/omnigent/run.py \
        "Restore dev/benchmarks/omnigent/run.py." || return $?
      require_command uv "Install uv and run: uv sync --locked --extra all --extra dev" || return $?
      managed_probe_script uv run --no-sync python - <<'PY'
from pathlib import Path

import httpx
import omnigent

print(f"omnigent={Path(omnigent.__file__).resolve()}")
print(f"httpx={httpx.__version__}")
PY
      local status=$?
      [[ "$status" -eq 0 ]] || return "$status"
      ;;
    smoke | collaboration | automations | core | full-ui | web-ui)
      doctor_ui || return $?
      if [[ "$target_profile" == "web-ui" ]]; then
        require_file web/package.json "Restore web/package.json." || return $?
        require_directory web/node_modules \
          "Run: pnpm install --frozen-lockfile --filter web" || return $?
        require_executable web/node_modules/.bin/vitest \
          "Run: CI=true pnpm install --frozen-lockfile" || return $?
        require_executable web/node_modules/.bin/tsc \
          "Run: CI=true pnpm install --frozen-lockfile" || return $?
      fi
      ;;
    desktop)
      doctor_ui || return $?
      require_file web/electron/package.json "Restore web/electron/package.json." || return $?
      require_directory web/node_modules \
        "Run: pnpm install --frozen-lockfile --filter web" || return $?
      require_directory web/electron/node_modules \
        "Run: pnpm install --frozen-lockfile --filter web/electron" || return $?
      require_executable web/electron/node_modules/.bin/electron-builder \
        "Run: CI=true pnpm install --frozen-lockfile" || return $?
      if python3 - <<'PY'
from pathlib import Path

raise SystemExit(0 if any(Path("web/electron/e2e").glob("*.e2e.js")) else 1)
PY
      then
        managed_probe node -e \
          'require.resolve("electron", {paths: ["web/electron"]}); require.resolve("playwright", {paths: ["web/electron"]})' || {
          echo "doctor blocker: Electron E2E files exist, but Node playwright and electron are not both importable from web/electron. Provision the dependencies documented in web/electron/e2e/README.md; a skipped native journey is not a pass." >&2
          return 1
        }
      fi
      ;;
    cli)
      case "$(uname -s)" in
        Darwin | Linux) ;;
        *)
          echo "doctor blocker: the CLI PTY driver requires macOS or Linux. Run this lane on a POSIX host." >&2
          return 1
          ;;
      esac
      require_file .claude/skills/cli-setup-verify/verify_cli.py \
        "Restore the checked-in CLI setup driver." || return $?
      require_file .venv/bin/python \
        "Run: uv sync --locked --extra all --extra dev" || return $?
      require_file .venv/bin/omnigent \
        "Run: uv sync --locked --extra all --extra dev" || return $?
      managed_probe_script .venv/bin/python - <<'PY'
import pexpect

print(f"pexpect={pexpect.__version__}")
PY
      ;;
    harness-client | harness-live)
      require_command uv "Install uv and run: uv sync --locked --extra all --extra dev" || return $?
      require_file tests/harness_bench/__main__.py \
        "Restore the checked-in harness bench." || return $?
      managed_probe uv run --no-sync python -m tests.harness_bench --list >/dev/null || {
        echo "doctor blocker: harness bench imports failed. Run: uv sync --locked --extra all --extra dev" >&2
        return 1
      }
      if [[ "$target_profile" == "harness-live" ]]; then
        if [[ -z "${OMNIGENT_VERIFY_HARNESS:-}" ]]; then
          echo "doctor blocker: set OMNIGENT_VERIFY_HARNESS to one name from: uv run --no-sync python -m tests.harness_bench --list" >&2
          return 1
        fi
        managed_probe_script uv run --no-sync python - "${OMNIGENT_VERIFY_DATABRICKS_PROFILE:-}" <<'PY'
import sys
from tests.harness_bench.runtime_env import bench_creds_skip_reason

profile = sys.argv[1] or None
reason = bench_creds_skip_reason(profile)
if reason:
    raise SystemExit(
        "doctor blocker: live harness credentials are unavailable: "
        f"{reason}. Configure ambient OPENAI_* credentials or set "
        "OMNIGENT_VERIFY_DATABRICKS_PROFILE."
    )
PY
      fi
      ;;
    universe)
      local universe_root_path="${UNIVERSE_ROOT:-$repo_root/../universe}"
      require_file "$skill_root/compatibility.md" \
        "Restore the downstream compatibility contract." || return $?
      require_file "$universe_root_path/agentbricks/mas/third_party/sync/UPSTREAM_REF" \
        "Set UNIVERSE_ROOT to a Universe checkout containing agentbricks/mas." || return $?
      require_file "$universe_root_path/agentbricks/mas/third_party/sync/UI_UPSTREAM_REF" \
        "Restore the MAS UI upstream pin." || return $?
      require_command python3 "Install Python 3.12 or newer and put python3 on PATH." || return $?
      require_command git "Install git and put it on PATH." || return $?
      require_command bazel "Install the Universe Bazel launcher and put it on PATH." || return $?
      managed_probe python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' || {
        echo "doctor blocker: Universe preflight requires python3 >= 3.12." >&2
        return 1
      }
      if [[ "$(uname -s)" == "Darwin" ]]; then
        echo "doctor note: Darwin can run source and hermetic checks; MAS runtime proof requires Linux because of known jemalloc/features.h and psutil/_psutil_osx blockers."
      fi
      ;;
    auto | all-surfaces)
      doctor_orchestration "$target_profile" || return $?
      ;;
    doctor)
      echo "doctor blocker: choose a concrete profile after doctor." >&2
      return 2
      ;;
    *)
      usage
      return 2
      ;;
  esac

  echo "doctor: $target_profile ready"
}

require_file() {
  local path="$1"
  local remediation="$2"
  if [[ ! -f "$path" ]]; then
    echo "doctor blocker: missing file $path. $remediation" >&2
    return 1
  fi
}

require_directory() {
  local path="$1"
  local remediation="$2"
  if [[ ! -d "$path" ]]; then
    echo "doctor blocker: missing directory $path. $remediation" >&2
    return 1
  fi
}

require_executable() {
  local path="$1"
  local remediation="$2"
  if [[ ! -x "$path" ]]; then
    echo "doctor blocker: missing executable $path. $remediation" >&2
    return 1
  fi
}

require_command() {
  local name="$1"
  local remediation="$2"
  if ! command -v "$name" >/dev/null; then
    echo "doctor blocker: command $name is unavailable. $remediation" >&2
    return 1
  fi
}

check_pnpm_install_state() {
  managed_probe_script python3 - "$repo_root" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
metadata = root / "node_modules" / ".modules.yaml"
if metadata.exists():
    try:
        value = json.loads(metadata.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(
            "doctor blocker: node_modules metadata is unreadable "
            f"({type(exc).__name__}). Run: CI=true pnpm install --frozen-lockfile"
        )
    raw_virtual_store = value.get("virtualStoreDir")
    if not isinstance(raw_virtual_store, str):
        raise SystemExit(
            "doctor blocker: node_modules metadata has no virtualStoreDir. "
            "Run: CI=true pnpm install --frozen-lockfile"
        )
    actual = (metadata.parent / raw_virtual_store).resolve()
    expected = (root / "node_modules" / ".pnpm").resolve()
    if actual != expected:
        raise SystemExit(
            "doctor blocker: node_modules belongs to a different checkout "
            f"({actual}). Run: CI=true pnpm install --frozen-lockfile"
        )
    raise SystemExit(0)

bins = root / "web" / "node_modules" / ".bin"
if not bins.is_dir() or not any(bins.iterdir()):
    raise SystemExit(
        "doctor blocker: no validated JavaScript dependency tree is available. "
        "Run: CI=true pnpm install --frozen-lockfile"
    )
for binary in bins.iterdir():
    try:
        binary.resolve(strict=True).relative_to(root)
    except (OSError, ValueError):
        raise SystemExit(
            f"doctor blocker: {binary} resolves outside this checkout. "
            "Run: CI=true pnpm install --frozen-lockfile"
        )
PY
}

check_node_version() {
  managed_probe_script node - <<'JS'
const [major, minor] = process.versions.node.split(".").map(Number);
if (major < 22 || (major === 22 && minor < 12)) {
  console.error(
    `doctor blocker: Node ${process.versions.node} is unsupported. Install Node 22.12 or newer.`,
  );
  process.exit(1);
}
JS
}

check_pnpm_version() {
  managed_probe_script python3 - <<'PY'
import json
import os
import subprocess
from pathlib import Path

package = json.load(open("package.json", encoding="utf-8"))
declared = package.get("packageManager")
if not isinstance(declared, str) or not declared.startswith("pnpm@"):
    raise SystemExit("doctor blocker: package.json has no exact packageManager pnpm version")
expected = declared.removeprefix("pnpm@")
tools_value = os.environ.get("OMNIGENT_VERIFY_TRUSTED_PNPM_TOOLS_ROOT")
if not tools_value:
    raise SystemExit(
        f"doctor blocker: authenticated pnpm {expected} is not prepared. Run: "
        ".agents/skills/verify-omnigent/scripts/verify.sh prepare-tools"
    )
tools = Path(tools_value).resolve(strict=True)
executable = (tools / expected / "bin" / "pnpm").resolve(strict=True)
executable.relative_to(tools)
actual = subprocess.run(
    [str(executable), "--version"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
if actual != expected:
    raise SystemExit(
        f"doctor blocker: pnpm {actual} does not match package.json ({expected}). "
        "Run: .agents/skills/verify-omnigent/scripts/verify.sh prepare-tools"
    )
PY
}

doctor_ui() {
      require_file tests/e2e_ui/conftest.py "Restore tests/e2e_ui/conftest.py." || return $?
      require_file tests/e2e_ui/playwright_evidence.py \
        "Restore tests/e2e_ui/playwright_evidence.py." || return $?
      require_command uv "Install uv and run: uv sync --locked --extra all --extra dev" || return $?
      require_command node "Install Node 22.12 or newer and put node on PATH." || return $?
      check_node_version || return $?
      check_pnpm_version || return $?
      check_pnpm_install_state || return $?
      require_executable web/node_modules/.bin/prettier \
        "Run: CI=true pnpm install --frozen-lockfile" || return $?
      require_executable web/node_modules/.bin/oxlint \
        "Run: CI=true pnpm install --frozen-lockfile" || return $?
      require_executable web/node_modules/.bin/tsc \
        "Run: CI=true pnpm install --frozen-lockfile" || return $?
      require_file web/node_modules/vite/bin/vite.js \
        "Run: CI=true pnpm install --frozen-lockfile" || return $?
      managed_probe_script uv run --no-sync python - <<'PY'
from pathlib import Path

import httpx
import omnigent
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    chromium = Path(playwright.chromium.executable_path)
    if not chromium.is_file():
        raise SystemExit(
            "Playwright Chromium is missing. "
            "Run: uv run --no-sync playwright install chromium"
        )

print(f"omnigent={Path(omnigent.__file__).resolve()}")
print(f"chromium={chromium}")
print(f"httpx={httpx.__version__}")
PY
      local status=$?
      [[ "$status" -eq 0 ]] || return "$status"
      managed_probe node --version || return $?
      managed_probe pnpm --version || return $?
}

doctor_orchestration() {
  local target_profile="$1"
  local plan_path="$run_dir/lane-plan.json"
  local -a plan_args selected_lanes
  plan_args=(
    python3 "$orchestration_helper"
    --repo-root "$repo_root"
    --profile "$target_profile"
    --base-ref "$base_ref"
    --output "$plan_path"
  )
  if [[ "$with_universe" -eq 1 ]]; then
    plan_args+=(--with-universe)
  fi
  local plan_status=0
  "${plan_args[@]}" || plan_status=$?
  if [[ -f "$plan_path" ]]; then
    python3 "$manifest_helper" record-plan --run-dir "$run_dir" --plan "$plan_path"
  fi
  [[ "$plan_status" -eq 0 ]] || return "$plan_status"
  selected_lanes=()
  while IFS= read -r lane; do
    selected_lanes+=("$lane")
  done < <(
    python3 - "$plan_path" <<'PY'
import json
import sys

for lane in json.load(open(sys.argv[1], encoding="utf-8"))["selected_lanes"]:
    print(lane)
PY
  )
  local lane
  for lane in "${selected_lanes[@]}"; do
    doctor "$lane" || return $?
  done
}

mkdir -p "$evidence_root"
safe_profile="$(
  python3 -c 'import re, sys; print(re.sub(r"[^A-Za-z0-9_-]", "_", sys.argv[1])[:40] or "attempt")' \
    "$profile"
)"
run_dir="$(
  mktemp -d "$evidence_root/$(date -u +%Y%m%dT%H%M%SZ)-${safe_profile}.XXXXXX"
)"
run_id="${run_dir##*/}"
mkdir -p "$run_dir/playwright"
control_dir="$(mktemp -d "${TMPDIR:-/tmp}/omnigent-verify-control.XXXXXX")"
chmod 700 "$control_dir"
cp "$skill_root/scripts/"*.py "$control_dir/"
runtime_helper="$control_dir/runtime_support.py"
manifest_helper="$control_dir/evidence_manifest.py"
initial_snapshot="$control_dir/repository-before.json"
export OMNIGENT_VERIFY_CONTROL_SNAPSHOT="$initial_snapshot"
export OMNIGENT_VERIFY_DEEP_DEPENDENCY_SNAPSHOT=1
trap cleanup_control_dir EXIT
python3 "$manifest_helper" init \
  --run-dir "$run_dir" \
  --profile "$profile" \
  --doctor-profile "$manifest_doctor_profile"

exec 3>&1 4>&2
trap finalize_run EXIT
trap 'handle_signal INT 130' INT
trap 'handle_signal TERM 143' TERM
trap 'handle_signal HUP 129' HUP
exec >>"$run_dir/run.log" 2>&1
logging_started=1
if [[ ! -f "$initial_snapshot" ]] &&
  ! python3 "$runtime_helper" snapshot \
    --repo-root "$repo_root" \
    --output "$initial_snapshot" \
    --deep-dependencies; then
  run_status="blocked"
  final_exit_status=1
  exit "$final_exit_status"
fi
initial_snapshot_sha256="$(
  python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' \
    "$initial_snapshot"
)"
cp "$initial_snapshot" "$control_dir/repository-before.backup.json"
orchestration_capability=""
if [[ "$profile" == "auto" ]]; then
  orchestration_capability="$run_dir/environment/comparison-capability"
  python3 - "$orchestration_capability" <<'PY'
import os
import secrets
import sys
from pathlib import Path

path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "wb") as handle:
    handle.write(secrets.token_bytes(32))
    handle.flush()
    os.fsync(handle.fileno())
PY
fi

if [[ "$profile" == "doctor" ]]; then
  doctor_argv=(doctor "$doctor_profile")
  if [[ "$doctor_profile" == "auto" ]]; then
    doctor_argv+=(--base-ref "$base_ref")
  fi
  if [[ "$doctor_profile" == "auto" || "$doctor_profile" == "all-surfaces" ]]; then
    if [[ "$with_universe" -eq 1 ]]; then
      doctor_argv+=(--with-universe --oss-ref "$oss_ref")
    fi
  elif [[ "$doctor_profile" == "universe" ]]; then
    doctor_argv+=(--base-ref "$base_ref" --oss-ref "$oss_ref")
  fi
  python3 "$manifest_helper" prepare \
    --run-dir "$run_dir" \
    --phase doctor \
    --argv-json "$(json_array "${doctor_argv[@]}")" \
    --tests-json '[]'
  set +e
  doctor "$doctor_profile"
  final_exit_status=$?
  set -e
  if [[ "$final_exit_status" -eq 0 ]]; then
    set +e
    check_repository_state
    final_exit_status=$?
    set -e
  fi
  if [[ "$final_exit_status" -eq 0 ]]; then
    run_status="passed"
  else
    run_status="blocked"
  fi
  exit "$final_exit_status"
fi

if [[ "$profile" == "auto" || "$profile" == "all-surfaces" ]]; then
  plan_path="$run_dir/lane-plan.json"
  plan_args=(
    python3 "$orchestration_helper"
    --repo-root "$repo_root"
    --profile "$profile"
    --base-ref "$base_ref"
    --output "$plan_path"
  )
  if [[ "$with_universe" -eq 1 ]]; then
    plan_args+=(--with-universe)
  fi
  set +e
  "${plan_args[@]}"
  plan_status=$?
  set -e
  if [[ -f "$plan_path" ]]; then
    python3 "$manifest_helper" record-plan --run-dir "$run_dir" --plan "$plan_path"
  fi
  selected_lanes=()
  while IFS= read -r lane; do
    selected_lanes+=("$lane")
  done < <(
    python3 - "$plan_path" <<'PY'
import json
import sys

for lane in json.load(open(sys.argv[1], encoding="utf-8"))["selected_lanes"]:
    print(lane)
PY
  )
  orchestration_argv=(orchestrate "$profile")
  if [[ "$profile" == "auto" ]]; then
    orchestration_argv+=(--base-ref "$base_ref")
  fi
  if [[ "$with_universe" -eq 1 ]]; then
    orchestration_argv+=(--with-universe --oss-ref "$oss_ref")
  fi
  python3 "$manifest_helper" prepare \
    --run-dir "$run_dir" \
    --phase orchestration \
    --argv-json "$(json_array "${orchestration_argv[@]}")" \
    --tests-json "$(json_array "${selected_lanes[@]}")"
  python3 "$manifest_helper" register-children \
    --run-dir "$run_dir" \
    --lanes-json "$(json_array "${selected_lanes[@]}")"
  if [[ "$plan_status" -ne 0 ]]; then
    run_status="blocked"
    final_exit_status="$plan_status"
    exit "$final_exit_status"
  fi

  prerequisite_status=0
  for lane in "${selected_lanes[@]}"; do
    echo "==> prerequisite check: $lane"
    set +e
    doctor "$lane"
    lane_doctor_status=$?
    set -e
    if [[ "$lane_doctor_status" -ne 0 ]]; then
      prerequisite_status="$lane_doctor_status"
      break
    fi
  done
  if [[ "$prerequisite_status" -ne 0 ]]; then
    run_status="blocked"
    final_exit_status="$prerequisite_status"
    exit "$final_exit_status"
  fi

  orchestration_status=0
  for lane in "${selected_lanes[@]}"; do
    child_root="$run_dir/children/$lane"
    mkdir -p "$child_root"
    echo "==> required lane: $lane"
    python3 "$manifest_helper" mark-child \
      --run-dir "$run_dir" \
      --lane "$lane" \
      --status running
    set +e
    child_argv=("$lane")
    if [[ "$lane" == "universe" ]]; then
      child_argv+=(--base-ref "$base_ref" --oss-ref "$oss_ref")
    fi
    child_command=(
      python3 "$runtime_helper" run
      --console-fd 3
      --run-dir "$run_dir"
      --cwd "$repo_root"
      --log "$run_dir/managed/child-$lane.log"
      --result "$run_dir/managed/child-$lane.json"
      --set-env "OMNIGENT_VERIFY_ADAPTER=${OMNIGENT_VERIFY_ADAPTER:-unspecified}"
      --set-env "OMNIGENT_VERIFY_BASE_REF=$base_ref"
      --set-env "OMNIGENT_VERIFY_EVIDENCE_DIR=$child_root"
      --set-env "OMNIGENT_VERIFY_REPO_ROOT=$repo_root"
      -- "$verify_script" "${child_argv[@]}"
    )
    "${child_command[@]}" &
    child_pid=$!
    wait "$child_pid"
    lane_status=$?
    child_pid=""
    set -e
    child_manifest="$(
      python3 - "$child_root" <<'PY'
import sys
from pathlib import Path

manifests = list(Path(sys.argv[1]).glob("*/manifest.json"))
if len(manifests) == 1:
    print(manifests[0])
PY
    )"
    if [[ -z "$child_manifest" ]]; then
      child_manifest="$child_root/missing-manifest.json"
      if [[ "$lane_status" -eq 0 ]]; then
        lane_status=1
      fi
    elif [[ "$(
      python3 - "$child_manifest" <<'PY'
import json
import sys

print(json.load(open(sys.argv[1], encoding="utf-8")).get("status", "unavailable"))
PY
    )" != "passed" && "$lane_status" -eq 0 ]]; then
      lane_status=1
    fi
    python3 "$manifest_helper" add-child \
      --run-dir "$run_dir" \
      --lane "$lane" \
      --child-manifest "$child_manifest" \
      --exit-status "$lane_status"
    if [[ "$lane_status" -ne 0 && "$orchestration_status" -eq 0 ]]; then
      orchestration_status="$lane_status"
      if [[ "$profile" == "auto" && -n "$child_manifest" && -f "$child_manifest" ]]; then
        comparison_path="$run_dir/baseline/$lane/comparison.json"
        comparison_command=(
          python3 "$runtime_helper" run
          --console-fd 3
          --run-dir "$run_dir"
          --cwd "$repo_root"
          --log "$run_dir/managed/baseline-$lane.log"
          --result "$run_dir/managed/baseline-$lane.json"
          --timeout "${OMNIGENT_VERIFY_BASELINE_TIMEOUT_SECONDS:-600}"
          --set-env "OMNIGENT_VERIFY_REPO_ROOT=$repo_root"
          -- python3 "$comparison_helper" baseline
          --repo-root "$repo_root"
          --skill-root "$skill_root"
          --run-dir "$run_dir"
          --lane "$lane"
          --base-ref "$base_ref"
          --child-manifest "$child_manifest"
          --timeout "${OMNIGENT_VERIFY_BASELINE_TIMEOUT_SECONDS:-600}"
          --capability-file "$orchestration_capability"
        )
        set +e
        "${comparison_command[@]}" &
        child_pid=$!
        wait "$child_pid"
        comparison_status=$?
        child_pid=""
        set -e
        if [[ -f "$comparison_path" ]]; then
          python3 "$manifest_helper" record-baseline \
            --run-dir "$run_dir" \
            --lane "$lane" \
            --comparison "$comparison_path"
        elif [[ "$comparison_status" -eq 0 ]]; then
          orchestration_status=1
        fi
      fi
      break
    fi
  done
  final_exit_status="$orchestration_status"
  if [[ "$final_exit_status" -eq 0 ]]; then
    set +e
    check_repository_state
    final_exit_status=$?
    set -e
    if [[ "$final_exit_status" -eq 0 ]]; then
      run_status="passed"
    fi
  fi
  exit "$final_exit_status"
fi

set +e
doctor "$profile"
doctor_status=$?
set -e
if [[ "$doctor_status" -ne 0 ]]; then
  run_status="blocked"
  final_exit_status="$doctor_status"
  exit "$final_exit_status"
fi

case "$profile" in
  backend)
    port="$(
      python3 - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
    )"
    command=(env "PORT=$port" scripts/backend-smoke.sh)
    tests=("backend API smoke")
    ;;
  smoke)
    tests=(tests/e2e_ui/chat/test_smoke.py)
    ;;
  collaboration)
    tests=(
      tests/e2e_ui/collaboration/test_permissions_modal.py
      tests/e2e_ui/collaboration/test_sharing_journey.py
      tests/e2e_ui/collaboration/test_sharing_mode_off.py
      tests/e2e_ui/collaboration/test_collab_realtime.py
      tests/e2e_ui/collaboration/test_author_label.py
    )
    ;;
  automations)
    tests=(tests/e2e_ui/scheduled/test_scheduled_tasks_page.py)
    ;;
  core)
    tests=(
      tests/e2e_ui/chat/test_smoke.py
      tests/e2e_ui/start_session/test_start_session.py
      tests/e2e_ui/files/test_file_autosave.py
      tests/e2e_ui/collaboration/test_permissions_modal.py
      tests/e2e_ui/collaboration/test_sharing_mode_off.py
    )
    ;;
  full-ui)
    tests=(tests/e2e_ui)
    ;;
  quality-gates | perf | server | db-migration-deploy | cli | web-ui | desktop | harness-client | harness-live | universe)
    relative_run_dir="$(run_dir_argument)"
    command=(
      python3 "$profile_runner"
      --profile "$profile"
      --repo-root .
      --run-dir "$relative_run_dir"
    )
    if [[ -n "$comparison_request" ]]; then
      command+=(
        --request-file "$comparison_request"
        --capability-file "$comparison_capability"
      )
    fi
    tests=("$profile profile steps")
    if [[ "$profile" == "web-ui" || "$profile" == "desktop" ]]; then
      export OMNIGENT_VERIFY_RUN_DIR="$run_dir"
      export OMNIGENT_VERIFY_CLEANUP_MARKER="$run_dir/cleanup.json"
    elif [[ "$profile" == "quality-gates" ]]; then
      export OMNIGENT_VERIFY_BASE_REF="$base_ref"
    elif [[ "$profile" == "universe" ]]; then
      export OMNIGENT_VERIFY_BASE_REF="$base_ref"
      export OMNIGENT_VERIFY_OSS_REF="$oss_ref"
    fi
    ;;
  *)
    usage
    final_exit_status=2
    exit "$final_exit_status"
    ;;
esac

relative_run_dir="$(run_dir_argument)"
if [[ "$profile" != "backend" && "$profile" != "server" && "$profile" != "perf" &&
  "$profile" != "db-migration-deploy" &&
  "$profile" != "universe" &&
  "$profile" != "quality-gates" &&
  "$profile" != "cli" && "$profile" != "web-ui" && "$profile" != "desktop" &&
  "$profile" != "harness-client" && "$profile" != "harness-live" ]]; then
  command=(
    uv run --no-sync pytest "${tests[@]}" -v -m "not visual"
    --screenshot=on
    --tracing=on
    --video=retain-on-failure
    -o junit_family=legacy
    "--output=$relative_run_dir/playwright"
    "--junitxml=$relative_run_dir/junit.xml"
  )
  export OMNIGENT_VERIFY_RUN_DIR="$run_dir"
  export OMNIGENT_VERIFY_CLEANUP_MARKER="$run_dir/cleanup.json"
fi

python3 "$manifest_helper" prepare \
  --run-dir "$run_dir" \
  --phase verification \
  --argv-json "$(json_array "${command[@]}")" \
  --tests-json "$(json_array "${tests[@]}")"

managed_command=(
  python3 "$runtime_helper" run
  --console-fd 3
  --run-dir "$run_dir"
  --cwd "$repo_root"
  --log "$run_dir/managed/verification.log"
  --result "$run_dir/managed/verification.json"
  --timeout "${OMNIGENT_VERIFY_LANE_TIMEOUT_SECONDS:-3600}"
  --set-env "OMNIGENT_VERIFY_ADAPTER=${OMNIGENT_VERIFY_ADAPTER:-unspecified}"
  --set-env "OMNIGENT_VERIFY_BASE_REF=$base_ref"
  --set-env "OMNIGENT_VERIFY_EVIDENCE_DIR=$evidence_root"
  --set-env "OMNIGENT_VERIFY_OSS_REF=$oss_ref"
  --set-env "OMNIGENT_VERIFY_REPO_ROOT=$repo_root"
  --set-env "OMNIGENT_VERIFY_RUN_DIR=$run_dir"
  --set-env "OMNIGENT_VERIFY_SKILL_ROOT=$skill_root"
)
if [[ -n "${OMNIGENT_VERIFY_STEP_TIMEOUT_SECONDS:-}" ]]; then
  managed_command+=(
    --set-env "OMNIGENT_VERIFY_STEP_TIMEOUT_SECONDS=$OMNIGENT_VERIFY_STEP_TIMEOUT_SECONDS"
  )
fi
if [[ "$profile" == "harness-live" ]]; then
  managed_command+=(--credentialed)
fi
if [[ "$profile" == "smoke" || "$profile" == "collaboration" ||
  "$profile" == "automations" || "$profile" == "core" ||
  "$profile" == "full-ui" || "$profile" == "web-ui" ||
  "$profile" == "desktop" ]]; then
  managed_command+=(
    --set-env "OMNIGENT_WEB_UI_DIST=$run_dir/build/e2e-web-ui"
  )
fi
if [[ -n "${OMNIGENT_VERIFY_HARNESS:-}" ]]; then
  managed_command+=(--set-env "OMNIGENT_VERIFY_HARNESS=$OMNIGENT_VERIFY_HARNESS")
fi
if [[ -n "${OMNIGENT_VERIFY_DATABRICKS_PROFILE:-}" ]]; then
  managed_command+=(
    --set-env "OMNIGENT_VERIFY_DATABRICKS_PROFILE=$OMNIGENT_VERIFY_DATABRICKS_PROFILE"
  )
fi
if [[ "$profile" == "universe" && -n "${UNIVERSE_ROOT:-}" ]]; then
  managed_command+=(--set-env "UNIVERSE_ROOT=$UNIVERSE_ROOT")
fi
if [[ "$profile" == "web-ui" || "$profile" == "desktop" ||
  "$profile" == "smoke" || "$profile" == "collaboration" ||
  "$profile" == "automations" || "$profile" == "core" || "$profile" == "full-ui" ]]; then
  managed_command+=(
    --set-env "OMNIGENT_VERIFY_CLEANUP_MARKER=$run_dir/cleanup.json"
  )
fi
if [[ "$profile" == "backend" ]]; then
  managed_command+=(--set-env "PORT=$port")
fi
managed_command+=(-- "${command[@]}")
set +e
"${managed_command[@]}" &
child_pid=$!
wait "$child_pid"
final_exit_status=$?
child_pid=""
set -e

if [[ "$final_exit_status" -eq 0 ]]; then
  if [[ -n "$comparison_request" ]]; then
    final_exit_status=0
  else
    set +e
    python3 "$manifest_helper" validate --run-dir "$run_dir" --profile "$profile"
    final_exit_status=$?
    set -e
  fi
  if [[ "$final_exit_status" -eq 0 ]]; then
    if [[ "$profile" == "perf" && "$base_ref" != "HEAD" ]]; then
      perf_comparison_command=(
        python3 "$runtime_helper" run
        --console-fd 3
        --run-dir "$run_dir"
        --cwd "$repo_root"
        --log "$run_dir/managed/performance-comparison.log"
        --result "$run_dir/managed/performance-comparison.json"
        --timeout "${OMNIGENT_VERIFY_PERF_TIMEOUT_SECONDS:-900}"
        --set-env "OMNIGENT_VERIFY_REPO_ROOT=$repo_root"
        -- python3 "$comparison_helper" perf
        --repo-root "$repo_root"
        --run-dir "$run_dir"
        --base-ref "$base_ref"
        --candidate "$run_dir/benchmark.json"
        --timeout "${OMNIGENT_VERIFY_PERF_TIMEOUT_SECONDS:-900}"
        --max-p50-regression-percent "$max_p50_regression_percent"
        --max-p99-regression-percent "$max_p99_regression_percent"
      )
      set +e
      "${perf_comparison_command[@]}" &
      child_pid=$!
      wait "$child_pid"
      final_exit_status=$?
      child_pid=""
      set -e
    fi
    if [[ "$final_exit_status" -eq 0 ]]; then
      set +e
      check_repository_state
      final_exit_status=$?
      set -e
      if [[ "$final_exit_status" -eq 0 ]]; then
        run_status="passed"
      fi
    fi
  fi
fi
exit "$final_exit_status"
