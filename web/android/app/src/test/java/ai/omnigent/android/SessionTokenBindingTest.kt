package ai.omnigent.android

import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class SessionTokenBindingTest {
    private val pinned = "https://example.com:9443"
    private val token = "eyJ.eyJ.sig"

    @Test
    fun `session cookie includes HttpOnly flag`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(pinned)
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        var capturedCookie = ""
        setInstallSessionCookie(activity) { _, cookie, callback ->
            capturedCookie = cookie
            callback(true)
        }
        activity.onSessionToken(token)
        assertTrue(
            "Cookie should contain HttpOnly flag",
            capturedCookie.contains("; HttpOnly")
        )
    }

    private fun setInstallSessionCookie(
        activity: MainActivity,
        block: (String, String, (Boolean) -> Unit) -> Unit,
    ) {
        MainActivity::class
            .java
            .getDeclaredField("installSessionCookie")
            .apply { isAccessible = true }
            .set(activity, block)
    }

    private fun MainActivity.onSessionToken(token: String) {
        MainActivity::class
            .java
            .getDeclaredMethod("onSessionToken", String::class.java)
            .apply { isAccessible = true }
            .invoke(this, token)
    }
}
