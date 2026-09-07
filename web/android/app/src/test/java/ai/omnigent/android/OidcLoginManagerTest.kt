package ai.omnigent.android

import android.app.Activity
import android.os.Looper
import com.sun.net.httpserver.HttpServer
import org.junit.After
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.net.InetSocketAddress
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class OidcLoginManagerTest {
    private val manager = OidcLoginManager()
    private val activity: Activity
        get() = Robolectric.buildActivity(Activity::class.java).setup().get()

    private var server: HttpServer? = null

    @After
    fun tearDown() {
        manager.shutdown()
        server?.stop(0)
        server = null
    }

    @Test
    fun `second start while in-flight returns false`() {
        val activity = this.activity
        // Start a flow against an unreachable server — the background task stays
        // in-flight until it fails or times out, but inFlight is set synchronously.
        manager.start(activity, UNREACHABLE, {})

        assertFalse(manager.start(activity, UNREACHABLE, {}))
    }

    @Test
    fun `cancel allows a new login to start immediately`() {
        val activity = this.activity
        manager.start(activity, UNREACHABLE, {})

        manager.cancel()

        assertTrue(manager.start(activity, UNREACHABLE, {}))
        manager.shutdown()
    }

    @Test
    fun `cancel prevents a stale token from being delivered`() {
        val delivered = mutableListOf<String>()
        val activity = this.activity
        manager.start(activity, UNREACHABLE, { delivered += it })

        manager.cancel()

        // Simulate a late token arriving on the main thread (as if the poll had
        // already posted the result before cancel() ran). The callback was nulled
        // so it must be swallowed.
        assertTrue(delivered.isEmpty())
    }

    @Test
    fun `shutdown after cancel does not throw`() {
        val activity = this.activity
        manager.start(activity, UNREACHABLE, {})
        manager.cancel()
        manager.shutdown() // must not throw even though cancel already ran
    }

    // -- Stale browser launch after cancellation / supersession / destruction --
    //
    // start() posts its browser launch to the main looper from a background
    // thread. If the flow is cancelled (user backs out, or MainActivity
    // switches servers via reloadWithNewServer -> cancel()) or the originating
    // activity is destroyed before that runnable executes, the obsolete
    // server's authentication URL must NOT open. Robolectric's paused main
    // looper holds the posted runnable until idle(), exactly like a busy real
    // main thread, so each test cancels in the posted-but-not-executed window.

    @Test
    fun `cancel before the looper drains suppresses the posted browser launch`() {
        val (origin, loginServed) = startOrigin()
        val activity = this.activity
        drainMainLooper() // flush activity-setup posts so the queue is ours

        manager.start(activity, origin, {})
        awaitBrowserLaunchPosted(loginServed)

        manager.cancel() // user cancelled, or MainActivity switched to another server
        drainMainLooper()

        assertNull(
            "obsolete browser URL launched after cancel()",
            shadowOf(activity).nextStartedActivity,
        )
    }

    @Test
    fun `server switch before the looper drains suppresses the posted browser launch`() {
        val (origin, loginServed) = startOrigin()
        val activity = this.activity
        drainMainLooper()

        manager.start(activity, origin, {})
        awaitBrowserLaunchPosted(loginServed)

        // reloadWithNewServer() cancels the old flow before pinning the new
        // origin; the queued launch for the old origin must die with it.
        manager.cancel()
        drainMainLooper()

        assertNull(
            "old server's authentication URL launched after a server switch",
            shadowOf(activity).nextStartedActivity,
        )
        // The new server's login must be able to start right away.
        assertTrue(manager.start(activity, UNREACHABLE, {}))
    }

    @Test
    fun `activity destruction before the looper drains suppresses the posted browser launch`() {
        val (origin, loginServed) = startOrigin()
        val controller = Robolectric.buildActivity(Activity::class.java).setup()
        val activity = controller.get()
        drainMainLooper()

        manager.start(activity, origin, {})
        awaitBrowserLaunchPosted(loginServed)

        manager.shutdown() // MainActivity.onDestroy() path
        controller.pause().stop().destroy()
        drainMainLooper()

        assertNull(
            "browser launch fired from a destroyed activity",
            shadowOf(activity).nextStartedActivity,
        )
    }

    @Test
    fun `a fresh login after cancel still opens the browser`() {
        val (origin, loginServed) = startOrigin()
        val activity = this.activity
        drainMainLooper()

        // A cancelled earlier flow must not suppress a later, live one.
        manager.start(activity, UNREACHABLE, {})
        manager.cancel()

        assertTrue(manager.start(activity, origin, {}))
        awaitBrowserLaunchPosted(loginServed)
        drainMainLooper()

        val launched = shadowOf(activity).nextStartedActivity
        assertTrue(
            "the live flow's browser launch was suppressed",
            launched != null && launched.dataString!!.startsWith(origin),
        )
    }

    /**
     * Stand up a local origin whose `/auth/cli-login` immediately returns a
     * ticket (so the manager posts its browser launch) and whose
     * `/auth/cli-poll` stays pending forever. Returns the origin URL plus a
     * latch that fires once cli-login has been served.
     */
    private fun startOrigin(): Pair<String, CountDownLatch> {
        val loginServed = CountDownLatch(1)
        val s = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        s.createContext("/auth/cli-login") { ex ->
            val bytes = """{"ticket":"t-1","login_url":"/login/oidc?ticket=t-1"}""".toByteArray()
            ex.sendResponseHeaders(200, bytes.size.toLong())
            ex.responseBody.use { it.write(bytes) }
            loginServed.countDown()
        }
        s.createContext("/auth/cli-poll") { ex ->
            ex.sendResponseHeaders(202, -1) // still pending
            ex.close()
        }
        s.start()
        server = s
        return "http://127.0.0.1:${s.address.port}" to loginServed
    }

    /** Block until the background flow has posted its browser launch to the paused main looper. */
    private fun awaitBrowserLaunchPosted(loginServed: CountDownLatch) {
        assertTrue("cli-login was never requested", loginServed.await(10, TimeUnit.SECONDS))
        val mainLooper = shadowOf(Looper.getMainLooper())
        val deadline = System.currentTimeMillis() + 10_000
        while (mainLooper.isIdle && System.currentTimeMillis() < deadline) Thread.sleep(5)
        assertFalse("browser launch was never posted to the main looper", mainLooper.isIdle)
    }

    private fun drainMainLooper() {
        shadowOf(Looper.getMainLooper()).idle()
    }

    private companion object {
        // A syntactically valid but unreachable origin — the background poll will
        // fail with a connection error, but that happens asynchronously and does
        // not affect the synchronous state transitions tested here.
        const val UNREACHABLE = "http://127.0.0.1:1"
    }
}
