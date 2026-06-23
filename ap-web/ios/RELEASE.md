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
   cd ap-web/ios
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
   `.env` is git-ignored and is loaded automatically by fastlane.

## Cutting a TestFlight build

```sh
cd ap-web/ios
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

## App Store submission (later)

```sh
bundle exec fastlane release
```

Uploads the binary without submitting for review. App Store metadata and
screenshots are not yet wired up — add them under `fastlane/metadata` and enable
submission in the `release` lane when ready.

## Other commands

- `bundle exec fastlane tests` — run the `OmnigentTests` unit suite.
- `bundle exec fastlane lanes` — list available lanes.
