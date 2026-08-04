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
    uv sync --extra all --extra dev

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
    cd web && test -d node_modules || pnpm install

_ensure-electron:
    cd web/electron && test -d node_modules || pnpm install

[group('electron')]
electron-dev: _ensure-web _ensure-electron
    pnpm --filter web/electron run dev

[group('electron')]
electron-build: _ensure-web _ensure-electron
    pnpm --filter web/electron run build

# --- Lint ---

[group('lint')]
lint: _ensure-uv
    uv run pre-commit run

[group('lint')]
lint-all: _ensure-uv
    uv run pre-commit run --all-files

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
    uv run scripts/normalize_uv_lock_registry.py uv.lock || true
