package ai.omnigent.android

import android.webkit.CookieManager
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class SessionTokenBindingTest {
    // JWT-shaped (three base64url segments) so isJwtShaped passes.
    private val token = "aGVhZGVy.cGF5bG9hZA.c2lnbmF0dXJl"
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
}
