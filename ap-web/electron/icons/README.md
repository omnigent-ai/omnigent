# App icons

- `AppIcon.icon` — source of truth for the macOS icon: an Apple Icon
  Composer bundle (layered artwork + gradient background).
- `Assets.car` + `icon.icns` — compiled from `AppIcon.icon` by `actool`
  (checked in so builds don't require Xcode 26+). `Assets.car` gives the
  native dynamic icon on macOS 26+ (liquid glass, light/dark/tinted);
  `icon.icns` is the static fallback used by older macOS and as
  electron-builder's `mac.icon`. The `build/afterPack.js` hook copies
  `Assets.car` into the app bundle, and `CFBundleIconName=AppIcon`
  (in `mac.extendInfo`) tells macOS to look for it. electron-builder
  has no native `.icon` support yet — see
  https://github.com/electron-userland/electron-builder/issues/9254.
- `icon.ico` / `icon.png` — Windows / Linux icons.

## Regenerating after editing AppIcon.icon

Requires Xcode 26+ (Icon Composer `.icon` support in actool):

```bash
cd ap-web/electron/icons
TMP=$(mktemp -d)
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcrun actool AppIcon.icon \
  --compile "$TMP" --platform macosx --minimum-deployment-target 11.0 \
  --app-icon AppIcon --output-partial-info-plist "$TMP/partial.plist"
cp "$TMP/Assets.car" Assets.car
cp "$TMP/AppIcon.icns" icon.icns
rm -rf "$TMP"
```
