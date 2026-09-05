package ai.omnigent.android

import android.app.NotificationManager
import android.content.Context
import android.content.RestrictionsManager
import android.content.res.Configuration
import android.os.Bundle
import android.webkit.CookieManager
import android.webkit.WebView
import androidx.core.view.WindowInsetsControllerCompat
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
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

    private fun MainActivity.invokePageReady(url: String) {
        MainActivity::class
            .java
            .getDeclaredMethod("onPageReady", String::class.java)
            .apply { isAccessible = true }
            .invoke(this, url)
    }

    private fun jwt(subject: String): String = TestJwt.forSubject(subject)
}
