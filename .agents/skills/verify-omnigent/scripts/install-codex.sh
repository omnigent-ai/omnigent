#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${OMNIGENT_VERIFY_REPO_ROOT:-}" ]]; then
  repo_root="$OMNIGENT_VERIFY_REPO_ROOT"
elif repo_root="$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null)"; then
  :
else
  echo "install-codex: run from the target Git checkout or set OMNIGENT_VERIFY_REPO_ROOT" >&2
  exit 2
fi
repo_root="$(cd "$repo_root" && pwd)"
source_dir="$repo_root/.agents/skills/verify-omnigent"
if [[ ! -f "$source_dir/scripts/verify.sh" ]]; then
  echo "install-codex: target checkout has no canonical verify-omnigent skill: $repo_root" >&2
  exit 2
fi
mode="${1:---host}"

usage() {
  echo "usage: $0 [--host [SKILLS_DIR] | --bundle AGENT_BUNDLE_DIR]" >&2
}

install_link() {
  local skills_dir="$1"
  local target="$skills_dir/verify-omnigent"
  mkdir -p "$skills_dir"
  if [[ -L "$target" && "$(readlink "$target")" == "$source_dir" ]]; then
    echo "verify-omnigent already installed: $target"
    return
  fi
  if [[ -e "$target" || -L "$target" ]]; then
    echo "refusing to replace existing Codex skill: $target" >&2
    return 1
  fi
  ln -s "$source_dir" "$target"
  echo "installed verify-omnigent for Codex: $target"
}

install_copy() {
  local bundle_dir="$1"
  local target="$bundle_dir/skills/verify-omnigent"
  if [[ ! -f "$bundle_dir/config.yaml" && ! -f "$bundle_dir/agent.yaml" ]]; then
    echo "not an Omnigent agent bundle (missing config.yaml or agent.yaml): $bundle_dir" >&2
    return 1
  fi
  if [[ -e "$target" || -L "$target" ]]; then
    echo "refusing to replace existing bundled skill: $target" >&2
    return 1
  fi
  mkdir -p "$bundle_dir/skills"
  python3 -c \
    'import shutil, sys; shutil.copytree(sys.argv[1], sys.argv[2], symlinks=False)' \
    "$source_dir" "$target"
  echo "copied verify-omnigent into agent bundle: $target"
}

case "$mode" in
  --host)
    [[ "$#" -le 2 ]] || {
      usage
      exit 2
    }
    install_link "${2:-$HOME/.codex/skills}"
    ;;
  --bundle)
    [[ "$#" -eq 2 ]] || {
      usage
      exit 2
    }
    install_copy "$2"
    ;;
  *)
    usage
    exit 2
    ;;
esac
