package ai.omnigent.android

import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient

/**
 * Signals [onPageReady] once a pinned-origin page finishes loading and decides
 * where the login flow runs: inline for servers matching [usesInWebViewAuth],
 * otherwise handed to the system browser via [onLoginRequired]. A landing on a
 * bare Databricks workspace root is bounced to the workspace's `/omnigent`
 * mount (see [workspaceRootTarget]).
 *
 * The facade is normally registered with `addDocumentStartJavaScript` in
 * `MainActivity`. Older WebViews that support the message listener but not
 * document-start scripts inject it after the pinned page finishes.
 *
 * Also injects [WorkspaceChromeScript] once each pinned-origin document finishes,
 * so a workspace-hosted server's nav chrome stays hidden.
 */
class OmnigentWebViewClient(
    private val pinnedOrigin: () -> String?,
    private val shouldInjectBridgeAtPageReady: () -> Boolean,
    private val onPageReady: (
        url: String?,
        mainFrameLoadFailed: Boolean,
        mainFramePersistenceFailed: Boolean,
        loadGeneration: Long?,
    ) -> Unit,
    private val onLoginRequired: () -> Unit,
    private val loadUrl: (WebView, String) -> Unit = WebView::loadUrl,
) : WebViewClient() {
    // Bare-root -> /omnigent bounces since the last app page loaded; see
    // workspaceRootTarget for why they're capped.
    private var rootBounces = 0

    private val mainHandler = Handler(Looper.getMainLooper())

    private var expectedLoadGeneration: Long? = null
    private var activeLoad: ActiveLoad? = null
    private var preStartLoadFailed = false
    private var preStartPersistenceFailed = false
    private var ignoreUnmatchedFinish = false
    private var tracksDocuments = false

    fun expectLoad(generation: Long) {
        expectedLoadGeneration = generation
    }

    fun supersedePendingLoads() {
        ignoreUnmatchedFinish = activeLoad != null
        expectedLoadGeneration = null
        activeLoad = null
        preStartLoadFailed = false
        preStartPersistenceFailed = false
    }

    override fun onPageStarted(
        view: WebView,
        url: String?,
        favicon: Bitmap?,
    ) {
        super.onPageStarted(view, url, favicon)
        tracksDocuments = true
        val generation = expectedLoadGeneration
        if (generation != null || activeLoad == null) {
            activeLoad =
                ActiveLoad(
                    generation = generation,
                    url = url,
                    loadFailed = preStartLoadFailed,
                    persistenceFailed = preStartPersistenceFailed,
                )
            expectedLoadGeneration = null
            preStartLoadFailed = false
            preStartPersistenceFailed = false
        } else {
            // Redirect starts belong to the navigation already in flight.
            activeLoad?.apply {
                this.url = url
            }
        }
        ignoreUnmatchedFinish = false

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

        // Workspace roots are caught here too, not only in
        // shouldOverrideUrlLoading: that callback is skipped for loads the shell
        // starts itself and for POST-driven navigations — which is how the
        // Databricks login chain hands the session back (a form POST landing on
        // the workspace root). onPageStarted sees every main-frame load.
        if (origin == pinned) {
            val target = workspaceRootTarget(url) ?: return
            view.stopLoading()
            bounce(view, target)
        }
    }

    /**
     * In-page navigation: the SPA swapped the URL with `pushState` /
     * `replaceState`, or the user moved through history. No page is loaded, so
     * neither [shouldOverrideUrlLoading] nor [onPageStarted] runs — this is the
     * only callback that observes it, and the only way to catch the user routing
     * client-side back to the workspace root.
     */
    override fun doUpdateVisitedHistory(
        view: WebView,
        url: String?,
        isReload: Boolean,
    ) {
        super.doUpdateVisitedHistory(view, url, isReload)
        if (originOf(url) != pinnedOrigin()) return
        val target = workspaceRootTarget(url) ?: return
        bounce(view, target)
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
        if (
            request.isForMainFrame &&
            isPinnedLoadError(request.url) &&
            isBlockingLoadError(error.errorCode, error.description)
        ) {
            when {
                activeLoad != null -> activeLoad?.loadFailed = true
                expectedLoadGeneration != null || !tracksDocuments -> preStartLoadFailed = true
            }
        }
    }

    override fun onReceivedHttpError(
        view: WebView,
        request: WebResourceRequest,
        errorResponse: WebResourceResponse,
    ) {
        super.onReceivedHttpError(view, request, errorResponse)
        if (request.isForMainFrame && isPinnedLoadError(request.url)) {
            when {
                activeLoad != null -> {
                    activeLoad?.persistenceFailed = true
                }

                expectedLoadGeneration != null || !tracksDocuments -> {
                    preStartPersistenceFailed = true
                }
            }
        }
    }

    private fun isPinnedLoadError(url: Uri): Boolean {
        if (originOf(url.toString()) != pinnedOrigin()) return false
        val load = activeLoad ?: return expectedLoadGeneration != null || !tracksDocuments
        return load.url == url.toString()
    }

    override fun onPageCommitVisible(
        view: WebView,
        url: String?,
    ) {
        super.onPageCommitVisible(view, url)
        // Inline-auth documents belong to the navigation in flight, but only
        // the returning app document may complete its load generation.
        if (originOf(url) != pinnedOrigin()) return
        val load = activeLoad?.takeIf { it.url == url } ?: return
        if (load.loadFailed || load.persistenceFailed) return
        completeLoad(view, url, load)
    }

    override fun onPageFinished(
        view: WebView,
        url: String?,
    ) {
        super.onPageFinished(view, url)
        val load = activeLoad
        val onPinnedOrigin = originOf(url) == pinnedOrigin()
        // Inline authentication can finish foreign documents before returning
        // to the app. They remain part of the current app navigation.
        if (load != null && !onPinnedOrigin) return
        if (load == null && (ignoreUnmatchedFinish || tracksDocuments)) {
            ignoreUnmatchedFinish = false
            return
        }
        if (load != null && !load.loadFailed && !load.persistenceFailed) return
        completeLoad(view, url, load)
    }

    private fun completeLoad(
        view: WebView,
        url: String?,
        load: ActiveLoad?,
    ) {
        val loadFailed = load?.loadFailed ?: preStartLoadFailed
        val persistenceFailed = load?.persistenceFailed ?: preStartPersistenceFailed
        val loadGeneration = load?.generation
        activeLoad = null
        preStartLoadFailed = false
        preStartPersistenceFailed = false
        ignoreUnmatchedFinish = tracksDocuments
        val onPinnedOrigin = originOf(url) == pinnedOrigin()
        // An app page loaded, so the mount works: re-arm the bounce budget for
        // the next time the user lands back on the workspace root.
        if (onPinnedOrigin && databricksWorkspaceUiUrl(url) == null) rootBounces = 0
        // Databricks workspace-hosted Omnigent renders inside the workspace's
        // top-nav chrome (the SPA is a workspace page). Hide it by overlaying
        // Omnigent's own root — see [WorkspaceChromeScript], which also explains why
        // this is keyed on the pinned origin and never on the URL's path. Re-applied
        // on every full load (a server switch is a fresh document); the SPA's
        // client-side routing keeps the same document, so the injected stylesheet
        // persists across in-app navigation.
        if (onPinnedOrigin) {
            view.evaluateJavascript(WorkspaceChromeScript.source, null)
        }
        if (onPinnedOrigin && shouldInjectBridgeAtPageReady()) {
            view.evaluateJavascript(
                NativeBridgeScript.source,
            ) { onPageReady(url, loadFailed, persistenceFailed, loadGeneration) }
            return
        }
        onPageReady(url, loadFailed, persistenceFailed, loadGeneration)
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

        // Same-origin app pages load in the WebView, except a landing on the bare
        // workspace root, which belongs to Databricks rather than the app.
        val origin = originOf(url.toString())
        val pinned = pinnedOrigin()
        if (origin == pinned) {
            val target = workspaceRootTarget(url.toString()) ?: return false
            bounce(view, target)
            return true
        }

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

    /**
     * The `/omnigent` URL to bounce to when [url] is a bare Databricks workspace
     * root — the workspace's own landing page rather than the app — or null when
     * there's nothing to do.
     *
     * Budgeted, and spent by the caller that acts on it: a workspace that answers
     * `/omnigent` with a redirect back to the root (e.g. the mount isn't enabled
     * there) would otherwise loop forever. One bounce per app page load, so a
     * failed bounce leaves the user on the workspace root and [onPageFinished]
     * re-arms the budget as soon as an app page loads.
     */
    private fun workspaceRootTarget(url: String?): String? {
        val target = databricksWorkspaceUiUrl(url) ?: return null
        if (rootBounces >= MAX_ROOT_BOUNCES) return null
        rootBounces++
        return target
    }

    /**
     * Posted, never loaded inline: a loadUrl issued while WebView is committing a
     * navigation can be dropped. Not view.post() — that queues until the view is
     * attached.
     */
    private fun bounce(
        view: WebView,
        target: String,
    ) {
        // MainActivity injects a generation-stamped loader for this app navigation.
        mainHandler.post { loadUrl(view, target) }
    }

    private data class ActiveLoad(
        val generation: Long?,
        var url: String?,
        var loadFailed: Boolean = false,
        var persistenceFailed: Boolean = false,
    )

    private companion object {
        const val MAX_ROOT_BOUNCES = 1
    }
}

internal fun isBlockingLoadError(
    errorCode: Int,
    description: CharSequence,
): Boolean = errorCode != WebViewClient.ERROR_UNKNOWN || !description.contains("ERR_ABORTED")
