package ai.omnigent.android

import android.content.Context
import android.content.RestrictionsManager
import android.content.res.Configuration
import android.os.Bundle
import android.view.ViewGroup
import android.webkit.RenderProcessGoneDetail
import android.webkit.WebView
import androidx.core.view.WindowInsetsControllerCompat
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotSame
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

        // Drive MAX_RENDERER_CRASHES + 1 clustered crashes: the last exceeds the
        // budget. Each still rebuilds — the manual recovery paths (server-switcher
        // Reload / Switch) reuse the webView field and would be dead if we left
        // the corpse in place — so the loop is broken by WHAT loads, not by
        // refusing to rebuild.
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

    private fun MainActivity.webView(): WebView =
        MainActivity::class
            .java
            .getDeclaredField("webView")
            .apply { isAccessible = true }
            .get(this) as WebView
}
