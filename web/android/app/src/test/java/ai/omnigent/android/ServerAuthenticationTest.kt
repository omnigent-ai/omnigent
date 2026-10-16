package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class ServerAuthenticationTest {
    @Test
    fun `classifies Databricks workspace domains`() {
        assertEquals(
            ServerAuthentication.DATABRICKS_WORKSPACE,
            serverAuthentication("https://databricks.com"),
        )
        assertEquals(
            ServerAuthentication.DATABRICKS_WORKSPACE,
            serverAuthentication("https://foo.cloud.databricks.com"),
        )
        assertEquals(
            ServerAuthentication.DATABRICKS_WORKSPACE,
            serverAuthentication("https://adb-123.azuredatabricks.net"),
        )
        assertEquals(
            ServerAuthentication.DATABRICKS_WORKSPACE,
            serverAuthentication("https://Foo.Databricks.COM"),
        )
    }

    @Test
    fun `classifies Databricks Apps separately`() {
        assertEquals(
            ServerAuthentication.DATABRICKS_APP,
            serverAuthentication("https://myapp.databricksapps.com"),
        )
        assertEquals(
            ServerAuthentication.DATABRICKS_APP,
            serverAuthentication("https://DATABRICKSAPPS.COM"),
        )
    }

    @Test
    fun `lookalikes and unrelated origins use generic OIDC`() {
        listOf(
            "https://databricks.com.example.org",
            "https://notdatabricks.com",
            "https://azuredatabricks.net.evil.tld",
            "https://databricksapps.com.evil.tld",
            "https://example.com",
            null,
            "about:blank",
        ).forEach { origin ->
            assertEquals(ServerAuthentication.OIDC, serverAuthentication(origin))
        }
    }

    @Test
    fun `only Databricks Apps retain inline authentication`() {
        assertFalse(ServerAuthentication.DATABRICKS_WORKSPACE.usesInWebViewAuth)
        assertTrue(ServerAuthentication.DATABRICKS_APP.usesInWebViewAuth)
        assertFalse(ServerAuthentication.OIDC.usesInWebViewAuth)
    }
}
