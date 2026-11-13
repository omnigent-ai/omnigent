package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import java.net.URI

@RunWith(RobolectricTestRunner::class)
class DatabricksOAuthIssuerTest {
    @Test
    fun `accepts workspace and account issuer shapes`() {
        val workspace = DatabricksOAuthIssuer(URI("https://dbc-123.cloud.databricks.com/oidc"))
        val account =
            DatabricksOAuthIssuer(
                URI("https://accounts.azuredatabricks.net/oidc/accounts/account_1-x"),
            )

        assertEquals(
            URI(
                "https://dbc-123.cloud.databricks.com/oidc/.well-known/openid-configuration",
            ),
            workspace.discoveryUri,
        )
        assertEquals(
            URI("https://accounts.azuredatabricks.net/oidc/accounts/account_1-x/v1/token"),
            account.tokenEndpoint,
        )
    }

    @Test
    fun `rejects unsupported issuer authority and paths`() {
        listOf(
            "http://dbc-123.cloud.databricks.com/oidc",
            "https://user@dbc-123.cloud.databricks.com/oidc",
            "https://dbc-123.cloud.databricks.com:8443/oidc",
            "https://dbc-123.cloud.databricks.com/oidc?x=1",
            "https://dbc-123.cloud.databricks.com/oidc#x",
            "https://dbc-123.cloud.databricks.com/oauth",
            "https://dbc-123.cloud.databricks.com/oidc/accounts/",
            "https://dbc-123.cloud.databricks.com/oidc/accounts/a%2Fb",
            "https://myapp.databricksapps.com/oidc",
            "https://databricks.com.example.org/oidc",
        ).forEach { value ->
            assertThrows(DatabricksOAuthException.InvalidIssuer::class.java) {
                DatabricksOAuthIssuer(URI(value))
            }
        }
    }

    @Test
    fun `discovery requires exact issuer and token endpoint`() {
        val issuer = DatabricksOAuthIssuer(URI("https://dbc-123.cloud.databricks.com/oidc"))
        issuer.validateDiscovery(
            """{"issuer":"${issuer.uri}","token_endpoint":"${issuer.tokenEndpoint}"}"""
                .toByteArray(),
        )

        listOf(
            """{"issuer":"https://other.cloud.databricks.com/oidc","token_endpoint":"${issuer.tokenEndpoint}"}""",
            """{"issuer":"${issuer.uri}","token_endpoint":"https://evil.example/token"}""",
            "{}",
            "not-json",
        ).forEach { body ->
            assertThrows(DatabricksOAuthException.InvalidDiscovery::class.java) {
                issuer.validateDiscovery(body.toByteArray())
            }
        }
    }
}
