#!/bin/sh

# Omnigent OSS bootstrap installer.
#
# The external counterpart to ``scripts/install.sh`` (which is the
# Databricks internal-beta bootstrap). This script installs the same
# toolchain an external user actually needs and deliberately drops every
# Databricks-internal piece:
#
#   ported over      uv + git (offer to install if missing), a public git
#                    install, PATH wiring, Python >= 3.12, Node >= 22 + npm +
#                    tmux checks (tmux: offer to install)
#   dropped          the internal PyPI / npm proxies, the SSH access check
#                    to the private repo, the demo `databricks` CLI + lakebox,
#                    `ucode`, the three Databricks OAuth profiles
#   excluded         the trailing `omnigent setup` question — first-run
#                    `omnigent run` owns model-credential + harness setup
#
# Pattern: same structure/helpers as the internal `install.sh`. The internal
# script only checks-and-fails on uv/git/npm; here we additionally OFFER to
# install the deps that have a trusted one-line installer (uv, git, tmux).
# Node/npm are check-and-fail: we never install them (distro Node is often
# older than the required 22.10, so pointing at the official installer is
# safer than a package-manager guess), but a missing/too-old Node or a
# missing npm aborts the run — `uv tool install` builds the web UI with npm
# and would hard-fail later with a noisier error anyway. The GA `databricks`
# CLI is an optional end-of-run hint since only the Databricks model
# provider needs it.
#
# Harness CLIs (claude / codex / pi) are intentionally NOT installed here:
# `omnigent run` / `omnigent configure harness` offer to `npm install`
# the harness you actually pick, so we only verify Node/npm/tmux are present.

set -eu

# Public repository to install from. Override with --repo.
# TODO(oss-release): point this at the public mirror once the repo name
# is final; for now it is overridable so the script is usable today.
REPO_URL="https://github.com/omnigent-ai/omnigent.git"
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
  printf 'Usage: install_oss.sh [--non-interactive] [--verbose] [--repo URL]\n'
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
      fail "--repo requires a non-empty URL."
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

  verbose "Repository install URL: $INSTALL_URL"
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

# Hard prerequisites: without these the package cannot be installed at all.
# The installer OFFERS to install each one with a trusted one-line installer
# rather than just checking-and-failing. git and uv are all it takes.
check_prerequisites() {
  ensure_git
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

# uv is the one true hard dependency with a trusted official one-liner, so we
# offer to run it. On decline / non-interactive / failure we fall back to a
# fail-with-hint (we cannot install the package without uv).
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
# Node and npm are HARD requirements: `uv tool install` builds the web UI
# with npm at install time (setup.py aborts the build without it), and the
# Claude / Codex / Pi harness CLIs are npm packages installed on demand by
# `omnigent run`. Failing here, before the slow install, gives a clearer
# error than the same failure surfacing mid-`uv tool install`. tmux stays a
# soft check (offer to install): only the terminal-harness launchers need it.

# Node >= 22.10: the bundled claude/codex/pi CLIs ship an `undici` that calls
# worker_threads.markAsUncloneable (added in 22.10). Probe the symbol directly
# rather than parse a version string, matching the runtime's Node check.
check_node() {
  if ! command -v node >/dev/null 2>&1; then
    fail "node not found on PATH — Omnigent needs Node.js 22 LTS or newer (a 22.10+ API is required) to build the web UI and run the Claude/Codex/Pi harnesses. Install it (https://docs.npmjs.com/downloading-and-installing-node-js-and-npm), then rerun this installer."
  fi
  if node -e "process.exit(typeof require('node:worker_threads').markAsUncloneable === 'function' ? 0 : 1)" >/dev/null 2>&1; then
    step "Node.js is new enough for the harness CLIs"
  else
    fail "Node.js is too old — Omnigent needs Node.js 22 LTS or newer (a 22.10+ API is required) for the web UI build and the bundled harness CLIs. Upgrade it (https://docs.npmjs.com/downloading-and-installing-node-js-and-npm), then rerun this installer."
  fi
}

check_npm() {
  if command -v npm >/dev/null 2>&1; then
    step "npm is available (builds the web UI; installs harness CLIs on first run)"
  else
    fail "npm not found on PATH — Omnigent needs it to build the web UI at install time and to install the Claude/Codex/Pi CLIs. Install it (https://docs.npmjs.com/downloading-and-installing-node-js-and-npm), then rerun this installer."
  fi
}

# tmux: `omnigent claude` / `omnigent codex` launch the agent through a
# local tmux terminal and refuse to start without it. This was a real
# stumbling block for OSS users (cryptic "tmux was not found" at launch), so
# we surface it up front and offer to install it.
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

install_omnigent() {
  step "Installing Omnigent (Python $PYTHON_VERSION)"
  # --force so re-running the installer actually RE-installs: `uv tool install`
  # is a no-op when the tool is already present (the version is static even
  # when git main has moved), so without it "rerun to upgrade" silently does
  # nothing. --force re-clones latest main, re-resolves deps, and rebuilds the
  # web UI (built at install time by setup.py, no longer committed).
  # -q keeps uv quiet — in particular it suppresses uv's "Installed N
  # executables: omni, omnigent" summary. The package ships two console-script
  # entry points, but the single command we point users at is `omnigent`;
  # `omni` is a short alias, not something to advertise at install time.
  run_with_spinner "uv tool install" uv tool install --force -q --python "$PYTHON_VERSION" "$INSTALL_URL"
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

  # `omni` is installed alongside `omnigent` as a console-script entry point
  # (see [project.scripts] in pyproject.toml), so `uv tool install` materializes
  # both. We don't advertise it, but do a silent check so a packaging regression
  # that drops it surfaces here as a warning rather than as a puzzled user
  # report later.
  for alias_cmd in omni; do
    if [ ! -x "$bin_dir/$alias_cmd" ] && ! command -v "$alias_cmd" >/dev/null 2>&1; then
      warn "the $alias_cmd alias was not installed (expected a console-script entry point alongside omnigent)."
    fi
  done
}

# Note: there is deliberately no `omnigent setup` step here. On OSS, a bare
# `omnigent` (first run) configures a model credential and offers to install
# the harness CLI you pick — there is no Databricks profile flow to run.
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
  install_omnigent
  bin_dir="$(uv_tool_bin_dir)"
  verify_omnigent "$bin_dir"
  maybe_add_bin_to_path "$bin_dir"
  print_next_steps "$bin_dir"
}

main "$@"
