# Omnigent Android

Thin Kotlin/`WebView` shell for Omnigent. Like the Electron app and the iOS
shell (`web/ios`), this target loads the server-served web UI instead of
shipping a duplicate copy of the SPA. It is a native _shell_, not a rewrite.

## Development

Open `web/android` in Android Studio (Ladybug / AGP 8.6+) and run the `app`
configuration on an API 34/35 emulator. Requires JDK 17 and the Android SDK
(`compileSdk 35`, `minSdk 28`).

Debug builds permit cleartext (`http://`) to localhost and private-range hosts
via `res/xml/network_security_config.xml` for local development; release builds
keep the platform default (HTTPS only), mirroring the iOS
`NSAllowsArbitraryLoadsInWebContent` debug-only posture.

## How it relates to the web bundle

The same `web/` bundle runs in a browser tab, the Electron shell, the iOS
WKWebView shell, and this Android WebView. Detection is feature-based at
runtime via `window.omnigentNative` — see `web/src/lib/nativeBridge.ts`. This
shell injects that object with `kind: "android"`; the web layer needs no
per-feature branching beyond the `kind` discriminator (`isAndroidShell()`).

The web→native transport is a `WebViewCompat.addWebMessageListener` channel
(`OmnigentBridgeListener`) **origin-allowlisted to the pinned server** and
gated on `isMainFrame`, rather than `addJavascriptInterface`. This is the
structural equivalent of the iOS bridge's frame-origin + `isMainFrame` check:
the transport object is never delivered to a sandboxed / cross-origin
agent-HTML iframe, so an injected artifact can't reach the native surface.

## Scope (first version)

Provides native setup chrome (server entry + recent servers via
`ConnectActivity`), `WebView` loading, foreground local notifications with tap
routing back into the SPA, a best-effort app badge, edge-to-edge inset plumbing
(measured insets injected as `--omnigent-android-safe-area-*`, consumed by the
web inset system), correct system-back / predictive-back handling, file
downloads — including `blob:` / `data:` exports via a fetch→base64→MediaStore
bridge, which closes omnigent-ai/omnigent#969 (the iOS shell drops these) —
file **uploads** (`<input type=file>` via `WebChromeClient.onShowFileChooser`),
and **microphone** capture for voice input (`onPermissionRequest`, granted to
the pinned origin only, with a runtime `RECORD_AUDIO` request).

### Deliberately deferred to the web in-page fallbacks

These are iOS-only native chrome; the SPA already renders its own equivalents
when the bridge methods are absent, so the Android shell omits them for now:

- **Interactive sidebar edge-swipe drawer.** Not portable: on Android 10+ the
  system back gesture owns both screen edges, and
  `View.setSystemGestureExclusionRects()` does not apply to it. The sidebar
  opens from the in-page hamburger, exactly as in a browser tab.
- **Native floating server switcher** and **Chat/Terminal bar.** Rendered
  in-page by the SPA.

### Known parity gaps

- **App badge count.** Android has no universal numeric badge API. We set
  `NotificationCompat.setNumber()` (shown by some launchers; AOSP/Pixel shows
  only a dot) and treat the notification dot as the guaranteed surface.
  `setBadgeCount(0)` is a no-op — we do not cancel notifications to clear a
  badge.

## Distribution

Gradle assembles a release APK/AAB; `fastlane` (Android) automates signing and
upload. Google Play restricts "WebView of a website" apps, so the initial
channel is direct APK / F-Droid; a user-configured server client is a stronger
Play case but review is unpredictable for this category.

> Status: builds clean — `gradlew :app:assembleDebug :app:lintDebug` produces a
> debug APK with 0 lint errors (JDK 17, Gradle 8.9 wrapper, `compileSdk 35`).
> Implementation for omnigent-ai/omnigent#1604; not yet exercised on a device
> (no runtime/instrumented testing here), so treat device behavior as unverified.
