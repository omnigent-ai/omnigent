package ai.omnigent.android

import android.content.Context
import android.content.RestrictionsManager
import android.content.res.Configuration
import android.os.Bundle
import android.view.MotionEvent
import android.view.TouchDelegate
import android.view.View
import android.webkit.WebView
import android.widget.FrameLayout
import androidx.core.view.WindowInsetsControllerCompat
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
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
    fun `a tap directly on the switch button still opens the server menu`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()

        // Original behavior, unaffected by the touch-delegate addition: a tap
        // that lands inside the pill's own visible bounds still reaches it
        // directly, with no delegate involved.
        assertTrue(button.dispatchTouchEvent(motionEventAt(DOWN, x = 1f, y = 1f)))
        assertTrue(button.dispatchTouchEvent(motionEventAt(UP, x = 1f, y = 1f)))
    }

    @Test
    fun `a touch delegate is installed on the switch button's parent`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val container = activity.switchButton().parent as FrameLayout

        // Proves the layout-change hook actually fires and wires a delegate
        // up in practice, not just that the standalone builder below works.
        assertNotNull(container.touchDelegate)
    }

    @Test
    fun `touch delegate floors the switch button's touch target at 48dp`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val dp = activity.resources.displayMetrics.density

        // Exercise the flooring math directly against #3731's real-device
        // numbers (a ~27dp-tall pill) rather than this environment's own
        // text measurement, which — per Robolectric's font-metrics
        // limitations — doesn't reproduce a real device's ~27dp and would
        // make this assertion meaningless either way.
        val delegate =
            activity.switchButtonTouchDelegate(
                left = 0,
                top = 0,
                right = (100 * dp).toInt(),
                bottom = (27 * dp).toInt(),
            )

        // A point a few dp below the visible pill, but within the floored
        // 48dp target, must still register — this is the bug: before the
        // fix there was no delegate at all, so this point fell through to
        // the WebView underneath.
        assertTrue(
            "tap just below the visible pill should hit the expanded target",
            delegate.onTouchEvent(motionEventAt(DOWN, x = 1f, y = (32 * dp))),
        )

        // A point clearly past the 48dp floor must NOT register — the
        // expansion is bounded, not an unlimited catch-all over the WebView.
        assertFalse(
            "tap well past the floored target should miss",
            delegate.onTouchEvent(motionEventAt(DOWN, x = 1f, y = (60 * dp))),
        )
    }

    private fun MainActivity.webView(): WebView =
        MainActivity::class
            .java
            .getDeclaredField("webView")
            .apply { isAccessible = true }
            .get(this) as WebView

    private fun MainActivity.switchButton(): View =
        MainActivity::class
            .java
            .getDeclaredField("switchButton")
            .apply { isAccessible = true }
            .get(this) as View

    private fun MainActivity.switchButtonTouchDelegate(
        left: Int,
        top: Int,
        right: Int,
        bottom: Int,
    ): TouchDelegate =
        MainActivity::class
            .java
            .getDeclaredMethod(
                "switchButtonTouchDelegate",
                Int::class.java,
                Int::class.java,
                Int::class.java,
                Int::class.java,
            ).apply { isAccessible = true }
            .invoke(this, left, top, right, bottom) as TouchDelegate

    private fun motionEventAt(
        action: Int,
        x: Float,
        y: Float,
    ): MotionEvent = MotionEvent.obtain(0, 0, action, x, y, 0)

    private companion object {
        const val DOWN = MotionEvent.ACTION_DOWN
        const val UP = MotionEvent.ACTION_UP
    }
}
