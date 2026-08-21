package ai.omnigent.android

import android.content.Intent
import android.net.Uri
import android.webkit.ValueCallback
import android.webkit.WebView
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
    fun `same-origin link after a failed load starts a retry and drains`() {
        store().connect("https://h.example")
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        val recording = RecordingLoadWebView(activity, "https://h.example")
        activity.setPrivateField("webView", recording)
        activity.invokeOnPageReady("https://h.example", mainFrameLoadFailed = true)

        controller.newIntent(viewIntent("omnigent://h.example/c/$hex"))

        assertFalse(activity.privateField("pageLoaded") as Boolean)
        assertFalse(activity.privateField("pinnedOriginLoadFailed") as Boolean)
        assertEquals("/c/$hex", activity.privateField("pendingNavigatePath"))
        assertEquals(1, recording.reloadCount)

        activity.invokeOnPageReady("https://h.example")
        assertNull(activity.privateField("pendingNavigatePath"))
        assertFalse(activity.privateField("processingDeepLink") as Boolean)
    }

    @Test
    fun `different-origin link clears a pinned-origin failure and drains`() {
        store().connect("https://next.example/mount")
        store().connect("https://h.example")
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        val recording = RecordingLoadWebView(activity, "https://h.example")
        activity.setPrivateField("webView", recording)
        activity.invokeOnPageReady("https://h.example", mainFrameLoadFailed = true)

        controller.newIntent(viewIntent("omnigent://next.example/c/$hex"))

        assertEquals("https://next.example", activity.privateField("pinnedOrigin"))
        assertFalse(activity.privateField("pinnedOriginLoadFailed") as Boolean)
        assertEquals(listOf("https://next.example/mount"), recording.loadedUrls)
        activity.invokeOnPageReady("https://next.example/mount")
        assertFalse(activity.privateField("processingDeepLink") as Boolean)
    }

    @Test
    fun `explicit retry clears a pinned-origin failure and drains`() {
        store().connect("https://h.example")
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        val recording = RecordingLoadWebView(activity, "https://h.example")
        activity.setPrivateField("webView", recording)
        activity.invokeOnPageReady("https://h.example", mainFrameLoadFailed = true)

        activity.invokeRetryPinnedOrigin()

        assertFalse(activity.privateField("pinnedOriginLoadFailed") as Boolean)
        assertEquals(1, recording.reloadCount)
        activity.invokeOnPageReady("https://h.example")
        assertTrue(activity.privateField("pageLoaded") as Boolean)
        assertFalse(activity.privateField("processingDeepLink") as Boolean)
    }

    @Test
    fun `failed head then same-origin retry then different-origin link drains FIFO`() {
        store().connect("https://next.example/mount")
        store().connect("https://h.example")
        val controller =
            Robolectric.buildActivity(
                MainActivity::class.java,
                viewIntent("omnigent://h.example/c/first-$hex"),
            )
        val activity = controller.setup().get()
        val recording = RecordingLoadWebView(activity, "https://h.example")
        activity.setPrivateField("webView", recording)
        controller.newIntent(viewIntent("omnigent://h.example/c/second-$hex"))
        controller.newIntent(viewIntent("omnigent://next.example/c/third-$hex"))

        activity.invokeOnPageReady("https://h.example", mainFrameLoadFailed = true)
        assertEquals("/c/second-$hex", activity.privateField("pendingNavigatePath"))
        assertFalse(activity.privateField("pinnedOriginLoadFailed") as Boolean)
        assertEquals(1, recording.reloadCount)

        activity.invokeOnPageReady("https://h.example", mainFrameLoadFailed = true)
        assertEquals("https://next.example", activity.privateField("pinnedOrigin"))
        assertFalse(activity.privateField("pinnedOriginLoadFailed") as Boolean)
        assertEquals(listOf("https://next.example/mount"), recording.loadedUrls)

        activity.invokeOnPageReady("https://next.example/mount")
        assertTrue((activity.privateField("deepLinkQueue") as ArrayDeque<*>).isEmpty())
        assertFalse(activity.privateField("processingDeepLink") as Boolean)
    }

    @Test
    fun `stale same-origin failure cannot resolve a newer retry head`() {
        store().connect("https://h.example")
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        val initialGeneration = activity.privateField("loadGeneration") as Long
        val recording = RecordingLoadWebView(activity, "https://h.example")
        activity.setPrivateField("webView", recording)
        controller.newIntent(viewIntent("omnigent://h.example/c/$hex"))

        activity.invokeRetryPinnedOrigin()
        val retryGeneration = activity.privateField("loadGeneration") as Long
        assertEquals(1, recording.reloadCount)
        activity.invokeOnPageReady(
            "https://h.example",
            mainFrameLoadFailed = true,
            loadGeneration = initialGeneration,
        )

        assertEquals("/c/$hex", activity.privateField("pendingNavigatePath"))
        assertTrue(activity.privateField("processingDeepLink") as Boolean)
        activity.invokeOnPageReady("https://h.example", loadGeneration = retryGeneration)
        assertNull(activity.privateField("pendingNavigatePath"))
        assertFalse(activity.privateField("processingDeepLink") as Boolean)
    }

    @Test
    fun `manual server switch supersedes a stale path and resumes FIFO`() {
        store().connect("https://h.example")
        val controller =
            Robolectric
                .buildActivity(
                    MainActivity::class.java,
                    viewIntent("omnigent://h.example/c/first-$hex"),
                ).setup()
        val activity = controller.get()
        controller.newIntent(viewIntent("omnigent://next.example/c/second-$hex"))
        store().connect("https://next.example/mount")

        controller.newIntent(
            Intent().putExtra(ConnectActivity.EXTRA_SERVER_CHANGED, true),
        )

        assertEquals("https://next.example", activity.privateField("pinnedOrigin"))
        assertEquals("/c/second-$hex", activity.privateField("pendingNavigatePath"))
        assertTrue(activity.privateField("processingDeepLink") as Boolean)

        activity.invokeOnPageReady("https://next.example/mount")
        assertFalse(activity.privateField("processingDeepLink") as Boolean)
    }

    @Test
    fun `notification activation waits behind an in-flight deep link`() {
        store().connect("https://h.example")
        val controller =
            Robolectric
                .buildActivity(
                    MainActivity::class.java,
                    viewIntent("omnigent://h.example/c/$hex"),
                ).setup()
        val activity = controller.get()
        val notificationPath = "/c/notification-$hex"

        controller.newIntent(
            Intent().putExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH, notificationPath),
        )

        assertEquals("/c/$hex", activity.privateField("pendingNavigatePath"))
        assertEquals(
            1,
            (activity.privateField("pendingNotifications") as ArrayDeque<*>).size,
        )

        activity.invokeOnPageReady("https://h.example")
        assertTrue(
            shadowOf(activity.testWebView()).lastEvaluatedJavascript.contains(notificationPath),
        )
        assertFalse(activity.privateField("processingDeepLink") as Boolean)
    }

    @Test
    fun `stacked notifications drain after the page becomes ready`() {
        store().connect("https://h.example")
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        val first = "/c/first-notification-$hex"
        val second = "/c/second-notification-$hex"
        controller.newIntent(
            Intent().putExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH, first),
        )
        controller.newIntent(
            Intent().putExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH, second),
        )

        activity.invokeOnPageReady("https://h.example")

        assertNull(activity.privateField("pendingNavigatePath"))
        assertTrue((activity.privateField("pendingNotifications") as ArrayDeque<*>).isEmpty())
        assertTrue(shadowOf(activity.testWebView()).lastEvaluatedJavascript.contains(second))
    }

    @Test
    fun `queued notifications do not cross a deep-link server switch`() {
        store().connect("https://next.example/mount")
        store().connect("https://h.example")
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        val first = "/c/first-notification-$hex"
        val second = "/c/second-notification-$hex"
        val scripts = mutableListOf<String>()
        activity.setPrivateField(
            "webView",
            object : WebView(activity) {
                override fun evaluateJavascript(
                    script: String,
                    resultCallback: ValueCallback<String>?,
                ) {
                    scripts.add(script)
                    super.evaluateJavascript(script, resultCallback)
                }
            },
        )

        controller.newIntent(
            Intent().putExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH, first),
        )
        controller.newIntent(
            Intent().putExtra(NativeNotificationManager.EXTRA_NAVIGATE_PATH, second),
        )
        controller.newIntent(viewIntent("omnigent://next.example/c/deep-link-$hex"))
        activity.invokeOnPageReady("https://next.example/mount")

        assertTrue((activity.privateField("pendingNotifications") as ArrayDeque<*>).isEmpty())
        assertTrue(scripts.any { it.contains("deep-link-$hex") })
        assertFalse(scripts.any { it.contains("notification-$hex") })
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
