package ai.omnigent.android

import android.content.Intent
import android.net.Uri
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DeepLinkRoutingTest {
    private val hex = TEST_CONVERSATION_ID

    private fun store() = testStore()

    @Test
    fun `manifest resolves omnigent view intents to the DeepLinkActivity trampoline`() {
        val pm = ApplicationProvider.getApplicationContext<android.content.Context>().packageManager
        val resolved = viewIntent("omnigent://h.example/c/$hex").resolveActivity(pm)
        assertNotNull(resolved)
        assertEquals(DeepLinkActivity::class.java.name, resolved.className)
    }

    @Test
    fun `DeepLinkActivity forwards to MainActivity with NEW_TASK, CLEAR_TOP, SINGLE_TOP`() {
        val activity =
            Robolectric
                .buildActivity(
                    DeepLinkActivity::class.java,
                    viewIntent("omnigent://h.example/c/$hex"),
                ).setup()
                .get()
        assertTrue(activity.isFinishing)
        val next = shadowOf(activity).nextStartedActivity
        assertNotNull(next)
        assertEquals(MainActivity::class.java.name, next.component?.className)
        assertEquals(Intent.ACTION_VIEW, next.action)
        assertEquals(Uri.parse("omnigent://h.example/c/$hex"), next.data)
        val expectedFlags =
            Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP or
                Intent.FLAG_ACTIVITY_SINGLE_TOP
        assertEquals(expectedFlags, next.flags and expectedFlags)
    }

    @Test
    fun `same-origin link queues its path for the SPA`() {
        store().connect("https://h.example")
        val activity =
            Robolectric
                .buildActivity(MainActivity::class.java, viewIntent("omnigent://h.example/c/$hex"))
                .setup()
                .get()
        assertEquals("/c/$hex", activity.privateField("pendingNavigatePath"))
        assertEquals("https://h.example", activity.privateField("pendingNavigateOrigin"))
        // Same origin: no reload away from the stored server.
        assertEquals("https://h.example", originOf(shadowOf(activity.testWebView()).lastLoadedUrl))
    }

    @Test
    fun `known-server link switches to the stored url including its mount`() {
        store().connect("https://ws.example/ml/omnigents")
        store().connect("https://current.example")
        val activity =
            Robolectric
                .buildActivity(MainActivity::class.java, viewIntent("omnigent://ws.example/c/$hex"))
                .setup()
                .get()
        // Switched: pinned to the link's origin, loading the stored (mounted) URL.
        assertEquals("https://ws.example", activity.privateField("pinnedOrigin"))
        assertEquals(
            "https://ws.example/ml/omnigents",
            shadowOf(activity.testWebView()).lastLoadedUrl,
        )
        assertEquals("/c/$hex", activity.privateField("pendingNavigatePath"))
        assertEquals("https://ws.example/ml/omnigents", store().currentServerUrl())
    }

    @Test
    fun `warm same-origin link arrives via onNewIntent`() {
        store().connect("https://h.example")
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        controller.newIntent(viewIntent("omnigent://h.example/c/$hex"))
        assertEquals("/c/$hex", activity.privateField("pendingNavigatePath"))
    }

    @Test
    fun `same-origin link after a failed load waits for a successful retry`() {
        store().connect("https://h.example")
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        activity.invokeOnPageReady("https://h.example", mainFrameLoadFailed = true)

        controller.newIntent(viewIntent("omnigent://h.example/c/$hex"))

        assertFalse(activity.privateField("pageLoaded") as Boolean)
        assertEquals("/c/$hex", activity.privateField("pendingNavigatePath"))

        activity.invokeOnPageReady("https://h.example")
        assertNull(activity.privateField("pendingNavigatePath"))
    }

    @Test
    fun `rejected link is ignored`() {
        store().connect("https://h.example")
        val activity =
            Robolectric
                .buildActivity(
                    MainActivity::class.java,
                    viewIntent("omnigent://h.example/c/$hex?view=terminal"),
                ).setup()
                .get()
        assertNull(activity.privateField("pendingNavigatePath"))
    }

    @Test
    fun `cold start with no server and a rejected link still routes to ConnectActivity`() {
        // Fresh install: no store().connect(...) call.
        val activity =
            Robolectric
                .buildActivity(
                    MainActivity::class.java,
                    viewIntent("omnigent://h.example/c/$hex?view=terminal"),
                ).setup()
                .get()
        assertTrue(activity.isFinishing)
        val next = shadowOf(activity).nextStartedActivity
        assertNotNull(next)
        assertEquals(ConnectActivity::class.java.name, next.component?.className)
    }
}
