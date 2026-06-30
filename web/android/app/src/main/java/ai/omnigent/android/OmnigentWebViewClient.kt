package ai.omnigent.android

import android.content.Intent
import android.graphics.Bitmap
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.webkit.WebViewFeature

/**
 * Injects the [NativeBridgeScript] facade on the pinned origin, signals
 * [onPageReady] once a pinned-origin page finishes loading, and routes the OIDC
 * login flow to a Custom Tab via [onLoginRequired].
 *
 * Unlike iOS's `WKUserScript(.atDocumentStart)`, Android has no pre-JS injection
 * hook; `onPageStarted` fires after the first response byte — in practice before
 * the SPA's bundle evaluates, so `window.omnigentNative` is present by the time
 * React mounts. Anything depending on the injected emit-callbacks (notification
 * replay, inset push) waits for [onPageReady].
 */
class OmnigentWebViewClient(
    private val pinnedOrigin: () -> String?,
    private val onPageReady: (url: String?) -> Unit,
    private val onLoginRequired: () -> Unit,
) : WebViewClient() {

    override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) {
        super.onPageStarted(view, url, favicon)

        // Landed on a foreign origin — the server bounced us to the OIDC IdP and
        // shouldOverrideUrlLoading didn't catch the redirect. Stop and run native
        // Custom Tabs login (RFC 8252: never authenticate in an embedded WebView;
        // Google blocks it and passkeys don't work). Idempotent: the login
        // manager ignores a second start while one is in flight.
        if (originOf(url) != pinnedOrigin()) {
            view.stopLoading()
            onLoginRequired()
            return
        }

        // Inject the facade ONLY on the pinned origin and only when the web
        // message listener is supported — otherwise the web would see a dead
        // bridge and suppress its own Web Notifications / fallbacks.
        if (WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) {
            view.evaluateJavascript(NativeBridgeScript.source, null)
        }
    }

    override fun onPageFinished(view: WebView, url: String?) {
        super.onPageFinished(view, url)
        onPageReady(url)
    }

    override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
        val url = request.url
        val scheme = url.scheme?.lowercase()

        // Subframes (cross-origin iframes: web previews, embeds) load inline.
        if (!request.isForMainFrame) return false

        // Non-http(s) schemes (mailto:, tel:, intent:, custom links) can't load in
        // the WebView — hand to the system, fail-closed if nothing handles them.
        if (scheme != "http" && scheme != "https") {
            runCatching { view.context.startActivity(Intent(Intent.ACTION_VIEW, url)) }
            return true
        }

        // Same-origin app pages load in the WebView.
        if (originOf(url.toString()) == pinnedOrigin()) return false

        // Off-origin top-level navigation. A server redirect (no user gesture) is
        // the OIDC flow bouncing to the IdP -> run native Custom Tabs login. A
        // user gesture is an external link -> hand to the system browser. Either
        // way the foreign page never loads in this WebView (which holds the
        // native bridge).
        if (request.hasGesture()) {
            runCatching { view.context.startActivity(Intent(Intent.ACTION_VIEW, url)) }
        } else {
            onLoginRequired()
        }
        return true
    }
}
