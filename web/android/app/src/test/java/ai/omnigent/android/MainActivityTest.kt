package ai.omnigent.android

import android.content.res.Configuration
import android.os.Looper
import android.text.TextUtils
import android.view.Gravity
import android.view.View
import android.webkit.WebView
import android.widget.FrameLayout
import android.widget.TextView
import androidx.core.graphics.Insets
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
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
    fun `server switcher starts centered with a capped accessible label`() {
        val host = "a-very-long-server-hostname-that-needs-truncation.example.com"
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://$host")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val density = activity.resources.displayMetrics.density
        val layout = button.layoutParams as FrameLayout.LayoutParams

        assertEquals(Gravity.TOP or Gravity.CENTER_HORIZONTAL, layout.gravity)
        assertEquals((172 * density).toInt(), button.maxWidth)
        assertEquals((48 * density).toInt(), button.minWidth)
        assertEquals(TextUtils.TruncateAt.MIDDLE, button.ellipsize)
        assertTrue(button.isSingleLine)
        assertEquals(host, button.contentDescription)
    }

    @Test
    @Config(qualifiers = "mdpi")
    fun `server switcher band uses an absolute left margin`() {
        val host = "a-very-long-server-hostname-that-needs-truncation.example.com"
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://$host")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        val band = ServerSwitcherBand(0.3, 0.8)
        activity.setSwitcherBand(band)

        layout(parent, width = 1000, height = 600)
        layout(parent, width = 1000, height = 600)
        // The width cap changes with the container, so the shared layout
        // listener repositions once more; a third pass applies it.
        layout(parent, width = 1000, height = 600)

        val layout = button.layoutParams as FrameLayout.LayoutParams
        // mdpi: the 48dp header-control reserve is 48px at each band edge.
        val expectedLeft = serverSwitcherLeftMargin(1000, button.width, band, edgeReserve = 48)
        assertEquals(Gravity.TOP or Gravity.LEFT, layout.gravity)
        assertEquals(expectedLeft, layout.leftMargin)
        assertEquals(expectedLeft, button.left)
        assertEquals(expectedLeft + button.width, button.right)
    }

    @Test
    fun `server switcher hides beside a narrow right edge band`() {
        val host = "a-very-long-server-hostname-that-needs-truncation.example.com"
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://$host")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        val band = ServerSwitcherBand(0.99, 1.0)
        layout(parent, width = 1000, height = 600)
        activity.setSwitcherBand(band)

        layout(parent, width = 1000, height = 600)

        val visibleWidth = minOf(button.right, parent.width) - maxOf(button.left, 0)
        assertEquals(View.INVISIBLE, button.visibility)
        assertTrue(button.width > serverSwitcherBandWidth(parent.width, band))
        assertTrue(button.left >= 0)
        assertTrue(button.right <= parent.width)
        assertEquals(button.width, visibleWidth)
    }

    @Test
    @Config(qualifiers = "mdpi")
    fun `server switcher absolute left margin does not mirror in RTL`() {
        val host = "a-very-long-server-hostname-that-needs-truncation.example.com"
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://$host")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        val band = ServerSwitcherBand(0.3, 0.8)
        parent.layoutDirection = View.LAYOUT_DIRECTION_RTL
        activity.setSwitcherBand(band)

        layout(parent, width = 1000, height = 600)
        layout(parent, width = 1000, height = 600)
        // Third pass: the container-driven width cap change repositions once more.
        layout(parent, width = 1000, height = 600)

        val expectedLeft = serverSwitcherLeftMargin(1000, button.width, band, edgeReserve = 48)
        assertEquals(expectedLeft, button.left)
        assertEquals(expectedLeft + button.width, button.right)
    }

    @Test
    @Config(qualifiers = "mdpi")
    fun `server switcher recentres when only its parent grows`() {
        val host = "a-very-long-server-hostname-that-needs-truncation.example.com"
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://$host")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        // This resize keeps insets unchanged; avoid Robolectric redispatching them.
        ViewCompat.setOnApplyWindowInsetsListener(activity.webView(), null)
        val band = ServerSwitcherBand(0.0, 1.0)
        activity.setSwitcherBand(band)

        layout(parent, width = 400, height = 600)
        layout(parent, width = 400, height = 600)
        val initialWidth = button.width
        assertEquals(
            serverSwitcherLeftMargin(400, initialWidth, band, edgeReserve = 48),
            button.left,
        )
        button.removeLayoutChangeListeners()

        layout(parent, width = 700, height = 600)
        val params = button.layoutParams as FrameLayout.LayoutParams
        assertEquals(initialWidth, button.width)
        val grownLeft = serverSwitcherLeftMargin(700, initialWidth, band, edgeReserve = 48)
        assertEquals(grownLeft, params.leftMargin)

        layout(parent, width = 700, height = 600)
        assertEquals(grownLeft, button.left)
    }

    @Test
    @Config(qualifiers = "mdpi")
    fun `redelivering the same band repositions after a parent resize`() {
        val host = "a-very-long-server-hostname-that-needs-truncation.example.com"
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://$host")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        val band = ServerSwitcherBand(0.0, 1.0)
        activity.setSwitcherBand(band)

        layout(parent, width = 400, height = 600)
        layout(parent, width = 400, height = 600)
        val staleLeft = (button.layoutParams as FrameLayout.LayoutParams).leftMargin
        layout(parent, width = 700, height = 600)
        layout(parent, width = 700, height = 600)

        val params = button.layoutParams as FrameLayout.LayoutParams
        params.leftMargin = staleLeft
        button.layoutParams = params
        assertFalse(activity.isDestroyed)
        assertFalse(activity.isFinishing)
        val redeliveredBand = ServerSwitcherBand(0.0, 1.0)
        activity.receiveSwitcherBand(redeliveredBand)

        assertTrue(activity.switcherBand() === redeliveredBand)
        assertEquals(
            serverSwitcherLeftMargin(700, button.width, redeliveredBand, edgeReserve = 48),
            (button.layoutParams as FrameLayout.LayoutParams).leftMargin,
        )
    }

    @Test
    fun `server switcher width bounds follow the published band`() {
        val host = "a-very-long-server-hostname-that-needs-truncation.example.com"
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://$host")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        val density = activity.resources.displayMetrics.density
        val recoveryFloor = (48 * density).toInt()
        layout(parent, width = 1000, height = 600)

        val wideBand = ServerSwitcherBand(0.4, 0.6)
        activity.setSwitcherBand(wideBand)
        assertEquals(104, button.maxWidth)
        assertEquals(recoveryFloor, button.minWidth)
        layout(parent, width = 1000, height = 600)
        assertEquals(View.VISIBLE, button.visibility)
        // The label may measure under the cap; placement must respect the reserve.
        assertTrue(button.width <= 104)
        val expectedLeft = serverSwitcherLeftMargin(1000, button.width, wideBand, edgeReserve = 48)
        assertEquals(expectedLeft, button.left)
        assertEquals(expectedLeft + button.width, button.right)

        activity.setSwitcherBand(ServerSwitcherBand(0.45, 0.55))
        assertEquals(recoveryFloor, button.maxWidth)
        assertEquals(recoveryFloor, button.minWidth)
        layout(parent, width = 1000, height = 600)
        assertEquals(View.INVISIBLE, button.visibility)
    }

    @Test
    @Config(qualifiers = "mdpi")
    fun `hidden state clears stale placement until a fresh band is shown`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        layout(parent, width = 1000, height = 600)
        activity.receiveSwitcherBand(ServerSwitcherBand(0.2, 0.8))

        activity.receiveSwitcherHidden(true)

        assertEquals(View.INVISIBLE, button.visibility)
        assertNull(activity.switcherBandOrNull())

        activity.receiveSwitcherBand(ServerSwitcherBand(0.2, 0.8))
        assertEquals(View.INVISIBLE, button.visibility)
        activity.receiveSwitcherHidden(false)
        assertEquals(View.VISIBLE, button.visibility)
    }

    @Test
    @Config(qualifiers = "mdpi")
    fun `unchanged insets do not request another switcher layout`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        val insets =
            WindowInsetsCompat
                .Builder()
                .setInsets(WindowInsetsCompat.Type.systemBars(), Insets.of(0, 24, 0, 0))
                .build()

        ViewCompat.dispatchApplyWindowInsets(activity.webView(), insets)
        layout(parent, width = 1000, height = 600)
        assertFalse(button.isLayoutRequested)

        ViewCompat.dispatchApplyWindowInsets(activity.webView(), insets)

        assertFalse(button.isLayoutRequested)
    }

    private fun layout(
        view: View,
        width: Int,
        height: Int,
    ) {
        // Force the traversal: an unchanged frame otherwise skips onLayout, so
        // children keep their stale positions.
        view.requestLayout()
        view.measure(
            View.MeasureSpec.makeMeasureSpec(width, View.MeasureSpec.EXACTLY),
            View.MeasureSpec.makeMeasureSpec(height, View.MeasureSpec.EXACTLY),
        )
        view.layout(0, 0, width, height)
    }

    private fun MainActivity.webView(): WebView =
        MainActivity::class
            .java
            .getDeclaredField("webView")
            .apply { isAccessible = true }
            .get(this) as WebView

    private fun MainActivity.switchButton(): TextView =
        MainActivity::class
            .java
            .getDeclaredField("switchButton")
            .apply { isAccessible = true }
            .get(this) as TextView

    private fun MainActivity.setSwitcherBand(band: ServerSwitcherBand) {
        MainActivity::class
            .java
            .getDeclaredField("switcherBand")
            .apply { isAccessible = true }
            .set(this, band)
        MainActivity::class
            .java
            .getDeclaredMethod("positionServerSwitcher")
            .apply { isAccessible = true }
            .invoke(this)
    }

    private fun MainActivity.switcherBand(): ServerSwitcherBand =
        MainActivity::class
            .java
            .getDeclaredField("switcherBand")
            .apply { isAccessible = true }
            .get(this) as ServerSwitcherBand

    private fun MainActivity.switcherBandOrNull(): ServerSwitcherBand? =
        MainActivity::class
            .java
            .getDeclaredField("switcherBand")
            .apply { isAccessible = true }
            .get(this) as? ServerSwitcherBand

    private fun View.removeLayoutChangeListeners() {
        val listenerInfo =
            View::class
                .java
                .getDeclaredField("mListenerInfo")
                .apply { isAccessible = true }
                .get(this) ?: return
        val listeners =
            listenerInfo::class
                .java
                .getDeclaredField("mOnLayoutChangeListeners")
                .apply { isAccessible = true }
                .get(listenerInfo) as? Iterable<*> ?: return
        listeners
            .filterIsInstance<View.OnLayoutChangeListener>()
            .toList()
            .forEach(::removeOnLayoutChangeListener)
    }

    private fun MainActivity.receiveSwitcherBand(band: ServerSwitcherBand) {
        val method =
            MainActivity::class
                .java
                .getDeclaredMethod("receiveServerSwitcherBand", ServerSwitcherBand::class.java)
                .apply { isAccessible = true }
        shadowOf(Looper.getMainLooper()).runPaused { method.invoke(this, band) }
    }

    private fun MainActivity.receiveSwitcherHidden(hidden: Boolean) {
        val method =
            MainActivity::class
                .java
                .getDeclaredMethod("receiveServerSwitcherHidden", Boolean::class.javaPrimitiveType)
                .apply { isAccessible = true }
        shadowOf(Looper.getMainLooper()).runPaused { method.invoke(this, hidden) }
    }
}
