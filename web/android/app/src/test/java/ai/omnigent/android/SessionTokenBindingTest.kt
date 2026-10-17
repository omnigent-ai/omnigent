package ai.omnigent.android

import android.content.Intent
import android.view.View
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
    private val pinned = "https://example.com:9443"
    private val token = "eyJ.eyJ.sig"

    @Test
    fun `session cookie includes HttpOnly flag`() {
        val activity = launch()
        var capturedCookie = ""
        activity.installSessionCookie = { _, cookie, callback ->
            capturedCookie = cookie
            callback(true)
        }
        activity.onSessionToken(token)
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

    private fun webViewOf(activity: MainActivity): WebView {
        fun find(view: View): WebView? =
            when (view) {
                is WebView -> {
                    view
                }

                is android.view.ViewGroup -> {
                    (0 until view.childCount).mapNotNull { find(view.getChildAt(it)) }.firstOrNull()
                }

                else -> {
                    null
                }
            }
        return checkNotNull(find(activity.window.decorView))
    }

    private fun launch(): MainActivity {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(pinned)
        return Robolectric.buildActivity(MainActivity::class.java).setup().get()
    }

    private fun MainActivity.onSessionToken(token: String) {
        MainActivity::class
            .java
            .getDeclaredMethod("onSessionToken", String::class.java)
            .apply { isAccessible = true }
            .invoke(this, token)
    }
}
