package ai.omnigent.android

import android.content.Context
import android.net.Uri
import android.webkit.ValueCallback
import android.webkit.WebResourceRequest
import android.webkit.WebView
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class OmnigentWebViewClientTest {
    @Test
    fun `page start does not inject into the outgoing document`() {
        val webView = RecordingWebView(ApplicationProvider.getApplicationContext())
        val client = client(shouldInjectBridgeAtPageReady = false)

        client.onPageStarted(webView, PINNED_URL, null)

        assertNull(webView.evaluatedScript)
    }

    @Test
    fun `fallback injects the facade before declaring the page ready`() {
        val webView = RecordingWebView(ApplicationProvider.getApplicationContext())
        var readyUrl: String? = null
        val client =
            client(shouldInjectBridgeAtPageReady = true) { url ->
                readyUrl = url
            }

        client.onPageFinished(webView, PINNED_URL)

        assertEquals(NativeBridgeScript.source, webView.evaluatedScript)
        assertNull(readyUrl)

        webView.completeEvaluation()
        assertEquals(PINNED_URL, readyUrl)
    }

    @Test
    fun `idp redirect loads inline when the server authenticates in the webview`() {
        val webView = RecordingWebView(ApplicationProvider.getApplicationContext())
        var logins = 0
        val client = client(pinnedOrigin = DATABRICKS_ORIGIN, onLoginRequired = { logins++ })

        val handled = client.shouldOverrideUrlLoading(webView, request(IDP_URL))

        assertFalse(handled) // false = let the WebView load the IdP page
        assertEquals(0, logins)
    }

    @Test
    fun `idp redirect goes to the system browser for other servers`() {
        val webView = RecordingWebView(ApplicationProvider.getApplicationContext())
        var logins = 0
        val client = client(onLoginRequired = { logins++ })

        val handled = client.shouldOverrideUrlLoading(webView, request(IDP_URL))

        assertTrue(handled)
        assertEquals(1, logins)
    }

    @Test
    fun `tapped external link on the app page leaves the webview`() {
        val webView = RecordingWebView(ApplicationProvider.getApplicationContext())
        webView.currentUrl = "$DATABRICKS_ORIGIN/app"
        var logins = 0
        val client = client(pinnedOrigin = DATABRICKS_ORIGIN, onLoginRequired = { logins++ })

        val handled =
            client.shouldOverrideUrlLoading(
                webView,
                request("https://example.org/docs", hasGesture = true),
            )

        assertTrue(handled)
        assertEquals(0, logins)
    }

    @Test
    fun `tapping sign-in on the idp page stays inline`() {
        val webView = RecordingWebView(ApplicationProvider.getApplicationContext())
        webView.currentUrl = IDP_URL // already mid-login on the IdP
        var logins = 0
        val client = client(pinnedOrigin = DATABRICKS_ORIGIN, onLoginRequired = { logins++ })

        // A button press inside the IdP is off-origin AND gesture-driven; it must
        // not be mistaken for an external link.
        val handled =
            client.shouldOverrideUrlLoading(
                webView,
                request("https://databricks.okta.com/login/step-up", hasGesture = true),
            )

        assertFalse(handled)
        assertEquals(0, logins)
    }

    @Test
    fun `off-origin landing keeps loading when the server authenticates in the webview`() {
        val webView = RecordingWebView(ApplicationProvider.getApplicationContext())
        var logins = 0
        val client = client(pinnedOrigin = DATABRICKS_ORIGIN, onLoginRequired = { logins++ })

        client.onPageStarted(webView, IDP_URL, null)

        assertFalse(webView.stopLoadingCalled)
        assertEquals(0, logins)
    }

    @Test
    fun `off-origin landing is stopped for other servers`() {
        val webView = RecordingWebView(ApplicationProvider.getApplicationContext())
        var logins = 0
        val client = client(onLoginRequired = { logins++ })

        client.onPageStarted(webView, IDP_URL, null)

        assertTrue(webView.stopLoadingCalled)
        assertEquals(1, logins)
    }

    private fun client(
        shouldInjectBridgeAtPageReady: Boolean = false,
        pinnedOrigin: String = PINNED_ORIGIN,
        onLoginRequired: () -> Unit = {},
        onPageReady: (String?) -> Unit = {},
    ) = OmnigentWebViewClient(
        pinnedOrigin = { pinnedOrigin },
        shouldInjectBridgeAtPageReady = { shouldInjectBridgeAtPageReady },
        onPageReady = onPageReady,
        onLoginRequired = onLoginRequired,
    )

    private fun request(
        url: String,
        hasGesture: Boolean = false,
    ) = object : WebResourceRequest {
        override fun getUrl(): Uri = Uri.parse(url)

        override fun isForMainFrame(): Boolean = true

        override fun isRedirect(): Boolean = !hasGesture

        override fun hasGesture(): Boolean = hasGesture

        override fun getMethod(): String = "GET"

        override fun getRequestHeaders(): Map<String, String> = emptyMap()
    }

    private class RecordingWebView(
        context: Context,
    ) : WebView(context) {
        var evaluatedScript: String? = null
        var stopLoadingCalled = false
        var currentUrl: String? = null
        private var callback: ValueCallback<String>? = null

        override fun getUrl(): String? = currentUrl

        override fun stopLoading() {
            stopLoadingCalled = true
        }

        override fun evaluateJavascript(
            script: String,
            resultCallback: ValueCallback<String>?,
        ) {
            evaluatedScript = script
            callback = resultCallback
        }

        fun completeEvaluation() {
            callback?.onReceiveValue("null")
        }
    }

    private companion object {
        const val PINNED_ORIGIN = "https://example.com"
        const val PINNED_URL = "$PINNED_ORIGIN/app"
        const val DATABRICKS_ORIGIN = "https://myshard.cloud.databricks.com"
        const val IDP_URL = "https://databricks.okta.com/authorize?state=abc"
    }
}
