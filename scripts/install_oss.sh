#!/bin/sh

# Omnigent installer.
#
# Installs the published `omnigent` wheel from PyPI with uv, wires up PATH,
# and points you at first-run. The wheel bundles the prebuilt web UI, so the
# default install needs no Node/npm and runs no build.
#
# Options:
#   --version X   install a specific PyPI release (default: latest)
#   --repo URL    install from a git checkout instead (builds from source;
#                 requires Node 22+/npm) — for development
#   --non-interactive, --verbose
#
# uv and git (only with --repo) are required; the installer offers to install
# them if missing. Node/npm are needed by the Claude/Codex/Pi harnesses, tmux
# by their terminal launchers, and bubblewrap (Linux only) to OS-sandbox those
# terminals — missing ones are warnings, not errors, unless building from
# source.

set -eu

# Published PyPI package, the default install. --version pins a release.
PACKAGE_NAME="omnigent"
VERSION=
# Set by --repo to install from a git checkout instead (development; builds
# the web UI from source). Empty => install the published wheel from PyPI.
REPO_URL=
PYTHON_VERSION="3.12"
INSTALL_URL=
NON_INTERACTIVE=false
VERBOSE=false
ESC=$(printf '\033')
RESET=
BOLD=
DIM=
CYAN=
GREEN=
YELLOW=
RED=

use_terminal_ui() {
  [ -t 1 ] && [ "${TERM:-}" != "dumb" ]
}

init_style() {
  if use_terminal_ui && [ -z "${NO_COLOR:-}" ]; then
    RESET="${ESC}[0m"
    BOLD="${ESC}[1m"
    DIM="${ESC}[2m"
    CYAN="${ESC}[36m"
    GREEN="${ESC}[32m"
    YELLOW="${ESC}[33m"
    RED="${ESC}[31m"
  fi
}

usage() {
  printf 'Usage: install_oss.sh [--non-interactive] [--verbose] [--version X] [--repo URL]\n'
}

step() {
  printf '%s==>%s %s\n' "$CYAN" "$RESET" "$1"
}

verbose() {
  if [ "$VERBOSE" = true ]; then
    printf '%sDEBUG:%s %s\n' "$DIM" "$RESET" "$1" >&2
  fi
}

warn() {
  printf '%sWARNING:%s %s\n' "$YELLOW" "$RESET" "$1" >&2
}

fail() {
  printf '%sERROR:%s %s\n' "$RED" "$RESET" "$1" >&2
  exit 1
}

spinner_frame() {
  case $(($1 % 4)) in
    0) printf '-' ;;
    1) printf '%s' "\\" ;;
    2) printf '|' ;;
    *) printf '/' ;;
  esac
}

run_with_spinner() {
  label="$1"
  shift

  if [ "$VERBOSE" = true ]; then
    verbose "Running: $*"
    "$@"
    return
  fi

  if ! use_terminal_ui; then
    "$@"
    return
  fi

  log_file="${TMPDIR:-/tmp}/omnigent-oss-installer.$$.log"
  status_file="${TMPDIR:-/tmp}/omnigent-oss-installer.$$.status"
  rm -f "$log_file" "$status_file"

  (
    if "$@" >"$log_file" 2>&1; then
      printf '0\n' >"$status_file"
    else
      printf '%s\n' "$?" >"$status_file"
    fi
  ) &
  command_pid=$!

  frame=0
  while [ ! -f "$status_file" ]; do
    spinner="$(spinner_frame "$frame")"
    printf '\r\033[K%s%s%s %s%s%s' "$CYAN" "$spinner" "$RESET" "$BOLD" "$label" "$RESET"
    frame=$((frame + 1))
    sleep 0.1
  done

  wait "$command_pid" 2>/dev/null || true
  status="$(cat "$status_file")"
  rm -f "$status_file"

  if [ "$status" = 0 ]; then
    printf '\r\033[K%sok%s %s\n' "$GREEN" "$RESET" "$label"
    rm -f "$log_file"
    return
  fi

  printf '\r\033[K'
  warn "$label failed"
  cat "$log_file" >&2
  rm -f "$log_file"
  return "$status"
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --non-interactive)
        NON_INTERACTIVE=true
        ;;
      --verbose)
        VERBOSE=true
        ;;
      --repo)
        if [ "$#" -lt 2 ]; then
          usage >&2
          exit 1
        fi
        REPO_URL="$2"
        shift
        ;;
      --version)
        if [ "$#" -lt 2 ]; then
          usage >&2
          exit 1
        fi
        VERSION="$2"
        shift
        ;;
      *)
        usage >&2
        exit 1
        ;;
    esac
    shift
  done
}

normalize_repo_url() {
  case "$REPO_URL" in
    "")
      # No --repo: install the published wheel from PyPI (the default).
      INSTALL_URL=
      return
      ;;
    git+ssh://* | git+https://* | git+http://*)
      INSTALL_URL="$REPO_URL"
      ;;
    ssh://*)
      INSTALL_URL="git+$REPO_URL"
      ;;
    https://* | http://*)
      INSTALL_URL="git+$REPO_URL"
      ;;
    *@*:*)
      user_host="${REPO_URL%%:*}"
      repo_path="${REPO_URL#*:}"
      INSTALL_URL="git+ssh://$user_host/$repo_path"
      ;;
    *)
      fail "Unsupported --repo URL: $REPO_URL. Use https://..., ssh://..., or git@host:org/repo.git."
      ;;
  esac

  if [ -n "$VERSION" ]; then
    fail "--version pins a PyPI release and cannot be combined with --repo (a git source install)."
  fi
  verbose "Repository install URL: $INSTALL_URL"
}

# True only for a from-source git install (--repo): that path builds the web
# UI with npm, so Node/npm are hard requirements. A default PyPI install pulls
# the prebuilt wheel and needs neither.
building_from_source() {
  [ -n "$INSTALL_URL" ]
}

prompt_yes_no() {
  prompt="$1"

  if [ "$NON_INTERACTIVE" = true ]; then
    return 1
  fi

  if ! ( : </dev/tty ) 2>/dev/null; then
    return 1
  fi

  printf '%s [Y/n] ' "$prompt" >/dev/tty
  if ! IFS= read -r answer </dev/tty; then
    answer=
  fi

  case "$answer" in
    "" | y | Y | yes | YES | Yes)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

check_platform() {
  case "$(uname -s)" in
    Darwin | Linux)
      ;;
    *)
      fail "install_oss.sh supports macOS and Linux only."
      ;;
  esac
}

# Hard prerequisites. uv is always required (it performs the install). git is
# only needed for a from-source `--repo` install — a PyPI wheel install never
# touches git — so we check it only in that mode. The installer OFFERS to
# install each with a trusted one-line installer rather than just failing.
check_prerequisites() {
  if building_from_source; then
    ensure_git
  fi
  ensure_uv
}

ensure_git() {
  if command -v git >/dev/null 2>&1; then
    step "git is available"
    return
  fi
  case "$(uname -s)" in
    Linux)
      install_cmd="$(linux_pkg_install_cmd git)"
      if [ -n "$install_cmd" ] && prompt_yes_no "git is required and not installed. Install it now ($install_cmd)?"; then
        run_with_spinner "install git" sh -c "$install_cmd" || true
        command -v git >/dev/null 2>&1 && return
      fi
      ;;
    Darwin)
      warn "git is required. Install the Xcode command-line tools with: xcode-select --install"
      ;;
  esac
  fail "git is required. Install it, then rerun this installer."
}

# uv performs the install, so it's required; offer the official one-liner,
# and fall back to a fail-with-hint on decline/non-interactive/failure.
ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    step "uv is available"
    return
  fi
  installer='curl -LsSf https://astral.sh/uv/install.sh | sh'
  if prompt_yes_no "uv is required and not installed. Install it now ($installer)?"; then
    run_with_spinner "install uv" sh -c "$installer" || true
    # uv's installer drops the binary in ~/.local/bin (or ~/.cargo/bin) and
    # wires PATH for *future* shells; pull it onto PATH for the rest of this run.
    if ! command -v uv >/dev/null 2>&1; then
      for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
        if [ -x "$d/uv" ]; then
          PATH="$d:$PATH"
          export PATH
          break
        fi
      done
    fi
    if command -v uv >/dev/null 2>&1; then
      step "Installed uv"
      return
    fi
    fail "uv installed but is not on PATH — open a new shell and rerun this installer."
  fi
  fail "uv is required. Install from https://docs.astral.sh/uv/getting-started/installation/, then rerun."
}

# ── Harness toolchain ────────────────────────────────────────────────
#
# Node 22+/npm power the Claude/Codex/Pi harnesses; the default PyPI install
# and the bare web UI don't need them, so a missing one is a warning here.
# With --repo (source build) npm is required up front, so it becomes an error.

# Report a missing prereq: fatal when building from source, otherwise a warning.
require_or_warn() {
  if building_from_source; then
    fail "$1"
  fi
  warn "$1"
}

# Node >= 22.10: the Claude/Codex/Pi CLIs need worker_threads.markAsUncloneable
# (added in 22.10). Probe the symbol rather than parse a version string.
check_node() {
  if ! command -v node >/dev/null 2>&1; then
    require_or_warn "node not found — Node.js 22+ is needed for the Claude/Codex/Pi harnesses (https://nodejs.org)."
    return
  fi
  if node -e "process.exit(typeof require('node:worker_threads').markAsUncloneable === 'function' ? 0 : 1)" >/dev/null 2>&1; then
    step "Node.js is new enough for the harness CLIs"
  else
    require_or_warn "Node.js is older than 22.10 — the Claude/Codex/Pi harnesses need 22 LTS or newer (https://nodejs.org)."
  fi
}

check_npm() {
  if command -v npm >/dev/null 2>&1; then
    step "npm is available (installs the Claude/Codex/Pi harness CLIs on first run)"
  else
    require_or_warn "npm not found — needed to install the Claude/Codex/Pi harness CLIs (https://nodejs.org)."
  fi
}

# `omnigent claude` / `omnigent codex` launch through a local tmux terminal
# and won't start without it, so surface it up front and offer to install it.
# Emit the package-manager command that installs $1 on this Linux box, or
# nothing when no known package manager is present. Shared by the tmux and
# git install offers.
linux_pkg_install_cmd() {
  pkg="$1"
  if command -v apt-get >/dev/null 2>&1; then
    printf 'sudo apt-get install -y %s' "$pkg"
  elif command -v dnf >/dev/null 2>&1; then
    printf 'sudo dnf install -y %s' "$pkg"
  elif command -v yum >/dev/null 2>&1; then
    printf 'sudo yum install -y %s' "$pkg"
  elif command -v pacman >/dev/null 2>&1; then
    printf 'sudo pacman -S --noconfirm %s' "$pkg"
  elif command -v zypper >/dev/null 2>&1; then
    printf 'sudo zypper install -y %s' "$pkg"
  fi
}

check_tmux() {
  if command -v tmux >/dev/null 2>&1; then
    step "tmux is available"
    return
  fi

  case "$(uname -s)" in
    Darwin)
      if command -v brew >/dev/null 2>&1; then
        if prompt_yes_no "tmux is missing (needed for \`omnigent claude\` / \`omnigent codex\`). Install it with brew?"; then
          run_with_spinner "brew install tmux" brew install tmux || warn "brew install tmux failed — install tmux manually before \`omnigent claude\`."
          return
        fi
      fi
      warn "tmux not found — \`omnigent claude\` / \`omnigent codex\` need it. Install with: brew install tmux"
      ;;
    Linux)
      install_cmd="$(linux_pkg_install_cmd tmux)"
      if [ -n "$install_cmd" ] && prompt_yes_no "tmux is missing (needed for \`omnigent claude\` / \`omnigent codex\`). Install it now ($install_cmd)?"; then
        run_with_spinner "install tmux" sh -c "$install_cmd" || warn "tmux install failed — run manually: $install_cmd"
        return
      fi
      if [ -n "$install_cmd" ]; then
        warn "tmux not found — \`omnigent claude\` / \`omnigent codex\` need it. Install with: $install_cmd"
      else
        warn "tmux not found — \`omnigent claude\` / \`omnigent codex\` need it. Install it with your package manager."
      fi
      ;;
  esac
}

# The native `omnigent claude` / `omnigent codex` / `pi` harnesses wrap each
# agent terminal in a bubblewrap (`bwrap`) OS-sandbox; on Linux that isolation
# is mandatory and fail-loud, so a missing `bwrap` binary makes those terminals
# fail to start. macOS sandboxes with the built-in seatbelt backend and needs
# nothing here, so this check is Linux-only.
check_bubblewrap() {
  [ "$(uname -s)" = Linux ] || return 0

  if command -v bwrap >/dev/null 2>&1; then
    step "bubblewrap (bwrap) is available"
    return
  fi

  install_cmd="$(linux_pkg_install_cmd bubblewrap)"
  if [ -n "$install_cmd" ] && prompt_yes_no "bubblewrap is missing (needed to sandbox native \`omnigent claude\` / \`omnigent codex\` terminals). Install it now ($install_cmd)?"; then
    run_with_spinner "install bubblewrap" sh -c "$install_cmd" || warn "bubblewrap install failed — run manually: $install_cmd"
    return
  fi
  if [ -n "$install_cmd" ]; then
    warn "bubblewrap (bwrap) not found — native \`omnigent claude\` / \`omnigent codex\` terminals need it on Linux. Install with: $install_cmd"
  else
    warn "bubblewrap (bwrap) not found — native \`omnigent claude\` / \`omnigent codex\` terminals need it on Linux. Install it with your package manager."
  fi
}

install_omnigent() {
  # Default: the published PyPI wheel (`omnigent`, optionally `omnigent==X`).
  # The wheel ships the prebuilt web UI, so there is no npm/Node step and no
  # source build — the fast, reliable path. `--repo` switches INSTALL_URL to a
  # git ref, which builds from source (and needs npm, checked above).
  if building_from_source; then
    target="$INSTALL_URL"
    step "Installing Omnigent from source (Python $PYTHON_VERSION)"
  elif [ -n "$VERSION" ]; then
    target="${PACKAGE_NAME}==${VERSION}"
    step "Installing Omnigent $VERSION (Python $PYTHON_VERSION)"
  else
    target="$PACKAGE_NAME"
    step "Installing Omnigent (Python $PYTHON_VERSION)"
  fi
  # --force so re-running upgrades instead of no-op'ing; -q hides uv's
  # "Installed N executables" summary (the package also ships an `omni` alias).
  run_with_spinner "uv tool install" uv tool install --force -q --python "$PYTHON_VERSION" "$target"
}

uv_tool_bin_dir() {
  uv tool dir --bin
}

path_contains() {
  dir="$1"

  case ":$PATH:" in
    *":$dir:"*)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

pick_profile() {
  case "$(uname -s):${SHELL:-}" in
    Darwin:*/zsh)
      printf '%s/.zprofile\n' "$HOME"
      ;;
    Darwin:*/bash)
      printf '%s/.bash_profile\n' "$HOME"
      ;;
    Linux:*/zsh)
      printf '%s/.zshrc\n' "$HOME"
      ;;
    Linux:*/bash)
      printf '%s/.bashrc\n' "$HOME"
      ;;
    *)
      printf '%s/.profile\n' "$HOME"
      ;;
  esac
}

maybe_add_bin_to_path() {
  bin_dir="$1"

  if path_contains "$bin_dir"; then
    step "$bin_dir is already on PATH"
    return
  fi

  path_line="export PATH=\"$bin_dir:\$PATH\""
  profile="$(pick_profile)"
  begin_marker="# >>> Omnigent installer >>>"
  end_marker="# <<< Omnigent installer <<<"

  warn "$bin_dir is not on PATH."
  if [ "$NON_INTERACTIVE" = true ]; then
    warn "Add it with: $path_line"
    return
  fi

  if [ -f "$profile" ] && grep -F "$begin_marker" "$profile" >/dev/null 2>&1; then
    if grep -F "$path_line" "$profile" >/dev/null 2>&1; then
      step "PATH is already configured in $profile"
      return
    fi
    fail "$profile already has an Omnigent installer block. Update it manually to: $path_line"
  fi

  if ! prompt_yes_no "Add $bin_dir to PATH in $profile?"; then
    warn "Skipping PATH update. Current shell can run: $path_line"
    return
  fi

  mkdir -p "${profile%/*}"
  {
    printf '\n%s\n' "$begin_marker"
    printf '%s\n' "$path_line"
    printf '%s\n' "$end_marker"
  } >>"$profile"
  step "Added $bin_dir to PATH in $profile"
}

verify_omnigent() {
  bin_dir="$1"
  cli_path="$bin_dir/omnigent"

  if [ ! -x "$cli_path" ]; then
    cli_path="$(command -v omnigent 2>/dev/null || true)"
  fi

  if [ -z "$cli_path" ]; then
    fail "Omnigent installed, but the omnigent command was not found."
  fi

  "$cli_path" --help >/dev/null
  step "Verified $cli_path"

  # `omni` is a shorthand alias installed alongside `omnigent`; check it so a
  # packaging regression that drops it surfaces here rather than later.
  for alias_cmd in omni; do
    if [ ! -x "$bin_dir/$alias_cmd" ] && ! command -v "$alias_cmd" >/dev/null 2>&1; then
      warn "the $alias_cmd alias was not installed (expected a console-script entry point alongside omnigent)."
    fi
  done
}

# No setup step here by design: the first `omnigent` run configures a model
# credential and offers to install the harness CLI you pick.
print_next_steps() {
  bin_dir="$1"
  command_prefix=

  if ! path_contains "$bin_dir"; then
    command_prefix="PATH=\"$bin_dir:\$PATH\" "
  fi

  printf '\n%sOmnigent installed successfully.%s\n\n' "$BOLD" "$RESET"
  printf 'Start chatting — first run sets up a model and a local web UI:\n'
  printf '  %s%somnigent%s\n\n' "$command_prefix" "$CYAN" "$RESET"
  printf 'Or launch a specific coding harness:\n'
  printf '  %somnigent claude          # Claude Code\n' "$command_prefix"
  printf '  %somnigent codex           # Codex\n\n' "$command_prefix"
  printf 'Manage model credentials any time:\n'
  printf '  %somnigent configure harness\n\n' "$command_prefix"
  printf '%sUsing a Databricks workspace as your model provider? Install the\n' "$DIM"
  printf 'Databricks CLI (https://docs.databricks.com/aws/en/dev-tools/cli/install)\n'
  printf 'and add it via: omnigent configure harness -> Databricks.%s\n' "$RESET"
}

main() {
  init_style
  parse_args "$@"
  normalize_repo_url
  check_platform
  check_prerequisites
  check_node
  check_npm
  check_tmux
  check_bubblewrap
  install_omnigent
  bin_dir="$(uv_tool_bin_dir)"
  verify_omnigent "$bin_dir"
  maybe_add_bin_to_path "$bin_dir"
  print_next_steps "$bin_dir"
}

main "$@"
