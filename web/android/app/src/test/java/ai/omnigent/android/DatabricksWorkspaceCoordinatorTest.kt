package ai.omnigent.android

import android.os.Looper
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import java.net.URI
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ExecutionException
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

@RunWith(RobolectricTestRunner::class)
class DatabricksWorkspaceCoordinatorTest {
    private val appContext = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val configuration =
        DatabricksOAuthConfiguration(
            "public-client",
            URI("https://login.databricks.com/mobile-redirect"),
        )
    private val context =
        DatabricksWebContext(
            configuration,
            DatabricksCredentialScope.from(
                URI("https://dbc-123.cloud.databricks.com?o=42"),
                configuration,
            ),
            URI("https://dbc-123.cloud.databricks.com/omnigent?o=42"),
        )
    private val issuer = DatabricksOAuthIssuer(URI("https://dbc-123.cloud.databricks.com/oidc"))

    @Test
    fun `saved grant creates and installs a session before completing`() {
        val credential = tokens("access", "refresh")
        val store = MemoryStore(credential)
        val tokenManager = DatabricksTokenManager(store, { _, _, _ -> credential })
        val backend = FakeBackend()
        val profile = DatabricksWebProfile(context.profileName, backend)
        val session = session("session")
        val coordinator =
            DatabricksWorkspaceCoordinator(
                appContext,
                tokenManager,
                DatabricksSessionCreating { _, received ->
                    assertEquals(credential, received)
                    session
                },
                Executors.newSingleThreadExecutor(),
            )

        val prepared = coordinator.prepare(context, profile)
        driveMainLooper(prepared)

        assertEquals(session, prepared.get(1, TimeUnit.SECONDS))
        assertEquals("session", backend.cookies["DBAUTH"])
    }

    @Test
    fun `cached access-token rejection forces one refresh and retry`() {
        val original = tokens("old", "refresh")
        val rotated = tokens("next", "next-refresh")
        val store = MemoryStore(original)
        val refreshes = AtomicInteger()
        val tokenManager =
            DatabricksTokenManager(
                store,
                { _, _, _ ->
                    refreshes.incrementAndGet()
                    rotated
                },
                Executors.newSingleThreadExecutor(),
            )
        val calls = AtomicInteger()
        val coordinator =
            DatabricksWorkspaceCoordinator(
                appContext,
                tokenManager,
                DatabricksSessionCreating { _, received ->
                    if (calls.getAndIncrement() == 0) throw DatabricksSessionException.Rejected(401)
                    assertEquals(rotated, received)
                    session("recovered")
                },
                Executors.newSingleThreadExecutor(),
            )
        val profile = DatabricksWebProfile(context.profileName, FakeBackend())

        val prepared = coordinator.prepare(context, profile)
        driveMainLooper(prepared)

        assertEquals(
            "recovered",
            prepared
                .get()
                .cookies
                .last()
                .value,
        )
        assertEquals(1, refreshes.get())
        assertEquals(2, calls.get())
        assertEquals(rotated, store.value)
    }

    @Test
    fun `missing grant requests explicit reauthentication without creating a session`() {
        val calls = AtomicInteger()
        val tokenManager = DatabricksTokenManager(MemoryStore(null))
        val coordinator =
            DatabricksWorkspaceCoordinator(
                appContext,
                tokenManager,
                DatabricksSessionCreating { _, _ ->
                    calls.incrementAndGet()
                    session("unused")
                },
                Executors.newSingleThreadExecutor(),
            )
        val future =
            coordinator.prepare(
                context,
                DatabricksWebProfile(context.profileName, FakeBackend()),
            )

        val error = assertThrows(ExecutionException::class.java) { future.get(1, TimeUnit.SECONDS) }
        assertTrue(error.cause is DatabricksSessionException.ReauthenticationRequired)
        assertEquals(0, calls.get())
    }

    private fun driveMainLooper(future: CompletableFuture<*>) {
        repeat(100) {
            shadowOf(Looper.getMainLooper()).idle()
            if (future.isDone) return
            Thread.sleep(5)
        }
    }

    private fun tokens(
        access: String,
        refresh: String,
    ) = DatabricksOAuthTokens(access, refresh, Long.MAX_VALUE, issuer)

    private fun session(value: String): DatabricksWebSession {
        val source = URI("https://dbc-123.cloud.databricks.com/auth/session/create")
        return DatabricksWebSession(
            context.pageUri,
            listOf(
                SessionCookie(
                    "DBAUTH",
                    value,
                    source.host,
                    "/",
                    true,
                    true,
                    true,
                    -1,
                    source,
                    "DBAUTH=$value; Path=/; Secure; HttpOnly",
                ),
            ),
            setOf(context.scope.workspaceOrigin.toString()),
            configuration,
            "42",
        )
    }

    private class MemoryStore(
        var value: DatabricksOAuthTokens?,
    ) : DatabricksCredentialStorage {
        override fun load(scope: DatabricksCredentialScope) = value

        override fun save(
            scope: DatabricksCredentialScope,
            tokens: DatabricksOAuthTokens,
        ) {
            value = tokens
        }

        override fun delete(scope: DatabricksCredentialScope) {
            value = null
        }
    }

    private class FakeBackend : WebProfileBackend {
        val cookies = mutableMapOf<String, String>()

        override fun setAcceptCookie(accept: Boolean) = Unit

        override fun removeAllCookies(callback: (Boolean) -> Unit) {
            cookies.clear()
            callback(true)
        }

        override fun setCookie(
            url: String,
            value: String,
            callback: (Boolean) -> Unit,
        ) {
            val pair = value.substringBefore(';').split('=', limit = 2)
            cookies[pair[0]] = pair[1]
            callback(true)
        }

        override fun getCookie(url: String): String? =
            cookies.entries.joinToString("; ") { "${it.key}=${it.value}" }

        override fun flushCookies() = Unit

        override fun clearWebStorage() = Unit
    }
}
