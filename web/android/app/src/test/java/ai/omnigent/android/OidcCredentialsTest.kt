package ai.omnigent.android

import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.net.URI
import java.util.UUID
import java.util.concurrent.CancellationException
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ExecutionException
import java.util.concurrent.Executor

@RunWith(RobolectricTestRunner::class)
class OidcCredentialsTest {
    private val context = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val server = "https://omni.example/omnigent/"
    private val origin = "https://omni.example"
    private val store = MemoryStore()
    private val transport = FakeTransport()
    private val executor = QueuedExecutor()

    // Every attempt gets state "state-1"; verifiers are numbered by call.
    private var randomCalls = 0
    private val pendingRecords =
        EncryptedRecordStore(context, "test-oidc-${UUID.randomUUID()}", PlainCipher)
    private val credentials =
        OidcCredentials(
            store = store,
            pending = OidcPendingSignInStore(context, pendingRecords) { 1_000L },
            transport = transport,
            verifyTransport = transport,
            executor = executor,
            now = { 1_000L },
            randomValue = { if (++randomCalls % 2 == 1) "state-1" else "verifier-$randomCalls" },
        )

    @Test
    fun `sign-in opens the server's login under its mount with a PKCE challenge`() {
        val authorization = credentials.beginSignIn(server, "ap_session")

        assertEquals(
            "https://omni.example/omnigent/auth/login",
            authorization.toString().substringBefore('?'),
        )
        assertEquals(
            mapOf(
                "native_redirect_uri" to "ai.omnigent.android:/oauth/callback",
                "native_state" to "state-1",
                "code_challenge" to OAuthSupport.challenge("verifier-2"),
                "code_challenge_method" to "S256",
            ),
            queryItems(authorization).toMap(),
        )
    }

    @Test
    fun `a callback exchanges its code once and stores the grant`() {
        credentials.beginSignIn(server, "ap_session")
        transport.respond(
            200,
            """{"token":"jwt.payload.sig","user_id":"alice@example.com","expires_in":28800,"refresh_token":"refresh-1"}""",
        )

        val completed = credentials.completeSignIn(callback("state=state-1&code=code-1"))

        assertEquals(
            OidcCredentials.CompletedSignIn(
                server,
                "ap_session",
                OidcSessionToken("jwt.payload.sig", 1_000L + 28_800_000L),
            ),
            completed,
        )
        val exchange = transport.requests.single()
        assertEquals("POST", exchange.method)
        assertEquals(URI("https://omni.example/omnigent/auth/native-token"), exchange.uri)
        assertEquals(
            mapOf(
                "code" to "code-1",
                "code_verifier" to "verifier-2",
                "redirect_uri" to "ai.omnigent.android:/oauth/callback",
            ),
            form(exchange),
        )
        assertEquals(OidcRefreshGrant("refresh-1", "alice@example.com"), store.grants[origin])
        assertThrows(OidcSignInException.InvalidCallback::class.java) {
            credentials.completeSignIn(callback("state=state-1&code=code-1"))
        }
    }

    @Test
    fun `a server without refresh grants forgets the older grant`() {
        store.grants[origin] = OidcRefreshGrant("old", null)
        credentials.beginSignIn(server, "ap_session")
        transport.respond(200, """{"token":"t","user_id":"alice@example.com"}""")

        val completed = credentials.completeSignIn(callback("state=state-1&code=c"))

        assertNull(completed.session.expiresAtEpochMillis)
        assertNull(store.grants[origin])
    }

    @Test
    fun `a sign-in that can't forget the older grant fails instead of keeping it`() {
        store.grants[origin] = OidcRefreshGrant("old", null)
        store.failDelete = true
        credentials.beginSignIn(server, "ap_session")
        transport.respond(200, """{"token":"t"}""")

        assertThrows(OidcSignInException.StorageUnavailable::class.java) {
            credentials.completeSignIn(callback("state=state-1&code=c"))
        }
    }

    @Test
    fun `an unreadable attempt asks to unlock rather than calling the callback invalid`() {
        credentials.beginSignIn(server, "ap_session")
        pendingRecords.write("pending", "garbage".toByteArray())

        assertThrows(OidcSignInException.StorageUnavailable::class.java) {
            credentials.completeSignIn(callback("state=state-1&code=c"))
        }
        assertTrue(transport.requests.isEmpty())
    }

    @Test
    fun `a grant that can't be stored is revoked`() {
        store.failSave = true
        credentials.beginSignIn(server, "ap_session")
        transport.respond(200, """{"token":"t","refresh_token":"refresh-1"}""")

        assertThrows(OidcSignInException.StorageUnavailable::class.java) {
            credentials.completeSignIn(callback("state=state-1&code=c"))
        }
        executor.runAll()

        val revoke = transport.requests.last()
        assertEquals(URI("https://omni.example/omnigent/oauth/revoke"), revoke.uri)
        assertEquals(mapOf("refresh_token" to "refresh-1"), form(revoke))
    }

    @Test
    fun `callbacks that aren't this sign-in's are refused without ending it`() {
        credentials.beginSignIn(server, "ap_session")
        listOf(
            "ai.omnigent.ios:/oauth/callback?state=state-1&code=c",
            "ai.omnigent.android://oauth/callback?state=state-1&code=c",
            "ai.omnigent.android://mobile-redirect?state=state-1&code=c",
            "ai.omnigent.android:/oauth/other?state=state-1&code=c",
            "ai.omnigent.android:/oauth/callback?code=c",
            "ai.omnigent.android:/oauth/callback?state=state-9&code=c",
            "ai.omnigent.android:/oauth/callback?state=state-1&state=state-1&code=c",
            "ai.omnigent.android:/oauth/callback?state=state-1&error=access_denied&state=x",
        ).forEach { raw ->
            assertThrows(raw, OidcSignInException.InvalidCallback::class.java) {
                credentials.completeSignIn(URI(raw))
            }
        }
        assertTrue(transport.requests.isEmpty())

        transport.respond(200, """{"token":"t"}""")
        assertEquals(
            "t",
            credentials.completeSignIn(callback("state=state-1&code=c")).session.token,
        )
    }

    @Test
    fun `an error callback ends the sign-in with the server's reason`() {
        credentials.beginSignIn(server, "ap_session")

        val refused =
            assertThrows(OidcSignInException.SignInRefused::class.java) {
                credentials.completeSignIn(
                    callback(
                        "error=access_denied&error_description=Email+domain+%27example.com%27+is+not+permitted&state=state-1",
                    ),
                )
            }

        assertEquals("Email domain 'example.com' is not permitted", refused.message)
        assertThrows(OidcSignInException.InvalidCallback::class.java) {
            credentials.completeSignIn(callback("state=state-1&code=c"))
        }
    }

    @Test
    fun `a callback without exactly one code ends the sign-in as invalid`() {
        val queries = listOf("state=state-1", "state=state-1&code=", "state=state-1&code=a&code=b")
        queries.forEach { query ->
            credentials.beginSignIn(server, "ap_session")
            assertThrows(query, OidcSignInException.InvalidCallback::class.java) {
                credentials.completeSignIn(callback(query))
            }
        }
        assertTrue(transport.requests.isEmpty())
    }

    @Test
    fun `a refused or unusable exchange is a refused sign-in`() {
        listOf(
            400 to """{"error":"invalid_grant"}""",
            200 to """{"token":"has;semicolon"}""",
            200 to """{"token":""}""",
            200 to "not json",
        ).forEach { (status, body) ->
            credentials.beginSignIn(server, "ap_session")
            transport.respond(status, body)
            val refused =
                assertThrows(body, OidcSignInException.SignInRefused::class.java) {
                    credentials.completeSignIn(callback("state=state-1&code=c"))
                }
            assertEquals("omni.example didn't accept the sign-in. Try again.", refused.message)
        }

        credentials.beginSignIn(server, "ap_session")
        transport.failNext()
        assertThrows(OidcSignInException.Network::class.java) {
            credentials.completeSignIn(callback("state=state-1&code=c"))
        }
    }

    @Test
    fun `without an attempt in progress a callback is invalid`() {
        assertThrows(OidcSignInException.InvalidCallback::class.java) {
            credentials.completeSignIn(callback("state=state-1&code=c"))
        }
        credentials.beginSignIn(server, "ap_session")
        credentials.cancelSignIn()
        assertThrows(OidcSignInException.InvalidCallback::class.java) {
            credentials.completeSignIn(callback("state=state-1&code=c"))
        }
    }

    @Test
    fun `the receiver's completed sign-in is handed to the shell once`() {
        credentials.beginSignIn(server, "ap_session")
        transport.respond(200, """{"token":"t"}""")

        credentials.completeHandedOffSignIn(callback("state=state-1&code=c"))

        assertEquals("t", credentials.takeHandedOff()!!.session.token)
        assertNull(credentials.takeHandedOff())
    }

    @Test
    fun `refreshes are shared per origin`() {
        store.grants[origin] = OidcRefreshGrant("refresh-1", null)
        transport.respond(
            200,
            """{"access_token":"fresh","token_type":"Bearer","expires_in":3600}""",
        )

        val first = credentials.refresh(server)
        val second = credentials.refresh("https://omni.example/other")
        executor.runAll()

        assertEquals(OidcSessionToken("fresh", 1_000L + 3_600_000L), first.get())
        assertEquals(first.get(), second.get())
        val refresh = transport.requests.single()
        assertEquals(URI("https://omni.example/omnigent/oauth/token"), refresh.uri)
        assertEquals(
            mapOf("grant_type" to "refresh_token", "refresh_token" to "refresh-1"),
            form(refresh),
        )

        transport.respond(200, """{"access_token":"again"}""")
        val third = credentials.refresh(server)
        executor.runAll()
        assertEquals("again", third.get().token)
    }

    @Test
    fun `dead grants are forgotten and every other failure keeps the grant`() {
        data class Case(
            val status: Int,
            val body: String,
            val error: Class<out OidcSignInException>,
            val forgotten: Boolean,
        )
        listOf(
            Case(
                400,
                """{"error":"invalid_grant"}""",
                OidcSignInException.GrantRejected::class.java,
                true,
            ),
            Case(
                400,
                """{"error":"expired_token"}""",
                OidcSignInException.GrantExpired::class.java,
                true,
            ),
            Case(
                404,
                """{"detail":"Not Found"}""",
                OidcSignInException.NoStoredGrant::class.java,
                true,
            ),
            Case(
                500,
                """{"error":"server_error"}""",
                OidcSignInException.Network::class.java,
                false,
            ),
            Case(302, "", OidcSignInException.Network::class.java, false),
            Case(
                200,
                """{"access_token":"bad token"}""",
                OidcSignInException.Network::class.java,
                false,
            ),
        ).forEach { case ->
            store.grants[origin] = OidcRefreshGrant("refresh-1", null)
            transport.respond(case.status, case.body)
            val refresh = credentials.refresh(server)
            executor.runAll()

            assertEquals(case.toString(), case.error, failure(refresh).javaClass)
            assertEquals(case.toString(), case.forgotten, store.grants[origin] == null)
        }

        store.grants[origin] = OidcRefreshGrant("refresh-1", null)
        transport.failNext()
        val unreachable = credentials.refresh(server)
        executor.runAll()
        assertTrue(failure(unreachable) is OidcSignInException.Network)
        assertNotNull(store.grants[origin])
    }

    @Test
    fun `a stale refresh never forgets a grant a newer sign-in saved`() {
        store.grants[origin] = OidcRefreshGrant("grant-A", null)
        transport.respond(400, """{"error":"invalid_grant"}""")
        transport.respond(200, """{"revoked":true}""")
        transport.onRequest = { request ->
            if (request.uri.path.endsWith("/oauth/token")) {
                // Signed out and back in while the old grant's refresh was on the wire.
                credentials.signOut(server)
                store.grants[origin] = OidcRefreshGrant("grant-B", null)
            }
        }

        val stale = credentials.refresh(server)
        executor.runAll()

        assertTrue(failure(stale) is CancellationException)
        assertEquals(OidcRefreshGrant("grant-B", null), store.grants[origin])
    }

    @Test
    fun `a dead grant that can't be forgotten is reported as unreadable storage`() {
        store.grants[origin] = OidcRefreshGrant("grant-1", null)
        store.failDelete = true
        transport.respond(400, """{"error":"invalid_grant"}""")

        val refresh = credentials.refresh(server)
        executor.runAll()

        assertTrue(failure(refresh) is OidcSignInException.StorageUnavailable)
        assertNotNull(store.grants[origin])
    }

    @Test
    fun `refresh without a readable grant asks to sign in or to unlock`() {
        val missing = credentials.refresh(server)
        executor.runAll()
        assertTrue(failure(missing) is OidcSignInException.NoStoredGrant)

        store.grants[origin] = OidcRefreshGrant("refresh-1", null)
        store.failLoad = true
        val unreadable = credentials.refresh(server)
        executor.runAll()
        assertTrue(failure(unreadable) is OidcSignInException.StorageUnavailable)
        store.failLoad = false
        assertNotNull(store.grants[origin])
        assertTrue(transport.requests.isEmpty())
    }

    @Test
    fun `a session check accepts only a 200 from v1 me`() {
        val answers = listOf(200 to true, 401 to false, 403 to false, 302 to false)
        answers.forEach { (status, accepted) ->
            transport.respond(status, "")
            assertEquals(
                status.toString(),
                accepted,
                credentials.isAccepted(server, "ap_session", "tok"),
            )
        }
        val check = transport.requests.first()
        assertEquals("GET", check.method)
        assertEquals(URI("https://omni.example/omnigent/v1/me"), check.uri)
        assertEquals("ap_session=tok", check.headers["Cookie"])

        transport.respond(503, "")
        assertThrows(OidcSignInException.Network::class.java) {
            credentials.isAccepted(server, "ap_session", "tok")
        }
    }

    @Test
    fun `sign-out forgets the grant before revoking it and drops a refresh in flight`() {
        store.grants[origin] = OidcRefreshGrant("refresh-1", null)
        transport.respond(200, """{"access_token":"minted-from-old-grant"}""")
        transport.respond(200, """{"revoked":true}""")
        val inFlight = credentials.refresh(server)
        var revocation: CompletableFuture<Void>? = null
        transport.onRequest = { request ->
            if (request.uri.path.endsWith("/oauth/token")) {
                // The user signs out while the refresh is on the wire.
                revocation = credentials.signOut(server)
            } else {
                assertNull(store.grants[origin])
            }
        }

        executor.runAll()
        revocation!!.get()

        assertTrue(failure(inFlight) is CancellationException)
        assertEquals(
            listOf("/omnigent/oauth/token", "/omnigent/oauth/revoke"),
            transport.requests.map { it.uri.path },
        )
        assertEquals(mapOf("refresh_token" to "refresh-1"), form(transport.requests.last()))
        assertNull(store.grants[origin])
    }

    @Test
    fun `sign-out reports a grant it couldn't delete`() {
        store.grants[origin] = OidcRefreshGrant("refresh-1", null)
        store.failDelete = true

        assertThrows(
            OidcSignInException.StorageUnavailable::class.java,
        ) { credentials.signOut(server) }
        store.failDelete = false
        store.failLoad = true
        credentials.signOut(server).get()
        store.failLoad = false
        assertNull(store.grants[origin])
    }

    @Test
    fun `stored grants are reported without reading the network`() {
        assertFalse(credentials.hasStoredGrant(server))
        store.grants[origin] = OidcRefreshGrant("refresh-1", null)
        assertTrue(credentials.hasStoredGrant(server))
        assertTrue(transport.requests.isEmpty())
    }

    @Test
    fun `the app redirect is matched exactly`() {
        assertTrue(OidcRedirect.matches(URI("ai.omnigent.android:/oauth/callback?code=c&state=s")))
        assertTrue(OidcRedirect.matches(URI("AI.OMNIGENT.ANDROID:/oauth/callback")))
        listOf(
            "ai.omnigent.android://oauth/callback",
            "ai.omnigent.android://user@host/oauth/callback",
            "ai.omnigent.android:/oauth/callback/",
            "ai.omnigent.android:/OAuth/Callback",
            "ai.omnigent.ios:/oauth/callback",
            "https://omni.example/oauth/callback",
        ).forEach { raw -> assertFalse(raw, OidcRedirect.matches(URI(raw))) }
    }

    @Test
    fun `session tokens must be RFC 6265 cookie values and never print`() {
        assertTrue(
            OidcSessionToken.isCookieSafe("eyJhbGciOi.eyJzdWIi.c2ln-_~!#$%&'()*+-./:<=>?@[]^`{|}"),
        )
        listOf("", "a b", "a;b", "a,b", "a\"b", "a\\b", "a\nb", "é").forEach { token ->
            assertFalse(token, OidcSessionToken.isCookieSafe(token))
        }
        assertEquals("OidcSessionToken(expiresAt=5)", OidcSessionToken("secret", 5L).toString())
    }

    private fun callback(query: String) = URI("ai.omnigent.android:/oauth/callback?$query")

    private fun form(request: OAuthHttpRequest): Map<String, String?> =
        queryItems(URI("x:/?" + request.body!!.toString(Charsets.UTF_8))).toMap()

    private fun failure(future: CompletableFuture<*>): Throwable {
        try {
            future.get()
        } catch (error: ExecutionException) {
            return error.cause!!
        } catch (error: CancellationException) {
            return error
        }
        throw AssertionError("expected a failure")
    }

    private class FakeTransport : OAuthTransport {
        val requests = mutableListOf<OAuthHttpRequest>()
        private val responses = ArrayDeque<OAuthHttpResponse?>()
        var onRequest: (OAuthHttpRequest) -> Unit = {}

        fun respond(
            status: Int,
            body: String,
        ) {
            responses +=
                OAuthHttpResponse(URI("https://unused.example"), status, body.toByteArray())
        }

        fun failNext() {
            responses += null
        }

        override fun execute(request: OAuthHttpRequest): OAuthHttpResponse {
            requests += request
            onRequest(request)
            val response = responses.removeFirst() ?: throw OAuthNetworkException()
            return response.copy(uri = request.uri)
        }
    }

    private class MemoryStore : OidcCredentialStorage {
        val grants = mutableMapOf<String, OidcRefreshGrant>()
        var failLoad = false
        var failSave = false
        var failDelete = false

        override fun load(origin: String): OidcRefreshGrant? {
            if (failLoad) throw CredentialStorageException.InvalidData()
            return grants[origin]
        }

        override fun save(
            origin: String,
            grant: OidcRefreshGrant,
        ) {
            if (failSave) throw CredentialStorageException.Unavailable()
            grants[origin] = grant
        }

        override fun delete(origin: String) {
            if (failDelete) throw CredentialStorageException.Unavailable()
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
}
