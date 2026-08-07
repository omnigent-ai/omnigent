package ai.omnigent.android

import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient

/**
 * Signals [onPageReady] once a pinned-origin page finishes loading and decides
 * where the login flow runs: inline for servers matching [usesInWebViewAuth],
 * otherwise handed to the system browser via [onLoginRequired].
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
) : WebViewClient() {
    override fun onPageStarted(
        view: WebView,
        url: String?,
        favicon: Bitmap?,
    ) {
        super.onPageStarted(view, url, favicon)

        val origin = originOf(url)
        val scheme = url?.let { Uri.parse(it).scheme?.lowercase() }
        val pinned = pinnedOrigin()

        // A real http(s) navigation to a foreign origin means the server bounced
        // us to the IdP and shouldOverrideUrlLoading didn't catch the redirect.
        // Stop and run native system-browser login (RFC 8252 — required for IdPs
        // like Google that reject embedded user-agents). Idempotent: the login
        // manager ignores a second start while one is in flight. Servers that
        // authenticate in the WebView keep loading instead.
        //
        // A null / about:blank / chrome-error:// URL is a failed or transitional
        // load of the pinned server (e.g. it's offline), NOT an IdP redirect —
        // don't misread it as a bounce and pop the browser. Mirror the http(s)
        // gate in shouldOverrideUrlLoading.
        if (isHttpScheme(scheme) && origin != pinned && !usesInWebViewAuth(pinned)) {
            // Log origin only, never the full URL (carries OAuth state/PKCE).
            authLog("off-origin landing $origin -> login")
            view.stopLoading()
            onLoginRequired()
            return
        }
    }

    override fun onPageFinished(
        view: WebView,
        url: String?,
    ) {
        super.onPageFinished(view, url)
        if (originOf(url) == pinnedOrigin() && shouldInjectBridgeAtPageReady()) {
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
        val pinned = pinnedOrigin()
        if (origin == pinned) return false

        authLog("off-origin nav $origin gesture=${request.hasGesture()}")

        // In-WebView auth: the IdP flow runs inline — safe because the native
        // bridge is origin-allowlisted to the pinned origin by WebView itself, so
        // the IdP page can't reach it. Only a gesture from a pinned-origin page is
        // an external link; once we're on the IdP's own pages its navigations
        // (sign-in buttons, form posts, tenant hops) are all gesture-driven and
        // must stay inline or the flow ejects to the browser mid-login.
        if (usesInWebViewAuth(pinned)) {
            if (originOf(view.url) == pinned && request.hasGesture()) {
                runCatching { view.context.startActivity(Intent(Intent.ACTION_VIEW, url)) }
                return true
            }
            return false
        }

        // System-browser auth. A user gesture is an external link -> hand to the
        // system browser. A server redirect is the login flow bouncing to the IdP.
        if (request.hasGesture()) {
            runCatching { view.context.startActivity(Intent(Intent.ACTION_VIEW, url)) }
        } else {
            onLoginRequired()
        }
        return true
    }
}
