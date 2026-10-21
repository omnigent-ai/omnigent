package ai.omnigent.android

import android.animation.ValueAnimator
import android.app.Activity
import android.content.ClipboardManager
import android.content.Context
import android.content.res.Configuration
import android.graphics.Rect
import android.os.Looper
import android.view.View
import android.widget.ScrollView
import android.widget.TextView
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.time.Duration

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class ConnectionStatusViewTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()

    @After
    fun resetAnimations() = setDurationScale(1f)

    @Test
    fun `reduced motion removes spinner and cuts immediately`() {
        setDurationScale(0f)
        assertFalse(ValueAnimator.areAnimatorsEnabled())
        val activity = Robolectric.buildActivity(Activity::class.java).setup().get()
        val view = ConnectionStatusView(activity) {}
        activity.setContentView(view)
        view.show(ConnectionStatus.Connecting("example.com"))
        assertEquals(View.VISIBLE, view.visibility)
        assertEquals(View.INVISIBLE, view.findViewById<View>(R.id.status_progress).visibility)
        view.hide()
        assertEquals(View.GONE, view.visibility)
    }

    private fun setDurationScale(scale: Float) {
        org.robolectric.shadows.ShadowValueAnimator::class.java
            .getDeclaredMethod("setDurationScale", Float::class.javaPrimitiveType)
            .apply { isAccessible = true }
            .invoke(null, scale)
    }

    @Test
    fun `details never render or copy a callback path query or state`() {
        val view = ConnectionStatusView(context) {}
        view.show(
            ConnectionStatus.ServerUnreachable(
                "example.com",
                UnreachableKind.GENERIC,
                ConnectionFailureDetails(
                    "https://example.com/oauth/callback?code=private-code&state=private-state",
                    "net::ERR_CONNECTION_REFUSED at https://example.com/oauth/callback?code=private-code&state=private-state",
                    1234L,
                ),
            ),
            immediate = true,
        )
        view.findViewById<View>(R.id.status_details_toggle).performClick()
        val rendered = view.findViewById<TextView>(R.id.status_details_text).text.toString()
        view.findViewById<View>(R.id.status_copy_details).performClick()
        val copied =
            context
                .getSystemService(ClipboardManager::class.java)
                .primaryClip!!
                .getItemAt(0)
                .text
                .toString()
        assertEquals(rendered, copied)

        for (text in listOf(rendered, copied)) {
            assertTrue(text.contains("https://example.com"))
            assertTrue(text.contains("net::ERR_CONNECTION_REFUSED"))
            assertFalse(text.contains("/oauth/callback"))
            assertFalse(text.contains("private-code"))
            assertFalse(text.contains("private-state"))
        }
    }

    @Test
    fun `first rendered status remains visible for 500 milliseconds and a new show cancels hide`() {
        val activity = Robolectric.buildActivity(Activity::class.java).setup().get()
        val view = ConnectionStatusView(activity) {}
        activity.setContentView(view)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(1))
        view.show(ConnectionStatus.Connecting("example.com"), immediate = true)
        view.hide()
        assertEquals(View.VISIBLE, view.visibility)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(498))
        assertEquals(View.VISIBLE, view.visibility)
        view.show(
            ConnectionStatus.ServerUnreachable("example.com", UnreachableKind.GENERIC, null),
            immediate = true,
        )
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(2))
        assertEquals(View.VISIBLE, view.visibility)
        view.hide()
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(501))
        assertEquals(View.GONE, view.visibility)
    }

    @Test
    fun `slow connection actions rest at bottom with a gap and scroll on a short screen`() {
        val view = ConnectionStatusView(context) {}
        view.show(ConnectionStatus.ConnectingSlow("example.com"), immediate = true)
        val density = context.resources.displayMetrics.density
        val width = (360 * density).toInt()
        val tallHeight = (800 * density).toInt()
        layout(view, width, tallHeight)
        val headline = view.findViewById<View>(R.id.status_headline)
        val firstAction = view.findViewById<View>(R.id.status_secondary)
        val scroll = view.findViewById<ScrollView>(R.id.status_scroll)
        val headlineBounds = Rect(0, 0, headline.width, headline.height)
        val actionBounds = Rect(0, 0, firstAction.width, firstAction.height)
        view.offsetDescendantRectToMyCoords(headline, headlineBounds)
        view.offsetDescendantRectToMyCoords(firstAction, actionBounds)
        assertTrue(actionBounds.top - headlineBounds.bottom >= (32 * density).toInt())
        assertTrue(scroll.height - actionBounds.bottom <= (100 * density).toInt())

        layout(view, width, (320 * density).toInt())
        assertTrue(scroll.getChildAt(0).height > scroll.height)

        val scaledContext =
            context.createConfigurationContext(
                Configuration(context.resources.configuration).apply { fontScale = 2f },
            )
        scaledContext.setTheme(R.style.Theme_Omnigent)
        val largeTypeView = ConnectionStatusView(scaledContext) {}
        largeTypeView.show(ConnectionStatus.ConnectingSlow("example.com"), immediate = true)
        layout(largeTypeView, width, (320 * density).toInt())
        val largeTypeScroll = largeTypeView.findViewById<ScrollView>(R.id.status_scroll)
        assertTrue(largeTypeScroll.getChildAt(0).height > largeTypeScroll.height)
    }

    private fun layout(
        view: View,
        width: Int,
        height: Int,
    ) {
        view.measure(
            View.MeasureSpec.makeMeasureSpec(width, View.MeasureSpec.EXACTLY),
            View.MeasureSpec.makeMeasureSpec(height, View.MeasureSpec.EXACTLY),
        )
        view.layout(0, 0, width, height)
    }
}
