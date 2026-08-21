package ai.omnigent.android

import android.content.Context
import android.content.RestrictionsManager
import android.content.res.Configuration
import android.os.Bundle
import android.webkit.WebView
import android.widget.Button
import android.widget.EditText
import androidx.core.graphics.Insets
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
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
    fun `cutout-only safe area is published on every edge`() {
        val cutout = Insets.of(11, 23, 31, 0)
        val insets =
            WindowInsetsCompat
                .Builder()
                .setInsets(WindowInsetsCompat.Type.displayCutout(), cutout)
                .build()

        val safeArea = systemSafeAreaInsets(insets)
        assertEquals(cutout, safeArea)

        val script = androidSafeAreaScript(safeArea, 1f)
        assertTrue(script.contains("const top = '23.0px'"))
        assertTrue(script.contains("const left = '11.0px'"))
        assertTrue(script.contains("const right = '31.0px'"))
        assertTrue(script.contains("setProperty('--omnigent-safe-left', left)"))
        assertTrue(script.contains("setProperty('--omnigent-safe-right', right)"))
    }

    @Test
    fun `landscape cutout unions with the system bars per edge`() {
        // Landscape phone: the gesture nav bar keeps the bottom inset while the
        // camera cutout eats the left edge — a systemBars()-only source would
        // report left as 0 and let the rail/drawers slide under the cutout.
        val bars = Insets.of(0, 24, 0, 16)
        val cutout = Insets.of(31, 0, 0, 0)
        val insets =
            WindowInsetsCompat
                .Builder()
                .setInsets(WindowInsetsCompat.Type.systemBars(), bars)
                .setInsets(WindowInsetsCompat.Type.displayCutout(), cutout)
                .build()

        val safeArea = systemSafeAreaInsets(insets)
        assertEquals(Insets.of(31, 24, 0, 16), safeArea)

        val script = androidSafeAreaScript(safeArea, 1f)
        assertTrue(script.contains("const top = '24.0px'"))
        assertTrue(script.contains("const bottom = '16.0px'"))
        assertTrue(script.contains("const left = '31.0px'"))
    }

    @Test
    fun `connect activity marks its handoff as an explicit server change`() {
        val activity = Robolectric.buildActivity(ConnectActivity::class.java).setup().get()
        activity.findViewById<EditText>(R.id.server_url).setText("https://new.example")

        activity.findViewById<Button>(R.id.connect).performClick()

        val intent = shadowOf(activity).nextStartedActivity
        assertTrue(intent.getBooleanExtra(ConnectActivity.EXTRA_SERVER_CHANGED, false))
    }

    @Test
    fun `webview leaves algorithmic darkening disabled`() {
        testStore().connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        assertFalse(activity.testWebView().settings.isAlgorithmicDarkeningAllowed)
    }

    @Test
    @Config(qualifiers = "notnight")
    fun `light configuration uses dark status bar icons`() {
        testStore().connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        assertTrue(insetsController.isAppearanceLightStatusBars)
        assertTrue(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    @Config(qualifiers = "night")
    fun `dark configuration uses light status bar icons`() {
        testStore().connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        assertFalse(insetsController.isAppearanceLightStatusBars)
        assertFalse(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    fun `configuration change updates system bar icon polarity`() {
        testStore().connect("https://example.com")
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

        assertEquals("https://example.com", shadowOf(activity.testWebView()).lastLoadedUrl)
    }
}
