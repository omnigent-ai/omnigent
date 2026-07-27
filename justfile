default:
    @just --list

export FASTLANE_SKIP_UPDATE_CHECK := "1"

# iOS device override (default: iPhone 17 Pro)
DEVICE := env("OMNIGENT_IOS_SIMULATOR", "iPhone 17 Pro")

# --- uv Python env ---

_check-uv:
    uv run --no-sync ruff --version
    uv run --no-sync pre-commit --version

# Sync from the committed lockfile without rewriting it.
#
# Root cause (read this before "fixing" the flag back to --locked):
# Many Databricks developers have
#   index-url = "https://pypi-proxy.cloud.databricks.com/simple"
# in ~/.config/uv/uv.toml. Any uv sync/lock that re-resolves then rewrites
# every registry URL in uv.lock to that proxy. `--locked` / `uv lock --check`
# then treat a clean pypi.org lock as stale — even when pyproject.toml is
# unchanged — which is why CI (UV_INDEX_URL=pypi.org, no user config) passes
# while local `--locked` fails. Forcing UV_INDEX_URL=pypi.org locally fixes
# the check but breaks machines where pypi.org DNS is unreachable (proxy-only
# networks still need the proxy for build-system.requires fetches).
#
# So we use `--frozen` (install pins, never touch the lock) plus an explicit
# pyproject.toml-vs-uv.lock mtime staleness gate below (skip with
# OMNIGENT_SKIP_LOCK_STALENESS=1 when git left misleading mtimes). CI remains
# the `--locked` freshness gate. Pinning `index-url` in the repo's uv.toml
# would make local and CI agree on resolution, but is deferred: it breaks
# proxy-only installs until those networks can reach pypi.org or a
# transparent mirror — see the PR for the owner decision.
#
# `--inexact` keeps optional harness extras (cursor / copilot / antigravity)
# that `omnigent setup` may have installed; `--extra all` only adds
# databricks-sdk, not those.
_ensure-uv:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! -f uv.lock ]]; then
        echo "error: uv.lock is missing. Run: just relock && just normalize-locks" >&2
        exit 1
    fi
    # Fail if the manifest is newer than the lock — `--frozen` would otherwise
    # silently install a stale environment after a pyproject.toml edit.
    # Git does not guarantee relative mtimes across checkout/stash/rebase, so
    # a false positive can block ensure/lint; skip with the env var below.
    if [[ "${OMNIGENT_SKIP_LOCK_STALENESS:-}" != "1" ]] && [[ pyproject.toml -nt uv.lock ]]; then
        echo "error: pyproject.toml is newer than uv.lock." >&2
        echo "  This check asserts the lock is at least as new as the manifest" >&2
        echo "  so \`uv sync --frozen\` does not silently install a stale env." >&2
        echo "  Options:" >&2
        echo "    1. Re-resolve:  just relock && just normalize-locks" >&2
        echo "    2. Skip gate:   OMNIGENT_SKIP_LOCK_STALENESS=1 just ensure" >&2
        echo "       (use when git left misleading mtimes but the lock is valid)" >&2
        echo "    3. Then re-run: just ensure" >&2
        exit 1
    fi
    set +e
    uv sync --frozen --inexact --extra all --extra dev
    status=$?
    set -e
    if [[ "${status}" -eq 0 ]]; then
        exit 0
    fi
    echo "" >&2
    echo "error: \`uv sync --frozen\` failed (exit ${status})." >&2
    echo "  Recovery:" >&2
    echo "    • Lock rewritten by an older \`just ensure\` (proxy URLs)?" >&2
    echo "        git checkout -- uv.lock && just normalize-locks" >&2
    echo "    • pyproject.toml changed and the lock needs re-resolving?" >&2
    echo "        just relock && just normalize-locks" >&2
    echo "  Then re-run: just ensure" >&2
    exit "${status}"

# Intentional re-resolve (updates uv.lock). Day-to-day setup uses
# `_ensure-uv` / `just ensure` with `--frozen` instead.
[group('setup')]
relock:
    uv sync --inexact --extra all --extra dev
    @echo "uv.lock may point at your local index; run \`just normalize-locks\` before committing."

# Run a command via the project venv without re-resolving (which would
# rewrite uv.lock under a corporate proxy). If the venv is missing or
# empty, fail with a pointer to ensure instead of a raw spawn /
# ModuleNotFoundError.
_uv-run-no-sync +args:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ ! -x .venv/bin/python ]]; then
        echo "error: project .venv is missing or incomplete." >&2
        echo "  Fix: just ensure   # syncs --extra all --extra dev from uv.lock" >&2
        exit 1
    fi
    set +e
    uv run --no-sync {{ args }}
    status=$?
    set -e
    if [[ "${status}" -eq 0 ]]; then
        exit 0
    fi
    # Empty venv from `uv run --no-sync` after a deleted .venv: spawn fails.
    if [[ ! -x .venv/bin/pre-commit ]] && [[ " {{ args }} " == *" pre-commit "* ]]; then
        echo "" >&2
        echo "error: pre-commit is not installed in .venv (dev extra missing?)." >&2
        echo "  Fix: just ensure" >&2
        exit 1
    fi
    exit "${status}"

# --- iOS Ruby dependencies ---

_check-ios:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ "$(uname -s)" != "Darwin" ]]; then
        echo "Skipping iOS check (not macOS)."
        exit 0
    fi
    if ! command -v bundle >/dev/null 2>&1; then
        echo "Skipping iOS check (Bundler not found)."
        exit 0
    fi
    cd web/ios && bundle check

_ensure-ios:
    #!/usr/bin/env bash
    set -euo pipefail
    if [[ "$(uname -s)" != "Darwin" ]]; then
        echo "Skipping iOS setup (not macOS)."
        exit 0
    fi
    if ! command -v bundle >/dev/null 2>&1; then
        echo "Skipping iOS setup (Bundler not found)."
        exit 0
    fi
    cd web/ios && (bundle check || bundle install)

# --- omnidev Rust dev tool ---

_install-omnidev:
    cargo install --path dev/omnidev --locked --force

_check-omnidev:
    command -v omnidev >/dev/null 2>&1

_ensure-omnidev:
    command -v omnidev >/dev/null 2>&1 || just _install-omnidev

# --- Aggregate setup checks / installs ---

[group('setup')]
check: _check-uv _check-ios _check-omnidev

[group('setup')]
ensure: _ensure-uv _ensure-ios _ensure-omnidev

# --- Local dev ---

[group('dev')]
dev: _ensure-omnidev
    omnidev

[group('dev')]
dev-mobile: _ensure-omnidev
    omnidev --vite-host 0.0.0.0 --trust-lan-origins

# --- Mobile builds ---

[group('mobile')]
run-ios: _ensure-ios
    cd web/ios && bundle exec fastlane simulator device:"{{ DEVICE }}"

[group('mobile')]
run-android:
    cd web/android && ./gradlew installDebug runDebug

[group('mobile')]
android-reverse:
    cd web/android && ./gradlew reverseProxy

# --- Electron desktop app ---

_ensure-web:
    cd web && test -d node_modules || npm install --no-audit --no-fund

_ensure-electron:
    cd web/electron && test -d node_modules || npm install --no-audit --no-fund

[group('electron')]
electron-dev: _ensure-web _ensure-electron
    npm --prefix web/electron run dev

[group('electron')]
electron-build: _ensure-web _ensure-electron
    npm --prefix web/electron run build

# --- Lint ---

[group('lint')]
lint: _ensure-uv
    just _uv-run-no-sync pre-commit run

[group('lint')]
lint-all: _ensure-uv
    just _uv-run-no-sync pre-commit run --all-files

# --- Lockfile maintenance ---

# Fixers exit 1 when they rewrite (pre-commit convention). Treat that as
# success here; real errors use exit code 2+ from the scripts.
# Always `--no-sync`: a bare `uv run` would re-resolve against the local
# index and rewrite uv.lock (undoing `--frozen` in `_ensure-uv`).
[group('lint')]
normalize-locks: _ensure-uv
    #!/usr/bin/env bash
    set -euo pipefail
    run_fixer() {
        local ec=0
        local out
        out="$(uv run --no-sync "$@" 2>&1)" || ec=$?
        printf '%s\n' "${out}"
        if [[ "${ec}" -eq 0 || "${ec}" -eq 1 ]]; then
            return 0
        fi
        if [[ "${out}" == *"No module named"* ]] \
            || [[ "${out}" == *"Failed to spawn"* ]] \
            || [[ "${out}" == *"No such file or directory"* ]]; then
            echo "error: project .venv is missing tools needed for normalize-locks." >&2
            echo "  Fix: just ensure" >&2
            return 1
        fi
        return "${ec}"
    }
    run_fixer scripts/normalize_package_lock_registry.py \
        web/package-lock.json web/electron/package-lock.json editors/vscode/package-lock.json
    run_fixer scripts/normalize_uv_lock_registry.py uv.lock
