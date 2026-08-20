package ai.omnigent.android

import android.content.Context
import android.content.res.Configuration
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class ThemeTest {
    @Test
    fun themeTracksSystemNightMode() {
        assertEquals(true, isLightTheme(Configuration.UI_MODE_NIGHT_NO))
        assertEquals(false, isLightTheme(Configuration.UI_MODE_NIGHT_YES))
    }

    private fun isLightTheme(nightMode: Int): Boolean {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val configuration = Configuration(context.resources.configuration)
        configuration.uiMode =
            (configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or nightMode
        val themedContext = context.createConfigurationContext(configuration)
        themedContext.setTheme(R.style.Theme_Omnigent)
        val attributes =
            themedContext.obtainStyledAttributes(
                intArrayOf(android.R.attr.isLightTheme),
            )
        return try {
            attributes.getBoolean(0, false)
        } finally {
            attributes.recycle()
        }
    }
}
