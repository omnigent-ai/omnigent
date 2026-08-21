package ai.omnigent.android

import android.app.Activity
import android.os.Looper
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
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class OidcLoginManagerTest {
    // Port 1 is never listening — requestTicket fails fast, and the flow ends
    // without a token. The start/cancel bookkeeping tests use it so they never
    // touch a real login; the delivery tests stub the two network steps instead.
    private val deadOrigin = "http://127.0.0.1:1"
    private val origin = "https://server.example"

    // Build the host once per test and reuse it: driving an Activity lifecycle
    // idles the main looper, which would run a finished flow's queued completion
    // (the post that frees the slot) in the middle of the assertions below.
    private fun activity(): Activity = Robolectric.buildActivity(Activity::class.java).setup().get()

    // The flow completes on its own thread and posts back; pump the (paused)
    // main looper until the post lands or the window closes.
    private fun drainMain(condition: () -> Boolean = { false }) {
        val deadline = System.currentTimeMillis() + 2_000
        while (System.currentTimeMillis() < deadline) {
            shadowOf(Looper.getMainLooper()).idle()
            if (condition()) return
            Thread.sleep(5)
        }
    }

    @Test
    fun `second start while a flow is in flight is refused`() {
        val activity = activity()
        val manager = OidcLoginManager()
        assertTrue(manager.start(activity, deadOrigin) { _, _ -> })
        assertFalse(manager.start(activity, deadOrigin) { _, _ -> })
        manager.shutdown()
    }

    @Test
    fun `cancel frees the manager for an immediate new start`() {
        val activity = activity()
        val manager = OidcLoginManager()
        assertTrue(manager.start(activity, deadOrigin) { _, _ -> })
        manager.cancel()
        assertTrue(manager.start(activity, deadOrigin) { _, _ -> })
        manager.shutdown()
    }

    @Test
    fun `a completed flow delivers the origin it was started for with the token`() {
        val activity = activity()
        val manager = OidcLoginManager()
        manager.requestTicket = { OidcLoginManager.Ticket("ticket-1", "/auth/login?t=1") }
        manager.pollForToken = { _, _ -> "session-jwt" }

        var delivered: Pair<String, String>? = null
        assertTrue(manager.start(activity, origin) { o, t -> delivered = o to t })
        drainMain { delivered != null }

        assertEquals(origin to "session-jwt", delivered)
        manager.shutdown()
    }

    @Test
    fun `a cancelled flow does not deliver a token that lands after the switch`() {
        val activity = activity()
        val manager = OidcLoginManager()
        val polling = CountDownLatch(1)
        val cancelled = CountDownLatch(1)
        manager.requestTicket = { OidcLoginManager.Ticket("ticket-1", "/auth/login?t=1") }
        manager.pollForToken = { _, _ ->
            // Hold the flow mid-poll until the host has switched servers, then
            // hand back a token anyway — cancel() interrupts this wait.
            polling.countDown()
            runCatching { cancelled.await(2, TimeUnit.SECONDS) }
            "stale-token"
        }

        var delivered: Pair<String, String>? = null
        assertTrue(manager.start(activity, origin) { o, t -> delivered = o to t })
        assertTrue(polling.await(2, TimeUnit.SECONDS))
        manager.cancel()
        cancelled.countDown()
        drainMain { delivered != null }

        assertNull(delivered)
    }

    @Test
    fun `cancel invalidates a browser launch already posted to the main looper`() {
        val activity = activity()
        val manager = OidcLoginManager()
        val polling = CountDownLatch(1)
        val release = CountDownLatch(1)
        manager.requestTicket = { OidcLoginManager.Ticket("ticket-1", "/auth/login?t=1") }
        manager.pollForToken = { _, _ ->
            polling.countDown()
            release.await(2, TimeUnit.SECONDS)
            null
        }

        assertTrue(manager.start(activity, origin) { _, _ -> })
        assertTrue(polling.await(2, TimeUnit.SECONDS))
        manager.cancel()
        shadowOf(Looper.getMainLooper()).idle()

        assertNull(shadowOf(activity).nextStartedActivity)
        release.countDown()
    }

    @Test
    fun `destroyed activity cannot launch a posted browser handoff`() {
        val controller = Robolectric.buildActivity(Activity::class.java).setup()
        val activity = controller.get()
        val manager = OidcLoginManager()
        val ticketRequested = CountDownLatch(1)
        val releaseTicket = CountDownLatch(1)
        manager.requestTicket = {
            ticketRequested.countDown()
            releaseTicket.await(2, TimeUnit.SECONDS)
            OidcLoginManager.Ticket("ticket-1", "/auth/login?t=1")
        }
        manager.pollForToken = { _, _ -> null }

        assertTrue(manager.start(activity, origin) { _, _ -> })
        assertTrue(ticketRequested.await(2, TimeUnit.SECONDS))
        controller.destroy()
        releaseTicket.countDown()
        drainMain()

        assertNull(shadowOf(activity).nextStartedActivity)
        manager.shutdown()
    }
}
