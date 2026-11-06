package ai.omnigent.android

import android.animation.ValueAnimator
import android.app.Activity
import android.content.ClipboardManager
import android.content.Context
import android.content.res.Configuration
import android.graphics.Rect
import android.graphics.drawable.ColorDrawable
import android.os.Build
import android.os.Looper
import android.text.Spanned
import android.text.style.AbsoluteSizeSpan
import android.text.style.ForegroundColorSpan
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.ImageView
import android.widget.ScrollView
import android.widget.TextView
import androidx.core.content.ContextCompat
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

    @Test
    fun `expanded details use muted labels divider and copy icon in both themes`() {
        for (nightMode in listOf(Configuration.UI_MODE_NIGHT_NO, Configuration.UI_MODE_NIGHT_YES)) {
            val themed =
                context.createConfigurationContext(
                    Configuration(context.resources.configuration).apply {
                        uiMode = (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or nightMode
                    },
                )
            themed.setTheme(R.style.Theme_Omnigent)
            val view = ConnectionStatusView(themed) {}
            view.show(
                ConnectionStatus.ServerUnreachable(
                    "example.com",
                    UnreachableKind.GENERIC,
                    ConnectionFailureDetails("https://example.com", "ERROR_CONNECT", 1234L),
                ),
                immediate = true,
            )
            view.findViewById<Button>(R.id.status_details_toggle).performClick()

            val detailsText = view.findViewById<TextView>(R.id.status_details_text)
            val rendered = detailsText.text
            assertTrue(rendered is Spanned)
            val styled = rendered as Spanned
            for (label in listOf(
                R.string.status_server,
                R.string.status_error,
                R.string.status_time,
                R.string.status_app_version_label,
            )) {
                val start = styled.indexOf(themed.getString(label))
                assertTrue(start >= 0)
                val color =
                    styled.getSpans(
                        start,
                        start + themed.getString(label).length,
                        ForegroundColorSpan::class.java,
                    )
                val size =
                    styled.getSpans(
                        start,
                        start + themed.getString(label).length,
                        AbsoluteSizeSpan::class.java,
                    )
                assertEquals(
                    ContextCompat.getColor(themed, R.color.brand_muted_foreground),
                    color.single().foregroundColor,
                )
                assertEquals(12, size.single().size)
            }
            val dividerId =
                themed.resources.getIdentifier(
                    "status_details_divider",
                    "id",
                    themed.packageName,
                )
            assertTrue(dividerId != 0)
            val divider = view.findViewById<View>(dividerId)
            assertEquals(View.VISIBLE, divider.visibility)
            assertEquals(
                ContextCompat.getColor(themed, R.color.brand_border),
                (divider.background as ColorDrawable).color,
            )
            val copy = view.findViewById<Button>(R.id.status_copy_details)
            assertEquals(
                (48 * themed.resources.displayMetrics.density + .5f).toInt(),
                copy.layoutParams.height,
            )
            assertTrue(copy.compoundDrawablesRelative[0] != null)
            assertEquals(Gravity.START or Gravity.CENTER_VERTICAL, copy.gravity)
        }
    }

    @Test
    fun `host chip and details use the approved icons and alignment`() {
        val view = ConnectionStatusView(context) {}
        view.show(
            ConnectionStatus.ServerUnreachable(
                "example.com",
                UnreachableKind.GENERIC,
                ConnectionFailureDetails("https://example.com", "ERROR_CONNECT", 1234L),
            ),
            immediate = true,
        )
        val host = view.findViewById<TextView>(R.id.status_host)
        val details = view.findViewById<Button>(R.id.status_details_toggle)
        assertTrue(host.compoundDrawablesRelative[0] != null)
        assertEquals(Gravity.START or Gravity.CENTER_VERTICAL, details.gravity)
        assertTrue(details.compoundDrawablesRelative[2] != null)
        assertEquals(0f, details.compoundDrawablesRelative[2].level.toFloat(), 0f)
        details.performClick()
        assertEquals(View.VISIBLE, view.findViewById<View>(R.id.status_details_content).visibility)
        assertEquals(1, details.compoundDrawablesRelative[2].level)
        assertTrue(view.findViewById<ImageView>(R.id.status_icon).drawable != null)
    }

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
                    "https://example.com/oauth/callback?code=private-code&state=private-state&ticket=private-ticket#private-fragment",
                    "https://example.com/oauth/callback?code=private-code&state=ERR_PRIVATESTATE&ticket=private-ticket#private-fragment",
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
        assertTrue(copied.startsWith(rendered))
        assertTrue(copied.contains("Time (UTC)\n1970-01-01T00:00:01.234Z"))
        assertTrue(copied.contains("Android API\n${Build.VERSION.SDK_INT}"))
        assertTrue(Regex("WebView version\\n[^\\n]+").containsMatchIn(copied))
        assertFalse(rendered.contains("Time (UTC)"))
        assertFalse(rendered.contains("Android API"))
        assertFalse(rendered.contains("WebView version"))

        for (text in listOf(rendered, copied)) {
            assertTrue(text.contains("https://example.com"))
            assertTrue(text.contains("ERROR_UNKNOWN"))
            assertFalse(text.contains("/oauth/callback"))
            assertFalse(text.contains("private-code"))
            assertFalse(text.contains("private-state"))
            assertFalse(text.contains("ERR_PRIVATESTATE"))
            assertFalse(text.contains("ticket="))
            assertFalse(text.contains("private-ticket"))
            assertFalse(text.contains("private-fragment"))
            assertFalse(text.contains("Cookie"))
        }
    }

    @Test
    fun `details accept only a bare net error beside a known WebView code`() {
        val view = ConnectionStatusView(context) {}
        val details =
            ConnectionFailureDetails(
                "https://example.com",
                "net::ERR_CONNECTION_REFUSED (ERROR_CONNECT)",
                1234L,
            )
        view.show(
            ConnectionStatus.ServerUnreachable("example.com", UnreachableKind.GENERIC, details),
            immediate = true,
        )
        view.findViewById<View>(R.id.status_details_toggle).performClick()
        val rendered = view.findViewById<TextView>(R.id.status_details_text).text.toString()
        assertTrue(rendered, rendered.contains("net::ERR_CONNECTION_REFUSED (ERROR_CONNECT)"))

        view.show(
            ConnectionStatus.ServerUnreachable(
                "example.com",
                UnreachableKind.GENERIC,
                details.copy(error = "net::ERR_FOO?x=1 (ERROR_CONNECT)"),
            ),
            immediate = true,
        )
        view.findViewById<View>(R.id.status_details_toggle).performClick()
        val rejected = view.findViewById<TextView>(R.id.status_details_text).text.toString()
        assertTrue(rejected.contains("ERROR_UNKNOWN"))
        assertFalse(rejected.contains("ERR_FOO"))
    }

    @Test
    fun `deferred hide disables actions and show rearms them`() {
        val activity = Robolectric.buildActivity(Activity::class.java).setup().get()
        val selected = mutableListOf<StatusAction>()
        val view = ConnectionStatusView(activity) { selected.add(it) }
        activity.setContentView(view)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(1))
        val status =
            ConnectionStatus.ServerUnreachable(
                "example.com",
                UnreachableKind.GENERIC,
                null,
            )
        view.show(status, immediate = true)
        val retry = view.findViewById<Button>(R.id.status_primary)
        val changeServer = view.findViewById<Button>(R.id.status_secondary)
        view.hide()
        assertEquals(View.VISIBLE, view.visibility)
        assertFalse(retry.isEnabled)
        assertFalse(changeServer.isEnabled)
        retry.performClick()
        changeServer.performClick()
        assertTrue(selected.isEmpty())
        view.show(status, immediate = true)
        assertTrue(retry.isEnabled)
        retry.performClick()
        assertEquals(listOf(StatusAction.RETRY), selected)
    }

    @Test
    fun `actions fade with content and stay disabled during the old state fade`() {
        val activity = Robolectric.buildActivity(Activity::class.java).setup().get()
        val view = ConnectionStatusView(activity) {}
        activity.setContentView(view)
        layout(view, 1080, 1920)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(1))
        view.show(
            ConnectionStatus.ServerUnreachable("example.com", UnreachableKind.GENERIC, null),
            immediate = true,
        )
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(501))
        val actions = view.findViewById<View>(R.id.status_actions)
        val retry = view.findViewById<Button>(R.id.status_primary)
        assertTrue(ValueAnimator.areAnimatorsEnabled())
        view.show(ConnectionStatus.SignInRequired("example.com", null, false))
        shadowOf(Looper.getMainLooper()).runOneTask()
        shadowOf(Looper.getMainLooper()).runOneTask()
        assertTrue(actions.alpha < 1f)
        assertEquals(view.findViewById<View>(R.id.status_content).alpha, actions.alpha)
        assertFalse(retry.isEnabled)
        shadowOf(Looper.getMainLooper()).idleFor(Duration.ofMillis(250))
        assertEquals(1f, actions.alpha)
        assertTrue(retry.isEnabled)
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
