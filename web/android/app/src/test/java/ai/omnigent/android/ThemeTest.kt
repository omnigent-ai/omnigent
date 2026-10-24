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
import org.xmlpull.v1.XmlPullParser

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class ThemeTest {
    @Test
    fun launchMarkIsCentredInsideThePlatformSafeCircle() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        val parser = context.resources.getXml(R.drawable.splash_mark)
        while (parser.eventType != XmlPullParser.END_DOCUMENT && parser.name != "group") {
            parser.next()
        }
        assertEquals("group", parser.name)
        val namespace = "http://schemas.android.com/apk/res/android"
        val scaleX = parser.getAttributeValue(namespace, "scaleX").toFloat()
        val scaleY = parser.getAttributeValue(namespace, "scaleY").toFloat()
        val translateX = parser.getAttributeValue(namespace, "translateX").toFloat()
        val translateY = parser.getAttributeValue(namespace, "translateY").toFloat()
        val left = 113f * scaleX + translateX
        val right = 408f * scaleX + translateX
        val top = 46f * scaleY + translateY
        val bottom = 339f * scaleY + translateY
        assertTrue(kotlin.math.abs((left + right) / 2 - 227f) <= 8f)
        assertTrue(kotlin.math.abs((top + bottom) / 2 - 227f) <= 8f)
        assertTrue(left >= 76f && right <= 378f && top >= 76f && bottom <= 378f)
    }

    @Test
    @Config(sdk = [35], qualifiers = "night")
    fun nightLaunchKeepsPlatformSplashAttributes() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        context.setTheme(R.style.Theme_Omnigent_Launch)
        val attributes =
            context.obtainStyledAttributes(
                intArrayOf(
                    android.R.attr.windowSplashScreenBackground,
                    android.R.attr.windowSplashScreenAnimatedIcon,
                    android.R.attr.windowBackground,
                ),
            )
        try {
            assertEquals(R.color.brand_background, attributes.getResourceId(0, 0))
            assertEquals(R.drawable.splash_mark, attributes.getResourceId(1, 0))
            assertEquals(R.color.brand_background, attributes.getResourceId(2, 0))
        } finally {
            attributes.recycle()
        }
    }

    @Test
    @Config(sdk = [28])
    fun pre31LaunchUsesStaticMarkThenActivityThemeUsesBrandBackground() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        context.setTheme(R.style.Theme_Omnigent_Launch)
        val launch = context.obtainStyledAttributes(intArrayOf(android.R.attr.windowBackground))
        assertEquals(R.drawable.launch_background, launch.getResourceId(0, 0))
        launch.recycle()
        context.setTheme(R.style.Theme_Omnigent)
        val activity = context.obtainStyledAttributes(intArrayOf(android.R.attr.windowBackground))
        assertEquals(R.color.brand_background, activity.getResourceId(0, 0))
        activity.recycle()
    }

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
