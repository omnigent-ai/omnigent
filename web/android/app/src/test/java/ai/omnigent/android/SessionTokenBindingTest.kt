package ai.omnigent.android

import android.content.Intent
import android.view.View
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.WebView
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class SessionTokenBindingTest {
    // JWT-shaped (three base64url segments) so isJwtShaped passes. Built from
    // plain words at runtime — this is not a credential.
    private val token =
        listOf("header", "payload", "signature").joinToString(".") {
            java.util.Base64
                .getUrlEncoder()
                .withoutPadding()
                .encodeToString(it.toByteArray())
        }
    private val pinned = "https://example.com"

    private fun launch(): MainActivity {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(pinned)
        return Robolectric.buildActivity(MainActivity::class.java).setup().get()
    }

    @Test
    fun `token from a different origin is dropped`() {
        val activity = launch()
        activity.onSessionToken("https://other.example", token)
        val cookie = CookieManager.getInstance().getCookie(pinned)
        assertFalse(cookie?.contains("ap_session") == true)
    }

    @Test
    fun `token from the pinned origin is injected`() {
        val activity = launch()
        activity.onSessionToken(pinned, token)
        val cookie = CookieManager.getInstance().getCookie(pinned)
        assertTrue(cookie?.contains("ap_session") == true)
    }

    @Test
    fun `cookie install completing after a server switch does not steer back to the old origin`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(pinned)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()

        // Hold setCookie's async completion so a server switch can land in the
        // gap between the origin check and the post-install reload.
        var held: ((Boolean) -> Unit)? = null
        activity.installSessionCookie = { _, _, callback -> held = callback }
        activity.onSessionToken(pinned, token)

        val switched = "https://switched.example"
        ServerStore(ApplicationProvider.getApplicationContext()).connect(switched)
        controller.newIntent(Intent(activity, MainActivity::class.java))

        // The install completes only now, after the switch. Without the
        // callback's origin re-check, this would loadUrl(pinned) — steering
        // the WebView back to the server the user just left.
        held!!(true)

        val lastLoaded = checkNotNull(shadowOf(webViewOf(activity)).lastLoadedUrl)
        assertTrue(lastLoaded.startsWith(switched))
    }

    private fun webViewOf(activity: MainActivity): WebView {
        fun find(view: View): WebView? =
            when (view) {
                is WebView -> {
                    view
                }

                is ViewGroup -> {
                    (0 until view.childCount).firstNotNullOfOrNull { find(view.getChildAt(it)) }
                }

                else -> {
                    null
                }
            }
        return checkNotNull(find(activity.findViewById(android.R.id.content)))
    }
}
