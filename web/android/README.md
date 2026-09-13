# Omnigent Android

Thin Kotlin/`WebView` shell for Omnigent. Like the Electron app and the iOS
shell (`web/ios`), this target loads the server-served web UI instead of
shipping a duplicate copy of the SPA. It is a native _shell_, not a rewrite.

## Development

Open `web/android` in Android Studio Meerkat (AGP 9.1+) and run the `app`
configuration on an API 36 emulator. Requires JDK 17 and the Android SDK
(`compileSdk 36`, `targetSdk 36`, `minSdk 28`).

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

## Databricks workspaces

A Databricks workspace serves its own landing page at the root and mounts the
Omnigent SPA at `/omnigent`, so the shell rewrites a **bare** workspace root to
that mount (`Origins.databricksWorkspaceUiUrl`):

- `https://dbc-a5d4177a-49dc.cloud.databricks.com` →
  `https://dbc-a5d4177a-49dc.cloud.databricks.com/omnigent`
- `?o=<org>` and any fragment are preserved; a URL that already carries a path
  (a deep link, or `/omnigent` itself) is left alone.

The rewrite happens when the pinned server URL is read
(`ServerStore.currentServerUrl`), and in all three `OmnigentWebViewClient`
callbacks that can observe the WebView reaching the root, because no single one
sees every case:

- `shouldOverrideUrlLoading` — link/redirect navigations. Not called for loads
  the shell starts itself, nor for POST-driven ones.
- `onPageStarted` — every committed main-frame load, including the login chain's
  POST hand-back.
- `doUpdateVisitedHistory` — in-page routing (`pushState`/`replaceState`,
  back/forward), which loads nothing and so fires neither of the above.

Bounces are budgeted at one per app-page load (`MAX_ROOT_BOUNCES`): if a
workspace answers `/omnigent` with a redirect back to the root, the user stays
on the root instead of looping, and a successful app page load re-arms the
budget. They're also posted to the main looper — a `loadUrl` issued while
WebView is committing a navigation can be dropped.

Host matching is by domain (`*.databricks.com`, `*.azuredatabricks.net`) — no
probe request. `*.databricksapps.com` is excluded: Apps serve their own app at
the root and have no workspace mount. All three native shells redirect a bare
workspace root to `/omnigent`.

## Managed configuration (org-preset servers)

Organizations can preconfigure server URLs so users don't type one. The app
publishes an [Android managed
configuration](https://developer.android.com/work/managed-configurations)
(`app/src/main/res/xml/app_restrictions.xml`) with a single key, which any EMM
(Intune, Jamf, Workspace ONE, Google Workspace, Android Management API) can push
to enrolled devices:

| Key          | Type   | Value                                                           |
| ------------ | ------ | --------------------------------------------------------------- |
| `serverUrls` | string | Server URLs, comma- or newline-separated, most preferred first. |

```json
{ "serverUrls": "https://omnigent.corp.example.com" }
```

Behaviour (`ManagedConfig` + `ServerStore`):

- The URLs are **offered**, listed ahead of the user's recent servers on the
  connect screen and in the server switcher. The user still taps one to connect —
  this is true for a single URL as much as for several.
- The app never auto-connects to a preset and never skips the connect screen, so
  a policy can't silently move someone onto a different server.
- Presets are not a lock either: a user can still type any other server, and the
  one they picked stays current.
- A preset is never written to the app's prefs, so an admin's later edit is
  picked up the next time the list is shown.
- Unparseable entries are dropped; an entry without a scheme gets `https://`;
  same-origin duplicates collapse; the list is capped at 8.

To test without an EMM, use Google's **Test DPC** on an emulator with no
accounts (a wiped AVD):

```bash
adb install -r TestDPC_<ver>.apk      # github.com/googlesamples/android-testdpc releases
adb shell dpm set-device-owner com.afwsamples.testdpc/.DeviceAdminReceiver
```

Then Test DPC → _Managed configurations_ → pick Omnigent → **Load manifest
restrictions** (this renders our schema, confirming the manifest wiring) → set
`serverUrls` → **Save**. Verify the policy actually landed with:

```bash
adb shell dumpsys device_policy | grep serverUrls
```

A physical device that already has a corporate work profile can't be used for
this: Test DPC can't take over an existing managed profile, and device-owner
mode requires a device with no accounts.

iOS has no equivalent yet; when it lands it should reuse the `serverUrls` key
verbatim via Managed App Configuration.

### Known parity gaps

- **App badge count.** Android has no universal numeric badge API. We set
  `NotificationCompat.setNumber()` (shown by some launchers; AOSP/Pixel shows
  only a dot) and treat the notification dot as the guaranteed surface.
  `setBadgeCount(0)` is a no-op — we do not cancel notifications to clear a
  badge.

## Distribution

Gradle assembles a release APK/AAB. Google Play restricts "WebView of a
website" apps, so the initial channel is direct APK / F-Droid; a
user-configured server client is a stronger Play case but review is
unpredictable for this category.

### Release signing

`bundleRelease` signs the artifact when signing credentials are available;
without them the release build is left unsigned so debug builds still work.
Credentials come from either a gitignored `keystore.properties` (copy
`keystore.properties.example`) or, for CI, these environment variables:

- `OMNIGENT_KEYSTORE_FILE` — path to the upload keystore
- `OMNIGENT_KEYSTORE_PASSWORD`
- `OMNIGENT_KEY_ALIAS`
- `OMNIGENT_KEY_PASSWORD`

Create the upload keystore once and back it up (Play App Signing then manages
the app signing key):

```sh
keytool -genkeypair -v -keystore omnigent-upload.jks \
  -keyalg RSA -keysize 2048 -validity 10000 -alias omnigent-upload
```

Build the Play-ready App Bundle (Play requires an `.aab`, not an APK):

```sh
./gradlew bundleRelease   # → app/build/outputs/bundle/release/app-release.aab
```

### Versioning

`versionCode` and `versionName` default to the values in
`app/build.gradle.kts`, and either can be overridden at build time — no source
edit needed for a one-off build:

```sh
./gradlew bundleRelease -PversionCode=10 -PversionName=0.2.0
```

Bump `versionCode` for every Play upload (Play rejects a reused code). The
`Android Bundle` workflow takes both as `workflow_dispatch` inputs; leaving
`version-name` blank keeps the checked-in default.

### Automated publishing (Gradle Play Publisher)

After the first release is uploaded manually (Google blocks the Play API until
an app has one human upload), `./gradlew publishReleaseBundle` builds the signed
AAB and uploads it to the **internal** track. It needs a Google Play
service-account key:

1. In Google Cloud, create a service account and a JSON key.
2. In Play Console → _Users & permissions_, invite that service account and
   grant it release permissions.
3. Point `PLAY_SERVICE_ACCOUNT_JSON` at the JSON, or drop it at
   `web/android/play-credentials.json` (both gitignored).

```sh
export PLAY_SERVICE_ACCOUNT_JSON=/path/to/play-credentials.json
./gradlew publishReleaseBundle   # signs + uploads to the internal track
```

The publish tasks are inert when no credentials file is present, so ordinary
builds are unaffected. Change the target track via `track.set(...)` in
`app/build.gradle.kts` (`internal` → `alpha` → `beta` → `production`).

> Status: builds clean — `gradlew :app:assembleDebug :app:lintDebug` produces a
> debug APK with 0 lint errors (JDK 17, Gradle 9.3 wrapper, `compileSdk 36`).
> Implementation for omnigent-ai/omnigent#1604; not yet exercised on a device
> (no runtime/instrumented testing here), so treat device behavior as unverified.
