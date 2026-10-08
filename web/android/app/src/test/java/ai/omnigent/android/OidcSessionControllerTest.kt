package ai.omnigent.android

import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.net.URI
import java.util.UUID
import java.util.concurrent.Executor

@RunWith(RobolectricTestRunner::class)
class OidcSessionControllerTest {
    private val context = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val server = FakeServer()
    private val store = MemoryStore()
    private val jar = FakeJar()
    private val host = FakeHost()
    private var now = 100_000L
    private var manifest = nativeManifest("ap_session")
    private val direct = Executor { it.run() }
    private val credentials =
        OidcCredentials(
            store = store,
            pending =
                OidcPendingSignInStore(
                    context,
                    EncryptedRecordStore(context, "test-oidc-${UUID.randomUUID()}", PlainCipher),
                ) { now },
            transport = server,
            verifyTransport = server,
            executor = direct,
            now = { now },
        )
    private val controller =
        OidcSessionController(host, credentials, jar, io = direct, main = direct, now = {
            now
        }) { manifest }

    init {
        host.credentials = credentials
    }

    @Test
    fun `a server without native sign-in loads as before`() {
        manifest = ServerManifest.BASELINE

        controller.connect(SERVER, interactive = true)

        assertEquals(listOf("progress:CONNECTING", "legacy:$SERVER"), host.events)
        assertTrue(server.requests.isEmpty())
        assertFalse(controller.handlesNavigation("$SERVER/auth/login"))
    }

    @Test
    fun `an accepted session cookie is reused without renewing`() {
        jar.values["ap_session"] = "still-good"
        server.accepted += "still-good"

        controller.connect(SERVER, interactive = false)

        assertEquals(listOf("progress:CONNECTING", "load:$SERVER"), host.events)
        assertEquals(listOf("GET /omnigent/v1/me"), server.requests)
    }

    @Test
    fun `a rejected cookie is renewed from the stored grant before the page loads`() {
        jar.values["ap_session"] = "stale"
        store.grants[ORIGIN] = OidcRefreshGrant("grant-1", null)
        server.refresh = 200 to """{"access_token":"fresh","expires_in":3600}"""
        server.accepted += "fresh"

        controller.connect(SERVER, interactive = false)

        assertEquals(
            listOf("progress:CONNECTING", "progress:SIGNING_IN", "load:$SERVER"),
            host.events,
        )
        assertEquals("fresh", jar.values["ap_session"])
        assertEquals(ORIGIN, jar.writes.single().first)
        assertTrue(
            jar.writes.single().second.startsWith(
                "ap_session=fresh; Path=/; HttpOnly; SameSite=Lax; Secure; Expires=",
            ),
        )
        assertEquals(
            listOf("GET /omnigent/v1/me", "POST /omnigent/oauth/token", "GET /omnigent/v1/me"),
            server.requests,
        )
    }

    @Test
    fun `an explicit connect with nothing to renew signs in through the browser`() {
        server.exchange =
            200 to """{"token":"signed-in","expires_in":28800,"refresh_token":"grant-2"}"""
        server.accepted += "signed-in"

        controller.connect(SERVER, interactive = true)
        assertEquals(1, host.launches.size)
        controller.onBrowserCallback(host.callback("code=c"))

        assertEquals(
            listOf(
                "progress:CONNECTING",
                "progress:SIGNING_IN",
                "progress:SIGNING_IN",
                "progress:COMPLETING",
                "load:$SERVER",
            ),
            host.events,
        )
        assertEquals("signed-in", jar.values["ap_session"])
        assertEquals(OidcRefreshGrant("grant-2", null), store.grants[ORIGIN])
        assertEquals("ap_session" to SERVER, host.launches.single())
    }

    @Test
    fun `a relaunch with nothing to renew asks before opening the browser`() {
        controller.connect(SERVER, interactive = false)

        assertEquals("ask:Sign in to omni.example to continue.", host.events.last())
        assertTrue(host.launches.isEmpty())

        controller.signIn()
        assertEquals(1, host.launches.size)
    }

    @Test
    fun `closing the browser keeps the reason it opened`() {
        store.grants[ORIGIN] = OidcRefreshGrant("grant-1", null)
        server.refresh = 400 to """{"error":"expired_token"}"""

        controller.connect(SERVER, interactive = true)
        controller.onBrowserClosed()

        assertEquals(
            "setup:Your sign-in to omni.example has expired. Sign in again to continue.",
            host.events.last(),
        )
        assertNull(store.grants[ORIGIN])
    }

    @Test
    fun `closing the browser with nothing to renew returns quietly`() {
        controller.connect(SERVER, interactive = true)
        controller.onBrowserClosed()

        assertEquals("setup:null", host.events.last())
        assertThrows { credentials.completeSignIn(host.callback("code=c")) }
    }

    @Test
    fun `an unreachable server is a connection error and never opens the browser`() {
        store.grants[ORIGIN] = OidcRefreshGrant("grant-1", null)
        server.unreachable = true

        controller.connect(SERVER, interactive = true)

        assertEquals(
            "setup:Couldn't reach omni.example. Check your connection and try again.",
            host.events.last(),
        )
        assertTrue(host.launches.isEmpty())
        assertEquals(OidcRefreshGrant("grant-1", null), store.grants[ORIGIN])
    }

    @Test
    fun `a refused sign-in shows the server's reason`() {
        controller.connect(SERVER, interactive = true)
        controller.onBrowserCallback(
            host.callback("error=access_denied&error_description=Not+permitted"),
        )

        assertEquals("setup:Not permitted", host.events.last())
    }

    @Test
    fun `a session the server or the web view refuses is never loaded`() {
        server.exchange = 200 to """{"token":"refused"}"""
        controller.connect(SERVER, interactive = true)
        controller.onBrowserCallback(host.callback("code=c"))
        assertEquals(
            "setup:omni.example didn't accept the session. Sign in again to continue.",
            host.events.last(),
        )

        server.exchange = 200 to """{"token":"accepted"}"""
        server.accepted += "accepted"
        jar.refuse = true
        controller.connect(SERVER, interactive = true)
        controller.onBrowserCallback(host.callback("code=c"))
        assertEquals(
            "setup:omni.example didn't accept the session. Sign in again to continue.",
            host.events.last(),
        )
        assertFalse(host.events.any { it.startsWith("load:") })
    }

    @Test
    fun `the page asking to sign in renews silently and reloads the last page`() {
        connectWithCookie()
        store.grants[ORIGIN] = OidcRefreshGrant("grant-1", null)
        server.refresh = 200 to """{"access_token":"renewed"}"""
        server.accepted += "renewed"
        controller.onPageVisited("$SERVER/c/42")
        controller.onPageVisited("https://idp.example/login")

        assertTrue(controller.handlesNavigation("$SERVER/auth/login?return_to=%2Fc%2F42"))

        assertEquals("load:$SERVER/c/42", host.events.last())
        assertEquals("renewed", jar.values["ap_session"])
    }

    @Test
    fun `asking again within 15 s asks the user instead of renewing`() {
        connectWithCookie()
        store.grants[ORIGIN] = OidcRefreshGrant("grant-1", null)
        server.refresh = 200 to """{"access_token":"renewed"}"""
        server.accepted += "renewed"
        controller.onSignInRequested()
        val refreshes = server.requests.count { it.endsWith("/oauth/token") }

        now += 5_000
        controller.onSignInRequested()

        assertEquals(
            "ask:omni.example didn't accept the session. Sign in again to continue.",
            host.events.last(),
        )
        assertEquals(refreshes, server.requests.count { it.endsWith("/oauth/token") })
        controller.signIn()
        assertEquals(1, host.launches.size)
    }

    @Test
    fun `a renewal the page asked for asks with the cause, or returns on a connection error`() {
        connectWithCookie()
        store.grants[ORIGIN] = OidcRefreshGrant("grant-1", null)
        server.refresh = 400 to """{"error":"invalid_grant"}"""
        controller.onSignInRequested()
        assertEquals(
            "ask:omni.example ended your session. Sign in again to continue.",
            host.events.last(),
        )

        connectWithCookie()
        store.grants[ORIGIN] = OidcRefreshGrant("grant-1", null)
        server.unreachable = true
        controller.onSignInRequested()
        assertEquals(
            "setup:Couldn't reach omni.example. Check your connection and try again.",
            host.events.last(),
        )
    }

    @Test
    fun `only the login route of the connected server is taken over`() {
        assertFalse(controller.handlesNavigation("$SERVER/auth/login"))
        connectWithCookie()

        assertFalse(controller.handlesNavigation("$SERVER/auth/logout"))
        assertFalse(controller.handlesNavigation("$SERVER/c/1"))
        assertFalse(controller.handlesNavigation("https://other.example/omnigent/auth/login"))
    }

    @Test
    fun `results for a replaced connection are dropped`() {
        val queued = QueuedExecutor()
        val replaced =
            OidcSessionController(host, credentials, jar, io = queued, main = direct, now = {
                now
            }) { manifest }
        jar.values["ap_session"] = "still-good"
        server.accepted += "still-good"

        replaced.connect(SERVER, interactive = true)
        replaced.connect("https://other.example", interactive = true)
        queued.runAll()

        assertEquals(
            listOf("load:https://other.example"),
            host.events.filter { it.startsWith("load:") },
        )
    }

    @Test
    fun `a sign-in the callback receiver finished is installed`() {
        server.exchange = 200 to """{"token":"handed-off"}"""
        server.accepted += "handed-off"
        val authorization = credentials.beginSignIn(SERVER, "ap_session")
        val state = queryItems(authorization).toMap()["native_state"]
        credentials.completeHandedOffSignIn(
            URI("ai.omnigent.android:/oauth/callback?state=$state&code=c"),
        )

        assertTrue(controller.resumeHandedOffSignIn())

        assertEquals("load:$SERVER", host.events.last())
        assertEquals("handed-off", jar.values["ap_session"])
        assertFalse(controller.resumeHandedOffSignIn())
    }

    private fun connectWithCookie() {
        jar.values["ap_session"] = "still-good"
        server.accepted += "still-good"
        controller.connect(SERVER, interactive = false)
        assertEquals("load:$SERVER", host.events.last())
    }

    private fun assertThrows(block: () -> Unit) {
        val failed = runCatching(block).isFailure
        assertTrue("expected a failure", failed)
    }

    private class FakeServer : OAuthTransport {
        val requests = mutableListOf<String>()
        val accepted = mutableSetOf<String>()
        var refresh = 400 to """{"error":"invalid_grant"}"""
        var exchange = 200 to """{"token":"signed-in"}"""
        var unreachable = false

        override fun execute(request: OAuthHttpRequest): OAuthHttpResponse {
            requests += "${request.method} ${request.uri.path}"
            if (unreachable) throw OAuthNetworkException()
            val path = request.uri.path
            val (status, body) =
                when {
                    path.endsWith("/v1/me") -> {
                        val token = request.headers["Cookie"]?.substringAfter('=')
                        (if (token in accepted) 200 else 401) to "{}"
                    }

                    path.endsWith("/oauth/token") -> {
                        refresh
                    }

                    path.endsWith("/auth/native-token") -> {
                        exchange
                    }

                    path.endsWith("/oauth/revoke") -> {
                        200 to """{"revoked":true}"""
                    }

                    else -> {
                        404 to "{}"
                    }
                }
            return OAuthHttpResponse(request.uri, status, body.toByteArray())
        }
    }

    private class FakeJar : OidcCookieJar {
        val values = mutableMapOf<String, String>()
        val writes = mutableListOf<Pair<String, String>>()
        var refuse = false

        override fun get(url: String): String? =
            values.entries.joinToString("; ") { "${it.key}=${it.value}" }.ifEmpty { null }

        override fun set(
            url: String,
            setCookie: String,
            callback: (Boolean) -> Unit,
        ) {
            writes += url to setCookie
            if (refuse) {
                callback(false)
                return
            }
            val (name, value) = setCookie.substringBefore(';').split('=', limit = 2)
            if (setCookie.contains("Max-Age=0")) values.remove(name) else values[name] = value
            callback(true)
        }

        override fun flush() = Unit
    }

    private class FakeHost : OidcSessionController.Host {
        lateinit var credentials: OidcCredentials
        val events = mutableListOf<String>()
        val launches = mutableListOf<Pair<String, String>>()
        private var state: String? = null

        fun callback(query: String) = URI("ai.omnigent.android:/oauth/callback?state=$state&$query")

        override fun showProgress(progress: OidcSessionController.Progress) {
            events += "progress:$progress"
        }

        override fun showSignInRequired(message: String) {
            events += "ask:$message"
        }

        override fun loadWithoutNativeSignIn(url: String) {
            events += "legacy:$url"
        }

        override fun loadPage(url: String) {
            events += "load:$url"
        }

        override fun returnToSetup(message: String?) {
            events += "setup:$message"
        }

        override fun launchSignIn(
            serverUrl: String,
            cookieName: String,
        ): Boolean {
            launches += cookieName to serverUrl
            state =
                queryItems(credentials.beginSignIn(serverUrl, cookieName)).toMap()["native_state"]
            return true
        }
    }

    private class MemoryStore : OidcCredentialStorage {
        val grants = mutableMapOf<String, OidcRefreshGrant>()

        override fun load(origin: String): OidcRefreshGrant? = grants[origin]

        override fun save(
            origin: String,
            grant: OidcRefreshGrant,
        ) {
            grants[origin] = grant
        }

        override fun delete(origin: String) {
            grants.remove(origin)
        }
    }

    private class QueuedExecutor : Executor {
        private val tasks = ArrayDeque<Runnable>()

        override fun execute(command: Runnable) {
            tasks += command
        }

        fun runAll() {
            while (tasks.isNotEmpty()) tasks.removeFirst().run()
        }
    }

    private object PlainCipher : RecordCipher {
        override fun encrypt(
            plaintext: ByteArray,
            associatedData: ByteArray,
        ): ByteArray = plaintext

        override fun decrypt(
            ciphertext: ByteArray,
            associatedData: ByteArray,
        ): ByteArray = ciphertext
    }

    private companion object {
        const val ORIGIN = "https://omni.example"
        const val SERVER = "https://omni.example/omnigent"

        fun nativeManifest(cookie: String) =
            ServerManifest(
                1.0,
                ServerManifest.Auth(
                    ServerManifest.Mode.OIDC,
                    cookie,
                    listOf(OidcRedirect.REDIRECT_URI),
                ),
            )
    }
}
