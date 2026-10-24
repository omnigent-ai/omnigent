package ai.omnigent.android

import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Looper
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowAlertDialog

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DeepLinkActivityTest {
    private val context = ApplicationProvider.getApplicationContext<Context>()
    private val store = ServerStore(context)

    @Test
    fun `a link to the current server opens the conversation without reconnecting`() {
        store.connect("https://omnigent.example.com")

        val opened = launch("omnigent://omnigent.example.com/c/abc").openedShell()

        assertEquals("/c/abc", opened.navigatePath())
        assertNull(opened.getStringExtra(MainActivity.EXTRA_CONNECT_REQUEST))
        assertEquals(listOf("https://omnigent.example.com"), store.recentServers())
    }

    @Test
    fun `a link to a server the user already chose switches to it without asking`() {
        store.connect("https://proxy.example.com/omnigent")
        store.connect("https://omnigent.example.com")

        val opened = launch("omnigent://proxy.example.com/c/abc").openedShell()

        assertNull(ShadowAlertDialog.getLatestAlertDialog())
        assertEquals("https://proxy.example.com/omnigent", store.currentServerUrl())
        assertEquals("/c/abc", opened.navigatePath())
        assertNull(opened.connectRequest())
    }

    @Test
    fun `an unknown server opens only after the user agrees to connect`() {
        val activity = launch("omnigent://new.example.com:8443/c/abc")
        val consent = ShadowAlertDialog.getLatestAlertDialog()

        assertTrue(consent.isShowing)
        assertEquals(
            "This link will connect Omnigent to new.example.com:8443 and open a conversation." +
                "\n\nOnly open links from a server you trust.",
            shadowOf(consent).message.toString(),
        )
        assertNull(shadowOf(activity).peekNextStartedActivity())
        assertFalse(store.hasServer())
        assertTrue(consent.getButton(AlertDialog.BUTTON_POSITIVE).filterTouchesWhenObscured)

        consent.getButton(AlertDialog.BUTTON_POSITIVE).performClick()
        shadowOf(Looper.getMainLooper()).idle()

        val opened = activity.openedShell()
        assertEquals("https://new.example.com:8443", store.currentServerUrl())
        assertEquals(emptyList<String>(), store.recentServers())
        assertEquals("/c/abc", opened.navigatePath())
        assertTrue(UserConnectRequests.consume(opened.connectRequest()))
    }

    @Test
    fun `a server that has not loaded since consent asks again`() {
        store.select("https://new.example.com")

        val activity = launch("omnigent://new.example.com/c/abc")

        assertTrue(ShadowAlertDialog.getLatestAlertDialog().isShowing)
        assertNull(shadowOf(activity).peekNextStartedActivity())
    }

    @Test
    fun `declining an unknown server leaves the app untouched`() {
        store.connect("https://omnigent.example.com")
        val activity = launch("omnigent://new.example.com/c/abc")

        val consent = ShadowAlertDialog.getLatestAlertDialog()
        consent.getButton(AlertDialog.BUTTON_NEGATIVE).performClick()
        shadowOf(Looper.getMainLooper()).idle()

        assertTrue(activity.isFinishing)
        assertNull(shadowOf(activity).peekNextStartedActivity())
        assertEquals(listOf("https://omnigent.example.com"), store.recentServers())
    }

    @Test
    fun `a malformed link opens the app without connecting or navigating`() {
        store.connect("https://omnigent.example.com")

        val opened = launch("omnigent://unknown.example.com/c/abc%3Fview=terminal").openedShell()

        assertNull(ShadowAlertDialog.getLatestAlertDialog())
        assertNull(opened.navigatePath())
        assertNull(opened.connectRequest())
        assertEquals(listOf("https://omnigent.example.com"), store.recentServers())
    }

    private fun launch(link: String): DeepLinkActivity =
        Robolectric
            .buildActivity(
                DeepLinkActivity::class.java,
                Intent(Intent.ACTION_VIEW, Uri.parse(link)),
            ).setup()
            .get()

    private fun DeepLinkActivity.openedShell(): Intent {
        val started = shadowOf(this).nextStartedActivity
        val reentry =
            Intent.FLAG_ACTIVITY_NEW_TASK or
                Intent.FLAG_ACTIVITY_CLEAR_TOP or
                Intent.FLAG_ACTIVITY_SINGLE_TOP
        assertEquals(MainActivity::class.java.name, started.component?.className)
        assertEquals(reentry, started.flags and reentry)
        assertTrue(isFinishing)
        return started
    }

    private fun Intent.navigatePath(): String? =
        getStringExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH)

    private fun Intent.connectRequest(): String? =
        getStringExtra(MainActivity.EXTRA_CONNECT_REQUEST)
}
