package ai.omnigent.android

import android.os.Looper
import android.view.View
import android.widget.PopupMenu
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import java.net.URI
import java.util.concurrent.TimeUnit

@RunWith(RobolectricTestRunner::class)
class DatabricksAuthDebugMenuTest {
    private val appContext = ApplicationProvider.getApplicationContext<android.content.Context>()
    private val configuration =
        DatabricksOAuthConfiguration(
            "omnigent",
            URI("https://login.databricks.com/mobile-redirect"),
        )
    private val pageUri = URI("https://dbc-123.cloud.databricks.com/omnigent?o=42")
    private val context =
        DatabricksWebContext(
            configuration,
            DatabricksCredentialScope.from(pageUri, configuration),
            pageUri,
        )
    private val issuer = DatabricksOAuthIssuer(URI("https://dbc-123.cloud.databricks.com/oidc"))

    @Test
    fun `debug menu exposes all authentication faults`() {
        val popup = PopupMenu(appContext, View(appContext))

        DatabricksAuthDebugMenu.addItems(popup.menu)

        assertNotNull(popup.menu.findItem(DatabricksAuthDebugMenu.CLEAR_SESSION_COOKIE))
        assertNotNull(popup.menu.findItem(DatabricksAuthDebugMenu.EXPIRE_ACCESS_TOKEN))
        assertNotNull(popup.menu.findItem(DatabricksAuthDebugMenu.REJECT_ACCESS_TOKEN))
        assertNotNull(popup.menu.findItem(DatabricksAuthDebugMenu.CLEAR_REFRESH_GRANT))
    }

    @Test
    fun `session-cookie fault deletes only DBAUTH`() {
        val backend =
            FakeBackend().apply {
                cookies["DBAUTH"] = "session"
                cookies["route"] = "one"
            }
        val profile = DatabricksWebProfile(context.profileName, backend)
        val future =
            DatabricksAuthDebugMenu.run(
                DatabricksAuthDebugMenu.CLEAR_SESSION_COOKIE,
                tokenManager(MemoryStore(tokens())),
                context,
                profile,
                session(),
            )
        shadowOf(Looper.getMainLooper()).idle()

        assertTrue(future.get(1, TimeUnit.SECONDS).contains("Session cookie cleared"))
        assertNull(backend.cookies["DBAUTH"])
        assertEquals("one", backend.cookies["route"])
    }

    @Test
    fun `credential faults expire reject and remove the saved grant`() {
        val store = MemoryStore(tokens())
        val manager = tokenManager(store)
        val profile = DatabricksWebProfile(context.profileName, FakeBackend())

        DatabricksAuthDebugMenu
            .run(
                DatabricksAuthDebugMenu.EXPIRE_ACCESS_TOKEN,
                manager,
                context,
                profile,
                session(),
            ).get(1, TimeUnit.SECONDS)
        assertEquals(1L, store.value!!.expiresAtEpochMillis)
        assertEquals("debug-expired-access-token", store.value!!.accessToken)

        store.value = tokens()
        DatabricksAuthDebugMenu
            .run(
                DatabricksAuthDebugMenu.REJECT_ACCESS_TOKEN,
                manager,
                context,
                profile,
                session(),
                now = 1_000L,
            ).get(1, TimeUnit.SECONDS)
        assertEquals("debug-rejected-access-token", store.value!!.accessToken)
        assertTrue(store.value!!.expiresAtEpochMillis >= 601_000L)

        DatabricksAuthDebugMenu
            .run(
                DatabricksAuthDebugMenu.CLEAR_REFRESH_GRANT,
                manager,
                context,
                profile,
                session(),
            ).get(1, TimeUnit.SECONDS)
        assertNull(store.value)
    }

    private fun tokenManager(store: MemoryStore) = DatabricksTokenManager(store)

    private fun tokens() = DatabricksOAuthTokens("access", "refresh", 1_000_000L, issuer)

    private fun session(): DatabricksWebSession {
        val source = URI("https://dbc-123.cloud.databricks.com/auth/session/create")
        return DatabricksWebSession(
            pageUri,
            listOf(
                SessionCookie(
                    "route",
                    "one",
                    source.host,
                    "/",
                    true,
                    true,
                    true,
                    -1,
                    source,
                    "route=one; Path=/; Secure; HttpOnly",
                ),
                SessionCookie(
                    "DBAUTH",
                    "session",
                    source.host,
                    "/",
                    true,
                    true,
                    true,
                    -1,
                    source,
                    "DBAUTH=session; Path=/; Secure; HttpOnly",
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
            if (value.contains("Max-Age=0")) {
                cookies.remove(pair[0])
            } else {
                cookies[pair[0]] = pair.getOrElse(1) { "" }
            }
            callback(true)
        }

        override fun getCookie(url: String): String? =
            cookies.entries.joinToString("; ") { "${it.key}=${it.value}" }

        override fun flushCookies() = Unit

        override fun clearWebStorage() = Unit
    }
}
