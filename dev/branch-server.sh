#!/usr/bin/env bash
# Run an isolated, throwaway Omnigent dev server for one worktree/branch.
#
# Each instance gets its own free port, a fresh run dir under /tmp holding its
# database, artifacts, config and logs, and the worktree's own Python code and
# web build. It never reads or writes ~/.omnigent, and never touches another
# server, so several branches can be tested side by side without restarting
# anything shared.
#
# Usage:
#   dev/branch-server.sh up [WORKTREE] [--port N] [--with-host] [--no-build] [--rebuild]
#   dev/branch-server.sh down RUN_DIR|--all [--keep]
#   dev/branch-server.sh list
#
# `up` prints KEY=VALUE lines (URL, RUN_DIR, PORT, SERVER_PID, HOST_PID); the
# last line is the URL. `down` stops the instance's process groups and deletes
# its run dir (--keep leaves the dir for post-mortem). `--all` stops every
# instance started from the current worktree. See dev/branch-server.md for the e2e recipe.
set -euo pipefail

RUN_ROOT=${TMPDIR:-/tmp}
RUN_PREFIX=omnigent-branch-
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# Ports that belong to long-lived shared servers; never bind these.
RESERVED_PORTS=" 6767 6768 "

die() { echo "branch-server: $*" >&2; exit 1; }

# The repo's default worktree, i.e. the checkout that owns .venv and the
# shared web/node_modules, found from git's common dir.
main_checkout() {
  local common
  common=$(git -C "$1" rev-parse --path-format=absolute --git-common-dir)
  dirname "$common"
}

free_port() {
  local py=$1 port
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    port=$("$py" -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
    [[ $RESERVED_PORTS == *" $port "* ]] || { echo "$port"; return; }
  done
  die "could not find a free port"
}

stop_group() {
  local pid=$1
  [[ -n $pid ]] && kill -0 "$pid" 2>/dev/null || return 0
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || return 0; sleep 0.5; done
  kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
}

# Refuse anything that isn't a run dir this script created.
check_run_dir() {
  local dir
  dir=$(cd "$1" 2>/dev/null && pwd) || die "no such run dir: $1"
  [[ $(basename "$dir") == "$RUN_PREFIX"* && -f $dir/state ]] || die "not a branch-server run dir: $dir"
  echo "$dir"
}

web_needs_build() {
  local wt=$1 index=$1/omnigent/server/static/web-ui/index.html
  [[ -f $index ]] || return 0
  [[ -n $(find "$wt/web" -path "$wt/web/node_modules" -prune -o -type f -newer "$index" -print -quit) ]]
}

cmd_up() {
  local wt="" port="" with_host=0 build=auto
  while (($#)); do
    case $1 in
      --port) port=$2; shift 2 ;;
      --with-host) with_host=1; shift ;;
      --no-build) build=never; shift ;;
      --rebuild) build=always; shift ;;
      -*) die "unknown option: $1" ;;
      *) wt=$1; shift ;;
    esac
  done
  wt=$(cd "${wt:-$SCRIPT_DIR/..}" && git rev-parse --show-toplevel) || die "not a git worktree"
  [[ -f $wt/omnigent/cli.py ]] || die "$wt is not an omnigent checkout"

  local main py
  main=$(main_checkout "$wt")
  py=$main/.venv/bin/python
  [[ -x $py ]] || die "no venv at $main/.venv"

  # Web build: the server serves <worktree>/omnigent/server/static/web-ui.
  # pnpm links web/node_modules into the root store, so borrow both.
  local modules
  for modules in node_modules web/node_modules; do
    [[ -e $wt/$modules ]] && continue
    [[ -d $main/$modules ]] || die "no $modules in $wt or $main; run pnpm install"
    ln -s "$main/$modules" "$wt/$modules"
  done
  if [[ $build == always ]] || { [[ $build == auto ]] && web_needs_build "$wt"; }; then
    echo "building web in $wt/web ..." >&2
    (cd "$wt/web" && npm run build >&2) || die "web build failed"
  fi
  [[ -f $wt/omnigent/server/static/web-ui/index.html ]] || die "web UI not built (drop --no-build)"

  if [[ -n $port ]]; then
    [[ $RESERVED_PORTS != *" $port "* ]] || die "port $port is reserved for a shared server"
  else
    port=$(free_port "$py")
  fi

  local name run
  name=$(git -C "$wt" rev-parse --abbrev-ref HEAD | tr '/' '-')
  run=$(mktemp -d "$RUN_ROOT/${RUN_PREFIX}${name}-XXXXXX")
  mkdir -p "$run/data" "$run/config" "$run/artifacts" "$run/logs"
  local db="sqlite:///$run/data/chat.db" url="http://127.0.0.1:$port"

  # Everything the server and host persist lives under $run. PYTHONPATH puts
  # the worktree's code ahead of the venv's editable install of the main checkout.
  # Drop OMNIGENT_* inherited from an agent session (its runner ids, log file),
  # or the new processes would write to and act as part of that session.
  local var
  for var in $(compgen -e | grep '^OMNIGENT_' || true); do unset "$var"; done
  export OMNIGENT_WRAPPER_BYPASS=1 \
    OMNIGENT_DATA_DIR="$run/data" \
    OMNIGENT_CONFIG_HOME="$run/config" \
    OMNIGENT_DATABASE_URI="$db" \
    OMNIGENT_WEB_UI_DIST="$wt/omnigent/server/static/web-ui" \
    PYTHONPATH="$wt${PYTHONPATH:+:$PYTHONPATH}"

  cd "$wt"
  setsid nohup "$py" -m omnigent --log-to-stderr server --host 127.0.0.1 --port "$port" \
    --database-uri "$db" --artifact-location "$run/artifacts" \
    > "$run/logs/server.log" 2>&1 < /dev/null &
  local server_pid=$! host_pid=""
  printf 'WORKTREE=%s\nPORT=%s\nURL=%s\nSERVER_PID=%s\n' "$wt" "$port" "$url" "$server_pid" > "$run/state"

  local up=0
  for _ in $(seq 1 90); do
    if curl -fsS "$url/" -o /dev/null 2>/dev/null; then up=1; break; fi
    kill -0 "$server_pid" 2>/dev/null || break
    sleep 1
  done
  if ((!up)); then
    echo "server failed to start; tail of $run/logs/server.log:" >&2
    tail -20 "$run/logs/server.log" >&2
    stop_group "$server_pid"
    exit 1
  fi

  if ((with_host)); then
    setsid nohup "$py" -m omnigent host --server "$url" --no-open --non-interactive \
      > "$run/logs/host.log" 2>&1 < /dev/null &
    host_pid=$!
    echo "HOST_PID=$host_pid" >> "$run/state"
    local connected=0
    for _ in $(seq 1 30); do
      if grep -q "Connected as" "$run/logs/host.log" 2>/dev/null; then connected=1; break; fi
      kill -0 "$host_pid" 2>/dev/null || break
      sleep 1
    done
    ((connected)) || { echo "warning: host did not connect; see $run/logs/host.log" >&2; }
  fi

  printf 'RUN_DIR=%s\nPORT=%s\nSERVER_PID=%s\nHOST_PID=%s\nURL=%s\n' \
    "$run" "$port" "$server_pid" "$host_pid" "$url"
}

down_one() {
  local run keep=$2 SERVER_PID="" HOST_PID=""
  run=$(check_run_dir "$1")
  # shellcheck disable=SC1091
  source "$run/state"
  stop_group "$HOST_PID"
  stop_group "$SERVER_PID"
  if ((keep)); then echo "stopped; kept $run"; else rm -rf -- "$run"; echo "stopped; removed $run"; fi
}

cmd_down() {
  local target="" keep=0
  while (($#)); do
    case $1 in
      --keep) keep=1; shift ;;
      *) target=$1; shift ;;
    esac
  done
  [[ -n $target ]] || die "usage: down RUN_DIR|--all [--keep]"
  if [[ $target == --all ]]; then
    # Only this worktree's instances: other workers' servers share $RUN_ROOT.
    local d wt
    wt=$(git rev-parse --show-toplevel 2>/dev/null) || wt=$(cd "$SCRIPT_DIR/.." && pwd)
    for d in "$RUN_ROOT/$RUN_PREFIX"*/; do
      [[ -f $d/state ]] && grep -qxF "WORKTREE=$wt" "$d/state" && down_one "$d" "$keep"
    done
    return 0
  fi
  down_one "$target" "$keep"
}

cmd_list() {
  local d
  for d in "$RUN_ROOT/$RUN_PREFIX"*/; do
    [[ -f $d/state ]] || continue
    (
      SERVER_PID="" URL="" WORKTREE=""
      # shellcheck disable=SC1091
      source "$d/state"
      state=stopped; kill -0 "$SERVER_PID" 2>/dev/null && state=running
      printf '%s\t%s\t%s\t%s\n' "$state" "$URL" "${d%/}" "$WORKTREE"
    )
  done
}

case ${1:-} in
  up) shift; cmd_up "$@" ;;
  down) shift; cmd_down "$@" ;;
  list) shift; cmd_list ;;
  *) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
