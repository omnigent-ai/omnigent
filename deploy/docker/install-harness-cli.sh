#!/usr/bin/env bash
# install-harness-cli.sh — install optional harness CLIs into an Omnigent host
# image, selected by harness NAME.
#
# The host image's default CLI set (claude / codex / pi via npm, plus pinned
# kiro-cli and agy) has no shared inclusion policy for additions yet, so this
# script is the opt-in extension point: a downstream deployment names the extra
# harnesses it wants baked in — via the EXTRA_HARNESS_CLIS build-arg — instead
# of forking the Dockerfile:
#
#   docker build -t omnigent-host:latest --target host \
#                -f deploy/docker/Dockerfile \
#                --build-arg EXTRA_HARNESS_CLIS="goose jcode opencode" .
#
# Each entry is NAME[@VERSION]; VERSION pins the CLI the way the harness's own
# installer expects (an npm dist-tag/semver, GOOSE_VERSION, JCODE_VERSION).
# An entry of the form npm:<pkg-spec> installs an arbitrary npm package
# directly — the escape hatch for a CLI this script has no row for yet (npm:
# entries skip the post-install binary smoke check, since the binary name
# isn't known here).
#
# Why names instead of raw npm specs: the row knows HOW the harness ships.
# Several harness CLIs are not npm packages at all (goose and jcode ship
# single-binary vendor installers), and for the npm ones the row carries the
# right package and default pin (e.g. opencode → opencode-ai@~1.18.0, mirroring
# omnigent/onboarding/harness_install.py — keep the two in sync). An unknown
# name fails the build loudly instead of npm-installing an unrelated package
# that happens to share the name.
#
# Binaries land on a system PATH dir (/usr/local/bin) that every sandbox user
# shares — including the non-root `sandbox` user the OpenShell provider runs
# as — and each row is smoke-checked with `<binary> --version` under a fresh
# HOME, so a CLI that only works from the build user's home fails the build
# here rather than the first managed-sandbox session.
#
# Supported rows:
#   claude    → npm @anthropic-ai/claude-code   (already in the default set;
#   codex     → npm @openai/codex                name it only to pin a version,
#   pi        → npm @earendil-works/pi-coding-agent  e.g. claude@2.1.161)
#   opencode  → npm opencode-ai (default pin ~1.18.0, as harness_install.py)
#   qwen      → npm @qwen-code/qwen-code
#   goose     → vendor installer (aaif-goose/goose download_cli.sh)
#   jcode     → vendor installer (1jehuang/jcode install.sh) — jcode runs as a
#               user-configured ACP agent (acp.agents in config.yaml), not a
#               builtin harness, but the managed host still needs the binary
#
# kiro-cli and agy ship in the host image by default (version-pinned inline in
# the Dockerfiles) and are deliberately not rows here.
#
# Also runnable by hand on any Linux host with bash, curl, and npm (for the
# npm rows) — e.g. to test a row before rebuilding an image:
#   bash deploy/docker/install-harness-cli.sh goose jcode@0.75.5

set -euo pipefail

# System PATH dir shared by every sandbox user. Overridable for testing.
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
# Shared, world-readable home for jcode's builds tree (see install_jcode).
JCODE_HOME="${JCODE_HOME:-/opt/jcode}"

die() { echo "ERROR: $*" >&2; exit 1; }

# Smoke-check: the binary resolves on PATH and runs under a fresh HOME, the
# way a managed-sandbox session (possibly a non-root user) would invoke it.
verify() {
    local binary="$1" out scratch
    command -v "$binary" >/dev/null 2>&1 \
        || die "$binary was installed but is not on PATH"
    scratch="$(mktemp -d)"
    out="$(HOME="$scratch" "$binary" --version 2>&1 | tail -n1)" \
        || { rm -rf "$scratch"; die "$binary --version failed after install"; }
    rm -rf "$scratch"
    echo ">> $binary OK: ${out:-<no version output>}"
}

install_npm() { # <pkg-spec> <binary>
    local spec="$1" binary="$2"
    echo ">> installing $binary from npm package $spec"
    npm install -g --no-audit --no-fund "$spec"
    npm cache clean --force
    verify "$binary"
}

install_goose() { # <version|"">
    local version="$1"
    # goose's Linux release archive is .tar.bz2 — tar needs bzip2 to extract it.
    command -v bzip2 >/dev/null 2>&1 \
        || die "goose needs bzip2 to extract its release archive (the host Dockerfiles install it)"
    local -a install_env=(
        # Install straight onto the shared PATH and skip the interactive
        # `goose configure` prompt — auth stays user-owned at runtime.
        "GOOSE_BIN_DIR=$BIN_DIR"
        "CONFIGURE=false"
    )
    [ -z "$version" ] || install_env+=("GOOSE_VERSION=$version")
    echo ">> installing goose ${version:-<latest stable>} via aaif-goose/goose installer"
    curl -fsSL https://github.com/aaif-goose/goose/releases/download/stable/download_cli.sh \
        | env "${install_env[@]}" bash
    verify goose
}

install_jcode() { # <version|"">
    local version="$1"
    # jcode's launcher (JCODE_INSTALL_DIR/jcode) is a symlink chain into
    # $HOME/.jcode/builds/... — installing as root with the default HOME would
    # strand the real binary under /root (0700), unreachable to the non-root
    # sandbox user. Redirect HOME so the builds tree lands in a shared
    # location, then relax perms for every sandbox user.
    local -a install_env=(
        "HOME=$JCODE_HOME"
        "JCODE_INSTALL_DIR=$BIN_DIR"
        "JCODE_NO_TELEMETRY=1"
        "JCODE_SKIP_SERVER_RELOAD=1"
    )
    if [ -n "$version" ]; then
        # The installer requires a v-prefixed release tag.
        [[ "$version" == v* ]] || version="v$version"
        install_env+=("JCODE_VERSION=$version")
    fi
    echo ">> installing jcode ${version:-<latest>} via 1jehuang/jcode installer"
    mkdir -p "$JCODE_HOME"
    curl -fsSL https://raw.githubusercontent.com/1jehuang/jcode/master/scripts/install.sh \
        | env "${install_env[@]}" bash
    chmod -R a+rX "$JCODE_HOME"
    verify jcode
}

[ $# -gt 0 ] || die "usage: install-harness-cli.sh NAME[@VERSION]... | npm:<pkg-spec>..."

for spec in "$@"; do
    case "$spec" in
        npm:*)
            pkg="${spec#npm:}"
            [ -n "$pkg" ] || die "empty npm: package spec"
            echo ">> installing npm package $pkg (no row — skipping binary smoke check)"
            npm install -g --no-audit --no-fund "$pkg"
            npm cache clean --force
            continue
            ;;
        *@*) name="${spec%%@*}"; version="${spec#*@}" ;;
        *)   name="$spec"; version="" ;;
    esac
    case "$name" in
        claude)   install_npm "@anthropic-ai/claude-code${version:+@$version}" claude ;;
        codex)    install_npm "@openai/codex${version:+@$version}" codex ;;
        pi)       install_npm "@earendil-works/pi-coding-agent${version:+@$version}" pi ;;
        opencode) install_npm "opencode-ai@${version:-~1.18.0}" opencode ;;
        qwen)     install_npm "@qwen-code/qwen-code${version:+@$version}" qwen ;;
        goose)    install_goose "$version" ;;
        jcode)    install_jcode "$version" ;;
        kiro | kiro-cli | agy | antigravity)
            die "$name ships in the host image by default (version-pinned) — no EXTRA_HARNESS_CLIS entry needed" ;;
        *)
            die "unknown harness CLI '$name' — supported names: claude, codex, pi, opencode, qwen, goose, jcode (or npm:<pkg-spec> for a package with no row)" ;;
    esac
done
