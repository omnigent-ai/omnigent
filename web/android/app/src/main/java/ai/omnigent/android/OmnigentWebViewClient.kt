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
        val scheme = request.url.scheme?.lowercase()

        // Non-http(s) schemes (mailto:, tel:, intent:, custom app links) can't load
        // in the WebView — hand a top-level one to the system, fail-closed if
        // nothing handles it.
        if (request.isForMainFrame && scheme != "http" && scheme != "https") {
            runCatching {
                view.context.startActivity(Intent(Intent.ACTION_VIEW, request.url))
            }
            return true
        }

        // All http/https navigation loads in the WebView — same-origin app pages
        // AND the off-origin OIDC redirect chain (the server bounces the main frame
        // to the IdP for login, then back to its own callback that sets the session
        // cookie). Handing that redirect to an external browser instead (the prior
        // behavior) completed auth in Chrome, where the cookie landed — so the
        // WebView never got the session and login silently failed.
        //
        // Mirrors the iOS shell, which likewise lets top-level http/https load in
        // the WKWebView. Safe here because the native bridge is origin-allowlisted
        // (addWebMessageListener) and the window.omnigentNative facade is injected
        // only on the pinned origin, so a foreign auth page can't reach native.
        //
        // NOTE: an IdP that bounces to Google still hits Google's WebView block
        // (disallowed_useragent) — that needs a Custom-Tabs hand-off with a session
        // hand-back and is tracked as a follow-up.
        return false
    }
}
