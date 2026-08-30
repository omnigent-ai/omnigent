package ai.omnigent.android

import android.webkit.WebView
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.annotation.Implementation
import org.robolectric.annotation.Implements
import org.robolectric.shadows.ShadowWebView

/**
 * Background lifecycle: leaving the foreground must suspend WebView JS timers.
 *
 * Android never auto-pauses a WebView: when the user backgrounds the app, the
 * renderer keeps executing JS timers, sockets, and rAF until the OS freezes
 * the cached process, draining battery the whole time. The shell owns the
 * WebView, so it must suspend JS on leaving the foreground
 * (`webView.pauseTimers()` — timers only, so screen-off audio keeps playing)
 * and resume on returning (`webView.resumeTimers()`).
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], shadows = [MainActivityLifecycleTest.TimerTrackingShadowWebView::class])
class MainActivityLifecycleTest {
    /**
     * Records pauseTimers()/resumeTimers(), which the stock [ShadowWebView]
     * does not track. The calls are process-global on a real WebView, so the
     * flags are static.
     */
    @Implements(WebView::class)
    class TimerTrackingShadowWebView : ShadowWebView() {
        companion object {
            var pauseTimersCalled = false
            var resumeTimersCalled = false

            fun reset() {
                pauseTimersCalled = false
                resumeTimersCalled = false
            }
        }

        @Implementation
        fun pauseTimers() {
            pauseTimersCalled = true
        }

        @Implementation
        fun resumeTimers() {
            resumeTimersCalled = true
        }
    }

    @Before
    fun reset() {
        TimerTrackingShadowWebView.reset()
        // AppVisibility is a process-wide singleton and Robolectric reuses the
        // sandbox classloader across test classes, so activities other tests
        // started (and never stopped) would suppress the some→none visibility
        // edge that gates pauseTimers(). Start each test from a clean slate.
        resetAppVisibility()
    }

    private fun resetAppVisibility() {
        val field = AppVisibility::class.java.getDeclaredField("startedActivities")
        field.isAccessible = true
        (field.get(AppVisibility) as MutableCollection<*>).clear()
    }

    @Test
    fun `backgrounding the activity suspends WebView JS timers`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        TimerTrackingShadowWebView.reset()

        // The user presses Home: the activity is paused, then stopped.
        controller.pause().stop()

        assertTrue(
            "MainActivity must call webView.pauseTimers() when the app leaves " +
                "the foreground; otherwise the renderer keeps running JS timers, " +
                "sockets, and rAF in the background (battery drain)",
            TimerTrackingShadowWebView.pauseTimersCalled,
        )
    }

    @Test
    fun `returning to the foreground resumes WebView JS timers`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        TimerTrackingShadowWebView.reset()

        // Background, then bring the app back to the foreground.
        controller.pause().stop()
        assertTrue(TimerTrackingShadowWebView.pauseTimersCalled)
        assertFalse(TimerTrackingShadowWebView.resumeTimersCalled)

        controller.restart().resume()

        assertTrue(
            "MainActivity must call webView.resumeTimers() when the app returns " +
                "to the foreground so the page's timers and rendering restart",
            TimerTrackingShadowWebView.resumeTimersCalled,
        )
    }
}
