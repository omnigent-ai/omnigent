# Platform Assets

Shared native-platform assets for wrappers around the Omnigent web UI.

- `AppIcon.icon` is the Apple Icon Composer source of truth for the app icon.
  The iOS project references it directly. Electron consumes generated
  artifacts in `electron/icons/` (`Assets.car`, `icon.icns`, `icon.png`, and
  `icon.ico`) so packaging does not require Xcode 26.
- `logos/` contains the setup-screen logo SVGs. Electron loads them from
  `platform-assets` at runtime; iOS symlinks them into its asset catalog so the
  SwiftUI setup screen uses the same sources.
