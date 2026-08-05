package ai.omnigent.android

import android.net.Uri
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class OmnigentWebViewHttpErrorTest {
    @Test
    fun `main frame HTTP errors fail persistence without becoming load errors`() {
        val webView = WebView(ApplicationProvider.getApplicationContext())
        var loadFailed = true
        var persistenceFailed = false
        val client =
            client { _, load, persistence ->
                loadFailed = load
                persistenceFailed = persistence
            }

        client.onPageStarted(webView, PINNED_URL, null)
        client.onReceivedHttpError(
            webView,
            request(PINNED_URL, mainFrame = true),
            WebResourceResponse("text/html", "UTF-8", null),
        )
        client.onPageFinished(webView, PINNED_URL)

        assertFalse(loadFailed)
        assertTrue(persistenceFailed)
    }

    @Test
    fun `subresource HTTP errors do not fail persistence`() {
        val webView = WebView(ApplicationProvider.getApplicationContext())
        var persistenceFailed = true
        val client = client { _, _, persistence -> persistenceFailed = persistence }

        client.onPageStarted(webView, PINNED_URL, null)
        client.onReceivedHttpError(
            webView,
            request("$PINNED_ORIGIN/image.png", mainFrame = false),
            WebResourceResponse("image/png", null, null),
        )
        client.onPageFinished(webView, PINNED_URL)

        assertFalse(persistenceFailed)
    }

    @Test
    fun `main frame HTTP error arriving before onPageStarted still fails persistence`() {
        // Chromium delivers a main-frame onReceivedHttpError BEFORE onPageStarted
        // (observed on device) — a start-time flag reset would erase it.
        val webView = WebView(ApplicationProvider.getApplicationContext())
        var persistenceFailed = false
        val client = client { _, _, persistence -> persistenceFailed = persistence }

        client.onReceivedHttpError(
            webView,
            request(PINNED_URL, mainFrame = true),
            WebResourceResponse("text/html", "UTF-8", null),
        )
        client.onPageStarted(webView, PINNED_URL, null)
        client.onPageFinished(webView, PINNED_URL)

        assertTrue(persistenceFailed)
    }

    @Test
    fun `error flags are consumed by onPageFinished so a retry reports clean`() {
        val webView = WebView(ApplicationProvider.getApplicationContext())
        var loadFailed = true
        var persistenceFailed = true
        val client =
            client { _, load, persistence ->
                loadFailed = load
                persistenceFailed = persistence
            }

        client.onReceivedHttpError(
            webView,
            request(PINNED_URL, mainFrame = true),
            WebResourceResponse("text/html", "UTF-8", null),
        )
        client.onPageStarted(webView, PINNED_URL, null)
        client.onPageFinished(webView, PINNED_URL)
        // Retry succeeds: the consumed flags must not leak into this load.
        client.onPageStarted(webView, PINNED_URL, null)
        client.onPageFinished(webView, PINNED_URL)

        assertFalse(loadFailed)
        assertFalse(persistenceFailed)
    }

    @Test
    fun `HTTP error tracking does not block a login redirect`() {
        val webView = WebView(ApplicationProvider.getApplicationContext())
        var loginRequired = false
        val client = client(onLoginRequired = { loginRequired = true })

        client.onPageStarted(webView, PINNED_URL, null)
        client.onReceivedHttpError(
            webView,
            request(PINNED_URL, mainFrame = true),
            WebResourceResponse("text/html", "UTF-8", null),
        )
        client.onPageStarted(webView, "https://login.example/oidc", null)

        assertTrue(loginRequired)
    }

    private fun client(
        onLoginRequired: () -> Unit = {},
        onPageReady: (String?, Boolean, Boolean) -> Unit = { _, _, _ -> },
    ) = OmnigentWebViewClient(
        pinnedOrigin = { PINNED_ORIGIN },
        shouldInjectBridgeAtPageReady = { false },
        onPageReady = onPageReady,
        onLoginRequired = onLoginRequired,
    )

    private fun request(
        url: String,
        mainFrame: Boolean,
    ): WebResourceRequest =
        object : WebResourceRequest {
            override fun getUrl(): Uri = Uri.parse(url)

            override fun isForMainFrame(): Boolean = mainFrame

            override fun isRedirect(): Boolean = false

            override fun hasGesture(): Boolean = false

            override fun getMethod(): String = "GET"

            override fun getRequestHeaders(): Map<String, String> = emptyMap()
        }

    private companion object {
        const val PINNED_ORIGIN = "https://example.com"
        const val PINNED_URL = "$PINNED_ORIGIN/app"
    }
}
