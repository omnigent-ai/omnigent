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
class DatabricksOAuthConfigurationTest {
    private val configuration =
        DatabricksOAuthConfiguration(
            clientId = "public-client",
            redirectUri = URI("https://login.databricks.com/mobile-redirect"),
        )

    @Test
    fun `requires a public client id and exact HTTPS redirect`() {
        assertThrows(IllegalArgumentException::class.java) {
            DatabricksOAuthConfiguration("", configuration.redirectUri)
        }
        assertThrows(IllegalArgumentException::class.java) {
            DatabricksOAuthConfiguration("client id", configuration.redirectUri)
        }
        listOf(
            "http://login.databricks.com/mobile-redirect",
            "https://user@login.databricks.com/mobile-redirect",
            "https://login.databricks.com:8443/mobile-redirect",
            "https://login.databricks.com",
            "https://login.databricks.com/mobile-redirect?next=x",
            "https://login.databricks.com/mobile-redirect#fragment",
        ).forEach { redirect ->
            assertThrows(IllegalArgumentException::class.java) {
                DatabricksOAuthConfiguration("public-client", URI(redirect))
            }
        }
    }

    @Test
    fun `callback destination match is exact except host casing and default port`() {
        assertTrue(
            configuration.matchesCallback(
                URI("https://LOGIN.DATABRICKS.COM:443/mobile-redirect?code=x&state=y"),
            ),
        )
        assertFalse(
            configuration.matchesCallback(
                URI("https://login.databricks.com/mobile-redirect/extra?code=x"),
            ),
        )
        assertFalse(
            configuration.matchesCallback(
                URI("https://login.databricks.com/mobile-redirect?code=x#fragment"),
            ),
        )
    }

    @Test
    fun `credential scope normalizes origin and preserves one ASCII workspace id`() {
        val scope =
            DatabricksCredentialScope.from(
                URI("https://DBC-123.Cloud.Databricks.com:443/omnigent?o=00123#chat"),
                configuration,
            )

        assertEquals(URI("https://dbc-123.cloud.databricks.com"), scope.workspaceOrigin)
        assertEquals("00123", scope.workspaceId)
        assertEquals("public-client", scope.clientId)
        assertTrue(scope.account.startsWith("o:5:00123:"))
    }

    @Test
    fun `credential scope rejects ambiguous or malformed workspace ids`() {
        listOf(
            "https://dbc-123.cloud.databricks.com/?o=1&o=2",
            "https://dbc-123.cloud.databricks.com/?o=",
            "https://dbc-123.cloud.databricks.com/?o=12a",
            "https://dbc-123.cloud.databricks.com/?o=%D9%A1%D9%A2",
            "https://my-app.databricksapps.com/?o=12",
            "http://dbc-123.cloud.databricks.com/?o=12",
        ).forEach { url ->
            assertThrows(DatabricksOAuthException.InvalidWorkspace::class.java) {
                DatabricksCredentialScope.from(URI(url), configuration)
            }
        }
    }
}
