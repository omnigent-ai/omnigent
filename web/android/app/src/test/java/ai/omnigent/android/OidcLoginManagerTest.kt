package ai.omnigent.android

import android.app.Activity
import android.os.Looper
import android.webkit.CookieManager
import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowCookieManager
import org.robolectric.shadows.ShadowLog
import org.robolectric.shadows.ShadowLooper
import java.net.InetSocketAddress
import java.nio.charset.StandardCharsets
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class OidcLoginManagerTest {
    private lateinit var server: HttpServer
    private lateinit var origin: String
    private val managers = mutableListOf<OidcLoginManager>()

    @Before
    fun setUp() {
        ShadowCookieManager.resetCookies()
        server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        origin = "http://127.0.0.1:${server.address.port}"
        server.start()
    }

    @After
    fun tearDown() {
        managers.forEach(OidcLoginManager::shutdown)
        server.stop(0)
        ShadowCookieManager.resetCookies()
    }

    @Test
    fun `cli-login redirect is rejected without requesting its target`() {
        val redirectHits = AtomicInteger()
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(302, location = "$origin/redirect-target")
        }
        server.createContext("/redirect-target") { exchange ->
            redirectHits.incrementAndGet()
            exchange.respond(200, ticketBody())
        }

        val result = runLogin(manager())

        assertEquals(LoginResult.Rejected, result)
        assertEquals(0, redirectHits.get())
    }

    @Test
    fun `cli-poll redirect is rejected without requesting valid token target`() {
        val redirectHits = AtomicInteger()
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(200, ticketBody())
        }
        server.createContext("/auth/cli-poll") { exchange ->
            exchange.respond(302, location = "$origin/redirect-target")
        }
        server.createContext("/redirect-target") { exchange ->
            redirectHits.incrementAndGet()
            exchange.respond(200, tokenBody())
        }

        val result = runLogin(manager())

        assertEquals(LoginResult.Rejected, result)
        assertEquals(0, redirectHits.get())
    }

    @Test
    fun `WebView cookies are attached to login and poll endpoints`() {
        val handlerFailure = AtomicReference<Throwable?>()
        val expectedCookie = "front_door=session"
        CookieManager.getInstance().setCookie(origin, expectedCookie)
        server.createContext("/auth/cli-login") { exchange ->
            exchange.assertCookie(expectedCookie, handlerFailure)
            exchange.respond(200, ticketBody())
        }
        server.createContext("/auth/cli-poll") { exchange ->
            exchange.assertCookie(expectedCookie, handlerFailure)
            exchange.respond(200, tokenBody())
        }

        val result = runLogin(manager())

        assertEquals(LoginResult.Success(TOKEN), result)
        handlerFailure.get()?.let { throw it }
    }

    @Test
    @Config(sdk = [35], shadows = [RecordingCookieManagerShadow::class])
    fun `cookies are queried per endpoint URL so path-scoped cookies match`() {
        RecordingCookieManagerShadow.reset()
        val handlerFailure = AtomicReference<Throwable?>()
        val expectedCookie = "front_door=session"
        // A proxy session cookie scoped Path=/auth exists only for /auth URLs,
        // never for the bare origin the old lookup used.
        RecordingCookieManagerShadow.cookieForUrl = { url ->
            if (android.net.Uri
                    .parse(url)
                    .path
                    .orEmpty()
                    .startsWith("/auth")
            ) {
                expectedCookie
            } else {
                null
            }
        }
        server.createContext("/auth/cli-login") { exchange ->
            exchange.assertCookie(expectedCookie, handlerFailure)
            exchange.respond(200, ticketBody())
        }
        server.createContext("/auth/cli-poll") { exchange ->
            exchange.assertCookie(expectedCookie, handlerFailure)
            exchange.respond(200, tokenBody())
        }

        val result =
            try {
                runLogin(manager())
            } finally {
                RecordingCookieManagerShadow.reset()
            }

        assertEquals(LoginResult.Success(TOKEN), result)
        handlerFailure.get()?.let { throw it }
    }

    @Test
    fun `absolute and scheme-relative login urls are rejected`() {
        val loginUrls =
            ConcurrentLinkedQueue(
                listOf(
                    "https://other.example/browser-login",
                    "//other.example/browser-login",
                ),
            )
        val pollHits = AtomicInteger()
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(200, ticketBody(checkNotNull(loginUrls.poll())))
        }
        server.createContext("/auth/cli-poll") { exchange ->
            pollHits.incrementAndGet()
            exchange.respond(200, tokenBody())
        }

        repeat(2) {
            assertEquals(LoginResult.Rejected, runLogin(manager()))
        }
        assertEquals(0, pollHits.get())
    }

    @Test
    fun `deadline expiry is timed out rather than rejected`() {
        val pollHits = AtomicInteger()
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(200, ticketBody())
        }
        server.createContext("/auth/cli-poll") { exchange ->
            pollHits.incrementAndGet()
            exchange.respond(202)
        }

        val result = runLogin(manager(pollTimeoutMs = 250))

        assertEquals(LoginResult.TimedOut, result)
        assertTrue(pollHits.get() >= 2)
    }

    @Test
    fun `poll deadline uses the injected monotonic clock`() {
        val clockReads = AtomicInteger()
        val pollHits = AtomicInteger()
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(200, ticketBody())
        }
        server.createContext("/auth/cli-poll") { exchange ->
            pollHits.incrementAndGet()
            exchange.respond(202)
        }

        val result =
            runLogin(
                manager(
                    pollTimeoutMs = 1_000,
                    clock = {
                        if (clockReads.incrementAndGet() < 3) 10_000L else 11_000L
                    },
                ),
            )

        assertEquals(LoginResult.TimedOut, result)
        assertTrue(clockReads.get() >= 3)
        assertEquals(0, pollHits.get())
    }

    @Test
    fun `malformed poll payload is rejected without another poll`() {
        val pollHits = AtomicInteger()
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(200, ticketBody())
        }
        server.createContext("/auth/cli-poll") { exchange ->
            pollHits.incrementAndGet()
            exchange.respond(200, "not-json")
        }

        val result = runLogin(manager(pollTimeoutMs = 1_000))

        assertEquals(LoginResult.Rejected, result)
        assertEquals(1, pollHits.get())
    }

    @Test
    fun `a delivered result is not replayed to a later start`() {
        val pollHits = AtomicInteger()
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(200, ticketBody())
        }
        server.createContext("/auth/cli-poll") { exchange ->
            if (pollHits.incrementAndGet() == 1) {
                exchange.respond(200, tokenBody())
            } else {
                exchange.respond(202)
            }
        }
        val manager = manager(pollTimeoutMs = 30_000)
        val results = mutableListOf<LoginResult>()
        manager.attach(origin) { _, result -> results.add(result) }

        assertTrue(manager.start(activity(), origin))
        awaitFlowCompletion(manager)
        assertEquals(listOf<LoginResult>(LoginResult.Success(TOKEN)), results)

        assertTrue(manager.start(activity(), origin))
        ShadowLooper.idleMainLooper()

        // The second flow is still polling — the first result must not replay.
        assertEquals(1, results.size)
        manager.cancel()
    }

    @Test
    fun `a queued start cannot open a second flow before the first delivers`() {
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(200, ticketBody())
        }
        server.createContext("/auth/cli-poll") { exchange ->
            exchange.respond(200, tokenBody())
        }
        val manager = manager()
        val activity = activity() // built up front — setup() drains the main looper
        val results = mutableListOf<LoginResult>()
        manager.attach(origin) { _, result -> results.add(result) }

        assertTrue(manager.start(activity, origin))
        // The window where the flow has finished off-thread but its result has
        // not yet crossed to the main thread — a second start used to slip in
        // here and open a duplicate browser flow.
        awaitOffMainThread { manager.isAwaitingDeliveryForTest() }
        assertFalse(manager.start(activity, origin))

        ShadowLooper.idleMainLooper()
        assertEquals(listOf<LoginResult>(LoginResult.Success(TOKEN)), results)
        // Once the result is delivered the slot is free again.
        assertTrue(manager.start(activity, origin))
        manager.cancel()
    }

    @Test
    fun `result completed while detached is delivered to a matching attachment`() {
        val pollEntered = CountDownLatch(1)
        val releasePoll = CountDownLatch(1)
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(200, ticketBody())
        }
        server.createContext("/auth/cli-poll") { exchange ->
            pollEntered.countDown()
            releasePoll.await(5, TimeUnit.SECONDS)
            exchange.respond(200, tokenBody())
        }
        val manager = manager()
        val detachedCalls = AtomicInteger()
        val result = AtomicReference<LoginResult?>()
        val firstAttachment =
            manager.attach(origin) { _, _ ->
                detachedCalls.incrementAndGet()
            }

        assertTrue(manager.start(activity(), origin))
        assertTrue(pollEntered.await(5, TimeUnit.SECONDS))
        manager.detach(firstAttachment)
        releasePoll.countDown()
        awaitFlowCompletion(manager)
        ShadowLooper.idleMainLooper()

        assertEquals(0, detachedCalls.get())
        assertTrue(manager.hasHeldResultForTest())

        manager.attach(origin) { deliveredOrigin, deliveredResult ->
            assertEquals(origin, deliveredOrigin)
            result.set(deliveredResult)
        }

        assertEquals(LoginResult.Success(TOKEN), result.get())
        assertEquals(0, detachedCalls.get())
        assertFalse(manager.hasHeldResultForTest())
    }

    @Test
    fun `rejected result completed while detached is dropped`() {
        val requestEntered = CountDownLatch(1)
        val releaseRequest = CountDownLatch(1)
        server.createContext("/auth/cli-login") { exchange ->
            requestEntered.countDown()
            releaseRequest.await(5, TimeUnit.SECONDS)
            exchange.respond(500)
        }
        val manager = manager()
        val detachedCalls = AtomicInteger()
        val firstAttachment =
            manager.attach(origin) { _, _ ->
                detachedCalls.incrementAndGet()
            }

        assertTrue(manager.start(activity(), origin))
        assertTrue(requestEntered.await(5, TimeUnit.SECONDS))
        manager.detach(firstAttachment)
        releaseRequest.countDown()
        awaitFlowCompletion(manager)
        awaitMainThread {
            ShadowLog
                .getLogsForTag("OmnigentAuth")
                .any { item -> item.msg.contains("dropping unattached login failure") }
        }

        assertEquals(0, detachedCalls.get())
        assertFalse(manager.hasHeldResultForTest())
        assertTrue(
            ShadowLog
                .getLogsForTag("OmnigentAuth")
                .any { item -> item.msg.contains("dropping unattached login failure") },
        )
        val laterCalls = AtomicInteger()
        manager.attach(origin) { _, _ -> laterCalls.incrementAndGet() }
        assertEquals(0, laterCalls.get())
    }

    @Test
    fun `held result is discarded when a different origin attaches`() {
        val pollEntered = CountDownLatch(1)
        val releasePoll = CountDownLatch(1)
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(200, ticketBody())
        }
        server.createContext("/auth/cli-poll") { exchange ->
            pollEntered.countDown()
            releasePoll.await(5, TimeUnit.SECONDS)
            exchange.respond(200, tokenBody())
        }
        val manager = manager()
        val firstAttachment = manager.attach(origin) { _, _ -> }

        assertTrue(manager.start(activity(), origin))
        assertTrue(pollEntered.await(5, TimeUnit.SECONDS))
        manager.detach(firstAttachment)
        releasePoll.countDown()
        awaitFlowCompletion(manager)
        ShadowLooper.idleMainLooper()

        assertTrue(manager.hasHeldResultForTest())
        val staleOriginCalls = AtomicInteger()
        val originalOriginCalls = AtomicInteger()
        manager.attach("https://other.example") { _, _ -> staleOriginCalls.incrementAndGet() }
        manager.attach(origin) { _, _ -> originalOriginCalls.incrementAndGet() }

        assertEquals(0, staleOriginCalls.get())
        assertEquals(0, originalOriginCalls.get())
        assertFalse(manager.hasHeldResultForTest())
    }

    @Test
    fun `a different origin attachment abandons the active flow and permits a new start`() {
        val requestEntered = CountDownLatch(1)
        val releaseRequest = CountDownLatch(1)
        server.createContext("/auth/cli-login") { exchange ->
            requestEntered.countDown()
            releaseRequest.await(5, TimeUnit.SECONDS)
            exchange.respond(500)
        }
        val manager = manager(pollTimeoutMs = 30_000)
        val firstAttachment = manager.attach(origin) { _, _ -> }

        try {
            assertTrue(manager.start(activity(), origin))
            assertTrue(requestEntered.await(5, TimeUnit.SECONDS))
            manager.detach(firstAttachment)

            manager.attach("http://localhost:${server.address.port}") { _, _ -> }

            assertFalse(manager.isInFlightForTest())
            assertTrue(manager.start(activity(), "http://localhost:${server.address.port}"))
        } finally {
            manager.cancel()
            releaseRequest.countDown()
        }
    }

    @Test
    fun `a matching origin attachment leaves the active flow running`() {
        val requestEntered = CountDownLatch(1)
        val releaseRequest = CountDownLatch(1)
        server.createContext("/auth/cli-login") { exchange ->
            requestEntered.countDown()
            releaseRequest.await(5, TimeUnit.SECONDS)
            exchange.respond(500)
        }
        val manager = manager(pollTimeoutMs = 30_000)
        val firstAttachment = manager.attach(origin) { _, _ -> }

        try {
            assertTrue(manager.start(activity(), origin))
            assertTrue(requestEntered.await(5, TimeUnit.SECONDS))
            manager.detach(firstAttachment)

            manager.attach(origin) { _, _ -> }

            assertTrue(manager.isInFlightForTest())
            assertFalse(manager.start(activity(), origin))
        } finally {
            manager.cancel()
            releaseRequest.countDown()
        }
    }

    @Test
    fun `cancel discards a result held while detached`() {
        val pollEntered = CountDownLatch(1)
        val releasePoll = CountDownLatch(1)
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(200, ticketBody())
        }
        server.createContext("/auth/cli-poll") { exchange ->
            pollEntered.countDown()
            releasePoll.await(5, TimeUnit.SECONDS)
            exchange.respond(200, tokenBody())
        }
        val manager = manager()
        val firstAttachment = manager.attach(origin) { _, _ -> }

        assertTrue(manager.start(activity(), origin))
        assertTrue(pollEntered.await(5, TimeUnit.SECONDS))
        manager.detach(firstAttachment)
        releasePoll.countDown()
        awaitFlowCompletion(manager)
        ShadowLooper.idleMainLooper()

        assertTrue(manager.hasHeldResultForTest())
        manager.cancel()
        assertFalse(manager.hasHeldResultForTest())
        val delivered = AtomicInteger()
        manager.attach(origin) { _, _ -> delivered.incrementAndGet() }

        assertEquals(0, delivered.get())
    }

    @Test
    fun `cancel suppresses a result that completed before it`() {
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(200, ticketBody())
        }
        server.createContext("/auth/cli-poll") { exchange ->
            exchange.respond(200, tokenBody())
        }
        val manager = manager()
        val delivered = AtomicInteger()
        manager.attach(origin) { _, _ -> delivered.incrementAndGet() }

        // The flow finishes and posts its result; the cancel lands before the
        // main looper drains it, so the abandoned callback must never run.
        assertTrue(manager.start(activity(), origin))
        awaitOffMainThread { manager.isAwaitingDeliveryForTest() }

        manager.cancel()
        ShadowLooper.idleMainLooper()

        assertEquals(0, delivered.get())
    }

    @Test
    fun `cancel permits a new start and suppresses the abandoned callback`() {
        val loginAttempts = AtomicInteger()
        val firstPollCompleted = CountDownLatch(1)
        server.createContext("/auth/cli-login") { exchange ->
            val attempt = loginAttempts.incrementAndGet()
            exchange.respond(200, ticketBody(ticket = "ticket-$attempt"))
        }
        server.createContext("/auth/cli-poll") { exchange ->
            if (exchange.requestURI.query == "ticket=ticket-1") {
                exchange.respond(202)
                firstPollCompleted.countDown()
            } else {
                exchange.respond(200, tokenBody())
            }
        }
        val manager = manager(pollTimeoutMs = 30_000)
        val results = mutableListOf<LoginResult>()
        val delivered = CountDownLatch(1)
        manager.attach(origin) { _, result ->
            results.add(result)
            delivered.countDown()
        }

        assertTrue(manager.start(activity(), origin))
        assertTrue(firstPollCompleted.await(5, TimeUnit.SECONDS))

        manager.cancel()

        assertFalse(manager.isInFlightForTest())
        assertTrue(manager.start(activity(), origin))
        awaitCallback(delivered)

        // Only the second flow's token arrives; the abandoned flow stays silent.
        assertEquals(listOf<LoginResult>(LoginResult.Success(TOKEN)), results)
    }

    @Test
    fun `start after shutdown does not throw or leave inFlight set`() {
        val manager = manager()
        manager.attach(origin) { _, _ ->
            throw AssertionError("shutdown manager delivered a callback")
        }
        manager.shutdown()

        val started = manager.start(activity(), origin)

        assertFalse(started)
        assertFalse(manager.isInFlightForTest())
        ShadowLooper.idleMainLooper()
    }

    @Test
    fun `results are delivered on the main thread`() {
        val deliveredOnMain = AtomicBoolean(false)
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(200, ticketBody())
        }
        server.createContext("/auth/cli-poll") { exchange ->
            exchange.respond(200, tokenBody())
        }

        val result =
            runLogin(manager()) {
                deliveredOnMain.set(Looper.myLooper() == Looper.getMainLooper())
            }

        assertEquals(LoginResult.Success(TOKEN), result)
        assertTrue(deliveredOnMain.get())
    }

    private fun manager(
        pollIntervalMs: Long = 1,
        pollTimeoutMs: Long = 1_000,
        clock: () -> Long = { System.nanoTime() / 1_000_000L },
    ): OidcLoginManager =
        OidcLoginManager(
            pollIntervalMs = pollIntervalMs,
            pollTimeoutMs = pollTimeoutMs,
            clock = clock,
        ).also(managers::add)

    private fun runLogin(
        manager: OidcLoginManager,
        onResult: (LoginResult) -> Unit = {},
    ): LoginResult {
        val result = AtomicReference<LoginResult?>()
        val delivered = CountDownLatch(1)
        manager.attach(origin) { _, loginResult ->
            result.set(loginResult)
            onResult(loginResult)
            delivered.countDown()
        }
        assertTrue(manager.start(activity(), origin))
        awaitCallback(delivered)
        return checkNotNull(result.get())
    }

    private fun awaitCallback(delivered: CountDownLatch) {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5)
        while (delivered.count > 0 && System.nanoTime() < deadline) {
            ShadowLooper.idleMainLooper()
            Thread.yield()
        }
        assertTrue("login callback was not delivered", delivered.await(0, TimeUnit.MILLISECONDS))
    }

    /** Idle until the worker's posted outcome is observable on the main thread. */
    private fun awaitMainThread(condition: () -> Boolean) {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5)
        while (!condition() && System.nanoTime() < deadline) {
            ShadowLooper.idleMainLooper()
            Thread.yield()
        }
        ShadowLooper.idleMainLooper()
    }

    /** Busy-wait for [condition] WITHOUT idling the main looper. */
    private fun awaitOffMainThread(condition: () -> Boolean) {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5)
        while (!condition() && System.nanoTime() < deadline) {
            Thread.yield()
        }
        assertTrue("condition was not reached", condition())
    }

    /** A finished flow vacates its slot on the main thread, so completion needs a drain. */
    private fun awaitFlowCompletion(manager: OidcLoginManager) {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5)
        while (manager.isInFlightForTest() && System.nanoTime() < deadline) {
            ShadowLooper.idleMainLooper()
            Thread.yield()
        }
        assertFalse("login flow did not complete", manager.isInFlightForTest())
    }

    private fun activity(): Activity =
        Robolectric
            .buildActivity(Activity::class.java)
            .setup()
            .get()

    private fun ticketBody(
        loginUrl: String = "/browser-login",
        ticket: String = "ticket-1",
    ): String = """{"ticket":"$ticket","login_url":"$loginUrl"}"""

    private fun tokenBody(): String = """{"token":"$TOKEN"}"""

    private fun HttpExchange.assertCookie(
        expected: String,
        failure: AtomicReference<Throwable?>,
    ) {
        runCatching {
            assertEquals(expected, requestHeaders.getFirst("Cookie"))
        }.exceptionOrNull()?.let { failure.compareAndSet(null, it) }
    }

    private fun HttpExchange.respond(
        status: Int,
        body: String = "",
        location: String? = null,
    ) {
        if (location != null) responseHeaders.add("Location", location)
        val bytes = body.toByteArray(StandardCharsets.UTF_8)
        sendResponseHeaders(status, bytes.size.toLong())
        responseBody.use { it.write(bytes) }
        close()
    }

    private companion object {
        const val TOKEN = "header.payload.signature"
    }
}
