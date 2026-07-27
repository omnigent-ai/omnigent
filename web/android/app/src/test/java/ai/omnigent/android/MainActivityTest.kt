package ai.omnigent.android

import android.content.res.Configuration
import android.webkit.WebView
import androidx.core.view.WindowInsetsControllerCompat
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

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

    private fun MainActivity.webView(): WebView =
        MainActivity::class
            .java
            .getDeclaredField("webView")
            .apply { isAccessible = true }
            .get(this) as WebView
}
