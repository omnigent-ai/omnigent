default:
    @just --list

export FASTLANE_SKIP_UPDATE_CHECK := "1"

# iOS device override (default: iPhone 17 Pro)
DEVICE := env("OMNIGENT_IOS_SIMULATOR", "iPhone 17 Pro")

# --- uv Python env ---

_check-uv:
    uv run --no-sync ruff --version
    uv run --no-sync pyrefly --version
    uv run --no-sync pre-commit --version

_ensure-uv:
    uv sync --extra all --group dev

# --- iOS Ruby dependencies ---

_check-ios:
    cd web/ios && bundle check

_ensure-ios:
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

[group('dev')]
crdb-up:
    docker compose -f deploy/cockroachdb/docker-compose.yml up -d --wait --wait-timeout 90
    docker compose -f deploy/cockroachdb/docker-compose.yml exec -T crdb-23-2-28 cockroach sql --insecure --execute="SET CLUSTER SETTING sql.txn.read_committed_isolation.enabled = true"

[group('dev')]
crdb-stop:
    docker compose -f deploy/cockroachdb/docker-compose.yml stop

[group('dev')]
crdb-test: crdb-up
    ./scripts/test_crdb_matrix.sh

# Destructive: stops CRDB and deletes all four persistent development volumes.
[group('dev')]
crdb-reset:
    docker compose -f deploy/cockroachdb/docker-compose.yml down --volumes

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

# --- Web ---

_ensure-web:
    cd web && test -d node_modules || pnpm install

[group('web')]
storybook: _ensure-web
    pnpm --filter web run storybook

[group('web')]
storybook-build: _ensure-web
    pnpm --filter web run build:storybook

[group('web')]
generate-theme-palettes: _ensure-web
    cd web && node --experimental-strip-types scripts/generate-theme-palettes.mjs

# --- Electron desktop app ---

_ensure-electron:
    cd web/electron && test -d node_modules || pnpm install

[group('electron')]
electron-dev: _ensure-web _ensure-electron
    pnpm --filter ./web/electron run dev

[group('electron')]
electron-build: _ensure-web _ensure-electron
    pnpm --filter ./web/electron run build

# Build (if needed) and launch the packaged app (reads MDM managed prefs).
# Flags: --rebuild (force a fresh build even if one exists),
#        --v2-flow (force the new server-selector wizard on),
#        --reset-state (first uninstall the CLI + wipe app data for a fresh
#                       user; destructive, asks for confirmation).
[group('electron')]
electron-run *flags: _ensure-web _ensure-electron
    #!/usr/bin/env bash
    set -euo pipefail
    [ "$(uname)" = "Darwin" ] || { echo "electron-run is macOS-only (managed-prefs testing); build with 'just electron-build' and open the app for your OS."; exit 1; }
    rebuild=0; v2=0; reset_state=0
    for f in {{flags}}; do
        case "$f" in
            --rebuild) rebuild=1 ;;
            --v2-flow) v2=1 ;;
            --reset-state) reset_state=1 ;;
            *) echo "unknown flag: $f (supported: --rebuild, --v2-flow, --reset-state)"; exit 2 ;;
        esac
    done
    app="$(ls -d web/electron/dist/mac*/Omnigent.app 2>/dev/null | head -1 || true)"
    if [ "$rebuild" = 1 ] || [ -z "$app" ]; then
        echo "Building the packaged app (this takes a few minutes)…"
        pnpm --filter ./web/electron run build
        app="$(ls -d web/electron/dist/mac*/Omnigent.app 2>/dev/null | head -1)"
    fi
    echo "Quitting any running Omnigent…"
    osascript -e 'quit app "Omnigent"' 2>/dev/null || true
    pkill -x Omnigent 2>/dev/null || true
    sleep 1
    if [ "$reset_state" = 1 ]; then
        echo "--reset-state will UNINSTALL the omnigent CLI and remove the desktop"
        echo "app's data (session cookies, recent servers) for a fresh-user test."
        read -r -p 'Type "yes" to proceed: ' reply
        [ "$reply" = "yes" ] || { echo "Aborted."; exit 1; }
        sh scripts/uninstall_oss.sh cli desktop-data --yes
    fi
    echo "Launching $app${v2:+ (v2 flow forced)}"
    if [ "$v2" = 1 ]; then
        open -n "$app" --env OMNIGENT_SERVER_SELECTOR_V2=1
    else
        open -n "$app"
    fi

# --- Lint ---

[group('lint')]
lint: _ensure-uv
    uv run --no-sync pre-commit run

[group('lint')]
lint-all: _ensure-uv
    uv run --no-sync pre-commit run --all-files

[group('lint')]
typecheck-python: _ensure-uv
    uv run --no-sync pyrefly check

[group('lint')]
lint-ts:
    pnpm install --frozen-lockfile --filter web --filter omnigent-vscode
    pnpm --filter web run lint
    pnpm --filter web run type-check
    pnpm --filter omnigent-vscode run type-check

# --- Lockfile maintenance ---

[group('lint')]
normalize-locks: _ensure-uv
    uv run --no-sync scripts/normalize_uv_lock_registry.py uv.lock || true
