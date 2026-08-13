package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

/** Bare Databricks workspace roots resolve to the `/omnigent` mount. */
@RunWith(RobolectricTestRunner::class)
class OriginsWorkspaceUiUrlTest {
    @Test
    fun `expands a bare workspace root`() {
        assertEquals(
            "https://dbc-a5d4177a-49dc.cloud.databricks.com/omnigent",
            databricksWorkspaceUiUrl("https://dbc-a5d4177a-49dc.cloud.databricks.com"),
        )
        assertEquals(
            "https://gtm-ai-agent.cloud.databricks.com/omnigent",
            databricksWorkspaceUiUrl("https://gtm-ai-agent.cloud.databricks.com/"),
        )
        assertEquals(
            "https://adb-123.azuredatabricks.net/omnigent",
            databricksWorkspaceUiUrl("https://adb-123.azuredatabricks.net"),
        )
    }

    @Test
    fun `keeps the workspace selector and fragment`() {
        assertEquals(
            "https://ws.cloud.databricks.com/omnigent?o=123#/c/abc",
            databricksWorkspaceUiUrl("https://ws.cloud.databricks.com/?o=123#/c/abc"),
        )
    }

    @Test
    fun `normalizes casing and the default port`() {
        assertEquals(
            "https://ws.cloud.databricks.com/omnigent",
            databricksWorkspaceUiUrl("https://WS.Cloud.Databricks.COM:443"),
        )
    }

    @Test
    fun `leaves a url that already carries a path alone`() {
        // Already on the mount, or a deliberate deep link — never override it.
        assertNull(databricksWorkspaceUiUrl("https://ws.cloud.databricks.com/omnigent"))
        assertNull(databricksWorkspaceUiUrl("https://ws.cloud.databricks.com/omnigent/c/abc"))
        assertNull(databricksWorkspaceUiUrl("https://ws.cloud.databricks.com/ml/dashboard"))
    }

    @Test
    fun `leaves non-workspace hosts alone`() {
        // Databricks Apps serve their own app at the root: no workspace mount.
        assertNull(databricksWorkspaceUiUrl("https://my-app.aws.databricksapps.com"))
        assertNull(databricksWorkspaceUiUrl("https://omnigent.example.com"))
        // Lookalike hosts must not match, same dot-boundary rule as the auth list.
        assertNull(databricksWorkspaceUiUrl("https://databricks.com.evil.tld"))
        assertNull(databricksWorkspaceUiUrl("https://notdatabricks.com"))
    }

    @Test
    fun `leaves unusable input alone`() {
        assertNull(databricksWorkspaceUiUrl(null))
        assertNull(databricksWorkspaceUiUrl("about:blank"))
        assertNull(databricksWorkspaceUiUrl("omnigent://ws.cloud.databricks.com"))
    }
}
