#!/usr/bin/env bash
# Eval guard for unsandboxed harnesses (claude-native runs with the real HOME).
# Linked as omnigent, omni, uv, pipx, pip, pip3, and brew ahead of the real
# binaries on PATH: read-only commands pass through, and anything that would
# change this machine (stop servers, install, upgrade, uninstall, write config)
# is refused so the agent describes it instead.
set -u
self_dir="$(cd "$(dirname "$0")" && pwd)"
name="$(basename "$0")"
real=""
IFS=:
for dir in $PATH; do
  [ "$dir" = "$self_dir" ] && continue
  if [ -x "$dir/$name" ]; then real="$dir/$name"; break; fi
done
unset IFS
[ -n "$real" ] || { echo "$name: command not found" >&2; exit 127; }

refuse() {
  echo "This environment disables '$name $*' because it would change the machine. Describe the command to the user instead of running it." >&2
  exit 1
}

args=" $* "
case "$name" in
  omnigent|omni)
    case "$args" in *" --help "*|*" -h "*) exec "$real" "$@" ;; esac
    case "${1:-}" in
      ""|--version|version|diagnose) exec "$real" "$@" ;;
      upgrade|update)
        case "$args" in *" --check "*|*" --dry-run "*) exec "$real" "$@" ;; esac
        refuse "$@" ;;
      uninstall)
        # Bare uninstall only previews; the destructive flags are refused.
        case "$args" in *" --yes "*|*" -y "*|*" --purge "*|*" --force "*) refuse "$@" ;; esac
        exec "$real" "$@" ;;
      config)
        [ "${2:-}" = "list" ] && exec "$real" "$@"
        refuse "$@" ;;
      *) refuse "$@" ;;
    esac ;;
  uv|pipx|pip|pip3|brew)
    case "${1:-} ${2:-}" in
      "tool install"*|"tool upgrade"*|"tool uninstall"*|"pip install"*|"pip uninstall"*|"self update"*) refuse "$@" ;;
    esac
    case "${1:-}" in
      install|upgrade|uninstall|reinstall|remove) refuse "$@" ;;
    esac
    exec "$real" "$@" ;;
esac
exec "$real" "$@"
