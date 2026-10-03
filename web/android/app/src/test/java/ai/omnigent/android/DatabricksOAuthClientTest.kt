package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.net.URI

@RunWith(RobolectricTestRunner::class)
class DatabricksOAuthClientTest {
    private val configuration =
        DatabricksOAuthConfiguration(
            "public-client",
            URI("https://login.databricks.com/mobile-redirect"),
        )
    private val attempt =
        DatabricksOAuthAttempt(
            configuration,
            DatabricksCredentialScope.from(
                URI("https://dbc-123.cloud.databricks.com?o=42"),
                configuration,
            ),
            "state",
            "verifier",
        )
    private val issuer =
        DatabricksOAuthIssuer(URI("https://accounts.cloud.databricks.com/oidc/accounts/a-1"))

    @Test
    fun `exchange validates discovery before posting the authorization code`() {
        val transport = RecordingTransport()
        transport.responses +=
            OAuthHttpResponse(
                issuer.discoveryUri,
                200,
                """{"issuer":"${issuer.uri}","token_endpoint":"${issuer.tokenEndpoint}"}"""
                    .toByteArray(),
            )
        transport.responses +=
            OAuthHttpResponse(
                issuer.tokenEndpoint,
                200,
                """{"access_token":"access","refresh_token":"refresh","token_type":"Bearer","expires_in":3600}"""
                    .toByteArray(),
            )
        val client = DatabricksOAuthClient(transport) { 1_000L }

        val tokens = client.exchange("code-value", attempt, issuer)

        assertEquals("access", tokens.accessToken)
        assertEquals("refresh", tokens.refreshToken)
        assertEquals(3_601_000L, tokens.expiresAtEpochMillis)
        assertEquals(issuer, tokens.issuer)
        assertEquals(listOf("GET", "POST"), transport.requests.map { it.method })
        val form =
            transport.requests
                .last()
                .body!!
                .toString(Charsets.UTF_8)
        assertTrue(form.contains("client_id=public-client"))
        assertTrue(form.contains("grant_type=authorization_code"))
        assertTrue(form.contains("code_verifier=verifier"))
        assertTrue(form.contains("code=code-value"))
    }

    @Test
    fun `inconsistent discovery prevents token exchange`() {
        val transport = RecordingTransport()
        transport.responses +=
            OAuthHttpResponse(
                issuer.discoveryUri,
                200,
                """{"issuer":"${issuer.uri}","token_endpoint":"https://evil.example/token"}"""
                    .toByteArray(),
            )
        val client = DatabricksOAuthClient(transport)

        assertThrows(DatabricksOAuthException.InvalidDiscovery::class.java) {
            client.exchange("code", attempt, issuer)
        }
        assertEquals(1, transport.requests.size)
    }

    @Test
    fun `refresh retains an omitted refresh token and issuer`() {
        val transport = RecordingTransport()
        transport.responses +=
            OAuthHttpResponse(
                issuer.tokenEndpoint,
                200,
                """{"access_token":"next","token_type":"bearer","expires_in":60}"""
                    .toByteArray(),
            )
        val tokens =
            DatabricksOAuthClient(transport) { 2_000L }
                .refresh("old-refresh", attempt.credentialScope, issuer)

        assertEquals("next", tokens.accessToken)
        assertEquals("old-refresh", tokens.refreshToken)
        assertEquals(issuer, tokens.issuer)
        val form =
            transport.requests
                .single()
                .body!!
                .toString(Charsets.UTF_8)
        assertTrue(form.contains("grant_type=refresh_token"))
        assertTrue(form.contains("refresh_token=old-refresh"))
    }

    @Test
    fun `only a validated invalid grant has the refresh-grant meaning`() {
        val invalidGrant = RecordingTransport()
        invalidGrant.responses +=
            OAuthHttpResponse(
                issuer.tokenEndpoint,
                400,
                """{"error":"invalid_grant"}""".toByteArray(),
            )
        assertThrows(DatabricksOAuthException.InvalidRefreshGrant::class.java) {
            DatabricksOAuthClient(invalidGrant).refresh(
                "old-refresh",
                attempt.credentialScope,
                issuer,
            )
        }

        val serverFailure = RecordingTransport()
        serverFailure.responses +=
            OAuthHttpResponse(
                issuer.tokenEndpoint,
                500,
                """{"error":"invalid_grant"}""".toByteArray(),
            )
        assertThrows(DatabricksOAuthException.TokenExchangeFailed::class.java) {
            DatabricksOAuthClient(serverFailure).refresh(
                "old-refresh",
                attempt.credentialScope,
                issuer,
            )
        }
    }

    @Test
    fun `transport redirects are rejected even when the response succeeds`() {
        val transport = RecordingTransport()
        transport.responses +=
            OAuthHttpResponse(
                URI("https://other.cloud.databricks.com/oidc/.well-known/openid-configuration"),
                200,
                byteArrayOf(),
            )

        assertThrows(DatabricksOAuthException.TokenExchangeFailed::class.java) {
            DatabricksOAuthClient(transport).exchange("code", attempt, issuer)
        }
    }

    @Test
    fun `token parser rejects malformed or incomplete responses`() {
        listOf(
            "{}",
            """{"access_token":"a","refresh_token":"r","token_type":"mac","expires_in":1}""",
            """{"access_token":"a b","refresh_token":"r","token_type":"bearer","expires_in":1}""",
            """{"access_token":"a","refresh_token":"r","token_type":"bearer","expires_in":0}""",
        ).forEach { body ->
            assertThrows(DatabricksOAuthException.InvalidTokenResponse::class.java) {
                DatabricksOAuthClient.tokens(body.toByteArray(), 0)
            }
        }
    }

    private class RecordingTransport : OAuthTransport {
        val requests = mutableListOf<OAuthHttpRequest>()
        val responses = ArrayDeque<OAuthHttpResponse>()

        override fun execute(request: OAuthHttpRequest): OAuthHttpResponse {
            requests += request
            return responses.removeFirst()
        }
    }
}
