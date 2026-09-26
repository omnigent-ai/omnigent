package ai.omnigent.android

import android.content.Intent
import android.view.View
import android.view.ViewGroup
import android.webkit.CookieManager
import android.webkit.RenderProcessGoneDetail
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

    @Test
    fun `session cookie includes HttpOnly flag`() {
        val activity = launch()
        var capturedCookie = ""
        activity.installSessionCookie = { _, cookie, callback ->
            capturedCookie = cookie
            callback(true)
        }
        activity.onSessionToken(pinned, token)
        assertTrue("Cookie should contain HttpOnly flag", capturedCookie.contains("; HttpOnly"))
    }

    @Test
    fun `switching to another port on the same host expires the session before loading`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(pinned)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        val writes = mutableListOf<String>()
        activity.installSessionCookie = { _, cookie, callback ->
            writes += cookie
            callback(true)
        }
        activity.readCookies = { null }

        val otherPort = "https://example.com:8443"
        ServerStore(ApplicationProvider.getApplicationContext()).connect(otherPort)
        controller.newIntent(Intent(activity, MainActivity::class.java))

        assertTrue(writes.any { it.startsWith("ap_session=;") && "Max-Age=0" in it })
        assertTrue(writes.any { it.startsWith("__Host-ap_session=;") && "Max-Age=0" in it })
        val lastLoaded = checkNotNull(shadowOf(webViewOf(activity)).lastLoadedUrl)
        assertTrue(lastLoaded.startsWith(otherPort))
    }

    @Test
    fun `switching to another host leaves session cookies alone`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(pinned)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        val writes = mutableListOf<String>()
        activity.installSessionCookie = { _, cookie, callback ->
            writes += cookie
            callback(true)
        }

        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://switched.example")
        controller.newIntent(Intent(activity, MainActivity::class.java))

        assertTrue(writes.isEmpty())
    }

    // Robolectric's CookieManager ignores expiry writes, so record them at the seam,
    // installed before onCreate runs the first load.
    private fun launchRecordingCookieWrites(writes: MutableList<String>): MainActivity {
        val controller = Robolectric.buildActivity(MainActivity::class.java)
        controller.get().installSessionCookie = { _, cookie, callback ->
            writes += cookie
            callback(true)
        }
        controller.get().readCookies = { null }
        return controller.setup().get()
    }

    @Test
    fun `cold start drops a session another port on the same host left behind`() {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        ServerStore(context).connect(pinned)
        ServerStore(context).recordSessionCookieOwner("example.com", "https://example.com:8443")
        val writes = mutableListOf<String>()

        val activity = launchRecordingCookieWrites(writes)

        assertTrue(writes.any { it.startsWith("ap_session=;") && "Max-Age=0" in it })
        assertTrue(writes.any { it.startsWith("__Host-ap_session=;") && "Max-Age=0" in it })
        assertTrue(checkNotNull(shadowOf(webViewOf(activity)).lastLoadedUrl).startsWith(pinned))
        assertTrue(ServerStore(context).sessionCookieOwner("example.com") == pinned)
    }

    @Test
    fun `an unrecorded host shared with another saved server is cleared before loading`() {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        ServerStore(context).connect("https://example.com:8443")
        ServerStore(context).connect(pinned)
        val writes = mutableListOf<String>()

        launchRecordingCookieWrites(writes)

        assertTrue(writes.any { it.startsWith("ap_session=;") && "Max-Age=0" in it })
    }

    @Test
    fun `an unrecorded host with no other saved server keeps its session`() {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        ServerStore(context).connect(pinned)
        val writes = mutableListOf<String>()

        launchRecordingCookieWrites(writes)

        assertTrue(writes.isEmpty())
        assertTrue(ServerStore(context).sessionCookieOwner("example.com") == pinned)
    }

    @Test
    fun `a failed session expiry returns to the server picker instead of loading`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(pinned)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        val loadedBefore = shadowOf(webViewOf(activity)).lastLoadedUrl
        activity.installSessionCookie = { _, _, callback -> callback(false) }

        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com:8443")
        controller.newIntent(Intent(activity, MainActivity::class.java))

        assertTrue(shadowOf(webViewOf(activity)).lastLoadedUrl == loadedBefore)
        val next = checkNotNull(shadowOf(activity).nextStartedActivity)
        assertTrue(next.component?.className == ConnectActivity::class.java.name)
        assertTrue(activity.isFinishing)
    }

    @Test
    fun `a session cookie that survives the expiry blocks the load`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(pinned)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        val loadedBefore = shadowOf(webViewOf(activity)).lastLoadedUrl
        activity.installSessionCookie = { _, _, callback -> callback(true) }
        activity.readCookies = { "theme=dark; ap_session=parent-domain" }

        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com:8443")
        controller.newIntent(Intent(activity, MainActivity::class.java))

        assertTrue(shadowOf(webViewOf(activity)).lastLoadedUrl == loadedBefore)
        val next = checkNotNull(shadowOf(activity).nextStartedActivity)
        assertTrue(next.component?.className == ConnectActivity::class.java.name)
        assertTrue(activity.isFinishing)
    }

    @Test
    fun `renderer recovery mid-switch releases the other port's session before loading`() {
        val context = ApplicationProvider.getApplicationContext<android.content.Context>()
        ServerStore(context).connect(pinned)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        val held = mutableListOf<(Boolean) -> Unit>()
        activity.installSessionCookie = { _, _, callback -> held += callback }
        activity.readCookies = { null }

        val otherPort = "https://example.com:8443"
        ServerStore(context).connect(otherPort)
        controller.newIntent(Intent(activity, MainActivity::class.java))
        val dead = webViewOf(activity)
        dead.webViewClient.onRenderProcessGone(dead, rendererGone())

        val recovered = webViewOf(activity)
        assertFalse(shadowOf(recovered).lastLoadedUrl?.startsWith(otherPort) == true)
        activity.installSessionCookie = { _, _, callback -> callback(true) }
        held.last()(true)
        held.first()(true) // the superseded switch's late callback must do nothing
        assertTrue(checkNotNull(shadowOf(recovered).lastLoadedUrl).startsWith(otherPort))
    }

    private fun rendererGone() =
        object : RenderProcessGoneDetail() {
            override fun didCrash(): Boolean = false

            override fun rendererPriorityAtExit(): Int = WebView.RENDERER_PRIORITY_IMPORTANT
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
