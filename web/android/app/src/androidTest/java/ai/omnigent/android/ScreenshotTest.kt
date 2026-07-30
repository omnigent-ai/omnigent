package ai.omnigent.android

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.filters.LargeTest
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import androidx.test.uiautomator.Until
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File

/**
 * Instrumented screenshot test for the WebView shell.
 *
 * Captures real SPA rendering inside [MainActivity]'s WebView (and the native
 * [ConnectActivity] server-selection screen) on a device/emulator against a
 * *live* Vite dev server. This is the only way to screenshot the product UI the
 * shell hosts.
 *
 * Driven by the `recordScreenshots` Gradle task, which runs this test once per
 * screen (passing `-e screen <name>`) and pulls each PNG into
 * app/build/screenshots/. Screens:
 *
 *  - `server_select` — [ConnectActivity] (native server-entry screen). Captured
 *    before tapping Connect, so no WebView is involved.
 *  - `home` — the SPA landing page at `/` (new-chat composer + sidebar).
 *  - `session_list` — the SPA home; the session list *is* the sidebar
 *    (`AppShell`), always visible on every route. Same route as `home` (there's
 *    no dedicated `/sessions` page); a populated list needs the omnigent
 *    backend (proxied at :6767) to serve `/v1/sessions`.
 *  - `session` — a chat/session page at `/c/:conversationId`. Pass a real id via
 *    `-e sessionId=conv_...` to capture one with content; without it (or without
 *    the backend) the session page chrome renders empty/loading.
 *
 * Prerequisites (enforced/handled by the `recordScreenshots` Gradle task):
 *  1. A Vite dev server reachable at http://localhost:5173 (the task starts one
 *     automatically if none is running, and runs `adb reverse tcp:5173`).
 *  2. The debug network-security config permits cleartext to `localhost`
 *     (app/src/debug/res/xml/network_security_config.xml).
 *  3. App data is `pm clear`-ed before each screen so launch routes to
 *    ConnectActivity, and POST_NOTIFICATIONS is pre-granted (suppresses the
 *    runtime dialog that would cover the WebView).
 *
 * This is a pure UI Automator (out-of-process, black-box) test: it launches the
 * app from the launcher, types the server URL into the real ConnectActivity
 * field, and taps Connect. ServerStore is written in the *app* process (the
 * production code path MainActivity reads) and the real MainActivity is brought
 * to the foreground as the user sees it. (An instrumented test runs in its own
 * process `ai.omnigent.android.test`; writing SharedPreferences directly, or
 * launching via ActivityScenario, would stay in the test process and MainActivity
 * would never foreground.)
 *
 * Once MainActivity is up, the test waits for the floating server-switcher pill
 * (added after the WebView is built and loadUrl is called) as the "shell is up"
 * signal, plus a render buffer for the web content, then captures via
 * [UiDevice.takeScreenshot].
 */
@LargeTest
@RunWith(AndroidJUnit4::class)
class ScreenshotTest {
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private val device get() = UiDevice.getInstance(instrumentation)
    private val targetPackage get() = instrumentation.targetContext.packageName

    /** Which screen to capture, passed via `am instrument -e screen <name>`. */
    private val screen: String
        get() = InstrumentationRegistry.getArguments().getString("screen") ?: "home"

    /** Optional real session id for the `session` screen (-e sessionId=conv_...). */
    private val sessionId: String?
        get() =
            InstrumentationRegistry
                .getArguments()
                .getString(
                    "sessionId",
                )?.takeIf { it.isNotBlank() }

    /** Base server URL the device reaches via `adb reverse`. */
    private val serverBaseUrl: String
        get() =
            InstrumentationRegistry
                .getArguments()
                .getString(
                    "serverUrl",
                )?.takeIf { it.isNotBlank() }
                ?: "http://localhost:5173"

    @Test
    fun capture() {
        device.wakeUp()
        if (device.isScreenOn) device.pressMenu() // dismiss any keyguard guard

        // Pre-grant POST_NOTIFICATIONS (API 33+) so MainActivity's runtime
        // request doesn't surface a dialog over the WebView. App data is already
        // pm-cleared by the Gradle task; we can't pm clear here (shared uid).
        try {
            instrumentation.uiAutomation.grantRuntimePermission(
                targetPackage,
                android.Manifest.permission.POST_NOTIFICATIONS,
            )
        } catch (e: Exception) {
            // pre-33 or already granted
        }

        when (screen) {
            "server_select" -> captureConnectActivity()
            "session" -> captureWebView(routePath = "/c/${sessionId ?: SCREENSHOT_DEMO_SESSION_ID}")
            "session_list" -> captureSessionList()
            else -> captureWebView(routePath = "/") // home
        }
    }

    /** Captures the native ConnectActivity server-selection screen (pre-connect). */
    private fun captureConnectActivity() {
        launchApp()
        waitConnectField()
        // Don't tap Connect — capture the server-entry screen as-is.
        Thread.sleep(RENDER_BUFFER_MS)
        capturePng("server_select.png")
    }

    /**
     * Drives ConnectActivity: types the server URL (base + [routePath]), taps
     * Connect, waits for the shell (switch pill), then captures the rendered
     * SPA at that route. The SPA fully supports deep-linking (server-side SPA
     * fallback returns index.html, react-router resolves the path), so loading
     * base + `/c/:id` renders the session page directly.
     */
    private fun captureWebView(routePath: String) {
        val serverUrl = serverBaseUrl.trimEnd('/') + routePath

        launchApp()
        waitConnectField()
        device.findObject(By.text(FIELD_HINT))!!.text = serverUrl
        device.findObject(By.text("Connect"))!!.click()

        // If the POST_NOTIFICATIONS dialog still appeared (grant failed),
        // dismiss it by tapping a system "Allow"-prefixed button.
        device.wait(Until.hasObject(By.textStartsWith("Allow")), 4_000)?.let {
            if (it) device.findObject(By.textStartsWith("Allow"))?.click()
        }

        // The floating switch pill's label is hostLabelOf(serverUrl). With a
        // route path appended, hostLabelOf still yields "localhost:5173" (it
        // parses host:port, ignoring the path) — so the same label works for
        // every screen. Its presence means the WebView shell is up.
        val switchLabel = "localhost:5173"
        assertTrue(
            "Server-switcher pill \"$switchLabel\" never appeared — " +
                "is the Vite dev server up at $serverBaseUrl and is " +
                "`adb reverse tcp:5173 tcp:5173` in place?",
            device.wait(Until.hasObject(By.text(switchLabel)), SHELL_UP_TIMEOUT_MS),
        )

        // Render buffer for the network load + first paint of the SPA route.
        Thread.sleep(RENDER_BUFFER_MS)
        capturePng("$screen.png")
    }

    /**
     * The session list is the sidebar (Conversations drawer), which on a
     * phone-width WebView is *closed* by default. Rather than coordinate-tapping
     * the top-left toggle (fragile, and uiautomator dump can't see inside the
     * WebView to find it), we deep-link to `/?sidebar=open` — AppShell reads
     * that query param and opens the sidebar drawer on mount, then strips it.
     * This reliably renders the conversation-list drawer on any device width.
     */
    private fun captureSessionList() {
        captureWebView(routePath = "/?sidebar=open")
    }

    private fun launchApp() {
        val ctx = instrumentation.context
        val intent =
            ctx.packageManager.getLaunchIntentForPackage(targetPackage)
                ?: error("No launch intent for $targetPackage")
        intent.addFlags(
            android.content.Intent.FLAG_ACTIVITY_CLEAR_TASK or
                android.content.Intent.FLAG_ACTIVITY_NEW_TASK,
        )
        ctx.startActivity(intent)
    }

    private fun waitConnectField() {
        assertTrue(
            "ConnectActivity server-url field never appeared",
            device.wait(Until.hasObject(By.text(FIELD_HINT)), LAUNCH_TIMEOUT_MS),
        )
    }

    private fun capturePng(name: String) {
        val outDir = File(instrumentation.targetContext.getExternalFilesDir(null), "screenshots")
        outDir.mkdirs()
        val outFile = File(outDir, name)
        val ok = device.takeScreenshot(outFile)
        assertTrue("takeScreenshot returned false", ok)
        assertTrue("screenshot not written to ${outFile.absolutePath}", outFile.length() > 0)
        // The Gradle task pulls this via `adb pull` before uninstalling the app
        // (we run via `am instrument`, not AGP's connectedDebugAndroidTest, which
        // auto-uninstalls and would delete this dir first).
    }

    private companion object {
        // ConnectActivity's URL-field hint (R.string.connect_hint) — unique, so
        // we match by text rather than resource-id to avoid app-vs-test
        // application-id ambiguity in By.res(pkg, id).
        const val FIELD_HINT = "https://omnigent.example.com"

        // Placeholder session id for the `session` screen when no real id is
        // supplied. The SPA renders the session-page chrome and attempts to
        // load it; without the backend it shows a loading/empty state. Pass
        // `-e sessionId=conv_...` against a live backend for real content.
        const val SCREENSHOT_DEMO_SESSION_ID = "screenshot-demo"

        const val LAUNCH_TIMEOUT_MS = 20_000L
        const val SHELL_UP_TIMEOUT_MS = 20_000L

        // Render buffer for the network load + first paint. Tuned conservatively
        // for a local dev server; bump if captures come back blank/loading.
        const val RENDER_BUFFER_MS = 6_000L
    }
}
