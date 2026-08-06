package ai.omnigent.android

import android.content.Context
import android.webkit.ValueCallback
import android.webkit.WebView
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
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
    fun `page start resets native layout before the new page finishes`() {
        val webView = RecordingWebView(ApplicationProvider.getApplicationContext())
        var navigationStarts = 0
        val client =
            client(
                shouldInjectBridgeAtPageReady = false,
                onNavigationStarted = { navigationStarts++ },
            )

        client.onPageStarted(webView, PINNED_URL, null)
        assertEquals(1, navigationStarts)

        client.onPageFinished(webView, PINNED_URL)
        assertEquals(1, navigationStarts)
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

    // onPageReady stays last so callers can pass it as a trailing lambda.
    private fun client(
        shouldInjectBridgeAtPageReady: Boolean,
        onNavigationStarted: () -> Unit = {},
        onPageReady: (String?) -> Unit = {},
    ) = OmnigentWebViewClient(
        pinnedOrigin = { PINNED_ORIGIN },
        shouldInjectBridgeAtPageReady = { shouldInjectBridgeAtPageReady },
        onPageReady = onPageReady,
        onLoginRequired = {},
        onNavigationStarted = onNavigationStarted,
    )

    private class RecordingWebView(
        context: Context,
    ) : WebView(context) {
        var evaluatedScript: String? = null
        private var callback: ValueCallback<String>? = null

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
    }
}
