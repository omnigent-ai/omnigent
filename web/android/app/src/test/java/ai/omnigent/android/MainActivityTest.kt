package ai.omnigent.android

import android.content.res.Configuration
import androidx.core.view.WindowInsetsControllerCompat
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
    fun `configuration change preserves explicit light system bars`() {
        // Skip onCreate to isolate the bare config-change path from WebView dispatch.
        val activity = Robolectric.buildActivity(MainActivity::class.java).get()
        activity.applyColorScheme(isLight = true)
        val insetsController = WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        val darkConfiguration =
            Configuration(activity.resources.configuration).apply {
                uiMode =
                    (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or
                    Configuration.UI_MODE_NIGHT_YES
            }
        activity.onConfigurationChanged(darkConfiguration)

        assertTrue(insetsController.isAppearanceLightStatusBars)
        assertTrue(insetsController.isAppearanceLightNavigationBars)
    }

    private fun MainActivity.applyColorScheme(isLight: Boolean) {
        MainActivity::class
            .java
            .getDeclaredMethod("applyColorScheme", Boolean::class.java)
            .apply { isAccessible = true }
            .invoke(this, isLight)
    }
}
