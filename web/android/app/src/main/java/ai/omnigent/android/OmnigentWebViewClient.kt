package ai.omnigent.android

import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.net.http.SslError
import android.os.SystemClock
import android.webkit.RenderProcessGoneDetail
import android.webkit.SslErrorHandler
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient

enum class ProxyAuthState {
    IDLE,
    IN_FLIGHT,
    REFUSED,
}

/**
 * Signals [onPageReady] once a pinned-origin page finishes loading and routes
 * the OIDC login flow to the system browser via [onLoginRequired].
 *
 * The facade is normally registered with `addDocumentStartJavaScript` in
 * `MainActivity`. Older WebViews that support the message listener but not
 * document-start scripts inject it after the pinned page finishes.
 */
class OmnigentWebViewClient(
    private val pinnedOrigin: () -> String?,
    private val shouldInjectBridgeAtPageReady: () -> Boolean,
    private val onPageReady: (url: String?) -> Unit,
    private val onLoginRequired: () -> Unit,
    private val onNavigationStarted: () -> Unit,
    private val onProxyAuthFlowEnded: () -> Unit,
    private val clock: () -> Long = { SystemClock.elapsedRealtime() },
    private val onEmbeddedSignInUnsupported: () -> Unit,
    private val onWebViewUnusable: () -> Unit,
) : WebViewClient() {
    private var proxyAuthState = ProxyAuthState.IDLE
    private var flowStartedAt = 0L

    // Error callbacks may arrive after a server switch, so exits require the
    // URL last started while the current flow was in flight.
    private var trackedMainFrameUrl: String? = null

    // Whether the tracked hop reached onPageStarted. A committed hop renders
    // its response even on an error status (an IdP's 401 login form), so only
    // an HTTP error on a hop that never committed may end the flow.
    private var trackedMainFrameCommitted = false

    // The self-stop ledger describes WebView loading, independently of auth.
    private var activeMainFrameUrl: String? = null
    private var isLoading = false
    private var lastSelfStoppedUrl: String? = null
    private var awaitedPageOrigin: String? = null

    /** Stop the current load while owning the compatibility finish it causes. */
    fun stopLoadingAndLedger(view: WebView) {
        if (isLoading) lastSelfStoppedUrl = activeMainFrameUrl
        view.stopLoading()
    }

    /** Ignore callbacks from the previous navigation until the new server starts. */
    fun resetForOriginChange(newOrigin: String) {
        endProxyAuth()
        awaitedPageOrigin = newOrigin
    }

    private fun enterProxyAuth() {
        if (proxyAuthState != ProxyAuthState.IDLE) return
        proxyAuthState = ProxyAuthState.IN_FLIGHT
        flowStartedAt = clock()
    }

    /** End any flow silently — the reset for server switches, expiry, and teardown. */
    fun endProxyAuth() {
        proxyAuthState = ProxyAuthState.IDLE
        flowStartedAt = 0L
        trackedMainFrameUrl = null
        trackedMainFrameCommitted = false
        lastSelfStoppedUrl = null
    }

    private fun expireProxyAuthIfNeeded() {
        if (proxyAuthState == ProxyAuthState.IN_FLIGHT &&
            clock() - flowStartedAt > PROXY_AUTH_DEADLINE_MILLIS
        ) {
            endProxyAuth()
        }
    }

    override fun onPageStarted(
        view: WebView,
        url: String?,
        favicon: Bitmap?,
    ) {
        super.onPageStarted(view, url, favicon)
        onNavigationStarted()
        expireProxyAuthIfNeeded()

        // No pin means no trust decision is possible — never load inline.
        val pin = pinnedOrigin()
        if (pin == null) {
            stopLoadingAndLedger(view)
            return
        }
        val awaitedOrigin = awaitedPageOrigin
        if (awaitedOrigin != null) {
            if (originOf(url) != awaitedOrigin) return
            awaitedPageOrigin = null
        }

        activeMainFrameUrl = url
        isLoading = true

        if (proxyAuthState == ProxyAuthState.REFUSED) return

        val origin = originOf(url)
        val isOffOrigin =
            isHttpScheme(url?.let { Uri.parse(it).scheme }) && origin != pin

        if (proxyAuthState == ProxyAuthState.IDLE &&
            isOffOrigin &&
            isProxyAuthUrl(url, pin)
        ) {
            enterProxyAuth()
        }

        if (proxyAuthState == ProxyAuthState.IN_FLIGHT) {
            trackedMainFrameUrl = url
            trackedMainFrameCommitted = true
            if (isEmbeddedSignInUnsupported(url)) {
                proxyAuthState = ProxyAuthState.REFUSED
                stopLoadingAndLedger(view)
                onEmbeddedSignInUnsupported()
                return
            }
        }

        if (isOffOrigin) {
            if (proxyAuthState == ProxyAuthState.IN_FLIGHT) {
                authLog("proxy-auth landing $origin — loading inline")
                return
            }

            authLog("off-origin landing $origin -> login")
            stopLoadingAndLedger(view)
            onLoginRequired()
        }
    }

    override fun onPageFinished(
        view: WebView,
        url: String?,
    ) {
        super.onPageFinished(view, url)

        if (isLoading && activeMainFrameUrl == url) isLoading = false

        // Any main-frame finish clears the ledger; a matching one is also
        // consumed — it must not end the flow the shell's own stop preceded.
        val consumesSelfStoppedFinish = lastSelfStoppedUrl != null && lastSelfStoppedUrl == url
        lastSelfStoppedUrl = null
        val pin = pinnedOrigin() ?: return
        if (!consumesSelfStoppedFinish &&
            proxyAuthState == ProxyAuthState.IN_FLIGHT &&
            originOf(url) == pin
        ) {
            endProxyAuth()
            onProxyAuthFlowEnded()
        }

        if (originOf(url) == pin && shouldInjectBridgeAtPageReady()) {
            view.evaluateJavascript(NativeBridgeScript.source) { onPageReady(url) }
            return
        }
        onPageReady(url)
    }

    override fun shouldOverrideUrlLoading(
        view: WebView,
        request: WebResourceRequest,
    ): Boolean {
        val url = request.url

        // Subframes (cross-origin iframes: web previews, embeds) load inline.
        if (!request.isForMainFrame) return false

        expireProxyAuthIfNeeded()

        val urlString = url.toString()
        val origin = originOf(urlString)

        // Non-http(s) schemes must be handed to an installed system handler.
        if (!isHttpScheme(url.scheme)) {
            openExternally(view, url)
            return true
        }

        val pin = pinnedOrigin() ?: return true

        if (proxyAuthState == ProxyAuthState.REFUSED) {
            return origin != pin
        }

        if (origin == pin) return false

        // Track each hop allowed to proceed so a pre-commit failure (delivered
        // to handleReceivedError without any onPageStarted) still exits the flow.
        if (proxyAuthState == ProxyAuthState.IN_FLIGHT) {
            trackedMainFrameUrl = urlString
            trackedMainFrameCommitted = false
            authLog("proxy-auth nav $origin — loading inline")
            return false
        }

        if (isProxyAuthUrl(urlString, pin)) {
            if (request.isRedirect) {
                enterProxyAuth()
                trackedMainFrameUrl = urlString
                trackedMainFrameCommitted = false
                authLog("proxy-auth nav $origin — loading inline")
                return false
            }
            if (!request.hasGesture()) return false

            authLog("proxy-shaped external nav $origin")
            openExternally(view, url)
            return true
        }

        authLog("off-origin nav $origin gesture=${request.hasGesture()}")
        if (request.hasGesture()) {
            openExternally(view, url)
        } else {
            onLoginRequired()
        }
        return true
    }

    override fun onReceivedError(
        view: WebView,
        request: WebResourceRequest,
        error: WebResourceError,
    ) {
        super.onReceivedError(view, request, error)
        handleReceivedError(request)
    }

    override fun onReceivedHttpError(
        view: WebView,
        request: WebResourceRequest,
        errorResponse: WebResourceResponse,
    ) {
        super.onReceivedHttpError(view, request, errorResponse)
        handleReceivedError(request, httpError = true)
    }

    override fun onReceivedSslError(
        view: WebView,
        handler: SslErrorHandler,
        error: SslError,
    ) {
        super.onReceivedSslError(view, handler, error)
        // Only a certificate failure on the flow's own main-frame hop kills
        // the flow; a subresource or stale-navigation error must not misroute
        // the next off-origin redirect into native login.
        if (proxyAuthState == ProxyAuthState.IN_FLIGHT && error.url == trackedMainFrameUrl) {
            endProxyAuth()
        }
    }

    override fun onRenderProcessGone(
        view: WebView,
        detail: RenderProcessGoneDetail,
    ): Boolean {
        activeMainFrameUrl = null
        isLoading = false
        endProxyAuth()
        onWebViewUnusable()
        return true
    }

    /**
     * Settle the trackers for a main-frame load that ended in an error, from
     * either error callback. `internal` because [WebResourceError] has no
     * constructor tests can reach.
     */
    internal fun handleReceivedError(
        request: WebResourceRequest,
        httpError: Boolean = false,
    ) {
        if (!request.isForMainFrame) return

        val requestUrl = request.url.toString()
        if (activeMainFrameUrl == requestUrl) isLoading = false
        // A committed hop renders its body even on an error status — an IdP's
        // 401/403 interactive login page — so only a hop that never reached
        // onPageStarted lets an HTTP error end the flow.
        if (httpError && trackedMainFrameCommitted) return
        if (proxyAuthState == ProxyAuthState.IN_FLIGHT && trackedMainFrameUrl == requestUrl) {
            endProxyAuth()
        }
    }

    // Hand a URL to the system, fail-closed if nothing handles it.
    private fun openExternally(
        view: WebView,
        url: Uri,
    ) {
        runCatching { view.context.startActivity(Intent(Intent.ACTION_VIEW, url)) }
    }

    private fun isEmbeddedSignInUnsupported(url: String?): Boolean {
        val uri = url?.let(Uri::parse) ?: return false
        if (uri.isOpaque) return false
        return hasEmbeddedRejectionParam(uri.encodedQuery) ||
            hasEmbeddedRejectionParam(uri.encodedFragment)
    }

    /**
     * True when [component] carries the rejection as an error parameter's
     * value, directly or inside a redirect parameter one encoding level deep.
     * A benign parameter that merely mentions the token (`state=...`) must not
     * abort authentication.
     */
    private fun hasEmbeddedRejectionParam(
        component: String?,
        depth: Int = 0,
    ): Boolean {
        if (component.isNullOrEmpty()) return false
        for (pair in component.split('&')) {
            val key = Uri.decode(pair.substringBefore('='))
            val value = Uri.decode(pair.substringAfter('=', missingDelimiterValue = ""))
            if (key in EMBEDDED_REJECTION_KEYS && value == EMBEDDED_REJECTION) return true
            if (depth > 0) continue
            val nested = runCatching { Uri.parse(value) }.getOrNull() ?: continue
            if (nested.isOpaque) continue
            if (hasEmbeddedRejectionParam(nested.encodedQuery, depth + 1) ||
                hasEmbeddedRejectionParam(nested.encodedFragment, depth + 1)
            ) {
                return true
            }
        }
        return false
    }

    private companion object {
        const val EMBEDDED_REJECTION = "disallowed_useragent"
        val EMBEDDED_REJECTION_KEYS = setOf("error", "error_subtype")
        const val PROXY_AUTH_DEADLINE_MILLIS = 6 * 60_000L
    }
}
