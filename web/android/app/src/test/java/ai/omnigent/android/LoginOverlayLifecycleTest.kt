package ai.omnigent.android

import android.os.Looper
import android.view.View
import android.view.ViewGroup
import android.webkit.WebView
import android.widget.Button
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

/**
 * Lifecycle of the pending-login cover: it must hide the stale SPA the moment
 * the system-browser login starts, come down once an authenticated
 * pinned-origin page loads, and offer a working retry once the login retry
 * budget is exhausted. Complements [LoginPendingUiStateTest], which asserts
 * the bug's user-visible symptoms never come back.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class LoginOverlayLifecycleTest {
    @Test
    fun `pending login covers the webview until an app page loads`() {
        val activity = launchConnectedActivity()
        val webView = activity.webView()

        // The server bounces the main frame to its IdP: background login starts.
        webView.webViewClient.onPageStarted(webView, IDP_URL, null)
        idleMainLooper()

        val overlay = activity.overlay()
        assertEquals("overlay must cover the stale SPA", View.VISIBLE, overlay.visibility)
        assertEquals("stale SPA must not stay presented", View.INVISIBLE, webView.visibility)

        // Login completed: the authenticated pinned-origin page finishes loading.
        webView.webViewClient.onPageFinished(webView, SERVER_URL)
        idleMainLooper()

        assertEquals("overlay must come down", View.GONE, overlay.visibility)
        assertEquals("app content must be presented", View.VISIBLE, webView.visibility)
    }

    @Test
    fun `retry from the exhausted-budget error re-arms the login budget`() {
        val activity = launchConnectedActivity()
        val webView = activity.webView()
        activity.setLoginAttempts(MAX_LOGIN_ATTEMPTS)

        // One more IdP bounce past the exhausted budget: the shell gives up,
        // which must surface the error overlay instead of a silent return.
        webView.webViewClient.onPageStarted(webView, IDP_URL, null)
        idleMainLooper()
        val overlay = activity.overlay()
        assertEquals("giving up must be visible", View.VISIBLE, overlay.visibility)
        val retry = overlay.retryButton()
        assertEquals("error state must offer retry", View.VISIBLE, retry.visibility)

        // A deliberate tap restarts the flow with a fresh budget.
        retry.performClick()
        assertEquals("retry must start a fresh attempt", 1, activity.loginAttempts())
        assertEquals("login must stay covered", View.VISIBLE, overlay.visibility)
    }

    private fun launchConnectedActivity(): MainActivity {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(SERVER_URL)
        return Robolectric.buildActivity(MainActivity::class.java).setup().get()
    }

    private fun idleMainLooper() = shadowOf(Looper.getMainLooper()).idle()

    private fun MainActivity.webView(): WebView =
        MainActivity::class
            .java
            .getDeclaredField("webView")
            .apply { isAccessible = true }
            .get(this) as WebView

    private fun MainActivity.overlay(): PendingLoginOverlay {
        val container = webView().parent as ViewGroup
        return (0 until container.childCount)
            .map(container::getChildAt)
            .filterIsInstance<PendingLoginOverlay>()
            .single()
    }

    private fun PendingLoginOverlay.retryButton(): Button =
        (0 until childCount)
            .map(::getChildAt)
            .filterIsInstance<Button>()
            .single()

    private fun MainActivity.setLoginAttempts(value: Int) {
        MainActivity::class
            .java
            .getDeclaredField("loginAttempts")
            .apply { isAccessible = true }
            .setInt(this, value)
    }

    private fun MainActivity.loginAttempts(): Int =
        MainActivity::class
            .java
            .getDeclaredField("loginAttempts")
            .apply { isAccessible = true }
            .getInt(this)

    private companion object {
        // Loopback with nothing listening: the background login flow fails fast
        // instead of doing real network from the unit test.
        const val SERVER_URL = "http://127.0.0.1:9"

        const val IDP_URL = "https://idp.example.org/authorize?state=abc"

        // Mirrors MainActivity.MAX_LOGIN_ATTEMPTS (private companion const).
        const val MAX_LOGIN_ATTEMPTS = 3
    }
}
