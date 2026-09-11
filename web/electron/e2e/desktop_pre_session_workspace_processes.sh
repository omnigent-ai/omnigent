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
  local value
  mock_pid=""
  mock_port=""
  mock_started=""
  page_pid=""
  page_port=""
  page_started=""
  omnidev_pid=""
  omnidev_started=""
  electron_pid=""
  electron_started=""

  if [[ -L "$file" || ! -f "$file" || ! -O "$file" ]]; then
    echo "Refusing unsafe demo process file: $file" >&2
    return 1
  fi

  while IFS='=' read -r name value; do
    case "$name" in
      *_pid)
        [[ "$value" =~ ^[0-9]+$ ]] && (( value > 1 )) || {
          echo "Invalid demo process file: $file" >&2
          return 1
        }
        ;;
      *_port)
        [[ "$value" =~ ^[0-9]+$ ]] && (( value > 1 && value <= 65535 )) || {
          echo "Invalid demo process file: $file" >&2
          return 1
        }
        ;;
      *_started)
        [[ "$value" =~ ^[[:alnum:]]{10,40}$ ]] || {
          echo "Invalid demo process file: $file" >&2
          return 1
        }
        ;;
      *)
        echo "Invalid demo process file: $file" >&2
        return 1
        ;;
    esac
    case "$name" in
      mock_pid)
        [[ -z "$mock_pid" ]] || return 1
        mock_pid="$value"
        ;;
      mock_port)
        [[ -z "$mock_port" ]] || return 1
        mock_port="$value"
        ;;
      mock_started)
        [[ -z "$mock_started" ]] || return 1
        mock_started="$value"
        ;;
      page_pid)
        [[ -z "$page_pid" ]] || return 1
        page_pid="$value"
        ;;
      page_port)
        [[ -z "$page_port" ]] || return 1
        page_port="$value"
        ;;
      page_started)
        [[ -z "$page_started" ]] || return 1
        page_started="$value"
        ;;
      omnidev_pid)
        [[ -z "$omnidev_pid" ]] || return 1
        omnidev_pid="$value"
        ;;
      omnidev_started)
        [[ -z "$omnidev_started" ]] || return 1
        omnidev_started="$value"
        ;;
      electron_pid)
        [[ -z "$electron_pid" ]] || return 1
        electron_pid="$value"
        ;;
      electron_started)
        [[ -z "$electron_started" ]] || return 1
        electron_started="$value"
        ;;
    esac
  done < "$file"

  if [[ -z "$mock_pid" || -z "$mock_port" || -z "$mock_started" || -z "$page_pid" || \
    -z "$page_port" || -z "$page_started" || -z "$omnidev_pid" || \
    -z "$omnidev_started" || -z "$electron_pid" || -z "$electron_started" ]]; then
    echo "Incomplete demo process file: $file" >&2
    return 1
  fi
}

demo_process_start_token() {
  LC_ALL=C ps -p "$1" -o lstart= 2>/dev/null | tr -cd '[:alnum:]'
}

demo_process_matches() {
  local role="$1"
  local pid="$2"
  local command
  local expected
  command="$(ps -ww -p "$pid" -o args= 2>/dev/null || true)"
  [[ -n "$command" ]] || return 1
  case "$role" in
    mock)
      expected="$python $repo_root/tests/server/integration/mock_llm_server.py $mock_port"
      [[ "$(demo_process_start_token "$pid")" == "$mock_started" && "$command" == "$expected" ]]
      ;;
    page)
      expected="$python -m http.server $page_port --bind 127.0.0.1 --directory $demo_root/page"
      [[ "$(demo_process_start_token "$pid")" == "$page_started" && "$command" == "$expected" ]]
      ;;
    omnidev)
      expected="$expect $demo_root/omnidev.exp"
      [[ "$(demo_process_start_token "$pid")" == "$omnidev_started" && \
        "$command" == "$expected" ]]
      ;;
    electron)
      expected="$electron $repo_root/web/electron --user-data-dir=$demo_root/electron-profile"
      [[ "$(demo_process_start_token "$pid")" == "$electron_started" && \
        "$command" == "$expected" ]]
      ;;
    *)
      return 1
      ;;
  esac
}

demo_process_parent() {
  ps -p "$1" -o ppid= 2>/dev/null | tr -d '[:space:]'
}

stop_demo_descendant_tree() {
  local pid="$1"
  local parent="$2"
  local children
  local child
  if [[ ! "$pid" =~ ^[0-9]+$ ]] || (( pid <= 1 )); then
    return
  fi
  [[ "$(demo_process_parent "$pid")" == "$parent" ]] || return
  children="$(pgrep -P "$pid" 2>/dev/null || true)"
  for child in $children; do
    stop_demo_descendant_tree "$child" "$pid"
  done
  if [[ "$(demo_process_parent "$pid")" == "$parent" ]]; then
    kill "$pid" 2>/dev/null || true
  fi
}

stop_demo_process() {
  local role="$1"
  local pid="$2"
  if demo_process_matches "$role" "$pid"; then
    local children
    local child
    children="$(pgrep -P "$pid" 2>/dev/null || true)"
    for child in $children; do
      stop_demo_descendant_tree "$child" "$pid"
    done
    if demo_process_matches "$role" "$pid"; then
      kill "$pid" 2>/dev/null || true
    fi
  elif kill -0 "$pid" 2>/dev/null; then
    echo "Skipping stale $role PID $pid because its command does not match this demo." >&2
  fi
}

stop_demo_processes() {
  stop_demo_process electron "$electron_pid"
  stop_demo_process omnidev "$omnidev_pid"
  stop_demo_process page "$page_pid"
  stop_demo_process mock "$mock_pid"
}

cleanup_failed_demo_launch() {
  local status=$?
  trap - EXIT INT TERM
  if (( cleanup_owned )); then
    stop_demo_processes
    [[ -z "$state_tmp" ]] || rm -f -- "$state_tmp"
    rmdir -- "$launch_lock" 2>/dev/null || true
  fi
  exit "$status"
}

begin_demo_process_ownership() {
  launch_lock="$1"
  demo_root="$2"
  repo_root="$3"
  mock_pid=""
  mock_port=""
  mock_started=""
  page_pid=""
  page_port=""
  page_started=""
  omnidev_pid=""
  omnidev_started=""
  electron_pid=""
  electron_started=""
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
