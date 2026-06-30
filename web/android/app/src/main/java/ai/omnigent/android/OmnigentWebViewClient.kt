package ai.omnigent.android

import android.content.Intent
import android.graphics.Bitmap
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.webkit.WebViewFeature

/**
 * Injects the [NativeBridgeScript] into each page as early as Android allows and
 * signals [onPageReady] once a page has finished loading.
 *
 * Unlike iOS's `WKUserScript(.atDocumentStart)`, Android has no pre-JS injection
 * hook; `onPageStarted` fires after the first response byte. In practice that is
 * before the SPA's bundle evaluates, so `window.omnigentNative` is present by
 * the time React mounts and reads it. Anything that depends on the injected
 * emit-callbacks existing (notification replay, inset push) must wait for
 * [onPageReady], not fire eagerly at load time.
 */
class OmnigentWebViewClient(
    private val pinnedOrigin: () -> String?,
    private val onPageReady: (url: String?) -> Unit,
) : WebViewClient() {

    override fun onPageStarted(view: WebView, url: String?, favicon: Bitmap?) {
        super.onPageStarted(view, url, favicon)
        // Inject the window.omnigentNative facade ONLY where its transport exists:
        // on the pinned origin (never a foreign page reached via a top-level
        // redirect, which must not receive native->web emits) and only when the
        // web message listener is supported. Otherwise the web would see a dead
        // bridge and suppress its own Web Notifications / fallbacks.
        if (originOf(url) == pinnedOrigin() &&
            WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)
        ) {
            view.evaluateJavascript(NativeBridgeScript.source, null)
        }
    }

    override fun onPageFinished(view: WebView, url: String?) {
        super.onPageFinished(view, url)
        onPageReady(url)
    }

    override fun shouldOverrideUrlLoading(view: WebView, request: WebResourceRequest): Boolean {
        // Subframe loads (cross-origin iframes: web previews, OAuth, embeds) stay
        // inline. Safe because the native bridge is no longer injected into
        // subframes (origin-allowlisted web message listener).
        if (!request.isForMainFrame) return false

        // Same-origin top-level navigation stays in the WebView.
        if (originOf(request.url.toString()) == pinnedOrigin()) return false

        // Off-origin: hand to the system browser, but ALWAYS intercept (return
        // true) — fail closed. If no browser can handle it, we drop the link
        // rather than load a foreign origin into a WebView that exposes the
        // native bridges.
        runCatching {
            view.context.startActivity(Intent(Intent.ACTION_VIEW, request.url))
        }
        return true
    }
}
