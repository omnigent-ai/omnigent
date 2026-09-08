package ai.omnigent.android

import android.net.Uri
import android.os.Looper
import android.view.View
import android.view.ViewGroup
import android.webkit.WebResourceRequest
import android.webkit.WebView
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertFalse
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowDialog
import org.robolectric.shadows.ShadowToast

/**
 * On an OIDC-auth server the shell cancels the off-origin IdP bounce and runs
 * the RFC 8252 system-browser login in the background — the WebView must not
 * keep presenting the last-painted SPA document, or the app looks signed in
 * while every API call is 401. And once the capped login retries are
 * exhausted, giving up must not be silent.
 *
 * These tests drive the user journey through the real [MainActivity] and its
 * [OmnigentWebViewClient] exactly as Android delivers it (the server's
 * main-frame bounce to the IdP arrives as an off-origin page start / redirect)
 * and assert the user-observable outcome: while a native login is pending, the
 * stale SPA page must not remain presented as-if-authenticated, and retry
 * exhaustion must surface a visible error. They fail on the current code and
 * become the regression guard for the fix.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class LoginPendingUiStateTest {
    @Test
    fun `off-origin idp landing must not leave the stale spa presented`() {
        val activity = launchConnectedActivity()
        val webView = activity.webView()
        val container = webView.parent as ViewGroup
        val childrenBefore = container.childCount

        // Journey step: the OIDC server answers the app load with a main-frame
        // bounce to its IdP; Android delivers it as an off-origin page start.
        webView.webViewClient.onPageStarted(webView, IDP_URL, null)
        idleMainLooper()

        // The shell stopped the IdP load and launched the background browser
        // login. Until that login completes, the user must not be shown the
        // stale SPA home screen as if signed in: the shell has to navigate the
        // WebView away (blank / signing-in page), hide it, or cover it with a
        // pending-login view. None of those happening = the reported bug.
        val staleSpaStillPresented =
            shadowOf(webView).lastLoadedUrl == SERVER_URL &&
                webView.visibility == View.VISIBLE &&
                container.childCount == childrenBefore
        assertFalse(
            "WebView still presents the stale SPA as signed-in while the native " +
                "login runs in the background",
            staleSpaStillPresented,
        )
    }

    @Test
    fun `off-origin idp redirect must not leave the stale spa presented`() {
        val activity = launchConnectedActivity()
        val webView = activity.webView()
        val container = webView.parent as ViewGroup
        val childrenBefore = container.childCount

        // Same journey via the other interception path: the server-driven
        // (gestureless) redirect to the IdP reaches shouldOverrideUrlLoading.
        webView.webViewClient.shouldOverrideUrlLoading(webView, request(IDP_URL))
        idleMainLooper()

        val staleSpaStillPresented =
            shadowOf(webView).lastLoadedUrl == SERVER_URL &&
                webView.visibility == View.VISIBLE &&
                container.childCount == childrenBefore
        assertFalse(
            "WebView still presents the stale SPA as signed-in after the IdP " +
                "redirect was cancelled for background login",
            staleSpaStillPresented,
        )
    }

    @Test
    fun `exhausted login attempts must surface a visible error`() {
        val activity = launchConnectedActivity()
        val webView = activity.webView()
        val container = webView.parent as ViewGroup

        // Journey: the user never completes the browser login (dismissed it),
        // so every retry re-bounces to the IdP until the budget is spent.
        activity.setLoginAttempts(MAX_LOGIN_ATTEMPTS)
        val childrenBefore = container.childCount

        // One more IdP bounce past the exhausted budget: startLogin() gives up.
        webView.webViewClient.onPageStarted(webView, IDP_URL, null)
        idleMainLooper()

        // Giving up must be user-visible — a toast, a dialog, an error page, a
        // hidden WebView, or an error view. A silent log-only return leaves the
        // user stuck on a fake signed-in screen forever = the reported bug.
        val failedSilently =
            ShadowToast.getLatestToast() == null &&
                ShadowDialog.getLatestDialog() == null &&
                shadowOf(webView).lastLoadedUrl == SERVER_URL &&
                webView.visibility == View.VISIBLE &&
                container.childCount == childrenBefore
        assertFalse(
            "Shell gives up after MAX_LOGIN_ATTEMPTS with no user-visible error",
            failedSilently,
        )
    }

    /** Connect to an OIDC-auth (system-browser login) server and launch the shell. */
    private fun launchConnectedActivity(): MainActivity {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(SERVER_URL)
        return Robolectric.buildActivity(MainActivity::class.java).setup().get()
    }

    /** Run posted work (the client's mainHandler bounces) before asserting. */
    private fun idleMainLooper() = shadowOf(Looper.getMainLooper()).idle()

    private fun MainActivity.webView(): WebView =
        MainActivity::class
            .java
            .getDeclaredField("webView")
            .apply { isAccessible = true }
            .get(this) as WebView

    private fun MainActivity.setLoginAttempts(value: Int) {
        MainActivity::class
            .java
            .getDeclaredField("loginAttempts")
            .apply { isAccessible = true }
            .setInt(this, value)
    }

    private fun request(
        url: String,
        hasGesture: Boolean = false,
    ) = object : WebResourceRequest {
        override fun getUrl(): Uri = Uri.parse(url)

        override fun isForMainFrame(): Boolean = true

        override fun isRedirect(): Boolean = !hasGesture

        override fun hasGesture(): Boolean = hasGesture

        override fun getMethod(): String = "GET"

        override fun getRequestHeaders(): Map<String, String> = emptyMap()
    }

    private companion object {
        // Loopback with nothing listening: the background OidcLoginManager flow
        // fails fast instead of doing real network from the unit test.
        const val SERVER_URL = "http://127.0.0.1:9"

        const val IDP_URL = "https://idp.example.org/authorize?state=abc"

        // Mirrors MainActivity.MAX_LOGIN_ATTEMPTS (private companion const).
        const val MAX_LOGIN_ATTEMPTS = 3
    }
}
