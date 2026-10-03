package ai.omnigent.android

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.net.URI
import java.util.Base64

@RunWith(RobolectricTestRunner::class)
class DatabricksOAuthAttemptTest {
    private val configuration =
        DatabricksOAuthConfiguration(
            "public-client",
            URI("https://login.databricks.com/mobile-redirect"),
        )
    private val attempt =
        DatabricksOAuthAttempt(
            configuration = configuration,
            credentialScope =
                DatabricksCredentialScope.from(
                    URI("https://dbc-123.cloud.databricks.com/omnigent?o=42"),
                    configuration,
                ),
            state = "state-value",
            verifier = "verifier-value",
        )

    @Test
    fun `PKCE challenge matches RFC 7636 vector`() {
        assertEquals(
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            DatabricksOAuthAttempt.challenge(
                "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
            ),
        )
    }

    @Test
    fun `authorization request includes only OAuth fields and workspace hint`() {
        val uri = attempt.authorizationUri
        val fields = queryItems(uri).toMap()

        assertEquals(
            URI("https://dbc-123.cloud.databricks.com/oidc/v1/authorize"),
            URI(uri.scheme, uri.authority, uri.path, null, null),
        )
        assertEquals("public-client", fields["client_id"])
        assertEquals(configuration.redirectUri.toString(), fields["redirect_uri"])
        assertEquals("code", fields["response_type"])
        assertEquals("all-apis offline_access", fields["scope"])
        assertEquals("state-value", fields["state"])
        assertEquals("S256", fields["code_challenge_method"])
        assertEquals("42", fields["o"])
        assertEquals(8, fields.size)
    }

    @Test
    fun `callback validates state and falls back to workspace issuer`() {
        val response =
            attempt.authorizationResponse(
                URI("https://login.databricks.com/mobile-redirect?code=abc&state=state-value"),
            )

        assertEquals("abc", response.code)
        assertEquals(
            URI("https://dbc-123.cloud.databricks.com/oidc"),
            response.issuer.uri,
        )
    }

    @Test
    fun `callback retains a supported account issuer`() {
        val issuer = "https%3A%2F%2Faccounts.cloud.databricks.com%2Foidc%2Faccounts%2Facct_1"
        val response =
            attempt.authorizationResponse(
                URI(
                    "https://login.databricks.com/mobile-redirect" +
                        "?code=abc&state=state-value&iss=$issuer",
                ),
            )

        assertEquals(
            URI("https://accounts.cloud.databricks.com/oidc/accounts/acct_1"),
            response.issuer.uri,
        )
    }

    @Test
    fun `callback rejects wrong destination state duplicates and provider errors`() {
        val invalidCallbacks =
            listOf(
                "https://evil.example/mobile-redirect?code=abc&state=state-value",
                "https://login.databricks.com/mobile-redirect?code=abc&state=wrong",
                "https://login.databricks.com/mobile-redirect?code=a&code=b&state=state-value",
                "https://login.databricks.com/mobile-redirect?code=abc&state=state-value&state=again",
                "https://login.databricks.com/mobile-redirect?state=state-value",
            )
        invalidCallbacks.forEach { callback ->
            assertThrows(DatabricksOAuthException.InvalidCallback::class.java) {
                attempt.authorizationResponse(URI(callback))
            }
        }
        assertThrows(DatabricksOAuthException.AuthorizationDenied::class.java) {
            attempt.authorizationResponse(
                URI(
                    "https://login.databricks.com/mobile-redirect" +
                        "?error=access_denied&state=state-value",
                ),
            )
        }
        assertThrows(DatabricksOAuthException.AuthorizationFailed::class.java) {
            attempt.authorizationResponse(
                URI(
                    "https://login.databricks.com/mobile-redirect" +
                        "?error=server_error&state=state-value",
                ),
            )
        }
    }

    @Test
    fun `fresh attempts encode mobile redirect scheme and random nonce in state`() {
        var seed = 0
        val created =
            DatabricksOAuthAttempt.create(
                URI("https://dbc-123.cloud.databricks.com"),
                configuration,
            ) { size -> ByteArray(size) { (seed++).toByte() } }

        val stateDocument =
            JSONObject(String(Base64.getDecoder().decode(created.state)))
        assertEquals("ai.omnigent.android", stateDocument.getString("scheme"))
        assertTrue(stateDocument.getString("nonce").matches(Regex("[A-Za-z0-9_-]{43}")))
        assertTrue(created.verifier.matches(Regex("[A-Za-z0-9_-]{43}")))
        assertTrue(created.authorizationUri.rawQuery.contains("state=${formEncode(created.state)}"))
    }
}
