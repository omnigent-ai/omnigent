package ai.omnigent.android

import android.Manifest
import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.webkit.CookieManager
import android.webkit.MimeTypeMap
import android.webkit.PermissionRequest
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebView
import androidx.activity.ComponentActivity
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.core.content.getSystemService
import androidx.core.graphics.Insets
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature

/**
 * The single WebView host. Mirrors the iOS `WebShellView` + `OmnigentWebView`:
 * loads the server-served SPA, installs the `window.omnigentNative` bridge, and
 * wires the native capabilities the web layer expects.
 *
 * Server URL comes from [ServerStore]; when none is set yet, launch routes to
 * [ConnectActivity] first. Sidebar edge-swipe is intentionally absent (README).
 */
class MainActivity : ComponentActivity() {
    private lateinit var webView: WebView
    private lateinit var notifications: NativeNotificationManager
    private lateinit var blobSaver: BlobSaver
    private val loginManager = OidcLoginManager()
    private var pinnedOrigin: String? = null

    // Bridge-dependent work deferred until the page (and its injected emit
    // callbacks) exist — see onPageReady.
    private var pendingNavigatePath: String? = null
    private var lastInsets: Insets? = null
    private var pageLoaded = false
    private var loginAttempts = 0 // capped browser-login retries; reset in onPageReady
    private var historyCleared = false // drop pre-auth/login-redirect history once

    // WebChromeClient affordances that need Activity-scoped result launchers.
    // Transient by design: rotation is covered by configChanges (no recreation),
    // so the only loss is the process-death case (killed while the picker /
    // permission dialog is foreground) — the re-delivered result finds a null
    // field and the fresh page simply has no pending input. No hang or crash.
    private var pendingFileCallback: ValueCallback<Array<Uri>>? = null
    private var pendingMicRequest: PermissionRequest? = null

    private val requestNotifications =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) {
            // Granted or not: notify() no-ops when notifications are disabled and
            // the web layer keeps working without OS toasts.
        }

    private val requestMic =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            val request = pendingMicRequest
            pendingMicRequest = null
            if (granted && request != null) {
                request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
            } else {
                request?.deny()
            }
        }

    private val pickFiles =
        registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
            val callback = pendingFileCallback
            pendingFileCallback = null
            callback?.onReceiveValue(uris.toTypedArray())
        }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        // Edge-to-edge: the WebView spans system bars; insets are pushed to CSS
        // below. Display-cutout handling is set in the manifest theme.
        WindowCompat.setDecorFitsSystemWindows(window, false)

        val store = ServerStore(this)
        if (!store.hasServer()) {
            // No server configured yet — send the user to the connect screen first.
            startActivity(Intent(this, ConnectActivity::class.java))
            finish()
            return
        }
        val serverUrl = store.currentServerUrl()
        pinnedOrigin = originOf(serverUrl)

        // Application context for the long-lived helpers so the WebView's bridge
        // reference chain can't pin this Activity.
        notifications = NativeNotificationManager(applicationContext)
        blobSaver = BlobSaver(applicationContext)

        // Capture (don't replay yet) a notification tap that cold-started us.
        pendingNavigatePath = navigatePathOf(intent)

        if (BuildConfig.DEBUG) WebView.setWebContentsDebuggingEnabled(true) // chrome://inspect

        webView =
            WebView(this).apply {
                settings.javaScriptEnabled = true
                settings.domStorageEnabled = true
                settings.mediaPlaybackRequiresUserGesture = false

                webViewClient =
                    OmnigentWebViewClient(
                        pinnedOrigin = { pinnedOrigin },
                        onPageReady = ::onPageReady,
                        onLoginRequired = ::startLogin,
                    )
                webChromeClient =
                    OmnigentWebChromeClient(
                        onChooseFiles = ::chooseFiles,
                        onPermission = ::handlePermissionRequest,
                    )
                setDownloadListener { downloadUrl, _, contentDisposition, mimeType, _ ->
                    downloadFile(downloadUrl, contentDisposition, mimeType)
                }
            }
        setContentView(webView)
        installBridge()

        // Measure the OS safe area and push it into the page as CSS custom
        // properties — Android WebView can't rely on `env(safe-area-inset-*)`
        // alone (unreliable < API 30 and across OEM builds). Cached so the first
        // post-load emit (in onPageReady) isn't lost to the pre-load race.
        ViewCompat.setOnApplyWindowInsetsListener(webView) { _, insets ->
            val bars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
            // When the soft keyboard is up it sits over the nav bar, so the bottom
            // safe area must collapse to 0 — otherwise the composer (which
            // adjustResize already keeps above the IME) floats a nav-bar height
            // above the keyboard. Subtracting the IME inset does exactly that and
            // is a no-op when the keyboard is hidden.
            lastInsets = Insets.of(bars.left, bars.top, bars.right, maxOf(0, bars.bottom - ime.bottom))
            emitInsets()
            insets
        }

        onBackPressedDispatcher.addCallback(
            this,
            object : OnBackPressedCallback(true) {
                override fun handleOnBackPressed() {
                    // Ask the page to dismiss an open overlay first (drawer/dialog);
                    // only navigate WebView history / leave the app if there was
                    // nothing to dismiss. evaluateJavascript's result is JSON, so a
                    // true return arrives as the string "true".
                    //
                    // This callback always consumes Back, but the JS round-trip is
                    // async and its callback is silently dropped if the renderer is
                    // gone (OOM-killed / hung) — which would strand the user with a
                    // dead Back. So race a timeout fallback: whichever of the JS
                    // result or the timer fires first navigates, the other no-ops.
                    // Both run on the main thread, so the plain flag needs no lock.
                    var acted = false
                    // If the host is already going away when a late callback/timer
                    // fires, don't touch the (possibly destroyed) WebView.
                    val navigate = {
                        if (!isDestroyed && !isFinishing && ::webView.isInitialized) {
                            if (webView.canGoBack()) webView.goBack() else finish()
                        }
                    }
                    val fallback = Runnable { if (!acted) { acted = true; navigate() } }
                    webView.postDelayed(fallback, BACK_FALLBACK_MS)
                    webView.evaluateJavascript(
                        "!!(window.__omnigentNativeHandleBack && window.__omnigentNativeHandleBack())",
                    ) { handled ->
                        if (!acted) {
                            acted = true
                            webView.removeCallbacks(fallback)
                            if (handled != "true") navigate()
                        }
                    }
                }
            },
        )

        ensureNotificationPermission()
        webView.loadUrl(serverUrl)
    }

    /**
     * Install the web -> native bridge as an origin-allowlisted web message
     * listener (NOT addJavascriptInterface): the transport object reaches only
     * frames on the pinned origin, and [OmnigentBridgeListener] also drops
     * non-main-frame messages — so a sandboxed agent-HTML iframe can't reach it.
     * Requires WebView 88+ (the same floor as our env()/inset handling); if the
     * feature is missing the bridge is simply absent and the web layer falls back.
     */
    private fun installBridge() {
        val origin = pinnedOrigin ?: return
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) return
        try {
            WebViewCompat.addWebMessageListener(
                webView,
                OmnigentBridgeListener.JS_OBJECT_NAME,
                setOf(origin),
                OmnigentBridgeListener(notifications, blobSaver),
            )
        } catch (_: IllegalArgumentException) {
            // Malformed origin rule — leave the bridge absent; the web layer falls back.
        }
    }

    /**
     * Run the RFC 8252 login flow: authenticate in the system browser
     * (Google/passkey work there, not in a WebView), then [onSessionToken]
     * injects the session. Triggered by [OmnigentWebViewClient] when the server
     * redirects to the IdP.
     *
     * Capped retries: if injecting the session still leaves us redirected to
     * login (rejected cookie, expired token, clock skew), don't relaunch the
     * browser forever — give up after [MAX_LOGIN_ATTEMPTS]. The counter resets in
     * onPageReady once a pinned-origin page actually loads (i.e. we're past the
     * login redirect).
     */
    private fun startLogin() {
        val origin = pinnedOrigin ?: return
        if (loginAttempts >= MAX_LOGIN_ATTEMPTS) {
            authLog("login attempts exhausted ($loginAttempts) — not retrying")
            return
        }
        loginAttempts++
        // A re-login (session expired mid-use) bounces through the IdP again,
        // leaving a stopped off-origin entry + stale pre-expiry pages on the back
        // stack. Re-arm the one-shot history clear so the next authenticated
        // page-ready purges them — otherwise Back walks into the stopped IdP entry
        // and re-pops the login browser.
        historyCleared = false
        loginManager.start(this, origin, ::onSessionToken)
    }

    /**
     * Bridge the session from the browser into the WebView: the polled JWT is
     * exactly the session-cookie value, so set it as the cookie (the browser's
     * cookie store is isolated from the WebView's), reload authenticated, and get
     * the user back to the app.
     *
     * Foregrounding ourselves from the background (the poll completes while the
     * browser is in front) is blocked by Android's background-activity-launch
     * rules, so we both attempt a reorder-to-front (works within the grace
     * period) AND post a "tap to return" notification as the reliable path back.
     */
    private fun onSessionToken(token: String) {
        // The poll can land after the activity is gone (it ran on a background
        // thread up to 5 min) — never touch a destroyed WebView.
        if (isDestroyed || isFinishing || !::webView.isInitialized) return
        // Defense-in-depth: the token is interpolated into the cookie string, so a
        // value carrying ';' or whitespace could smuggle in cookie attributes
        // (e.g. Domain=, defeating the __Host- prefix). A real session token is an
        // HS256 JWT — three base64url segments — which never contains those, so
        // this only ever rejects a malformed/hostile value, never a valid login.
        if (!isJwtShaped(token)) {
            authLog("onSessionToken: token not JWT-shaped — rejecting")
            return
        }
        val origin = pinnedOrigin ?: return
        val secure = origin.startsWith("https://")
        // Matches the server's session_cookie_name: __Host- prefix on HTTPS.
        val name = if (secure) "__Host-ap_session" else "ap_session"
        val cookie = buildString {
            append(name).append('=').append(token).append("; Path=/")
            if (secure) append("; Secure")
            append("; SameSite=Lax")
        }
        val cookies = CookieManager.getInstance()
        cookies.setAcceptCookie(true)
        authLog("onSessionToken: injecting $name (token len=${token.length})")
        cookies.setCookie(origin, cookie) { accepted ->
            // setCookie's callback is async — re-check the WebView is still alive.
            if (isDestroyed || !::webView.isInitialized) return@setCookie
            authLog("setCookie accepted=$accepted present=${cookies.getCookie(origin)?.contains(name) == true}")
            cookies.flush()
            webView.loadUrl(origin)
        }
        startActivity(
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT or Intent.FLAG_ACTIVITY_SINGLE_TOP),
        )
        notifications.notify(
            title = getString(R.string.signed_in_title),
            body = getString(R.string.signed_in_body),
            navigatePath = "/",
        )
    }

    /**
     * True if [token] is shaped like a JWT — three non-empty base64url segments
     * (`header.payload.signature`). base64url is `[A-Za-z0-9_-]`, so a JWT can
     * never carry the `;`, whitespace, or control chars that would let a value
     * break out of the cookie string and inject attributes.
     */
    private fun isJwtShaped(token: String): Boolean {
        val parts = token.split('.')
        if (parts.size != 3) return false
        return parts.all { part ->
            part.isNotEmpty() &&
                part.all { c ->
                    c in 'A'..'Z' || c in 'a'..'z' || c in '0'..'9' || c == '-' || c == '_'
                }
        }
    }

    override fun onDestroy() {
        // Unblock a pending file input / mic request, then release WebView + worker.
        pendingFileCallback?.onReceiveValue(null)
        pendingFileCallback = null
        pendingMicRequest?.deny()
        pendingMicRequest = null
        loginManager.shutdown()
        if (::blobSaver.isInitialized) blobSaver.shutdown()
        if (::webView.isInitialized) webView.destroy() // releases the bridge chain
        super.onDestroy()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        val path = navigatePathOf(intent) ?: return
        pendingNavigatePath = path
        // Replay now if the page is up; otherwise onPageReady will flush it.
        if (pageLoaded) flushPendingActivation()
    }

    /** Run bridge-dependent work once a pinned-origin page has finished loading. */
    private fun onPageReady(url: String?) {
        // Only a real pinned-origin load carries the injected facade — an error
        // page (chrome-error://) or a foreign redirect must NOT drain
        // pendingNavigatePath or push insets into a page that can't consume them.
        if (originOf(url) != pinnedOrigin) return
        // First authenticated app page: drop everything before it from the
        // back/forward list. Otherwise Back walks into the pre-auth root and the
        // login-redirect reload (the `loadUrl(origin)` after the cookie injection),
        // which bounces to login or shows a blank — "back lands on the wrong
        // screen" / "exits the app". After this the SPA builds clean history.
        if (!historyCleared) {
            historyCleared = true
            webView.clearHistory()
        }
        pageLoaded = true
        loginAttempts = 0 // reached a pinned-origin page — we're past the login redirect
        flushPendingActivation()
        emitInsets()
    }

    private fun flushPendingActivation() {
        emitNotificationActivation(pendingNavigatePath)
        pendingNavigatePath = null
    }

    private fun navigatePathOf(intent: Intent?): String? =
        intent?.getStringExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH)
            ?.takeIf { it.startsWith("/") }

    private fun emitNotificationActivation(path: String?) {
        if (path == null) return
        webView.evaluateJavascript(
            "window.__omnigentNativeEmitNotificationActivated && " +
                "window.__omnigentNativeEmitNotificationActivated(${jsString(path)});",
            null,
        )
    }

    private fun emitInsets() {
        // Feed the OS safe area into the web layer two ways, because the shell
        // pins to a user-supplied server whose web build may PRE-DATE the Android
        // shell's CSS — it can't be assumed to carry the `[data-android-native]`
        // fold:
        //   1. `--omnigent-safe-top/bottom` — the app's OWN base inset vars. Every
        //      build already derives `--omnigent-inset-*` and its layout from
        //      these, defaulting them to `env(safe-area-inset-*)`, which Android
        //      WebView reports as 0. Setting them inline (highest priority)
        //      overrides that 0 everywhere the layout already reads them.
        //   2. `--omnigent-android-safe-area-*` — consumed by the shell's own
        //      `[data-android-native]` rules when the server IS up to date (folded
        //      via max() in index.css); a harmless no-op otherwise.
        // We deliberately do NOT call `__omnigentNativeEmitInsets` — that feeds the
        // iOS *floating-bar* footprints (--omnigent-native-*-bar; nativeInsets.ts
        // is a "no-op off the iOS shell"), and Android has no such bars. Routing
        // the safe area there would mis-assign it to a bar-footprint variable.
        val bars = lastInsets ?: return
        val d = resources.displayMetrics.density
        val js =
            """
            (() => {
              const s = document.documentElement.style;
              const top = '${bars.top / d}px';
              const bottom = '${bars.bottom / d}px';
              s.setProperty('--omnigent-safe-top', top);
              s.setProperty('--omnigent-safe-bottom', bottom);
              s.setProperty('--omnigent-android-safe-area-top', top);
              s.setProperty('--omnigent-android-safe-area-bottom', bottom);
              s.setProperty('--omnigent-android-safe-area-left', '${bars.left / d}px');
              s.setProperty('--omnigent-android-safe-area-right', '${bars.right / d}px');
            })();
            """.trimIndent()
        webView.evaluateJavascript(js, null)
    }

    private fun hasPermission(permission: String): Boolean =
        ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED

    private fun ensureNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return // granted at install < API 33
        if (!hasPermission(Manifest.permission.POST_NOTIFICATIONS)) {
            requestNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    /** Back [OmnigentWebChromeClient.onShowFileChooser] with a document picker. */
    private fun chooseFiles(callback: ValueCallback<Array<Uri>>, acceptTypes: Array<String>): Boolean {
        pendingFileCallback?.onReceiveValue(null) // cancel any in-flight chooser
        pendingFileCallback = callback
        // Keep MIME types as-is; resolve ".pdf"-style extension tokens to MIME so
        // the declared accept constraint isn't silently widened to */*.
        val mimeTypes = acceptTypes.mapNotNull(::mimeTypeFor).toTypedArray().ifEmpty { arrayOf("*/*") }
        return try {
            pickFiles.launch(mimeTypes)
            true
        } catch (_: Throwable) {
            pendingFileCallback = null
            callback.onReceiveValue(null) // resolve the <input> rather than hang it
            true
        }
    }

    /** A web accept token (a MIME type, or a ".pdf"-style extension) -> a MIME type, or null. */
    private fun mimeTypeFor(accept: String): String? {
        val token = accept.trim()
        return when {
            token.isEmpty() -> null
            token.contains('/') -> token // already a MIME type / wildcard
            else ->
                MimeTypeMap.getSingleton()
                    .getMimeTypeFromExtension(token.removePrefix(".").lowercase())
        }
    }

    /** Back [OmnigentWebChromeClient.onPermissionRequest] — grant mic to the pinned origin only. */
    private fun handlePermissionRequest(request: PermissionRequest) {
        val wantsAudio = request.resources.contains(PermissionRequest.RESOURCE_AUDIO_CAPTURE)
        if (!wantsAudio || originOf(request.origin?.toString()) != pinnedOrigin) {
            request.deny()
            return
        }
        if (hasPermission(Manifest.permission.RECORD_AUDIO)) {
            request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
        } else {
            pendingMicRequest?.deny() // don't leave a prior request hanging forever
            pendingMicRequest = request
            requestMic.launch(Manifest.permission.RECORD_AUDIO)
        }
    }

    private fun downloadFile(url: String, contentDisposition: String?, mimeType: String?) {
        val name = URLUtil.guessFileName(url, contentDisposition, mimeType)

        // Agent-generated files arrive as blob:/data: URLs, which DownloadManager
        // can't handle — fetch them in page context and save via the blob bridge
        // (fixes omnigent-ai/omnigent#969, which the iOS shell leaves broken).
        if (url.startsWith("blob:") || url.startsWith("data:")) {
            webView.evaluateJavascript(BlobDownloadScript.fetchAsBase64(url, name), null)
            return
        }

        if (!url.startsWith("http")) return
        val request =
            DownloadManager.Request(Uri.parse(url)).apply {
                setMimeType(mimeType)
                setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name)
                setNotificationVisibility(
                    DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED,
                )
            }
        getSystemService<DownloadManager>()?.enqueue(request)
    }

    private companion object {
        const val MAX_LOGIN_ATTEMPTS = 3

        // Back-press fallback: long enough that a healthy renderer's JS round-trip
        // (a few ms) always wins the race, short enough to not feel stuck if it
        // doesn't answer. Only the timer ever fires when the renderer is gone.
        const val BACK_FALLBACK_MS = 600L
    }
}
