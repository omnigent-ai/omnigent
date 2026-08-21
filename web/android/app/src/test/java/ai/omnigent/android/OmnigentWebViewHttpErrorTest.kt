package ai.omnigent.android

import android.net.Uri
import android.webkit.TestWebResourceError
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
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
    fun `ERR_ABORTED does not classify a rendered page as failed`() {
        assertFalse(isBlockingLoadError(WebViewClient.ERROR_UNKNOWN, "net::ERR_ABORTED"))
    }

    @Test
    fun `main frame HTTP errors fail persistence without becoming load errors`() {
        val webView = WebView(ApplicationProvider.getApplicationContext())
        var loadFailed = true
        var persistenceFailed = false
        val client =
            client { _, load, persistence, _ ->
                loadFailed = load
                persistenceFailed = persistence
            }

        client.onPageStarted(webView, PINNED_URL, null)
        client.onReceivedHttpError(
            webView,
            request(PINNED_URL, mainFrame = true),
            WebResourceResponse("text/html", "UTF-8", null),
        )
        client.onPageCommitVisible(webView, PINNED_URL)
        client.onPageFinished(webView, PINNED_URL)

        assertFalse(loadFailed)
        assertTrue(persistenceFailed)
    }

    @Test
    fun `subresource HTTP errors do not fail persistence`() {
        val webView = WebView(ApplicationProvider.getApplicationContext())
        var persistenceFailed = true
        val client = client { _, _, persistence, _ -> persistenceFailed = persistence }

        client.onPageStarted(webView, PINNED_URL, null)
        client.onReceivedHttpError(
            webView,
            request("$PINNED_ORIGIN/image.png", mainFrame = false),
            WebResourceResponse("image/png", null, null),
        )
        client.onPageCommitVisible(webView, PINNED_URL)
        client.onPageFinished(webView, PINNED_URL)

        assertFalse(persistenceFailed)
    }

    @Test
    fun `main frame HTTP error arriving before onPageStarted still fails persistence`() {
        // Chromium delivers a main-frame onReceivedHttpError BEFORE onPageStarted
        // (observed on device) — a start-time flag reset would erase it.
        val webView = WebView(ApplicationProvider.getApplicationContext())
        var persistenceFailed = false
        val client = client { _, _, persistence, _ -> persistenceFailed = persistence }

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
            client { _, load, persistence, _ ->
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
        client.onPageCommitVisible(webView, PINNED_URL)
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

    @Test
    fun `foreign main frame errors do not poison the pinned load`() {
        val webView = WebView(ApplicationProvider.getApplicationContext())
        var loadFailed = true
        var persistenceFailed = true
        val client =
            client { _, load, persistence, _ ->
                loadFailed = load
                persistenceFailed = persistence
            }

        client.onPageStarted(webView, PINNED_URL, null)
        client.onReceivedError(webView, request("https://login.example/oidc", true), error())
        client.onReceivedHttpError(
            webView,
            request("https://login.example/oidc", true),
            WebResourceResponse("text/html", "UTF-8", null),
        )
        client.onPageCommitVisible(webView, PINNED_URL)
        client.onPageFinished(webView, PINNED_URL)

        assertFalse(loadFailed)
        assertFalse(persistenceFailed)
    }

    @Test
    fun `stale pinned document errors do not poison a replacement path`() {
        val webView = WebView(ApplicationProvider.getApplicationContext())
        var loadFailed = true
        var persistenceFailed = true
        val replacement = "$PINNED_ORIGIN/replacement"
        val client =
            client { _, load, persistence, _ ->
                loadFailed = load
                persistenceFailed = persistence
            }

        client.onPageStarted(webView, PINNED_URL, null)
        client.onPageStarted(webView, replacement, null)
        client.onReceivedError(webView, request(PINNED_URL, true), error())
        client.onReceivedHttpError(
            webView,
            request(PINNED_URL, true),
            WebResourceResponse("text/html", "UTF-8", null),
        )
        client.onPageCommitVisible(webView, replacement)
        client.onPageFinished(webView, replacement)

        assertFalse(loadFailed)
        assertFalse(persistenceFailed)
    }

    @Test
    fun `ambiguous same URL network error fails closed`() {
        val webView = WebView(ApplicationProvider.getApplicationContext())
        var loadFailed = false
        val client = client { _, load, _, _ -> loadFailed = load }
        client.expectLoad(1)
        client.onPageStarted(webView, PINNED_URL, null)

        client.supersedePendingLoads()
        client.expectLoad(2)
        client.onReceivedError(webView, request(PINNED_URL, true), error())
        client.onPageStarted(webView, PINNED_URL, null)
        client.onPageCommitVisible(webView, PINNED_URL)
        client.onPageFinished(webView, PINNED_URL)

        // WebView supplies no navigation identity here, so this could be either
        // the superseded request or generation 2. Never report ambiguous readiness.
        assertTrue(loadFailed)
    }

    @Test
    fun `ambiguous same URL HTTP error fails closed`() {
        val webView = WebView(ApplicationProvider.getApplicationContext())
        var persistenceFailed = false
        val client = client { _, _, persistence, _ -> persistenceFailed = persistence }
        client.expectLoad(1)
        client.onPageStarted(webView, PINNED_URL, null)

        client.supersedePendingLoads()
        client.expectLoad(2)
        client.onReceivedHttpError(
            webView,
            request(PINNED_URL, true),
            WebResourceResponse("text/html", "UTF-8", null),
        )
        client.onPageStarted(webView, PINNED_URL, null)
        client.onPageCommitVisible(webView, PINNED_URL)
        client.onPageFinished(webView, PINNED_URL)

        // A retry gets a fresh generation; generation 2 must not persist an
        // unverified server when callback attribution is ambiguous.
        assertTrue(persistenceFailed)
    }

    @Test
    fun `same URL replacement network failure before page start is not reported ready`() {
        val webView = WebView(ApplicationProvider.getApplicationContext())
        var loadFailed = false
        val client = client { _, load, _, _ -> loadFailed = load }
        client.expectLoad(1)
        client.onPageStarted(webView, PINNED_URL, null)

        client.supersedePendingLoads()
        client.expectLoad(2)
        client.onReceivedError(webView, request(PINNED_URL, true), error())
        client.onPageStarted(webView, PINNED_URL, null)
        client.onPageFinished(webView, PINNED_URL)

        assertTrue(loadFailed)
    }

    @Test
    fun `same URL replacement HTTP failure before page start is not reported ready`() {
        val webView = WebView(ApplicationProvider.getApplicationContext())
        var persistenceFailed = false
        val client = client { _, _, persistence, _ -> persistenceFailed = persistence }
        client.expectLoad(1)
        client.onPageStarted(webView, PINNED_URL, null)

        client.supersedePendingLoads()
        client.expectLoad(2)
        client.onReceivedHttpError(
            webView,
            request(PINNED_URL, true),
            WebResourceResponse("text/html", "UTF-8", null),
        )
        client.onPageStarted(webView, PINNED_URL, null)
        client.onPageFinished(webView, PINNED_URL)

        assertTrue(persistenceFailed)
    }

    @Test
    fun `replacement errors before its page start remain attributed`() {
        val webView = WebView(ApplicationProvider.getApplicationContext())
        var loadFailed = false
        var persistenceFailed = false
        val replacement = "$PINNED_ORIGIN/replacement"
        val client =
            client { _, load, persistence, _ ->
                loadFailed = load
                persistenceFailed = persistence
            }
        client.expectLoad(1)
        client.onPageStarted(webView, PINNED_URL, null)

        client.supersedePendingLoads()
        client.expectLoad(2)
        client.onReceivedError(webView, request(replacement, true), error())
        client.onReceivedHttpError(
            webView,
            request(replacement, true),
            WebResourceResponse("text/html", "UTF-8", null),
        )
        client.onPageStarted(webView, replacement, null)
        client.onPageFinished(webView, replacement)

        assertTrue(loadFailed)
        assertTrue(persistenceFailed)
    }

    private fun error(): WebResourceError = TestWebResourceError()

    private fun client(
        onLoginRequired: () -> Unit = {},
        onPageReady: (String?, Boolean, Boolean, Long?) -> Unit = { _, _, _, _ -> },
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
