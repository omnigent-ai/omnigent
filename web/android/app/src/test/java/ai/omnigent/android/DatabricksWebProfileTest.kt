package ai.omnigent.android

import android.os.Looper
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import java.net.URI
import java.util.concurrent.ExecutionException
import java.util.concurrent.TimeUnit

@RunWith(RobolectricTestRunner::class)
class DatabricksWebProfileTest {
    private val configuration =
        DatabricksOAuthConfiguration(
            "public-client",
            URI("https://login.databricks.com/mobile-redirect"),
        )

    @Test
    fun `profile identity is stable per credential scope`() {
        val first = context("42")
        val same = context("42")
        val otherWorkspace = context("43")
        val otherClient =
            DatabricksWebContext(
                configuration.copy(clientId = "other-client"),
                DatabricksCredentialScope.from(
                    URI("https://dbc-123.cloud.databricks.com?o=42"),
                    configuration.copy(clientId = "other-client"),
                ),
                URI("https://dbc-123.cloud.databricks.com/omnigent?o=42"),
            )

        assertEquals(first.profileName, same.profileName)
        assertNotEquals(first.profileName, otherWorkspace.profileName)
        assertNotEquals(first.profileName, otherClient.profileName)
        assertTrue(first.profileName.matches(Regex("[a-z0-9-]+")))
    }

    @Test
    fun `install clears old cookies writes the session and verifies DBAUTH`() {
        val backend = FakeBackend()
        backend.cookies["old"] = "value"
        val profile = DatabricksWebProfile("profile-install", backend)
        val session = session()

        val future = profile.install(session)
        shadowOf(Looper.getMainLooper()).idle()

        future.get(1, TimeUnit.SECONDS)
        assertEquals("session", backend.cookies["DBAUTH"])
        assertEquals(listOf("remove", "set:route", "set:DBAUTH", "flush"), backend.events)
    }

    @Test
    fun `rejected cookie write fails before page load`() {
        val backend = FakeBackend().apply { rejectName = "DBAUTH" }
        val profile = DatabricksWebProfile("profile-reject", backend)

        val future = profile.install(session())
        shadowOf(Looper.getMainLooper()).idle()

        val error =
            assertThrows(ExecutionException::class.java) {
                future.get(1, TimeUnit.SECONDS)
            }
        assertTrue(error.cause is DatabricksSessionException.UnsafeCookie)
        assertFalse(backend.events.contains("flush"))
    }

    @Test
    fun `clear removes profile cookies and web storage`() {
        val backend = FakeBackend()
        backend.cookies["DBAUTH"] = "session"
        val profile = DatabricksWebProfile("profile-clear", backend)

        val future = profile.clear()
        shadowOf(Looper.getMainLooper()).idle()
        future.get(1, TimeUnit.SECONDS)

        assertTrue(backend.cookies.isEmpty())
        assertTrue(backend.storageCleared)
    }

    private fun context(workspaceId: String): DatabricksWebContext {
        val uri = URI("https://dbc-123.cloud.databricks.com/omnigent?o=$workspaceId")
        return DatabricksWebContext(
            configuration,
            DatabricksCredentialScope.from(uri, configuration),
            uri,
        )
    }

    private fun session(): DatabricksWebSession {
        val source = URI("https://dbc-123.cloud.databricks.com/auth/session/create")
        val page = URI("https://dbc-123.cloud.databricks.com/omnigent?o=42")
        return DatabricksWebSession(
            page,
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
            setOf(originOf(page.toString())!!),
            configuration,
            "42",
        )
    }

    private class FakeBackend : WebProfileBackend {
        val cookies = mutableMapOf<String, String>()
        val events = mutableListOf<String>()
        var rejectName: String? = null
        var storageCleared = false

        override fun setAcceptCookie(accept: Boolean) = Unit

        override fun removeAllCookies(callback: (Boolean) -> Unit) {
            events += "remove"
            cookies.clear()
            callback(true)
        }

        override fun setCookie(
            url: String,
            value: String,
            callback: (Boolean) -> Unit,
        ) {
            val pair = value.substringBefore(';').split('=', limit = 2)
            val name = pair[0]
            events += "set:$name"
            if (name == rejectName) {
                callback(false)
            } else {
                cookies[name] = pair.getOrElse(1) { "" }
                callback(true)
            }
        }

        override fun getCookie(url: String): String? =
            cookies.entries.joinToString("; ") { "${it.key}=${it.value}" }

        override fun flushCookies() {
            events += "flush"
        }

        override fun clearWebStorage() {
            storageCleared = true
        }
    }
}
