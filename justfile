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
electron-run *flags:
    #!/usr/bin/env bash
    set -euo pipefail
    # Validate flags and get reset confirmation BEFORE any side effect (dep
    # install, build, quitting the app), so declining leaves the machine
    # untouched.
    [ "$(uname)" = "Darwin" ] || { echo "electron-run is macOS-only (managed-prefs testing); build with 'just electron-build' and open the app for your OS."; exit 1; }
    # just delivers variadic args via {{flags}} substitution; put them into
    # positional params so the loop treats each as one datum. (Local dev recipe:
    # the caller already has shell access, so this isn't a trust boundary.)
    set -- {{flags}}
    rebuild=0; v2=0; reset_state=0
    for f in "$@"; do
        case "$f" in
            --rebuild) rebuild=1 ;;
            --v2-flow) v2=1 ;;
            --reset-state) reset_state=1 ;;
            *) echo "unknown flag: $f (supported: --rebuild, --v2-flow, --reset-state)"; exit 2 ;;
        esac
    done
    if [ "$reset_state" = 1 ]; then
        echo "--reset-state will UNINSTALL the omnigent CLI and remove the desktop"
        echo "app's data (session cookies, recent servers) for a fresh-user test."
        read -r -p 'Type "yes" to proceed: ' reply
        [ "$reply" = "yes" ] || { echo "Aborted."; exit 1; }
    fi
    # Pick the bundle for THIS machine's architecture (electron-builder emits both
    # mac-arm64 and mac-x64/mac); a bare glob could launch the wrong one on Intel.
    case "$(uname -m)" in
        arm64) archdir="mac-arm64" ;;
        *) archdir="mac" ;;  # electron-builder names the x64 output "mac"
    esac
    find_app() { ls -d "web/electron/dist/$archdir/Omnigent.app" 2>/dev/null | head -1 || true; }
    app="$(find_app)"
    if [ "$rebuild" = 1 ] || [ -z "$app" ]; then
        echo "Building the packaged app (this takes a few minutes)…"
        just _ensure-web _ensure-electron
        pnpm --filter ./web/electron run build
        app="$(find_app)"
    fi
    [ -n "$app" ] || { echo "No $archdir build found after building."; exit 1; }
    echo "Quitting any running Omnigent…"
    osascript -e 'quit app "Omnigent"' 2>/dev/null || true
    pkill -x Omnigent 2>/dev/null || true
    sleep 1
    if [ "$reset_state" = 1 ]; then
        # Uninstall the CLI. The shared uninstaller exits non-zero both when no
        # install exists (fine) and on a genuine failure (e.g. removal blocked) —
        # and its exit codes don't distinguish those. So verify the OUTCOME
        # instead: after running it, if the binary is still resolvable the
        # uninstall really failed — abort BEFORE deleting data or launching, so a
        # half-reset can't masquerade as a clean fresh-user state.
        sh scripts/uninstall_oss.sh cli --yes || true
        hash -r 2>/dev/null || true
        if command -v omnigent >/dev/null 2>&1; then
            echo "CLI uninstall did not remove 'omnigent' (still on PATH). Aborting" >&2
            echo "before touching app data. Remove it manually, then retry." >&2
            exit 1
        fi
        # Wipe the desktop app data directly so a fresh-user reset works even with
        # no CLI installed (the uninstaller's global guard skips desktop-data in
        # that case). Intentional destroy — see the confirmation above.
        rm -rf "$HOME/Library/Application Support/Omnigent" \
               "$HOME/Library/Caches/Omnigent" \
               "$HOME/Library/Logs/Omnigent"
    fi
    if [ "$v2" = 1 ]; then
        echo "Launching $app (v2 flow forced)"
        open -n "$app" --env OMNIGENT_SERVER_SELECTOR_V2=1
    else
        echo "Launching $app"
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
