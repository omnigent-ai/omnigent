#!/bin/sh

# Omnigent uninstaller. POSIX sh by design so it still works when the Python
# wheel is wedged or PATH is broken.

set -u

MARKER_BEGIN="# >>> Omnigent installer >>>"
MARKER_END="# <<< Omnigent installer <<<"
TAB=$(printf '\t')
TARGETS=""
DRY_RUN=false
YES=false
JSON=false
FORCE=false
PURGE=false
NO_BACKUP=false
PURGE_WORKSPACE=false
MODIFY_EXTERNAL_CONFIG=false
ASSUME_INFERRED=false
DESTRUCTIVE_FLAG=false
EXPLICIT_TARGET=false
DONE=0
SKIPPED=0
FAILED=0
REPORTED=0
EXIT_CODE=0
ACTIONS_FILE="$(mktemp "${TMPDIR:-/tmp}/omnigent-uninstall-actions.XXXXXX")" || exit 1
BACKUPS_FILE="$(mktemp "${TMPDIR:-/tmp}/omnigent-uninstall-backups.XXXXXX")" || exit 1

cleanup() {
  rm -f "$ACTIONS_FILE" "$BACKUPS_FILE"
}
trap cleanup EXIT HUP INT TERM

usage() {
  cat <<'EOF'
Usage: uninstall_oss.sh [cli|state|desktop-data|all ...] [flags]

Flags:
  --purge                    Remove state data (backs up first)
  --purge-workspace          With --purge, also remove ~/omnigent non-interactively
  --dry-run                  Print planned actions only
  --yes                      Non-interactive for auto-removable artifacts
  --json                     Emit a machine-readable summary
  --force                    SIGKILL stubborn processes and override refusals
  --modify-external-config   Allow marker-scoped third-party config edits
  --no-backup                Skip purge backup creation
  --assume-inferred          Allow inferred entries when combined with their gates
EOF
}

json_escape() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g; s/	/\\t/g'
}

record_action() {
  artifact="$1"
  path="$2"
  planned="$3"
  status="$4"
  gate="$5"
  detail="$6"
  case "$status" in
    done) DONE=$((DONE + 1)) ;;
    skipped) SKIPPED=$((SKIPPED + 1)) ;;
    failed) FAILED=$((FAILED + 1)); EXIT_CODE=1 ;;
    reported) REPORTED=$((REPORTED + 1)) ;;
  esac
  printf '{"artifact":"%s","path":"%s","planned":"%s","status":"%s","gate":%s,"detail":"%s"}\n' \
    "$(json_escape "$artifact")" \
    "$(json_escape "$path")" \
    "$(json_escape "$planned")" \
    "$(json_escape "$status")" \
    "$(if [ -n "$gate" ]; then printf '"%s"' "$(json_escape "$gate")"; else printf 'null'; fi)" \
    "$(json_escape "$detail")" >>"$ACTIONS_FILE"
  if [ "$JSON" != true ]; then
    printf '%s: %s (%s)\n' "$status" "$artifact${path:+ $path}" "$detail"
  fi
}

has_target() {
  case " $TARGETS " in
    *" $1 "*) return 0 ;;
    *) return 1 ;;
  esac
}

add_target() {
  case "$1" in
    all)
      add_target cli
      add_target state
      add_target desktop-data
      ;;
    cli | state | desktop-data)
      if ! has_target "$1"; then
        TARGETS="${TARGETS}${TARGETS:+ }$1"
      fi
      ;;
    *)
      usage >&2
      exit 3
      ;;
  esac
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --purge) PURGE=true; DESTRUCTIVE_FLAG=true; add_target state ;;
    --purge-workspace) PURGE_WORKSPACE=true; DESTRUCTIVE_FLAG=true ;;
    --dry-run) DRY_RUN=true ;;
    --yes) YES=true; DESTRUCTIVE_FLAG=true ;;
    --json) JSON=true ;;
    --force) FORCE=true; DESTRUCTIVE_FLAG=true ;;
    --modify-external-config) MODIFY_EXTERNAL_CONFIG=true; DESTRUCTIVE_FLAG=true ;;
    --no-backup) NO_BACKUP=true; DESTRUCTIVE_FLAG=true ;;
    --assume-inferred) ASSUME_INFERRED=true; DESTRUCTIVE_FLAG=true ;;
    -h | --help) usage; exit 0 ;;
    --*) usage >&2; exit 3 ;;
    *) EXPLICIT_TARGET=true; add_target "$1" ;;
  esac
  shift
done

if [ "$EXPLICIT_TARGET" != true ]; then
  add_target cli
fi

if [ "$DESTRUCTIVE_FLAG" != true ]; then
  DRY_RUN=true
fi

state_home() {
  if [ -n "${OMNIGENT_DATA_DIR:-}" ]; then
    printf '%s\n' "$OMNIGENT_DATA_DIR"
  else
    printf '%s/.omnigent\n' "$HOME"
  fi
}

is_pid_alive() {
  pid="$1"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  state="$(ps -o stat= -p "$pid" 2>/dev/null | awk 'NR == 1 { print $1 }')"
  case "$state" in
    Z*) return 1 ;;
  esac
  return 0
}

stop_pid() {
  pid="$1"
  origin="$2"
  if ! is_pid_alive "$pid"; then
    record_action process "$origin" stop done "" "already stopped"
    return 0
  fi
  if [ "$DRY_RUN" = true ]; then
    record_action process "$origin" stop reported "" "would SIGTERM pid $pid"
    return 0
  fi
  kill -TERM "$pid" 2>/dev/null || true
  i=0
  while [ "$i" -lt 50 ]; do
    if ! is_pid_alive "$pid"; then
      record_action process "$origin" stop done "" "stopped pid $pid"
      return 0
    fi
    sleep 0.1
    i=$((i + 1))
  done
  if [ "$FORCE" = true ]; then
    kill -KILL "$pid" 2>/dev/null || true
    record_action process "$origin" stop done "" "SIGKILL sent to pid $pid"
    return 0
  fi
  record_action process "$origin" stop failed "--force" "pid $pid did not stop"
  EXIT_CODE=2
  return 1
}

stop_processes() {
  home_dir="$(state_home)"
  pidfiles="$(mktemp "${TMPDIR:-/tmp}/omnigent-uninstall-pidfiles.XXXXXX")" || return 1
  : >"$pidfiles"
  if [ -d "$home_dir" ]; then
    for root in "$home_dir/run" "$home_dir/daemons" "$home_dir/runners" "$home_dir/local_server"; do
      [ -d "$root" ] || continue
      find "$root" -type f \( -name '*.pid' -o -name 'local_server.pid' \) 2>/dev/null >>"$pidfiles" || true
    done
    for pidfile in "$home_dir/local_server.pid" "$home_dir/host.pid"; do
      [ -f "$pidfile" ] && printf '%s\n' "$pidfile" >>"$pidfiles"
    done
    while IFS= read -r pidfile; do
      [ -n "$pidfile" ] || continue
      pid="$(awk 'NR == 1 { sub(/[^0-9].*$/, ""); print; exit }' "$pidfile" 2>/dev/null || true)"
      [ -n "$pid" ] && stop_pid "$pid" "$pidfile" || true
    done <"$pidfiles"
  fi
  if command -v tmux >/dev/null 2>&1; then
    sessions_file="$(mktemp "${TMPDIR:-/tmp}/omnigent-uninstall-tmux.XXXXXX")" || return 1
    tmux list-sessions -F '#S' >"$sessions_file" 2>/dev/null || true
    while IFS= read -r session; do
      case "$session" in
        omnigent:*)
          if [ "$DRY_RUN" = true ]; then
            record_action tmux "$session" stop reported "" "would kill tmux session"
          elif tmux kill-session -t "$session" 2>/dev/null; then
            record_action tmux "$session" stop done "" "killed tmux session"
          else
            record_action tmux "$session" stop failed "" "failed to kill tmux session"
          fi
          ;;
      esac
    done <"$sessions_file"
    rm -f "$sessions_file"
  fi
  rm -f "$pidfiles"
  if [ "$EXIT_CODE" = 2 ]; then
    return 1
  fi
}

unload_launch_agents() {
  [ -n "${OMNIGENT_UNINSTALL_LEDGER_MANIFEST:-}" ] && [ -f "$OMNIGENT_UNINSTALL_LEDGER_MANIFEST" ] || return 0
  while IFS="$TAB" read -r artifact kind unit_path label source confidence rest; do
    [ "$artifact" = launch_agent ] || continue
    if [ "$DRY_RUN" = true ]; then
      record_action launch_agent "$unit_path" unload reported "" "would unload $kind $label"
      continue
    fi
    case "$kind" in
      launchd)
        if command -v launchctl >/dev/null 2>&1; then
          launchctl bootout "gui/$(id -u)" "$unit_path" >/dev/null 2>&1 || launchctl unload "$unit_path" >/dev/null 2>&1 || true
          record_action launch_agent "$unit_path" unload done "" "requested launchd unload for $label"
        else
          record_action launch_agent "$unit_path" unload skipped "" "launchctl not found"
        fi
        ;;
      systemd_user)
        if command -v systemctl >/dev/null 2>&1; then
          systemctl --user disable --now "$label" >/dev/null 2>&1 || systemctl --user stop "$label" >/dev/null 2>&1 || true
          record_action launch_agent "$unit_path" unload done "" "requested systemd user stop for $label"
        else
          record_action launch_agent "$unit_path" unload skipped "" "systemctl not found"
        fi
        ;;
      *)
        record_action launch_agent "$unit_path" unload skipped "" "unknown launch agent kind $kind"
        ;;
    esac
    if [ -e "$unit_path" ]; then
      if rm -f "$unit_path"; then
        record_action launch_agent "$unit_path" remove done "" "removed unit file"
      else
        record_action launch_agent "$unit_path" remove failed "" "failed to remove unit file"
      fi
    else
      record_action launch_agent "$unit_path" remove skipped "" "unit file already absent"
    fi
  done <"$OMNIGENT_UNINSTALL_LEDGER_MANIFEST"
}

profile_candidates() {
  printf '%s\n' \
    "$HOME/.zprofile" \
    "$HOME/.zshrc" \
    "$HOME/.bash_profile" \
    "$HOME/.bashrc" \
    "$HOME/.profile" \
    "$HOME/.config/fish/config.fish"
  if [ -d "$HOME/.config/fish/conf.d" ]; then
    find "$HOME/.config/fish/conf.d" -type f -name '*.fish' 2>/dev/null
  fi
}

has_shell_install_signal() {
  [ -n "${OMNIGENT_UNINSTALL_LEDGER_SOURCE:-}" ] && [ "$OMNIGENT_UNINSTALL_LEDGER_SOURCE" != unknown ] && return 0
  [ -f "$(state_home)/installation_id" ] && return 0
  command -v omnigent >/dev/null 2>&1 && return 0
  command -v omni >/dev/null 2>&1 && return 0
  profiles_file="$(mktemp "${TMPDIR:-/tmp}/omnigent-uninstall-anchor-profiles.XXXXXX")" || return 1
  profile_candidates >"$profiles_file"
  while IFS= read -r profile; do
    if profile_has_block "$profile"; then
      rm -f "$profiles_file"
      return 0
    fi
  done <"$profiles_file"
  rm -f "$profiles_file"
  return 1
}

profile_has_block() {
  [ -f "$1" ] || return 1
  awk -v begin="$MARKER_BEGIN" -v end="$MARKER_END" '
    $0 == begin { found_begin=1 }
    found_begin && $0 == end { found_end=1 }
    END { exit(found_begin && found_end ? 0 : 1) }
  ' "$1"
}

sha256_file() {
  file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$file" | awk '{print $1}'
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 "$file" | awk '{print $NF}'
  else
    return 1
  fi
}

write_profile_block() {
  profile="$1"
  output="$2"
  awk -v begin="$MARKER_BEGIN" -v end="$MARKER_END" '
    $0 == begin { printing=1 }
    printing { print }
    printing && $0 == end { exit }
  ' "$profile" >"$output"
}

remove_profile_block() {
  profile="$1"
  expected_sha="${2:-}"
  if ! profile_has_block "$profile"; then
    record_action profile_block "$profile" remove skipped "" "marker block absent"
    return 0
  fi
  line_range="$(awk -v begin="$MARKER_BEGIN" -v end="$MARKER_END" '
    $0 == begin { start=NR }
    start && $0 == end { print start "-" NR; exit }
  ' "$profile")"
  if [ "$DRY_RUN" = true ]; then
    record_action profile_block "$profile" remove reported "" "would remove lines $line_range"
    return 0
  fi
  if [ -n "$expected_sha" ]; then
    block_file="$(mktemp "${TMPDIR:-/tmp}/omnigent-uninstall-block.XXXXXX")" || return 1
    write_profile_block "$profile" "$block_file"
    actual_sha="$(sha256_file "$block_file" 2>/dev/null || true)"
    rm -f "$block_file"
    if [ -z "$actual_sha" ]; then
      record_action profile_block "$profile" remove failed "" "could not verify block hash"
      return 1
    fi
    if [ "$actual_sha" != "$expected_sha" ] && [ "$FORCE" != true ]; then
      record_action profile_block "$profile" remove failed "--force" "marker block hash mismatch; refusing tampered block"
      EXIT_CODE=3
      return 1
    fi
  fi
  backup="$profile.omnigent.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  tmp="$(mktemp "$profile.omnigent.tmp.XXXXXX")" || return 1
  if ! cp "$profile" "$backup"; then
    record_action profile_block "$profile" remove failed "" "failed to write backup"
    return 1
  fi
  if awk -v begin="$MARKER_BEGIN" -v end="$MARKER_END" '
    $0 == begin { skipping=1; found=1; next }
    skipping && $0 == end { skipping=0; next }
    !skipping { print }
    END { exit(found ? 0 : 42) }
  ' "$profile" >"$tmp" && mv "$tmp" "$profile"; then
    record_action profile_block "$profile" remove done "" "block removed, backup at $backup"
  else
    rm -f "$tmp"
    record_action profile_block "$profile" remove failed "" "failed to remove block"
  fi
}

cleanup_profiles() {
  if [ -n "${OMNIGENT_UNINSTALL_LEDGER_MANIFEST:-}" ] && [ -f "$OMNIGENT_UNINSTALL_LEDGER_MANIFEST" ]; then
    while IFS="$TAB" read -r artifact profile expected_sha source confidence rest; do
      [ "$artifact" = profile_block ] || continue
      remove_profile_block "$profile" "$expected_sha"
      [ "$EXIT_CODE" = 3 ] && return 1
    done <"$OMNIGENT_UNINSTALL_LEDGER_MANIFEST"
  else
    profiles_file="$(mktemp "${TMPDIR:-/tmp}/omnigent-uninstall-profiles.XXXXXX")" || return 1
    profile_candidates >"$profiles_file"
    while IFS= read -r profile; do
      [ -n "$profile" ] || continue
      if profile_has_block "$profile"; then
        remove_profile_block "$profile" ""
        if [ "$EXIT_CODE" = 3 ]; then
          rm -f "$profiles_file"
          return 1
        fi
      fi
    done <"$profiles_file"
    rm -f "$profiles_file"
  fi
}

remove_delimited_external_config() {
  path="$1"
  marker="$2"
  [ -f "$path" ] || { record_action external_config "$path" remove skipped "" "already absent"; return 0; }
  if ! grep -F "$marker" "$path" >/dev/null 2>&1; then
    record_action external_config "$path" remove skipped "" "marker absent"
    return 0
  fi
  if [ "$DRY_RUN" = true ]; then
    record_action external_config "$path" remove reported "" "would remove marker block $marker"
    return 0
  fi
  backup="$path.omnigent.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  tmp="$(mktemp "$path.omnigent.tmp.XXXXXX")" || return 1
  cp "$path" "$backup" || { record_action external_config "$path" remove failed "" "failed to write backup"; return 1; }
  if awk -v marker="$marker" '
    index($0, marker) && !skipping { skipping=1; found=1; next }
    index($0, marker) && skipping { skipping=0; next }
    !skipping { print }
    END { exit(found && !skipping ? 0 : 42) }
  ' "$path" >"$tmp" && mv "$tmp" "$path"; then
    record_action external_config "$path" remove done "" "removed marker block, backup at $backup"
  else
    rm -f "$tmp"
    record_action external_config "$path" remove failed "" "failed to remove marker block"
  fi
}

remove_keyed_external_config() {
  path="$1"
  marker="$2"
  format="$3"
  [ -f "$path" ] || { record_action external_config "$path" remove skipped "" "already absent"; return 0; }
  if [ "$DRY_RUN" = true ]; then
    record_action external_config "$path" remove reported "" "would remove $marker from $format config"
    return 0
  fi
  if [ "$format" = toml ]; then
    backup="$path.omnigent.bak.$(date -u +%Y%m%dT%H%M%SZ)"
    tmp="$(mktemp "$path.omnigent.tmp.XXXXXX")" || return 1
    cp "$path" "$backup" || { record_action external_config "$path" remove failed "" "failed to write backup"; return 1; }
    if awk -v marker="$marker" '
      $0 == "[" marker "]" || $0 ~ "^\\[" marker "\\." { skipping=1; found=1; next }
      skipping && /^\[/ { skipping=0 }
      !skipping { print }
      END { exit(found ? 0 : 42) }
    ' "$backup" >"$tmp" && mv "$tmp" "$path"; then
      record_action external_config "$path" remove done "" "removed TOML table $marker, backup at $backup"
    else
      rm -f "$tmp"
      record_action external_config "$path" remove skipped "" "TOML table $marker absent"
    fi
    return 0
  fi
  py="$(command -v python3 2>/dev/null || command -v python 2>/dev/null || true)"
  if [ -z "$py" ]; then
    record_action external_config "$path" remove skipped "" "python not found for structured config edit"
    return 0
  fi
  backup="$path.omnigent.bak.$(date -u +%Y%m%dT%H%M%SZ)"
  cp "$path" "$backup" || { record_action external_config "$path" remove failed "" "failed to write backup"; return 1; }
  if "$py" - "$path" "$marker" "$format" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
marker = sys.argv[2]
fmt = sys.argv[3]
if fmt != "json":
    raise SystemExit(42)
original = path.read_text()
data = json.loads(original)
current = data
parts = marker.split(".")
for part in parts[:-1]:
    current = current.get(part, {}) if isinstance(current, dict) else {}
if not isinstance(current, dict) or parts[-1] not in current:
    raise SystemExit(42)
current.pop(parts[-1])
indent = 2 if "\n  \"" in original else None
path.write_text(json.dumps(data, indent=indent, sort_keys=False) + "\n")
PY
  then
    record_action external_config "$path" remove done "" "removed $marker, backup at $backup"
  else
    record_action external_config "$path" remove skipped "" "unsupported structured format; backup kept at $backup"
  fi
}

cleanup_external_configs() {
  [ -n "${OMNIGENT_UNINSTALL_LEDGER_MANIFEST:-}" ] && [ -f "$OMNIGENT_UNINSTALL_LEDGER_MANIFEST" ] || return 0
  while IFS="$TAB" read -r artifact path marker format expected_sha source confidence rest; do
    [ "$artifact" = external_config ] || continue
    if [ "$MODIFY_EXTERNAL_CONFIG" != true ]; then
      record_action external_config "$path" remove skipped "--modify-external-config" "gate not provided"
      continue
    fi
    if { [ "$source" = inferred ] || [ "$confidence" = low ] || [ "$confidence" = medium ]; } && [ "$ASSUME_INFERRED" != true ]; then
      record_action external_config "$path" remove skipped "--assume-inferred" "confidence gate not provided"
      continue
    fi
    if [ "$format" = delimited_block ]; then
      remove_delimited_external_config "$path" "$marker"
    else
      remove_keyed_external_config "$path" "$marker" "$format"
    fi
  done <"$OMNIGENT_UNINSTALL_LEDGER_MANIFEST"
}

archive_path_for() {
  target="$1"
  if [ -n "${XDG_STATE_HOME:-}" ]; then
    backup_root="$XDG_STATE_HOME/omnigent-backups"
  else
    backup_root="$HOME/.omnigent-backups"
  fi
  mkdir -p "$backup_root" || return 1
  ts="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
  base="$(printf '%s' "$target" | sed 's/[^A-Za-z0-9._-]/_/g; s/^_*//; s/_*$//')"
  if command -v zstd >/dev/null 2>&1; then
    printf '%s/%s-%s-%s.tar.zst\n' "$backup_root" "$ts" "$base" "$$"
  else
    printf '%s/%s-%s-%s.tar.gz\n' "$backup_root" "$ts" "$base" "$$"
  fi
}

backup_path() {
  target="$1"
  [ -e "$target" ] || return 0
  if [ "$NO_BACKUP" = true ]; then
    return 0
  fi
  archive="$(archive_path_for "$target")" || return 1
  parent="$(dirname "$target")"
  base="$(basename "$target")"
  case "$archive" in
    *.tar.zst)
      tar_tmp="$(mktemp "$archive.tar.XXXXXX")" || return 1
      if ! tar -cf "$tar_tmp" -C "$parent" "$base"; then
        rm -f "$tar_tmp" "$archive"
        return 1
      fi
      if ! zstd -q -o "$archive" "$tar_tmp"; then
        rm -f "$tar_tmp" "$archive"
        return 1
      fi
      rm -f "$tar_tmp"
      ;;
    *.tar.gz)
      tar -czf "$archive" -C "$parent" "$base" || return 1
      ;;
  esac
  printf '%s\n' "$archive" >>"$BACKUPS_FILE"
  record_action backup "$target" archive done "" "restore with: tar -xf $archive -C $parent"
}

remove_tree() {
  artifact="$1"
  path="$2"
  gate="$3"
  if [ ! -e "$path" ]; then
    record_action "$artifact" "$path" remove skipped "" "already absent"
    return 0
  fi
  if [ -n "$gate" ]; then
    record_action "$artifact" "$path" remove skipped "$gate" "gate not provided"
    return 0
  fi
  if [ "$DRY_RUN" = true ]; then
    size="$(du -sk "$path" 2>/dev/null | awk '{print $1 " KiB"}' || printf unknown)"
    record_action "$artifact" "$path" remove reported "" "would remove $size"
    return 0
  fi
  if backup_path "$path" && rm -rf "$path"; then
    record_action "$artifact" "$path" remove done "" "removed"
  else
    record_action "$artifact" "$path" remove failed "" "failed to remove"
  fi
}

desktop_paths() {
  case "$(uname -s)" in
    Darwin)
      printf '%s\n' \
        "$HOME/Library/Application Support/Omnigent" \
        "$HOME/Library/Caches/Omnigent" \
        "$HOME/Library/Logs/Omnigent"
      ;;
    *)
      printf '%s\n' \
        "${XDG_CONFIG_HOME:-$HOME/.config}/Omnigent" \
        "${XDG_CACHE_HOME:-$HOME/.cache}/Omnigent" \
        "${XDG_STATE_HOME:-$HOME/.local/state}/Omnigent"
      ;;
  esac
}

purge_state() {
  remove_tree state "$(state_home)" ""
  workspace="$HOME/omnigent"
  if [ -e "$workspace" ]; then
    if [ "$PURGE_WORKSPACE" = true ]; then
      remove_tree workspace "$workspace" ""
    elif [ "$YES" = true ]; then
      record_action workspace "$workspace" remove skipped "--purge-workspace" "workspace kept"
    elif [ "$DRY_RUN" = true ]; then
      record_action workspace "$workspace" remove reported "--purge-workspace" "would prompt separately"
    else
      printf 'Remove workspace %s? [y/N] ' "$workspace" >/dev/tty 2>/dev/null || true
      answer=
      IFS= read -r answer </dev/tty 2>/dev/null || true
      case "$answer" in
        y | Y | yes | YES | Yes) remove_tree workspace "$workspace" "" ;;
        *) record_action workspace "$workspace" remove skipped "--purge-workspace" "workspace kept" ;;
      esac
    fi
  fi
}

purge_desktop_data() {
  desktop_file="$(mktemp "${TMPDIR:-/tmp}/omnigent-uninstall-desktop.XXXXXX")" || return 1
  desktop_paths >"$desktop_file"
  while IFS= read -r path; do
    [ -n "$path" ] || continue
    remove_tree desktop_data "$path" ""
  done <"$desktop_file"
  rm -f "$desktop_file"
}

report_shared_deps() {
  for dep in uv node npm tmux bwrap; do
    dep_path="$(command -v "$dep" 2>/dev/null || true)"
    if [ -n "$dep_path" ]; then
      record_action shared_dep "$dep" report reported "" "present at $dep_path; not removed"
    fi
  done
}

uninstall_wheel() {
  if [ "$DRY_RUN" = true ]; then
    record_action wheel omnigent remove reported "" "would run uv tool uninstall omnigent"
    return 0
  fi
  if ! command -v uv >/dev/null 2>&1; then
    if ! command -v omnigent >/dev/null 2>&1 && ! command -v omni >/dev/null 2>&1; then
      record_action wheel omnigent remove skipped "" "already absent"
      return 0
    fi
    record_action wheel omnigent remove failed "" "uv not found; remove the tool manually"
    return 1
  fi
  uv_output="$(mktemp "${TMPDIR:-/tmp}/omnigent-uninstall-uv.XXXXXX")" || return 1
  if uv tool uninstall omnigent >"$uv_output" 2>&1; then
    rm -f "$uv_output"
    record_action wheel omnigent remove done "" "uv tool uninstall omnigent"
  else
    output="$(cat "$uv_output" 2>/dev/null || true)"
    rm -f "$uv_output"
    case "$output" in
      *'not installed'* | *'No tool'* | *'not found'*)
        record_action wheel omnigent remove skipped "" "already absent"
        ;;
      *)
        record_action wheel omnigent remove failed "" "uv tool uninstall failed: $output"
        ;;
    esac
  fi
}

emit_json() {
  printf '{\n'
  printf '  "schema_version": 1,\n'
  printf '  "dry_run": %s,\n' "$DRY_RUN"
  printf '  "ledger_source": "%s",\n' "${OMNIGENT_UNINSTALL_LEDGER_SOURCE:-unknown}"
  printf '  "actions": [\n'
  first=true
  if [ -f "$ACTIONS_FILE" ]; then
    while IFS= read -r line; do
      if [ "$first" = true ]; then first=false; else printf ',\n'; fi
      printf '    %s' "$line"
    done <"$ACTIONS_FILE"
  fi
  printf '\n  ],\n'
  printf '  "backups": ['
  first=true
  if [ -f "$BACKUPS_FILE" ]; then
    while IFS= read -r line; do
      if [ "$first" = true ]; then first=false; else printf ','; fi
      printf '"%s"' "$(json_escape "$line")"
    done <"$BACKUPS_FILE"
  fi
  printf '],\n'
  printf '  "summary": {"done": %s, "skipped": %s, "failed": %s, "reported": %s},\n' "$DONE" "$SKIPPED" "$FAILED" "$REPORTED"
  printf '  "exit_code": %s\n' "$EXIT_CODE"
  printf '}\n'
}

if ! has_shell_install_signal; then
  record_action anchor omnigent detect failed "" "no Omnigent install detected"
  EXIT_CODE=3
  [ "$JSON" = true ] && emit_json
  exit "$EXIT_CODE"
fi

unload_launch_agents
if ! stop_processes; then
  [ "$JSON" = true ] && emit_json
  exit "$EXIT_CODE"
fi

if has_target cli; then
  cleanup_profiles
  if [ "$EXIT_CODE" = 3 ]; then
    [ "$JSON" = true ] && emit_json
    exit "$EXIT_CODE"
  fi
  cleanup_external_configs
fi
if has_target state; then
  if [ "$PURGE" = true ]; then
    purge_state
  else
    record_action state "$(state_home)" remove skipped "--purge" "state preserved"
  fi
fi
if has_target desktop-data; then
  purge_desktop_data
fi
report_shared_deps
if has_target cli; then
  uninstall_wheel
fi

if [ "$JSON" = true ]; then
  emit_json
elif [ "$DRY_RUN" = true ]; then
  printf 'Preview only — nothing was changed. Re-run with --yes to apply CLI cleanup, or --purge --yes to remove state.\n'
elif [ -f "$BACKUPS_FILE" ]; then
  while IFS= read -r backup; do
    printf 'backup: %s\n' "$backup"
  done <"$BACKUPS_FILE"
fi

exit "$EXIT_CODE"
