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
# installer expects (an npm dist-tag/semver, GOOSE_VERSION, JCODE_VERSION, ...).
# An entry of the form npm:<pkg-spec> installs an
# arbitrary npm package directly — the escape hatch for a CLI this script has
# no row for yet (npm: entries skip the post-install binary smoke check, since
# the binary name isn't known here).
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
# shares, and each row is smoke-checked twice: once under a fresh HOME (a CLI
# that only works from the build user's home — e.g. state the installer left
# under /root — fails here), then re-run as the non-root `sandbox` user the
# OpenShell provider runs agents as (a binary reachable only through the build
# user's home would pass the fresh-HOME check yet break the first
# managed-sandbox session; images without a `sandbox` user, like UBI, skip it).
#
# Supported rows — the CLI-gated harnesses NOT in the default image set (the
# canonical list is _HARNESS_INSTALL in omnigent/onboarding/harness_install.py):
#   opencode  → npm opencode-ai (default pin ~1.18.0, as harness_install.py)
#   qwen      → npm @qwen-code/qwen-code
#   goose     → vendor installer (aaif-goose/goose download_cli.sh), default
#               pin 1.38.0 (mirroring _GOOSE_MIN_VERSION)
#   jcode     → vendor installer (1jehuang/jcode install.sh) — jcode runs as a
#               user-configured ACP agent (acp.agents in config.yaml), not a
#               builtin harness; a deployment that runs jcode that way still
#               needs the binary on the managed host's PATH, since the sandbox
#               cannot run the installer at session start
#   cursor    → vendor installer (cursor.com/install) — always fetches the
#               latest agent build, so VERSION pins are rejected
#   kimi      → vendor installer (code.kimi.com/kimi-code/install.sh)
#   agy       → pinned per-arch GitHub release asset + sha256 (AGY_VERSION +
#               the two AGY_SHA256_* values) — the same control as the default
#               image, exposed as a row because UBI does not bake agy by default
#
# hermes is deliberately NOT a row: it requires Node >= 26 at runtime (its
# installer self-installs a managed Node into the installing user's
# HERMES_HOME) while the host images ship Node 22 — it needs the image's
# Node baseline raised first, not just a baked binary.
#
# Supply-chain note: only agy here (and kiro-cli baked in the Dockerfile) is
# pinned to an immutable asset + sha256. The vendor-installer rows (goose,
# jcode, cursor, kimi) run the harness's own `curl | bash` off mutable refs and
# are checked only with `--version`; cursor cannot be pinned at all.
# Off-by-default bounds this, but a deployment needing kiro-cli-grade integrity
# should pin + verify in its own image rather than rely on a name here.
#
# Default-set CLIs are deliberately not rows: claude/codex/pi install unpinned
# from npm in the Dockerfiles (override via the npm: escape hatch, e.g.
# npm:@openai/codex@0.147.0), and kiro-cli is version-pinned there via the
# KIRO_CLI_VERSION build ARG. agy IS a row (see install_agy) because UBI does
# not install it by default.
#
# Also runnable by hand on any Linux host with bash, curl, and npm (for the
# npm rows) — e.g. to test a row before rebuilding an image:
#   bash deploy/docker/install-harness-cli.sh goose jcode@0.75.5

set -euo pipefail

# System PATH dir shared by every sandbox user. Overridable for testing.
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
# Shared, world-readable homes for CLIs whose installers are HOME-bound
# (see install_jcode / install_cursor).
JCODE_HOME="${JCODE_HOME:-/opt/jcode}"
CURSOR_HOME="${CURSOR_HOME:-/opt/cursor}"

die() { echo "ERROR: $*" >&2; exit 1; }

# Smoke-check: the binary resolves on PATH and runs under a fresh HOME, then
# again as the non-root sandbox user when the image has one — the two ways a
# managed-sandbox session (possibly a different user) would invoke it.
verify() {
    local binary="$1" out scratch
    command -v "$binary" >/dev/null 2>&1 \
        || die "$binary was installed but is not on PATH"
    scratch="$(mktemp -d)"
    out="$(HOME="$scratch" "$binary" --version 2>&1 | tail -n1)" \
        || { rm -rf "$scratch"; die "$binary --version failed after install"; }
    rm -rf "$scratch"
    echo ">> $binary OK as root: ${out:-<no version output>}"
    # Re-run as the non-root `sandbox` user OpenShell runs agents as, when the
    # image has one: a binary reachable only through the build user's home
    # would pass the root check above yet fail the first managed-sandbox
    # session. UBI has no sandbox user, so guard on `id sandbox`.
    if [ "$(id -u)" = 0 ] && id sandbox >/dev/null 2>&1 && command -v setpriv >/dev/null 2>&1; then
        out="$(HOME=/sandbox setpriv --reuid=sandbox --regid=sandbox --clear-groups "$binary" --version 2>&1 | tail -n1)" \
            || die "$binary --version failed as the non-root sandbox user"
        echo ">> $binary OK as sandbox: ${out:-<no version output>}"
    fi
}

install_npm() { # <pkg-spec> <binary>
    local spec="$1" binary="$2"
    echo ">> installing $binary from npm package $spec"
    npm install -g --no-audit --no-fund "$spec"
    npm cache clean --force
    verify "$binary"
}

install_goose() { # <version|"">
    # Default pin mirrors _GOOSE_MIN_VERSION in harness_install.py (the native
    # forwarder is verified against it); goose already accepts GOOSE_VERSION,
    # so pinning it rather than tracking `stable` costs nothing.
    local version="${1:-1.38.0}"
    # goose's Linux release archive is .tar.bz2 — tar needs bzip2 to extract it.
    command -v bzip2 >/dev/null 2>&1 \
        || die "goose needs bzip2 to extract its release archive (the host Dockerfiles install it)"
    local -a install_env=(
        # Install straight onto the shared PATH and skip the interactive
        # `goose configure` prompt — auth stays user-owned at runtime.
        "GOOSE_BIN_DIR=$BIN_DIR"
        "CONFIGURE=false"
        "GOOSE_VERSION=$version"
    )
    echo ">> installing goose $version via aaif-goose/goose installer"
    curl -fsSL https://github.com/aaif-goose/goose/releases/download/stable/download_cli.sh \
        | env "${install_env[@]}" bash
    verify goose
}

install_agy() {
    # The one row with real integrity pinning, mirroring the Dockerfile's baked
    # agy: immutable per-arch GitHub release asset + sha256. The version and
    # both sha256s come from the Dockerfile build ARGs (exported as ENV), so
    # this row and the baked default stay on the same value.
    [ -n "${AGY_VERSION:-}" ] \
        || die "agy needs AGY_VERSION — set it via the Dockerfile build ARG (see deploy/docker/Dockerfile)"
    local asset sha
    case "$(uname -m)" in
        x86_64)
            asset="agy_cli_linux_x64.tar.gz"
            [ -n "${AGY_SHA256_AMD64:-}" ] || die "agy needs AGY_SHA256_AMD64"
            sha="$AGY_SHA256_AMD64"
            ;;
        aarch64)
            asset="agy_cli_linux_arm64.tar.gz"
            [ -n "${AGY_SHA256_ARM64:-}" ] || die "agy needs AGY_SHA256_ARM64"
            sha="$AGY_SHA256_ARM64"
            ;;
        *) die "unsupported arch '$(uname -m)' for agy" ;;
    esac
    echo ">> installing agy $AGY_VERSION (sha256 verified)"
    curl -fsSL -o /tmp/agy.tar.gz \
        "https://github.com/google-antigravity/antigravity-cli/releases/download/${AGY_VERSION}/${asset}"
    echo "${sha} /tmp/agy.tar.gz" | sha256sum -c - \
        || die "agy sha256 mismatch for ${asset}"
    tar -xzf /tmp/agy.tar.gz -C /tmp antigravity
    install -m 0755 /tmp/antigravity "$BIN_DIR/agy"
    rm -f /tmp/agy.tar.gz /tmp/antigravity
    verify agy
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

install_cursor() {
    # cursor's installer hardcodes $HOME/.local/{share,bin} with no directory
    # override, so it gets the jcode treatment: redirect HOME to a shared
    # location, relax perms, then link onto the shared PATH. The installer
    # also creates an `agent` symlink — too generic for a shared PATH, so
    # only cursor-agent is linked.
    mkdir -p "$CURSOR_HOME"
    echo ">> installing cursor-agent <latest> via cursor.com installer"
    curl -fsSL https://cursor.com/install | HOME="$CURSOR_HOME" bash
    chmod -R a+rX "$CURSOR_HOME"
    ln -sf "$CURSOR_HOME/.local/bin/cursor-agent" "$BIN_DIR/cursor-agent"
    verify cursor-agent
}

install_kimi() { # <version|"">
    local version="$1"
    # kimi installs a single static binary at ${KIMI_INSTALL_DIR}/bin/kimi —
    # point it at BIN_DIR's parent so it lands on the shared PATH.
    [[ "$BIN_DIR" == */bin ]] \
        || die "kimi needs BIN_DIR to end in /bin (got $BIN_DIR)"
    local -a install_env=(
        "KIMI_INSTALL_DIR=${BIN_DIR%/bin}"
        "KIMI_NO_MODIFY_PATH=1"
    )
    [ -z "$version" ] || install_env+=("KIMI_VERSION=$version")
    echo ">> installing kimi ${version:-<latest>} via code.kimi.com installer"
    curl -fsSL https://code.kimi.com/kimi-code/install.sh \
        | env "${install_env[@]}" bash
    verify kimi
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
        opencode) install_npm "opencode-ai@${version:-~1.18.0}" opencode ;;
        qwen)     install_npm "@qwen-code/qwen-code${version:+@$version}" qwen ;;
        goose)    install_goose "$version" ;;
        agy | antigravity)
            [ -z "$version" ] \
                || die "agy is pinned via the AGY_VERSION build ARG (sha256-verified), not an @version suffix"
            install_agy
            ;;
        jcode)    install_jcode "$version" ;;
        cursor)
            [ -z "$version" ] \
                || die "cursor's installer always fetches the latest build — cursor@VERSION pins are not supported"
            install_cursor
            ;;
        kimi)     install_kimi "$version" ;;
        hermes)
            die "hermes needs Node >= 26 at runtime and the host image ships Node 22 — its installer's managed Node lands in the build user's home. Raise the image's Node baseline first; no EXTRA_HARNESS_CLIS row until then" ;;
        claude)   die "claude ships in the host image by default (unpinned npm install) — pin a different version via the npm: escape hatch: npm:@anthropic-ai/claude-code@<version>" ;;
        codex)    die "codex ships in the host image by default (unpinned npm install) — pin a different version via the npm: escape hatch: npm:@openai/codex@<version>" ;;
        pi)       die "pi ships in the host image by default (unpinned npm install) — pin a different version via the npm: escape hatch: npm:@earendil-works/pi-coding-agent@<version>" ;;
        kiro | kiro-cli)
            die "$name ships in the host image by default, version-pinned via the KIRO_CLI_VERSION build ARG — override with --build-arg instead" ;;
        *)
            die "unknown harness CLI '$name' — supported names: opencode, qwen, goose, agy, jcode, cursor, kimi (or npm:<pkg-spec> for a package with no row)" ;;
    esac
done
