# Android `omnigent://` Deep-Link Handling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register the Android shell as an `omnigent://<host>[:port]/c/<id>` handler with consent-gated server switching, per `docs/android-deep-link-design.md` — preceded by an OIDC login/origin-binding fix that lands as its own PR.

**Architecture:** Part A hardens `OidcLoginManager` so login results are bound to their originating origin and server switches cancel in-flight logins (pre-existing race, its own PR). Part B adds a manifest `VIEW` intent filter, a pure `DeepLink.kt` parser sharing `originOf` canonicalization, and FIFO-queued routing in `MainActivity` (same-origin navigate / known-server switch / unknown-server consent with load-success-gated persistence).

**Tech Stack:** Kotlin, Android WebView shell (`web/android`), Gradle 9.3 wrapper (JDK 17), JUnit 4 + Robolectric (`@Config(sdk = [35])`).

## Global Constraints

- All commands run from `web/android/` unless noted; unit tests: `./gradlew :app:testDebugUnitTest --tests "<class>"`; lint gate: `./gradlew :app:lintDebug` (must stay at 0 errors).
- Every commit is signed off: `git commit -s` (DCO), and ends with `Co-authored-by: Isaac`.
- Run `pre-commit run --all-files` before each commit (or let the git hook run on staged files).
- Comments: ≤3 lines, describe the scenario not the change history, no PR/issue numbers.
- Part A lands on branch `android-oidc-origin-binding` (cut from `main`) as its own PR. Part B continues on the existing `android-deep-link` branch (which holds the spec commits) and its PR notes the dependency on Part A.
- Parser contract (spec §"Link contract"): scheme inferred `http` for `localhost`/`127.0.0.1`/`::1`, `https` otherwise; links with userinfo/query/fragment REJECTED; max link length 2048; path exactly `/c/<id>` (optional trailing slash); id denylist `?` `#` `/` `.` `%` + control chars (U+0000–U+001F, U+007F).
- Consent copy (spec §6, mirrors iOS): title "Open conversation?", body "This link will connect Omnigent to %1$s and open a conversation.", buttons "Open" / system Cancel.

---

## Part A — OIDC login/origin binding (precursor PR)

### Task A1: Origin-bound, cancellable login flows in `OidcLoginManager`

**Files:**
- Modify: `app/src/main/java/ai/omnigent/android/OidcLoginManager.kt`
- Create: `app/src/test/java/ai/omnigent/android/OidcLoginManagerTest.kt`

**Interfaces:**
- Produces: `OidcLoginManager.start(activity: Activity, origin: String, onSession: (origin: String, token: String) -> Unit): Boolean` — callback now receives the origin the flow was started for. New `fun cancel()` — abandons the in-flight flow (safe to call with none) and frees the manager for an immediate new `start()`. `shutdown()` delegates to `cancel()`.
- Consumed by: Task A2 (`MainActivity` passes a two-arg callback and calls `cancel()` from `reloadWithNewServer`).

**Design:** the single shared `io` executor is replaced by one executor **per flow**. `cancel()` shuts down the current flow's executor (`shutdownNow()` interrupts the polling sleep) and clears its callback, so a stale poll can neither block the next login for up to 5 minutes (the old `inFlight` flag held by a dead poll) nor deliver a token. The origin is captured per-flow and passed through the callback.

- [ ] **Step 1: Write the failing test**

Create `app/src/test/java/ai/omnigent/android/OidcLoginManagerTest.kt`:

```kotlin
package ai.omnigent.android

import android.app.Activity
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class OidcLoginManagerTest {
    // Port 1 is never listening — requestTicket fails fast, and the flow ends
    // without a token. The tests below only exercise start/cancel bookkeeping,
    // never a real login.
    private val deadOrigin = "http://127.0.0.1:1"

    private fun activity(): Activity = Robolectric.buildActivity(Activity::class.java).setup().get()

    @Test
    fun `second start while a flow is in flight is refused`() {
        val manager = OidcLoginManager()
        assertTrue(manager.start(activity(), deadOrigin) { _, _ -> })
        assertFalse(manager.start(activity(), deadOrigin) { _, _ -> })
        manager.shutdown()
    }

    @Test
    fun `cancel frees the manager for an immediate new start`() {
        val manager = OidcLoginManager()
        assertTrue(manager.start(activity(), deadOrigin) { _, _ -> })
        manager.cancel()
        assertTrue(manager.start(activity(), deadOrigin) { _, _ -> })
        manager.shutdown()
    }

    @Test
    fun `callback receives the origin the flow was started for`() {
        // Compile-time contract check: the two-arg callback shape. The dead
        // origin never yields a token, so the body must not run.
        val manager = OidcLoginManager()
        manager.start(activity(), deadOrigin) { origin, _ ->
            throw AssertionError("no token expected from $origin")
        }
        manager.shutdown()
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./gradlew :app:testDebugUnitTest --tests "ai.omnigent.android.OidcLoginManagerTest"`
Expected: COMPILE FAILURE — `cancel()` doesn't exist and the callback is `(String) -> Unit`, not two-arg.

- [ ] **Step 3: Rework `OidcLoginManager`**

In `OidcLoginManager.kt`:

1. Replace the fields:

```kotlin
    private val main = Handler(Looper.getMainLooper())

    // One executor per login flow; cancel() shuts it down (interrupting the
    // polling sleep) so a stale flow can't block or outlive a server switch.
    private var flow: ExecutorService? = null

    @Volatile private var sessionCallback: ((String, String) -> Unit)? = null
```

(Remove `io` and `inFlight`; add `import java.util.concurrent.ExecutorService`.)

2. Rework `start` (signature + flow bookkeeping; body of the task otherwise unchanged):

```kotlin
    fun start(
        activity: Activity,
        origin: String,
        onSession: (origin: String, token: String) -> Unit,
    ): Boolean {
        if (flow?.isShutdown == false) return false
        val executor = Executors.newSingleThreadExecutor()
        flow = executor
        sessionCallback = onSession
        executor.execute {
            var token: String? = null
            try {
                val ticket = requestTicket(origin)
                authLog("cli-login -> ${if (ticket != null) "ticket ok" else "FAILED"}")
                if (ticket != null) {
                    main.post { launchTab(activity, origin + ticket.loginUrl) }
                    token = pollForToken(origin, ticket.id)
                    authLog(
                        "poll -> ${if (token != null) "token (len=${token.length})" else "no token"}",
                    )
                }
            } catch (_: InterruptedException) {
                // cancel()/shutdown() interrupted the poll — drop silently.
            } catch (t: Throwable) {
                authLog("login flow error: ${t.javaClass.simpleName}")
            } finally {
                executor.shutdown()
            }
            val result = token
            // A cancelled flow's executor is shut down before its callback could
            // matter; re-check so a late token is dropped, and bind the origin.
            if (result != null && !executor.isShutdown) {
                main.post { sessionCallback?.invoke(origin, result) }
            }
        }
        return true
    }
```

Note the delivery guard: `executor.shutdown()` in `finally` marks *normal* completion too, so gate delivery on cancellation only — replace the `finally` body with a completion flag instead if simpler:

```kotlin
            // (Alternative, equivalent intent:) use a per-flow cancelled flag.
```

Implementer's choice, but the observable contract is: after `cancel()`, no callback fires; a flow that completes normally still delivers `(origin, token)`.

3. Add `cancel()` and delegate `shutdown()`:

```kotlin
    /** Abandon any in-flight login: no callback will fire, and a new start()
     *  is immediately possible. Safe to call with no flow in flight. */
    fun cancel() {
        sessionCallback = null
        flow?.shutdownNow() // interrupts the polling sleep so the task exits promptly
        flow = null
    }

    /** Release the host entirely. Call from onDestroy. */
    fun shutdown() = cancel()
```

- [ ] **Step 4: Fix the one compile error in `MainActivity`**

`MainActivity.startLogin` passes `::onSessionToken`. Make it compile by updating the reference — the real origin check lands in Task A2; for now just widen the signature:

```kotlin
    private fun onSessionToken(
        loginOrigin: String,
        token: String,
    ) {
```

(leave the body untouched in this task).

- [ ] **Step 5: Run tests to verify they pass**

Run: `./gradlew :app:testDebugUnitTest --tests "ai.omnigent.android.OidcLoginManagerTest"`
Expected: PASS (3 tests). Also run the full unit suite to catch regressions: `./gradlew :app:testDebugUnitTest`.

- [ ] **Step 6: Commit**

```bash
git add app/src/main/java/ai/omnigent/android/OidcLoginManager.kt \
        app/src/main/java/ai/omnigent/android/MainActivity.kt \
        app/src/test/java/ai/omnigent/android/OidcLoginManagerTest.kt
git commit -s -m "fix(android): make OIDC login flows origin-bound and cancellable

Co-authored-by: Isaac"
```

### Task A2: Reject cross-origin tokens; cancel login on server switch

**Files:**
- Modify: `app/src/main/java/ai/omnigent/android/MainActivity.kt` (`onSessionToken`, `reloadWithNewServer`)
- Create: `app/src/test/java/ai/omnigent/android/SessionTokenBindingTest.kt`

**Interfaces:**
- Consumes: Task A1's `(origin, token)` callback and `cancel()`.
- Produces: `internal fun onSessionToken(loginOrigin: String, token: String)` (visibility widened from `private` to `internal` for the test); `reloadWithNewServer` cancels the login manager.

- [ ] **Step 1: Write the failing test**

Create `app/src/test/java/ai/omnigent/android/SessionTokenBindingTest.kt`:

```kotlin
package ai.omnigent.android

import android.webkit.CookieManager
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class SessionTokenBindingTest {
    // JWT-shaped (three base64url segments) so isJwtShaped passes.
    private val token = "aGVhZGVy.cGF5bG9hZA.c2lnbmF0dXJl"
    private val pinned = "https://example.com"

    private fun launch(): MainActivity {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(pinned)
        return Robolectric.buildActivity(MainActivity::class.java).setup().get()
    }

    @Test
    fun `token from a different origin is dropped`() {
        val activity = launch()
        activity.onSessionToken("https://other.example", token)
        val cookie = CookieManager.getInstance().getCookie(pinned)
        assertFalse(cookie?.contains("ap_session") == true)
    }

    @Test
    fun `token from the pinned origin is injected`() {
        val activity = launch()
        activity.onSessionToken(pinned, token)
        val cookie = CookieManager.getInstance().getCookie(pinned)
        assertTrue(cookie?.contains("ap_session") == true)
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./gradlew :app:testDebugUnitTest --tests "ai.omnigent.android.SessionTokenBindingTest"`
Expected: `token from a different origin is dropped` FAILS (cookie gets set today); the other may pass. If `onSessionToken` is not visible, that's the compile failure — proceed.

- [ ] **Step 3: Implement the binding + cancellation**

In `MainActivity.kt`:

1. `onSessionToken` — widen to `internal`, reject mismatched origins first:

```kotlin
    internal fun onSessionToken(
        loginOrigin: String,
        token: String,
    ) {
        // A server switch can land between starting a login and its poll
        // completing; a token minted for another origin must never be
        // injected into the current one.
        if (loginOrigin != pinnedOrigin) {
            authLog("onSessionToken: origin changed since login started — dropping token")
            return
        }
        if (isDestroyed || isFinishing || !::webView.isInitialized) return
        // ... existing body unchanged from here ...
```

2. `reloadWithNewServer` — first line becomes:

```kotlin
        loginManager.cancel() // a login for the old origin must not outlive the switch
        removeBridge()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./gradlew :app:testDebugUnitTest`
Expected: all green, including both new tests.

- [ ] **Step 5: Lint, commit, open the precursor PR**

```bash
./gradlew :app:lintDebug
git add -A app/src
git commit -s -m "fix(android): drop OIDC tokens minted for a different origin

A server switch mid-login (server-switcher menu today, deep links next)
could inject the old server's session token into the new origin's
cookie store. Bind each login result to its originating origin and
cancel any in-flight login when the pinned server changes.

Co-authored-by: Isaac"
```

Open the PR per the repo template (`.github/pull_request_template.md`): Summary (the race + fix), Test Plan (the two new test classes + full `testDebugUnitTest`), Demo `N/A` (non-visual), Type of change "Bug fix", coverage boxes as applicable. Push to the `fork` remote and PR from `btli:android-oidc-origin-binding` (no direct pushes).

---

## Part B — deep-link feature (branch `android-deep-link`)

> Rebase `android-deep-link` onto Part A's branch (or onto `main` once A merges) before starting: Task B3+ calls `loginManager.cancel()`-backed `reloadWithNewServer` and the two-arg `onSessionToken`.

### Task B1: IPv6-safe origin canonicalization in `originOf`

**Files:**
- Modify: `app/src/main/java/ai/omnigent/android/Origins.kt`
- Create: `app/src/test/java/ai/omnigent/android/OriginsTest.kt`

**Interfaces:**
- Produces: `originOf(url: String?): String?` now returns bracketed IPv6 hosts (`https://[::1]:8443`) instead of a malformed `https://::1:8443`. All existing behavior (lowercasing, default-port elision) unchanged.
- Consumed by: Task B2's parser (canonicalizes through `originOf`) and all existing callers.

- [ ] **Step 1: Write the failing test**

Create `app/src/test/java/ai/omnigent/android/OriginsTest.kt`:

```kotlin
package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class OriginsTest {
    @Test
    fun `canonicalizes case and default ports`() {
        assertEquals("https://example.com", originOf("HTTPS://Example.COM:443/some/path"))
        assertEquals("http://example.com", originOf("http://example.com:80"))
        assertEquals("https://example.com:8443", originOf("https://example.com:8443"))
    }

    @Test
    fun `rebrackets ipv6 hosts`() {
        assertEquals("http://[::1]:8000", originOf("http://[::1]:8000"))
        assertEquals("https://[2001:db8::1]", originOf("https://[2001:db8::1]"))
    }

    @Test
    fun `null for scheme-less or host-less input`() {
        assertNull(originOf("example.com"))
        assertNull(originOf(null))
        assertNull(originOf("https://"))
    }
}
```

- [ ] **Step 2: Run test to verify the IPv6 case fails**

Run: `./gradlew :app:testDebugUnitTest --tests "ai.omnigent.android.OriginsTest"`
Expected: `rebrackets ipv6 hosts` FAILS (produces `http://::1:8000` — `Uri.getHost` strips brackets). If it unexpectedly passes on this Robolectric version, keep the test as a pin and skip Step 3.

- [ ] **Step 3: Implement re-bracketing**

In `Origins.kt`, inside `originOf`, after `host` is derived:

```kotlin
    // Uri.getHost strips IPv6 brackets; restore them or the rebuilt origin
    // is unparseable ("https://::1:8000").
    val hostPart = if (":" in host) "[$host]" else host
```

and use `hostPart` in the two return expressions.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./gradlew :app:testDebugUnitTest`
Expected: PASS, including all pre-existing suites (this function gates the bridge allowlist — full-suite green is the regression check).

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/ai/omnigent/android/Origins.kt \
        app/src/test/java/ai/omnigent/android/OriginsTest.kt
git commit -s -m "fix(android): bracket IPv6 hosts in originOf

Co-authored-by: Isaac"
```

### Task B2: `DeepLink.kt` parser

**Files:**
- Create: `app/src/main/java/ai/omnigent/android/DeepLink.kt`
- Create: `app/src/test/java/ai/omnigent/android/DeepLinkTest.kt`

**Interfaces:**
- Consumes: `originOf` (Task B1).
- Produces: `data class DeepLink(val origin: String, val path: String)` with `companion object { fun parse(raw: Uri): DeepLink? }`. `origin` is `originOf`-canonical with no trailing slash (e.g. `"http://localhost:8000"`); `path` is always `"/c/<id>"`.

- [ ] **Step 1: Write the failing tests**

Create `app/src/test/java/ai/omnigent/android/DeepLinkTest.kt`:

```kotlin
package ai.omnigent.android

import android.net.Uri
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DeepLinkTest {
    // A bare 32-hex uuid — the id form the API emits today.
    private val hex = "e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9"

    private fun parse(link: String) = DeepLink.parse(Uri.parse(link))

    // --- valid links ---

    @Test
    fun `loopback hosts infer http`() {
        assertEquals(
            DeepLink("http://localhost:8000", "/c/$hex"),
            parse("omnigent://localhost:8000/c/$hex"),
        )
        assertEquals(
            DeepLink("http://127.0.0.1:8000", "/c/$hex"),
            parse("omnigent://127.0.0.1:8000/c/$hex"),
        )
        assertEquals(
            DeepLink("http://[::1]:8000", "/c/$hex"),
            parse("omnigent://[::1]:8000/c/$hex"),
        )
    }

    @Test
    fun `remote hosts infer https`() {
        assertEquals(
            DeepLink("https://my-workspace.cloud.databricks.com", "/c/$hex"),
            parse("omnigent://my-workspace.cloud.databricks.com/c/$hex"),
        )
        assertEquals(
            DeepLink("https://example.com:8443", "/c/$hex"),
            parse("omnigent://example.com:8443/c/$hex"),
        )
    }

    @Test
    fun `canonicalizes case and default ports`() {
        assertEquals(
            DeepLink("https://example.com", "/c/$hex"),
            parse("OMNIGENT://Example.COM:443/c/$hex"),
        )
    }

    @Test
    fun `tolerates one trailing slash and forwards ids as-is`() {
        assertEquals("/c/$hex", parse("omnigent://localhost:8000/c/$hex/")?.path)
        // Dashed uuids and legacy conv_ prefixes are the SPA router's business.
        assertEquals(
            "/c/e4f5a6b7-c8d9-e0f1-a2b3-c4d5e6f7a8b9",
            parse("omnigent://h.example/c/e4f5a6b7-c8d9-e0f1-a2b3-c4d5e6f7a8b9")?.path,
        )
        assertEquals("/c/conv_$hex", parse("omnigent://h.example/c/conv_$hex")?.path)
    }

    // --- rejected links ---

    @Test
    fun `rejects wrong scheme and missing host`() {
        assertNull(parse("https://example.com/c/$hex"))
        assertNull(parse("omnigent:///c/$hex"))
        assertNull(parse("omnigent:$hex")) // opaque, not hierarchical
    }

    @Test
    fun `rejects non-conversation paths`() {
        assertNull(parse("omnigent://h.example/"))
        assertNull(parse("omnigent://h.example/c/"))
        assertNull(parse("omnigent://h.example/settings"))
        assertNull(parse("omnigent://h.example/c/$hex/extra"))
    }

    @Test
    fun `rejects structure smuggled into the id`() {
        // Percent-encoded separators decode into literals in Uri.getPath.
        assertNull(parse("omnigent://h.example/c/$hex%3Fview=terminal")) // ?
        assertNull(parse("omnigent://h.example/c/$hex%23frag")) // #
        assertNull(parse("omnigent://h.example/c/..%2F..%2Fetc")) // / and .
        assertNull(parse("omnigent://h.example/c/$hex%00")) // control char
        assertNull(parse("omnigent://h.example/c/$hex%zz")) // malformed escape -> stray %
        assertNull(parse("omnigent://h.example/c/.."))
    }

    @Test
    fun `rejects real query fragment and userinfo`() {
        assertNull(parse("omnigent://h.example/c/$hex?view=terminal"))
        assertNull(parse("omnigent://h.example/c/$hex#frag"))
        assertNull(parse("omnigent://user@h.example/c/$hex"))
        assertNull(parse("omnigent://h.example/c/$hex?")) // empty query still rejected
    }

    @Test
    fun `rejects invalid ports and oversized input`() {
        assertNull(parse("omnigent://h.example:99999/c/$hex"))
        assertNull(parse("omnigent://h.example:0/c/$hex"))
        assertNull(parse("omnigent://h.example/c/" + "a".repeat(3000)))
    }

    @Test
    fun `normalizes unicode hosts to punycode`() {
        assertEquals(
            "https://xn--bcher-kva.example",
            parse("omnigent://bücher.example/c/$hex")?.origin,
        )
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./gradlew :app:testDebugUnitTest --tests "ai.omnigent.android.DeepLinkTest"`
Expected: COMPILE FAILURE — `DeepLink` doesn't exist.

- [ ] **Step 3: Implement the parser**

Create `app/src/main/java/ai/omnigent/android/DeepLink.kt`:

```kotlin
package ai.omnigent.android

import android.net.Uri
import java.net.IDN

/**
 * A parsed `omnigent://<host>[:port]/c/<id>` deep link — the Android analog of
 * the desktop shell's `parseOmnigentDeepLink` and iOS's `DeepLink.swift` (see
 * docs/android-deep-link-design.md for the shared contract). Stricter than
 * both: a link carrying userinfo, a query, or a fragment is rejected outright
 * rather than silently stripped.
 */
data class DeepLink(
    /** `originOf`-canonical http(s) origin, no trailing slash. */
    val origin: String,
    /** Basename-less SPA conversation path, always `/c/<id>`. */
    val path: String,
) {
    companion object {
        private const val MAX_LINK_LENGTH = 2048

        // Hosts that resolve to the local machine — http (local dev is plain
        // http); everything else https. Mirrors iOS/desktop `localHosts`.
        private val LOCAL_HOSTS = setOf("localhost", "127.0.0.1", "::1")

        // Denylist, not a grammar: characters that smuggle URL structure,
        // enable traversal, or signal a malformed escape. The SPA's /c/:id
        // route stays the authority on what a valid id IS.
        private val BLOCKED_ID_CHARS = setOf('?', '#', '/', '.', '%')

        /** Parse an `omnigent://` URI; null for anything off-contract. */
        fun parse(raw: Uri): DeepLink? {
            if (raw.toString().length > MAX_LINK_LENGTH) return null
            if (!raw.isHierarchical) return null
            if (raw.scheme?.lowercase() != "omnigent") return null
            // Stricter than iOS/desktop (which drop these silently): our own
            // link emitters never produce them, so their presence is off-contract.
            if (raw.encodedUserInfo != null) return null
            if (raw.encodedQuery != null || raw.encodedFragment != null) return null

            val rawHost = raw.host?.takeIf { it.isNotEmpty() } ?: return null
            val port = raw.port
            if (port != -1 && port !in 1..65535) return null

            // Uri.getPath is percent-DECODED, so encoded separators (%3F, %23,
            // %2F, %2E, %00) reappear as literals and hit the denylist below;
            // a malformed escape (%zz) leaves a stray literal '%'.
            val path = raw.path ?: return null
            if (!path.startsWith("/c/")) return null
            var id = path.substring(3)
            if (id.endsWith("/")) id = id.dropLast(1)
            if (id.isEmpty()) return null
            if (id.any { it.code <= 0x1F || it.code == 0x7F || it in BLOCKED_ID_CHARS }) {
                return null
            }

            val host = canonicalHost(rawHost) ?: return null
            val scheme = if (host in LOCAL_HOSTS) "http" else "https"
            val hostPart = if (":" in host) "[$host]" else host
            val rebuilt = if (port != -1) "$scheme://$hostPart:$port" else "$scheme://$hostPart"
            val origin = originOf(rebuilt) ?: return null
            return DeepLink(origin, "/c/$id")
        }

        /** IDNA-normalize to a lowercase ASCII host, or null if impossible. */
        private fun canonicalHost(host: String): String? =
            try {
                IDN.toASCII(host.lowercase()).takeIf { it.isNotEmpty() }
            } catch (_: IllegalArgumentException) {
                null
            }
    }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./gradlew :app:testDebugUnitTest --tests "ai.omnigent.android.DeepLinkTest"`
Expected: PASS. If `Uri` behavior differs from an assumption (e.g. `getPort` on a malformed port, IPv6 host bracketing), fix the parser — the tests define the contract, not the sketch above.

- [ ] **Step 5: Commit**

```bash
git add app/src/main/java/ai/omnigent/android/DeepLink.kt \
        app/src/test/java/ai/omnigent/android/DeepLinkTest.kt
git commit -s -m "feat(android): add omnigent:// deep-link parser

Co-authored-by: Isaac"
```

### Task B3: Manifest registration + FIFO intake + same-origin / known-server routing

**Files:**
- Modify: `app/src/main/AndroidManifest.xml`
- Modify: `app/src/main/java/ai/omnigent/android/MainActivity.kt`
- Create: `app/src/test/java/ai/omnigent/android/DeepLinkRoutingTest.kt`

**Interfaces:**
- Consumes: `DeepLink.parse` (B2), `originOf` (B1).
- Produces: `MainActivity` fields `deepLinkQueue: ArrayDeque<DeepLink>`, `processingDeepLink: Boolean`, `pendingNavigateOrigin: String?`; functions `enqueueDeepLink(intent: Intent?)`, `processNextDeepLink()`, `finishDeepLink()`. Task B4 plugs `showDeepLinkConsent(link)` into the unknown-server branch (this task leaves that branch as a drop + `finishDeepLink()`).

- [ ] **Step 1: Write the failing tests**

Create `app/src/test/java/ai/omnigent/android/DeepLinkRoutingTest.kt`:

```kotlin
package ai.omnigent.android

import android.content.Intent
import android.net.Uri
import android.webkit.WebView
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DeepLinkRoutingTest {
    private val hex = "e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9"

    private fun store() = ServerStore(ApplicationProvider.getApplicationContext())

    private fun viewIntent(link: String) =
        Intent(Intent.ACTION_VIEW, Uri.parse(link)).addCategory(Intent.CATEGORY_BROWSABLE)

    private fun MainActivity.field(name: String): Any? =
        MainActivity::class.java.getDeclaredField(name).apply { isAccessible = true }.get(this)

    private fun MainActivity.webView(): WebView = field("webView") as WebView

    @Test
    fun `manifest resolves omnigent view intents to MainActivity`() {
        val pm = ApplicationProvider.getApplicationContext<android.content.Context>().packageManager
        val resolved = viewIntent("omnigent://h.example/c/$hex").resolveActivity(pm)
        assertNotNull(resolved)
        assertEquals(MainActivity::class.java.name, resolved.className)
    }

    @Test
    fun `same-origin link queues its path for the SPA`() {
        store().connect("https://h.example")
        val activity =
            Robolectric
                .buildActivity(MainActivity::class.java, viewIntent("omnigent://h.example/c/$hex"))
                .setup()
                .get()
        assertEquals("/c/$hex", activity.field("pendingNavigatePath"))
        assertEquals("https://h.example", activity.field("pendingNavigateOrigin"))
        // Same origin: no reload away from the stored server.
        assertEquals("https://h.example", originOf(shadowOf(activity.webView()).lastLoadedUrl))
    }

    @Test
    fun `known-server link switches to the stored url including its mount`() {
        store().connect("https://ws.example/ml/omnigents")
        store().connect("https://current.example")
        val activity =
            Robolectric
                .buildActivity(MainActivity::class.java, viewIntent("omnigent://ws.example/c/$hex"))
                .setup()
                .get()
        // Switched: pinned to the link's origin, loading the stored (mounted) URL.
        assertEquals("https://ws.example", activity.field("pinnedOrigin"))
        assertEquals("https://ws.example/ml/omnigents", shadowOf(activity.webView()).lastLoadedUrl)
        assertEquals("/c/$hex", activity.field("pendingNavigatePath"))
        assertEquals("https://ws.example/ml/omnigents", store().currentServerUrl())
    }

    @Test
    fun `warm same-origin link arrives via onNewIntent`() {
        store().connect("https://h.example")
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        controller.newIntent(viewIntent("omnigent://h.example/c/$hex"))
        assertEquals("/c/$hex", activity.field("pendingNavigatePath"))
    }

    @Test
    fun `rejected link is ignored`() {
        store().connect("https://h.example")
        val activity =
            Robolectric
                .buildActivity(
                    MainActivity::class.java,
                    viewIntent("omnigent://h.example/c/$hex?view=terminal"),
                ).setup()
                .get()
        assertNull(activity.field("pendingNavigatePath"))
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./gradlew :app:testDebugUnitTest --tests "ai.omnigent.android.DeepLinkRoutingTest"`
Expected: FAIL — manifest resolution returns null and the fields don't exist.

- [ ] **Step 3: Add the intent filter**

In `AndroidManifest.xml`, inside the `MainActivity` `<activity>` element, after the LAUNCHER filter:

```xml
            <!-- omnigent://<host>/c/<id> deep links; see docs/android-deep-link-design.md.
                 Custom schemes are unverified — any co-installed app can also
                 claim this one — so DeepLink.parse treats the URI as untrusted. -->
            <intent-filter>
                <action android:name="android.intent.action.VIEW" />
                <category android:name="android.intent.category.DEFAULT" />
                <category android:name="android.intent.category.BROWSABLE" />
                <data android:scheme="omnigent" />
            </intent-filter>
```

- [ ] **Step 4: Implement intake + routing in `MainActivity`**

1. New fields next to `pendingNavigatePath`:

```kotlin
    // Deep links process strictly FIFO, one at a time — a link resolves
    // (navigated, or consent answered) before the next dequeues, so a consent
    // dialog and a pending path can never belong to different links.
    private val deepLinkQueue = ArrayDeque<DeepLink>()
    private var processingDeepLink = false

    // Origin the pending path belongs to; null = the pinned origin (the
    // notification-tap path). A pending path never flushes cross-origin.
    private var pendingNavigateOrigin: String? = null
```

2. In `onCreate`, replace the no-server early-return block with:

```kotlin
        val store = ServerStore(this)
        val coldDeepLink = intent?.takeIf { it.action == Intent.ACTION_VIEW }?.data
        if (!store.hasServer() && coldDeepLink == null) {
            // No server configured yet — send the user to the connect screen first.
            startActivity(Intent(this, ConnectActivity::class.java))
            finish()
            return
        }
        val serverUrl = if (store.hasServer()) store.currentServerUrl() else null
        pinnedOrigin = serverUrl?.let(::originOf)
```

then guard the two `serverUrl` consumers at the bottom of `onCreate`:

```kotlin
        ensureNotificationPermission()
        if (serverUrl != null) webView.loadUrl(serverUrl)
        enqueueDeepLink(intent)
```

and the switcher label:

```kotlin
                text = serverUrl?.let(::hostLabelOf) ?: ""
```

(`installBridge()` already no-ops on a null `pinnedOrigin`.)

3. In `onNewIntent`, after the existing server-change check and `navigatePathOf` handling, add:

```kotlin
        enqueueDeepLink(intent)
```

4. New functions (near `flushPendingActivation`):

```kotlin
    private fun enqueueDeepLink(intent: Intent?) {
        if (intent?.action != Intent.ACTION_VIEW) return
        val uri = intent.data ?: return
        val link = DeepLink.parse(uri) ?: return
        deepLinkQueue.addLast(link)
        processNextDeepLink()
    }

    private fun processNextDeepLink() {
        if (processingDeepLink) return
        val link = deepLinkQueue.removeFirstOrNull() ?: return
        processingDeepLink = true
        val store = ServerStore(this)
        val known =
            (listOf(store.currentServerUrl()).filter { store.hasServer() } + store.recentServers())
                .firstOrNull { originOf(it) == link.origin }
        when {
            link.origin == pinnedOrigin -> {
                pendingNavigatePath = link.path
                pendingNavigateOrigin = link.origin
                if (pageLoaded) flushPendingActivation()
                finishDeepLink()
            }

            known != null -> {
                store.connect(known)
                pendingNavigatePath = link.path
                pendingNavigateOrigin = link.origin
                reloadWithNewServer(known, link.origin)
                finishDeepLink()
            }

            else -> {
                // Unknown server: consent lands in the next change; drop for now.
                finishDeepLink()
            }
        }
    }

    private fun finishDeepLink() {
        processingDeepLink = false
        processNextDeepLink()
    }
```

5. Pending-path hygiene — replace `flushPendingActivation` body:

```kotlin
    private fun flushPendingActivation() {
        // Parked off-origin (e.g. mid re-login): keep the path pending; the
        // next pinned-origin onPageReady flushes it.
        if (originOf(webView.url) != pinnedOrigin) return
        // A path bound to another origin is stale (a later switch superseded
        // it) — drop it rather than navigate the wrong server.
        if (pendingNavigateOrigin != null && pendingNavigateOrigin != pinnedOrigin) {
            pendingNavigatePath = null
            pendingNavigateOrigin = null
            return
        }
        emitNotificationActivation(pendingNavigatePath)
        pendingNavigatePath = null
        pendingNavigateOrigin = null
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./gradlew :app:testDebugUnitTest`
Expected: all green (new routing tests + all pre-existing suites).

- [ ] **Step 6: Commit**

```bash
git add app/src/main/AndroidManifest.xml \
        app/src/main/java/ai/omnigent/android/MainActivity.kt \
        app/src/test/java/ai/omnigent/android/DeepLinkRoutingTest.kt
git commit -s -m "feat(android): register omnigent:// handler and route known-server links

Co-authored-by: Isaac"
```

### Task B4: Unknown-server consent + load-success-gated persistence

**Files:**
- Modify: `app/src/main/java/ai/omnigent/android/MainActivity.kt`
- Modify: `app/src/main/res/values/strings.xml`
- Create: `app/src/test/java/ai/omnigent/android/DeepLinkConsentTest.kt`

**Interfaces:**
- Consumes: B3's queue (`processNextDeepLink` unknown branch, `finishDeepLink`), `reloadWithNewServer`, `onPageReady`.
- Produces: `showDeepLinkConsent(link: DeepLink)`; field `pendingPersistUrl: String?` persisted by `onPageReady` on the first successful pinned-origin load.

- [ ] **Step 1: Write the failing tests**

Create `app/src/test/java/ai/omnigent/android/DeepLinkConsentTest.kt`:

```kotlin
package ai.omnigent.android

import android.content.DialogInterface
import android.content.Intent
import android.net.Uri
import android.webkit.WebView
import androidx.appcompat.app.AlertDialog
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowDialog

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DeepLinkConsentTest {
    private val hex = "e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9"

    private fun store() = ServerStore(ApplicationProvider.getApplicationContext())

    private fun viewIntent(link: String) =
        Intent(Intent.ACTION_VIEW, Uri.parse(link)).addCategory(Intent.CATEGORY_BROWSABLE)

    private fun MainActivity.field(name: String): Any? =
        MainActivity::class.java.getDeclaredField(name).apply { isAccessible = true }.get(this)

    private fun MainActivity.webView(): WebView = field("webView") as WebView

    private fun latestDialog(): AlertDialog = ShadowDialog.getLatestDialog() as AlertDialog

    private fun launchWithLink(link: String): MainActivity {
        store().connect("https://current.example")
        return Robolectric
            .buildActivity(MainActivity::class.java, viewIntent(link))
            .setup()
            .get()
    }

    @Test
    fun `unknown server shows consent and does nothing until answered`() {
        val activity = launchWithLink("omnigent://new.example/c/$hex")
        assertTrue(latestDialog().isShowing)
        // Nothing loaded, pinned, or persisted pre-consent.
        assertEquals("https://current.example", activity.field("pinnedOrigin"))
        assertFalse(store().recentServers().any { it.contains("new.example") })
    }

    @Test
    fun `consent open loads the origin but persists only on page ready`() {
        val activity = launchWithLink("omnigent://new.example/c/$hex")
        latestDialog().getButton(DialogInterface.BUTTON_POSITIVE).performClick()

        assertEquals("https://new.example", activity.field("pinnedOrigin"))
        assertEquals("https://new.example", shadowOf(activity.webView()).lastLoadedUrl)
        assertEquals("/c/$hex", activity.field("pendingNavigatePath"))
        // Not yet a trusted recent: the load hasn't succeeded.
        assertFalse(store().recentServers().any { it.contains("new.example") })
        assertEquals("https://current.example", store().currentServerUrl())

        // Simulate the first successful pinned-origin load.
        invokeOnPageReady(activity, "https://new.example/")
        assertEquals("https://new.example", store().currentServerUrl())
        assertTrue(store().recentServers().contains("https://new.example"))
    }

    @Test
    fun `consent cancel drops the link`() {
        val activity = launchWithLink("omnigent://new.example/c/$hex")
        latestDialog().getButton(DialogInterface.BUTTON_NEGATIVE).performClick()

        assertEquals("https://current.example", activity.field("pinnedOrigin"))
        assertNull(activity.field("pendingNavigatePath"))
        assertFalse(store().recentServers().any { it.contains("new.example") })
    }

    @Test
    fun `cold start with no server and unknown link consents instead of redirecting`() {
        // No store().connect — nothing configured.
        val activity =
            Robolectric
                .buildActivity(MainActivity::class.java, viewIntent("omnigent://new.example/c/$hex"))
                .setup()
                .get()
        assertFalse(activity.isFinishing)
        assertNotNull(ShadowDialog.getLatestDialog())
    }

    @Test
    fun `second link waits for the first consent (FIFO)`() {
        store().connect("https://current.example")
        val controller =
            Robolectric.buildActivity(
                MainActivity::class.java,
                viewIntent("omnigent://first.example/c/$hex"),
            )
        val activity = controller.setup().get()
        val first = latestDialog()
        controller.newIntent(viewIntent("omnigent://second.example/c/$hex"))
        // Still the first dialog; the second link is queued, not racing it.
        assertEquals(first, ShadowDialog.getLatestDialog())

        first.getButton(DialogInterface.BUTTON_NEGATIVE).performClick()
        // First resolved -> second dequeues and asks.
        val second = ShadowDialog.getLatestDialog() as AlertDialog
        assertTrue(second !== first && second.isShowing)
    }

    private fun invokeOnPageReady(
        activity: MainActivity,
        url: String,
    ) {
        MainActivity::class
            .java
            .getDeclaredMethod("onPageReady", String::class.java)
            .apply { isAccessible = true }
            .invoke(activity, url)
    }
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./gradlew :app:testDebugUnitTest --tests "ai.omnigent.android.DeepLinkConsentTest"`
Expected: FAIL — no dialog is shown (B3 drops unknown links).

- [ ] **Step 3: Add the strings**

In `app/src/main/res/values/strings.xml`:

```xml
    <string name="deep_link_consent_title">Open conversation?</string>
    <string name="deep_link_consent_body">This link will connect Omnigent to %1$s and open a conversation.</string>
    <string name="deep_link_consent_open">Open</string>
```

- [ ] **Step 4: Implement consent + deferred persistence**

In `MainActivity.kt`:

1. New field next to `pendingNavigateOrigin`:

```kotlin
    // Consent-approved server URL awaiting its first successful load; only
    // then does it become a trusted recent (an unreachable or hostile link
    // target must never be remembered).
    private var pendingPersistUrl: String? = null
```

2. Replace the unknown-server branch in `processNextDeepLink`:

```kotlin
            else -> showDeepLinkConsent(link)
```

3. Add (near `showServerSwitcherMenu`; import `androidx.appcompat.app.AlertDialog`):

```kotlin
    /** Consent gate for a link to a never-connected server: pinning a new
     *  origin grants it the bridge and notifications, so it needs an explicit
     *  yes. No network request or persistence happens before Open. */
    private fun showDeepLinkConsent(link: DeepLink) {
        var answered = false
        val resolve = { accepted: Boolean ->
            if (!answered) {
                answered = true
                if (accepted) {
                    // reload first: it clears any superseded pendingPersistUrl,
                    // then this link's own pending state is installed.
                    reloadWithNewServer(link.origin, link.origin)
                    pendingNavigatePath = link.path
                    pendingNavigateOrigin = link.origin
                    pendingPersistUrl = link.origin
                } else if (!ServerStore(this).hasServer()) {
                    // Cold-start link was the only way in; fall back to setup.
                    startActivity(Intent(this, ConnectActivity::class.java))
                    finish()
                }
                finishDeepLink()
            }
        }
        AlertDialog
            .Builder(this)
            .setTitle(R.string.deep_link_consent_title)
            .setMessage(getString(R.string.deep_link_consent_body, hostLabelOf(link.origin)))
            .setPositiveButton(R.string.deep_link_consent_open) { _, _ -> resolve(true) }
            .setNegativeButton(android.R.string.cancel) { _, _ -> resolve(false) }
            .setOnCancelListener { resolve(false) } // Back key = Cancel
            .show()
    }
```

4. In `onPageReady`, after `pageLoaded = true`:

```kotlin
        // First successful load of a consent-approved server: only now does it
        // become the stored current server / a trusted recent.
        pendingPersistUrl?.takeIf { originOf(it) == pinnedOrigin }?.let {
            ServerStore(this).connect(it)
            (switchButton as? TextView)?.text = hostLabelOf(it)
        }
        pendingPersistUrl = null
```

5. At the top of `reloadWithNewServer` add (this is why the accept branch above calls `reloadWithNewServer` *before* installing its own pending state):

```kotlin
        pendingPersistUrl = null // superseded: a newer switch owns persistence now
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `./gradlew :app:testDebugUnitTest`
Expected: all green. If `onNewIntent`'s server-change check interferes with the FIFO test, remember `enqueueDeepLink` runs after it — the second link must queue because `processingDeepLink` is still true while dialog one is open.

- [ ] **Step 6: Commit**

```bash
git add app/src/main/java/ai/omnigent/android/MainActivity.kt \
        app/src/main/res/values/strings.xml \
        app/src/test/java/ai/omnigent/android/DeepLinkConsentTest.kt
git commit -s -m "feat(android): consent-gate deep links to unknown servers

Co-authored-by: Isaac"
```

### Task B5: Release loopback cleartext + README + lint/manual verification

**Files:**
- Modify: `app/src/main/res/xml/network_security_config.xml`
- Modify: `README.md` (i.e. `web/android/README.md`)

**Interfaces:**
- Consumes: nothing new; documents B2–B4.
- Produces: release builds can load `http://localhost` / `http://127.0.0.1` / `http://[::1]`; README documents the deep-link contract and the workspace-mount gap.

- [ ] **Step 1: Scope the release cleartext exemption to loopback**

Replace `app/src/main/res/xml/network_security_config.xml` content:

```xml
<?xml version="1.0" encoding="utf-8"?>
<!--
  Release default: HTTPS only, EXCEPT device-local loopback — omnigent://
  deep links infer http for loopback hosts (matching iOS, whose ATS exempts
  loopback even in release), and that traffic never leaves the device. Debug
  builds additionally permit emulator/LAN cleartext for development; that
  overlay lives in app/src/debug/res/xml/network_security_config.xml.
-->
<network-security-config>
    <base-config cleartextTrafficPermitted="false" />
    <domain-config cleartextTrafficPermitted="true">
        <domain includeSubdomains="false">localhost</domain>
        <domain includeSubdomains="false">127.0.0.1</domain>
        <domain includeSubdomains="false">::1</domain>
    </domain-config>
</network-security-config>
```

- [ ] **Step 2: Document in the Android README**

In `web/android/README.md`, add a "Deep links" section (near the notifications/auth sections):

```markdown
## Deep links

The app handles `omnigent://<host>[:port]/c/<id>` links (`DeepLink.kt`,
routed in `MainActivity`; design: `docs/android-deep-link-design.md`):
same-origin links navigate in place, previously-connected servers switch
directly, and never-connected servers require a consent dialog before any
network request — the server is remembered only after its first successful
page load. Links carrying a query, fragment, or userinfo are rejected.

Known gap: unlike iOS/desktop, there is no workspace-mount discovery
(`WorkspaceURLExpander` equivalent), so a consented **unknown** Databricks
workspace host connects to the bare origin without probing `/ml/omnigents`.
Links to already-connected workspaces work (the stored URL keeps the mount).
Follow-up: port the mount probe, and consider verified Android App Links
for operator-controlled domains.

Try it: `adb shell am start -a android.intent.action.VIEW -d "omnigent://<host>/c/<id>"`.
```

- [ ] **Step 3: Full verification pass**

```bash
./gradlew :app:testDebugUnitTest :app:lintDebug :app:assembleDebug
```

Expected: tests green, 0 lint errors, APK builds.

Manual/device checks (emulator, `just run-android` or `./gradlew :app:installDebug`) — run the spec's matrix:

```bash
# cold start, saved server = target
adb shell am start -a android.intent.action.VIEW -d "omnigent://10.0.2.2:8000/c/<id>"
# warm same-origin (app already open on that server) — expect in-place navigation
# known-server switch (connect to a second server first, then link back)
# unknown server — expect consent; verify both Open and Cancel
# link while ConnectActivity is on top; link during an in-flight browser login
```

- [ ] **Step 4: Commit and open the PR**

```bash
git add app/src/main/res/xml/network_security_config.xml README.md
git commit -s -m "feat(android): permit loopback cleartext in release and document deep links

Co-authored-by: Isaac"
```

PR from `btli:android-deep-link` per the repo template: Summary (contract + consent model, link to the design doc), Test Plan (the four new test classes + manual matrix), **Demo: screen recording of the consent flow + a known-server link** (UI change — check "UI / frontend change"), note the dependency on the Part A PR. Push to the `fork` remote.

---

## Self-review notes

- Spec coverage: precursor (A1–A2), manifest + threat-model comment (B3), parser grammar incl. strict rejects/IDNA/length/ports (B2), shared canonicalization + IPv6 (B1), FIFO + origin-paired pending path + hygiene (B3), consent + cold-start-no-server + load-success persistence (B4), Back table pinned implicitly by existing `clearHistory` behavior (no new mechanism — spec §4), loopback release policy (B5), README gap note (B5), test plan (B2–B4 + manual matrix in B5).
- The consent dialog's Back-key path is covered by `setOnCancelListener` (spec's Back table row 4).
- Robolectric shadow behavior (`ShadowDialog`, `lastLoadedUrl`, manifest resolution) is assumed from documented APIs; where a shadow disagrees, adjust the test mechanics, not the production contract.
