package ai.omnigent.android

import android.content.Context
import android.content.res.Configuration
import android.graphics.Color
import android.graphics.drawable.ColorDrawable
import android.view.ViewGroup
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class ThemeTest {
    @Test
    @Config(qualifiers = "night")
    fun connectScreenUsesWebPaletteInSystemDarkMode() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        context
            .getSharedPreferences(
                "ai.omnigent.android.servers",
                Context.MODE_PRIVATE,
            ).edit()
            .clear()
            .commit()

        val launcher = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val connectIntent = shadowOf(launcher).nextStartedActivity
        assertEquals(ConnectActivity::class.java.name, connectIntent.component?.className)

        val connect =
            Robolectric
                .buildActivity(
                    ConnectActivity::class.java,
                    connectIntent,
                ).setup()
                .get()
        val background =
            connect
                .findViewById<ViewGroup>(
                    android.R.id.content,
                ).getChildAt(0)
                .background
        assertTrue(background is ColorDrawable)
        assertEquals(Color.rgb(14, 16, 19), (background as ColorDrawable).color)
    }

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
