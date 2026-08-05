package ai.omnigent.android

import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient

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
    private val onPageReady: (
        url: String?,
        mainFrameLoadFailed: Boolean,
        mainFramePersistenceFailed: Boolean,
    ) -> Unit,
    private val onLoginRequired: () -> Unit,
) : WebViewClient() {
    // Set when the main frame's current load errors out (network/TLS, or an
    // HTTP >=400 respectively); consumed by onPageFinished. WebView calls
    // onPageFinished with the ORIGINAL url after such an error — never a
    // chrome-error:// one — so callers can't tell success from failure from
    // the url alone; these flags are the only signal. They must NOT be reset
    // in onPageStarted: Chromium delivers a main-frame onReceivedHttpError
    // BEFORE onPageStarted (observed on device), so a start-time reset would
    // erase the error before the load it belongs to finishes.
    private var mainFrameLoadFailed = false
    private var mainFramePersistenceFailed = false

    override fun onPageStarted(
        view: WebView,
        url: String?,
        favicon: Bitmap?,
    ) {
        super.onPageStarted(view, url, favicon)

        val origin = originOf(url)
        val scheme = url?.let { Uri.parse(it).scheme?.lowercase() }

        // A real http(s) navigation to a foreign origin means the server bounced
        // us to the OIDC IdP and shouldOverrideUrlLoading didn't catch the
        // redirect. Stop and run native system-browser login (RFC 8252: never
        // authenticate in an embedded WebView; Google blocks it and passkeys don't
        // work). Idempotent: the login manager ignores a second start while one is
        // in flight.
        //
        // A null / about:blank / chrome-error:// URL is a failed or transitional
        // load of the pinned server (e.g. it's offline), NOT an IdP redirect —
        // don't misread it as a bounce and pop the browser. Mirror the http(s)
        // gate in shouldOverrideUrlLoading.
        if (isHttpScheme(scheme) && origin != pinnedOrigin()) {
            // Log origin only, never the full URL (carries OAuth state/PKCE).
            authLog("off-origin landing $origin -> login")
            view.stopLoading()
            onLoginRequired()
            return
        }
    }

    // request.isForMainFrame is only meaningful on this (API 23+, our floor
    // is 28) overload — the deprecated int/String one can't distinguish a
    // subframe (an embedded image, an iframe) failure from the page's own.
    override fun onReceivedError(
        view: WebView,
        request: WebResourceRequest,
        error: WebResourceError,
    ) {
        super.onReceivedError(view, request, error)
        if (request.isForMainFrame) mainFrameLoadFailed = true
    }

    override fun onReceivedHttpError(
        view: WebView,
        request: WebResourceRequest,
        errorResponse: WebResourceResponse,
    ) {
        super.onReceivedHttpError(view, request, errorResponse)
        if (request.isForMainFrame) mainFramePersistenceFailed = true
    }

    override fun onPageFinished(
        view: WebView,
        url: String?,
    ) {
        super.onPageFinished(view, url)
        // Consume the error flags for the load that just finished. A stopped
        // load (no onPageFinished) can leave a flag armed for the NEXT finish;
        // that fails safe — it can only skip one persist, never allow one.
        val loadFailed = mainFrameLoadFailed
        val persistenceFailed = mainFramePersistenceFailed
        mainFrameLoadFailed = false
        mainFramePersistenceFailed = false
        if (originOf(url) == pinnedOrigin() && shouldInjectBridgeAtPageReady()) {
            view.evaluateJavascript(
                NativeBridgeScript.source,
            ) { onPageReady(url, loadFailed, persistenceFailed) }
            return
        }
        onPageReady(url, loadFailed, persistenceFailed)
    }

    override fun shouldOverrideUrlLoading(
        view: WebView,
        request: WebResourceRequest,
    ): Boolean {
        val url = request.url
        val scheme = url.scheme?.lowercase()

        // Subframes (cross-origin iframes: web previews, embeds) load inline.
        if (!request.isForMainFrame) return false

        // Non-http(s) schemes (mailto:, tel:, intent:, custom links) can't load in
        // the WebView — hand to the system, fail-closed if nothing handles them.
        if (!isHttpScheme(scheme)) {
            runCatching { view.context.startActivity(Intent(Intent.ACTION_VIEW, url)) }
            return true
        }

        // Same-origin app pages load in the WebView.
        val origin = originOf(url.toString())
        if (origin == pinnedOrigin()) return false

        // Off-origin top-level navigation. A server redirect (no user gesture) is
        // the OIDC flow bouncing to the IdP -> run native system-browser login. A
        // user gesture is an external link -> hand to the system browser. Either
        // way the foreign page never loads in this WebView (which holds the
        // native bridge).
        authLog("off-origin nav $origin gesture=${request.hasGesture()}")
        if (request.hasGesture()) {
            runCatching { view.context.startActivity(Intent(Intent.ACTION_VIEW, url)) }
        } else {
            onLoginRequired()
        }
        return true
    }
}
