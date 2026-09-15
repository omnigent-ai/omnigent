package ai.omnigent.android

import android.app.NotificationManager
import android.content.Context
import android.content.RestrictionsManager
import android.content.res.Configuration
import android.os.Bundle
import android.os.Looper
import android.text.TextUtils
import android.view.View
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.RenderProcessGoneDetail
import android.webkit.WebView
import android.widget.TextView
import androidx.core.view.WindowInsetsControllerCompat
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadow.api.Shadow
import org.robolectric.shadows.ShadowRestrictionsManager
import java.time.Duration

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class MainActivityTest {
    @Test
    fun `webview leaves algorithmic darkening disabled`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        assertFalse(activity.webView().settings.isAlgorithmicDarkeningAllowed)
    }

    @Test
    @Config(qualifiers = "notnight")
    fun `light configuration uses dark status bar icons`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        assertTrue(insetsController.isAppearanceLightStatusBars)
        assertTrue(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    @Config(qualifiers = "night")
    fun `dark configuration uses light status bar icons`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        assertFalse(insetsController.isAppearanceLightStatusBars)
        assertFalse(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    fun `configuration change updates system bar icon polarity`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        val darkConfiguration =
            Configuration(activity.resources.configuration).apply {
                uiMode =
                    (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or
                    Configuration.UI_MODE_NIGHT_YES
            }
        activity.onConfigurationChanged(darkConfiguration)
        assertFalse(insetsController.isAppearanceLightStatusBars)
        assertFalse(insetsController.isAppearanceLightNavigationBars)

        val lightConfiguration =
            Configuration(activity.resources.configuration).apply {
                uiMode =
                    (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or
                    Configuration.UI_MODE_NIGHT_NO
            }
        activity.onConfigurationChanged(lightConfiguration)
        assertTrue(insetsController.isAppearanceLightStatusBars)
        assertTrue(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    fun `navigation hides the server pill until the watchdog expires`() {
        val activity = launch()
        val webView = activity.webView()
        val pill = activity.switchButton()

        webView.webViewClient.onPageStarted(webView, "https://example.com/app", null)
        assertEquals(View.GONE, pill.visibility)

        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofSeconds(7))
        assertEquals(View.VISIBLE, pill.visibility)
    }

    @Test
    fun `picker requests cancel the watchdog and hide a visible fallback`() {
        val activity = launch()
        val webView = activity.webView()

        webView.webViewClient.onPageStarted(webView, "https://example.com/app", null)
        activity.invoke("onServerPickerRequested")
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofSeconds(7))
        assertEquals(View.GONE, activity.switchButton().visibility)

        webView.webViewClient.onPageStarted(webView, "https://example.com/app", null)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofSeconds(7))
        assertEquals(View.VISIBLE, activity.switchButton().visibility)

        activity.invoke("onServerPickerRequested")

        assertEquals(View.GONE, activity.switchButton().visibility)
        val script = shadowOf(webView).lastEvaluatedJavascript.orEmpty()
        assertTrue(script.contains("__omnigentNativeEmitServerPicker"))
        assertTrue(script.contains("currentOrigin") && script.contains("example.com"))
    }

    @Test
    fun `server switches accept only picker-offered URLs`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        ServerStore(context).connect("https://second.example.test")
        val activity = launch()

        activity.invoke(
            "onSwitchServerRequested",
            arrayOf(String::class.java),
            "https://unlisted.example.test",
        )
        assertEquals("https://example.com", ServerStore(context).currentServerUrl())

        activity.invoke(
            "onSwitchServerRequested",
            arrayOf(String::class.java),
            "https://second.example.test",
        )
        assertEquals("https://second.example.test", ServerStore(context).currentServerUrl())
        assertEquals("https://second.example.test", shadowOf(activity.webView()).lastLoadedUrl)
    }

    @Test
    fun `server pill tracks the container width and truncates in the middle`() {
        val activity = launch()
        val pill = activity.switchButton() as TextView
        val container = pill.parent as ViewGroup
        val density = activity.resources.displayMetrics.density

        container.layout(0, 0, (300 * density).toInt(), (800 * density).toInt())
        assertEquals((120 * density).toInt(), pill.maxWidth)

        container.layout(0, 0, (400 * density).toInt(), (800 * density).toInt())
        assertEquals((152 * density).toInt(), pill.maxWidth)

        container.layout(0, 0, (500 * density).toInt(), (800 * density).toInt())
        assertEquals((172 * density).toInt(), pill.maxWidth)
        assertEquals(TextUtils.TruncateAt.MIDDLE, pill.ellipsize)
    }

    @Test
    fun `renderer death swaps in a fresh WebView and reloads the server`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val dead = activity.webView()
        val container = dead.parent as ViewGroup

        val handled = dead.webViewClient.onRenderProcessGone(dead, rendererGone())

        assertTrue(handled)
        val replacement = activity.webView()
        assertNotSame(dead, replacement)
        assertSame(container, replacement.parent)
        assertEquals("https://example.com", shadowOf(replacement).lastLoadedUrl)
    }

    @Test
    fun `a late renderer-death report for a replaced WebView is ignored`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val first = activity.webView()
        first.webViewClient.onRenderProcessGone(first, rendererGone())
        val replacement = activity.webView()

        // The stale report names the WebView that was already torn down.
        replacement.webViewClient.onRenderProcessGone(first, rendererGone())

        assertSame(replacement, activity.webView())
        assertFalse(shadowOf(replacement).wasDestroyCalled())
    }

    @Test
    fun `renderer death reloads the last route, not the server root`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val dead = activity.webView()
        // The user was on a deep same-origin route when the renderer died;
        // getUrl() reports the last-committed URL, which survives the death.
        dead.loadUrl("https://example.com/chat/abc123")

        dead.webViewClient.onRenderProcessGone(dead, rendererGone())

        // The rebuilt WebView returns to the route, not the landing page.
        assertEquals(
            "https://example.com/chat/abc123",
            shadowOf(activity.webView()).lastLoadedUrl,
        )
    }

    @Test
    fun `a foreign last URL falls back to the server root on recovery`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val dead = activity.webView()
        // A foreign / error origin must not be restored into — reload the server.
        dead.loadUrl("https://accounts.google.com/o/oauth2/v2/auth")

        dead.webViewClient.onRenderProcessGone(dead, rendererGone())

        assertEquals("https://example.com", shadowOf(activity.webView()).lastLoadedUrl)
    }

    @Test
    fun `a renderer crash loop stops auto-reloading but still rebuilds a live WebView`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        // MAX_RENDERER_CRASHES + 1 clustered crashes. Each still rebuilds (manual
        // recovery reuses the webView field, so the corpse can't stay); the loop
        // is broken by what loads, not by refusing to rebuild.
        var dead = activity.webView()
        repeat(4) {
            dead.loadUrl("https://example.com/chat/loops")
            val container = dead.parent as ViewGroup
            dead.webViewClient.onRenderProcessGone(dead, rendererGone(crashed = true))
            val rebuilt = activity.webView()
            // Every death — even the over-budget one — yields a fresh, attached,
            // non-destroyed WebView, so manual recovery always has a live target.
            assertNotSame(dead, rebuilt)
            assertSame(container, rebuilt.parent)
            assertFalse(shadowOf(rebuilt).wasDestroyCalled())
            dead = rebuilt
        }

        // Over budget: the live WebView shows the offline recovery page (no route
        // reload), so the crash loop can't continue while recovery stays possible.
        assertTrue(
            "over-budget recovery must load the local error page, not the route",
            shadowOf(dead).lastLoadDataWithBaseURL?.data?.contains("Reload") == true,
        )
    }

    @Test
    fun `a load-then-crash loop still trips the budget despite successful page loads`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        // Common crash shape: page loads fine, then crashes seconds later. A
        // page-load reset would clear the budget each cycle and never trip; the
        // gap-based budget must accumulate these clustered crashes.
        var dead = activity.webView()
        repeat(4) {
            dead.loadUrl("https://example.com/chat/heavy")
            // Simulate the successful load that precedes each crash.
            dead.webViewClient.onPageFinished(dead, "https://example.com/chat/heavy")
            dead.webViewClient.onRenderProcessGone(dead, rendererGone(crashed = true))
            dead = activity.webView()
        }

        // The 4th crash exceeded the budget even though every cycle had a healthy
        // load in between: the recovery page is shown, breaking the loop.
        assertTrue(
            "load-then-crash loop must still trip the budget",
            shadowOf(dead).lastLoadDataWithBaseURL?.data?.contains("Reload") == true,
        )
    }

    @Test
    fun `system reclaims never exhaust the crash budget`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        // Far more than MAX_RENDERER_CRASHES, but all system reclaims
        // (didCrash=false): each must recover, none counts against the budget.
        repeat(6) {
            val live = activity.webView()
            live.webViewClient.onRenderProcessGone(live, rendererGone(crashed = false))
        }
        val latest = activity.webView()

        latest.webViewClient.onRenderProcessGone(latest, rendererGone(crashed = false))
        assertNotSame(latest, activity.webView())
    }

    private fun rendererGone(crashed: Boolean = false) =
        object : RenderProcessGoneDetail() {
            override fun didCrash(): Boolean = crashed

            override fun rendererPriorityAtExit(): Int = WebView.RENDERER_PRIORITY_IMPORTANT
        }

    @Test
    fun `a managed preset never overrides the server the user picked`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        ServerStore(context).connect("https://example.com")
        val manager = context.getSystemService(RestrictionsManager::class.java)
        Shadow
            .extract<ShadowRestrictionsManager>(manager)
            .setApplicationRestrictions(
                Bundle().apply {
                    putString(ManagedConfig.KEY_SERVER_URLS, "https://managed.example.com")
                },
            )

        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        assertEquals("https://example.com", shadowOf(activity.webView()).lastLoadedUrl)
    }

    @Test
    fun `pinned page load binds an accounts-mode WebView login and clears prior account state`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val origin = "https://example.com"
        val tokenB = jwt("user-b")
        val store = seedPriorAccount(context, origin)
        CookieManager.getInstance().setCookie(origin, "__Host-ap_session=$tokenB")

        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        activity.invokePageReady("$origin/")

        assertEquals(sessionAccountIdentity(origin, tokenB), store.lastAccountIdentity())
        assertTrue(store.load().isEmpty())
        val systemNotifications = context.getSystemService(NotificationManager::class.java)
        assertEquals(0, shadowOf(systemNotifications).size())
    }

    @Test
    fun `pinned login page unbinds an accounts-mode WebView logout`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val origin = "https://example.com"
        CookieManager.getInstance().removeAllCookies(null)
        val store = seedPriorAccount(context, origin)

        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        activity.invokePageReady("$origin/login")

        assertNull(store.lastAccountIdentity())
        assertTrue(store.load().isEmpty())
        val systemNotifications = context.getSystemService(NotificationManager::class.java)
        assertEquals(0, shadowOf(systemNotifications).size())
    }

    /**
     * Seed the store + notifications with a prior account "user-a" (bound,
     * a persisted snapshot, and a leftover per-session notification), returning
     * the store the account-transition tests then assert against.
     */
    private fun seedPriorAccount(
        context: Context,
        origin: String,
    ): SessionSnapshotStore {
        ServerStore(context).connect(origin)
        val store = SessionSnapshotStore(context)
        val accountA = sessionAccountIdentity(origin, jwt("user-a"))
        store.bindAccount(accountA) {}
        assertTrue(
            store.saveIfCurrentAccount(
                mapOf("conv_a" to SessionSnapshot("running", 0)),
                store.generation(),
                accountA,
            ),
        )
        NativeNotificationManager(context).notify("A", "finished", "/c/conv_a", tag = "conv_a")
        return store
    }

    private fun MainActivity.webView(): WebView =
        MainActivity::class
            .java
            .getDeclaredField("webView")
            .apply { isAccessible = true }
            .get(this) as WebView

    private fun launch(): MainActivity {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        return Robolectric.buildActivity(MainActivity::class.java).setup().get()
    }

    private fun MainActivity.switchButton(): View =
        MainActivity::class
            .java
            .getDeclaredField("switchButton")
            .apply { isAccessible = true }
            .get(this) as View

    private fun MainActivity.invoke(
        name: String,
        parameterTypes: Array<Class<*>> = emptyArray(),
        vararg args: Any,
    ) {
        MainActivity::class
            .java
            .getDeclaredMethod(name, *parameterTypes)
            .apply { isAccessible = true }
            .invoke(this, *args)
    }

    private fun MainActivity.invokePageReady(url: String) {
        MainActivity::class
            .java
            .getDeclaredMethod("onPageReady", String::class.java)
            .apply { isAccessible = true }
            .invoke(this, url)
    }

    private fun jwt(subject: String): String = TestJwt.forSubject(subject)
}
