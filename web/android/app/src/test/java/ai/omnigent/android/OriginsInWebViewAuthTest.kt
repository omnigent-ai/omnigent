package ai.omnigent.android

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class OriginsInWebViewAuthTest {
    @Test
    fun `matches the bare domain and its subdomains`() {
        assertTrue(usesInWebViewAuth("https://databricks.com"))
        assertTrue(usesInWebViewAuth("https://foo.cloud.databricks.com"))
        assertTrue(usesInWebViewAuth("https://adb-123.azuredatabricks.net"))
        assertTrue(usesInWebViewAuth("https://myapp.databricksapps.com"))
    }

    @Test
    fun `matches regardless of host casing`() {
        assertTrue(usesInWebViewAuth("https://Foo.Databricks.COM"))
    }

    @Test
    fun `does not match a lookalike parent domain`() {
        // The dot boundary is the whole point: a host that merely *contains* the
        // domain must not authenticate in the WebView.
        assertFalse(usesInWebViewAuth("https://databricks.com.example.org"))
        assertFalse(usesInWebViewAuth("https://notdatabricks.com"))
        assertFalse(usesInWebViewAuth("https://azuredatabricks.net.evil.tld"))
        assertFalse(usesInWebViewAuth("https://databricksapps.com.evil.tld"))
    }

    @Test
    fun `does not match unrelated or unusable origins`() {
        assertFalse(usesInWebViewAuth("https://example.com"))
        assertFalse(usesInWebViewAuth(null))
        assertFalse(usesInWebViewAuth("about:blank"))
    }
}
