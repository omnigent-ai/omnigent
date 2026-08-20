# Releasing Omnigent iOS

Releases are built locally with [fastlane](https://fastlane.tools). The `beta`
lane archives a signed Release build and uploads it to TestFlight; the `release`
lane uploads to App Store Connect (binary only — review submission is a
follow-up).

## One-time setup

1. **Xcode 16+** with the command-line tools selected
   (`xcode-select -p` should point at your Xcode).
2. **Install fastlane** (pinned via `Gemfile`):
   ```sh
   cd web/ios
   bundle install
   ```
3. **Create the app record** in [App Store Connect](https://appstoreconnect.apple.com)
   for bundle ID `ai.omnigent.ios` (My Apps → +), if it doesn't exist yet.
4. **Generate an App Store Connect API key**: Users and Access → Integrations →
   App Store Connect API → generate a key with the **App Manager** role.
   Download the `.p8` (you can only download it once) and place it in
   `ios/fastlane/` — it is git-ignored.
5. **Configure env vars**:
   ```sh
   cp fastlane/.env.example fastlane/.env
   # edit fastlane/.env: set ASC_KEY_ID, ASC_ISSUER_ID, ASC_KEY_PATH
   ```
   `.env` is git-ignored and is loaded automatically by fastlane. The lanes
   also accept `APPLE_API_KEY_ID`, `APPLE_API_ISSUER`, and `APPLE_API_KEY`
   (`.p8` file path) for compatibility with local shell exports.

## Cutting a TestFlight build

```sh
cd web/ios
bundle exec fastlane beta
```

This bumps the build number to one past the latest on TestFlight, archives the
Release configuration (HTTPS-only, automatic signing under team `8RMX4WU6F8`),
and uploads the `.ipa`. The build appears in App Store Connect → TestFlight after
Apple finishes processing.

## Versioning

- **Build number** (`CFBundleVersion = $(CURRENT_PROJECT_VERSION)`) is computed
  per upload as `latest_testflight_build_number + 1` and injected at archive time
  via an xcodebuild `CURRENT_PROJECT_VERSION=…` override. Nothing in the repo is
  modified, so every `beta`/`release` upload gets a unique, monotonic build
  number with no version churn in git. Don't bump it by hand.
- **Marketing version** (`CFBundleShortVersionString`, currently `0.1.0`) is set
  manually. Bump `MARKETING_VERSION` for both the Debug and Release
  configurations of the **Omnigent** target in Xcode (or via `fastlane
increment_version_number`) when shipping a new user-facing version.
- **App icon** is the shared Icon Composer source at
  `platform-assets/AppIcon.icon`, which the iOS target includes directly for
  Liquid Glass app icon rendering.

## App Store submission (later)

```sh
bundle exec fastlane prod
```

Prepares the App Store version without submitting for review. The lane reuses
the latest uploaded TestFlight build and uploads `fastlane/metadata` plus
`fastlane/screenshots`. Set
`OMNIGENT_PROD_BUILD_NUMBER=4` to pin a specific processed build.

## Other commands

- `bundle exec fastlane tests` — run the `OmnigentTests` unit suite.
- `bundle exec fastlane screenshots` — build and capture App Store screenshots
  into `fastlane/screenshots` using the `OmnigentUITests` snapshot target. The
  lane rebuilds the web UI, starts an isolated local Omnigent server via `uv` on
  a non-6767 port, and connects the simulator to that server automatically.
- `bundle exec fastlane release` — upload a new binary to App Store Connect
  without submitting it for review. Prefer `prod` after a TestFlight build is
  already uploaded.
- `bundle exec fastlane lanes` — list available lanes.
