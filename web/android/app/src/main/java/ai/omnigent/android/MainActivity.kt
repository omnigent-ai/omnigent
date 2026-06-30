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
            lastInsets = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            emitInsets()
            insets
        }

        onBackPressedDispatcher.addCallback(
            this,
            object : OnBackPressedCallback(true) {
                override fun handleOnBackPressed() {
                    // TODO: close an open in-page drawer first once the SPA exposes that state.
                    if (webView.canGoBack()) webView.goBack() else finish()
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
     * Run the RFC 8252 login flow: authenticate in a Custom Tab (Google/passkey
     * work there, not in a WebView), then [onSessionToken] injects the session.
     * Triggered by [OmnigentWebViewClient] when the server redirects to the IdP.
     */
    private fun startLogin() {
        val origin = pinnedOrigin ?: return
        loginManager.start(this, origin, ::onSessionToken)
    }

    /**
     * Bridge the session from the Custom Tab into the WebView: the polled JWT is
     * exactly the session-cookie value, so set it as the cookie (the WebView's
     * cookie store is isolated from the Custom Tab's), then reload authenticated
     * and bring this activity back over the Custom Tab.
     */
    private fun onSessionToken(token: String) {
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
        cookies.setCookie(origin, cookie) {
            cookies.flush()
            webView.loadUrl(origin)
        }
        startActivity(
            Intent(this, MainActivity::class.java)
                .addFlags(Intent.FLAG_ACTIVITY_REORDER_TO_FRONT or Intent.FLAG_ACTIVITY_SINGLE_TOP),
        )
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
        pageLoaded = true
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
        // We push the OS safe area straight into `--omnigent-android-safe-area-*`
        // (folded via max() in index.css). We deliberately do NOT call
        // `__omnigentNativeEmitInsets` — that path feeds the iOS *floating-bar*
        // footprints (--omnigent-native-*-bar; nativeInsets.ts is a "no-op off
        // the iOS shell"), and Android has no such bars. Routing the safe area
        // there would mis-assign it to a bar-footprint variable.
        val bars = lastInsets ?: return
        val d = resources.displayMetrics.density
        val js =
            """
            (() => {
              const s = document.documentElement.style;
              s.setProperty('--omnigent-android-safe-area-top', '${bars.top / d}px');
              s.setProperty('--omnigent-android-safe-area-bottom', '${bars.bottom / d}px');
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
}
