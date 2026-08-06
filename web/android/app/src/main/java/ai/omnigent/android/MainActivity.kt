package ai.omnigent.android

import android.Manifest
import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.Configuration
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.text.TextUtils
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.MimeTypeMap
import android.webkit.PermissionRequest
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebView
import android.widget.FrameLayout
import android.widget.PopupMenu
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.core.content.getSystemService
import androidx.core.graphics.Insets
import androidx.core.view.MenuCompat
import androidx.core.view.ViewCompat
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.webkit.ScriptHandler
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import java.lang.ref.WeakReference

internal fun systemSafeAreaInsets(insets: WindowInsetsCompat): Insets =
    insets.getInsets(
        WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout(),
    )

internal fun androidSafeAreaScript(
    insets: Insets,
    density: Float,
): String =
    """
    (() => {
      const s = document.documentElement.style;
      const top = '${insets.top / density}px';
      const bottom = '${insets.bottom / density}px';
      const left = '${insets.left / density}px';
      const right = '${insets.right / density}px';
      s.setProperty('--omnigent-safe-top', top);
      s.setProperty('--omnigent-safe-bottom', bottom);
      s.setProperty('--omnigent-safe-left', left);
      s.setProperty('--omnigent-safe-right', right);
      s.setProperty('--omnigent-android-safe-area-top', top);
      s.setProperty('--omnigent-android-safe-area-bottom', bottom);
      s.setProperty('--omnigent-android-safe-area-left', left);
      s.setProperty('--omnigent-android-safe-area-right', right);
    })();
    """.trimIndent()

/**
 * The single WebView host. Mirrors the iOS `WebShellView` + `OmnigentWebView`:
 * loads the server-served SPA, installs the `window.omnigentNative` bridge, and
 * wires the native capabilities the web layer expects.
 *
 * Server URL comes from [ServerStore]; when none is set yet, launch routes to
 * [ConnectActivity] first. Sidebar edge-swipe is intentionally absent (README).
 */
class MainActivity : AppCompatActivity() {
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
    private var bridgeTransportInstalled = false
    private var bridgeScriptHandler: ScriptHandler? = null
    private var loginAttempts = 0 // capped browser-login retries; reset in onPageReady
    private var historyCleared = false // drop pre-auth/login-redirect history once

    // Floating server switcher — mirrors the iOS `ServerSwitcher`. Always
    // visible so it's always available as a recovery path (backward compatible
    // with older web builds). Theme-aware via brand colors (light/dark XML).
    private lateinit var switchButton: TextView

    // Null before each page publishes, preserving whole-window centring.
    private var switcherBand: ServerSwitcherBand? = null
    private var switcherHidden = false

    // WebChromeClient affordances that need Activity-scoped result launchers.
    // Transient by design: rotation is covered by configChanges (no recreation),
    // so the only loss is the process-death case (killed while the picker /
    // permission dialog is foreground) — the re-delivered result finds a null
    // field and the fresh page simply has no pending input. No hang or crash.
    private var pendingFileCallback: ValueCallback<Array<Uri>>? = null
    private var pendingMicRequest: PermissionRequest? = null

    private val requestNotifications =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            // Denied: notify() no-ops when notifications are disabled and the
            // web layer keeps working without OS toasts. Granted: replay the
            // badge the web layer may have computed (and deduped) while the
            // permission dialog was still open — its post was silently dropped.
            if (granted) notifications.replayBadge()
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
                        shouldInjectBridgeAtPageReady = {
                            bridgeTransportInstalled && bridgeScriptHandler == null
                        },
                        onPageReady = ::onPageReady,
                        onLoginRequired = ::startLogin,
                        onNavigationStarted = ::clearServerSwitcherBand,
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
        // Wrap the WebView in a FrameLayout so the floating server-switcher
        // pill can sit on top of it. The pill uses the app's brand palette
        // (values/values-night colors.xml) so it adapts to light/dark mode.
        val container = FrameLayout(this)
        container.addView(webView)
        val dp = resources.displayMetrics.density
        switchButton =
            TextView(this).apply {
                applyHostLabel(serverUrl)
                background =
                    ContextCompat.getDrawable(this@MainActivity, R.drawable.bg_floating_switch)
                setTextColor(ContextCompat.getColor(this@MainActivity, R.color.brand_foreground))
                textSize = 12f
                setPadding((12 * dp).toInt(), (6 * dp).toInt(), (12 * dp).toInt(), (6 * dp).toInt())
                elevation = 6 * dp
                isClickable = true
                isFocusable = true
                isSingleLine = true
                ellipsize = TextUtils.TruncateAt.MIDDLE
                setOnClickListener { showServerSwitcherMenu(it) }
            }
        // Same helper the runtime path uses, so the no-band defaults agree.
        applyServerSwitcherWidthBounds(containerWidth = 0, band = null)
        switchButton.layoutParams =
            FrameLayout
                .LayoutParams(
                    FrameLayout.LayoutParams.WRAP_CONTENT,
                    FrameLayout.LayoutParams.WRAP_CONTENT,
                    Gravity.TOP or Gravity.CENTER_HORIZONTAL,
                ).apply {
                    // Initial position below the status bar; corrected by the
                    // insets listener once system bar insets are measured.
                    topMargin = (8 * dp).toInt()
                }
        container.addView(switchButton)
        // The pill's own bounds can stay unchanged when only its parent resizes,
        // so observe both views.
        val repositionOnLayout =
            View.OnLayoutChangeListener { _, _, _, _, _, _, _, _, _ -> positionServerSwitcher() }
        container.addOnLayoutChangeListener(repositionOnLayout)
        switchButton.addOnLayoutChangeListener(repositionOnLayout)
        setContentView(container)
        applySystemBarContrast()
        installBridge()

        // Measure the OS safe area and push it into the page as CSS custom
        // properties — Android WebView can't rely on `env(safe-area-inset-*)`
        // alone (unreliable < API 30 and across OEM builds). Cached so the first
        // post-load emit (in onPageReady) isn't lost to the pre-load race.
        ViewCompat.setOnApplyWindowInsetsListener(webView) { view, insets ->
            val bars = systemSafeAreaInsets(insets)
            val ime = insets.getInsets(WindowInsetsCompat.Type.ime())
            // Edge-to-edge (setDecorFitsSystemWindows=false, above) neutralizes the
            // manifest's adjustResize: the window no longer shrinks when the IME
            // opens, so bottom-anchored web content (a chat composer, a terminal
            // input) would sit BEHIND the keyboard. Resize the WebView ourselves —
            // shrink its laid-out HEIGHT by the IME inset. It must be the view
            // height (a bottom margin), not bottom padding: the CSS viewport
            // (100vh / the visual viewport that fixed/sticky content anchors to)
            // tracks the WebView's height, not its content box, so padding alone
            // wouldn't reflow the composer above the keyboard. This is the
            // adjustResize equivalent for an edge-to-edge window; the status/nav
            // bars stay CSS safe-areas so content still draws behind them.
            // Type.ime() is the real platform inset on API 30+; on 28-29 androidx
            // backfills it from adjustResize's systemWindowInsets, so the resize
            // still fires. If some pre-30 OEM reports none, the margin stays 0 and
            // we simply degrade to the old (unresized) behavior — no regression.
            (view.layoutParams as? ViewGroup.MarginLayoutParams)?.let { lp ->
                if (lp.bottomMargin != ime.bottom) {
                    lp.bottomMargin = ime.bottom
                    view.layoutParams = lp
                }
            }
            // Bottom safe-area: the nav bar when the keyboard is hidden; 0 while
            // it's up (the resize already lifts content above the keyboard, and the
            // keyboard covers the nav bar). Top/left/right are IME-independent.
            val bottom = if (ime.bottom > 0) 0 else bars.bottom
            lastInsets = Insets.of(bars.left, bars.top, bars.right, bottom)
            // Push the floating switch button below the status bar so it doesn't
            // disappear under the notch/status icons on edge-to-edge layouts.
            (switchButton.layoutParams as? FrameLayout.LayoutParams)?.let { lp ->
                val topMargin = bars.top + (8 * dp).toInt()
                if (lp.topMargin != topMargin) {
                    lp.topMargin = topMargin
                    switchButton.layoutParams = lp
                }
            }
            positionServerSwitcher()
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
                    val fallback =
                        Runnable {
                            if (!acted) {
                                acted = true
                                navigate()
                            }
                        }
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

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        applySystemBarContrast(newConfig)
        if (::webView.isInitialized) {
            // Notify matchMedia listeners without reloading the SPA.
            webView.dispatchConfigurationChanged(newConfig)
            webView.post(::positionServerSwitcher)
        }
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
        // Weak so the long-lived listener cannot pin this Activity.
        val host = WeakReference(this)
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER)) return
        try {
            WebViewCompat.addWebMessageListener(
                webView,
                OmnigentBridgeListener.JS_OBJECT_NAME,
                setOf(origin),
                OmnigentBridgeListener(
                    notifications = notifications,
                    blobSaver = blobSaver,
                    onServerSwitcherBand = { band -> host.get()?.receiveServerSwitcherBand(band) },
                    onServerSwitcherHidden = { hidden ->
                        host.get()?.receiveServerSwitcherHidden(hidden)
                    },
                ),
            )
        } catch (_: IllegalArgumentException) {
            // Malformed origin rule — leave the bridge absent; the web layer falls back.
            return
        }
        bridgeTransportInstalled = true
        if (WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
            try {
                bridgeScriptHandler =
                    WebViewCompat.addDocumentStartJavaScript(
                        webView,
                        NativeBridgeScript.source,
                        setOf(origin),
                    )
            } catch (_: IllegalArgumentException) {
                // Keep the transport; onPageFinished will inject the facade instead.
            }
        }
    }

    /** Reads the config the framework hands us; `resources` lags a change. */
    private fun applySystemBarContrast(config: Configuration = resources.configuration) {
        val isLightMode =
            config.uiMode and Configuration.UI_MODE_NIGHT_MASK !=
                Configuration.UI_MODE_NIGHT_YES
        WindowInsetsControllerCompat(window, window.decorView).apply {
            isAppearanceLightStatusBars = isLightMode
            isAppearanceLightNavigationBars = isLightMode
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
        // start() no-ops when a login is already in flight — a multi-hop OIDC
        // redirect can re-enter this before the first browser hand-off settles.
        // Count (and re-arm the history clear for) only a call that actually
        // launches a flow, so re-entrant redirects can't burn the retry budget
        // without ever relaunching and suppress a legitimate later retry.
        if (!loginManager.start(this, origin, ::onSessionToken)) return
        loginAttempts++
        // A re-login (session expired mid-use) bounces through the IdP again,
        // leaving a stopped off-origin entry + stale pre-expiry pages on the back
        // stack. Re-arm the one-shot history clear so the next authenticated
        // page-ready purges them — otherwise Back walks into the stopped IdP entry
        // and re-pops the login browser.
        historyCleared = false
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
        val cookie =
            buildString {
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
            authLog(
                "setCookie accepted=$accepted present=${cookies
                    .getCookie(
                        origin,
                    )?.contains(name) == true}",
            )
            // A rejected cookie means the reload would land unauthenticated,
            // bounce to login, and re-launch the browser — burning the retry
            // budget on a failure that retrying can't fix. Stay put instead.
            if (!accepted) return@setCookie
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

    /** The visible text is middle-ellipsized, so the description carries the full host. */
    private fun TextView.applyHostLabel(url: String) {
        val label = hostLabelOf(url)
        text = label
        contentDescription = label
    }

    private fun receiveServerSwitcherBand(band: ServerSwitcherBand) {
        runOnUiThread {
            if (isDestroyed || isFinishing) return@runOnUiThread
            switcherBand = band
            positionServerSwitcher()
        }
    }

    private fun receiveServerSwitcherHidden(hidden: Boolean) {
        runOnUiThread {
            if (isDestroyed || isFinishing) return@runOnUiThread
            switcherHidden = hidden
            if (hidden) switcherBand = null
            positionServerSwitcher()
        }
    }

    /** Forget the web-published band; the pill returns to the window centre. */
    private fun clearServerSwitcherBand() {
        if (switcherBand == null && !switcherHidden) return
        switcherBand = null
        switcherHidden = false
        positionServerSwitcher()
    }

    /** Centre in the latest band, or in the whole window before publication. */
    private fun positionServerSwitcher() {
        if (!::switchButton.isInitialized) return
        val lp = switchButton.layoutParams as? FrameLayout.LayoutParams ?: return
        val band = switcherBand
        // The pill's leftMargin is relative to its parent, so the band fraction
        // must resolve against the parent's width.
        val containerWidth = (switchButton.parent as? View)?.width ?: 0
        val bandCanFit = applyServerSwitcherWidthBounds(containerWidth, band)
        val visibility = if (!switcherHidden && bandCanFit) View.VISIBLE else View.INVISIBLE
        if (switchButton.visibility != visibility) switchButton.visibility = visibility
        val switcherWidth = switchButton.width

        val gravity: Int
        val leftMargin: Int
        if (band == null || containerWidth <= 0 || switcherWidth <= 0) {
            gravity = Gravity.TOP or Gravity.CENTER_HORIZONTAL
            leftMargin = 0
        } else {
            // The band uses physical viewport coordinates, so it must not mirror in RTL.
            gravity = Gravity.TOP or Gravity.LEFT
            leftMargin =
                serverSwitcherLeftMargin(
                    containerWidth,
                    switcherWidth,
                    band,
                    switcherControlReservePx(),
                )
        }
        if (lp.gravity == gravity && lp.leftMargin == leftMargin) return
        lp.gravity = gravity
        lp.leftMargin = leftMargin
        switchButton.layoutParams = lp
    }

    /** Shared dp→px conversion so the fit test and margin math can't drift. */
    private fun switcherControlReservePx(): Int =
        (SWITCHER_CONTROL_RESERVE_DP * resources.displayMetrics.density).toInt()

    private fun applyServerSwitcherWidthBounds(
        containerWidth: Int,
        band: ServerSwitcherBand?,
    ): Boolean {
        val dp = resources.displayMetrics.density
        val defaultMax = (SWITCHER_MAX_WIDTH_DP * dp).toInt()
        val defaultMin = (SWITCHER_MIN_WIDTH_DP * dp).toInt()
        val controlReserve = switcherControlReservePx()
        val max =
            if (band == null || containerWidth <= 0) {
                defaultMax
            } else {
                val usableWidth = serverSwitcherUsableWidth(containerWidth, band, controlReserve)
                maxOf(defaultMin, minOf(defaultMax, usableWidth))
            }
        // Floors the WIDTH only — the height stays the text's — so this bounds how
        // far a label can shrink, not the full touch target.
        if (switchButton.minWidth != defaultMin) switchButton.minWidth = defaultMin
        if (switchButton.maxWidth != max) switchButton.maxWidth = max
        return band == null ||
            serverSwitcherBandCanFit(containerWidth, band, controlReserve, defaultMin)
    }

    /**
     * Extract a short host[:port] label from a URL for the server switcher pill.
     * Mirrors the iOS `URL.omnigentHostLabel` in `URL+Omnigent.swift`.
     */
    private fun hostLabelOf(url: String): String {
        val uri = Uri.parse(url)
        val host = uri.host ?: return url
        val port = uri.port
        return if (port != -1 &&
            !(
                (uri.scheme?.lowercase() == "https" && port == 443) ||
                    (uri.scheme?.lowercase() == "http" && port == 80)
            )
        ) {
            "$host:$port"
        } else {
            host
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
        if (::webView.isInitialized) {
            removeBridge()
            webView.destroy()
        }
        super.onDestroy()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)

        // Detect a server change: ConnectActivity re-enters us via
        // CLEAR_TOP|SINGLE_TOP after the user picks a different server. The
        // bridge is origin-allowlisted, so a server switch without re-registering
        // leaves the bridge dead for the new origin.
        val store = ServerStore(this)
        val newServerUrl = store.currentServerUrl()
        val newOrigin = originOf(newServerUrl)
        if (newOrigin != null && newOrigin != pinnedOrigin) {
            reloadWithNewServer(newServerUrl, newOrigin)
        }

        val path = navigatePathOf(intent) ?: return
        pendingNavigatePath = path
        // Replay now if the page is up; otherwise onPageReady will flush it.
        if (pageLoaded) flushPendingActivation()
    }

    /**
     * Swap to a new pinned server: remove the old bridge (allowlisted to the
     * old origin), update [pinnedOrigin], re-install the bridge for the new
     * origin, reset page state, and reload. Called from [onNewIntent] when
     * ConnectActivity returns with a different server.
     */
    private fun reloadWithNewServer(
        serverUrl: String,
        newOrigin: String,
    ) {
        removeBridge()
        pinnedOrigin = newOrigin
        pageLoaded = false
        historyCleared = false
        loginAttempts = 0
        switchButton.applyHostLabel(serverUrl)
        installBridge()
        webView.loadUrl(serverUrl)
    }

    private fun removeBridge() {
        bridgeScriptHandler?.remove()
        bridgeScriptHandler = null
        bridgeTransportInstalled = false
        try {
            WebViewCompat.removeWebMessageListener(
                webView,
                OmnigentBridgeListener.JS_OBJECT_NAME,
            )
        } catch (_: Exception) {
            // Not registered (feature unsupported, or already removed) — no-op.
        }
    }

    /**
     * Show the server-switcher dropdown menu, mirroring the iOS `ServerSwitcher`
     * `Menu`. Lists the current server (disabled header), other recent servers,
     * Reload, and Connect to New Server. Tapping a recent server switches
     * directly without leaving the app; "Connect to New Server" opens
     * [ConnectActivity] for manual URL entry.
     */
    private fun showServerSwitcherMenu(anchor: View) {
        val store = ServerStore(this)
        val currentUrl = store.currentServerUrl()
        val otherServers = store.recentServers().filter { originOf(it) != pinnedOrigin }

        val popup = PopupMenu(this, anchor, Gravity.TOP)
        MenuCompat.setGroupDividerEnabled(popup.menu, true)
        popup.menu.apply {
            // Group 0: current server — disabled header.
            add(0, 0, 0, hostLabelOf(currentUrl)).isEnabled = false
            // Group 1: other recent servers (divider before this group).
            otherServers.forEachIndexed { i, url ->
                add(1, 100 + i, 0, hostLabelOf(url))
            }
            // Group 2: actions (divider before this group).
            add(2, 3, 0, getString(R.string.menu_reload))
            add(2, 4, 0, getString(R.string.menu_connect_new))
        }
        popup.setOnMenuItemClickListener { item ->
            when (item.itemId) {
                3 -> {
                    webView.reload()
                    true
                }

                4 -> {
                    startActivity(Intent(this@MainActivity, ConnectActivity::class.java))
                    true
                }

                in 100..Int.MAX_VALUE -> {
                    val url = otherServers[item.itemId - 100]
                    store.connect(url)
                    originOf(url)?.let { reloadWithNewServer(url, it) }
                    true
                }

                else -> {
                    false
                }
            }
        }
        popup.show()
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
        // A tap can arrive (onNewIntent) while the WebView is parked off-origin —
        // e.g. mid re-login — where the bridge facade doesn't exist, so emitting
        // would silently drop the path. Keep it pending; the next pinned-origin
        // onPageReady flushes it.
        if (originOf(webView.url) != pinnedOrigin) return
        emitNotificationActivation(pendingNavigatePath)
        pendingNavigatePath = null
    }

    private fun navigatePathOf(intent: Intent?): String? =
        intent
            ?.getStringExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH)
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
        //   1. `--omnigent-safe-*` — the app's OWN base inset vars. Every
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
        webView.evaluateJavascript(androidSafeAreaScript(bars, d), null)
    }

    private fun hasPermission(permission: String): Boolean =
        ContextCompat.checkSelfPermission(this, permission) == PackageManager.PERMISSION_GRANTED

    private fun ensureNotificationPermission() {
        // Notification permission is granted at install time below API 33.
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (!hasPermission(Manifest.permission.POST_NOTIFICATIONS)) {
            requestNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    /** Back [OmnigentWebChromeClient.onShowFileChooser] with a document picker. */
    private fun chooseFiles(
        callback: ValueCallback<Array<Uri>>,
        acceptTypes: Array<String>,
    ): Boolean {
        pendingFileCallback?.onReceiveValue(null) // cancel any in-flight chooser
        pendingFileCallback = callback
        // Keep MIME types as-is; resolve ".pdf"-style extension tokens to MIME so
        // the declared accept constraint isn't silently widened to */*.
        val mimeTypes =
            acceptTypes
                .mapNotNull(
                    ::mimeTypeFor,
                ).toTypedArray()
                .ifEmpty { arrayOf("*/*") }
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
            token.isEmpty() -> {
                null
            }

            token.contains('/') -> {
                token
            }

            // already a MIME type / wildcard
            else -> {
                MimeTypeMap
                    .getSingleton()
                    .getMimeTypeFromExtension(token.removePrefix(".").lowercase())
            }
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

    private fun downloadFile(
        url: String,
        contentDisposition: String?,
        mimeType: String?,
    ) {
        val name = URLUtil.guessFileName(url, contentDisposition, mimeType)

        // Agent-generated files arrive as blob:/data: URLs, which DownloadManager
        // can't handle — fetch them in page context and save via the blob bridge
        // (fixes omnigent-ai/omnigent#969, which the iOS shell leaves broken).
        if (url.startsWith("blob:") || url.startsWith("data:")) {
            webView.evaluateJavascript(BlobDownloadScript.fetchAsBase64(url, name), null)
            return
        }

        // Same normalization as the navigation gate: accepts odd casing
        // ("HTTPS://") and rejects non-http lookalikes ("httpfoo:"), which
        // DownloadManager.Request would otherwise throw on.
        if (!isHttpScheme(Uri.parse(url).scheme)) return
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

        // The 48dp floor is tap safety, not the iOS 120dp visual floor.
        const val SWITCHER_MAX_WIDTH_DP = 172
        const val SWITCHER_MIN_WIDTH_DP = 48
        const val SWITCHER_CONTROL_RESERVE_DP = 48

        // Back-press fallback: long enough that a healthy renderer's JS round-trip
        // (a few ms) always wins the race, short enough to not feel stuck if it
        // doesn't answer. Only the timer ever fires when the renderer is gone.
        const val BACK_FALLBACK_MS = 600L
    }
}
