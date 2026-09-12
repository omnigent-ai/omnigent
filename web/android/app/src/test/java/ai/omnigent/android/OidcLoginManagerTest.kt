package ai.omnigent.android

import android.app.Activity
import android.os.Looper
import com.sun.net.httpserver.HttpServer
import org.junit.After
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
import java.net.HttpURLConnection
import java.net.InetSocketAddress
import java.net.URL
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

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

    // The stale-launch tests' local origin, torn down after each test.
    private var server: HttpServer? = null

    @After
    fun tearDown() {
        server?.stop(0)
        server = null
    }

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
        manager.requestTicket = { _, _ -> OidcLoginManager.Ticket("ticket-1", "/auth/login?t=1") }
        manager.pollForToken = { _, _, _ -> "session-jwt" }

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
        manager.requestTicket = { _, _ -> OidcLoginManager.Ticket("ticket-1", "/auth/login?t=1") }
        manager.pollForToken = { _, _, _ ->
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
    fun `a cancelled flow does not launch the browser for a ticket that lands after cancel`() {
        val activity = activity()
        val manager = OidcLoginManager()
        val polling = CountDownLatch(1)
        val cancelled = CountDownLatch(1)
        // The launchTab post is queued (paused main looper) right before the
        // poll starts; cancelling while the poll holds must drop that launch.
        manager.requestTicket = { _, _ -> OidcLoginManager.Ticket("ticket-1", "/auth/login?t=1") }
        manager.pollForToken = { _, _, _ ->
            polling.countDown()
            runCatching { cancelled.await(2, TimeUnit.SECONDS) }
            null
        }

        assertTrue(manager.start(activity, origin) { _, _ -> })
        assertTrue(polling.await(2, TimeUnit.SECONDS))
        manager.cancel()
        cancelled.countDown()
        drainMain()

        assertNull(shadowOf(activity).nextStartedActivity)
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
    // These drive the real HTTP seams against a local server.

    @Test
    fun `cancel before the looper drains suppresses the posted browser launch`() {
        val (origin, loginServed) = startOrigin()
        val activity = activity()
        val manager = OidcLoginManager()
        realClock(manager)
        drainMainLooper() // flush activity-setup posts so the queue is ours

        assertTrue(manager.start(activity, origin) { _, _ -> })
        awaitBrowserLaunchPosted(loginServed)

        manager.cancel() // user cancelled, or MainActivity switched to another server
        drainMainLooper()

        assertNull(
            "obsolete browser URL launched after cancel()",
            shadowOf(activity).nextStartedActivity,
        )
        manager.shutdown()
    }

    @Test
    fun `server switch before the looper drains suppresses the posted browser launch`() {
        val (origin, loginServed) = startOrigin()
        val activity = activity()
        val manager = OidcLoginManager()
        realClock(manager)
        drainMainLooper()

        assertTrue(manager.start(activity, origin) { _, _ -> })
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
        assertTrue(manager.start(activity, deadOrigin) { _, _ -> })
        manager.shutdown()
    }

    @Test
    fun `activity destruction before the looper drains suppresses the posted browser launch`() {
        val (origin, loginServed) = startOrigin()
        val controller = Robolectric.buildActivity(Activity::class.java).setup()
        val activity = controller.get()
        val manager = OidcLoginManager()
        realClock(manager)
        drainMainLooper()

        assertTrue(manager.start(activity, origin) { _, _ -> })
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
        val activity = activity()
        val manager = OidcLoginManager()
        realClock(manager)
        drainMainLooper()

        // A cancelled earlier flow must not suppress a later, live one.
        assertTrue(manager.start(activity, deadOrigin) { _, _ -> })
        manager.cancel()

        assertTrue(manager.start(activity, origin) { _, _ -> })
        assertTrue("cli-login was never requested", loginServed.await(10, TimeUnit.SECONDS))
        // The dead flow's failure post shares the queue with the live launch,
        // so a non-idle looper alone doesn't prove the launch is queued — pump
        // until it has actually run.
        drainMain { shadowOf(activity).peekNextStartedActivity() != null }

        val launched = shadowOf(activity).nextStartedActivity
        assertTrue(
            "the live flow's browser launch was suppressed",
            launched != null && launched.dataString!!.startsWith(origin),
        )
        manager.shutdown()
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

    // Real monotonic clock for the HTTP-seam tests: Robolectric's SystemClock
    // is simulated and does not advance with the real sleeps inside the
    // ticket/poll loops, so drive deadlines from the JVM's own monotonic time.
    private fun realClock(manager: OidcLoginManager): () -> Long {
        val clock = { System.nanoTime() / 1_000_000 }
        manager.monotonicNowMs = clock
        return clock
    }

    // A one-shot local server: transient failures for the first [failures]
    // requests to a path, then the real answer — drives the default (HTTP)
    // requestTicket/pollForToken seams end to end on the JVM.
    private fun transientThenOk(
        failures: Int,
        status: Int,
        okBody: String,
    ): Pair<HttpServer, AtomicInteger> {
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        val hits = AtomicInteger(0)
        server.createContext("/") { exchange ->
            if (hits.incrementAndGet() <= failures) {
                exchange.sendResponseHeaders(status, -1)
                exchange.close()
            } else {
                val bytes = okBody.toByteArray()
                exchange.sendResponseHeaders(200, bytes.size.toLong())
                exchange.responseBody.use { it.write(bytes) }
            }
        }
        server.start()
        return server to hits
    }

    @Test
    fun `ticket creation retries a transient 503 and completes on the next 200`() {
        val (server, hits) =
            transientThenOk(1, 503, """{"ticket":"t-1","login_url":"/auth/login?ticket=t-1"}""")
        try {
            val manager = OidcLoginManager()
            val clock = realClock(manager)
            val origin = "http://127.0.0.1:${server.address.port}"
            val ticket = manager.requestTicket(origin, clock() + 30_000)
            assertEquals(OidcLoginManager.Ticket("t-1", "/auth/login?ticket=t-1"), ticket)
            assertEquals(2, hits.get())
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `ticket creation still fails fast on a non-transient error`() {
        val (server, hits) = transientThenOk(1, 401, """{"ticket":"t","login_url":"/l"}""")
        try {
            val manager = OidcLoginManager()
            val clock = realClock(manager)
            val origin = "http://127.0.0.1:${server.address.port}"
            assertNull(manager.requestTicket(origin, clock() + 30_000))
            assertEquals(1, hits.get())
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `a ticket arriving past the deadline is rejected`() {
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        val hits = AtomicInteger(0)
        server.createContext("/") { exchange ->
            hits.incrementAndGet()
            Thread.sleep(500)
            val bytes =
                """{"ticket":"t-1","login_url":"/auth/login?ticket=t-1"}""".toByteArray()
            exchange.sendResponseHeaders(200, bytes.size.toLong())
            exchange.responseBody.use { it.write(bytes) }
        }
        server.start()
        try {
            val manager = OidcLoginManager()
            val clock = realClock(manager)
            val origin = "http://127.0.0.1:${server.address.port}"
            assertNull(manager.requestTicket(origin, clock() + 300))
            assertEquals(1, hits.get())
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `polling rides out a transient 503 and returns the token from the next 200`() {
        val (server, hits) = transientThenOk(1, 503, """{"token":"session-jwt"}""")
        try {
            val manager = OidcLoginManager()
            val clock = realClock(manager)
            val origin = "http://127.0.0.1:${server.address.port}"
            val token = manager.pollForToken(origin, "t-1", clock() + 30_000)
            assertEquals("session-jwt", token)
            assertEquals(2, hits.get())
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `polling never issues a request once the sleep crosses the deadline`() {
        val (server, hits) = transientThenOk(0, 200, """{"token":"session-jwt"}""")
        try {
            val manager = OidcLoginManager()
            val clock = realClock(manager)
            val origin = "http://127.0.0.1:${server.address.port}"
            // The 2s inter-poll sleep crosses this deadline before the first
            // request — the loop must exit without touching the server.
            assertNull(manager.pollForToken(origin, "t-1", clock() + 1_000))
            assertEquals(0, hits.get())
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `a token arriving past the deadline is rejected`() {
        // The request is issued in time, but the server's delayed 200 lands
        // the token past the deadline — expired-window output must be dropped.
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        val hits = AtomicInteger(0)
        server.createContext("/") { exchange ->
            hits.incrementAndGet()
            Thread.sleep(500)
            val bytes = """{"token":"session-jwt"}""".toByteArray()
            exchange.sendResponseHeaders(200, bytes.size.toLong())
            exchange.responseBody.use { it.write(bytes) }
        }
        server.start()
        try {
            val manager = OidcLoginManager()
            val clock = realClock(manager)
            val origin = "http://127.0.0.1:${server.address.port}"
            // 2s sleep + request dispatch fit; the 500ms response delay does not.
            assertNull(manager.pollForToken(origin, "t-1", clock() + 2_300))
            assertEquals(1, hits.get())
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `an abandoned request's cleanup does not break the successor's cancel`() {
        // Regression: the shared connection slot was cleared unconditionally in
        // finally, so an old flow's cleanup running after a cancel-then-restart
        // wiped the NEW flow's connection and the next cancel() had nothing to
        // disconnect.
        val firstArrived = CountDownLatch(1)
        val releaseFirst = CountDownLatch(1)
        val secondArrived = CountDownLatch(1)
        val holdSecond = CountDownLatch(1)
        val hits = AtomicInteger(0)
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        // Two requests are held concurrently — the default dispatcher thread
        // would serialize (and deadlock) them.
        server.executor = Executors.newCachedThreadPool()
        server.createContext("/") { exchange ->
            if (hits.incrementAndGet() == 1) {
                firstArrived.countDown()
                runCatching { releaseFirst.await(8, TimeUnit.SECONDS) }
                val bytes =
                    """{"ticket":"t-1","login_url":"/auth/login?ticket=t-1"}"""
                        .toByteArray()
                exchange.sendResponseHeaders(200, bytes.size.toLong())
                exchange.responseBody.use { it.write(bytes) }
            } else {
                secondArrived.countDown()
                runCatching { holdSecond.await(8, TimeUnit.SECONDS) }
                exchange.sendResponseHeaders(200, -1)
                exchange.close()
            }
        }
        server.start()
        try {
            val manager = OidcLoginManager()
            val clock = realClock(manager)
            val origin = "http://127.0.0.1:${server.address.port}"
            // An old request is in flight (its connection holds the slot)...
            val first = Thread { runCatching { manager.requestTicket(origin, clock() + 30_000) } }
            first.start()
            assertTrue(firstArrived.await(2, TimeUnit.SECONDS))
            // ...then a successor request publishes ITS connection...
            var second: OidcLoginManager.Ticket? = OidcLoginManager.Ticket("sentinel", "/x")
            val successor =
                Thread {
                    second =
                        runCatching {
                            manager.requestTicket(origin, clock() + 30_000)
                        }.getOrNull()
                }
            successor.start()
            assertTrue(secondArrived.await(2, TimeUnit.SECONDS))
            // ...and the old request completes, running its cleanup.
            releaseFirst.countDown()
            first.join(3_000)
            assertFalse(first.isAlive)
            // cancel() must still find and abort the successor's blocked I/O.
            manager.cancel()
            successor.join(3_000)
            assertFalse(successor.isAlive)
            assertNull(second)
        } finally {
            releaseFirst.countDown()
            holdSecond.countDown()
            server.stop(0)
        }
    }

    @Test
    fun `a cancelled generation cannot overwrite the successor connection`() {
        class TrackingConnection : HttpURLConnection(URL("http://127.0.0.1")) {
            var disconnected = false

            override fun connect() = Unit

            override fun disconnect() {
                disconnected = true
            }

            override fun usingProxy(): Boolean = false
        }

        val manager = OidcLoginManager()
        val abandonedGeneration = manager.currentConnectionGeneration()
        manager.cancel()
        val successorGeneration = manager.currentConnectionGeneration()
        val successor = TrackingConnection()
        val abandoned = TrackingConnection()

        assertTrue(manager.publishConnection(successor, successorGeneration))
        assertFalse(manager.publishConnection(abandoned, abandonedGeneration))
        assertTrue(abandoned.disconnected)
        assertFalse(successor.disconnected)

        manager.cancel()
        assertTrue(successor.disconnected)
    }

    @Test
    fun `cancel disconnects an in-flight request so a blocked thread exits promptly`() {
        val requestArrived = CountDownLatch(1)
        val hold = CountDownLatch(1)
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        server.createContext("/") { exchange ->
            requestArrived.countDown()
            // Hold the response longer than the join below — only cancel()'s
            // disconnect (not shutdownNow's interrupt) can unblock the read.
            runCatching { hold.await(8, TimeUnit.SECONDS) }
            exchange.sendResponseHeaders(200, -1)
            exchange.close()
        }
        server.start()
        try {
            val manager = OidcLoginManager()
            val clock = realClock(manager)
            val origin = "http://127.0.0.1:${server.address.port}"
            var result: OidcLoginManager.Ticket? = OidcLoginManager.Ticket("sentinel", "/x")
            val worker =
                Thread {
                    result =
                        runCatching { manager.requestTicket(origin, clock() + 30_000) }.getOrNull()
                }
            worker.start()
            assertTrue(requestArrived.await(2, TimeUnit.SECONDS))
            manager.cancel()
            worker.join(3_000)
            assertFalse(worker.isAlive)
            assertNull(result)
        } finally {
            hold.countDown()
            server.stop(0)
        }
    }

    @Test
    fun `polling stays fatal on 410 expired`() {
        val (server, hits) = transientThenOk(1, 410, """{"token":"session-jwt"}""")
        try {
            val manager = OidcLoginManager()
            val clock = realClock(manager)
            val origin = "http://127.0.0.1:${server.address.port}"
            assertNull(manager.pollForToken(origin, "t-1", clock() + 30_000))
            assertEquals(1, hits.get())
        } finally {
            server.stop(0)
        }
    }
}
