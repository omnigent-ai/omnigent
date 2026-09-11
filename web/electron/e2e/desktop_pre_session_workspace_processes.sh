#!/usr/bin/env bash

desktop_demo_root() {
  local temp_root="${TMPDIR:-/tmp}"
  printf '%s/omnigent-prechat-manual-demo-%s\n' "${temp_root%/}" "$(id -u)"
}

ensure_secure_demo_root() {
  local root="$1"
  umask 077
  if [[ -L "$root" || ( -e "$root" && ! -d "$root" ) || ( -e "$root" && ! -O "$root" ) ]]; then
    echo "Refusing unsafe demo directory: $root" >&2
    return 1
  fi
  if [[ ! -d "$root" ]]; then
    mkdir -m 700 -- "$root"
  else
    chmod 700 "$root"
  fi
}

load_demo_process_state() {
  local file="$1"
  local name
  local pid
  mock_pid=""
  page_pid=""
  omnidev_pid=""
  electron_pid=""

  if [[ -L "$file" || ! -f "$file" || ! -O "$file" ]]; then
    echo "Refusing unsafe demo process file: $file" >&2
    return 1
  fi

  while IFS='=' read -r name pid; do
    if [[ ! "$pid" =~ ^[0-9]+$ ]] || (( pid <= 1 )); then
      echo "Invalid demo process file: $file" >&2
      return 1
    fi
    case "$name" in
      mock_pid)
        [[ -z "$mock_pid" ]] || return 1
        mock_pid="$pid"
        ;;
      page_pid)
        [[ -z "$page_pid" ]] || return 1
        page_pid="$pid"
        ;;
      omnidev_pid)
        [[ -z "$omnidev_pid" ]] || return 1
        omnidev_pid="$pid"
        ;;
      electron_pid)
        [[ -z "$electron_pid" ]] || return 1
        electron_pid="$pid"
        ;;
      *)
        echo "Invalid demo process file: $file" >&2
        return 1
        ;;
    esac
  done < "$file"

  if [[ -z "$mock_pid" || -z "$page_pid" || -z "$omnidev_pid" || -z "$electron_pid" ]]; then
    echo "Incomplete demo process file: $file" >&2
    return 1
  fi
}

stop_demo_process_tree() {
  local pid="$1"
  local children
  local child
  if [[ ! "$pid" =~ ^[0-9]+$ ]] || (( pid <= 1 )); then
    return
  fi
  children="$(pgrep -P "$pid" 2>/dev/null || true)"
  for child in $children; do
    stop_demo_process_tree "$child"
  done
  kill "$pid" 2>/dev/null || true
}

stop_demo_processes() {
  local pid
  for pid in "$@"; do
    stop_demo_process_tree "$pid"
  done
}

cleanup_failed_demo_launch() {
  local status=$?
  trap - EXIT INT TERM
  if (( cleanup_owned )); then
    stop_demo_processes "$electron_pid" "$omnidev_pid" "$page_pid" "$mock_pid"
    [[ -z "$state_tmp" ]] || rm -f -- "$state_tmp"
    rmdir -- "$launch_lock" 2>/dev/null || true
  fi
  exit "$status"
}

begin_demo_process_ownership() {
  launch_lock="$1"
  mock_pid=""
  page_pid=""
  omnidev_pid=""
  electron_pid=""
  state_tmp=""
  cleanup_owned=1
  trap cleanup_failed_demo_launch EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
}

transfer_demo_process_ownership() {
  cleanup_owned=0
  rmdir -- "$launch_lock"
  trap - EXIT INT TERM
}

wait_for_demo_services() {
  local server_url="$1"
  local mock_url="$2"
  local attempts="$3"
  local delay="$4"
  local workspace_ready=0
  local mock_ready=0
  local unused

  for ((unused = 0; unused < attempts; unused += 1)); do
    if (( ! workspace_ready )) && curl -fsS "$server_url/health" >/dev/null 2>&1 && \
      curl -fsS "$server_url/v1/hosts" 2>/dev/null | grep -q '"online"'; then
      workspace_ready=1
    fi
    if (( ! mock_ready )) && curl -fsS "$mock_url/stats" >/dev/null 2>&1; then
      mock_ready=1
    fi
    if (( workspace_ready && mock_ready )); then
      return 0
    fi
    sleep "$delay"
  done

  if (( ! workspace_ready )); then
    echo "Workspace server or host did not become ready at $server_url" >&2
  fi
  if (( ! mock_ready )); then
    echo "Mock model server did not become ready at $mock_url" >&2
  fi
  return 1
}
