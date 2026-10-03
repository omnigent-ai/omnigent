package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.net.URI

@RunWith(RobolectricTestRunner::class)
class DatabricksSessionClientTest {
    private val configuration =
        DatabricksOAuthConfiguration(
            "public-client",
            URI("https://login.databricks.com/mobile-redirect"),
        )
    private val context =
        DatabricksWebContext(
            configuration,
            DatabricksCredentialScope.from(
                URI("https://dbc-123.cloud.databricks.com/omnigent?o=42"),
                configuration,
            ),
            URI("https://dbc-123.cloud.databricks.com/omnigent?o=42&view=chat#latest"),
        )
    private val issuer = DatabricksOAuthIssuer(URI("https://dbc-123.cloud.databricks.com/oidc"))
    private val tokens = DatabricksOAuthTokens("access", "refresh", Long.MAX_VALUE, issuer)

    @Test
    fun `session exchange strips bearer after first hop and carries validated cookies`() {
        val transport = QueueTransport()
        transport.responses +=
            Response(
                302,
                mapOf(
                    "Location" to
                        listOf("https://alias.cloud.databricks.com/auth/session/create?o=42"),
                    "Set-Cookie" to
                        listOf("route=one; Domain=.cloud.databricks.com; Path=/; Secure; HttpOnly"),
                ),
            )
        transport.responses +=
            Response(
                302,
                mapOf(
                    "Location" to listOf("/omnigent?o=42"),
                    "Set-Cookie" to
                        listOf(
                            "DBAUTH=session; Domain=.cloud.databricks.com; Path=/; Secure; HttpOnly",
                        ),
                ),
            )
        transport.responses += Response(200)

        val session = DatabricksSessionClient(transport).create(context, tokens)

        assertEquals(
            URI("https://alias.cloud.databricks.com/omnigent?o=42&view=chat#latest"),
            session.pageUri,
        )
        assertEquals("42", session.workspaceId)
        assertEquals("Bearer access", transport.requests[0].headers["Authorization"])
        assertFalse(transport.requests[1].headers.containsKey("Authorization"))
        assertTrue(transport.requests[1].headers["Cookie"]!!.contains("route=one"))
        assertTrue(transport.requests[2].headers["Cookie"]!!.contains("DBAUTH=session"))
        assertTrue(session.cookies.any { it.name == "DBAUTH" && it.secure && it.httpOnly })
    }

    @Test
    fun `session requires secure HttpOnly DBAUTH with valid response scope`() {
        listOf(
            "DBAUTH=value; Path=/; HttpOnly",
            "DBAUTH=value; Path=/; Secure",
            "DBAUTH=value; Domain=evil.example; Path=/; Secure; HttpOnly",
        ).forEach { header ->
            assertThrows(DatabricksSessionException.UnsafeCookie::class.java) {
                DatabricksSessionClient.parseCookies(
                    SessionHttpResponse(
                        URI("https://dbc-123.cloud.databricks.com/auth/session/create"),
                        200,
                        mapOf("Set-Cookie" to listOf(header)),
                    ),
                )
            }
        }
    }

    @Test
    fun `redirect cannot leave Databricks or change workspace identity`() {
        val unsafeRedirect = DatabricksSessionException.UnsafeRedirect::class.java
        val workspaceChanged = DatabricksSessionException.WorkspaceChanged::class.java
        listOf(
            "https://evil.example/omnigent" to unsafeRedirect,
            "https://other.cloud.databricks.com/omnigent?o=43" to workspaceChanged,
            "http://dbc-123.cloud.databricks.com/omnigent?o=42" to unsafeRedirect,
        ).forEach { (location, expected) ->
            val transport = QueueTransport()
            transport.responses += Response(302, mapOf("Location" to listOf(location)))
            assertThrows(expected) {
                DatabricksSessionClient(transport).create(context, tokens)
            }
        }
    }

    @Test
    fun `account grant requires an explicit workspace id`() {
        val noWorkspace =
            DatabricksWebContext(
                configuration,
                DatabricksCredentialScope.from(
                    URI("https://dbc-123.cloud.databricks.com"),
                    configuration,
                ),
                URI("https://dbc-123.cloud.databricks.com/omnigent"),
            )
        val accountTokens =
            tokens.copy(
                issuer =
                    DatabricksOAuthIssuer(
                        URI("https://accounts.cloud.databricks.com/oidc/accounts/account-1"),
                    ),
            )

        assertThrows(DatabricksSessionException.WorkspaceRequired::class.java) {
            DatabricksSessionClient(QueueTransport()).create(noWorkspace, accountTokens)
        }
    }

    @Test
    fun `success without applicable DBAUTH is rejected`() {
        val transport = QueueTransport()
        transport.responses += Response(200)
        assertThrows(DatabricksSessionException.MissingCookie::class.java) {
            DatabricksSessionClient(transport).create(context, tokens)
        }
    }

    private data class Response(
        val status: Int,
        val headers: Map<String, List<String>> = emptyMap(),
    )

    private class QueueTransport : SessionTransport {
        val requests = mutableListOf<SessionHttpRequest>()
        val responses = ArrayDeque<Response>()

        override fun execute(request: SessionHttpRequest): SessionHttpResponse {
            requests += request
            val response = responses.removeFirst()
            return SessionHttpResponse(request.uri, response.status, response.headers)
        }
    }
}
