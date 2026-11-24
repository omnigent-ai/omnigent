package ai.omnigent.android

import android.app.AlertDialog
import android.app.Application
import android.content.Context
import android.content.Intent
import android.content.RestrictionsManager
import android.net.Uri
import android.os.Bundle
import android.os.Looper
import android.widget.TextView
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
import org.robolectric.shadow.api.Shadow
import org.robolectric.shadows.ShadowAlertDialog
import org.robolectric.shadows.ShadowRestrictionsManager

/** Which server an `omnigent://` link connects the app to, and when it asks first. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DeepLinkActivityTest {
    private val context: Context = ApplicationProvider.getApplicationContext()
    private val store get() = ServerStore(context)

    @Test
    fun `only omnigent links reach the app, from a browser or another app`() {
        assertEquals(
            listOf(DeepLinkActivity::class.java.name),
            receivers("omnigent://example.test/c/abc"),
        )
        assertEquals(
            listOf(DeepLinkActivity::class.java.name),
            receivers("omnigent://localhost:8000/c/abc?file=a.kt"),
        )
        assertEquals(emptyList<String>(), receivers("https://example.test/c/abc"))
    }

    @Test
    fun `a link to the current server opens its conversation without asking`() {
        store.connect("https://example.test")

        val opened = follow("omnigent://example.test/c/abc?file=a.kt")

        assertNull(ShadowAlertDialog.getLatestAlertDialog())
        assertEquals("https://example.test", store.currentServerUrl())
        assertEquals(MainActivity::class.java.name, opened?.component?.className)
        assertEquals(
            "/c/abc?file=a.kt",
            opened?.getStringExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH),
        )
    }

    @Test
    fun `a link to an earlier server switches to it as the user entered it`() {
        store.connect("https://workspace.example.test/?o=123")
        store.connect("https://example.test")

        val opened = follow("omnigent://workspace.example.test/c/abc")

        assertNull(ShadowAlertDialog.getLatestAlertDialog())
        // The workspace selector comes back with the server; the link's host alone would lose it.
        assertEquals(
            listOf("https://workspace.example.test/?o=123", "https://example.test"),
            store.recentServers(),
        )
        assertEquals(
            "/c/abc",
            opened?.getStringExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH),
        )
        // Tapping a link is not asking to connect, so it is not marked as a connect request.
        assertFalse(opened!!.hasExtra(MainActivity.EXTRA_CONNECT_REQUEST))
    }

    @Test
    fun `a link to a server the user never chose connects only after they agree`() {
        store.connect("https://example.test")

        val controller = start("omnigent://evil.example.test/c/abc")
        val prompt = ShadowAlertDialog.getLatestAlertDialog()

        assertTrue(prompt.isShowing)
        assertTrue(messageOf(prompt).contains("evil.example.test"))
        assertEquals("https://example.test", store.currentServerUrl())
        assertNull(nextStarted())

        prompt.getButton(AlertDialog.BUTTON_POSITIVE).performClick()
        shadowOf(Looper.getMainLooper()).idle()

        assertEquals("https://evil.example.test", store.currentServerUrl())
        val opened = nextStarted()
        assertEquals(
            "/c/abc",
            opened?.getStringExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH),
        )
        // Agreeing to the prompt is asking to connect.
        assertTrue(
            UserConnectRequests.consume(opened?.getStringExtra(MainActivity.EXTRA_CONNECT_REQUEST)),
        )
        assertTrue(controller.get().isFinishing)
    }

    @Test
    fun `declining the prompt leaves the app where it was`() {
        store.connect("https://example.test")

        val controller = start("omnigent://evil.example.test/c/abc")
        ShadowAlertDialog
            .getLatestAlertDialog()
            .getButton(
                AlertDialog.BUTTON_NEGATIVE,
            ).performClick()
        shadowOf(Looper.getMainLooper()).idle()

        assertEquals("https://example.test", store.currentServerUrl())
        assertEquals(listOf("https://example.test"), store.recentServers())
        assertNull(nextStarted())
        assertTrue(controller.get().isFinishing)
    }

    // A preset is an offer the user has not taken, so a link must not take it for them.
    @Test
    fun `an organization's preset is asked about, then connected as the organization wrote it`() {
        Shadow
            .extract<ShadowRestrictionsManager>(
                context.getSystemService(RestrictionsManager::class.java),
            ).setApplicationRestrictions(
                Bundle().apply {
                    putString(
                        ManagedConfig.KEY_SERVER_URLS,
                        "https://workspace.example.test/?o=123",
                    )
                },
            )

        start("omnigent://workspace.example.test/c/abc")
        val prompt = ShadowAlertDialog.getLatestAlertDialog()
        assertFalse(store.hasServer())

        prompt.getButton(AlertDialog.BUTTON_POSITIVE).performClick()
        shadowOf(Looper.getMainLooper()).idle()

        assertEquals(listOf("https://workspace.example.test/?o=123"), store.recentServers())
    }

    @Test
    fun `a link the app does not open shows the app and changes nothing`() {
        store.connect("https://example.test")

        val opened = follow("omnigent://evil.example.test/settings")

        assertNull(ShadowAlertDialog.getLatestAlertDialog())
        assertEquals("https://example.test", store.currentServerUrl())
        assertEquals(MainActivity::class.java.name, opened?.component?.className)
        assertFalse(opened!!.hasExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH))
        // A launcher-style start resumes the task as it is. CLEAR_TOP would close a screen left
        // open above the conversation, such as the server picker with a URL half typed.
        assertEquals(Intent.ACTION_MAIN, opened.action)
        assertTrue(opened.hasCategory(Intent.CATEGORY_LAUNCHER))
        assertEquals(0, opened.flags and Intent.FLAG_ACTIVITY_CLEAR_TOP)
        assertEquals(0, opened.flags and Intent.FLAG_ACTIVITY_CLEAR_TASK)
        assertTrue(opened.flags and Intent.FLAG_ACTIVITY_NEW_TASK != 0)
    }

    private fun start(link: String) =
        Robolectric
            .buildActivity(
                DeepLinkActivity::class.java,
                Intent(Intent.ACTION_VIEW, Uri.parse(link)),
            ).setup()

    /** Follows [link] and returns what it started, for links that need no prompt. */
    private fun follow(link: String): Intent? {
        val controller = start(link)
        assertTrue(controller.get().isFinishing)
        return nextStarted()
    }

    private fun nextStarted(): Intent? = shadowOf(context as Application).nextStartedActivity

    private fun messageOf(dialog: AlertDialog): String =
        dialog.findViewById<TextView>(android.R.id.message).text.toString()

    private fun receivers(uri: String): List<String> =
        context.packageManager
            .queryIntentActivities(
                Intent(Intent.ACTION_VIEW, Uri.parse(uri)).addCategory(Intent.CATEGORY_BROWSABLE),
                0,
            ).map { it.activityInfo.name }
}
